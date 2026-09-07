"""
bot/phone_quote.py
==================
The plumber quoted over the phone: the row, the form, and the follow-up.

The third route to a quote, beside ``post_visit`` (someone went and looked) and
``plan_quote`` (a drawing arrived). It follows both of those deliberately —
anchors and offsets rather than a job queue, every send gated by the timestamp
written as it goes out, lead state re-checked before EVERY customer-facing
send, and one bad lead never stopping the run.

What is different is where the row starts. ``plan_quote`` opens on something
the LEAD did, so its first job is to get hold of the plumber and chase them
through four emails. Here the plumber opens the row themselves, in the
dashboard, while the customer is on the phone. There is nobody to chase, so the
row is created and answered in one sitting and the cadence starts at the part
the other paths only reach afterwards: asking the customer about the quote.

That also means no contact-window check on the follow-up. ``plan_quote`` guards
its Branch B because it fires on a clock and +12h from a 3pm plan is 3am. This
one is triggered by a plumber filling in a form, which is itself proof of
working hours, exactly like that module's Branch A.
"""

import logging

from django.utils import timezone

from .models import PhoneQuoteRequest

logger = logging.getLogger(__name__)


# How long after the call before we ask the customer about the quote. Same
# offset as plan_quote's Branch A, and for the same reason: long enough that
# the quote has actually been sent, short enough that we are still the people
# they just spoke to.
LEAD_FOLLOWUP_AFTER_FORM_HOURS = 1


def ensure_request(appointment, when=None):
    """The row for this lead, created if it does not exist yet.

    Idempotent, like ``post_visit.ensure_report``: the plumber may open the
    form, wander off, and come back to it from the appointment screen. That
    must be the same row, or the single-use gate means nothing.
    """
    row = getattr(appointment, 'phone_quote_request', None)
    if row is not None:
        return row
    return PhoneQuoteRequest.objects.create(
        appointment=appointment,
        tenant=getattr(appointment, 'tenant', None),
    )


def form_url(row, absolute=True):
    """The tokenized, single-use form URL for this request."""
    from django.conf import settings
    from django.urls import reverse

    path = reverse('phone_quote_form', kwargs={'token': row.token})
    if not absolute:
        return path
    return '{}{}'.format(settings.SITE_URL.rstrip('/'), path)


def lead_is_done(appointment) -> bool:
    """May we still message this lead at all?

    Delegates to plan_quote rather than restating the rule: parked, handed off,
    stopped, out of area, inactive, cancelled, completed, or already booked.
    One resolver for "leave this lead alone" across all three quote paths, so a
    lead who is suppressed on one is suppressed on every one.
    """
    from .plan_quote import lead_is_done as _done
    return _done(appointment)


