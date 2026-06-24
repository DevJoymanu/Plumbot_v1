from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods, require_GET
from django.utils.decorators import method_decorator
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.generic import ListView, DetailView, TemplateView, CreateView, UpdateView, DeleteView
from django.db.models import Count, Q
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.forms import modelformset_factory
from django.templatetags.static import static
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import requests
import pytz
import os
import json
import re
import tempfile
import base64
import logging

from ..models import (
    Appointment, Quotation, QuotationItem,
    QuotationTemplate, QuotationTemplateItem, ConversationMessage,
)
from ..forms import (
    AppointmentForm, SettingsForm, CalendarSettingsForm, AISettingsForm,
    QuotationForm, QuotationItemFormSet,
    QuotationTemplateForm, QuotationTemplateItemFormSet,
)
from ..decorators import staff_required, anonymous_required, StaffRequiredMixin
from ..whatsapp_cloud_api import whatsapp_api
from ..services.clients import (
    twilio_client, deepseek_client,
    TWILIO_WHATSAPP_NUMBER, GOOGLE_CALENDAR_CREDENTIALS,
    DEEPSEEK_API_KEY,
)
from ..utils import (
    _to_decimal, _to_float, _safe_logo_url, _safe_logo_data_uri,
    _reset_pk_sequence, _append_admin_note,
    clean_phone_number, format_phone_number_for_storage,
)

logger = logging.getLogger(__name__)


def _dashboard_workspace_data(response_age='1w_minus'):
    from bot.models import Job

    # Use the date in the configured TIME_ZONE (Africa/Johannesburg), not the UTC
    # date — timezone.now().date() rolls over 2h early and shows "yesterday" at night.
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    day_after_tomorrow = today + timedelta(days=2)
    week_end = today + timedelta(days=(6 - today.weekday()))
    now = timezone.now()

    age_map_minus = {
        '1w_minus': timedelta(weeks=1),
        '4w_minus': timedelta(weeks=4),
    }

    appointments = Appointment.objects.all()
    if response_age != 'all' and response_age in age_map_minus:
        cutoff = now - age_map_minus[response_age]
        appointments = appointments.filter(last_customer_response__gte=cutoff)

    # Follow-ups: only leads that are actually DUE to be contacted now (not merely
    # follow_up_status='pending'). should_send_followup_now() already excludes
    # booked/inactive leads and enforces the timing, so it's the "due" definition.
    _followup_candidates = (
        Appointment.objects
        .filter(is_lead_active=True, status='pending')
        .exclude(followup_stage__in=['completed', 'responded'])
        .order_by('-updated_at')
    )
    due_followups = [a for a in _followup_candidates if a.should_send_followup_now()]
    followups = due_followups[:3]
    followups_due_count = len(due_followups)

    this_week_appointments = appointments.filter(
        status__in=['confirmed', 'pending'],
        scheduled_datetime__date__range=(day_after_tomorrow, week_end),
    ).order_by('scheduled_datetime')
    week_jobs = Job.objects.filter(
        scheduled_datetime__date__range=(today, week_end),
    ).select_related('site_visit').order_by('scheduled_datetime')
    # Hot leads: priority (very-hot + hot) leads from the last week that haven't
    # booked yet. Shared with the nav badge + context processor via this helper,
    # so the dashboard, its sidebar, and the global badge always agree.
    hot_lead_count = priority_lead_count()

    return {
        'selected_response_age': response_age,
        'today': today,
        'hot_lead_count': hot_lead_count,
        'todays_confirmed_appointments': appointments.filter(
            status='confirmed',
            scheduled_datetime__date=today,
        ).order_by('scheduled_datetime'),
        'tomorrows_confirmed_appointments': appointments.filter(
            status='confirmed',
            scheduled_datetime__date=tomorrow,
        ).order_by('scheduled_datetime'),
        'this_week_appointments': this_week_appointments,
        'week_jobs': week_jobs,
        'followups': followups,
        'followups_due_count': followups_due_count,
    }


_PRIORITY_AGE_MAP = {
    '1w_minus': timedelta(weeks=1),
    '2w_minus': timedelta(weeks=2),
    '3w_minus': timedelta(weeks=3),
    '4w_minus': timedelta(weeks=4),
}


