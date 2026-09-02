"""
bot/post_visit.py
=================
Everything that happens AFTER a site visit: the debrief form's state machine
and the follow-up scheduler that reads it.

The shape of the flow (see the feature brief):

    visit ends
      - in-app banner on the appointment detail screen   -\
      - 35 min later, an emailed link to the plumber      -/-> the SAME form

    form submitted, outcome = went ahead
      - specific date on file   -> CASE A: one confirmation ask, 2 days before
      - rough timeframe / none  -> CASE B: ask #1 next day noon, #2 +3d, #3 +7d
                                           then cold + tell the plumber

    form never submitted by noon the next day
      - CASE C: treat as "no date, lead has email" and run CASE B directly
                (no email on file -> nothing to follow up on, tell the plumber)

Two rules hold the whole thing together and are asserted by the tests:

* **One resolver decides whether we may message a lead at all** --
  :func:`lead_is_suppressed`. Parked, handed-off, stopped, already-booked and
  inactive leads are never chased, whatever the schedule says.
* **No follow-up ever renders a null date.** Every date that reaches copy comes
  from :meth:`SiteVisitReport.expectation_label` or is guarded at the call site.

The functions here are deterministic and offline; the management command
``send_post_visit_followups`` is a thin shell around :func:`run_post_visit_tick`.
"""

import logging
import re
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)


# -- Timing constants --------------------------------------------------------

# How long after the visit ENDS we email the plumber the form link, if the
# in-app button has not already been tapped. Short enough that the visit is
# still fresh, long enough that we are not emailing them in the driveway.
FALLBACK_EMAIL_DELAY_MINUTES = 35

# The hour (local) that "next day at 12pm" means, for both the Case C deadline
# and Case B's first ask.
ASK_HOUR = 12

# Case B spacing, in days, measured from the previous ask.
ASK_2_AFTER_DAYS = 3
ASK_3_AFTER_DAYS = 7
MAX_ASKS = 3

# Case A: how far ahead of the job date the confirmation goes out, and at what
# local hour.
CONFIRM_DAYS_BEFORE = 2
CONFIRM_HOUR = 9

# Lead-state tags that mean "we have already decided not to chase this lead".
# Mirrors send_followups._exclude_suppressed_states -- the decision lives on the
# lead, so every send path reads the same tags rather than re-deriving them.
SUPPRESSED_TAGS = (
    '[HANDED_OFF]',
    '[PARKED]',
    '[STOP_REQUESTED]',
    '[OOS_DECLINED]',
    '[EXCLUDED_AREA',
)


# -- Local-time helpers ------------------------------------------------------

def _local(dt):
    """An aware datetime in the project's local zone (Africa/Johannesburg)."""
    if timezone.is_aware(dt):
        return timezone.localtime(dt)
    return timezone.make_aware(dt, timezone.get_current_timezone())


def _at_local_hour(day, hour):
    """An aware datetime at `hour` local time on `day` (a date)."""
    naive = datetime.combine(day, time(hour=hour))
    return timezone.make_aware(naive, timezone.get_current_timezone())


def next_day_noon(after):
    """Noon local on the day AFTER `after` -- "next day at 12pm"."""
    return _at_local_hour(_local(after).date() + timedelta(days=1), ASK_HOUR)


def visit_end(appointment):
    """When the site visit finished, or None when it was never scheduled.

    Prefers the explicit end_datetime, but only when it is CONSISTENT with the
    start. Appointment.save() fills end_datetime once and never recomputes it,
    so a rescheduled visit keeps the end time of the slot it used to hold -- and
    trusting that blindly would have us emailing the plumber a debrief form for a
    visit that has not happened yet. A stale end falls back to start + duration.
    """
    start = getattr(appointment, 'scheduled_datetime', None)
    end = getattr(appointment, 'end_datetime', None)
    if end and (start is None or end >= start):
        return end
    if not start:
        return None
    return start + (getattr(appointment, 'duration', None) or timedelta(hours=2))


