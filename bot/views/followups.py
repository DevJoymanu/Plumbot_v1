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
from ..services.lead_scoring import refresh_lead_score


@staff_required
def followup_dashboard(request):
    """Dashboard showing follow-up statistics and leads"""
    response_age = request.GET.get('response_age', '').strip()
    if not response_age:
        response_age = '1w_minus'
    context = _followups_workspace_data(response_age)
    context['active_nav'] = 'followups'
    
    return render(request, 'bot/pages/followup_dashboard.html', context)


@staff_required
def mark_lead_inactive(request, pk):
    """Manually mark a lead as inactive"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        reason = request.POST.get('reason', 'manual')
        appointment.mark_as_inactive_lead(reason=reason)
        
        messages.success(request, f'Lead for {appointment.customer_name or appointment.phone_number} marked as inactive')
        return redirect('appointments_list')
    
    return render(request, 'bot/pages/confirm_mark_inactive.html', {
        'appointment': appointment
    })


@staff_required
def reactivate_lead(request, pk):
    """Reactivate an inactive lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        appointment.is_lead_active = True
        appointment.followup_stage = 'none'
        appointment.lead_marked_inactive_at = None
        appointment.save()
        
        messages.success(request, f'Lead reactivated for {appointment.customer_name or appointment.phone_number}')
        return redirect('appointment_detail', pk=appointment.pk)
    
    return render(request, 'bot/pages/confirm_reactivate.html', {
        'appointment': appointment
    })


