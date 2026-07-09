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
def schedule_job(request, pk):
    """Schedule job appointment after site visit"""
    site_visit = get_object_or_404(Appointment, pk=pk)
    
    # Check if this appointment can have a job scheduled
    if site_visit.appointment_type == 'job' or site_visit.status != 'confirmed':
        messages.error(request, 'Cannot schedule job for this appointment')
        return redirect('appointment_detail', pk=site_visit.pk)
    
    if request.method == 'POST':
        try:
            # Get form data
            job_date = request.POST.get('job_date')
            job_time = request.POST.get('job_time')
            duration_hours = int(request.POST.get('duration_hours', 4))
            job_description = request.POST.get('job_description', '')
            materials_needed = request.POST.get('materials_needed', '')
            
            # Validate required fields
            if not job_date or not job_time:
                messages.error(request, 'Please provide both date and time')
                return render(request, 'bot/pages/schedule_job.html', {
                    'site_visit': site_visit,
                })
            
            # Parse datetime
            job_datetime_str = f"{job_date} {job_time}"
            job_datetime = datetime.strptime(job_datetime_str, '%Y-%m-%d %H:%M')
            
            # Localize to South Africa timezone
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            job_datetime = sa_timezone.localize(job_datetime)
            
            # Check if time is in the future
            if job_datetime <= timezone.now():
                messages.error(request, 'Job time must be in the future')
                return render(request, 'bot/pages/schedule_job.html', {
                    'site_visit': site_visit,
                })
            
            # Check business hours (8 AM - 6 PM, Monday-Friday)
            if job_datetime.weekday() == 5:  # Saturday only
                messages.error(request, 'Jobs can only be scheduled Sunday-Friday (closed Saturdays)')
                return render(request, 'bot/pages/schedule_job.html', {
                    'site_visit': site_visit,
                })
            
            if job_datetime.hour < 8 or job_datetime.hour >= 18:
                messages.error(request, 'Jobs must be scheduled between 8 AM and 6 PM')
                return render(request, 'bot/pages/schedule_job.html', {
                    'site_visit': site_visit,
                })
            
            # FIXED: Create job appointment properly
            # Generate unique phone number for job (since phone_number is unique)
            import uuid
            job_phone = f"job_{uuid.uuid4().hex[:8]}_{site_visit.phone_number}"
            
            job_appointment = Appointment.objects.update(
 #               phone_number=site_visit.phone_number,  # Unique identifier for the job
                customer_name=site_visit.customer_name,
                customer_email=site_visit.customer_email or '',
                customer_area=site_visit.customer_area,
                project_type=site_visit.project_type,
                property_type=site_visit.property_type,
                project_description=job_description or site_visit.project_description,
                scheduled_datetime=job_datetime,
                appointment_type='job',  # Mark as job appointment
                status='scheduled',
                has_plan=site_visit.has_plan,
                timeline=f'{duration_hours} hours',
            )
            
            # Store reference to original site visit if you have a field for it
            # job_appointment.related_site_visit_id = site_visit.id
            # job_appointment.save()
            
            # Send notifications
            try:
                send_job_notifications(job_appointment, materials_needed)
            except Exception as notify_error:
                print(f"⚠️ Notification error: {notify_error}")
            
            messages.success(
                request, 
                f'Job scheduled for {job_datetime.strftime("%B %d, %Y at %I:%M %p")}'
            )
            return redirect('appointment_detail', pk=job_appointment.pk)
            
        except ValueError as e:
            messages.error(request, f'Invalid date/time format: {str(e)}')
        except Exception as e:
            messages.error(request, f'Error scheduling job: {str(e)}')
            print(f"❌ Schedule job error: {str(e)}")
    
    return render(request, 'bot/pages/schedule_job.html', {
        'site_visit': site_visit,
    })


@require_POST
@staff_required
def update_job_status(request, pk):
    """Update job appointment status"""
    job_appointment = get_object_or_404(Appointment, pk=pk)
    
    if job_appointment.appointment_type != 'job_appointment':
        return JsonResponse({'success': False, 'error': 'Not a job appointment'})
    
    new_status = request.POST.get('status')
    
    if new_status not in dict(Appointment.JOB_STATUS_CHOICES):
        return JsonResponse({'success': False, 'error': 'Invalid status'})
    
    job_appointment.job_status = new_status
    
    # If marking as completed, set completion time
    if new_status == 'completed':
        job_appointment.job_completed_at = timezone.now()
    
    job_appointment.save()
    
    # Send notification to customer about status change
    send_job_status_update_notification(job_appointment, new_status)
    
    return JsonResponse({
        'success': True,
        'message': f'Job status updated to {job_appointment.get_job_status_display()}'
    })


