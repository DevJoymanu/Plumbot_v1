"""
bot/price_guide.py
==================
A general price question from a lead who has told us the job: the price guide
PDF, then "a quick look at the space, or a quote online first?".

WHAT (owner, 2026-09-21, widened 2026-09-22): once a lead has given the three
fields (service type, description, area), EVERY price question gets, in
order: the approximate prices (the highlighted photo's own prices, else the
items named in the message, else the job they described), the portfolio /
price-guide PDF, then one choice question. A lead who already has the PDF
gets the prices and the question. A GENERAL ask ("I need prices first")
prices the job they described (owner, 2026-09-22; it was PDF only before),
and is PDF and question only when nothing on file can be priced. "Online" sends the plumber handoff (his WhatsApp, their
details already typed in); "a look" gets the booking question. A lead who has
not given the three fields yet keeps the ordinary pricing reply.

WHY: the lead magnet carries the whole price picture and the past work, which
a chat price block cannot, and the choice turns "prices first" into a next
step the lead picks rather than a number they go quiet on.

HOW: `applies` is the deterministic gate (webhook STEP 1d, before the pricing
steps); `CHOICE_TAG` marks that the question is open, and `read_choice` reads
the answer on the next turn (webhook STEP 0-b), deterministic, English and
common Shona. The tag is cleared on that next turn whatever it says, so it
never holds a lead: the customer's own words outrank it.

Pinned by the "price guide" cases in TEST 0 and PriceGuideTests.
"""

import re

CHOICE_TAG = '[PRICE_CHOICE_PENDING]'

# "A quick look at the space": the first option.
_VISIT_RE = re.compile(
    r"\b(?:visit|come|coming|look|see\s+(?:it|the|my)|in\s+person|physical(?:ly)?|site"
    r"|personal|personali[sz]ed|first\s+(?:one|option)|1st|option\s*1|muuye|uyai"
    r"|kuuya|mauya)\b", re.IGNORECASE)
# "A quote online first": the second option.
_ONLINE_RE = re.compile(
    r"\b(?:online|on\s*line|whatsapp|here|chat|photos?|pictures?|pics|second\s+(?:one|option)"
    r"|2nd|option\s*2|remote(?:ly)?|pa\s*online)\b", re.IGNORECASE)
# A bare yes to the choice: taken as the first option, the visit (read_choice).
_BARE_YES_RE = re.compile(
    r"^\s*(?:yes|yes please|yeah|yep|yup|ya|yah|sure|definitely|absolutely"
    r"|hongu|ehe|ehoi)\s*[.!]*\s*$", re.IGNORECASE)


def three_fields(appointment) -> bool:
    """Service type (a real one, not 'other'), description and area are in."""
    from .lead_handoff import service_label
    return bool(service_label(appointment)
                and str(getattr(appointment, 'project_description', '') or '').strip()
                and str(getattr(appointment, 'customer_area', '') or '').strip())


def applies(message, appointment, plumbot, price_asked=None) -> bool:
    """Is this a price question from a lead with the three fields?

    EVERY price question counts (owner, 2026-09-22): a general one, one naming
    an item ("how much is a tub?") and one on a highlighted photo ("this one
    how much") all get the full sequence: the prices, the price guide PDF,
    then the online-or-visit question. It used to exclude a named item, which
    kept that item's price block and skipped the guide. `price_asked` lets a
    caller that has already decided it is a price ask (the quoted-photo step,
    where "how mucu" defeats the keyword check) say so. A lead who has already
    booked is not sent a price guide: they have committed (never re-pitch a
    committed lead).
    """
    if getattr(appointment, 'status', '') == 'confirmed':
        return False
    if not three_fields(appointment):
        return False
    if price_asked is None:
        try:
            price_asked = plumbot._asks_price_figure(message)
        except Exception:
            price_asked = False
    return bool(price_asked)


def pdf_already_sent(appointment) -> bool:
    """The portfolio PDF is already in this chat (it is never sent twice)."""
    return '[LEAD_MAGNET_WA_SENT]' in (getattr(appointment, 'internal_notes', '') or '')


def read_choice(message) -> str:
    """'visit', 'online', or '' when the reply does not pick one clearly.

    Both options named (or neither) is not a choice, so '' and the ordinary
    flow answers what they said. A bare yes ("Yes", "yes please", "hongu") is
    the visit (owner, 2026-09-25): the quick look is the first option and the
    one we lead with, and '' there sent Barmak lead 1236 the same question
    again. Only a yes and nothing else, every line of a batched turn; a mere
    acknowledgement ("ok", "thanks") is not a choice and stays ''.
    """
    text = message or ''
    visit = bool(_VISIT_RE.search(text))
    online = bool(_ONLINE_RE.search(text))
    if visit == online:
        lines = [l for l in text.splitlines() if l.strip()]
        if not visit and lines and all(_BARE_YES_RE.match(l) for l in lines):
            return 'visit'
        return ''
    return 'visit' if visit else 'online'


def intro_line(appointment, shona: bool = False) -> str:
    """The line before the PDF, or the "already sent" line when it is in the
    chat already (the PDF is never sent twice by this flow). `shona=True`
    gives the `_SN` pair for a Shona lead."""
    from . import copy_catalog as cc
    if pdf_already_sent(appointment):
        return cc.PRICE_GUIDE_ALREADY_SENT_SN if shona else cc.PRICE_GUIDE_ALREADY_SENT
    return cc.PRICE_GUIDE_INTRO_SN if shona else cc.PRICE_GUIDE_INTRO
