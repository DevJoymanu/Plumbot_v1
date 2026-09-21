"""
bot/job_date_ladder.py
======================
The follow-ups a delayed lead gets once they have named a date more than a
week out (handoff brief, Rule 1, owner 2026-09-21).

WHAT: a lead says "in a month" and that resolves to a JOB DATE. We then:

  1. ask, in the delay flow, whether we may follow up on job date minus 7 days,
     stated as a real date ("the 14th of October"), with the reason: time to go
     over a quote, then reach back out in good time for the job;
  2. follow up at job - 7 days, and again at job - 3 days (email when we have
     one, else WhatsApp when the free window happens to be open);
  3. if they have not answered either touch by job - 2 days, hand the PLUMBER
     a phone-call brief: the lead's details and the call script, routed to the
     quote-given (B1) or no-quote (B2) branch automatically.

WHY a separate ladder rather than the reactivation loop's two touches: that
loop fires on the agreed date and again four days later with a "last check"
email, which is the wrong shape here. These touches sit BEFORE the date the
lead named, and the last step is a person on the phone, not a third email.

HOW: state lives in internal_notes tags (no migration): ``[JOB_DATE]`` the
date, ``[JOB_LADDER]`` the next step (0 to 3), ``[JOB_LADDER_T1]`` when the
first touch went out (a reply after that is what cancels the call). The delay
flow arms it (``arm``); ``send_followups._process_job_date_ladder`` walks it
each tick. Dates under 8 days out never arm it: those keep the existing
near-date rules (the booking pivot, or a gracious park for "I'll get in touch").

Pinned by the ``job ladder`` cases in TEST 0.
"""

import logging
import re
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# Days before the job date for each step. The owner's numbers, in one place.
FIRST_TOUCH_DAYS = 7
SECOND_TOUCH_DAYS = 3
PLUMBER_CALL_DAYS = 2

# Steps, as stored in [JOB_LADDER]. DONE means nothing left to do.
STEP_FIRST, STEP_SECOND, STEP_CALL, STEP_DONE = 0, 1, 2, 3

JOB_DATE_TAG = '[JOB_DATE]'
STEP_TAG = '[JOB_LADDER]'
FIRST_SENT_TAG = '[JOB_LADDER_T1]'

# The transcript prefix for a ladder touch. Listed in
# send_followups.PROACTIVE_MARKERS so the four-touch cap counts it.
TRANSCRIPT_MARKER = '[JOB DATE FOLLOW-UP]'

_JOB_DATE_RE = re.compile(r'\[JOB_DATE\]\s*(\d{4}-\d{2}-\d{2})')
_STEP_RE = re.compile(r'\[JOB_LADDER\]\s*(\d)')
_FIRST_SENT_RE = re.compile(r'\[JOB_LADDER_T1\]\s*(\S+)')
# Every ladder tag line, for a clean re-arm.
_ALL_TAGS_RE = re.compile(r'^\[JOB_(?:DATE|LADDER|LADDER_T1)\][^\n]*\n?', re.MULTILINE)


# ── Dates ────────────────────────────────────────────────────────────────────

def applies(job_date, today=None) -> bool:
    """True when the job date is more than a week out.

    Exactly the complement of ``out_of_scope_handler._timeframe_is_near``
    (0 to 7 days): a week or less keeps the existing near-date rules, and the
    brief says so. It also guarantees job - 7 is tomorrow at the earliest, so
    we never ask permission to follow up on a day that has passed.
    """
    today = today or date.today()
    return (job_date - today).days > FIRST_TOUCH_DAYS


def first_followup_date(job_date):
    """The date we ask permission to follow up on: job date minus 7 days."""
    return job_date - timedelta(days=FIRST_TOUCH_DAYS)


def ordinal(n: int) -> str:
    # 11th, 12th and 13th break the last-digit rule.
    if 10 <= n % 100 <= 20:
        return f'{n}th'
    suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def spoken_date(day) -> str:
    """'the 14th of October', the way the owner writes a date to a lead.

    The literal date, never "a week before": the brief is explicit that the
    lead is told the actual day.
    """
    return f'the {ordinal(day.day)} of {day.strftime("%B")}'


