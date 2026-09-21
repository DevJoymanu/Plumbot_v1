"""
bot/plumber_link.py
===================
The lead messaging the PLUMBER's own WhatsApp, with the message already written.

WHAT: a ``wa.me`` deep link to the plumber's separate number, carrying a
pre-filled message in the LEAD's voice that asks for a free online quote, plus
the one customer-facing paragraph that offers it.

WHY: the plumber runs on a separate WhatsApp Business app number with no
template protection, so he must never open a cold thread. A link the LEAD taps
makes every conversation on that number inbound. It is the shared escape hatch
of the handoff brief (owner, 2026-09-21): sent up front, beside the portfolio,
on every delay signal (Rule 1), and as the second follow-up of a lead who went
quiet once the three fields were in (Rule 2, the last resort there).

HOW: template slots, never generation. No model call happens at the handoff:
the message is interpolated from the lead's row and URL-encoded. The
measurements line is FIXED because it is what tells the lead to gather those
things and tells the plumber this is an online quote, not a call-out. A thin
field drops its sentence rather than leaving an empty bracket.

Nothing here sends anything. Callers put ``quote_offer`` into a reply that goes
through the normal outbound chain.

Tenancy: the number is ``Appointment.plumber_contact()`` (per-lead override,
then the lead's OWN tenant). No number means no link and no offer, never
another tenant's number. Pinned by the ``plumber link`` cases in TEST 0.
"""

import re
from urllib.parse import quote

from . import copy_catalog
from .lead_handoff import service_label

# The fixed sentence (brief: "Keep the 'I can send measurements, photos, and a
# plan' line FIXED"). The brief's dash became a full stop: no dash punctuation
# in anything a customer writes or reads.
_ONLINE_QUOTE_LINE = (
    "I'd like a free online quote. I can send measurements, photos, and a plan "
    "of the space so you can quote without coming out. Can you help?"
)

# Written on the lead the first time the link goes out, by whichever path sends
# it (the delay flow's portfolio branches, or the ghosted lead's second
# follow-up), so no path sends it twice.
LINK_SENT_TAG = '[PLUMBER_LINK_SENT]'

# The description is one line, not a paragraph pasted into somebody else's
# mouth. Cut at a word boundary under this length.
_DESCRIPTION_MAX_CHARS = 140


def _digits(raw) -> str:
    return ''.join(c for c in str(raw or '') if c.isdigit())


def plumber_number(appointment) -> str:
    """The plumber's number as wa.me wants it (digits only), or ''.

    Read through ``plumber_contact()`` so a per-lead plumber override wins and
    the tenant fallback is the lead's own tenant. '' when neither is set, and
    every caller then omits the offer.
    """
    try:
        return _digits(appointment.plumber_contact())
    except Exception:
        return ''


def _service_phrase(appointment) -> str:
    """'a bathroom renovation', 'an outdoor tap', or 'some plumbing work'.

    The service TYPE, as ``lead_handoff.service_label`` words it ('other' is
    not a service and reads as blank). The fallback keeps the sentence whole
    instead of leaving "interested in a  in Borrowdale".
    """
    label = service_label(appointment).lower()
    if not label:
        return 'some plumbing work'
    article = 'an' if label[:1] in 'aeiou' else 'a'
    return f'{article} {label}'


