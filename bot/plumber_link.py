"""
bot/plumber_link.py
===================
The lead messaging the PLUMBER's own WhatsApp, with a message already written.

WHAT: the link to the plumber's separate number (short, and on WhatsApp's own
domain), the owner's handoff follow-up that carries it, the paragraph the
delay flow puts beside the portfolio.

WHY: the plumber runs on a separate WhatsApp Business app number with no
template protection, so he must never open a cold thread. A link the LEAD taps
makes every conversation on that number inbound. It is the SECOND automatic
follow-up of every silence, for a lead who gave a delay signal or has all three
fields (owner rule, 2026-09-21), and it rides with the portfolio in the delay
flow.

THE LINK HAS TO LOOK SAFE (owner, 2026-09-21): these are leads from a Facebook
ad with their guard up about scams, so the link is WhatsApp's own and short,
never our domain and never a shortener. It still carries a pre-filled message.
The only link that is all three (short, wa.me, pre-filled) is the plumber's
WhatsApp Business "short link" (wa.me/message/...), which he creates in his app
with his own default message; his number is on the app, not the Cloud API, so
we cannot make it for him. It is read from the tenant
(``TenantConfig.plumber_quote_link``). Until one is set (owner, 2026-09-21),
the link is wa.me/<number> with the PER-LEAD pre-fill (service, area, job and
the fixed measurements line), about 330 characters, so the handoff says why it
is long before it and puts it at the bottom. The plumber is NOT emailed on a
handoff (owner, 2026-09-21: not an email for every handoff sent); the per-lead
pre-fill is what tells him who the lead is.

Nothing here sends to a customer. Tenancy: the link and number come from the
lead's OWN tenant (``plumber_contact()``, then its profile); none means no link
and no handoff, never another tenant's. Pinned by "plumber link" in TEST 0.
"""

import logging
import re
from urllib.parse import quote

from . import copy_catalog

logger = logging.getLogger(__name__)

# The fixed line at the end of the per-lead pre-fill (the brief: "Keep the 'I
# can send measurements, photos, and a plan' line FIXED"). It tells the lead
# what to gather and the plumber that this is an online quote, not a call-out.
_ONLINE_QUOTE_LINE = (
    "I'd like a free online quote. I can send measurements, photos, and a plan "
    "of the space so you can quote without coming out. Can you help?"
)

# Written on the lead the first time the link goes out in a conversational
# reply (the delay flow's portfolio branches), so those replies never repeat
# it. The follow-up handoff does not read it: it goes on every silence.
LINK_SENT_TAG = '[PLUMBER_LINK_SENT]'

# The job line in the pre-filled message is one line, not a paragraph.
_DESCRIPTION_MAX_CHARS = 140


def _digits(raw) -> str:
    return ''.join(c for c in str(raw or '') if c.isdigit())


def plumber_number(appointment) -> str:
    """The plumber's number as wa.me wants it (digits only), or ''.

    Read through ``plumber_contact()`` so a per-lead plumber override wins and
    the tenant fallback is the lead's own tenant.
    """
    try:
        return _digits(appointment.plumber_contact())
    except Exception:
        return ''


def _tenant_short_link(appointment) -> str:
    """The plumber's WhatsApp Business short link for the lead's tenant, or ''."""
    try:
        from .tenant_config import get_config
        return get_config(getattr(appointment, 'tenant', None)).plumber_quote_link
    except Exception:
        logger.warning('Could not read the plumber short link', exc_info=True)
        return ''


def _service_phrase(appointment) -> str:
    """'a bathroom renovation', 'an outdoor tap', or 'some plumbing work'."""
    from .lead_handoff import service_label
    label = service_label(appointment).lower()
    if not label:
        return 'some plumbing work'
    return f"{'an' if label[:1] in 'aeiou' else 'a'} {label}"


