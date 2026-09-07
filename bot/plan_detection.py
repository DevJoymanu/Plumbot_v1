"""
bot/plan_detection.py
=====================
Which product path an inbound file puts the lead on (spec §10.1).

Two routes to a quote. A lead who sends a real plan does not need the measure
up visit, because the measurements are already on the drawing: the plumber
quotes off it. A lead who sends a photo of their bathroom is still on the
measure path, and that photo is context, not a quotable document.

Getting this wrong costs money in both directions. Reading a plan as a photo
sends someone a $10 call-out for work their architect already did. Reading a
photo as a plan sends the plumber something they cannot quote from.

Nothing here calls an API. The judgement is made from the description vision
already produced (`services/vision`), which is asked to say when it is looking
at a drawing, and from the file's MIME type. Vision stays one call per image,
per the spec's invariant.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PLAN_PDF = 'plan_pdf'
PLAN_IMAGE = 'plan_image'
PHOTO = 'photo'
AMBIGUOUS = 'ambiguous'

MEDIA_KINDS = frozenset({PLAN_PDF, PLAN_IMAGE, PHOTO, AMBIGUOUS})

# Kinds that put the lead on the PLAN path and get emailed to the plumber.
PLAN_KINDS = frozenset({PLAN_PDF, PLAN_IMAGE})

_PDF_MIMES = frozenset({'application/pdf', 'application/x-pdf'})

# Words that hint at a drawing but do not settle it. "drawing" on its own is
# what vision says about a sketch of a tub, and "layout" is what it says about
# how a bathroom is arranged in a photograph. `_description_is_a_plan` rejects
# both on purpose, and until now they fell through as ordinary photos with
# nobody asking. One question is cheaper than either mistake.
_MAYBE_PLAN_WORDS = (
    'drawing', 'sketch', 'layout', 'diagram', 'plan', 'drawn', 'blueprint',
)

# If vision named real fixtures in the room, it was looking at a room. That
# outranks a stray "layout", so an ordinary bathroom photo is never queried.
_ROOM_WORDS = (
    'photo', 'photograph', 'wall', 'floor tile', 'tiled', 'ceiling',
    'installed', 'fitted', 'existing', 'condition', 'leak', 'rust',
    'damaged', 'broken', 'dirty', 'mounted',
)


def classify_media_kind(mime_type: str = '', description: str = '',
                        is_plan_document: bool = False) -> str:
    """What did the customer just send us?

    `is_plan_document` is the caller's own earlier verdict (the media handler
    already treats a PDF as a plan); it is honoured so this function can be
    added without contradicting a decision already made upstream.
    """
    mime = (mime_type or '').lower().split(';')[0].strip()
    if mime in _PDF_MIMES:
        return PLAN_PDF
    if is_plan_document:
        return PLAN_PDF

    text = (description or '').strip().lower()
    if not text:
        # Vision did not run or failed. The handler already fails open here and
        # treats it as a photo, which is the safer of the two wrong answers: it
        # keeps the lead on a flow that works instead of asking a question
        # about a file we cannot see.
        return PHOTO

    from bot.whatsapp_webhook import _description_is_a_plan
    if _description_is_a_plan(description):
        return PLAN_IMAGE

    if any(w in text for w in _MAYBE_PLAN_WORDS) and not any(
            w in text for w in _ROOM_WORDS):
        return AMBIGUOUS

    return PHOTO


def is_plan_kind(kind: str) -> bool:
    """True for the kinds that go to the plumber as something to quote from."""
    return kind in PLAN_KINDS


def plan_clarifier(is_shona: bool = False) -> str:
    """The one question we ask when we cannot tell.

    One question, their words back. Never "please clarify the nature of the
    attachment": we are asking a person who just sent a picture whether it is a
    drawing or the room.
    """
    if is_shona:
        return 'Ndeyeplan here iyi, kana kuti mufananidzo wenzvimbo?'
    return 'Is that a plan, or a photo of the space?'


# ── Whose document is this? ──────────────────────────────────────────────────
# `is_plan_document` is only `mime == application/pdf`, and a PDF is not
# evidence of anything. Both failure directions have happened:
#
#   too loose  barmak 966 — a SUPPLIER opened with "I'm Primrose from Edenvine
#              construction, a leading supplier" and sent "our catalogue". It
#              was filed as their plan, and the bot sent them fifteen photos of
#              Barmak's own work asking which they liked.
#   too tight  barmak 1012 — a real customer sent their plan unprompted as a
#              PDF. Requiring vision to confirm it (vision cannot read PDFs)
#              left them off the plan path, and the bot answered their
#              quotation request with "send us the plan" — which they had.
#
# The signal that separates them is not in the file, it is in the conversation.
# So ask, the way the rest of this codebase asks: model first, keywords when
# the API is down.

_VENDOR_WORDS = (
    'supplier', 'suppliers', 'we supply', 'we sell', 'distributor',
    'wholesale', 'wholesaler', 'our catalogue', 'our catalog',
    'our products', 'our price list', 'we manufacture', 'trade prices',
    'partnership', 'stockist',
)


def _vendor_keywords(appointment) -> bool:
    """Does the customer's own side of the conversation read as a sales pitch?"""
    said = ' '.join(
        str(e.get('content') or '')
        for e in (getattr(appointment, 'conversation_history', None) or [])
        if isinstance(e, dict) and e.get('role') == 'user'
    ).lower()
    return any(w in said for w in _VENDOR_WORDS)


