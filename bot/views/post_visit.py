"""
bot/views/post_visit.py
=======================
The two entry points into the post-visit debrief form, and the email half of
the quote send.

Both entry points open the SAME tokenized URL:

* ``site_visit_start`` — the in-app button on the appointment detail screen
  (staff-only). It makes sure the report row exists, then redirects to the
  token URL.
* ``site_visit_form``  — the public, token-gated form itself, which is also
  what the 35-minute fallback email links to.

Because there is one URL and one ``submitted_at``, the two paths cannot both
submit: whichever gets there first closes the form, and the cron stops sending
the fallback email the moment that timestamp is set.

The form is deliberately PUBLIC (token only). The plumber taps the link on
their phone, from an email, usually without a dashboard session. The token is a
uuid4 and the row is single-use, so the link is the credential. Anything that
follows from it and needs real authority — creating the quote — goes to a
staff-gated screen and will ask them to log in there.
"""

import logging
import os

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..decorators import staff_required
from ..models import Appointment, Quotation, SiteVisitReport
from ..post_visit import apply_submission, ensure_report, form_url, is_due_for_report
from ..utils import _append_admin_note

logger = logging.getLogger(__name__)


@staff_required
def site_visit_start(request, pk):
    """In-app entry: make sure the report exists, then open the shared form."""
    appointment = get_object_or_404(
        Appointment.objects.for_tenant_or_seed(getattr(request, 'tenant', None)), pk=pk)
    report = ensure_report(appointment)
    return redirect(form_url(report, absolute=False))


def site_visit_form(request, token):
    """The debrief form. Public but token-gated, and single-use."""
    report = get_object_or_404(
        SiteVisitReport.objects.select_related('appointment', 'tenant'), token=token)
    appointment = report.appointment

    if not report.is_open:
        # Already handled — by this link or by the in-app button. Say so rather
        # than showing a form whose submit would be refused.
        return render(request, 'bot/pages/site_visit_done.html', {
            'report': report,
            'appointment': appointment,
            'already_done': True,
        })

    if request.method == 'POST':
        outcome = (request.POST.get('outcome') or '').strip()
        valid = dict(SiteVisitReport.OUTCOME_CHOICES)
        if outcome not in valid:
            return _render_form(request, report, error='Please choose how the visit went.')

        if outcome != 'went_ahead':
            _route_other_outcome(report, outcome, user=request.user)
            return render(request, 'bot/pages/site_visit_done.html', {
                'report': report,
                'appointment': appointment,
                'already_done': False,
            })

        email = (request.POST.get('lead_email') or '').strip()
        if not email:
            return _render_form(
                request, report,
                error='We need the customer\'s email address to follow up on the quote.')

        expectation = (request.POST.get('expectation') or '').strip()
        if expectation not in dict(SiteVisitReport.EXPECTATION_CHOICES):
            return _render_form(request, report,
                                error='Please say when the customer expects the job done.')

        expected_date = None
        expected_timeframe = ''
        if expectation == 'specific_date':
            raw = (request.POST.get('expected_date') or '').strip()
            expected_date = _parse_date(raw)
            if not expected_date:
                return _render_form(request, report, error='Please pick a valid date.')
        elif expectation == 'timeframe':
            expected_timeframe = (request.POST.get('expected_timeframe') or '').strip()
            if expected_timeframe not in dict(SiteVisitReport.TIMEFRAME_CHOICES):
                return _render_form(request, report, error='Please pick a timeframe.')

        applied = apply_submission(
            report,
            outcome='went_ahead',
            expectation=expectation,
            expected_date=expected_date,
            expected_timeframe=expected_timeframe,
            job_notes=(request.POST.get('job_notes') or '').strip(),
            email=email,
            user=request.user,
            via='app' if getattr(request.user, 'is_authenticated', False) else 'link',
        )
        if not applied:
            # Lost the race with the other entry point — nothing to do but say so.
            return render(request, 'bot/pages/site_visit_done.html', {
                'report': report, 'appointment': appointment, 'already_done': True,
            })

        # Straight into the quote, pre-filled with the job notes.
        return redirect(reverse('create_quotation', kwargs={'pk': appointment.pk}))

    return _render_form(request, report)