def priority_leads_qs():
    """Canonical priority-leads queryset — the single source of truth for every
    'priority / hot lead' stat (nav badge, dashboard, priority-leads page + sidebar).

    Active leads (excluding completed/cancelled), annotated with a computed score
    and status derived from how complete their qualification is. Previously the nav
    badge and dashboard counted the stored ``lead_status`` field while the priority
    page computed status on the fly — so the three numbers never agreed. Everything
    now derives from this one queryset.
    """
    from django.db.models import Case, F, IntegerField, Q, Value, When
    from django.db.models.functions import Coalesce

    has_project_type = Case(
        When(Q(project_type__isnull=False) & ~Q(project_type=''), then=Value(1)),
        default=Value(0), output_field=IntegerField(),
    )
    has_property_type = Case(
        When(Q(property_type__isnull=False) & ~Q(property_type=''), then=Value(1)),
        default=Value(0), output_field=IntegerField(),
    )
    has_area = Case(
        When(Q(customer_area__isnull=False) & ~Q(customer_area=''), then=Value(1)),
        default=Value(0), output_field=IntegerField(),
    )
    has_timeline = Case(
        When(Q(timeline__isnull=False) & ~Q(timeline=''), then=Value(1)),
        default=Value(0), output_field=IntegerField(),
    )
    has_site_visit = Case(
        When(scheduled_datetime__isnull=False, then=Value(1)),
        default=Value(0), output_field=IntegerField(),
    )

    return (
        Appointment.objects.annotate(
            completed_fields=has_project_type + has_property_type + has_area + has_timeline + has_site_visit,
            computed_score=Case(
                When(scheduled_datetime__isnull=False, then=Value(100)),
                default=(has_project_type + has_property_type + has_area + has_timeline + has_site_visit) * Value(20),
                output_field=IntegerField(),
            ),
        ).annotate(
            computed_status=Case(
                When(scheduled_datetime__isnull=False, then=Value('very_hot')),
                When(completed_fields__lte=1, then=Value('cold')),
                When(completed_fields__lte=3, then=Value('warm')),
                When(completed_fields=4, then=Value('hot')),
                default=Value('very_hot'),
            ),
            recent_activity=Coalesce('last_inbound_at', 'updated_at'),
            last_response_at=Coalesce('last_customer_response', 'created_at'),
        )
        .filter(is_lead_active=True)
        .exclude(status__in=['completed', 'cancelled'])
        .order_by(F('computed_score').desc(), F('recent_activity').desc(nulls_last=True))
    )


def priority_lead_count():
    """Count of actionable hot leads: very-hot + hot, from the last week, that
    haven't booked yet. Single source of truth for the nav badge, the global
    context processor, and the dashboard 'Hot Leads' figure, so every surface
    shows the same number."""
    week_ago = timezone.now() - timedelta(weeks=1)
    return (
        priority_leads_qs()
        .filter(computed_status__in=['very_hot', 'hot'])
        .filter(last_response_at__gte=week_ago)
        .exclude(status='confirmed')
        .count()
    )


def _priority_leads_workspace_data(response_age='all'):
    from django.db.models import F  # noqa: F401 — kept for parity with callers

    age_map_minus = _PRIORITY_AGE_MAP

    leads = priority_leads_qs()
    if response_age != 'all' and response_age in age_map_minus:
        cutoff = timezone.now() - age_map_minus[response_age]
        leads = leads.filter(last_response_at__gte=cutoff)

    very_hot = list(leads.filter(computed_status='very_hot'))
    hot = list(leads.filter(computed_status='hot'))
    warm = list(leads.filter(computed_status='warm'))
    luke = list(leads.filter(computed_status='cold', computed_score=20))
    cold = list(leads.filter(computed_status='cold', computed_score=0))

    return {
        'selected_response_age': response_age,
        'total_leads': leads.count(),
        'very_hot_leads': very_hot,
        'hot_leads': hot,
        'warm_leads': warm,
        'luke_warm_leads': luke,
        'cold_leads': cold,
    }