def apply_plumber_form(row, *, outcome='quoting', quote_sent=False,
                       lead_email='', expectation='', expected_date=None,
                       expected_timeframe='', job_notes='', customer_area='',
                       quote_amount=None, user=None, now=None):
    """The plumber answered. Closes the form.

    Single use, like ``SiteVisitReport.apply_submission`` and
    ``plan_quote.apply_plumber_form``: a row that already carries
    ``form_completed_at`` is left exactly as it is. The link is reachable from
    the appointment screen and from a phone, and a second visit to it must show
    what was already said rather than overwrite it or raise a second quote.

    Returns True when this call was the one that closed it.
    """
    if row.form_completed_at:
        return False

    now = now or timezone.now()
    row.form_completed_at = now
    row.outcome = outcome
    # 'sent_confirmed' only when the plumber says the quote has already gone.
    # Raising it in the app does not count: that is what the send flow marks.
    row.quote_status = 'sent_confirmed' if quote_sent else ''
    row.expectation = expectation
    row.expected_date = expected_date
    row.expected_timeframe = expected_timeframe
    row.job_notes = job_notes
    row.customer_area = (customer_area or '').strip()
    if user is not None and getattr(user, 'is_authenticated', False):
        row.completed_by = user
    fields = ['form_completed_at', 'outcome', 'quote_status', 'expectation',
              'expected_date', 'expected_timeframe', 'job_notes',
              'customer_area', 'completed_by']

    if quote_amount is not None:
        row.quote_amount = quote_amount
        fields.append('quote_amount')

    row.save(update_fields=fields)

    appointment = row.appointment
    lead_fields = []

    # The form is the one place a missing lead email can be added, which is
    # what makes any later follow-up possible at all.
    email = (lead_email or '').strip()
    if email and not getattr(appointment, 'customer_email', None):
        appointment.customer_email = email
        lead_fields.append('customer_email')

    # An area a human typed on a call beats one the flow inferred. The
    # extraction prompt's example suburb reached 221 leads as though it were
    # fact, so this overwrites rather than filling a blank.
    area = (customer_area or '').strip()
    if area and area != getattr(appointment, 'customer_area', ''):
        appointment.customer_area = area
        lead_fields.append('customer_area')

    # The notes are what the plumber heard on the call, which is a better
    # description of the job than anything the bot extracted. Only fills a
    # blank, though: the lead's own words win where we have them.
    notes = (job_notes or '').strip()
    if notes and not (getattr(appointment, 'project_description', '') or '').strip():
        appointment.project_description = notes
        lead_fields.append('project_description')

    if lead_fields:
        appointment.save(update_fields=lead_fields)

    # A named date switches the lead onto the confirmation branch, exactly as
    # the site-visit debrief and the plan-quote form do. Same resolver, so the
    # three paths cannot drift on what a date means.
    if expectation == 'specific_date' and expected_date:
        try:
            from .post_visit import record_lead_expected_date
            record_lead_expected_date(appointment, expected_date,
                                      source='phone quote form')
        except Exception:
            logger.warning('Could not record the expected date for apt %s',
                           row.appointment_id, exc_info=True)

    return True


def stop(row, reason=''):
    """No quote and no follow-ups will go out for this row."""
    if row.stopped_at:
        return False
    row.stopped_at = timezone.now()
    row.save(update_fields=['stopped_at'])
    logger.info('Phone quote stopped for apt %s (%s)', row.appointment_id, reason)
    return True


def open_requests(now=None, tenant=None):
    """Rows with the form answered and the customer not yet followed up."""
    qs = (PhoneQuoteRequest.objects
          .select_related('appointment', 'tenant')
          .filter(form_completed_at__isnull=False,
                  lead_followup_sent_at__isnull=True,
                  stopped_at__isnull=True,
                  outcome='quoting'))
    if tenant is not None:
        qs = qs.filter(tenant=tenant)
    return qs


def _due(anchor, hours, now):
    from datetime import timedelta
    return bool(anchor) and now >= anchor + timedelta(hours=hours)


def run_phone_quote_tick(now=None, dry_run=False, log=None, tenant=None):
    """One pass of the phone-quote machine.

    Idempotent: the single send is gated by the timestamp written the moment it
    goes out, so a five-minute cron re-running this is safe. Returns a counter
    dict, the same shape the other two ticks return.
    """
    now = now or timezone.now()
    stats = {'lead_followups': 0, 'skipped': 0, 'errors': 0}

    def emit(msg):
        if log:
            log(msg)

    for row in open_requests(now=now, tenant=tenant):
        try:
            apt = row.appointment
            if not _due(row.form_completed_at,
                        LEAD_FOLLOWUP_AFTER_FORM_HOURS, now):
                continue

            # Re-checked immediately before a customer-facing send, never only
            # at the top of the run: a lead can be parked or booked between two
            # ticks.
            if lead_is_done(apt):
                stats['skipped'] += 1
                continue

            if dry_run:
                emit('[dry-run] quote follow-up -> lead (apt {})'.format(apt.pk))
                stats['lead_followups'] += 1
                continue

            row.quote_status = row.quote_status or 'assumed'
            row.lead_followup_sent_at = now
            row.save(update_fields=['quote_status', 'lead_followup_sent_at'])
            stats['lead_followups'] += 1
            emit('[follow-up] lead asked about the phone quote (apt {})'
                 .format(apt.pk))
        except Exception:
            # One bad lead never stops the run.
            stats['errors'] += 1
            logger.exception('Phone quote tick failed for apt %s',
                             getattr(row, 'appointment_id', None))

    return stats
