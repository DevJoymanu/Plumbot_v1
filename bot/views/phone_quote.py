"""
bot/views/phone_quote.py
========================
The plumber quoting over the phone: the in-app entry and the form.

Deliberately the same shape as ``plan_quote`` and the site-visit debrief it is
modelled on — same gate-then-reveal, same questions, same route into the quote
screen. The plumber should not have to learn a third form.

What differs is the gate. Nobody visited and no drawing arrived, so the
question that decides this path is whether the job can be priced from the call
at all. If it cannot, the lead goes back to the measure path and the visit.

Single use matters here for the same reason it does on the other two: the row
is reachable from the appointment screen as well as its own link, so a plumber
who opens it twice must see what they already said rather than be offered the
chance to overwrite it or raise a second quote.
"""

import logging

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from ..decorators import staff_required
from ..models import Appointment, PhoneQuoteRequest
from ..phone_quote import apply_plumber_form, ensure_request, form_url

logger = logging.getLogger(__name__)


def _parse_date(raw):
    """An ISO date from the form, or None. Never raises on a typo."""
    from datetime import datetime
    try:
        return datetime.strptime((raw or '').strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _render_form(request, row, error=''):
    return render(request, 'bot/pages/phone_quote_form.html', {
        'row': row,
        'appointment': row.appointment,
        'outcome_choices': PhoneQuoteRequest.OUTCOME_CHOICES,
        'timeframe_choices': PhoneQuoteRequest.TIMEFRAME_CHOICES,
        'today': timezone.localdate().isoformat(),
        'error': error,
    })


@staff_required
def phone_quote_start(request, pk):
    """In-app entry: make sure the row exists, then open the shared form.

    Mirrors ``site_visit_start``. The button lives on the appointment screen,
    so the row is created on the way through rather than by a cron.
    """
    appointment = get_object_or_404(
        Appointment.objects.for_tenant_or_seed(getattr(request, 'tenant', None)),
        pk=pk)
    row = ensure_request(appointment)
    return redirect(form_url(row, absolute=False))


def phone_quote_form(request, token):
    """Quoting on the phone? Token-gated and single-use.

    Public like the other two forms: the plumber may be on their phone with no
    session, and the token is the credential.
    """
    row = get_object_or_404(
        PhoneQuoteRequest.objects.select_related('appointment', 'tenant'),
        token=token)
    appointment = row.appointment

    if not row.is_open:
        return render(request, 'bot/pages/phone_quote_done.html', {
            'row': row, 'appointment': appointment, 'already_done': True,
        })

    if request.method != 'POST':
        return _render_form(request, row)

    outcome = (request.POST.get('outcome') or '').strip()
    if outcome not in dict(PhoneQuoteRequest.OUTCOME_CHOICES):
        return _render_form(
            request, row,
            error='Please say whether you can quote from the call.')

    # Anything but "quoting" submits straight through: no quote, no customer
    # follow-up, and the lead state the rest of the dashboard reads.
    if outcome != 'quoting':
        _route_other_outcome(row, outcome, user=request.user)
        return render(request, 'bot/pages/phone_quote_done.html', {
            'row': row, 'appointment': appointment, 'already_done': False,
        })

    # Email is the only channel that survives past the WhatsApp window, and
    # this form is the one place a missing one can be added. Without it the
    # quote has nowhere to go.
    email = ((request.POST.get('lead_email') or '').strip()
             or (appointment.customer_email or '').strip())
    if not email:
        return _render_form(
            request, row,
            error="We need the customer's email address to send the quote.")

    expectation = (request.POST.get('expectation') or '').strip()
    if expectation not in dict(PhoneQuoteRequest.EXPECTATION_CHOICES):
        return _render_form(
            request, row,
            error='Please say when the customer expects the job done.')

    expected_date = None
    expected_timeframe = ''
    if expectation == 'specific_date':
        expected_date = _parse_date(request.POST.get('expected_date'))
        if not expected_date:
            return _render_form(request, row,
                                error='Please pick the date they gave.')
    elif expectation == 'timeframe':
        expected_timeframe = (request.POST.get('expected_timeframe') or '').strip()
        if expected_timeframe not in dict(PhoneQuoteRequest.TIMEFRAME_CHOICES):
            return _render_form(request, row, error='Please pick a timeframe.')

    applied = apply_plumber_form(
        row,
        outcome='quoting',
        quote_sent=False,
        lead_email=email,
        expectation=expectation,
        expected_date=expected_date,
        expected_timeframe=expected_timeframe,
        job_notes=(request.POST.get('job_notes') or '').strip(),
        customer_area=(request.POST.get('customer_area') or '').strip(),
        user=getattr(request, 'user', None),
    )
    if not applied:
        # Lost the race with a second tab.
        return render(request, 'bot/pages/phone_quote_done.html', {
            'row': row, 'appointment': appointment, 'already_done': True,
        })

    # Straight into the quote, pre-filled with the job notes — the same landing
    # both other forms use, so all three paths end in one quote screen.
    return redirect(reverse('create_quotation', kwargs={'pk': appointment.pk}))


def _route_other_outcome(row, outcome, user=None):
    """Not quoting. Write the lead state the rest of the dashboard reads.

    `needs_visit` is the honest fallback: a job that cannot be priced down the
    phone goes back to the measure path, where the visit is how the
    measurements get taken. `not_proceeding` sets the flag every follow-up path
    already honours, so nothing chases them again.
    """
    from bot.utils import _append_admin_note

    apt = row.appointment
    apply_plumber_form(row, outcome=outcome, quote_sent=False, user=user)

    if outcome == 'needs_visit':
        _append_admin_note(
            apt, 'Could not quote on the phone — back to the site visit.')
    elif outcome == 'not_proceeding':
        apt.is_lead_active = False
        apt.save(update_fields=['is_lead_active'])
        _append_admin_note(apt, 'Lead not proceeding (said so on the phone).')