# -- Guards ------------------------------------------------------------------

def lead_is_suppressed(appointment):
    """True when we must not send this lead a proactive message.

    Covers the three states the brief names -- parked, handed-off, confirmed --
    plus the lead-level stops the rest of the codebase already honours. A lead
    whose job is booked is "confirmed": there is nothing left to ask them.
    """
    notes = getattr(appointment, 'internal_notes', '') or ''
    if any(tag in notes for tag in SUPPRESSED_TAGS):
        return True
    if not getattr(appointment, 'is_lead_active', True):
        return True
    if getattr(appointment, 'status', '') in ('cancelled', 'completed'):
        return True
    # The job is on the diary -- the lead has converted, so the ask sequence has
    # nothing left to chase.
    if getattr(appointment, 'job_scheduled_datetime', None):
        return True
    if getattr(appointment, 'job_status', '') in ('scheduled', 'in_progress', 'completed'):
        return True
    return False


def lead_email(appointment):
    """The lead's email address, or '' -- never None, so callers can just test
    it. Email is the follow-up channel, so an empty string is a real branch."""
    return (getattr(appointment, 'customer_email', '') or '').strip()


# -- Report lifecycle --------------------------------------------------------

def ensure_report(appointment):
    """The report row for this appointment, created if missing.

    Called from the detail page (so the banner has a link), from the cron (so
    the fallback email has one) and from the form view. get_or_create keeps the
    OneToOne single even when two of those race.
    """
    from bot.models import SiteVisitReport

    report, _ = SiteVisitReport.objects.get_or_create(
        appointment=appointment,
        defaults={'tenant_id': appointment.tenant_id},
    )
    return report


def form_url(report, absolute=True):
    """The tokenized, single-use form URL for this report."""
    path = reverse('site_visit_form', kwargs={'token': report.token})
    if not absolute:
        return path
    return '{}{}'.format(settings.SITE_URL.rstrip('/'), path)


def is_due_for_report(appointment, now=None):
    """True when this appointment is a finished site visit wanting a debrief.

    A visit that was never scheduled or is still in the future does not.
    """
    now = now or timezone.now()
    if getattr(appointment, 'appointment_type', 'site_visit') != 'site_visit':
        return False
    if getattr(appointment, 'status', '') != 'confirmed':
        return False
    end = visit_end(appointment)
    return bool(end) and end <= now


def due_visits(now=None, tenant=None):
    """Every finished site visit that should have a debrief by now."""
    from bot.models import Appointment

    now = now or timezone.now()
    if tenant is None:
        qs = Appointment.objects.real()
    else:
        qs = Appointment.objects.for_tenant(tenant).exclude(
            phone_number__startswith='whatsapp:+999')
    return (
        qs.filter(
            appointment_type='site_visit',
            status='confirmed',
            scheduled_datetime__isnull=False,
            scheduled_datetime__lte=now,
        )
        # Synthetic keys: quotation-only stubs are not leads anybody visited.
        .exclude(phone_number__startswith='quotation_only_')
        .select_related('site_visit_report', 'tenant')
        .order_by('scheduled_datetime')
    )


# -- Submitting the form -----------------------------------------------------

