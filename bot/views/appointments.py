from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods, require_GET
from django.utils.decorators import method_decorator
from .dashboard import (
    _dashboard_workspace_data,
    _priority_leads_workspace_data,
    _followups_workspace_data,
    _appointments_sidebar_context,
)
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect, FileResponse, Http404
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
import mimetypes
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
from ..email_catalog import catalog_for_template as _email_catalog_for_template
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
from ..services.lead_scoring import refresh_lead_score, calculate_lead_score

@method_decorator(staff_required, name='dispatch')
class ConversationsView(ListView):
    """Lead inbox — formerly the Appointments list, now the Conversations page.

    Same queryset/filters as before (no DB change); only the template file and
    the nav label moved. The calendar-based Appointments page is a separate view.
    """
    template_name = 'bot/pages/conversations.html'
    model = Appointment
    context_object_name = 'appointments'
    paginate_by = 20
    ordering = ['-updated_at']

    # Every tab defaults to the last 7 days; the date filter lets staff widen it.
    TAB_AGE_DEFAULTS = {
        'all':       '1w_minus',
        'booked':    '1w_minus',
        'pending':   '1w_minus',
        'cancelled': '1w_minus',
        'delayed':   '1w_minus',
        'ad':        '1w_minus',
    }
    TAB_AGE_MAP = {
        '1w_minus': timedelta(weeks=1),
        '3w_minus': timedelta(weeks=3),
        '4w_minus': timedelta(weeks=4),
    }
    # (value, label) options for the date-filter dropdown, in display order.
    AGE_FILTER_OPTIONS = [
        ('1w_minus', 'Last 7 days'),
        ('3w_minus', 'Last 21 days'),
        ('4w_minus', 'Last 30 days'),
        ('all',      'All time'),
    ]

    def _resolve_age(self):
        """Return (status_filter, response_age) honouring per-tab defaults."""
        status_filter = self.request.GET.get('status_filter', 'all')
        if 'response_age' in self.request.GET:
            age = self.request.GET['response_age'].strip()
            if age not in self.TAB_AGE_MAP and age != 'all':
                age = self.TAB_AGE_DEFAULTS.get(status_filter, '1w_minus')
        else:
            age = self.TAB_AGE_DEFAULTS.get(status_filter, '1w_minus')
        return status_filter, age

    def get_queryset(self):
        from django.db.models import Case, IntegerField, Q, Value, When

        status_filter, response_age = self._resolve_age()
        # Cache for get_context_data
        self._status_filter = status_filter
        self._response_age  = response_age

        age_map_minus = self.TAB_AGE_MAP

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

        completed_fields = has_project_type + has_property_type + has_area + has_timeline + has_site_visit
        # Test lines (999-prefixed console/scenario leads) never appear on the
        # client-facing appointments page — they live on the staff-only
        # /test-leads/ page instead.
        queryset = (
            Appointment.objects.real().annotate(
                computed_score=Case(
                    When(scheduled_datetime__isnull=False, then=Value(100)),
                    default=completed_fields * Value(20),
                    output_field=IntegerField(),
                ),
            ).annotate(
                computed_status=Case(
                    When(scheduled_datetime__isnull=False, then=Value('very_hot')),
                    When(computed_score__lte=20, then=Value('cold')),
                    When(computed_score__lte=60, then=Value('warm')),
                    When(computed_score=80, then=Value('hot')),
                    default=Value('very_hot'),
                ),
                computed_status_label=Case(
                    When(scheduled_datetime__isnull=False, then=Value('Very Hot')),
                    When(computed_score__lte=20, then=Value('Cold')),
                    When(computed_score__lte=60, then=Value('Warm')),
                    When(computed_score=80, then=Value('Hot')),
                    default=Value('Very Hot'),
                ),
            ).order_by('-updated_at')
        )

        cutoff = None
        if response_age != 'all' and response_age in age_map_minus:
            cutoff = timezone.now() - age_map_minus[response_age]

        # Booked = conversions, so its date window measures the booking date
        # (booked_at); every other tab measures the last customer response.
        date_field = 'booked_at' if status_filter == 'booked' else 'last_customer_response'
        if cutoff:
            queryset = queryset.filter(**{f'{date_field}__gte': cutoff})

        if status_filter == 'booked':
            queryset = queryset.filter(status='confirmed')
        elif status_filter == 'pending':
            queryset = queryset.filter(status='pending').exclude(
                internal_notes__contains='[DELAY_SIGNAL]'
            )
        elif status_filter == 'cancelled':
            queryset = queryset.filter(status='cancelled')
        elif status_filter == 'delayed':
            queryset = queryset.filter(
                status='pending',
                internal_notes__contains='[DELAY_SIGNAL]'
            )
        elif status_filter == 'ad':
            # CTWA ad leads still inside their 72h free-form window.
            queryset = queryset.filter(
                ctwa_entry_at__gt=timezone.now() - timedelta(hours=Appointment.CTWA_WINDOW_HOURS)
            )

        return queryset
        
    #
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Reuse values computed in get_queryset (called first by ListView)
        status_filter = getattr(self, '_status_filter', 'all')
        response_age  = getattr(self, '_response_age',  '1w_minus')
        age_map_minus = self.TAB_AGE_MAP

        base_qs = Appointment.objects.real()
        if response_age != 'all' and response_age in age_map_minus:
            cutoff = timezone.now() - age_map_minus[response_age]
            base_qs = base_qs.filter(last_customer_response__gte=cutoff)

        # Delayed = leads with a [DELAY_SIGNAL] in internal_notes that are still active
        delayed_qs = base_qs.filter(
            status='pending',
            internal_notes__contains='[DELAY_SIGNAL]',
        ).order_by('updated_at')

        from django.utils import timezone as _tz
        _now = _tz.now()
        DELAY_WINDOW = 14  # ← CHANGE THIS FROM 14 TO 21 DAYS

        delayed_leads_with_countdown = []
        for lead in delayed_qs:
            # Use updated_at as the signal start time (when the delay was set)
            signal_start = lead.updated_at
            follow_up_due = signal_start + timedelta(days=DELAY_WINDOW)
            
            # Calculate days remaining (can be negative for overdue)
            days_remaining = (follow_up_due.date() - _now.date()).days
            
            # Calculate days elapsed
            days_elapsed = DELAY_WINDOW - max(0, days_remaining)
            pct_elapsed = min(100, int((days_elapsed / DELAY_WINDOW) * 100)) if days_remaining > 0 else 100
            
            # Check if overdue
            overdue = _now > follow_up_due

            delayed_leads_with_countdown.append({
                'lead': lead,
                'days_remaining': abs(days_remaining) if overdue else max(0, days_remaining),
                'days_elapsed': days_elapsed,
                'pct_elapsed': pct_elapsed,
                'follow_up_due_at': follow_up_due,
                'follow_up_window_days': DELAY_WINDOW,
                'overdue': overdue,
            })

        today = timezone.localdate()
        todays_confirmed_appointments = base_qs.filter(
            status='confirmed',
            scheduled_datetime__date=today
        ).order_by('scheduled_datetime')

        context['active_nav'] = 'conversations'
        context['status_counts'] = {
            'total': base_qs.count(),
            'pending': base_qs.filter(status='pending').exclude(
                internal_notes__contains='[DELAY_SIGNAL]'
            ).count(),
            'confirmed': base_qs.filter(status='confirmed').count(),
            'cancelled': base_qs.filter(status='cancelled').count(),
            'delayed': delayed_qs.count(),
            'ad': base_qs.filter(
                ctwa_entry_at__gt=timezone.now() - timedelta(hours=Appointment.CTWA_WINDOW_HOURS)
            ).count(),
            'todays_confirmed_appointments': todays_confirmed_appointments,
        }
        context['delayed_leads_with_countdown'] = delayed_leads_with_countdown
        context['selected_response_age'] = response_age
        context['selected_status_filter'] = status_filter
        context['age_filter_options'] = self.AGE_FILTER_OPTIONS
        return context


