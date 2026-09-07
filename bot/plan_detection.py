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
