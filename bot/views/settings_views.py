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


@staff_required
def settings_view(request):
    if request.method == 'POST':
        form = SettingsForm(request.POST)
        if form.is_valid():
            # Save settings to database or configuration
            messages.success(request, 'Settings updated successfully')
            return redirect('settings')
    else:
        initial_data = {
            'twilio_account_sid': getattr(settings, 'TWILIO_ACCOUNT_SID', ''),
            'twilio_auth_token': getattr(settings, 'TWILIO_AUTH_TOKEN', ''),
            'twilio_whatsapp_number': getattr(settings, 'TWILIO_WHATSAPP_NUMBER', ''),
            'team_numbers': '\n'.join(getattr(settings, 'TEAM_NUMBERS', [])),
        }
        form = SettingsForm(initial=initial_data)
    
    return render(request, 'bot/pages/settings.html', {
        'form': form,
        'active_tab': 'general'
    })


@staff_required
def calendar_settings_view(request):
    if request.method == 'POST':
        form = CalendarSettingsForm(request.POST)
        if form.is_valid():
            # Save calendar settings
            messages.success(request, 'Calendar settings updated successfully')
            return redirect('calendar_settings')
    else:
        initial_data = {
            'google_calendar_credentials': json.dumps(
                getattr(settings, 'GOOGLE_CALENDAR_CREDENTIALS', {}),
                indent=2
            ),
            'calendar_id': getattr(settings, 'GOOGLE_CALENDAR_ID', 'primary'),
        }
        form = CalendarSettingsForm(initial=initial_data)
    
    return render(request, 'bot/pages/settings.html', {
        'form': form,
        'active_tab': 'calendar'
    })


@staff_required
def ai_settings_view(request):
    if request.method == 'POST':
        form = AISettingsForm(request.POST)
        if form.is_valid():
            # Save AI settings
            messages.success(request, 'AI settings updated successfully')
            return redirect('ai_settings')
    else:
        initial_data = {
            'deepseek_api_key': getattr(settings, 'DEEPSEEK_API_KEY', ''),
            'ai_temperature': getattr(settings, 'AI_TEMPERATURE', 0.7),
        }
        form = AISettingsForm(initial=initial_data)
    
    return render(request, 'bot/pages/settings.html', {
        'form': form,
        'active_tab': 'ai'
    })


@staff_required
def test_whatsapp(request):
    results = None
    if request.method == 'POST':
        try:
            client = Client(
                settings.TWILIO_ACCOUNT_SID,
                settings.TWILIO_AUTH_TOKEN
            )
            
            test_message = """🧪 TEST NOTIFICATION

This is a test message to verify WhatsApp notifications are working.
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

If you receive this, notifications are working! ✅"""

            team_numbers = getattr(settings, 'TEAM_NUMBERS', [])
            results = {
                'success': True,
                'results': []
            }
            
            for number in team_numbers:
                try:
                    message = client.messages.create(
                        body=test_message,
                        from_=settings.TWILIO_WHATSAPP_NUMBER,
                        to=number
                    )
                    results['results'].append({
                        'number': number,
                        'status': 'success',
                        'sid': message.sid,
                        'error': None
                    })
                except Exception as e:
                    results['results'].append({
                        'number': number,
                        'status': 'failed',
                        'sid': None,
                        'error': str(e)
                    })
            
        except Exception as e:
            results = {
                'success': False,
                'error': str(e)
            }
    
    return render(request, 'bot/pages/test_whatsapp.html', {
        'results': results
    })
