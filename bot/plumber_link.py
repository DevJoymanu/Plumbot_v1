"""
bot/plumber_link.py
===================
The lead messaging the PLUMBER's own WhatsApp, with a message already written.

WHAT: the link to the plumber's separate number (short, and on WhatsApp's own
domain), the owner's handoff follow-up that carries it, the paragraph the
delay flow puts beside the portfolio, and the heads-up email that tells the
plumber who is about to message him.

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
is long before it and puts it at the bottom. The plumber is also emailed the
lead's details at the moment of handoff (``notify_plumber_of_handoff``), which
matters most once the short link's generic pre-fill replaces this one.

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

# The job line in the plumber's heads-up email is one line, not a paragraph.
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


def is_short_link(link) -> bool:
    """True for the plumber's own WhatsApp Business short link."""
    return '/message/' in (link or '')


def quote_link(appointment) -> str:
    """The link the lead taps, or '' with no plumber number.

    1. The plumber's own short link (wa.me/message/..., his pre-filled text),
       once he has made one and it is stored on the tenant.
    2. Until then (owner, 2026-09-21): wa.me/<number> carrying the PER-LEAD
       pre-fill, so the message the plumber receives already says the job.
       Long (~330 characters), which is why the handoff explains it.
    A per-lead override number (``plumber_contact_number``) outranks the
    tenant's short link, because that link belongs to the tenant's default
    plumber, not the one assigned to this lead.
    """
    number = plumber_number(appointment)
    if not number:
        return ''
    if not getattr(appointment, 'plumber_contact_number', ''):
        short = _tenant_short_link(appointment)
        if short:
            return short
    return f'https://wa.me/{number}?text={quote(lead_voice_message(appointment), safe="")}'


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
    why = cc.HANDOFF_TAP_THE_LINK if is_short_link(link) else cc.HANDOFF_WHY_LINK_IS_LONG
    number = f'+{plumber_number(appointment)}'
    contact = (cc.HANDOFF_NUMBER_OF.format(who=who, number=number) if who
               else cc.HANDOFF_NUMBER.format(number=number))
    return f'{cc.HANDOFF_LEAVE_IT_HERE}\n\n{handles}\n\n{why}\n{link}\n\n{contact}'


def quote_offer(appointment) -> str:
    """The paragraph the delay flow puts beside the portfolio, link on its own
    line, or '' with no plumber number."""
    link = quote_link(appointment)
    if not link:
        return ''
    return f'{copy_catalog.PLUMBER_QUOTE_OFFER}\n\n{link}'


def _description_line(appointment) -> str:
    """The job in one line for the plumber's email, or '' when it is chat.

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


def notify_plumber_of_handoff(appointment, dry_run=False) -> bool:
    """Email the plumber that this lead has just been given his link.

    WHY: the short link carries a generic pre-fill, so the message he gets is
    "Hi I would like a free quote" from a number he does not know. This puts
    the lead's details in front of him first, so he can answer as someone who
    already knows the job. Internal copy, so it names people plainly.
    HOW: the normal plumber-alert channel (the lead's own tenant's inbox);
    never raises, returns whether the email went.
    """
    try:
        from .lead_handoff import service_label
        from .plumber_notifications import send_plumber_notification_email
        name = (getattr(appointment, 'customer_name', '') or '').strip()
        phone = _digits(getattr(appointment, 'phone_number', ''))
        label = name or (f'+{phone}' if phone else 'A lead')
        lines = [
            f'{label} has just been sent your WhatsApp link for a free quote, so '
            'they may message you directly. Their details, so you know who it is:',
            '',
            f'Name: {name or "not given"}',
            f'WhatsApp: +{phone}' if phone else 'WhatsApp: not given',
            f'Service: {service_label(appointment) or "not given"}',
            f'Area: {getattr(appointment, "customer_area", "") or "not given"}',
            f'Job: {_description_line(appointment) or "not given"}',
            f'Email: {getattr(appointment, "customer_email", "") or "not given"}',
            '',
            'They were asked to send photos, measurements or a rough plan and what '
            'they need done, for a clear price as a PDF.',
        ]
        return bool(send_plumber_notification_email(
            f'[Quote lead] {label} may message you for a free quote',
            '\n'.join(lines), dry_run=dry_run,
            tenant=getattr(appointment, 'tenant', None), appointment=appointment,
        ))
    except Exception:
        logger.exception('Plumber handoff heads-up failed for apt %s',
                         getattr(appointment, 'pk', None))
        return False