@staff_required  
def test_followup_message(request, pk):
    """Send a test follow-up message for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        stage = request.POST.get('stage', 'day_1')
        
        # Import the management command to use its message generator
        from django.core.management import call_command
        from io import StringIO
        
        # You can manually craft a test message or use the generator
        from bot.management.commands.send_followups import Command
        cmd = Command()
        message = cmd.generate_followup_message(appointment, stage)
        
        # Send it
        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, message)
        
        messages.success(request, f'Test {stage} follow-up sent to {appointment.phone_number}')
        return redirect('appointment_detail', pk=appointment.pk)
    
    return render(request, 'bot/pages/test_followup.html', {
        'appointment': appointment,
        'stages': ['day_1', 'day_3', 'week_1', 'week_2', 'month_1']
    })


@staff_required
@require_POST
def manual_followup_check(request):
    """Manually trigger follow-up check (for testing/debugging)"""
    from django.core.management import call_command
    from io import StringIO
    
    try:
        out = StringIO()
        call_command('send_followups', stdout=out)
        output = out.getvalue()
        
        messages.success(request, 'Follow-up check completed successfully')
        
        # Show summary
        for line in output.split('\n'):
            if 'Sent:' in line or 'Skipped:' in line or 'Errors:' in line:
                messages.info(request, line.strip())
        
    except Exception as e:
        messages.error(request, f'Error running follow-up check: {str(e)}')
    
    return redirect('followup_dashboard')


@staff_required
@require_POST
def pause_chatbot(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.pause_chatbot()
    # Also write delay signal so automated follow-ups stop immediately
    notes = appointment.internal_notes or ''
    if '[DELAY_SIGNAL]' not in notes:
        appointment.internal_notes = (notes + '\n[DELAY_SIGNAL]').strip()
        appointment.save(update_fields=['internal_notes'])
    _append_admin_note(appointment, f"{request.user.username}: chatbot paused — follow-ups also paused.")
    messages.success(request, 'Chatbot paused. Automated follow-ups also stopped.')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def resume_chatbot(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.resume_chatbot()
    # Clear delay signal so automated follow-ups can resume
    notes = appointment.internal_notes or ''
    if '[DELAY_SIGNAL]' in notes:
        cleaned = '\n'.join(
            line for line in notes.splitlines()
            if '[DELAY_SIGNAL]' not in line
        ).strip()
        appointment.internal_notes = cleaned
        appointment.save(update_fields=['internal_notes'])
    _append_admin_note(appointment, f"{request.user.username}: chatbot resumed — follow-ups also resumed.")
    messages.success(request, 'Chatbot resumed. Automated follow-ups also restarted.')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def pause_auto_followup(request, pk):
    """Pause automatic follow-ups for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    pause_duration = request.POST.get('pause_duration')
    
    if pause_duration == 'permanent':
        # Pause indefinitely
        appointment.manual_followup_paused = True
        appointment.manual_followup_paused_until = None
        pause_msg = "permanently"
    else:
        # Pause for specified hours
        hours = int(pause_duration)
        pause_until = timezone.now() + timedelta(hours=hours)
        appointment.manual_followup_paused = True
        appointment.manual_followup_paused_until = pause_until
        
        # Human-friendly duration
        if hours == 24:
            pause_msg = "for 24 hours"
        elif hours == 48:
            pause_msg = "for 48 hours"
        elif hours == 168:
            pause_msg = "for 1 week"
        elif hours == 720:
            pause_msg = "for 1 month"
        else:
            pause_msg = f"for {hours} hours"
    
    appointment.save()
    
    messages.success(request, f'⏸️ Automatic follow-ups paused {pause_msg}')
    logger.info(f"Auto follow-ups paused {pause_msg} for appointment {pk} by {request.user.username}")
    
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def resume_auto_followup(request, pk):
    """Resume automatic follow-ups for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    appointment.manual_followup_paused = False
    appointment.manual_followup_paused_until = None
    appointment.save()
    
    messages.success(request, '▶️ Automatic follow-ups resumed')
    logger.info(f"Auto follow-ups resumed for appointment {pk} by {request.user.username}")
    
    return redirect('appointment_detail', pk=pk)


@staff_required
def send_followup(request, pk):
    """Send MANUAL follow-up message via WhatsApp."""
    from django.http import JsonResponse as _JsonResponse
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method != 'POST':
        return redirect('appointment_detail', pk=appointment.pk)

    message = request.POST.get('message', '').strip()
    if not message:
        if is_ajax:
            return _JsonResponse({'ok': False, 'error': 'Message cannot be empty.'}, status=400)
        messages.error(request, 'Message cannot be empty')
        return redirect('appointment_detail', pk=appointment.pk)

    try:
        customer_name = appointment.customer_name or "there"
        personalized_message = message.replace('{name}', customer_name)
        clean_phone = clean_phone_number(appointment.phone_number)

        whatsapp_api.send_text_message(clean_phone, personalized_message)

        appointment.add_conversation_message('assistant', f"[MANUAL FOLLOW-UP] {personalized_message}")
        appointment.last_followup_sent = timezone.now()
        appointment.followup_count = (appointment.followup_count or 0) + 1
        appointment.followup_stage = 'responded'
        appointment.save(update_fields=['last_followup_sent', 'followup_count', 'followup_stage'])

        logger.info(f"✅ MANUAL follow-up sent by {request.user.username} to {clean_phone}")

        if is_ajax:
            return _JsonResponse({'ok': True, 'message': f'Message sent to {clean_phone}.'})
        messages.success(request, f'✅ Manual follow-up sent to {clean_phone}!')

    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ MANUAL follow-up error: {error_msg}")
        if is_ajax:
            return _JsonResponse({'ok': False, 'error': f'Failed to send: {error_msg}'}, status=500)
        messages.error(request, f'Failed to send message: {error_msg}')

    return redirect('appointment_detail', pk=appointment.pk)


@staff_required
@require_POST  
def send_portfolio_to_lead(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    clean_phone = clean_phone_number(appointment.phone_number)
    sent = send_previous_work_photos(clean_phone, appointment)
    if sent:
        messages.success(request, 'Portfolio photos queued for sending.')
    else:
        messages.warning(request, 'No portfolio photos configured.')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def send_image_to_lead(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    image_url = request.POST.get('image_url', '').strip()
    caption = request.POST.get('caption', '').strip()

    if not image_url:
        messages.error(request, 'No image URL provided.')
        return redirect('appointment_detail', pk=pk)

    try:
        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_media_message(
            to=clean_phone,
            media_url=image_url,
            media_type='image',
            caption=caption or None,
        )
        appointment.add_conversation_message(
            'assistant',
            f'[IMAGE SENT] URL: {image_url} | Caption: {caption}'
        )
        appointment.last_outbound_at = timezone.now()
        appointment.save(update_fields=['last_outbound_at'])
        messages.success(request, 'Image sent successfully!')
    except Exception as e:
        messages.error(request, f'Failed to send image: {str(e)}')

    return redirect('appointment_detail', pk=pk)


@staff_required
def send_bulk_followup(request):
    """Send manual follow-up to multiple leads at once"""
    if request.method == 'POST':
        lead_ids = request.POST.getlist('lead_ids')
        message_template = request.POST.get('message_template', '').strip()
        pause_duration = int(request.POST.get('pause_duration', 48))
        
        if not lead_ids or not message_template:
            messages.error(request, 'Please select leads and provide a message')
            return redirect('followup_dashboard')
        
        results = {
            'sent': 0,
            'failed': 0,
            'errors': []
        }
        
        for lead_id in lead_ids:
            try:
                appointment = Appointment.objects.get(id=lead_id)
                
                # Personalize message with customer name
                customer_name = appointment.customer_name or "there"
                personalized_message = message_template.replace('{name}', customer_name)
                
                # Send message
                clean_phone = clean_phone_number(appointment.phone_number)
                whatsapp_api.send_text_message(clean_phone, personalized_message)
                
                # Update tracking
                appointment.add_conversation_message('assistant', f"[BULK MANUAL FOLLOW-UP] {personalized_message}")
                appointment.last_followup_sent = timezone.now()
                appointment.last_manual_followup_sent = timezone.now()
                appointment.followup_count = (appointment.followup_count or 0) + 1
                appointment.manual_followup_count = (appointment.manual_followup_count or 0) + 1
                appointment.is_automatic_followup = False
                appointment.followup_stage = 'responded'
                
                # Pause automatic follow-ups
                pause_until = timezone.now() + timedelta(hours=pause_duration)
                appointment.manual_followup_paused = True
                appointment.manual_followup_paused_until = pause_until
                
                appointment.save()
                
                results['sent'] += 1
                
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"Lead {lead_id}: {str(e)}")
                logger.error(f"Bulk follow-up error for lead {lead_id}: {str(e)}")
        
        # Show results
        if results['sent'] > 0:
            messages.success(request, f"✅ Sent {results['sent']} manual follow-ups (auto follow-ups paused)")
        if results['failed'] > 0:
            messages.warning(request, f"⚠️ Failed to send {results['failed']} messages")
        
        return redirect('followup_dashboard')
    
    # GET request - show bulk follow-up form
    active_leads = Appointment.objects.filter(
        is_lead_active=True,
        status='pending'
    ).order_by('-last_customer_response')
    
    return render(request, 'bot/pages/bulk_followup.html', {
        'leads': active_leads
    })