def _followups_workspace_data(response_age='1w_minus'):
    now = timezone.now()
    age_map_minus = {
        '1w_minus': timedelta(weeks=1),
        '4w_minus': timedelta(weeks=4),
    }
    cutoff = None
    if response_age != 'all' and response_age in age_map_minus:
        cutoff = now - age_map_minus[response_age]

    base_active = Appointment.objects.filter(
        is_lead_active=True,
        status='pending'
    )
    if cutoff:
        base_active = base_active.filter(last_customer_response__gte=cutoff)

    stage_counts = {}
    for stage_code, stage_name in Appointment._meta.get_field('followup_stage').choices:
        stage_qs = Appointment.objects.filter(
            is_lead_active=True,
            followup_stage=stage_code
        )
        if cutoff:
            stage_qs = stage_qs.filter(last_customer_response__gte=cutoff)
        count = stage_qs.count()
        if count > 0:
            stage_counts[stage_name] = count

    leads_needing_followup = Appointment.objects.filter(
        is_lead_active=True,
        status='pending'
    ).exclude(
        followup_stage='completed'
    ).exclude(
        followup_stage='responded'
    )
    if cutoff:
        leads_needing_followup = leads_needing_followup.filter(last_customer_response__gte=cutoff)

    ready_for_followup = [lead for lead in leads_needing_followup if lead.should_send_followup_now()]
    recent_responses = Appointment.objects.filter(
        last_customer_response__isnull=False,
        is_lead_active=True
    )
    if cutoff:
        recent_responses = recent_responses.filter(last_customer_response__gte=cutoff)
    recent_responses = recent_responses.order_by('-last_customer_response')[:10]

    recent_inactive = Appointment.objects.filter(
        is_lead_active=False,
        lead_marked_inactive_at__gte=now - timedelta(days=30)
    ).order_by('-lead_marked_inactive_at')[:10]

    # Annotate each lead with its next automatic follow-up (attempt, due time,
    # and whether it's on the 72h CTWA ad cadence) so the dashboard can show it.
    # Uses the cron's own timing core, so the displayed time matches what sends.
    from bot.management.commands.send_followups import Command as _FollowupCmd
    _fu_cmd = _FollowupCmd()
    ready_for_followup = list(ready_for_followup)
    recent_responses = list(recent_responses)
    recent_inactive = list(recent_inactive)
    for _lead in (*ready_for_followup, *recent_responses, *recent_inactive):
        try:
            _lead.fu_info = _fu_cmd.next_followup_due_at(_lead)
        except Exception:
            _lead.fu_info = None

    # ── Per-channel follow-up views (WhatsApp / Emails tabs) ──
    from ..models import ScheduledFollowup
    scheduled_whatsapp = list(
        ScheduledFollowup.objects
        .filter(channel='whatsapp', status__in=['pending', 'failed'])
        .select_related('appointment').order_by('scheduled_for')[:50]
    )

    # Upcoming emails = staff-queued rows PLUS the delay/reminder emails the
    # automation will send (delayed leads with an email captured, etc.). The
    # latter aren't ScheduledFollowup rows — they're computed per lead — so
    # surface them here too, otherwise delayed leads due for an email never show.
    upcoming_email = []
    for sf in (
        ScheduledFollowup.objects
        .filter(channel='email', status__in=['pending', 'failed'])
        .select_related('appointment').order_by('scheduled_for')[:50]
    ):
        upcoming_email.append({
            'lead': sf.appointment,
            'label': sf.subject or sf.message or 'Email follow-up',
            'scheduled_for': sf.scheduled_for,
            'status': sf.status,
            'source': 'scheduled',
        })

    email_leads = (
        Appointment.objects
        .exclude(customer_email__isnull=True).exclude(customer_email='')
        .filter(Q(delay_followup_due_at__isnull=False) | Q(scheduled_datetime__isnull=False))
        .order_by('delay_followup_due_at', 'scheduled_datetime')[:300]
    )
    for apt in email_leads:
        for it in apt.get_upcoming_emails()['items']:
            if it['status'] in ('pending', 'overdue'):
                upcoming_email.append({
                    'lead': apt,
                    'label': it['label'],
                    'scheduled_for': it['scheduled_for'],
                    'status': it['status'],
                    'source': it.get('source', 'delay'),
                })
    upcoming_email.sort(key=lambda x: x['scheduled_for'])
    upcoming_email = upcoming_email[:60]

    # Recently-sent follow-ups, flattened from each lead's history. Bounded to
    # the most recently-updated leads so the page stays fast; each event carries
    # its lead so the template can link straight to the conversation.
    _epoch = datetime(1970, 1, 1, tzinfo=pytz.utc)
    sent_whatsapp, sent_email = [], []
    for apt in Appointment.objects.order_by('-updated_at')[:200]:
        for ev in apt.get_followup_log():
            ts = ev.get('timestamp')
            if cutoff and ts and ts < cutoff:
                continue
            (sent_whatsapp if ev['channel'] == 'whatsapp' else sent_email).append(
                {'lead': apt, 'ev': ev}
            )
    sent_whatsapp.sort(key=lambda x: x['ev']['timestamp'] or _epoch, reverse=True)
    sent_email.sort(key=lambda x: x['ev']['timestamp'] or _epoch, reverse=True)
    sent_whatsapp = sent_whatsapp[:40]
    sent_email = sent_email[:40]

    # Split queued items into Overdue vs Upcoming. Overdue is limited to the
    # last 7 days — older missed items are dropped rather than piling up forever.
    overdue_since = now - timedelta(days=7)

    def _is_overdue(when):
        return bool(when) and overdue_since <= when < now

    overdue_whatsapp = [sf for sf in scheduled_whatsapp if _is_overdue(sf.scheduled_for)]
    upcoming_whatsapp = [sf for sf in scheduled_whatsapp if sf.scheduled_for and sf.scheduled_for >= now]
    overdue_email = [e for e in upcoming_email if _is_overdue(e['scheduled_for'])]
    upcoming_email_list = [e for e in upcoming_email if e['scheduled_for'] and e['scheduled_for'] >= now]

    return {
        'selected_response_age': response_age,
        'total_active_leads': base_active.count(),
        'stage_counts': stage_counts,
        'ready_count': len(ready_for_followup),
        'ready_leads': ready_for_followup[:20],
        'recent_responses': recent_responses,
        'recent_inactive': recent_inactive,
        'overdue_whatsapp': overdue_whatsapp,
        'upcoming_whatsapp': upcoming_whatsapp,
        'overdue_email': overdue_email,
        'upcoming_email': upcoming_email_list,
        'sent_whatsapp': sent_whatsapp,
        'sent_email': sent_email,
        'response_age_label': 'All-time' if response_age == 'all' else (
            'Last 30 Days' if response_age == '4w_minus' else 'Last 7 Days'
        ),
    }