def apply_submission(report, *, outcome, expectation='', expected_date=None,
                     expected_timeframe='', job_notes='', email='',
                     user=None, via='app', now=None):
    """Record the plumber's answers and arm whatever comes next.

    This is the ONLY writer of the single-use gate. It is idempotent by
    refusal: a report that already carries a ``submitted_at`` is left exactly
    as it is and False is returned, so a second tap of either entry point
    cannot re-arm the sequence or re-send anything.
    """
    now = now or timezone.now()
    if report.submitted_at:
        return False

    apt = report.appointment

    address = (email or '').strip()
    if address and address != (apt.customer_email or ''):
        apt.customer_email = address
        apt.save(update_fields=['customer_email'])

    report.submitted_at = now
    report.submitted_by = user if getattr(user, 'is_authenticated', False) else None
    report.submitted_via = via
    report.outcome = outcome
    report.job_notes = job_notes or ''

    if outcome == 'went_ahead':
        report.expectation = expectation or 'unknown'
        report.expected_date = expected_date if expectation == 'specific_date' else None
        report.expected_timeframe = expected_timeframe if expectation == 'timeframe' else ''
        _arm_sequence(report, now=now)
    else:
        # No-show, rescheduled, not proceeding: these route to reschedule/close,
        # never to a quote or a follow-up sequence.
        report.expectation = ''
        report.expected_date = None
        report.expected_timeframe = ''
        report.sequence = 'stopped'
        report.next_action_at = None

    report.save()

    # The visit is now formally complete on the lead itself, which is what the
    # jobs board and the schedule-job screen read.
    if outcome == 'went_ahead' and not apt.site_visit_completed:
        try:
            apt.mark_site_visit_completed(notes=report.job_notes or '',
                                          assessment=apt.plumber_assessment or '')
        except Exception:
            logger.exception('mark_site_visit_completed failed - apt %s', apt.pk)
    return True


def _arm_sequence(report, now=None):
    """Point the report at its next action, given the answers it now holds.

    Case A when a specific date is on file, Case B otherwise. Sets
    ``next_action_at`` to a real moment or finishes the sequence -- the cron
    never has to re-derive the branch.
    """
    now = now or timezone.now()

    if report.expectation == 'specific_date' and report.expected_date:
        if report.confirmation_sent_at or _local(now).date() > report.expected_date:
            # Already confirmed, or the date has gone by -- confirming a date in
            # the past would be worse than saying nothing.
            report.sequence = 'done'
            report.next_action_at = None
            return
        due = _at_local_hour(report.expected_date - timedelta(days=CONFIRM_DAYS_BEFORE),
                             CONFIRM_HOUR)
        report.sequence = 'confirm'
        # Booked closer than two days out: confirm on the next tick rather than
        # at a moment that has already passed.
        report.next_action_at = max(due, now)
        return

    report.sequence = 'asks'
    if report.ask_count == 0:
        report.next_action_at = next_day_noon(report.submitted_at or now)
    else:
        report.next_action_at = _next_ask_due(report)


def _next_ask_due(report):
    """When the ask after the one just sent falls due."""
    base = report.last_ask_at or report.submitted_at or timezone.now()
    if report.ask_count == 1:
        return base + timedelta(days=ASK_2_AFTER_DAYS)
    if report.ask_count == 2:
        return base + timedelta(days=ASK_3_AFTER_DAYS)
    return None


# -- The lead giving a date later (Case B -> Case A) -------------------------

_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
_WEEKDAYS = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}
_MONTH_NAMES = '|'.join(_MONTHS)
_WEEKDAY_NAMES = '|'.join(_WEEKDAYS)


def extract_expected_date(text, today=None):
    """A specific calendar date from the lead's own words, or None.

    Deterministic on purpose (the project rule for short/fuzzy strings): the
    only thing that may flip a lead from the ask sequence to the confirmation
    branch is an unambiguous date. A rough timeframe ("in a few weeks", "next
    month sometime") must NOT match -- that is the very thing Case B exists to
    chase, and treating it as a date would send a confirmation for a day the
    lead never named.
    """
    if not text:
        return None
    today = today or timezone.localdate()
    low = str(text).lower()

    # 2026-03-15
    m = re.search(r'\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b', low)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 15/03, 15/03/26, 15-03-2026 (day first -- the local convention)
    m = re.search(r'\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b', low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        raw_year = m.group(3)
        if raw_year:
            year = int(raw_year) + 2000 if len(raw_year) == 2 else int(raw_year)
            return _safe_date(year, month, day)
        return _roll_forward(today, month, day)

    # "15 March", "15th of March"
    m = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(' + _MONTH_NAMES + r')\w*', low)
    if m:
        return _roll_forward(today, _MONTHS[m.group(2)[:3]], int(m.group(1)))

    # "March 15"
    m = re.search(r'\b(' + _MONTH_NAMES + r')\w*\s+(\d{1,2})(?:st|nd|rd|th)?\b', low)
    if m:
        return _roll_forward(today, _MONTHS[m.group(1)[:3]], int(m.group(2)))

    # "next Monday", "on Tuesday" -- a named day IS a specific date.
    m = re.search(r'\b(?:next|on|this)\s+(' + _WEEKDAY_NAMES + r')\b', low)
    if m:
        ahead = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)

    if re.search(r'\btomorrow\b', low):
        return today + timedelta(days=1)
    return None


