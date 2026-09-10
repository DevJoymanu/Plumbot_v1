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
from ..decorators import (
    staff_required, superuser_required, anonymous_required, StaffRequiredMixin,
    owner_required,
)
from ..whatsapp_cloud_api import whatsapp_api
from ..services.clients import (
    deepseek_client, GOOGLE_CALENDAR_CREDENTIALS, DEEPSEEK_API_KEY,
)
from ..utils import (
    _to_decimal, _to_float, _safe_logo_url, _safe_logo_data_uri,
    _reset_pk_sequence, _append_admin_note,
    clean_phone_number, format_phone_number_for_storage,
)

logger = logging.getLogger(__name__)


@superuser_required
def settings_view(request):
    if request.method == 'POST':
        form = SettingsForm(request.POST)
        if form.is_valid():
            # Save settings to database or configuration
            messages.success(request, 'Settings updated successfully')
            return redirect('settings')
    else:
        initial_data = {
            'team_numbers': '\n'.join(getattr(settings, 'TEAM_NUMBERS', [])),
        }
        form = SettingsForm(initial=initial_data)
    
    return render(request, 'bot/pages/settings.html', {
        'form': form,
        'active_tab': 'general'
    })


@superuser_required
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


@superuser_required
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


@superuser_required
def email_settings_view(request):
    """Whether the bot can send, read and answer email -- and prove it.

    READ-ONLY for everybody who can reach this page; the TEST buttons are the
    platform owner's alone (`is_platform_owner`, i.e. adminJ), because each one
    performs a real action: sending mail, logging into the operator's own inbox,
    and spending a DeepSeek call. Seeing the state costs nothing and is what
    everybody needs; performing it is not.

    Note the page itself is superuser-only, like the rest of Settings -- it is
    platform config, not a tenant control. So "everybody else" here means the
    other superusers.
    """
    from ..email_health import email_capabilities

    tenant = getattr(request, 'tenant', None)
    return render(request, 'bot/pages/settings.html', {
        'active_tab': 'email',
        'email_capabilities': email_capabilities(tenant),
        'email_test_address': _email_test_address(request),
    })


def _email_test_address(request):
    """Where a test email goes: the operator's OWN address.

    Deliberately not a free-text field. A settings page with a "send an email to
    anything you type" button is a relay for whoever holds the account, and the
    question being answered here is only ever "does sending work at all", which
    the operator's own inbox answers.
    """
    own = (getattr(request.user, 'email', '') or '').strip()
    if own:
        return own
    # PLATFORM_NOTIFICATION_EMAIL is a module constant in plumber_notifications,
    # NOT a Django setting -- reading it off `settings` silently returned '' and
    # the send test then refused with "no address to send the test to" for any
    # operator whose login carries no email address.
    from ..plumber_notifications import PLATFORM_NOTIFICATION_EMAIL
    return (PLATFORM_NOTIFICATION_EMAIL or '').strip()


@owner_required
@require_POST
def email_health_test(request, capability):
    """Run one email test. Owner-only, and re-checked here rather than trusted to
    the template: hiding a button is presentation, not permission."""
    from ..email_health import capability_keys, run_test

    if capability not in capability_keys():
        messages.error(request, f'Unknown email test: {capability}')
        return redirect('email_settings')

    result = run_test(capability, to=_email_test_address(request),
                      tenant=getattr(request, 'tenant', None))
    body = result['summary'] + ((' ' + result['detail']) if result['detail'] else '')
    (messages.success if result['ok'] else messages.error)(request, body)
    return redirect('email_settings')


@staff_required
def test_whatsapp(request):
    """Send a test WhatsApp message to the team numbers via the Meta Cloud API.

    Was a direct Twilio client call; now goes out on the active tenant's own
    channel, the same path every real message uses.
    """
    results = None
    if request.method == 'POST':
        from ..whatsapp_cloud_api import get_client_for_tenant
        tenant = getattr(request, 'tenant', None)
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        test_message = (
            "Test notification\n\n"
            "This is a test message to verify WhatsApp notifications are working.\n"
            f"Time: {stamp}\n\n"
            "If you received this, notifications are working."
        )
        try:
            client = get_client_for_tenant(tenant)
            results = {'success': True, 'results': []}
            for number in getattr(settings, 'TEAM_NUMBERS', []):
                clean = (number or '').replace('whatsapp:', '').replace('+', '').strip()
                try:
                    resp = client.send_text_message(clean, test_message)
                    try:
                        wamid = (resp.get('messages') or [{}])[0].get('id', '')
                    except Exception:
                        wamid = ''
                    results['results'].append({
                        'number': number, 'status': 'success',
                        'sid': wamid, 'error': None,
                    })
                except Exception as e:
                    results['results'].append({
                        'number': number, 'status': 'failed',
                        'sid': None, 'error': str(e),
                    })
        except Exception as e:
            results = {'success': False, 'error': str(e)}

    return render(request, 'bot/pages/test_whatsapp.html', {
        'results': results
    })
