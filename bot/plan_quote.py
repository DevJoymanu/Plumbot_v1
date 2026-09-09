"""
bot/plan_quote.py
=================
Everything that happens after a lead sends a real plan (spec §10.3 to §10.5).

The plan carries the measurements, so there is no site visit to sell. What the
bot does instead is get the plan and the job context to the plumber, chase the
plumber until they say the quote went out, then chase the lead.

Built to the same rules as `bot/post_visit.py`, which is the module this is
modelled on:

- **Anchors and offsets, not a job queue.** There is no scheduler in this
  codebase. Every scheduled behaviour is a timestamp plus an offset, re-checked
  on each cron tick, and every send is gated by the timestamp written as it
  goes out. That makes the tick idempotent and, unlike a queue, it survives a
  missed run.

- **Lead state is re-checked before EVERY send, never once at the top.** A lead
  can be parked, booked or handed off between two ticks. This is the exact
  class of bug that had the old scheduler firing into confirmed and handed-off
  leads, so `lead_is_suppressed` is borrowed from post_visit rather than
  reimplemented.

- **One bad lead must not stop the run.** Each lead is ticked inside its own
  try.

Branch B waits for a contact window before it messages the lead, because it
fires on a clock: twelve hours after a plan that arrived at 3pm is 3am. Branch
A does not, because it is triggered by the plumber tapping the form, and they
only do that during the working day. The window itself is read from
`send_followups.CONTACT_WINDOWS`, never copied, so the owner moves it in one
place.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# How long the bot gets to collect the job context before the plumber is
# emailed. The point is that the plumber opens ONE complete email rather than a
# bare plan they then have to chase the lead about themselves.
PLUMBER_NOTIFY_DELAY_HOURS = 1

# Chase the plumber at these hours after the first email, until the form is in.
# A list, because the count is read off it: adding a fourth reminder here is
# the whole change.
PLUMBER_REMINDER_OFFSET_HOURS = (2, 4, 8)

# The plumber never answered. Rather than leave the lead sitting, assume the
# quote went out and follow up anyway.
ASSUME_QUOTE_SENT_AFTER_HOURS = 12

# Once the plumber DOES confirm, give them an hour before we ask the lead about
# a quote they may only just have sent.
LEAD_FOLLOWUP_AFTER_FORM_HOURS = 1


def ensure_request(appointment, when=None):
    """The row for this lead, created if the plan has just arrived.

    Idempotent, and safe to call from the media handler on every inbound file:
    a lead who sends three photos of the same drawing gets one row.
    """
    from bot.models import PlanQuoteRequest

    existing = getattr(appointment, 'plan_quote_request', None)
    if existing is not None:
        return existing

    row, _created = PlanQuoteRequest.objects.get_or_create(
        appointment=appointment,
        defaults={
            'tenant': getattr(appointment, 'tenant', None),
            'plan_received_at': when or timezone.now(),
        },
    )
    return row


def open_requests(now=None, tenant=None):
    """Every plan-quote row that still has something left to do."""
    from bot.models import PlanQuoteRequest

    rows = PlanQuoteRequest.objects.select_related('appointment').filter(
        stopped_at__isnull=True, plan_received_at__isnull=False,
    )
    if tenant is not None:
        rows = rows.filter(tenant=tenant)
    return rows


def _due(anchor, hours, now):
    return bool(anchor) and now >= anchor + timedelta(hours=hours)


def in_contact_window(when):
    """Is this a moment we may message a lead?

    Reads `send_followups.CONTACT_WINDOWS` rather than carrying its own copy.
    The owner moves these hours, and a second definition is always the one
    nobody remembers to update.
    """
    from bot.management.commands.send_followups import CONTACT_WINDOWS, SA_TIMEZONE

    if not CONTACT_WINDOWS:
        return True
    local = when.astimezone(SA_TIMEZONE)
    mins = local.hour * 60 + local.minute
    return any((oh * 60 + om) <= mins < (ch * 60 + cm)
               for oh, om, ch, cm in CONTACT_WINDOWS)


def projected_emails(appointment):
    """Every email the PLAN path will send or has sent for this lead.

    The plumber-facing half of this is what the dashboard was missing entirely:
    the plan alert, and the chases that ask whether they have quoted the lead
    yet. Same reason it lives here as in post_visit -- PLUMBER_NOTIFY_DELAY_HOURS
    and PLUMBER_REMINDER_OFFSET_HOURS are this module's constants.
    """
    row_source = getattr(appointment, 'plan_quote_request', None)
    if row_source is None:
        return []

    request = row_source
    now = timezone.now()
    rows = []

    def _row(label, when, sent_at, note, to='customer'):
        moment = sent_at or when
        if moment is None:
            return
        rows.append({
            'label': label,
            'scheduled_for': moment,
            'status': 'sent' if sent_at else ('pending' if when >= now else 'overdue'),
            'note': note,
            'source': 'plan_quote',
            'to': to,
        })

    anchor_at = request.plan_received_at or request.created_at
    _row('Plan sent to the plumber',
         anchor_at + timedelta(hours=PLUMBER_NOTIFY_DELAY_HOURS)
         if anchor_at else None,
         request.plumber_email_sent_at,
         'The job and the drawing, so they can quote off it without a visit.',
         to='plumber')

    # The chases: "have you quoted this lead yet?", until they answer the form.
    alert_at = request.plumber_email_sent_at
    if alert_at and (request.reminders_sent
                     or not request.plumber_form_completed_at):
        for number, offset in enumerate(PLUMBER_REMINDER_OFFSET_HOURS, 1):
            _row(f'Chase the plumber {number} of {len(PLUMBER_REMINDER_OFFSET_HOURS)}',
                 alert_at + timedelta(hours=offset),
                 alert_at + timedelta(hours=offset)
                 if request.reminders_sent >= number else None,
                 'Asks whether they have quoted this lead yet.', to='plumber')

    # The lead's own follow-up: an hour after the plumber confirms (Branch A),
    # or at +12h assuming the quote went out (Branch B).
    if request.plumber_form_completed_at:
        due = request.plumber_form_completed_at + timedelta(
            hours=LEAD_FOLLOWUP_AFTER_FORM_HOURS)
        note = 'Asks the customer about the quote the plumber confirmed sending.'
    elif alert_at:
        due = alert_at + timedelta(hours=ASSUME_QUOTE_SENT_AFTER_HOURS)
        note = ('The plumber never answered, so this assumes the quote went out '
                'and asks the customer anyway.')
    else:
        due, note = None, ''
    _row('Quote follow-up to the customer', due,
         request.lead_followup_sent_at, note)

    return rows


def lead_is_done(appointment) -> bool:
    """True when this lead must not be chased about a quote any more.

    `lead_is_suppressed` from post_visit covers most of it — parked, handed
    off, stopped, inactive, cancelled, job already on the diary — and is reused
    rather than reimplemented so the two flows cannot drift apart.

    What it does NOT cover, and this flow needs, is a lead sitting at
    status='confirmed' with an appointment ahead of them. On the post-visit
    path that is the ordinary state: the visit has happened and the row is
    about what came after it. On the plan path it means the opposite. They have
    booked, so the quote did its job and there is nothing left to chase.
    """
    from bot.post_visit import lead_is_suppressed

    if lead_is_suppressed(appointment):
        return True
    return (getattr(appointment, 'status', '') == 'confirmed'
            and getattr(appointment, 'scheduled_datetime', None) is not None)


# The three things the bot tries to collect in the hour before the plumber is
# emailed. Whatever is still missing is named IN that email as something for
# the plumber to ask, because a plan always goes over regardless.
PLAN_INFO_FIELDS = (
    ('project_description', 'what the job is'),
    ('customer_area', 'the area'),
    ('timeline', 'when they want it done'),
)


def missing_info(appointment) -> list:
    """Which of the three the bot did not get. Empty when it got them all."""
    return [label for field, label in PLAN_INFO_FIELDS
            if not str(getattr(appointment, field, '') or '').strip()]


def _info_is_complete(appointment) -> bool:
    return not missing_info(appointment)


def timeline_is_slow(appointment) -> bool:
    """True when the lead wants the job further out than a week.

    The plumber is told this in the subject, so a nurture lead is not worked
    as an urgent callout. Deterministic, and an unreadable timeline counts as
    NEAR: treating a maybe-urgent lead as slow is the worse mistake.
    """
    from bot.views.plumbot.extraction_mixin import ExtractionMixin

    class _Probe:
        """Borrows the flow's own near/far test so the email and the question
        order can never disagree about what 'soon' means."""
        appointment = None
        PLAN_NEAR_TIMELINE_DAYS = ExtractionMixin.PLAN_NEAR_TIMELINE_DAYS
        _plan_timeline_is_near = ExtractionMixin._plan_timeline_is_near

    probe = _Probe()
    probe.appointment = appointment
    try:
        return not probe._plan_timeline_is_near()
    except Exception:
        logger.warning('Could not read the timeline for apt %s',
                       getattr(appointment, 'pk', None), exc_info=True)
        return False


def run_plan_quote_tick(now=None, dry_run=False, log=None, tenant=None):
    """One pass of the plan-quote machine. Returns a counter dict.

    Idempotent, so the five-minute cron re-running it is safe.
    """
    now = now or timezone.now()
    stats = {'plumber_emails': 0, 'reminders': 0, 'lead_followups': 0,
             'skipped': 0}

    def emit(msg):
        if log:
            log(msg)

    for row in open_requests(now=now, tenant=tenant):
        apt = row.appointment
        try:
            _tick_one(apt, row, now, dry_run, emit, stats)
        except Exception as exc:  # noqa: BLE001 - one bad lead must not stop the run
            logger.exception('plan-quote tick failed for apt %s', apt.pk)
            emit('[error] apt {}: {}'.format(apt.pk, exc))

    return stats


def _tick_one(apt, row, now, dry_run, emit, stats):
    # Re-checked here, before anything, and again before the lead-facing send.
    # A lead can be parked or booked between two ticks.
    if lead_is_done(apt):
        stats['skipped'] += 1
        return

    # 1. The first email: lead details plus the plan (§10.3).
    if not row.plumber_email_sent_at:
        # The full hour, always. It is NOT an "or as soon as we have
        # everything" optimisation: the hour exists to give the bot time to
        # book the lead before the plumber is disturbed, and sending early
        # throws that away. A lead who books inside the hour never generates
        # this email at all — lead_is_done above already returned.
        if not _due(row.plan_received_at, PLUMBER_NOTIFY_DELAY_HOURS, now):
            return
        if dry_run:
            emit('[dry-run] plan -> plumber (apt {})'.format(apt.pk))
            stats['plumber_emails'] += 1
            return
        from bot.plumber_notifications import send_plan_quote_email
        if send_plan_quote_email(row):
            row.plumber_email_sent_at = now
            row.save(update_fields=['plumber_email_sent_at'])
            stats['plumber_emails'] += 1
            emit('[plan] emailed to the plumber (apt {})'.format(apt.pk))
        return

    # 2. Chase the plumber until the form is in (§10.4).
    if row.is_open:
        _tick_reminders(apt, row, now, dry_run, emit, stats)

    # 3. Follow the lead up about the quote (§10.5).
    _tick_lead_followup(apt, row, now, dry_run, emit, stats)


def _tick_reminders(apt, row, now, dry_run, emit, stats):
    """+2h, +4h, +8h, stopping the moment the form is submitted.

    The count is read off PLUMBER_REMINDER_OFFSET_HOURS, so the schedule and
    the number of reminders cannot disagree.
    """
    sent = row.reminders_sent or 0
    if sent >= len(PLUMBER_REMINDER_OFFSET_HOURS):
        return
    offset = PLUMBER_REMINDER_OFFSET_HOURS[sent]
    if not _due(row.plumber_email_sent_at, offset, now):
        return

    if dry_run:
        emit('[dry-run] reminder {} -> plumber (apt {})'.format(sent + 1, apt.pk))
        stats['reminders'] += 1
        return

    from bot.plumber_notifications import send_plan_quote_reminder
    if send_plan_quote_reminder(row, number=sent + 1):
        row.reminders_sent = sent + 1
        row.save(update_fields=['reminders_sent'])
        stats['reminders'] += 1
        emit('[reminder {}] plumber chased (apt {})'.format(sent + 1, apt.pk))


def _tick_lead_followup(apt, row, now, dry_run, emit, stats):
    """Ask the lead about the quote.

    Branch A: the plumber confirmed, so we wait an hour and ask.
    Branch B: the plumber never answered. After 12h we assume the quote went
    out and ask anyway, rather than leave the lead waiting on us. Both branches
    end in the same place; only the trigger and the recorded status differ.
    """
    if row.lead_followup_sent_at:
        return

    if row.plumber_form_completed_at:
        # Branch A. The plumber confirmed, so this follows an hour later.
        if not _due(row.plumber_form_completed_at,
                    LEAD_FOLLOWUP_AFTER_FORM_HOURS, now):
            return
        status = 'sent_confirmed'
    else:
        # Branch B. The plumber never answered, so at +12h we assume the quote
        # went out rather than leave the lead waiting on us.
        if not _due(row.plumber_email_sent_at,
                    ASSUME_QUOTE_SENT_AFTER_HOURS, now):
            return
        # Branch B alone snaps to the contact window. Branch A is triggered by
        # the plumber tapping the form, which they only do during the working
        # day; Branch B fires on a clock and would otherwise land at 3am, twelve
        # hours after a plan that arrived at 3pm.
        if not in_contact_window(now):
            return
        status = 'assumed'

    # Re-checked immediately before a customer-facing send, not only at the top
    # of the tick: the plumber may have booked this lead an hour ago.
    if lead_is_done(apt):
        stats['skipped'] += 1
        return

    if dry_run:
        emit('[dry-run] quote follow-up -> lead (apt {}, {})'.format(
            apt.pk, status))
        stats['lead_followups'] += 1
        return

    row.quote_status = row.quote_status or status
    row.lead_followup_sent_at = now
    row.save(update_fields=['quote_status', 'lead_followup_sent_at'])
    stats['lead_followups'] += 1
    emit('[follow-up] lead asked about the quote (apt {}, {})'.format(
        apt.pk, status))


def apply_plumber_form(row, *, outcome='quoting', quote_sent=False,
                       lead_email='', expectation='', expected_date=None,
                       expected_timeframe='', job_notes='', quote_amount=None,
                       now=None):
    """The plumber answered. Closes the form and stops the reminders.

    Single use, like SiteVisitReport.apply_submission: a row that already
    carries `plumber_form_completed_at` is left exactly as it is. The link goes
    out in four emails, and a plumber who taps two of them must not overwrite
    what they told us the first time, or raise a second quote for one job.

    Everything the site-visit debrief captures is captured here too, because
    the follow-ups after a quote need the same answers whichever path produced
    it: when the customer wants the job done, and what the quote should say.

    Returns True when this call was the one that closed it.
    """
    if row.plumber_form_completed_at:
        return False

    now = now or timezone.now()
    row.plumber_form_completed_at = now
    row.outcome = outcome
    # 'sent_confirmed' only when the plumber says the quote has already gone.
    # Creating it in the app does not count: that is what the send flow marks.
    row.quote_status = 'sent_confirmed' if quote_sent else ''
    row.expectation = expectation
    row.expected_date = expected_date
    row.expected_timeframe = expected_timeframe
    row.job_notes = job_notes
    fields = ['plumber_form_completed_at', 'outcome', 'quote_status',
              'expectation', 'expected_date', 'expected_timeframe', 'job_notes']

    if quote_amount is not None:
        row.quote_amount = quote_amount
        fields.append('quote_amount')

    row.save(update_fields=fields)

    # The form is the one place a missing lead email can be added, which is
    # what makes any later follow-up possible at all.
    email = (lead_email or '').strip()
    if email and not getattr(row.appointment, 'customer_email', None):
        row.appointment.customer_email = email
        row.appointment.save(update_fields=['customer_email'])

    # A named date switches the lead onto the confirm branch, exactly as the
    # site-visit debrief does. Same resolver, so the two paths cannot drift on
    # what a date means.
    if expectation == 'specific_date' and expected_date:
        try:
            from bot.post_visit import record_lead_expected_date
            record_lead_expected_date(row.appointment, expected_date,
                                      source='plan quote form')
        except Exception:
            logger.warning('Could not record the expected date for apt %s',
                           row.appointment_id, exc_info=True)

    return True


def form_url(row, absolute=True):
    """The tokenized, single-use form URL for this request."""
    from django.conf import settings
    from django.urls import reverse

    path = reverse('plan_quote_form', kwargs={'token': row.token})
    if not absolute:
        return path
    return '{}{}'.format(settings.SITE_URL.rstrip('/'), path)


def record_timeline(appointment, raw: str):
    """Resolve a stated timeline to days-from-today, once, and store it.

    Called when the lead answers the timeline question. The good parser is
    AI-first, which is fine here — once per lead, off the hot path — and the
    number it produces is what every later read uses.

    Returns the days, or None when nothing usable could be read. None means
    NEAR everywhere downstream: pushing a lead for a day is recoverable,
    quietly parking one who wanted the job this week is not.
    """
    from datetime import date

    from django.utils import timezone

    row = getattr(appointment, 'plan_quote_request', None)
    if row is None:
        return None

    days = None
    try:
        from bot.out_of_scope_handler import _compute_followup_date
        iso, _friendly = _compute_followup_date(raw or '')
        if iso:
            target = date.fromisoformat(str(iso)[:10])
            days = (target - timezone.localdate()).days
    except Exception:
        logger.warning('Could not read the timeline %r for apt %s',
                       (raw or '')[:40], appointment.pk, exc_info=True)

    row.timeline_days = days
    row.save(update_fields=['timeline_days'])
    return days