def _render_form(request, report, error=''):
    appointment = report.appointment
    return render(request, 'bot/pages/site_visit_form.html', {
        'report': report,
        'appointment': appointment,
        'outcome_choices': SiteVisitReport.OUTCOME_CHOICES,
        'timeframe_choices': SiteVisitReport.TIMEFRAME_CHOICES,
        'today': timezone.localdate().isoformat(),
        'error': error,
    })


def _parse_date(raw):
    """A YYYY-MM-DD value from the date picker, or None."""
    from datetime import datetime
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _route_other_outcome(report, outcome, user=None):
    """No-show / rescheduled / not proceeding: close the form, no sequence.

    None of these produce a quote or a customer follow-up. What they do change
    is the lead's own state, so the rest of the dashboard sees the same story
    the plumber just told us.
    """
    apply_submission(report, outcome=outcome, user=user,
                     via='app' if getattr(user, 'is_authenticated', False) else 'link')
    apt = report.appointment
    label = dict(SiteVisitReport.OUTCOME_CHOICES).get(outcome, outcome)
    _append_admin_note(apt, f'[SITE VISIT] {label}')

    if outcome == 'no_show':
        apt.status = 'no_show'
        apt.save(update_fields=['status'])
    elif outcome == 'rescheduled':
        # The slot did not happen; the visit needs a new one. The booking is
        # left on the row so the reschedule screen has something to move.
        apt.status = 'pending'
        apt.save(update_fields=['status'])
    elif outcome == 'not_proceeding':
        # is_lead_active=False is the flag every follow-up path already honours,
        # so this one write stops the cron, the ask sequence and the nudges.
        apt.is_lead_active = False
        apt.lead_marked_inactive_at = timezone.now()
        apt.followup_stage = 'completed'
        apt.save(update_fields=['is_lead_active', 'lead_marked_inactive_at',
                                'followup_stage'])


# -- Quote send: email (the WhatsApp half lives in views/quotations.py) -------

@require_POST
@staff_required
def send_quotation_email(request, pk):
    """Email the customer their quote as a PDF.

    An INDEPENDENT button from the WhatsApp send: the plumber may tap one, the
    other, or both, and neither disables the other. Neither has any effect on
    the follow-up channel, which is always email.
    """
    from .quotations import build_quotation_pdf_file
    from ..customer_emails import send_quotation_email_to_customer

    from .quotations import _visible_quotations
    quotation = get_object_or_404(_visible_quotations(request), pk=pk)
    appointment = quotation.appointment

    # The editor sends over fetch and wants JSON back; the quotes list and the
    # view page post a plain form and want a redirect. One handler, two shapes.
    wants_json = 'application/json' in (
        (request.headers.get('Accept') or '') + (request.content_type or '')).lower()

    def _fail(message, status=400):
        if wants_json:
            return JsonResponse({'success': False, 'error': message}, status=status)
        messages.error(request, message)
        return redirect('view_quotation', pk=quotation.pk)

    if not (appointment.customer_email or '').strip():
        return _fail('No email address on file for this customer. Add one on '
                     'the quote and save, then send.')

    pdf_path = None
    try:
        pdf_path = build_quotation_pdf_file(quotation)
        with open(pdf_path, 'rb') as fh:
            pdf_bytes = fh.read()
        sent = send_quotation_email_to_customer(
            quotation, pdf_bytes=pdf_bytes,
            filename=f'Quotation-{quotation.quotation_number}.pdf')
    except Exception as exc:  # noqa: BLE001 — surfaced to the plumber, not swallowed
        logger.exception('send_quotation_email failed — quotation %s', quotation.pk)
        return _fail(f'Could not email the quote: {exc}', status=500)
    finally:
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError:
                pass

    if not sent:
        return _fail('Could not email the quote. Check the email settings.', status=502)

    quotation.sent_via_email = True
    if quotation.status == 'draft':
        quotation.status = 'sent'
    quotation.sent_at = quotation.sent_at or timezone.now()
    quotation.save()

    from ..models import ConversationMessage
    ConversationMessage.objects.create(
        appointment=appointment,
        role='assistant',
        content=f'{quotation.get_display_name()} emailed to {appointment.customer_email}',
        timestamp=timezone.now(),
    )
    if wants_json:
        return JsonResponse({
            'success': True,
            'quotation_id': quotation.pk,
            'status': quotation.status,
            'sent_to': appointment.customer_email,
        })
    messages.success(request, f'Quote emailed to {appointment.customer_email}.')
    return redirect('view_quotation', pk=quotation.pk)
