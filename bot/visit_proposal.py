"""
bot/visit_proposal.py
=====================
The future-dated lead on the measure path (spec §11).

A lead who wants the job at the end of October will not book a visit for
tomorrow. Push one and you lose them. So we name a real day near their target,
ask if it works, and treat their yes as a plan rather than a booking.

**The constraint that shapes all of this:** we message leads on WhatsApp only
inside the free 24 hours after their last message, and we do not pay for
template sends. A check-in three weeks later cannot go by WhatsApp. It has to
be email, and that is precisely why the portfolio exists — it is the reason we
can ask for an address at all.

A lead who refuses an email is not dropped. The check-ins go to the PLUMBER
instead, carrying the job details and a prefilled message they can send from
their own phone. That starts a fresh 24-hour window, which is something we
cannot do for ourselves.

Same construction rules as `post_visit.py` and `plan_quote.py`: anchors and
offsets rather than a queue, every send gated by the timestamp written as it
goes out, lead state re-checked before each one, and one bad lead never stops
the run.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Days before the lead's target date that we check in. Two touches, far enough
# apart that the second is not nagging and close enough that the first is not
# forgotten by the time the day comes.
CHECKIN_OFFSET_DAYS = (7, 4)

# Plumber check-ins go out in the morning, when they are planning their week.
# The lead's own check-in has no such constraint: it is an email, and people
# read email when they read it.
PLUMBER_CHECKIN_HOUR = 8

# A proposal nobody answered by the day itself is not chased further. The visit
# was never real, and a lead who ignored two emails will not thank us for a
# third the morning of.
LAPSE_AFTER_TARGET = True


def ensure_proposal(appointment, *, target_date, proposed_date, when=None):
    """The row for a lead who has softly agreed to a future visit.

    Idempotent. Re-offering a date to a lead who already has a proposal
    UPDATES it rather than opening a second one: they have one visit in mind,
    however many times the conversation circles back to it.
    """
    from bot.models import VisitProposal

    row = getattr(appointment, 'visit_proposal', None)
    if row is None:
        row, _created = VisitProposal.objects.get_or_create(
            appointment=appointment,
            defaults={
                'tenant': getattr(appointment, 'tenant', None),
                'target_date': target_date,
                'proposed_date': proposed_date,
            },
        )
        return row

    changed = []
    if target_date and row.target_date != target_date:
        row.target_date = target_date
        changed.append('target_date')
    if proposed_date and row.proposed_date != proposed_date:
        row.proposed_date = proposed_date
        changed.append('proposed_date')
    # A lead who names a new date has re-opened a proposal they had declined.
    if changed and row.state == 'declined':
        row.state = 'proposed'
        row.responded_at = None
        changed += ['state', 'responded_at']
    if changed:
        row.save(update_fields=changed)
    return row


def answer_url(row, answer: str, absolute=True):
    """The yes / no link that goes in the check-in email.

    Two URLs rather than a form, because this has to work from an email client
    on a phone with one tap. The token carries the authority; the answer is in
    the path so there is nothing to fill in.
    """
    from django.conf import settings
    from django.urls import reverse

    path = reverse('visit_proposal_answer',
                   kwargs={'token': row.token, 'answer': answer})
    if not absolute:
        return path
    return '{}{}'.format(settings.SITE_URL.rstrip('/'), path)


def record_email_choice(row, *, gave_email: bool, channel: str = ''):
    """What the lead did when we asked for an address.

    A refusal is a real answer and has to be recorded, not left as unknown: it
    is what routes the check-ins to the plumber instead, and it is also why we
    must never ask them again.
    """
    row.email_opt_in = 'yes' if gave_email else 'declined'
    fields = ['email_opt_in']
    if channel:
        row.portfolio_sent_channel = channel
        fields.append('portfolio_sent_channel')
    row.save(update_fields=fields)
    return row


def open_proposals(tenant=None):
    from bot.models import VisitProposal

    rows = VisitProposal.objects.select_related('appointment').filter(
        state='proposed', target_date__isnull=False,
    )
    if tenant is not None:
        rows = rows.filter(tenant=tenant)
    return rows


def _local_date(when):
    from bot.management.commands.send_followups import SA_TIMEZONE
    return when.astimezone(SA_TIMEZONE).date()


def _due_checkin(row, now):
    """Which check-in is due: 1, 2, or None.

    Measured in days before the TARGET, not as offsets from each other, so a
    tick missed for a day cannot push the second one past the visit.
    """
    if not row.target_date:
        return None
    days_out = (row.target_date - _local_date(now)).days
    if days_out <= CHECKIN_OFFSET_DAYS[1] and not row.checkin_2_sent_at:
        # Only after the first has gone; otherwise a proposal made five days
        # out would fire both at once.
        return 2 if row.checkin_1_sent_at else 1
    if days_out <= CHECKIN_OFFSET_DAYS[0] and not row.checkin_1_sent_at:
        return 1
    return None


def run_visit_proposal_tick(now=None, dry_run=False, log=None, tenant=None):
    """One pass of the check-in machine. Returns a counter dict."""
    now = now or timezone.now()
    stats = {'lead_emails': 0, 'plumber_emails': 0, 'lapsed': 0, 'skipped': 0}

    def emit(msg):
        if log:
            log(msg)

    for row in open_proposals(tenant=tenant):
        try:
            _tick_one(row, now, dry_run, emit, stats)
        except Exception as exc:  # noqa: BLE001 - one bad lead must not stop the run
            logger.exception('visit-proposal tick failed for apt %s',
                             row.appointment_id)
            emit('[error] apt {}: {}'.format(row.appointment_id, exc))

    return stats


def _tick_one(row, now, dry_run, emit, stats):
    from bot.plan_quote import lead_is_done

    apt = row.appointment

    # The visit day has come and gone with no answer. Stop rather than send a
    # third ask about a date that has passed.
    if LAPSE_AFTER_TARGET and row.target_date and _local_date(now) > row.target_date:
        if not dry_run:
            row.state = 'lapsed'
            row.save(update_fields=['state'])
        stats['lapsed'] += 1
        emit('[lapsed] no answer by the target (apt {})'.format(apt.pk))
        return

    # Re-checked before every send, never once at the top: a lead can book or
    # be parked between two ticks, and three weeks is a long time.
    if lead_is_done(apt):
        stats['skipped'] += 1
        return

    number = _due_checkin(row, now)
    if number is None:
        return

    if row.goes_to_the_plumber:
        _send_plumber_checkin(row, number, now, dry_run, emit, stats)
    else:
        _send_lead_checkin(row, number, now, dry_run, emit, stats)


def _mark_sent(row, number, now):
    field = 'checkin_{}_sent_at'.format(number)
    setattr(row, field, now)
    row.save(update_fields=[field])


def _send_lead_checkin(row, number, now, dry_run, emit, stats):
    apt = row.appointment
    if dry_run:
        emit('[dry-run] check-in {} -> lead (apt {})'.format(number, apt.pk))
        stats['lead_emails'] += 1
        return
    from bot.plumber_notifications import send_visit_confirm_email
    if send_visit_confirm_email(row, number=number):
        _mark_sent(row, number, now)
        stats['lead_emails'] += 1
        emit('[check-in {}] emailed the lead (apt {})'.format(number, apt.pk))


def _send_plumber_checkin(row, number, now, dry_run, emit, stats):
    """No email on file, so the plumber does the outreach.

    Held to the morning: they are planning their week, and a lead-outreach
    prompt at 9pm gets read and forgotten.
    """
    from bot.management.commands.send_followups import SA_TIMEZONE

    apt = row.appointment
    if now.astimezone(SA_TIMEZONE).hour < PLUMBER_CHECKIN_HOUR:
        return

    if dry_run:
        emit('[dry-run] check-in {} -> plumber (apt {})'.format(number, apt.pk))
        stats['plumber_emails'] += 1
        return
    from bot.plumber_notifications import send_visit_handoff_email
    if send_visit_handoff_email(row, number=number):
        _mark_sent(row, number, now)
        stats['plumber_emails'] += 1
        emit('[check-in {}] handed to the plumber (apt {})'.format(number, apt.pk))


def apply_lead_answer(row, *, confirmed: bool, now=None):
    """The lead answered the check-in email. This is the only place a
    future-dated visit becomes a real booking.

    Single use: a lead who taps yes on both emails confirms once. Returns True
    when this call was the one that decided it.
    """
    if not row.is_open:
        return False

    now = now or timezone.now()

    # The diary write comes FIRST, and the row is only marked once it lands.
    # The other order looks harmless and is not: if the booking throws, the row
    # already reads 'confirmed', is_open goes false, the check-ins stop, and
    # nobody is on the diary. The lead would be told we could not save it while
    # the system quietly believed they were booked.
    if confirmed and row.proposed_date:
        _book_the_visit(row, now)

    row.state = 'confirmed' if confirmed else 'declined'
    row.responded_at = now
    row.save(update_fields=['state', 'responded_at'])
    return True


def _book_the_visit(row, now):
    """Put the agreed day on the diary.

    Only reached from a yes on the check-in email. Until this runs, the visit
    is a plan the lead nodded at, and the rest of the dashboard is right not to
    show it as booked.
    """
    from datetime import datetime, time

    from bot.management.commands.send_followups import SA_TIMEZONE

    apt = row.appointment
    try:
        when = SA_TIMEZONE.localize(
            datetime.combine(row.proposed_date, time(hour=9)))
        apt.scheduled_datetime = when
        apt.status = 'confirmed'
        apt.save(update_fields=['scheduled_datetime', 'status'])
    except Exception:
        # Never leave the row saying confirmed while the diary disagrees: the
        # caller sees the failure and the lead is told nothing.
        logger.exception('Could not book the proposed visit for apt %s', apt.pk)
        raise
