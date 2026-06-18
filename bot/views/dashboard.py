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

    today = timezone.now().date()
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

    followups = list(Appointment.objects.filter(follow_up_status='pending').order_by('-updated_at')[:3])
    this_week_appointments = appointments.filter(
        status__in=['confirmed', 'pending'],
        scheduled_datetime__date__range=(day_after_tomorrow, week_end),
    ).order_by('scheduled_datetime')
    week_jobs = Job.objects.filter(
        scheduled_datetime__date__range=(today, week_end),
    ).select_related('site_visit').order_by('scheduled_datetime')
    hot_lead_count = Appointment.objects.filter(
        is_lead_active=True,
        lead_status__in=['very_hot', 'hot'],
    ).exclude(status__in=['completed', 'cancelled']).count()

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
    }


def _priority_leads_workspace_data(response_age='1w_minus'):
    from django.db.models import Case, F, IntegerField, Q, Value, When
    from django.db.models.functions import Coalesce

    has_project_type = Case(
        When(Q(project_type__isnull=False) & ~Q(project_type=''), then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    has_property_type = Case(
        When(Q(property_type__isnull=False) & ~Q(property_type=''), then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    has_area = Case(
        When(Q(customer_area__isnull=False) & ~Q(customer_area=''), then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    has_timeline = Case(
        When(Q(timeline__isnull=False) & ~Q(timeline=''), then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    has_site_visit = Case(
        When(scheduled_datetime__isnull=False, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    age_map_minus = {
        '1w_minus': timedelta(weeks=1),
        '2w_minus': timedelta(weeks=2),
        '3w_minus': timedelta(weeks=3),
        '4w_minus': timedelta(weeks=4),
    }

    leads = (
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
    # One window applied to every section: backward (cutoff) for past/recent
    # items, forward (horizon) for upcoming/scheduled items. 'all' = unbounded.
    window = age_map_minus.get(response_age) if response_age != 'all' else None
    cutoff = now - window if window else None
    horizon = now + window if window else None

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

    # Inactive within the selected window (default to 30 days for 'all').
    recent_inactive = Appointment.objects.filter(
        is_lead_active=False,
        lead_marked_inactive_at__gte=(cutoff or now - timedelta(days=30)),
    ).order_by('-lead_marked_inactive_at')[:10]

    # ── Per-channel follow-up views (WhatsApp / Emails tabs) ──
    from ..models import ScheduledFollowup
    _sched_wa_qs = ScheduledFollowup.objects.filter(
        channel='whatsapp', status__in=['pending', 'failed']
    )
    if horizon:
        _sched_wa_qs = _sched_wa_qs.filter(scheduled_for__lte=horizon)
    scheduled_whatsapp = list(
        _sched_wa_qs.select_related('appointment').order_by('scheduled_for')[:50]
    )

    # Upcoming emails = staff-queued rows PLUS the delay/reminder emails the
    # automation will send (delayed leads with an email captured, etc.). The
    # latter aren't ScheduledFollowup rows — they're computed per lead — so
    # surface them here too, otherwise delayed leads due for an email never show.
    _sched_em_qs = ScheduledFollowup.objects.filter(
        channel='email', status__in=['pending', 'failed']
    )
    if horizon:
        _sched_em_qs = _sched_em_qs.filter(scheduled_for__lte=horizon)
    upcoming_email = []
    for sf in _sched_em_qs.select_related('appointment').order_by('scheduled_for')[:50]:
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
            if it['status'] not in ('pending', 'overdue'):
                continue
            # Overdue items always show (still due); future items only within
            # the selected forward horizon.
            if horizon and it['status'] == 'pending' and it['scheduled_for'] > horizon:
                continue
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

    return {
        'selected_response_age': response_age,
        'total_active_leads': base_active.count(),
        'stage_counts': stage_counts,
        'ready_count': len(ready_for_followup),
        'ready_leads': ready_for_followup[:20],
        'recent_responses': recent_responses,
        'recent_inactive': recent_inactive,
        'scheduled_whatsapp': scheduled_whatsapp,
        'upcoming_email': upcoming_email,
        'sent_whatsapp': sent_whatsapp,
        'sent_email': sent_email,
        'response_age_label': 'All-time' if response_age == 'all' else (
            'Last 30 Days' if response_age == '4w_minus' else 'Last 7 Days'
        ),
    }


def _appointments_sidebar_context(sidebar_filter='all'):
    return {
        'sidebar_filter': sidebar_filter,
        'sidebar_appointments': Appointment.objects.order_by('-updated_at')[:20],
        'appointment_status_counts': {
            'total': Appointment.objects.count(),
            'booked': Appointment.objects.filter(status='confirmed').count(),
            'pending': Appointment.objects.filter(status='pending').exclude(
                internal_notes__contains='[DELAY_SIGNAL]'
            ).count(),
            'cancelled': Appointment.objects.filter(status='cancelled').count(),
            'delayed': Appointment.objects.filter(
                status='pending',
                internal_notes__contains='[DELAY_SIGNAL]',
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