@method_decorator(staff_required, name='dispatch')
class ConversationDetailView(TemplateView):
    """WhatsApp-style conversation workspace: a chat list (left), the live
    message thread (centre) with a working reply composer, and an appointment
    editor + lead-intelligence sidebar (right). Reads/writes only existing
    fields — no schema change.
    """
    template_name = 'bot/pages/conversation_detail.html'

    def _thread_list(self, current):
        """Recent conversations for the left rail, current one guaranteed present."""
        rows = list(Appointment.objects.real().order_by('-updated_at')[:40])
        if current.pk not in {a.pk for a in rows}:
            rows.insert(0, current)
        return rows

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = get_object_or_404(Appointment.objects.real(), pk=kwargs['pk'])

        local_sched = None
        if appointment.scheduled_datetime:
            dt = appointment.scheduled_datetime
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            local_sched = timezone.localtime(dt)

        computed_score, computed_status = calculate_lead_score(appointment)
        status_labels = dict(Appointment._meta.get_field('lead_status').choices)

        context.update({
            'active_nav': 'conversations',
            'appointment': appointment,
            'threads': self._thread_list(appointment),
            'conversation_history': appointment.conversation_history or [],
            'sched_date': local_sched.strftime('%Y-%m-%d') if local_sched else '',
            'sched_time': local_sched.strftime('%H:%M') if local_sched else '',
            'computed_lead_score': computed_score,
            'computed_lead_status': computed_status,
            'computed_lead_status_label': status_labels.get(computed_status, 'Cold'),
        })
        return context

    def post(self, request, *args, **kwargs):
        appointment = get_object_or_404(Appointment.objects.real(), pk=kwargs['pk'])
        action = request.POST.get('action')

        if action == 'send':
            text = (request.POST.get('message') or '').strip()
            if text:
                try:
                    result = whatsapp_api.send_text_message(appointment.phone_number, text)
                    wamid = ''
                    if isinstance(result, dict):
                        wamid = (result.get('messages') or [{}])[0].get('id', '')
                    appointment.add_conversation_message('assistant', text, message_id=wamid or None)
                    messages.success(request, 'Message sent.')
                except Exception:
                    messages.error(
                        request,
                        "Couldn't send — the customer's 24-hour WhatsApp window may be closed.",
                    )
        elif action == 'update':
            self._apply_update(request, appointment)

        return redirect('conversation_detail', pk=appointment.pk)

    def _apply_update(self, request, appointment):
        update_fields = []

        if 'service' in request.POST:
            appointment.project_type = (request.POST.get('service') or '').strip() or None
            update_fields.append('project_type')

        if 'notes' in request.POST:
            appointment.internal_notes = (request.POST.get('notes') or '').strip() or None
            update_fields.append('internal_notes')

        date_raw = (request.POST.get('date') or '').strip()
        time_raw = (request.POST.get('time') or '').strip()
        if date_raw and time_raw:
            try:
                naive = datetime.strptime(f'{date_raw} {time_raw}', '%Y-%m-%d %H:%M')
                appointment.scheduled_datetime = timezone.make_aware(naive)
                update_fields.append('scheduled_datetime')
            except ValueError:
                pass

        if update_fields:
            update_fields.append('updated_at')
            appointment.save(update_fields=update_fields)
            messages.success(request, 'Appointment details updated.')