# Date-window options for the appointments sidebar (matches the main list page).
_SIDEBAR_AGE_MAP = {
    '1w_minus': timedelta(weeks=1),
    '3w_minus': timedelta(weeks=3),
    '4w_minus': timedelta(weeks=4),
}


def _appointments_sidebar_context(sidebar_filter='all', response_age='all'):
    # Apply the same last-response date window the main list uses, so the sidebar
    # can be filtered by 7 days / 21 days / 30 days / All time.
    base = Appointment.objects.all()
    if response_age in _SIDEBAR_AGE_MAP:
        cutoff = timezone.now() - _SIDEBAR_AGE_MAP[response_age]
        base = base.filter(last_customer_response__gte=cutoff)

    return {
        'sidebar_filter': sidebar_filter,
        'selected_response_age': response_age,
        'sidebar_appointments': base.order_by('-updated_at')[:30],
        'appointment_status_counts': {
            'total': base.count(),
            'booked': base.filter(status='confirmed').count(),
            'pending': base.filter(status='pending').exclude(
                internal_notes__contains='[DELAY_SIGNAL]'
            ).count(),
            'cancelled': base.filter(status='cancelled').count(),
            'delayed': base.filter(
                status='pending',
                internal_notes__contains='[DELAY_SIGNAL]',
            ).count(),
            # Click-to-WhatsApp ad leads still inside their 72h free-form window.
            'ad': base.filter(
                ctwa_entry_at__gt=timezone.now() - timedelta(hours=Appointment.CTWA_WINDOW_HOURS),
            ).count(),
        },
    }


@method_decorator(staff_required, name='dispatch')
class DashboardView(TemplateView):
    template_name = 'bot/pages/dashboard.html'


    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        response_age = self.request.GET.get('response_age', '').strip()
        if not response_age:
            response_age = '1w_minus'
        workspace = _dashboard_workspace_data(response_age)

        context.update({
            'active_nav': 'dashboard',
            **workspace,
            'calendar_status': 'Connected' if hasattr(settings, 'GOOGLE_CALENDAR_CREDENTIALS') else 'Not configured',
        })

        return context