def _roll_forward(today, month, day):
    """A day/month with no year means the next time that date comes round."""
    found = _safe_date(today.year, month, day)
    if found and found < today:
        found = _safe_date(today.year + 1, month, day)
    return found


def _safe_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def record_lead_expected_date(appointment, when, source=''):
    """The lead named a day -- move them onto the confirmation branch.

    ``when`` may be a date or a datetime. Returns True when a live report was
    switched. Callers pass anything; the guards here decide.
    """
    report = getattr(appointment, 'site_visit_report', None)
    if report is None or report.sequence not in ('awaiting_form', 'asks', 'confirm'):
        return False
    day = when.date() if isinstance(when, datetime) else when
    if not day or day < timezone.localdate():
        return False
    if report.sequence == 'confirm' and report.expected_date == day:
        return False

    report.expectation = 'specific_date'
    report.expected_date = day
    report.expected_timeframe = ''
    # A date arriving before the form was ever submitted still answers the
    # question the form asks, so the sequence may start from it (Case C).
    if not report.submitted_at:
        report.submitted_at = timezone.now()
        report.submitted_via = report.submitted_via or 'link'
        report.outcome = report.outcome or 'went_ahead'
    _arm_sequence(report)
    report.save()
    logger.info('Post-visit: lead %s gave a date (%s) via %s - switched to Case A',
                appointment.pk, day, source or 'unknown')
    return True


def note_inbound_reply(appointment, text, source=''):
    """Hook for every inbound customer turn (WhatsApp and email).

    A no-op unless this lead has a live post-visit report AND the message
    carries an unambiguous date. Never raises into the message pipeline -- a
    date parser is not allowed to break inbound handling.
    """
    try:
        if getattr(appointment, 'site_visit_report', None) is None:
            return False
        day = extract_expected_date(text)
        if not day:
            return False
        return record_lead_expected_date(appointment, day, source=source or 'inbound')
    except Exception:
        logger.exception('note_inbound_reply failed - apt %s', getattr(appointment, 'pk', None))
        return False


# -- The scheduler tick ------------------------------------------------------

def run_post_visit_tick(now=None, dry_run=False, log=None, tenant=None):
    """One pass of the whole post-visit machine.

    Idempotent: every send is gated by a timestamp written the moment it goes
    out, so a five-minute cron re-running this is safe. Returns a counter dict.

    Order matters. The fallback email is considered BEFORE the Case C deadline,
    so a plumber whose visit ended at 11:30 still gets the link at 12:05 rather
    than finding the lead sequence already running; and the lead-state guard is
    re-checked before every customer-facing send, never only at the top.
    """
    now = now or timezone.now()
    stats = {'form_emails': 0, 'asks': 0, 'confirmations': 0,
             'cold': 0, 'no_email': 0, 'skipped': 0}

    def emit(msg):
        if log:
            log(msg)

    for apt in due_visits(now=now, tenant=tenant):
        try:
            report = getattr(apt, 'site_visit_report', None)
            if report is None:
                report = ensure_report(apt)

            if report.is_open:
                _tick_open_report(apt, report, now, dry_run, emit, stats)
            else:
                _tick_sequence(apt, report, now, dry_run, emit, stats)
        except Exception as exc:  # noqa: BLE001 - one bad lead must not stop the run
            logger.exception('post-visit tick failed for apt %s', apt.pk)
            emit('[error] apt {}: {}'.format(apt.pk, exc))

    return stats