@method_decorator(staff_required, name='dispatch')
class AppointmentsListView(TemplateView):
    """Month calendar of scheduled appointments (main, right) plus a list of the
    month's booked appointments (left). Read-only view built entirely from
    ``Appointment.scheduled_datetime`` — no database changes.
    """
    template_name = 'bot/pages/appointments_list.html'

    # Sunday-first weekday order, matching the calendar mockup.
    WEEKDAY_HEADERS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

    def _local(self, dt):
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return timezone.localtime(dt)

    def get_context_data(self, **kwargs):
        import calendar as _pycal
        from datetime import date as _date

        context = super().get_context_data(**kwargs)
        today = timezone.localdate()

        # Resolve the visible month from ?month=YYYY-MM, defaulting to this month.
        raw = (self.request.GET.get('month') or '').strip()
        try:
            year_str, month_str = raw.split('-')
            first = _date(int(year_str), int(month_str), 1)
        except (ValueError, AttributeError):
            first = today.replace(day=1)
        year, month = first.year, first.month

        days_in_month = _pycal.monthrange(year, month)[1]
        month_start = _date(year, month, 1)
        month_end = _date(year, month, days_in_month)

        appts = (
            Appointment.objects.real()
            .filter(
                scheduled_datetime__date__gte=month_start,
                scheduled_datetime__date__lte=month_end,
            )
            .exclude(status='cancelled')
            .order_by('scheduled_datetime')
        )

        by_day = {}
        booked = []
        for appt in appts:
            local = self._local(appt.scheduled_datetime)
            entry = {
                'pk': appt.pk,
                'time': local.strftime('%H:%M'),
                'name': appt.customer_name or appt.phone_number or 'Appointment',
                'service': appt.get_project_type_display() if appt.project_type else 'No service',
                'status': appt.status,
            }
            by_day.setdefault(local.day, []).append(entry)
            if appt.status == 'confirmed':
                booked.append({**entry, 'date_label': local.strftime('%a %d %b')})

        _pycal.setfirstweekday(_pycal.SUNDAY)
        weeks = []
        for week in _pycal.monthcalendar(year, month):
            cells = []
            for day_num in week:
                if day_num == 0:
                    cells.append(None)
                    continue
                cells.append({
                    'day': day_num,
                    'is_today': (year == today.year and month == today.month and day_num == today.day),
                    'appts': by_day.get(day_num, []),
                })
            weeks.append(cells)

        prev_month = (month_start - timedelta(days=1)).replace(day=1)
        next_month = month_end + timedelta(days=1)

        context.update({
            'active_nav': 'appointments',
            'calendar_weeks': weeks,
            'weekday_headers': self.WEEKDAY_HEADERS,
            'month_label': month_start.strftime('%B %Y'),
            'prev_month': prev_month.strftime('%Y-%m'),
            'next_month': next_month.strftime('%Y-%m'),
            'is_current_month': (year == today.year and month == today.month),
            'booked_appointments': booked,
            'booked_count': len(booked),
            'scheduled_count': len(by_day),
        })
        return context