def is_their_own_plan(appointment, asked_for_it=False,
                      description: str = '') -> bool:
    """Is this document the customer's plan, or someone selling to us?

    Cheap short-circuits first, because a document arriving is rare and most of
    them need no call at all:
      * we ASKED for a plan, so it is one;
      * vision looked at it and saw a drawing.

    Otherwise the conversation decides. A wrong YES chases the plumber about a
    sales pitch; a wrong NO asks a customer for a plan they already sent. The
    second is the commoner event and the cheaper mistake, so an unreadable
    answer resolves to YES unless the keywords say vendor.
    """
    if asked_for_it:
        return True
    if description and _description_is_a_plan_safe(description):
        return True

    if _vendor_keywords(appointment):
        return False

    try:
        from bot.services.clients import deepseek_call
        said = '\n'.join(
            'CUSTOMER: %s' % str(e.get('content') or '')[:200]
            for e in (getattr(appointment, 'conversation_history', None) or [])
            if isinstance(e, dict) and e.get('role') == 'user'
        )[-1500:]
        raw = deepseek_call(
            [{'role': 'system', 'content':
              'A plumbing company received a document on WhatsApp. From what '
              'this person has said, are they a CUSTOMER sending their own '
              'building plan or drawing for us to quote, or a SUPPLIER or '
              'company sending us their catalogue, price list or a pitch?\n'
              # The literal word "json" must appear somewhere in the prompt or
              # the API refuses the request: 400, "Prompt must contain the word
              # 'json' in some form to use 'response_format' of type
              # 'json_object'". JSON syntax in the prompt is not enough.
              'Reply with json only: {"is_customer_plan": true} or '
              '{"is_customer_plan": false}.'},
             {'role': 'user', 'content': said or '(they have said nothing)'}],
            temperature=0.0, max_tokens=40, json_response=True,
            retries=1, timeout=10,
        )
        import json
        return bool(json.loads(raw).get('is_customer_plan', True))
    except Exception:
        logger.warning('Could not classify the document sender; '
                       'treating it as the customer plan', exc_info=True)
        return True


def _description_is_a_plan_safe(description: str) -> bool:
    try:
        from bot.whatsapp_webhook import _description_is_a_plan
        return bool(_description_is_a_plan(description))
    except Exception:
        return False
