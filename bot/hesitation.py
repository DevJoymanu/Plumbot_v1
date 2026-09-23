"""
bot/hesitation.py
=================
A lead hesitating over the visit gets the free online quote; a lead pushing
for an exact figure gets the soft no-guess line with the online quote under it.

WHAT (owner, 2026-09-23, decisions 1B, 6B, D): live chats lead with the visit
(a quick look and a firm price). The online quote is offered when the lead
HESITATES, and only then:

  * reluctant about the visit itself, at any point: "can't you just quote?",
    "do you have to come?", "can I send pictures instead?", "I don't want
    anyone coming yet";
  * a non-answer to the day or visit we just offered: "not sure yet",
    "maybe", "hmm", "I don't know".

    -> "No pressure at all on the visit." then the plumber handoff
       (plumber_link.portfolio_handoff):
       the free online quote first, send photos to the plumber, the pre-filled
       link and why it is long, the number.

  * pushing for an exact figure: "just give me the exact price", "how much
    exactly", "ballpark it".

    -> "We'd rather not guess. A quick look and you get a firm price." /
       "Or if it's easier, send a few photos for a free online quote first."
       then the link and number (plumber_link.link_block).

WHY: a visit is the best close, so it leads; but a lead who does not want one
yet is lost if the only offer is the visit again. The online quote is the
second door, offered once.

WHAT IT DELIBERATELY DOES NOT CATCH: a timeframe ("next month") is the delay
flow, a time clash ("I work weekdays") is other slots, a clear no is a graceful
exit, "let me think about it" / "I'll let you know" are the brush-off and
self-initiated-defer paths, which already carry the portfolio and the handoff.

HOW: deterministic phrase lists (short fuzzy strings are exactly what the
classifier gets wrong), English only until the owner approves the Shona
wording, only while the lead is not booked, only for short messages (a long
message has its own content and goes to the normal flow: the customer's words
win), and ONCE per lead ([HESITATION_ONLINE_OFFERED]); a second hesitation goes
to the normal flow. A lead who already has the plumber's link gets one short
line with his number instead of the whole handoff again. Called from the
webhook's STEP 0-h, after exit detection. Pinned by the "hesitation" cases in
TEST 0 and HesitationTests.
"""

from __future__ import annotations

import re

from . import copy_catalog

OFFERED_TAG = '[HESITATION_ONLINE_OFFERED]'
_MAX_WORDS = 14

_VISIT_RELUCTANT_RE = re.compile(
    r"\b(?:can'?t|cant|can you|could you|won'?t you)\s+(?:you\s+)?just\s+(?:quote|give|send|tell)"
    r"|\bquote\s+(?:me\s+)?(?:without|online|over the phone|here)\b"
    r"|\bwithout\s+(?:you\s+)?(?:coming|visiting|a visit)"
    r"|\b(?:do|must|have)\s+you\s+(?:have\s+to|need\s+to|got\s+to)\s+(?:come|visit)"
    r"|\bis\s+(?:a|the)\s+visit\s+(?:necessary|needed|required)"
    r"|\b(?:can|could)\s+i\s+(?:just\s+)?(?:send|share)\s+(?:you\s+)?(?:pictures|photos|pics|images)"
    r"|\bsend\s+(?:you\s+)?(?:pictures|photos|pics)\s+instead"
    r"|\b(?:don'?t|do not)\s+want\s+(?:anyone|anybody|someone|people|you)\s+(?:coming|to come)"
    r"|\bno\s+(?:need\s+(?:for|to)\s+)?(?:visit|site visit)\b",
    re.IGNORECASE)

_UNSURE_RE = re.compile(
    r"^\s*(?:not sure(?: yet)?|i'?m not sure(?: yet)?|maybe|perhaps|hmm+|mm+|"
    r"i don'?t know(?: yet)?|dunno|not yet sure|unsure)[\s.!?]*$",
    re.IGNORECASE)

_EXACT_FIGURE_RE = re.compile(
    r"\bexact\s+(?:price|figure|cost|amount|quote)"
    r"|\bhow much\s+exactly\b|\bexactly\s+how much\b"
    r"|\b(?:just|please)\s+give\s+me\s+(?:a|the)\s+(?:price|figure|number)"
    r"|\bfinal\s+(?:price|figure)\b|\bfirm\s+price\b|\bballpark\b",
    re.IGNORECASE)

# Our last message offered the visit or a day for it.
_VISIT_OFFER_MARKERS = ('for us to come through', 'quick look', 'come round',
                        'what works better for you', 'which suits',
                        'morning or afternoon', 'suit you better',
                        'book you in', 'come and have a look')


def _last_assistant_text(appointment) -> str:
    for turn in reversed(getattr(appointment, 'conversation_history', None) or []):
        if isinstance(turn, dict) and turn.get('role') == 'assistant':
            return str(turn.get('content', ''))
    return ''


def last_offered_visit(appointment) -> bool:
    text = _last_assistant_text(appointment).lower()
    return any(m in text for m in _VISIT_OFFER_MARKERS)


def is_visit_hesitation(message: str, appointment) -> bool:
    """Reluctance about the visit anywhere, or unsure right after we offered it."""
    text = (message or '').strip()
    if not text or len(text.split()) > _MAX_WORDS:
        return False
    if _VISIT_RELUCTANT_RE.search(text):
        return True
    return bool(_UNSURE_RE.match(text)) and last_offered_visit(appointment)


def pushes_for_exact_figure(message: str) -> bool:
    text = (message or '').strip()
    return bool(text) and len(text.split()) <= _MAX_WORDS and bool(
        _EXACT_FIGURE_RE.search(text))


def reply_for(message: str, appointment):
    """The reply for this turn, or None when this is not a hesitation we handle
    (the caller then carries on with the normal flow). Stamps OFFERED_TAG when
    it answers, so it answers once."""
    from .plumber_link import (LINK_SENT_TAG, _who_handles_quotes, link_block,
                               plumber_number, portfolio_handoff)
    if appointment is None or getattr(appointment, 'status', '') == 'confirmed':
        return None
    notes = getattr(appointment, 'internal_notes', '') or ''
    if OFFERED_TAG in notes:
        return None
    try:
        from .repeated_question_detector import detect_language_simple
        if detect_language_simple(message or '') != 'english':
            return None
    except Exception:
        return None

    exact = pushes_for_exact_figure(message)
    if not exact and not is_visit_hesitation(message, appointment):
        return None
    if not plumber_number(appointment):
        return None     # no plumber number for this tenant: nothing to hand off

    cc = copy_catalog
    if exact:
        block = '' if LINK_SENT_TAG in notes else link_block(appointment)
        reply = f'{cc.FIRM_PRICE_NO_GUESS}\n{cc.FIRM_PRICE_ONLINE}'
        if block:
            reply += f'\n\n{block}'
    elif LINK_SENT_TAG in notes:
        who = _who_handles_quotes(appointment)
        number = f'+{plumber_number(appointment)}'
        reply = (cc.ONLINE_QUOTE_STILL_OPEN.format(who=who, number=number) if who
                 else cc.ONLINE_QUOTE_STILL_OPEN_NAMELESS.format(number=number))
    else:
        reply = f'{cc.HESITATION_ACK}\n\n{portfolio_handoff(appointment)}'

    appointment.internal_notes = f'{notes}\n{OFFERED_TAG}'.strip()
    if LINK_SENT_TAG not in appointment.internal_notes:
        appointment.internal_notes += f'\n{LINK_SENT_TAG}'
    appointment.save(update_fields=['internal_notes'])
    return reply