@method_decorator(staff_required, name='dispatch')
class PriorityLeadsView(TemplateView):
    template_name = 'bot/pages/priority_leads_dashboard.html'

    def _group_leads_by_date(self, leads_qs):
        from collections import OrderedDict

        grouped = OrderedDict()
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)

        for lead in leads_qs:
            activity = lead.recent_activity
            if not activity:
                label = "No Activity Date"
            else:
                local_activity = timezone.localtime(activity)
                activity_date = local_activity.date()
                if activity_date == today:
                    label = "Today"
                elif activity_date == yesterday:
                    label = "Yesterday"
                else:
                    label = local_activity.strftime("%b %d, %Y")

            grouped.setdefault(label, []).append(lead)

        return [{"label": label, "leads": items} for label, items in grouped.items()]

    def _recommended_action(self, lead):
        if lead.computed_status == 'very_hot':
            if lead.manual_followup_done:
                return "Finalize booking time and send confirmation details."
            return "Call now and lock in the site visit time."
        if lead.computed_status == 'hot':
            if lead.manual_followup_done:
                return "Follow up on quote details and push to booking."
            return "Call within 30 minutes to complete missing details."
        if lead.computed_status == 'warm':
            if lead.manual_followup_done:
                return "Set a reminder and wait for customer response."
            return "Send a WhatsApp check-in for missing project info."
        if lead.computed_score == 20:
            return "Send a quick nudge to re-engage this lead."
        return "Move to nurture sequence or close as cold lead."

    def _enrich_leads(self, leads_qs):
        leads = list(leads_qs)
        for lead in leads:
            lead.recommended_action = self._recommended_action(lead)
        return leads

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        response_age = self.request.GET.get('response_age', '').strip()
        if not response_age:
            # Default to the last 7 days — the actionable recent leads, and
            # consistent with the nav badge / dashboard hot-lead count (also 7
            # days). Users can still widen via the Window selector.
            response_age = '1w_minus'
        workspace = _priority_leads_workspace_data(response_age)
        very_hot_leads = workspace['very_hot_leads']
        hot_leads = workspace['hot_leads']
        warm_leads = workspace['warm_leads']
        luke_warm_leads = workspace['luke_warm_leads']
        cold_leads = workspace['cold_leads']
        total_leads = workspace['total_leads']

        sections = [
            {
                'id': 'sec-vh',
                'title': 'Very Hot Leads',
                'icon': 'fire',
                'css': 'sec-vh',
                'status_bg': '#fee2e2',
                'status_fg': '#991b1b',
                'border': '#dc2626',
                'empty_label': 'No very hot leads.',
                'recommended_action': 'Call now and lock in the site visit time.',
                'count': len(very_hot_leads),
                'pending_count': len([lead for lead in very_hot_leads if not lead.manual_followup_done]),
                'done_count': len([lead for lead in very_hot_leads if lead.manual_followup_done]),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in very_hot_leads if not lead.manual_followup_done])),
                'done_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in very_hot_leads if lead.manual_followup_done])),
            },
            {
                'id': 'sec-hot',
                'title': 'Hot Leads',
                'icon': 'exclamation-triangle',
                'css': 'sec-hot',
                'status_bg': '#fef3c7',
                'status_fg': '#92400e',
                'border': '#f59e0b',
                'empty_label': 'No hot leads.',
                'recommended_action': 'Call within 30 minutes to complete missing details.',
                'count': len(hot_leads),
                'pending_count': len([lead for lead in hot_leads if not lead.manual_followup_done]),
                'done_count': len([lead for lead in hot_leads if lead.manual_followup_done]),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in hot_leads if not lead.manual_followup_done])),
                'done_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in hot_leads if lead.manual_followup_done])),
            },
            {
                'id': 'sec-warm',
                'title': 'Warm Leads',
                'icon': 'sun',
                'css': 'sec-warm',
                'status_bg': '#d1fae5',
                'status_fg': '#065f46',
                'border': '#10b981',
                'empty_label': 'No warm leads.',
                'recommended_action': 'Send a WhatsApp check-in for missing project info.',
                'count': len(warm_leads),
                'pending_count': len([lead for lead in warm_leads if not lead.manual_followup_done]),
                'done_count': len([lead for lead in warm_leads if lead.manual_followup_done]),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in warm_leads if not lead.manual_followup_done])),
                'done_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in warm_leads if lead.manual_followup_done])),
            },
            {
                'id': 'sec-luke',
                'title': 'Luke-warm Leads',
                'icon': 'temperature-low',
                'css': 'sec-luke',
                'status_bg': '#dbeafe',
                'status_fg': '#1e3a8a',
                'border': '#0ea5e9',
                'empty_label': 'No luke-warm leads.',
                'recommended_action': 'Send a quick nudge to re-engage this lead.',
                'count': len(luke_warm_leads),
                'pending_count': len([lead for lead in luke_warm_leads if not lead.manual_followup_done]),
                'done_count': len([lead for lead in luke_warm_leads if lead.manual_followup_done]),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in luke_warm_leads if not lead.manual_followup_done])),
                'done_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in luke_warm_leads if lead.manual_followup_done])),
            },
            {
                'id': 'sec-cold',
                'title': 'Cold Leads',
                'icon': 'snowflake',
                'css': 'sec-cold',
                'status_bg': '#e5e7eb',
                'status_fg': '#374151',
                'border': '#6b7280',
                'empty_label': 'No cold leads.',
                'recommended_action': 'Move to nurture sequence or close as cold lead.',
                'count': len(cold_leads),
                'pending_count': len([lead for lead in cold_leads if not lead.manual_followup_done]),
                'done_count': len([lead for lead in cold_leads if lead.manual_followup_done]),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in cold_leads if not lead.manual_followup_done])),
                'done_by_date': self._group_leads_by_date(self._enrich_leads([lead for lead in cold_leads if lead.manual_followup_done])),
            },
        ]

        context.update(
            {
                'very_hot_leads': very_hot_leads,
                'hot_leads': hot_leads,
                'warm_leads': warm_leads,
                'luke_warm_leads': luke_warm_leads,
                'cold_leads': cold_leads,
                'very_hot_by_date': self._group_leads_by_date(very_hot_leads),
                'hot_by_date': self._group_leads_by_date(hot_leads),
                'warm_by_date': self._group_leads_by_date(warm_leads),
                'luke_warm_by_date': self._group_leads_by_date(luke_warm_leads),
                'cold_by_date': self._group_leads_by_date(cold_leads),
                'total_leads': total_leads,
                'selected_response_age': response_age,
                'manual_followup_pending_count': len([lead for lead in very_hot_leads + hot_leads + warm_leads + luke_warm_leads + cold_leads if not lead.manual_followup_done]),
                'manual_followup_done_count': len([lead for lead in very_hot_leads + hot_leads + warm_leads + luke_warm_leads + cold_leads if lead.manual_followup_done]),
                'sections': sections,
                'follow_up_status_choices': Appointment._meta.get_field('follow_up_status').choices,
            }
        )
        return context


