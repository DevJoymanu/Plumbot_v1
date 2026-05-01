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


class CalendarView(View):
    template_name = 'bot/pages/calendar.html'

    def get(self, request):
        return render(request, self.template_name)


def appointment_data(request):
    """
    Return all appointments as JSON data
    Optional filter: ?service=bathroom or kitchen or installation
    """
    service_filter = request.GET.get('service')
    
    appointments = Appointment.objects.all()
    if service_filter and service_filter != "all":
        appointments = appointments.filter(project_type__icontains=service_filter)

    data = []
    for appt in appointments:
        if appt.scheduled_datetime:
            data.append({
                "id": appt.id,
                "customerName": appt.customer_name or "Unknown",
                "phone": appt.phone_number,
                "date": appt.scheduled_datetime.date().isoformat(),
                "time": appt.scheduled_datetime.time().strftime("%H:%M"),
                "service": map_project_type_to_service_key(appt.project_type),
                "serviceLabel": appt.get_project_type_display() if appt.project_type else "No service",
                "area": appt.customer_area or "N/A",
                "status": appt.status,
                "statusLabel": "Booked" if appt.status == "confirmed" else appt.get_status_display(),
                "projectDescription": appt.project_description or "No project description yet",
            })

    return JsonResponse(data, safe=False)


def map_project_type_to_service_key(project_type):
    """Map full project_type to frontend's JS service keys"""
    mapping = {
        "bathroom_renovation": "bathroom",
        "kitchen_renovation": "kitchen",
        "new_plumbing_installation": "installation",
    }
    return mapping.get(project_type, "other")