# ── State ────────────────────────────────────────────────────────────────────

def job_date(appointment):
    """The armed job date, or None."""
    m = _JOB_DATE_RE.search(getattr(appointment, 'internal_notes', '') or '')
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def step(appointment) -> int:
    m = _STEP_RE.search(getattr(appointment, 'internal_notes', '') or '')
    return int(m.group(1)) if m else STEP_FIRST


def first_sent_at(appointment):
    """When the first touch went out (aware datetime), or None."""
    m = _FIRST_SENT_RE.search(getattr(appointment, 'internal_notes', '') or '')
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def _strip_tags(notes: str) -> str:
    return _ALL_TAGS_RE.sub('', notes or '').strip()


def arm(appointment, job_day) -> None:
    """Start (or restart) the ladder for this job date.

    A lead who names a new date mid-ladder gets a fresh ladder, so every old
    tag goes first: a stale [JOB_LADDER_T1] would otherwise make their next
    reply look like an answer to a touch that was never sent for this date.
    """
    notes = _strip_tags(getattr(appointment, 'internal_notes', '') or '')
    appointment.internal_notes = (
        f'{notes}\n{JOB_DATE_TAG} {job_day.isoformat()}\n{STEP_TAG} {STEP_FIRST}'
    ).strip()
    appointment.save(update_fields=['internal_notes'])


def disarm(appointment) -> None:
    """Take the lead off the ladder entirely."""
    notes = getattr(appointment, 'internal_notes', '') or ''
    cleaned = _strip_tags(notes)
    if cleaned != notes.strip():
        appointment.internal_notes = cleaned
        appointment.save(update_fields=['internal_notes'])


def advance(appointment, to_step: int, now) -> None:
    """Record that a step is done. Stamps [JOB_LADDER_T1] on the first touch."""
    notes = re.sub(r'^\[JOB_LADDER\][^\n]*\n?', '',
                   getattr(appointment, 'internal_notes', '') or '',
                   flags=re.MULTILINE).strip()
    notes = f'{notes}\n{STEP_TAG} {to_step}'
    if to_step == STEP_SECOND and FIRST_SENT_TAG not in notes:
        notes += f'\n{FIRST_SENT_TAG} {now.isoformat()}'
    appointment.internal_notes = notes.strip()
    appointment.save(update_fields=['internal_notes'])


def step_day(job_day, at_step: int):
    """The calendar day a step falls on, or None once the ladder is done."""
    offsets = {STEP_FIRST: FIRST_TOUCH_DAYS, STEP_SECOND: SECOND_TOUCH_DAYS,
               STEP_CALL: PLUMBER_CALL_DAYS}
    if at_step not in offsets:
        return None
    return job_day - timedelta(days=offsets[at_step])


def replied_since_first_touch(appointment) -> bool:
    """Has the lead written to us since the first ladder touch went out?

    That reply is the "response" the brief means: the conversation owns them
    again, so neither the second touch nor the plumber's call is needed.
    """
    sent = first_sent_at(appointment)
    if sent is None:
        return False
    last_in = (getattr(appointment, 'last_customer_response', None)
               or getattr(appointment, 'last_inbound_at', None))
    return bool(last_in and last_in > sent)


# ── What the lead is asked, in the delay flow ────────────────────────────────

def permission_ask(followup_day) -> str:
    """The follow-up permission question, with the real date and the reason.

    The owner's wording from the brief, in WE voice ("if we follow up", not
    "if I follow up") and without "fixed": this goes to every tenant's lead,
    and a fixed price is one tenant's promise (see lead_handoff's quote copy).
    Ends on its question first so the outbound single-question rule keeps it.
    """
    return (
        f'Will it be okay if we follow up with you on {spoken_date(followup_day)}? '
        "That gives you time to go over a quote, and once you're happy with it "
        'you can reach back out in good time for us to get the job done.'
    )


# ── The two touches ──────────────────────────────────────────────────────────