@staff_required
@require_POST
def update_priority_lead_card(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    now = timezone.now()
    next_url = request.POST.get('next') or reverse('priority_leads')
    if not next_url.startswith('/'):
        next_url = reverse('priority_leads')

    update_fields = []
    manual_state = request.POST.get('manual_followup_state', '').strip()
    follow_up_status = request.POST.get('follow_up_status', '').strip()
    note = request.POST.get('note', '').strip()
    notes_to_prepend = []

    if manual_state in {'done', 'pending'}:
        appointment.manual_followup_done = manual_state == 'done'
        appointment.manual_followup_updated_at = now
        update_fields.extend(['manual_followup_done', 'manual_followup_updated_at'])
        notes_to_prepend.append(
            f"[{timezone.localtime(now).strftime('%Y-%m-%d %H:%M')}] "
            f"{request.user.username}: manual follow-up marked as "
            f"{'done' if appointment.manual_followup_done else 'pending'} from priority dashboard."
        )

    valid_statuses = {choice[0] for choice in Appointment._meta.get_field('follow_up_status').choices}
    if follow_up_status in valid_statuses:
        appointment.follow_up_status = follow_up_status
        update_fields.append('follow_up_status')

    if note:
        timestamp = timezone.localtime(now).strftime('%Y-%m-%d %H:%M')
        notes_to_prepend.append(f"[Priority Dashboard {timestamp}] {note}")

    if notes_to_prepend:
        existing_notes = appointment.admin_notes or ''
        appointment.admin_notes = "\n".join(notes_to_prepend + ([existing_notes] if existing_notes else []))
        update_fields.append('admin_notes')

    if update_fields:
        appointment.save(update_fields=sorted(set(update_fields)))
        messages.success(request, 'Priority lead updated.')
    else:
        messages.info(request, 'No changes were submitted.')

    return redirect(next_url)


@method_decorator(staff_required, name='dispatch')
class AppointmentDetailView(DetailView):
    template_name = 'bot/pages/appointment_detail.html'
    model = Appointment
    context_object_name = 'appointment'
    #
    @staticmethod
    def _followup_info_for(appointment):
        """Next automatic follow-up (attempt, due time, ad-cadence flag) for the
        detail page. Uses the cron's timing core so the shown time matches sends."""
        try:
            from bot.management.commands.send_followups import Command as _FollowupCmd
            return _FollowupCmd().next_followup_due_at(appointment)
        except Exception:
            return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_object()
        computed_score, computed_status = calculate_lead_score(appointment)
        conversation_history = appointment.conversation_history
        uploaded_files = appointment.get_all_uploaded_files()   # ← NEW
        detail_source = self.request.GET.get('source', 'appointments')
        valid_sources = {'appointments', 'conversations', 'dashboard', 'priority_leads', 'followups'}
        if detail_source not in valid_sources:
            detail_source = 'appointments'

        is_frame = self.request.GET.get('frame') == '1'
        base_template = 'bot/layouts/panel.html' if is_frame else 'bot/layouts/base.html'

        source_workspace = {}
        active_nav = 'appointments'
        source_back_url = reverse('appointments_list')
        source_title = 'Appointments'
        if detail_source == 'dashboard':
            source_workspace = _dashboard_workspace_data(self.request.GET.get('response_age', '1w_minus'))
            active_nav = 'dashboard'
            source_back_url = reverse('dashboard')
            source_title = 'Dashboard'
        elif detail_source == 'priority_leads':
            source_workspace = _priority_leads_workspace_data(self.request.GET.get('response_age', '1w_minus'))
            active_nav = 'leads'
            source_back_url = reverse('priority_leads')
            source_title = 'Priority Leads'
        elif detail_source == 'followups':
            source_workspace = _followups_workspace_data(self.request.GET.get('response_age', '1w_minus'))
            active_nav = 'followups'
            source_back_url = reverse('followup_dashboard')
            source_title = 'Follow-ups'
        elif detail_source == 'conversations':
            active_nav = 'conversations'
            source_back_url = reverse('conversations_list')
            source_title = 'Conversations'

        sidebar_filter = self.request.GET.get('sidebar_filter', 'all')
        sidebar_response_age = self.request.GET.get('sidebar_response_age', 'all')
        sidebar_context = _appointments_sidebar_context(sidebar_filter, sidebar_response_age)

        context.update({
            'active_nav': active_nav,
            'is_frame': is_frame,
            'base_template': base_template,
            'sidebar_filter': sidebar_filter,
            'conversation_history': conversation_history,
            'completeness': appointment.get_customer_info_completeness(),
            'documents': uploaded_files,
            'has_documents': appointment.has_uploaded_documents(),
            'document_count': len(uploaded_files),
            'uploaded_images': [f for f in uploaded_files if f['type'] in ('image', 'video')],  # ← NEW
            'computed_lead_score': computed_score,
            'computed_lead_status': computed_status,
            'computed_lead_status_label': dict(Appointment._meta.get_field('lead_status').choices).get(computed_status, 'Cold'),
            'followup_info': self._followup_info_for(appointment),
            'detail_source': detail_source,
            'source_workspace': source_workspace,
            'source_back_url': source_back_url,
            'source_title': source_title,
            'email_catalog': _email_catalog_for_template(),
            **sidebar_context,
        })
        return context
    def post(self, request, *args, **kwargs):
        """Handle form submission for updating appointment"""
        appointment = self.get_object()

        # Plan attach/replace from the glance card — its own one-field form,
        # handled before the edit-form fields so a plan upload never touches
        # any other appointment data. A replaced plan goes back to
        # 'plan_uploaded' (the new file hasn't been reviewed yet).
        plan_upload = request.FILES.get('plan_file')
        if plan_upload:
            try:
                appointment.plan_file = plan_upload
                appointment.has_plan = True
                appointment.plan_status = 'plan_uploaded'
                appointment.plan_uploaded_at = timezone.now()
                appointment.save()
                messages.success(request, 'Plan attached to this appointment.')
            except Exception as e:
                messages.error(request, f'Error attaching plan: {str(e)}')
            base_url = reverse('appointment_detail', kwargs={'pk': appointment.pk})
            qs = request.GET.urlencode()
            return redirect(f"{base_url}?{qs}" if qs else base_url)

        try:
            # Update fields from POST data
            appointment.customer_name = request.POST.get('customer_name', appointment.customer_name)
            appointment.project_type = request.POST.get('project_type', appointment.project_type)
            appointment.property_type = request.POST.get('property_type', appointment.property_type)
            appointment.customer_area = request.POST.get('customer_area', appointment.customer_area)
            # Project Description is editable from the detail form (it was being
            # dropped on save because the POST handler never read this field).
            appointment.project_description = request.POST.get('project_description', appointment.project_description)
            # Email is editable from the detail form; blank string clears it (field is null/blank-able)
            appointment.customer_email = (request.POST.get('customer_email', appointment.customer_email or '') or '').strip() or None
            appointment.timeline = request.POST.get('timeline', appointment.timeline)
            appointment.follow_up_status = request.POST.get('follow_up_status', appointment.follow_up_status)
            appointment.admin_notes = request.POST.get('admin_notes', appointment.admin_notes)

            next_follow_up_raw = request.POST.get('next_follow_up_at')
            if next_follow_up_raw:
                next_dt = datetime.fromisoformat(next_follow_up_raw)
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                if next_dt.tzinfo is None:
                    next_dt = sa_timezone.localize(next_dt)
                appointment.next_follow_up_at = next_dt

            # Handle datetime fields based on appointment type
            if appointment.appointment_type == 'job_appointment':
                job_datetime = request.POST.get('job_scheduled_datetime')
                if job_datetime:
                    # Parse string into datetime object
                    dt = datetime.strptime(job_datetime, "%Y-%m-%d %H:%M")
                    # Make timezone aware
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    appointment.job_scheduled_datetime = sa_timezone.localize(dt)
            else:
                scheduled_datetime = request.POST.get('scheduled_datetime')
                if scheduled_datetime:
                    dt = datetime.fromisoformat(scheduled_datetime)
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    if dt.tzinfo is None:
                        dt = sa_timezone.localize(dt)
                    appointment.scheduled_datetime = dt

            appointment.save()
            refresh_lead_score(appointment)
            messages.success(request, 'Appointment updated successfully!')

        except Exception as e:
            messages.error(request, f'Error updating appointment: {str(e)}')

        # Preserve source/frame/sidebar_filter so the split panel stays intact
        base_url = reverse('appointment_detail', kwargs={'pk': appointment.pk})
        qs = request.GET.urlencode()
        return redirect(f"{base_url}?{qs}" if qs else base_url)


@method_decorator(staff_required, name='dispatch')
class AppointmentDocumentsView(DetailView):
    template_name = 'bot/pages/appointment_documents.html'
    model = Appointment
    context_object_name = 'appointment'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_object()
        documents = appointment.get_all_uploaded_files()   # ← was get_uploaded_documents()

        context.update({
            'documents': documents,
            'document_count': len(documents),
        })
        return context


@staff_required
def download_document(request, pk, document_type):
    """View to download specific documents"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if document_type == 'plan_file' and appointment.plan_file:
        try:
            # Serve the file for download
            response = HttpResponse(appointment.plan_file.read(), content_type='application/octet-stream')
            filename = f"plan_{appointment.customer_name or 'customer'}_{appointment.id}{os.path.splitext(appointment.plan_file.name)[1]}"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        except Exception as e:
            messages.error(request, f'Error downloading file: {str(e)}')
    
    messages.error(request, 'Document not found')
    return redirect('appointment_documents', pk=appointment.pk)


@staff_required
def serve_document(request, pk, idx):
    """
    Stream uploaded plan/document number `idx` (index into
    get_all_uploaded_files) through Django. The browser never sees a storage
    URL: direct links proved unreliable in prod (presigned R2 URLs expire;
    a mis-set storage env yields a bare relative path that 404s). Reading
    via default_storage works for any backend and stays staff-gated.
    ?dl=1 forces a download instead of inline view.
    """
    appointment = get_object_or_404(Appointment, pk=pk)
    files = appointment.get_all_uploaded_files()
    if idx < 0 or idx >= len(files):
        raise Http404('Document not found')
    doc = files[idx]
    try:
        file_handle = default_storage.open(doc['path'], 'rb')
    except Exception:
        # Not in the current storage backend (e.g. saved to a container's
        # local disk before R2 was configured). A stored absolute URL is the
        # only remaining chance of reaching it.
        url = doc.get('url') or ''
        if url.startswith('http'):
            return HttpResponseRedirect(url)
        raise Http404('File is not available in storage')
    content_type = mimetypes.guess_type(doc['label'])[0] or 'application/octet-stream'
    return FileResponse(
        file_handle,
        content_type=content_type,
        as_attachment=request.GET.get('dl') == '1',
        filename=doc['label'],
    )


@staff_required
def update_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)

    # ✅ Documents (use helper methods)
    has_documents = appointment.has_uploaded_documents()
    document_count = appointment.get_document_count()

    # ✅ Conversation messages (use related_name)
    conversation_history = appointment.conversation_messages.all()

    if request.method == 'POST':
        form = AppointmentForm(request.POST, request.FILES, instance=appointment)
        if form.is_valid():
            form.save()
            messages.success(request, 'Appointment updated successfully')
            return redirect('appointment_detail', pk=appointment.pk)
    else:
        form = AppointmentForm(instance=appointment)

    return render(request, 'bot/pages/appointment_detail.html', {
        'appointment': appointment,
        'form': form,
        'has_documents': has_documents,
        'document_count': document_count,
        'conversation_history': conversation_history,
    })


def _detail_redirect(request, pk):
    """Redirect to the appointment detail, preserving the current frame/source/
    sidebar query string. Without this, an action triggered from an in-frame
    (tabbed) detail view redirects to the bare detail URL, which renders the FULL
    page layout (nav + appointment list) nested inside the frame — duplicating the
    whole list side by side. Mirrors AppointmentDetailView.post."""
    base_url = reverse('appointment_detail', kwargs={'pk': pk})
    qs = request.GET.urlencode()
    return redirect(f"{base_url}?{qs}" if qs else base_url)


@staff_required
def confirm_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.status = 'confirmed'
    appointment.save()
    try:
        if appointment.scheduled_datetime:
            # Local import (module-load cycle); Plumbot was never imported at
            # module level, so this line NameError'd and the bare except below
            # silently ate it — the Confirm button never sent the confirmation.
            from .plumbot.base import Plumbot
            plumbot = Plumbot(appointment.phone_number)
            appointment_details = plumbot.extract_appointment_details()
            plumbot.send_confirmation_message(appointment_details, appointment.scheduled_datetime)
    except Exception as exc:
        print(f"Failed to send confirmation message for appointment {appointment.pk}: {exc}")
    messages.success(request, 'Appointment confirmed successfully')
    return _detail_redirect(request, appointment.pk)


@staff_required
@require_POST
def complete_lead_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.status = 'completed'
    appointment.follow_up_status = 'completed'
    appointment.is_lead_active = False
    appointment.lead_marked_inactive_at = timezone.now()
    appointment.chatbot_paused = False
    appointment.save(
        update_fields=[
            'status',
            'follow_up_status',
            'is_lead_active',
            'lead_marked_inactive_at',
            'chatbot_paused',
            'updated_at',
        ]
    )
    _append_admin_note(appointment, f"{request.user.username}: lead marked complete from appointment detail.")
    messages.success(request, 'Lead marked as complete and removed from Priority Leads.')
    return _detail_redirect(request, appointment.pk)


@staff_required
def unbook_appointment(request, pk):
    """Reverse an accidental confirm: send the appointment back to pending so the
    chatbot resumes the conversation with the lead. The inverse of confirm_appointment."""
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.status = 'pending'
    appointment.chatbot_paused = False
    appointment.is_lead_active = True
    appointment.save(update_fields=['status', 'chatbot_paused', 'is_lead_active', 'updated_at'])
    messages.success(request, 'Appointment unbooked - back to pending and the chatbot will keep talking to this lead.')
    return _detail_redirect(request, appointment.pk)


@staff_required
def cancel_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.status = 'cancelled'
    appointment.save()
    messages.success(request, 'Appointment cancelled')
    return _detail_redirect(request, appointment.pk)


@staff_required
def export_appointments(request):
    from django.http import HttpResponse
    import csv
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="plumbing_appointments.csv"'
    
    writer = csv.writer(response)
    writer.writerow([
        'Name', 'Phone', 'Service', 'Property Type', 'Area',
        'Timeline', 'Status', 'Appointment Date', 'Created At'
    ])

    for appointment in Appointment.objects.real().order_by('-created_at'):
        writer.writerow([
            appointment.customer_name or '',
            appointment.phone_number,
            # project_type is a CharField — calling it TypeError'd every export.
            appointment.project_type or '',
            appointment.property_type or '',
            appointment.customer_area or '',
            appointment.timeline or '',
            appointment.get_status_display(),
            appointment.scheduled_datetime.strftime('%Y-%m-%d %H:%M') if appointment.scheduled_datetime else '',
            appointment.created_at.strftime('%Y-%m-%d %H:%M')
        ])
    
    return response


@staff_required
def complete_site_visit(request, pk):
    """Mark site visit as completed and prepare for job scheduling"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if appointment.appointment_type != 'site_visit':
        messages.error(request, 'This is not a site visit appointment')
        return redirect('appointment_detail', pk=appointment.pk)
    
    if request.method == 'POST':
        site_visit_notes = request.POST.get('site_visit_notes', '')
        plumber_assessment = request.POST.get('plumber_assessment', '')
        
        # Mark site visit as completed
        appointment.mark_site_visit_completed(
            notes=site_visit_notes,
            assessment=plumber_assessment
        )
        
        messages.success(request, 'Site visit marked as completed. You can now schedule the job appointment.')
        return redirect('schedule_job', pk=appointment.pk)
    
    return render(request, 'bot/pages/complete_site_visit.html', {
        'appointment': appointment
    })


@csrf_exempt
def handle_whatsapp_media(request):
    """FIXED: Handle incoming media files from WhatsApp"""
    if request.method == 'POST':
        try:
            # Get message details
            sender = request.POST.get('From', '')
            num_media = int(request.POST.get('NumMedia', 0))
            
            if not sender or num_media == 0:
                return HttpResponse(status=200)
            
            print(f"📎 Processing {num_media} media files from {sender}")
            
            # Get the appointment
            try:
                appointment = Appointment.objects.get(phone_number=sender)
            except Appointment.DoesNotExist:
                print(f"❌ No appointment found for {sender}")
                # Send helpful message
                twilio_client.messages.create(
                    body="I don't have an active appointment for this number. Please start by telling me about your plumbing needs.",
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender
                )
                return HttpResponse(status=200)
            
            # Check if we should accept media based on appointment state
            # (local import — Plumbot is not a module-level name here)
            from .plumbot.base import Plumbot
            plumbot = Plumbot(sender)
            
            # If they have a plan and we have basic info, initiate upload flow
            if (appointment.has_plan is True and 
                appointment.customer_area and 
                appointment.property_type and
                appointment.plan_status is None):
                
                # Start the plan upload process
                appointment.plan_status = 'pending_upload'
                appointment.save()
                print(f"🔄 Initiated plan upload flow for {sender}")
            
            # Only process media if we're in upload flow
            if appointment.plan_status != 'pending_upload':
                print(f"ℹ️ Ignoring media - not in upload flow. Status: {appointment.plan_status}")
                
                # Send helpful message
                if appointment.has_plan is True:
                    response_msg = "I'll need you to send your plan once we collect some basic information first. Let me continue with a few questions."
                else:
                    response_msg = "I see you sent a file, but I'm not currently expecting any documents. Let me continue with your appointment details."
                
                twilio_client.messages.create(
                    body=response_msg,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender
                )
                return HttpResponse(status=200)
            
            # Process each media file
            uploaded_files = []
            for i in range(num_media):
                media_url = request.POST.get(f'MediaUrl{i}', '')
                media_content_type = request.POST.get(f'MediaContentType{i}', '')
                
                if media_url:
                    file_info = download_and_save_media(
                        media_url, 
                        media_content_type, 
                        appointment, 
                        i
                    )
                    if file_info:
                        uploaded_files.append(file_info)
            
            if uploaded_files:
                # Update plan upload timestamp
                appointment.plan_uploaded_at = timezone.now()
                appointment.save()
                
                # Send acknowledgment using the plumbot's handle_plan_upload_flow
                ack_message = plumbot.handle_plan_upload_flow("file received")
                
                twilio_client.messages.create(
                    body=ack_message,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender
                )
            
            return HttpResponse(status=200)
            
        except Exception as e:
            print(f"❌ Media handling error: {str(e)}")
            return HttpResponse(status=500)
    
    return HttpResponse(status=405)


def download_and_save_media(media_url, content_type, appointment, file_index):
    """Download media from Twilio and save to Django storage - FIXED"""
    try:
        # FIXED: Use correct variable names from top of file
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)  # ✅ Changed from ACCOUNT_SID
        response = requests.get(media_url, auth=auth)
        
        if response.status_code != 200:
            print(f"❌ Failed to download media: {response.status_code}")
            return None
        
        # Determine file extension
        extension_map = {
            'image/jpeg': '.jpg',
            'image/png': '.png', 
            'image/webp': '.webp',
            'application/pdf': '.pdf',
            'image/gif': '.gif'
        }
        
        extension = extension_map.get(content_type, '.bin')
        
        # Generate filename
        timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
        customer_name = appointment.customer_name or 'customer'
        safe_name = ''.join(c for c in customer_name if c.isalnum())
        filename = f"plan_{safe_name}_{appointment.id}_{timestamp}_{file_index}{extension}"
        
        # Save file
        file_path = f"customer_plans/{filename}"
        file_content = ContentFile(response.content, name=filename)
        
        saved_path = default_storage.save(file_path, file_content)
        
        # Update appointment record if this is the first file
        if not getattr(appointment, 'plan_file', None):
            appointment.plan_file = saved_path
            appointment.save()
        
        print(f"✅ Saved media file: {saved_path}")
        
        return {
            'name': filename,
            'path': saved_path,
            'size': len(response.content),
            'content_type': content_type
        }
        
    except Exception as e:
        print(f"❌ Error downloading/saving media: {str(e)}")
        return None