def _description_line(appointment) -> str:
    """One sentence from the project description, or '' when it is thin.

    Thin means empty, fewer than two words, or nothing but the service label
    said again ("Bathroom renovation. Bathroom renovation."). The first line
    or sentence only, cut at a word boundary, first letter up, full stop on.
    """
    # The FIRST LINE only, before whitespace is collapsed: a stored description
    # is often the lead's messages joined ("toilet, Ruwa\nHow much"), and
    # collapsing first ran them into one sentence ("Toilet, Ruwa How much.").
    first_line = next((ln for ln in str(getattr(appointment, 'project_description', '')
                                      or '').splitlines() if ln.strip()), '')
    raw = ' '.join(first_line.split())
    if not raw:
        return ''
    # A stray chat reply saved as the job ("Ok\nNow you are talking") says
    # nothing about the work, and put into the lead's mouth it reads as
    # nonsense. Appointment.save no longer stores one; this covers rows
    # written before that, through the same rule (bot/job_text.py).
    from .job_text import describes_a_job
    if not describes_a_job(raw):
        return ''
    # The first sentence: a description is often "new tub. also the geyser
    # leaks. and the toilet runs", and one line is what the brief asks for.
    first = re.split(r'(?<=[.!?])\s+', raw, maxsplit=1)[0].strip().rstrip('.!?,; ')
    if len(first) > _DESCRIPTION_MAX_CHARS:
        first = first[:_DESCRIPTION_MAX_CHARS].rsplit(' ', 1)[0].rstrip(',;: ')
    if len(first.split()) < 2:
        return ''
    if first.lower() == service_label(appointment).lower():
        return ''
    # Spoken copy: nobody types "&" as a word in a sentence to a stranger.
    first = first.replace('&', 'and')
    return first[:1].upper() + first[1:] + '.'


def lead_voice_message(appointment) -> str:
    """The pre-filled message, in the lead's own voice.

    "Hi, I'm interested in a bathroom renovation in Borrowdale. Full re-tile
    and new fittings. I'd like a free online quote. I can send measurements,
    photos, and a plan of the space so you can quote without coming out. Can
    you help?"

    Area and description each drop out when we do not hold them; the opening
    and the fixed online-quote line are always there.
    """
    area = ' '.join(str(getattr(appointment, 'customer_area', '') or '').split())
    opening = f"Hi, I'm interested in {_service_phrase(appointment)}"
    opening += f' in {area}.' if area else '.'
    parts = [opening]
    description = _description_line(appointment)
    if description:
        parts.append(description)
    parts.append(_ONLINE_QUOTE_LINE)
    return ' '.join(parts)


def quote_link(appointment) -> str:
    """``https://wa.me/<plumber>?text=<encoded message>``, or '' with no number.

    ``quote(..., safe='')`` encodes every space and punctuation mark, so the
    link survives being pasted into a WhatsApp message whole: WhatsApp ends a
    link at the first raw space.
    """
    number = plumber_number(appointment)
    if not number:
        return ''
    text = quote(lead_voice_message(appointment), safe='')
    return f'https://wa.me/{number}?text={text}'


def handoff_message(appointment, delayed: bool = False) -> str:
    """The second follow-up: the plumber handoff, or '' with no plumber number.

    WHAT: a one-line opener, then `quote_offer` (the free-online-quote
    paragraph and the link).
    WHY two openers (owner rule, 2026-09-21): two groups get this as their
    second follow-up. A lead who gave a delay signal is told there is no rush
    and here is a way to get a price while they plan; a lead with all three
    fields who went quiet after the booking ask is told there is a way to get
    a price if a visit does not suit. Both are statements, so the offer's link
    is the last thing in the message.
    HOW: '' when `quote_offer` is '' (no plumber number for the lead's own
    tenant), and the caller then sends its ordinary message instead.
    """
    offer = quote_offer(appointment)
    if not offer:
        return ''
    name = (getattr(appointment, 'customer_name', '') or '').strip()
    hi = f'Hi {name}' if name else 'Hi there'
    if delayed:
        opener = (f"{hi}, no rush at all on the timing. If it helps while you "
                  "plan, there's another way to get your price.")
    else:
        opener = (f"{hi}, if a visit doesn't suit right now, there's another way "
                  "to get your price.")
    return f'{opener}\n\n{offer}'


def quote_offer(appointment) -> str:
    """The paragraph that offers the plumber's line, link on its own line.

    '' when the tenant has no plumber number, so the caller's reply simply
    goes out without it. The link sits on a line of its own so the outbound
    chain's sentence-level passes (the free-visit stripper splits on sentence
    ends) never see it inside a sentence.
    """
    link = quote_link(appointment)
    if not link:
        return ''
    return f'{copy_catalog.PLUMBER_QUOTE_OFFER}\n\n{link}'
