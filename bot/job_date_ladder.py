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
# The lead's answer to "Will it be okay if we give you a call on ...?"
# (owner, 2026-09-22: never call without permission). CALL_OK is a yes, for
# the brief; NO_CALL is an explicit no, and the cron then sends the plumber NO
# call brief. No answer means the call goes ahead. CALL_OK belongs to one job
# date and is cleared on a re-arm; NO_CALL is not, because a lead who said
# "don't call me" did not take that back by naming a new date.
CALL_OK_TAG = '[JOB_CALL_OK]'
NO_CALL_TAG = '[JOB_NO_CALL]'

# Every ladder tag line, for a clean re-arm.
_ALL_TAGS_RE = re.compile(r'^\[JOB_(?:DATE|LADDER|LADDER_T1|CALL_OK)\][^\n]*\n?', re.MULTILINE)


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

    WHAT: the owner's email copy (copy_catalog LADDER_EMAIL_FIRST / _SECOND,
    2026-09-22): it says back that the lead told us they would be going ahead
    around their date, sounds considerate rather than chasing, and offers the
    free online quote, with a Get quote button above the WhatsApp and Call
    buttons. WHY it no longer mirrors touch_message: the owner asked for this
    wording in the email; the WhatsApp touch keeps its own short copy.
    Speaks as WE at source, because customer email is outside speak_as_we.
    Pinned by LadderEmailCopyTests.
    """
    from html import escape
    from .copy_catalog import LADDER_EMAIL_FIRST, LADDER_EMAIL_SECOND
    from .customer_emails import (_business_name, _contact_buttons,
                                  _from_name, _get_quote_button, _wrap)
    name = (getattr(appointment, 'customer_name', '') or '').strip()
    day = job_date(appointment)
    when = spoken_date(day) if day else 'the date you mentioned'
    template = LADDER_EMAIL_FIRST if at_step == STEP_FIRST else LADDER_EMAIL_SECOND
    text = template.format(hi=f'Hi {name}' if name else 'Hi there',
                           job=_job_words(appointment), when=when)
    subject = (f'Ahead of {when}' if at_step == STEP_FIRST
               else f'{when[:1].upper()}{when[1:]} is coming up')
    paragraphs = ''.join(f'<p>{escape(p)}</p>' for p in text.split('\n\n'))
    body = (
        paragraphs
        + _get_quote_button(appointment)
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


def _checkins_sent(appointment, job_day) -> int:
    """How many ladder touches actually reached the lead for this job date.

    Read from the transcript (TRANSCRIPT_MARKER is written only when a touch
    went out), not from the ladder step, which advances even when no channel
    could carry the touch. That difference is the whole point: a lead with no
    email and a shut WhatsApp window got NO check-ins, and the call brief must
    not tell the plumber they ignored two.
    """
    since = (job_day - timedelta(days=FIRST_TOUCH_DAYS + 1)).isoformat()
    return sum(
        1 for turn in (getattr(appointment, 'conversation_history', None) or [])
        if turn.get('role') == 'assistant'
        and str(turn.get('content', '')).startswith(TRANSCRIPT_MARKER)
        and str(turn.get('timestamp', ''))[:10] >= since
    )


def _visit_offer(appointment) -> str:
    """The visit half of the no-quote branch, priced by the lead's OWN tenant.

    "Free" only when the tenant's visit is free (TenantConfig.visit_is_free);
    a tenant that charges a call-out has the fee said, and the waiver when it
    has one. Barmak charges US$10, so the old "free quote there and then"
    promised Barmak's leads something the plumber would then charge for.
    """
    try:
        from .tenant_config import get_config
        cfg = get_config(getattr(appointment, 'tenant', None))
    except Exception:
        cfg = None
    if cfg is None or cfg.visit_is_free():
        return 'take a look, and give you a free quote there and then'
    fee = f'{cfg.currency}{cfg.consultation_fee}'
    waived = (", and that's taken off if you go ahead with us"
              if cfg.visit_fee_waived_on_job() else '')
    return (f'take a look, and give you an exact quote there and then. '
            f'The call-out is {fee}{waived}')


def call_brief(appointment, now=None):
    """(subject, text, html) for the plumber: the job-date call, script first.

    WHAT: the owner's call-email layout (call_brief.build_call_email, the one
    approved for Barmak lead 1162 on 2026-09-22): one intro line, the script
    with the opening in a box, each branch as "If ...", the Log the call
    button, the lead's details last. The subject starts with
    CALL_SUBJECT_PREFIX, so the 24-hour reminder (call_brief.send_due_reminders)
    covers it too.
    WHY: the owner wants a script the plumber reads out with no thinking and
    no fact checking (2026-09-22). So the intro states only what the system
    KNOWS happened (when they gave the date, how many check-ins actually went
    out, per _checkins_sent), only the branch the system already knows
    applies is included (quote sent or not), and the visit is priced by the
    lead's own tenant (_visit_offer). The script's lines are the owner's from
    the handoff brief; the day choice reads "Would X or Y suit you better?"
    so the reminder can move it forward.
    Internal copy to the plumber: naming him and the company is allowed
    outside customer copy. Pinned by the "job ladder" call-brief cases in
    TEST 0 and LadderCallBriefTests.
    """
    from django.utils import timezone as _tz
    from .call_brief import (CALL_SUBJECT_PREFIX, _next_two_working_days,
                             build_call_email)
    from .lead_handoff import service_label
    from .phone_quote import ensure_request, form_url
    from .utils import business_name_for

    now = now or _tz.now()
    name = (getattr(appointment, 'customer_name', '') or '').strip()
    first = name.split()[0] if name else 'there'
    service = service_label(appointment).lower() or 'plumbing work'
    area = getattr(appointment, 'customer_area', '') or ''
    description = ' '.join((getattr(appointment, 'project_description', '') or '').split())
    day = job_date(appointment)
    named = day.strftime('%A %d %B %Y') if day else 'not given'
    when = spoken_date(day) if day else 'the date you mentioned'
    phone = ''.join(c for c in str(getattr(appointment, 'phone_number', '') or '') if c.isdigit())
    email = (getattr(appointment, 'customer_email', '') or '').strip()
    try:
        plumber = (appointment.plumber_display_name() or '').strip()
    except Exception:
        plumber = ''
    plumber = '' if plumber == 'the plumber' else plumber
    plumber_first = plumber.split()[0] if plumber else ''
    company = business_name_for(appointment, default='')
    d1, d2 = _next_two_working_days(appointment, now)
    quoted = quote_given(appointment)

    # What happened, from records only, so nothing in it needs checking.
    told = getattr(appointment, 'delay_signal_detected_at', None)
    told_on = f' on {_tz.localtime(told).strftime("%A %d %B")}' if told else ''
    sent = _checkins_sent(appointment, day) if day else 0
    if sent:
        times = 'once' if sent == 1 else f'{sent} times'
        channel = 'by email' if email else 'on WhatsApp'
        history = f'We checked in {times} since {channel} and have not heard back.'
    elif email:
        # An email on file but no touch sent (held after a handoff, or by the
        # touch cap): say only that we have not been in touch.
        history = 'We have not been in touch with them since.'
    else:
        history = ('We have had no way to reach them since: there is no email on file '
                   'and their WhatsApp has been quiet too long for us to message them, '
                   'so your call is the first contact since then.')
    # A no-email lead was ASKED whether we may call today (Option A, owner
    # 2026-09-22): the brief says whether they said yes or did not answer (an
    # explicit no never reaches here, the cron sends no brief). They were also
    # given the plumber's number, so they may have had the quote from him on
    # his own WhatsApp, where we cannot see it: the script has a branch for it.
    from .plumber_link import LINK_SENT_TAG
    notes = getattr(appointment, 'internal_notes', '') or ''
    told_call = ''
    if not email and CALL_OK_TAG in notes:
        told_call = ' They said yes when we asked if we could call them today.'
    elif not email and LINK_SENT_TAG in notes:
        told_call = (' We asked if we could call them today and they did not answer, '
                     'so the call goes ahead.')
    if not email and LINK_SENT_TAG in notes:
        told_call += (' They also have your number for a quote: if they messaged you, '
                      'have that chat open when you call.')
    intro = (f'Hi {plumber_first or "there"}, please call {name or "this lead"} today and '
             f'read the script below. They told us{told_on} they would be going ahead '
             f'with the {service} around {named}, two days from now. {history}'
             f'{told_call} Keep it a friendly check-in, not a pitch.')

    opening = (f"Hi {first}, it's {plumber_first or 'us'}"
               + (f' from {company}' if company else '')
               + f'. You were looking at getting the {service} done around {when}. '
               'Did you end up going with someone else, or are you still keen to get '
               'it sorted?')
    branches = [
        ("If they went with someone else, or no longer need it", [
            '"No worries at all. If it doesn\'t work out, you\'ve got my number."',
            'Then leave them alone.']),
    ]
    if quoted:
        branches += [
            ("If they still want it done (we have sent them a quote)", [
                f'"Perfect, the quote we sent you is still good, so let\'s get you '
                f'booked in. Would {d1} or {d2} suit you better?"',
                'If they can\'t find it: "No problem, I\'ll send it to you again now."']),
            ("If they hesitate", [
                '"Can I ask, was the price about what you expected, or was it a bit '
                'much? Either way\'s fine, I just want to help you get it sorted."']),
            ("If it is the price", [
                'Talk it through rather than cutting the number.',
                'Split it: "we can do it in two parts so you don\'t pay it all at once."',
                'Trim it: "tell me what you really need and what can wait, and I can '
                'bring the price down."',
                'Ask: "what were you hoping to pay? Let me see what I can do."']),
        ]
    else:
        branches += [
            ("If they still want it done (no quote sent yet)", [
                f'"Perfect, easiest thing is I pop round, {_visit_offer(appointment)}. '
                f'Would {d1} or {d2} suit you better?"',
                'If they pick a day: "Morning or afternoon?"']),
            ("If they already got a quote from you on WhatsApp", [
                '"Great, did you get a chance to go over the quote? I can send it again '
                f'if that helps. Would {d1} or {d2} suit you better to get started?"']),
            ("Only if they're not keen on a visit", [
                '"That\'s okay. If you have measurements, or a few photos, or a plan, '
                'just send those over with what you need done and I\'ll give you a '
                'price over the phone."']),
        ]
    branches += [
        ("If the timing has moved", [
            '"No problem at all. Roughly when are you thinking now?" Note the new date.']),
        ("If there's no answer", ['Try once more later today, then leave it.']),
    ]
    details = [
        ('Call', f'{name or "Name not given"}, +{phone}' if phone else (name or 'not given')),
        ('Job', description or service),
        ('Area', area or 'not given'),
        ('Date they named', named),
        ('Quote', 'already sent' if quoted else 'none sent yet'),
        ('Email', email or 'not given'),
    ]
    # The Log the call form needs a saved lead; preview_handoff renders an
    # unsaved one, so it gets a placeholder rather than a crash or a row.
    url = (form_url(ensure_request(appointment))
           if getattr(appointment, 'pk', None) else '#log-the-call')
    text, html = build_call_email(intro, opening, branches, details, url)
    subject = f'{CALL_SUBJECT_PREFIX}{name or "+" + phone}, {service} around {named}'
    return subject, text, html


def send_call_brief(appointment, dry_run=False) -> bool:
    """Email the plumber the call brief, to the tenant's notification list.

    Sent the way the other call emails are (shared_contact.send_brief): the
    tenant's recipients with the operator's hidden copy, category
    plumber_alert, so it sits with them and gets their 24-hour reminder.
    """
    from .plumber_notifications import (send_email_to_recipients,
                                        split_notification_recipients)
    subject, text, html = call_brief(appointment)
    if dry_run:
        return True
    tenant = getattr(appointment, 'tenant', None)
    to, hidden = split_notification_recipients(tenant)
    return bool(send_email_to_recipients(
        to, subject, text, html_message=html, tenant=tenant,
        appointment=appointment, bcc=hidden,
        category='plumber_alert', to_role='plumber'))