def lead_voice_message(appointment) -> str:
    """The per-lead pre-fill, in the lead's own voice.

    "Hi, I'm interested in a bathroom renovation in Borrowdale. Full re-tile
    and new fittings. I'd like a free online quote. I can send measurements,
    photos, and a plan of the space so you can quote without coming out. Can
    you help?" Area and description drop out when we do not hold them (and a
    description that is chat is never quoted, bot/job_text); the opening and
    the fixed online-quote line are always there.
    """
    area = ' '.join(str(getattr(appointment, 'customer_area', '') or '').split())
    opening = f"Hi, I'm interested in {_service_phrase(appointment)}"
    opening += f' in {area}.' if area else '.'
    parts = [opening]
    job = _description_line(appointment)
    if job and len(job.split()) >= 2:
        parts.append(job[:1].upper() + job[1:].rstrip('.!?,; ') + '.')
    parts.append(_ONLINE_QUOTE_LINE)
    return ' '.join(parts)


def is_long_link(link) -> bool:
    """True for the long wa.me link that carries the pre-fill in the URL, the
    one the handoff has to explain."""
    return '?text=' in (link or '')


def long_quote_link(appointment) -> str:
    """wa.me/<plumber>?text=<per-lead pre-fill>, or '' with no plumber number.

    What the lead lands on in every case: directly when no shorter link
    exists, and through the business-domain forwarder (bot/short_links.py)
    when one does, which rebuilds it from the lead's current details.
    """
    number = plumber_number(appointment)
    if not number:
        return ''
    return f'https://wa.me/{number}?text={quote(lead_voice_message(appointment), safe="")}'


def quote_link(appointment) -> str:
    """The link the lead SEES, shortest safe form first, or '' with no plumber.

    1. The plumber's own WhatsApp Business short link (wa.me/message/...), one
       fixed pre-fill, once he has made one. A per-lead override number
       outranks it: that link is the tenant's default plumber's.
    2. The business-domain short link (wa.<their domain>/q/<code>), which
       forwards to the per-lead wa.me link, once the tenant's domain is set.
    3. The long per-lead wa.me link itself (owner, 2026-09-21: use it until a
       shorter one exists), ~330 characters, which is why the handoff says why.
    """
    if not plumber_number(appointment):
        return ''
    if not getattr(appointment, 'plumber_contact_number', ''):
        short = _tenant_short_link(appointment)
        if short:
            return short
    from .short_links import short_url
    return short_url(appointment) or long_quote_link(appointment)


# The first sentence of the handoff follow-up. Recognises a handoff in the
# transcript whatever form its link took (wa.me, the plumber's short link, the
# business-domain short link), for the stop-after-handoff check in
# send_followups and the "is this about the link?" detector.
def is_handoff_text(text) -> bool:
    """True when `text` is (or carries) the plumber handoff: the owner's copy,
    or any of the link forms it has ever used."""
    text = text or ''
    return (copy_catalog.HANDOFF_LEAVE_IT_HERE.split('.')[0] in text
            or 'https://wa.me/' in text or 'https://api.whatsapp.com/' in text
            or '/q/' in text and 'https://' in text)


def _who_handles_quotes(appointment) -> str:
    """The plumber's name, else the business name, else ''.

    Names the person because the owner's copy does ("[plumber] handles the
    quotes"): the handoff is TO a person. The name is the lead's own tenant's
    (``plumber_display_name``), never borrowed.
    """
    try:
        name = (appointment.plumber_display_name() or '').strip()
    except Exception:
        name = ''
    if name and name != 'the plumber':
        return name
    from .utils import business_name_for
    return business_name_for(appointment, default='').strip()