def _tick_open_report(apt, report, now, dry_run, emit, stats):
    """The debrief has not been submitted: chase the plumber, then Case C."""
    end = visit_end(apt)

    # 1. The 35-minute fallback link to the plumber.
    if (not report.fallback_email_sent_at and end
            and now >= end + timedelta(minutes=FALLBACK_EMAIL_DELAY_MINUTES)):
        from bot.plumber_notifications import send_site_visit_form_email
        if dry_run:
            emit('[dry-run] visit form link -> plumber (apt {})'.format(apt.pk))
            stats['form_emails'] += 1
        elif send_site_visit_form_email(report):
            report.fallback_email_sent_at = now
            report.save(update_fields=['fallback_email_sent_at'])
            stats['form_emails'] += 1
            emit('[form] link emailed to the plumber (apt {})'.format(apt.pk))

    # 2. Case C - still nothing by noon the next day. Treat it as "no date, lead
    #    has email" and start Case B from here.
    #
    #    Case C leaves the form OPEN (the plumber may still get to it), so this
    #    branch is reached again on every later tick. It must therefore start
    #    the sequence exactly once: once started, the report is no longer
    #    'awaiting_form' and the ask cadence owns the timing. Without this the
    #    next tick would reset next_action_at to now and fire ask 2 minutes
    #    after ask 1.
    if report.sequence != 'awaiting_form':
        _tick_sequence(apt, report, now, dry_run, emit, stats)
        return

    if now < next_day_noon(end or apt.scheduled_datetime):
        return

    if lead_is_suppressed(apt):
        stats['skipped'] += 1
        return

    if not lead_email(apt):
        # The form is the only place a missing email can be added, and it was
        # never submitted, so there is nothing to follow up on.
        if not report.no_email_notified_at:
            from bot.plumber_notifications import send_post_visit_handback_email
            if dry_run:
                emit('[dry-run] no-email handback -> plumber (apt {})'.format(apt.pk))
            elif send_post_visit_handback_email(apt, reason='no_email'):
                report.no_email_notified_at = now
                report.sequence = 'stopped'
                report.next_action_at = None
                report.save(update_fields=['no_email_notified_at', 'sequence',
                                           'next_action_at'])
                emit('[handback] no email on file (apt {})'.format(apt.pk))
            stats['no_email'] += 1
        return

    if dry_run:
        emit('[dry-run] Case C -> start the ask sequence (apt {})'.format(apt.pk))
        stats['asks'] += 1
        return

    # Case C is defined as "no date, lead has email": the sequence starts, and
    # the form stays open in case the plumber gets to it later.
    report.expectation = 'unknown'
    report.sequence = 'asks'
    report.next_action_at = now
    report.save(update_fields=['expectation', 'sequence', 'next_action_at'])
    emit('[case C] ask sequence started without the form (apt {})'.format(apt.pk))
    _tick_sequence(apt, report, now, dry_run, emit, stats)


def _tick_sequence(apt, report, now, dry_run, emit, stats):
    """The lead-facing half: Case A confirmation, or the Case B asks."""
    if report.sequence not in ('confirm', 'asks'):
        return
    if not report.next_action_at or report.next_action_at > now:
        return

    # The state guard sits in front of every customer-facing send, not just at
    # the top of the run: a lead can be parked or booked between two ticks.
    if lead_is_suppressed(apt):
        stats['skipped'] += 1
        emit('[skip] suppressed lead state (apt {})'.format(apt.pk))
        return

    if not lead_email(apt):
        stats['skipped'] += 1
        return

    if report.sequence == 'confirm':
        _send_confirmation(apt, report, now, dry_run, emit, stats)
    else:
        _send_ask(apt, report, now, dry_run, emit, stats)