def check_job_availability(job_datetime, duration_hours, exclude_appointment_id=None):
    """Check if job time slot is available"""
    try:
        # Calculate job end time
        job_end_time = job_datetime + timedelta(hours=duration_hours)
        
        # Check for overlapping job appointments
        overlapping_jobs = Appointment.objects.filter(
            appointment_type='job_appointment',
            job_status__in=['scheduled', 'in_progress'],
            job_scheduled_datetime__isnull=False,
        )
        
        if exclude_appointment_id:
            overlapping_jobs = overlapping_jobs.exclude(id=exclude_appointment_id)
        
        for job in overlapping_jobs:
            existing_end = job.job_scheduled_datetime + timedelta(hours=job.job_duration_hours)
            
            # Check for overlap
            if (job_datetime < existing_end and job_end_time > job.job_scheduled_datetime):
                return False
        
        # Check business hours (8 AM - 6 PM, Sunday-Friday)
        if job_datetime.weekday() == 5:  # Saturday only
            return False

        if job_datetime.hour < 8 or job_end_time.hour > 18:
            return False
        
        # Check if it's not in the past
        if job_datetime <= timezone.now():
            return False
        
        return True
        
    except Exception as e:
        print(f"Error checking job availability: {str(e)}")
        return False


def send_job_appointment_notifications(job_appointment):
    """Send notifications about new job appointment - UPDATED"""
    try:
        job_date = job_appointment.job_scheduled_datetime.strftime('%A, %B %d, %Y')
        job_time = job_appointment.job_scheduled_datetime.strftime('%I:%M %p')
        duration = job_appointment.job_duration_hours
        
        # Customer notification
        customer_message = f"""🔧 JOB APPOINTMENT SCHEDULED

Hi {job_appointment.customer_name or 'Customer'},

Your plumbing job has been scheduled:

📅 Date: {job_date}
🕐 Time: {job_time}
⏱️ Duration: {duration} hours
📍 Location: {job_appointment.customer_area}
🔨 Work: {job_appointment.job_description or job_appointment.project_type}

We will contact you before arrival.

{f"Materials needed: {job_appointment.job_materials_needed}" if job_appointment.job_materials_needed else ""}

Questions? Reply to this message.

- Plumbing Team"""
        
        # Send to customer
        clean_phone = clean_phone_number(job_appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, customer_message)
        
        # Team notification
        plumber_name = job_appointment.assigned_plumber.get_full_name() if job_appointment.assigned_plumber else "Unassigned"
        
        team_message = f"""👷 NEW JOB SCHEDULED

Customer: {job_appointment.customer_name}
Phone: {job_appointment.phone_number.replace('whatsapp:', '')}
Date/Time: {job_date} at {job_time}
Duration: {duration} hours
Location: {job_appointment.customer_area}
Assigned to: {plumber_name}

Job Description:
{job_appointment.job_description or job_appointment.project_type}

{f"Materials: {job_appointment.job_materials_needed}" if job_appointment.job_materials_needed else ""}

View details: http://127.0.0.1:8000/appointments/{job_appointment.id}/"""
        
        # Send to team
        TEAM_NUMBERS = ['0774819901']
        for number in TEAM_NUMBERS:
            try:
                whatsapp_api.send_text_message(number, team_message)
            except Exception as e:
                print(f"Failed to send team notification: {str(e)}")

        send_plumber_notification_email(
            subject=f"New job scheduled for {job_appointment.customer_name or 'customer'}",
            message=team_message,
        )
        
    except Exception as e:
        print(f"Error sending job appointment notifications: {str(e)}")


def send_job_status_update_notification(job_appointment, new_status):
    """Send notification when job status changes"""
    try:
        status_messages = {
            'in_progress': f"🔧 Your plumbing job at {job_appointment.customer_area} has started. Our plumber is on-site working on your {job_appointment.project_type}.",
            'completed': f"✅ Your plumbing job at {job_appointment.customer_area} has been completed! Thank you for choosing our services. If you have any questions, please let us know.",
            'cancelled': f"❌ Your scheduled plumbing job for {job_appointment.job_scheduled_datetime.strftime('%B %d, %Y')} has been cancelled. We'll contact you to reschedule.",
        }
        
        if new_status in status_messages:
            twilio_client.messages.create(
                body=status_messages[new_status],
                from_=TWILIO_WHATSAPP_NUMBER,
                to=job_appointment.phone_number
            )
            
    except Exception as e:
        print(f"Error sending status update: {str(e)}")