def handoff_message(appointment) -> str:
    """The second follow-up of a silence: the owner's handoff copy, or ''.

    Layout (owner, 2026-09-21), the same for a delayed lead and a three-field
    lead:
        the owner's opener
        who handles the quotes, and what to send
        why the link is long and that it is the easiest way (long link only)
        the link, on its own line
        the plumber's number, on its own line, last
    The link and number sit at the BOTTOM so the copy reads before a long URL
    does, and on lines of their own so the outbound chain's sentence passes
    never see them mid-sentence. The explanation is there because a long link
    from a business met on a Facebook ad reads as a scam unless it is said
    why; the plumber's own short link needs none. "he'll send" became "you'll
    get" (see copy_catalog). '' when there is no plumber number (the caller
    sends its ordinary message instead).
    """
    link = quote_link(appointment)
    if not link:
        return ''
    cc = copy_catalog
    who = _who_handles_quotes(appointment)
    handles = cc.HANDOFF_WHO_HANDLES_QUOTES.format(who=who) if who else cc.HANDOFF_QUOTES_HERE
    opens = _link_opens_line(link, who)
    number = f'+{plumber_number(appointment)}'
    contact = (cc.HANDOFF_NUMBER_OF.format(who=who, number=number) if who
               else cc.HANDOFF_NUMBER.format(number=number))
    return f'{cc.HANDOFF_LEAVE_IT_HERE}\n\n{handles}\n\n{opens}\n{link}\n\n{contact}'


def portfolio_handoff(appointment, shona: bool = False) -> str:
    """The plumber handoff that goes WITH the portfolio in the delay flow, or ''.

    WHAT (owner, 2026-09-22): the free online quote offer, what the link opens and
    why it is long, the link on its own line, the number on its own line
    last. The same pieces as handoff_message, minus its "I've messaged a
    couple of times" opener (this is a live reply, not a follow-up), and in
    short lines because the owner asked for short lines here.
    WHY the link AND the number: the pre-filled link is the easy way, the
    number is for a lead wary of a long link or who wants to save it.
    HOW: '' with no plumber number for the lead's own tenant, never another
    tenant's. The link line matches the link actually sent: "why it's long"
    only for the long per-lead link, "a message ready to send" for the
    plumber's own short link, which carries no lead details.
    `shona=True` builds the same pieces from the `_SN` catalog lines, for a
    Shona lead who hesitates or picks "online" after the price guide (both
    were English only, so a Shona lead was never offered the online door).
    The link and its pre-filled message stay English: the pre-fill is written
    to the plumber, not to the lead.
    Pinned by LadderCallPermissionTests and HesitationTests.
    """
    link = quote_link(appointment)
    if not link:
        return ''
    cc = copy_catalog
    who = _who_handles_quotes(appointment)
    if shona:
        handles = (cc.PORTFOLIO_HANDOFF_WHO_SN.format(who=who) if who
                   else cc.PORTFOLIO_HANDOFF_WHO_NAMELESS_SN)
        lines = (cc.PORTFOLIO_HANDOFF_FREE_FIRST_SN, handles,
                 cc.PORTFOLIO_HANDOFF_CERTAINTY_SN)
    else:
        handles = (cc.PORTFOLIO_HANDOFF_WHO.format(who=who) if who
                   else cc.PORTFOLIO_HANDOFF_WHO_NAMELESS)
        lines = (cc.PORTFOLIO_HANDOFF_FREE_FIRST, handles,
                 cc.PORTFOLIO_HANDOFF_CERTAINTY)
    # The free online quote leads (owner: "they can get a free online quote
    # first"), then how, then why it is worth it, each on its own short line.
    offer = '\n'.join(lines)
    return f'{offer}\n\n{link_block(appointment, shona=shona)}'


