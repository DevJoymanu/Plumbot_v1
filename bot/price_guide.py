"""
bot/price_guide.py
==================
A general price question from a lead who has told us the job: the price guide
PDF, then "a quick look at the space, or a quote online first?".

WHAT (owner, 2026-09-21): once a lead has given the three fields (service
type, description, area), a GENERAL price question ("I need prices first",
"how much?", "what are your prices?") gets the portfolio / price-guide PDF on
WhatsApp instead of a price block, then one choice question. "Online" sends
the plumber handoff (his WhatsApp, their details already typed in); "a look"
gets the booking question. A price question that names one item ("how much is
a tub?") still gets that item's price, and a lead who has not given the three
fields yet keeps the ordinary pricing reply.

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


def three_fields(appointment) -> bool:
    """Service type (a real one, not 'other'), description and area are in."""
    from .lead_handoff import service_label
    return bool(service_label(appointment)
                and str(getattr(appointment, 'project_description', '') or '').strip()
                and str(getattr(appointment, 'customer_area', '') or '').strip())


def applies(message, appointment, plumbot) -> bool:
    """Is this a GENERAL price question from a lead with the three fields?

    General: it asks for a price (the shared `_asks_price_figure`) and names
    no single product (`_keyword_product_intent`), so "how much is a tub?"
    keeps its item price. A lead who has already booked is not sent a price
    guide: they have committed (never re-pitch a committed lead).
    """
    if getattr(appointment, 'status', '') == 'confirmed':
        return False
    if not three_fields(appointment):
        return False
    try:
        if not plumbot._asks_price_figure(message):
            return False
    except Exception:
        return False
    try:
        from .whatsapp_webhook import _keyword_product_intent
        if _keyword_product_intent(message, plumbot.tenant_cfg):
            return False
    except Exception:
        pass
    return True


def read_choice(message) -> str:
    """'visit', 'online', or '' when the reply does not pick one clearly.

    Both options named (or neither) is not a choice, so '' and the ordinary
    flow answers what they said.
    """
    text = message or ''
    visit = bool(_VISIT_RE.search(text))
    online = bool(_ONLINE_RE.search(text))
    if visit == online:
        return ''
    return 'visit' if visit else 'online'


def intro_line(appointment) -> str:
    """The line before the PDF, or the "already sent" line when it is in the
    chat already (the PDF is never sent twice by this flow)."""
    from . import copy_catalog
    if '[LEAD_MAGNET_WA_SENT]' in (getattr(appointment, 'internal_notes', '') or ''):
        return copy_catalog.PRICE_GUIDE_ALREADY_SENT
    return copy_catalog.PRICE_GUIDE_INTRO