@staff_required 
def reschedule_job(request, pk):
    """Reschedule a job appointment"""
    job_appointment = get_object_or_404(Appointment, pk=pk)
    
    if job_appointment.appointment_type != 'job_appointment':
        messages.error(request, 'This is not a job appointment')
        return redirect('appointment_detail', pk=job_appointment.pk)
    
    if request.method == 'POST':
        try:
            # Get new datetime
            job_date = request.POST.get('job_date')
            job_time = request.POST.get('job_time')
            
            job_datetime_str = f"{job_date} {job_time}"
            new_datetime = datetime.strptime(job_datetime_str, '%Y-%m-%d %H:%M')
            
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            new_datetime = sa_timezone.localize(new_datetime)
            
            # Check availability (excluding current appointment)
            is_available = check_job_availability(
                new_datetime, 
                job_appointment.job_duration_hours,
                exclude_appointment_id=job_appointment.id
            )
            
            if not is_available:
                messages.error(request, 'Selected time slot is not available')
                return render(request, 'bot/pages/reschedule_job.html', {'job_appointment': job_appointment})
            
            # Update appointment
            old_datetime = job_appointment.job_scheduled_datetime
            job_appointment.job_scheduled_datetime = new_datetime
            job_appointment.save()
            
            # Send notifications
            send_job_reschedule_notification(job_appointment, old_datetime, new_datetime)
            
            messages.success(request, f'Job rescheduled to {new_datetime.strftime("%B %d, %Y at %I:%M %p")}')
            return redirect('appointment_detail', pk=job_appointment.pk)
            
        except Exception as e:
            messages.error(request, f'Error rescheduling job: {str(e)}')
    
    return render(request, 'bot/pages/reschedule_job.html', {
        'job_appointment': job_appointment
    })


def send_job_reschedule_notification(job_appointment, old_datetime, new_datetime):
    """Send notification about job reschedule"""
    try:
        old_date_str = old_datetime.strftime('%A, %B %d at %I:%M %p')
        new_date_str = new_datetime.strftime('%A, %B %d at %I:%M %p')
        
        message = f"""📅 JOB RESCHEDULED

Hi {job_appointment.customer_name},

Your plumbing job has been rescheduled:

❌ Previous: {old_date_str}
✅ New: {new_date_str}

📍 Location: {job_appointment.customer_area}
🔨 Work: {job_appointment.job_description or job_appointment.project_type}

Our plumber will contact you before the new appointment time.

Questions? Reply to this message.

- Plumbing Team"""
        
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=job_appointment.phone_number
        )
        
    except Exception as e:
        print(f"Error sending reschedule notification: {str(e)}")


@staff_required
def job_appointments_list(request):
    """List all job appointments"""
    # Get all job appointments
    job_appointments = Appointment.objects.real().filter(
        appointment_type='job'
    ).order_by('-scheduled_datetime')
    
    # Calculate statistics (keyed on job_status — the field the row badges
    # display — not the general appointment `status`, which is a different
    # field and previously made these counts disagree with what was shown).
    total_jobs = job_appointments.count()
    scheduled_jobs = job_appointments.filter(job_status='scheduled').count()
    in_progress_jobs = job_appointments.filter(job_status='in_progress').count()
    completed_jobs = job_appointments.filter(job_status='completed').count()
    
    # Filter by status if provided
    status_filter = request.GET.get('status')
    if status_filter:
        job_appointments = job_appointments.filter(status=status_filter)
    
    # Filter by plumber if provided
    plumber_filter = request.GET.get('plumber')
    if plumber_filter:
        job_appointments = job_appointments.filter(assigned_plumber_id=plumber_filter)
    
    # Filter by date if provided
    date_filter = request.GET.get('date')
    if date_filter:
        job_appointments = job_appointments.filter(scheduled_datetime__date=date_filter)
    
    context = {
        'job_appointments': job_appointments,
  #      'plumbers': User.objects.filter(is_staff=True),  # Adjust based on your user model
        'status_choices': ['scheduled', 'in_progress', 'completed', 'cancelled'],
        'selected_status': status_filter,
        'selected_plumber': plumber_filter,
        'selected_date': date_filter,
        'total_jobs': total_jobs,
        'scheduled_jobs': scheduled_jobs,
        'in_progress_jobs': in_progress_jobs,
        'completed_jobs': completed_jobs,
    }
    
    return render(request, 'bot/pages/job_appointments_list.html', context)