def _job_words(appointment) -> str:
    """Their job as a phrase that sits after "the": their own words first.

    The first letter is lowered because it sits mid-sentence ("for the Full
    re-tile" read as a typo), unless the opening word is an acronym ("PVC").
    """
    from .lead_handoff import job_phrase
    words = job_phrase(appointment) or 'job'
    if len(words) > 1 and not words[1].isupper():
        words = words[0].lower() + words[1:]
    return words


def touch_message(appointment, at_step: int) -> str:
    """The customer copy for the first (job - 7) or second (job - 3) touch.

    First: offer the two ways to get a quote in hand before the date, as a
    choice (a presumptive this-or-that, not "are you still interested?").
    Second: one short question that surfaces whether they are still on or the
    timing moved, which is what the plumber's call would otherwise have to
    find out. Both name the date in the owner's form and their own job.
    """
    name = (getattr(appointment, 'customer_name', '') or '').strip()
    hi = f'Hi {name}' if name else 'Hi there'
    day = job_date(appointment)
    when = spoken_date(day) if day else 'the date you mentioned'
    job = _job_words(appointment)
    if at_step == STEP_FIRST:
        return (
            f'{hi}, as promised, checking in ahead of {when} for the {job}.\n\n'
            "If you'd like a quote in hand before then, send us measurements, "
            "a few photos or a plan of the space and we'll price it, or we can "
            'come and take a quick look. Which suits you better?'
        )
    return (
        f"{hi}, {when} is coming up. Are you still keen to get the {job} "
        'sorted then, or has the timing moved?'
    )


def touch_email(appointment, at_step: int):
    """(subject, html) for a touch sent by email.

    Same words as the WhatsApp copy, so a lead who reads both channels reads
    one message. Signed by the business, with the existing call and WhatsApp
    buttons. Speaks as WE at source: customer email is outside speak_as_we.
    """
    from html import escape
    from .customer_emails import (_business_name, _contact_buttons,
                                  _from_name, _wrap)
    text = touch_message(appointment, at_step)
    day = job_date(appointment)
    when = spoken_date(day) if day else 'your job'
    subject = (f'Ahead of {when}' if at_step == STEP_FIRST
               else f'{when[:1].upper()}{when[1:]} is coming up')
    paragraphs = ''.join(f'<p>{escape(p)}</p>' for p in text.split('\n\n'))
    body = (
        paragraphs
        + _contact_buttons(appointment)
        + f'<p>{escape(_from_name(appointment))}<br>'
          f'{escape(_business_name(appointment))}</p>'
    )
    return subject, _wrap(body, appointment)


# ── The plumber's phone call (job - 2, no reply) ─────────────────────────────

def quote_given(appointment) -> bool:
    """Has a quote actually gone to this lead? Routes the call to B1 or B2.

    A quote counts once it was SENT on either channel, or marked sent. A draft
    the plumber never sent is not a quote the lead has seen, and telling them
    "the quote we sent you is still good" about it would be false.
    """
    try:
        from django.db.models import Q
        return appointment.quotations.filter(
            Q(sent_via_whatsapp=True) | Q(sent_via_email=True)
            | Q(sent_at__isnull=False)
        ).exists()
    except Exception:
        logger.exception('quote_given failed for apt %s',
                         getattr(appointment, 'pk', None))
        return False


def _two_days(appointment):
    """Two real working days for the script's "[day] or [day]", or ('', '')."""
    try:
        from .tenant_config import get_config
        from .visit_slots import next_two_slots
        slots = next_two_slots(get_config(getattr(appointment, 'tenant', None)))
    except Exception:
        slots = []
    days = [s.date.strftime('%A') for s in slots]
    if len(days) == 2:
        return days[0], days[1]
    return ('', '')