def link_block(appointment, shona: bool = False) -> str:
    """What the link opens (and why it is long), the link, then the number, in
    short lines; '' with no plumber number. The tail of portfolio_handoff, and
    what the firm-price reply (bot/hesitation.py) puts under its two lines.
    "why it's long" only for the long per-lead link; the plumber's own short
    link carries a fixed message, so it says "a message ready to send".
    `shona=True` picks the `_SN` lines for a Shona lead, with the possessive
    written the Shona way ("yaTakudzwa", else "yedu yequotes"); which line
    goes with which link form is the same in both languages."""
    link = quote_link(appointment)
    if not link:
        return ''
    cc = copy_catalog
    who = _who_handles_quotes(appointment)
    number = f'+{plumber_number(appointment)}'
    if shona:
        whose = f'ya{who}' if who else 'yedu yequotes'
        long_line, ready_line, details_line = (
            cc.PORTFOLIO_LINK_LONG_SN, cc.PORTFOLIO_LINK_READY_SN,
            cc.PORTFOLIO_LINK_DETAILS_SN)
        contact = (cc.HANDOFF_NUMBER_OF_SN.format(who=who, number=number) if who
                   else cc.HANDOFF_NUMBER_SN.format(number=number))
    else:
        whose = f"{who}'s" if who else 'our quotes'
        long_line, ready_line, details_line = (
            cc.PORTFOLIO_LINK_LONG, cc.PORTFOLIO_LINK_READY,
            cc.PORTFOLIO_LINK_DETAILS)
        contact = (cc.HANDOFF_NUMBER_OF.format(who=who, number=number) if who
                   else cc.HANDOFF_NUMBER.format(number=number))
    if is_long_link(link):
        opens = long_line.format(whose=whose)
    elif '/message/' in link:
        opens = ready_line.format(whose=whose)
    else:
        opens = details_line.format(whose=whose)
    return f'{opens}\n{link}\n\n{contact}'


def _link_opens_line(link, who) -> str:
    """The sentence just above the link: whose WhatsApp it opens, and what is
    already in it.

    Owner, 2026-09-21: name the plumber and say the link goes straight to his
    WhatsApp with their details already typed in. The long per-lead link adds
    why it is long; the plumber's own short link carries his fixed message,
    not their details, so it says "a message ready to send" instead (never a
    claim the link cannot keep). No name on file reads "our quotes WhatsApp".
    """
    cc = copy_catalog
    whose = f"{who}'s" if who else 'our quotes'
    if is_long_link(link):
        return f'{cc.LINK_OPENS_WITH_DETAILS.format(whose=whose)} {cc.LINK_WHY_LONG_TAIL}'
    if '/message/' in (link or ''):
        return cc.LINK_OPENS_READY.format(whose=whose)
    # The business-domain short link forwards to the per-lead wa.me link, so
    # their details ARE in it; it is only not long.
    return cc.LINK_OPENS_WITH_DETAILS.format(whose=whose)


# ── A lead asking about, or wary of, the link ───────────────────────────────
# Owner, 2026-09-21: these are Facebook-ad leads with their guard up, and a
# ~330-character link invites "what is this?" or "is this a scam?". Answer it
# plainly (what it opens, why it is long) and give the plumber's number so they
# can skip the link; the webhook sends his contact card after the text.
#
# Deterministic on purpose: a short worried question right after we sent a link
# is exactly the fuzzy string the classifier gets wrong, and the answer is
# fixed copy. Two gates, both required: we sent the link in one of our last few
# messages, and this message asks about it or about trusting it.

# Mentions the link itself.
_LINK_WORD_RE = re.compile(r"\b(?:link|url|linki|rinki)\b", re.IGNORECASE)
# Asks what it is, why it looks as it does, or whether to trust it.
_LINK_QUESTION_RE = re.compile(
    r"\b(?:what|why|how|which|whose|long|safe|scam|legit|legitimate|real|genuine"
    r"|trust|fake|hack|hacked|virus|suspicious|sketchy|dodgy|click|clicking|tap"
    r"|tapping|open|opening|chii|sei|ndeyei|nderei)\b|\?", re.IGNORECASE)
# Worry that stands on its own, with or without the word "link".
_LINK_WORRY_RE = re.compile(
    r"\b(?:scam|scammer|fraud|legit|hacked|hack\s+me|virus|phishing)\b"
    r"|\bis\s+this\s+(?:safe|real|legit|genuine|you)\b"
    r"|\b(?:not|won'?t|don'?t\s+want\s+to|never)\s+(?:click|clicking|tap|tapping|open)\b",
    re.IGNORECASE)