def _send_confirmation(apt, report, now, dry_run, emit, stats):
    """Case A: one confirmation, two days before the date the lead gave."""
    if not report.expected_date:
        # Nothing to confirm and nothing to render - never send a date-shaped
        # email with no date in it. Fall back to the ask sequence instead.
        report.sequence = 'asks'
        report.next_action_at = now
        report.save(update_fields=['sequence', 'next_action_at'])
        return
    if report.expected_date < _local(now).date():
        report.sequence = 'done'
        report.next_action_at = None
        report.save(update_fields=['sequence', 'next_action_at'])
        return

    from bot.customer_emails import send_post_visit_confirmation_email
    if dry_run:
        emit('[dry-run] confirmation for {} (apt {})'.format(report.expected_date, apt.pk))
        stats['confirmations'] += 1
        return

    if send_post_visit_confirmation_email(apt, report.expected_date):
        report.confirmation_sent_at = now
        report.sequence = 'done'
        report.next_action_at = None
        report.save(update_fields=['confirmation_sent_at', 'sequence', 'next_action_at'])
        apt.add_conversation_message(
            'assistant',
            '[POST-VISIT CONFIRMATION] Emailed to confirm {}'.format(
                report.expected_date.strftime('%d %b %Y')))
        stats['confirmations'] += 1
        emit('[confirm] emailed for {} (apt {})'.format(report.expected_date, apt.pk))
    else:
        # Retry on the next tick rather than losing the confirmation entirely.
        report.next_action_at = now + timedelta(hours=1)
        report.save(update_fields=['next_action_at'])


def _send_ask(apt, report, now, dry_run, emit, stats):
    """Case B: asks 1, 2 and 3, then cold and back to the plumber."""
    if report.ask_count >= MAX_ASKS:
        _mark_cold(apt, report, now, dry_run, emit, stats)
        return

    ask_number = report.ask_count + 1
    from bot.customer_emails import send_post_visit_ask_email
    if dry_run:
        emit('[dry-run] post-visit ask #{} (apt {})'.format(ask_number, apt.pk))
        stats['asks'] += 1
        return

    if not send_post_visit_ask_email(apt, ask_number):
        report.next_action_at = now + timedelta(hours=1)
        report.save(update_fields=['next_action_at'])
        return

    report.ask_count = ask_number
    report.last_ask_at = now
    report.next_action_at = _next_ask_due(report)
    if report.next_action_at is None:
        # Ask 3 has gone out. The cold decision waits the same week the last gap
        # used, so the lead gets a real chance to answer it.
        report.next_action_at = now + timedelta(days=ASK_3_AFTER_DAYS)
    report.save(update_fields=['ask_count', 'last_ask_at', 'next_action_at'])
    apt.add_conversation_message(
        'assistant', '[POST-VISIT ASK {}] Emailed the lead'.format(ask_number))
    stats['asks'] += 1
    emit('[ask] #{} emailed (apt {})'.format(ask_number, apt.pk))


def _mark_cold(apt, report, now, dry_run, emit, stats):
    """After ask 3 with no result: cold, plumber told, manual close from here."""
    from bot.models import LeadStatus
    from bot.plumber_notifications import send_post_visit_handback_email

    if dry_run:
        emit('[dry-run] mark cold + hand back (apt {})'.format(apt.pk))
        stats['cold'] += 1
        return

    apt.lead_status = LeadStatus.COLD
    apt.followup_stage = 'completed'
    apt.save(update_fields=['lead_status', 'followup_stage'])
    # The bot stops chasing; the plumber owns the close from here.
    apt.mark_handed_off()

    send_post_visit_handback_email(apt, reason='gone_cold')
    report.cold_notified_at = now
    report.sequence = 'cold'
    report.next_action_at = None
    report.save(update_fields=['cold_notified_at', 'sequence', 'next_action_at'])
    apt.add_conversation_message(
        'assistant', '[POST-VISIT] No reply after three asks, lead marked cold')
    stats['cold'] += 1
    emit('[cold] lead marked cold and handed back (apt {})'.format(apt.pk))
