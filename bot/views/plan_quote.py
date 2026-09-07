"""
bot/views/plan_quote.py
=======================
The plumber's answer on a lead who sent a plan (spec §10.4).

One public, token-gated, single-use form, and deliberately the same shape as
the site-visit debrief it is modelled on: same gate-then-reveal, same
questions, same route into the quote screen. The plumber should not have to
learn two forms.

What differs is the gate. Nobody visited, so there is no no-show and nothing to
reschedule. The question that decides this path is whether the drawing is
enough to price from, and if it is not the lead falls back to the measure path
and the paid visit.

Single use matters here. The same link goes out in four emails, the first
notification and three reminders, so a plumber working a backlog will tap it
twice. The second tap must show what they already said, not offer to overwrite
it or raise a second quote.
"""

import logging

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from ..models import PlanQuoteRequest
from ..plan_quote import apply_plumber_form

logger = logging.getLogger(__name__)


def _parse_date(raw):
    """An ISO date from the form, or None. Never raises on a typo."""
    from datetime import datetime
    try:
        return datetime.strptime((raw or '').strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _render_form(request, row, error=''):
    return render(request, 'bot/pages/plan_quote_form.html', {
        'row': row,
        'appointment': row.appointment,
        'outcome_choices': PlanQuoteRequest.OUTCOME_CHOICES,
        'timeframe_choices': PlanQuoteRequest.TIMEFRAME_CHOICES,
        'today': timezone.localdate().isoformat(),
        'error': error,
    })


def plan_quote_form(request, token):
    """Can you quote from the plan? Public, token-gated, single-use."""
    row = get_object_or_404(
        PlanQuoteRequest.objects.select_related('appointment', 'tenant'),
        token=token)
    appointment = row.appointment

    if not row.is_open:
        # Already answered, by this link or one of the reminders.
        return render(request, 'bot/pages/plan_quote_done.html', {
            'row': row, 'appointment': appointment, 'already_done': True,
        })

    if request.method != 'POST':
        return _render_form(request, row)

    outcome = (request.POST.get('outcome') or '').strip()
    if outcome not in dict(PlanQuoteRequest.OUTCOME_CHOICES):
        return _render_form(request, row, error='Please say whether you can quote from the plan.')

    # Anything but "quoting" submits straight through: no quote, no customer
    # follow-up sequence, and the lead state the rest of the dashboard reads.
    if outcome != 'quoting':
        _route_other_outcome(row, outcome)
        return render(request, 'bot/pages/plan_quote_done.html', {
            'row': row, 'appointment': appointment, 'already_done': False,
        })

    # The email is the only channel that survives past the WhatsApp window, and
    # this form is the one place a missing one can be added. Without it the
    # quote has nowhere to go.
    email = (request.POST.get('lead_email') or '').strip()
    if not email:
        return _render_form(
            request, row,
            error="We need the customer's email address to send the quote.")

    expectation = (request.POST.get('expectation') or '').strip()
    if expectation not in dict(PlanQuoteRequest.EXPECTATION_CHOICES):
        return _render_form(request, row,
                            error='Please say when the customer expects the job done.')

    expected_date = None
    expected_timeframe = ''
    if expectation == 'specific_date':
        expected_date = _parse_date(request.POST.get('expected_date'))
        if not expected_date:
            return _render_form(request, row, error='Please pick the date they gave.')
    elif expectation == 'timeframe':
        expected_timeframe = (request.POST.get('expected_timeframe') or '').strip()
        if expected_timeframe not in dict(PlanQuoteRequest.TIMEFRAME_CHOICES):
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
    )
    if not applied:
        # Lost the race with another reminder's link.
        return render(request, 'bot/pages/plan_quote_done.html', {
            'row': row, 'appointment': appointment, 'already_done': True,
        })

    # Straight into the quote, pre-filled with the job notes — the same landing
    # the site-visit form uses, so both paths end in one quote screen.
    return redirect(reverse('create_quotation', kwargs={'pk': appointment.pk}))


def _route_other_outcome(row, outcome):
    """Not quoting. Write the lead state the rest of the dashboard reads.

    `needs_visit` is the fallback the spec asks for: a plan too thin to price
    puts the lead back on the measure path, where the paid visit is the way to
    get the measurements. `not_proceeding` sets the flag every follow-up path
    already honours, so nothing chases them again.
    """
    from bot.utils import _append_admin_note

    apt = row.appointment
    apply_plumber_form(row, outcome=outcome, quote_sent=False)

    if outcome == 'needs_visit':
        # Back to the measure path. has_plan goes false so the visit close is
        # available again, and plan_status is cleared so on_plan_path stops
        # suppressing it.
        apt.has_plan = False
        apt.plan_status = None
        apt.save(update_fields=['has_plan', 'plan_status'])
        _append_admin_note(apt, 'Plan not enough to quote from — back to the site visit.')
    elif outcome == 'not_proceeding':
        apt.is_lead_active = False
        apt.save(update_fields=['is_lead_active'])
        _append_admin_note(apt, 'Lead not proceeding (plan path).')