# How far back our link may be: the last few of OUR messages.
_LINK_LOOKBACK = 4


def _link_recently_sent(appointment) -> bool:
    """Did one of our last few messages carry the plumber link?"""
    history = getattr(appointment, 'conversation_history', None) or []
    ours = [m for m in history if isinstance(m, dict) and m.get('role') == 'assistant']
    return any(is_handoff_text(m.get('content') or '') for m in ours[-_LINK_LOOKBACK:])


def asks_about_the_link(message, appointment) -> bool:
    """True when the lead is asking what the plumber link is, or is wary of it.

    Only once we have sent it (one of our last few messages): "what is this?"
    means nothing about a link that was never sent. Then either the message
    names the link and asks about it ("what's this link for", "why is the
    link so long"), or it voices the worry on its own ("is this a scam", "I'm
    not clicking that"). Pinned by the "link question" cases in TEST 0.
    """
    text = message or ''
    if not text.strip() or not _link_recently_sent(appointment):
        return False
    if _LINK_WORRY_RE.search(text):
        return True
    return bool(_LINK_WORD_RE.search(text) and _LINK_QUESTION_RE.search(text))


def contact_card_name(appointment) -> str:
    """The name on the plumber's contact card: his, else the business's, else
    "Quotes". Never another tenant's."""
    return _who_handles_quotes(appointment) or 'Quotes'


def link_explanation(appointment) -> str:
    """What the link is, why it is long, and the plumber's number, or '' when
    there is no plumber number (nothing to explain or offer).

    The "why it is long" sentence only goes with the long per-lead wa.me link.
    Statements only: the lead asked a question and this answers it, so no
    tie-down is added, and the number is the way forward.
    """
    number = plumber_number(appointment)
    if not number:
        return ''
    cc = copy_catalog
    who = _who_handles_quotes(appointment)
    what = cc.LINK_WHAT_IT_IS.format(who=who) if who else cc.LINK_WHAT_IT_IS_NAMELESS
    why = cc.LINK_WHY_LONG if is_long_link(quote_link(appointment)) else cc.LINK_NOTHING_ELSE
    direct = (cc.LINK_OR_MESSAGE_DIRECTLY.format(who=who, number=f'+{number}') if who
              else cc.LINK_OR_MESSAGE_DIRECTLY_NAMELESS.format(number=f'+{number}'))
    return f'{what} {why}\n\n{direct}'


def quote_offer(appointment) -> str:
    """The paragraph the delay flow puts beside the portfolio, link on its own
    line, or '' with no plumber number."""
    link = quote_link(appointment)
    if not link:
        return ''
    # Names the plumber and says where the link goes, like the handoff (owner,
    # 2026-09-21); the link stays last, on its own line.
    who = _who_handles_quotes(appointment)
    offer = (copy_catalog.PLUMBER_QUOTE_OFFER.format(who=who) if who
             else copy_catalog.PLUMBER_QUOTE_OFFER_NAMELESS)
    return f'{offer}\n\n{_link_opens_line(link, who)}\n{link}'


def _description_line(appointment) -> str:
    """The job in one line for the pre-filled message, or '' when it is chat.

    First line only (a stored description is often the lead's messages joined),
    chat dropped through bot/job_text, cut at a word boundary.
    """
    first_line = next((ln for ln in str(getattr(appointment, 'project_description', '')
                                      or '').splitlines() if ln.strip()), '')
    raw = ' '.join(first_line.split())
    from .job_text import describes_a_job
    if not raw or not describes_a_job(raw):
        return ''
    if len(raw) > _DESCRIPTION_MAX_CHARS:
        raw = raw[:_DESCRIPTION_MAX_CHARS].rsplit(' ', 1)[0].rstrip(',;: ')
    return raw