def call_brief(appointment):
    """(subject, text) for the plumber: who to phone, and the script.

    Internal copy to the plumber, so it names him and his company in the
    opener (the we-voice rule covers customer copy only). The script is the
    owner's from the brief, filled in; only the branch the system already
    knows applies (B1 quote given, B2 no quote) is included, so the plumber
    does not have to check or choose anything.
    """
    from .utils import business_name_for
    from .lead_handoff import service_label

    name = (getattr(appointment, 'customer_name', '') or '').strip()
    first = name.split()[0] if name else 'there'
    service = service_label(appointment).lower() or 'plumbing work'
    area = getattr(appointment, 'customer_area', '') or 'not given'
    description = (getattr(appointment, 'project_description', '') or '').strip() or 'not given'
    day = job_date(appointment)
    named = day.strftime('%A %d %B %Y') if day else 'not given'
    phone = ''.join(c for c in str(getattr(appointment, 'phone_number', '') or '') if c.isdigit())
    email = (getattr(appointment, 'customer_email', '') or '').strip() or 'not given'
    try:
        plumber = appointment.plumber_display_name()
    except Exception:
        plumber = ''
    plumber = '' if plumber == 'the plumber' else plumber
    company = business_name_for(appointment, default='')
    d1, d2 = _two_days(appointment)
    days = f'{d1} or {d2}' if d1 else '[day] or [day]'
    when = spoken_date(day) if day else 'the date you mentioned'
    quoted = quote_given(appointment)

    subject = f'[Call] Phone {name or "+" + phone} today, their job date is {named}'

    lines = [
        f'Please phone {name or "this lead"} today. They named {named} for the '
        f'{service} and have not answered our two check-ins, so a call is the '
        'right touch two days out. Keep it a friendly check-in, not a pitch.',
        '',
        f'Name: {name or "not given"}',
        f'Service: {service}',
        f'Area: {area}',
        f'Job: {description}',
        f'Date named: {named}',
        f'Phone: +{phone}' if phone else 'Phone: not given',
        f'Email: {email}',
        f'Quote: {"already sent (branch B1)" if quoted else "none yet (branch B2)"}',
        '',
        'OPENER',
        f'"Hi {first}, it\'s {plumber or "[your name]"}'
        + (f' from {company}' if company else '')
        + f'. You were looking at getting the {service} done around {when}. '
        'Did you end up going with someone else, or are you still keen to get '
        'it sorted?"',
        '',
        'A. Went with someone else, or no longer needed',
        '"No worries at all. If it doesn\'t work out, you\'ve got my number."',
        'Then leave them alone.',
        '',
    ]
    if quoted:
        lines += [
            'B1. Still want it done (the quote has been sent)',
            f'"Perfect, the quote we sent you is still good, so let\'s get you '
            f'booked in. Does {days} work better?"',
            '',
            'If they hesitate, give them room to say it is the price:',
            '"Can I ask, was the price about what you expected, or was it a bit '
            'much? Either way\'s fine, I just want to help you get it sorted."',
            '',
            'If it is the price, talk it through rather than cutting the number:',
            '- Split into stages: "we can do it in two parts so you don\'t pay '
            'it all at once."',
            '- Change what\'s included: "tell me what you really need and what '
            'can wait, and I can bring the price down."',
            '- Ask their number: "what were you hoping to pay? Let me see what '
            'I can do."',
        ]
    else:
        lines += [
            'B2. Still want it done (no quote yet)',
            '"Perfect, easiest thing is I pop round, take a look, and give you a '
            f'free quote there and then, and we can sort a date. Does {days} '
            'work for you?"',
            '',
            'If they are not keen on the visit, quote over the phone instead:',
            '"That\'s okay. If you have measurements, or a few photos, or a plan, '
            'just send those over with what you need done and I\'ll give you a '
            'price over the phone."',
        ]
    lines += [
        '',
        'Order: find out if they have gone elsewhere or still want it. Only '
        'once they say they are still keen, lean towards a date.',
    ]
    return subject, '\n'.join(lines)


def send_call_brief(appointment, dry_run=False) -> bool:
    """Email the plumber the call brief through the normal plumber channel."""
    from .plumber_notifications import send_plumber_notification_email
    subject, text = call_brief(appointment)
    return bool(send_plumber_notification_email(
        subject, text, dry_run=dry_run,
        tenant=getattr(appointment, 'tenant', None), appointment=appointment,
    ))
