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
from ..whatsapp_cloud_api import get_client_for_tenant, whatsapp_api
from ..services.clients import (
    deepseek_client, GOOGLE_CALENDAR_CREDENTIALS, DEEPSEEK_API_KEY,
)
from ..utils import (
    _to_decimal, _to_float, _safe_logo_url, _safe_logo_data_uri,
    _reset_pk_sequence, _append_admin_note,
    clean_phone_number, format_phone_number_for_storage,
)
# Used by send_job_appointment_notifications — it was called there without
# ever being imported, so the plumber's job email died on a NameError that the
# function's own blanket except swallowed.
from ..plumber_notifications import send_plumber_notification_email

logger = logging.getLogger(__name__)


def _detail_url(request, pk):
    """appointment_detail URL that stays inside the conversations workspace
    iframe when the caller was in it — `frame=1` and `source` carried through,
    so a redirect from a job screen doesn't break out into the full page."""
    url = reverse('appointment_detail', args=[pk])
    params = {}
    if request.GET.get('frame') == '1':
        params['frame'] = '1'
    source = request.GET.get('source')
    if source:
        params['source'] = source
    if params:
        from urllib.parse import urlencode
        url += '?' + urlencode(params)
    return url


@staff_required
def schedule_job(request, pk):
    """Schedule job appointment after site visit"""
    site_visit = get_object_or_404(Appointment.objects.for_tenant_or_seed(getattr(request, 'tenant', None)), pk=pk)

    # Gate the screen on the SAME question the "Schedule Job" button asks
    # (`can_schedule_job`): a site visit that has been logged as complete.
    # It used to also demand `status == 'confirmed'`, which the button never
    # checks — so a visit logged through the banner on a lead the bot never
    # formally confirmed (status still 'pending') showed the button, then got
    # bounced straight back here with an error the moment it was pressed. That
    # is the "nothing happens, the page just refreshes" report: a real visit
    # that could not be turned into a job. Whether the LEAD was confirmed says
    # nothing about whether the VISIT happened, and plenty of real visits are
    # logged manually without the bot ever pinning a slot.
    if site_visit.appointment_type != 'site_visit':
        messages.error(request, 'Cannot schedule a job for this appointment')
        return redirect(_detail_url(request, site_visit.pk))

    if not site_visit.site_visit_completed:
        messages.error(request, 'Mark the site visit complete before scheduling the job')
        return redirect(_detail_url(request, site_visit.pk))

    # This screen is opened inside the conversations workspace iframe, which
    # already carries the app chrome. `frame=1` (carried through from the
    # detail link) renders the chromeless panel layout instead, so the page
    # doesn't stack a SECOND nav bar inside the frame.
    is_frame = request.GET.get('frame') == '1'
    _page_ctx = {
        'site_visit': site_visit,
        'today': timezone.localdate(),
        'base_template': 'bot/layouts/panel.html' if is_frame else 'bot/layouts/base.html',
        'is_frame': is_frame,
    }

    if request.method == 'POST':
        try:
            # Get form data
            job_date = request.POST.get('job_date')
            job_time = request.POST.get('job_time')
            duration_hours = int(request.POST.get('duration_hours', 4))
            job_description = request.POST.get('job_description', '')
            materials_needed = request.POST.get('materials_needed', '')
            # Some jobs run over several days. The plumber ticks "multiple days"
            # and gives the number of days (2+); a single-day job leaves it off
            # and behaves exactly as before.
            multi_day, job_days = _parse_job_days(request)

            # Validate required fields
            if not job_date or not job_time:
                messages.error(request, 'Please provide both date and time')
                return render(request, 'bot/pages/schedule_job.html', _page_ctx)

            # Parse datetime
            job_datetime_str = f"{job_date} {job_time}"
            job_datetime = datetime.strptime(job_datetime_str, '%Y-%m-%d %H:%M')

            # Localize to South Africa timezone
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            job_datetime = sa_timezone.localize(job_datetime)

            # One validator for every job-scheduling path: future, open day,
            # inside hours, fits the day (single) and within the crew's limit.
            _cfg = site_visit._schedule_cfg()
            job_end, error = _validate_job_slot(
                _cfg, job_datetime, duration_hours, job_days,
                appointment=site_visit, exclude_id=site_visit.id)
            if error:
                messages.error(request, error)
                return render(request, 'bot/pages/schedule_job.html', _page_ctx)

            # This lead BECOMES the job appointment (see
            # Appointment.schedule_job_appointment). It used to be written with
            # `Appointment.objects.update(...)` — a manager-level update with no
            # filter, which stamped this job onto every lead in every tenant and
            # flipped them all to VERY_HOT. The manager now refuses that call.
            try:
                job_appointment = site_visit.schedule_job_appointment(
                    job_datetime,
                    duration_hours=duration_hours,
                    description=job_description,
                    materials=materials_needed,
                    end_datetime=job_end if job_days > 1 else None,
                )
            except ValueError:
                messages.error(request, 'Mark the site visit complete before scheduling the job')
                return redirect(_detail_url(request, site_visit.pk))

            # Send notifications
            try:
                send_job_appointment_notifications(job_appointment)
            except Exception as notify_error:
                print(f"WARNING Notification error: {notify_error}")

            if job_days > 1:
                _msg = (f'Multi-day job scheduled from '
                        f'{job_datetime.strftime("%B %d")} to '
                        f'{job_end.strftime("%B %d, %Y")} '
                        f'(starting {job_datetime.strftime("%I:%M %p")})')
            else:
                _msg = f'Job scheduled for {job_datetime.strftime("%B %d, %Y at %I:%M %p")}'
            messages.success(request, _msg)
            return redirect(_detail_url(request, job_appointment.pk))
            
        except ValueError as e:
            messages.error(request, f'Invalid date/time format: {str(e)}')
        except Exception as e:
            messages.error(request, f'Error scheduling job: {str(e)}')
            print(f"❌ Schedule job error: {str(e)}")

    return render(request, 'bot/pages/schedule_job.html', _page_ctx)


@require_POST
@staff_required
def update_job_status(request, pk):
    """Update job appointment status"""
    job_appointment = get_object_or_404(Appointment.objects.for_tenant_or_seed(getattr(request, 'tenant', None)), pk=pk)
    
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


def check_job_availability(job_datetime, duration_hours, exclude_appointment_id=None,
                           appointment=None, job_end=None):
    """Check if job time slot is available.

    `appointment` supplies the tenant whose diary and working week we check
    against (it used to reach for a `request` that isn't in scope here, so every
    call raised NameError and reported 'not available').

    `job_end` is the real end of a multi-day job; without it the job is a
    single-day one ending `duration_hours` after it starts."""
    try:
        # Calculate job end time — a multi-day job carries its own end.
        job_end_time = job_end or (job_datetime + timedelta(hours=duration_hours))

        tenant = getattr(appointment, 'tenant', None)

        # Check for overlapping job appointments (this tenant's diary only)
        overlapping_jobs = Appointment.objects.for_tenant_or_seed(tenant).filter(
            appointment_type='job_appointment',
            job_status__in=['scheduled', 'in_progress'],
            job_scheduled_datetime__isnull=False,
        )
        
        if exclude_appointment_id:
            overlapping_jobs = overlapping_jobs.exclude(id=exclude_appointment_id)

        from ..tenant_config import get_config
        cfg = get_config(tenant)

        # How many jobs this business can run at the same time (default 1).
        # Count the jobs that actually overlap the requested window and only
        # refuse once the crew would be over that limit — so a firm that has
        # said it can take, say, three concurrent jobs can stack up to three.
        max_jobs = cfg.max_concurrent_jobs()
        overlap_count = 0
        for job in overlapping_jobs:
            existing_end = job.job_end() or job.job_scheduled_datetime
            if (job_datetime < existing_end and job_end_time > job.job_scheduled_datetime):
                overlap_count += 1
                if overlap_count >= max_jobs:
                    return False

        # Check business days/hours against the tenant's own schedule
        if not cfg.is_open_on(job_datetime.weekday()):
            return False

        if job_datetime.hour < cfg.open_hour() or job_end_time.hour > cfg.close_hour():
            return False
        
        # Check if it's not in the past
        if job_datetime <= timezone.now():
            return False
        
        return True
        
    except Exception as e:
        print(f"Error checking job availability: {str(e)}")
        return False


def _validate_job_slot(cfg, job_datetime, duration_hours, job_days,
                       appointment, exclude_id):
    """The one validator every job-scheduling path shares: future, on an open
    day, inside hours, fits the working day (single-day) and within the crew's
    concurrent-job limit.

    Returns ``(job_end, None)`` when the slot is good — ``job_end`` is the real
    end of the job (a multi-day job's last day at closing, else start + hours) —
    or ``(None, message)`` with a plumber-facing reason when it is not."""
    if job_datetime <= timezone.now():
        return None, 'Job time must be in the future'

    if not cfg.is_open_on(job_datetime.weekday()):
        phrase = cfg.closed_days_phrase() or 'that day'
        return None, f'Jobs cannot be scheduled on {phrase} — the business is closed'

    if job_datetime.hour < cfg.open_hour() or job_datetime.hour >= cfg.close_hour():
        return None, f'Jobs must be scheduled between {cfg.open_hour()}:00 and {cfg.close_hour()}:00'

    if job_days > 1:
        end_day = job_datetime + timedelta(days=job_days - 1)
        job_end = end_day.replace(hour=cfg.close_hour(), minute=0, second=0, microsecond=0)
    else:
        job_end = job_datetime + timedelta(hours=duration_hours)
        close_boundary = job_datetime.replace(
            hour=cfg.close_hour(), minute=0, second=0, microsecond=0)
        if job_end > close_boundary:
            return None, (f'A {duration_hours}-hour job from that time would run past '
                          f'closing ({cfg.close_hour()}:00). Start earlier, shorten it, '
                          f'or mark it a multi-day job')

    if not check_job_availability(
        job_datetime, duration_hours, exclude_appointment_id=exclude_id,
        appointment=appointment, job_end=job_end if job_days > 1 else None,
    ):
        cap = cfg.max_concurrent_jobs()
        clash = (f'The business is already at its limit of {cap} jobs at that '
                 f'time. Pick another slot') if cap > 1 else \
            'That time clashes with a job already booked. Pick another slot'
        return None, clash

    return job_end, None


def _parse_job_days(request):
    """(multi_day, job_days) from a schedule/create-job POST. A single-day job
    is 1 whatever number is in the field; multi-day is at least 2."""
    if not request.POST.get('multi_day'):
        return False, 1
    try:
        days = max(2, int(request.POST.get('job_days') or 2))
    except (TypeError, ValueError):
        days = 2
    return True, days


def send_job_appointment_notifications(job_appointment):
    """Send notifications about new job appointment - UPDATED"""
    try:
        job_date = job_appointment.job_scheduled_datetime.strftime('%A, %B %d, %Y')
        job_time = job_appointment.job_scheduled_datetime.strftime('%I:%M %p')
        # A multi-day job reports its span; a single-day one its hours.
        if job_appointment.is_multiday_job():
            date_line = f"📅 Dates: {job_appointment.job_span_label()}"
            duration_line = "⏱️ Duration: several days (we'll work through until it's done)"
        else:
            date_line = f"📅 Date: {job_date}"
            duration_line = f"⏱️ Duration: {job_appointment.job_duration_hours} hours"

        # Customer notification
        customer_message = f"""🔧 JOB APPOINTMENT SCHEDULED

Hi {job_appointment.customer_name or 'Customer'},

Your plumbing job has been scheduled:

{date_line}
🕐 Start time: {job_time}
{duration_line}
📍 Location: {job_appointment.customer_area}
🔨 Work: {job_appointment.job_description or job_appointment.project_type}

We will contact you before arrival.

{f"Materials needed: {job_appointment.job_materials_needed}" if job_appointment.job_materials_needed else ""}

Questions? Reply to this message.

- Plumbing Team"""
        
        # Send to customer
        clean_phone = clean_phone_number(job_appointment.phone_number)
        get_client_for_tenant(job_appointment.tenant).send_text_message(clean_phone, customer_message)
        
        # Team notification
        plumber_name = job_appointment.assigned_plumber.get_full_name() if job_appointment.assigned_plumber else "Unassigned"
        
        team_message = f"""👷 NEW JOB SCHEDULED

Customer: {job_appointment.customer_name}
Phone: {job_appointment.phone_number.replace('whatsapp:', '')}
Date/Time: {job_date} at {job_time}
Duration: {job_appointment.job_span_label() if job_appointment.is_multiday_job() else f"{job_appointment.job_duration_hours} hours"}
Location: {job_appointment.customer_area}
Assigned to: {plumber_name}

Job Description:
{job_appointment.job_description or job_appointment.project_type}

{f"Materials: {job_appointment.job_materials_needed}" if job_appointment.job_materials_needed else ""}

View details: http://127.0.0.1:8000/appointments/{job_appointment.id}/"""
        
        # The team is contacted by EMAIL, always (owner rule, 2026-09-05).
        # This also drops a hardcoded number that sent EVERY tenant's jobs to
        # Homebase's plumber, which is the leak CLAUDE.md warns about: no
        # Homebase value may reach another tenant.

        send_plumber_notification_email(
            subject=f"New job scheduled for {job_appointment.customer_name or 'customer'}",
            message=team_message,
            tenant=getattr(job_appointment, 'tenant', None),
        )
        
    except Exception as e:
        print(f"Error sending job appointment notifications: {str(e)}")


def _notify_customer(job_appointment, message):
    """Send a customer WhatsApp message as the job's OWN tenant.

    Replaces the shared Twilio sender: every tenant's customers used to be
    messaged from one number that was not the business they had dealt with.
    """
    from ..whatsapp_cloud_api import get_client_for_tenant
    clean = (job_appointment.phone_number or '').replace('whatsapp:', '').replace('+', '').strip()
    if not clean:
        return False
    get_client_for_tenant(getattr(job_appointment, 'tenant', None)).send_text_message(clean, message)
    return True


def send_job_status_update_notification(job_appointment, new_status):
    """Send notification when job status changes"""
    try:
        status_messages = {
            'in_progress': f"Your plumbing job at {job_appointment.customer_area} has started. Our plumber is on-site working on your {job_appointment.project_type}.",
            'completed': f"Your plumbing job at {job_appointment.customer_area} is complete. Thank you for choosing us. Any questions, just reply here.",
            'cancelled': f"Your plumbing job booked for {job_appointment.job_scheduled_datetime.strftime('%B %d, %Y')} has been cancelled. We'll be in touch to reschedule.",
        }

        if new_status in status_messages:
            _notify_customer(job_appointment, status_messages[new_status])
            
    except Exception as e:
        print(f"Error sending status update: {str(e)}")


@staff_required 
def reschedule_job(request, pk):
    """Reschedule a job appointment"""
    job_appointment = get_object_or_404(Appointment.objects.for_tenant_or_seed(getattr(request, 'tenant', None)), pk=pk)
    
    if job_appointment.appointment_type != 'job_appointment':
        messages.error(request, 'This is not a job appointment')
        return redirect(_detail_url(request, job_appointment.pk))

    # Chromeless panel when opened inside the workspace iframe (see schedule_job).
    is_frame = request.GET.get('frame') == '1'
    _page_ctx = {
        'job_appointment': job_appointment,
        'base_template': 'bot/layouts/panel.html' if is_frame else 'bot/layouts/base.html',
        'is_frame': is_frame,
    }

    if request.method == 'POST':
        try:
            # Get new datetime
            job_date = request.POST.get('job_date')
            job_time = request.POST.get('job_time')

            job_datetime_str = f"{job_date} {job_time}"
            new_datetime = datetime.strptime(job_datetime_str, '%Y-%m-%d %H:%M')

            sa_timezone = pytz.timezone('Africa/Johannesburg')
            new_datetime = sa_timezone.localize(new_datetime)

            # A multi-day job keeps its length when it moves — shift the end by
            # the same delta so a three-day job stays three days.
            old_datetime = job_appointment.job_scheduled_datetime
            new_end = None
            if job_appointment.is_multiday_job() and old_datetime:
                new_end = job_appointment.job_end_datetime + (new_datetime - old_datetime)

            # Check availability (excluding current appointment)
            is_available = check_job_availability(
                new_datetime,
                job_appointment.job_duration_hours,
                exclude_appointment_id=job_appointment.id,
                appointment=job_appointment,
                job_end=new_end,
            )

            if not is_available:
                messages.error(request, 'Selected time slot is not available')
                return render(request, 'bot/pages/reschedule_job.html', _page_ctx)

            # Update appointment
            job_appointment.job_scheduled_datetime = new_datetime
            if new_end is not None:
                job_appointment.job_end_datetime = new_end
            job_appointment.save()

            # Send notifications
            send_job_reschedule_notification(job_appointment, old_datetime, new_datetime)

            messages.success(request, f'Job rescheduled to {new_datetime.strftime("%B %d, %Y at %I:%M %p")}')
            return redirect(_detail_url(request, job_appointment.pk))

        except Exception as e:
            messages.error(request, f'Error rescheduling job: {str(e)}')

    return render(request, 'bot/pages/reschedule_job.html', _page_ctx)


def send_job_reschedule_notification(job_appointment, old_datetime, new_datetime):
    """Send notification about job reschedule"""
    try:
        old_date_str = old_datetime.strftime('%A, %B %d at %I:%M %p')
        new_date_str = new_datetime.strftime('%A, %B %d at %I:%M %p')
        
        from ..utils import business_name_for
        signature = business_name_for(job_appointment, default='Our team')
        work = job_appointment.job_description or job_appointment.project_type

        message = f"""Hi {job_appointment.customer_name}, your plumbing job has been rescheduled.

Previous: {old_date_str}
New: {new_date_str}

Location: {job_appointment.customer_area}
Work: {work}

Our plumber will contact you before the new appointment time. Any questions, just reply here.

{signature}"""

        _notify_customer(job_appointment, message)

    except Exception as e:
        print(f"Error sending reschedule notification: {str(e)}")


@staff_required
def job_appointments_list(request):
    """List all job appointments"""
    # Get all job appointments
    job_appointments = Appointment.objects.for_tenant_or_seed(getattr(request, 'tenant', None)).real().filter(
        appointment_type='job_appointment'
    ).order_by('-job_scheduled_datetime')
    
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
        job_appointments = job_appointments.filter(job_status=status_filter)
    
    # Filter by plumber if provided
    plumber_filter = request.GET.get('plumber')
    if plumber_filter:
        job_appointments = job_appointments.filter(assigned_plumber_id=plumber_filter)
    
    # Filter by date if provided
    date_filter = request.GET.get('date')
    if date_filter:
        job_appointments = job_appointments.filter(job_scheduled_datetime__date=date_filter)
    
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


@staff_required
def create_job(request):
    """Create a job directly from the Jobs tab.

    Not every job comes through the bot's site-visit flow: a walk-in, a phone
    booking or a repeat customer is booked straight in. This creates (or reuses,
    for a number already on file) the customer's row and schedules the job on
    it, going through the SAME `_validate_job_slot` every other path uses, so a
    directly-booked job respects the working week, the hours and the crew's
    concurrent-job limit exactly like a converted one."""
    from ..tenant_config import get_config
    tenant = getattr(request, 'tenant', None)
    cfg = get_config(tenant)
    page_ctx = {
        'today': timezone.localdate(),
        'open_hour': cfg.open_hour(),
        'close_hour': cfg.close_hour(),
        # Echo the submitted values back on an error so nothing is retyped.
        'form': {},
    }

    if request.method == 'POST':
        page_ctx['form'] = request.POST
        name = (request.POST.get('customer_name') or '').strip()
        phone_raw = (request.POST.get('phone_number') or '').strip()
        area = (request.POST.get('customer_area') or '').strip()
        description = (request.POST.get('job_description') or '').strip()
        materials = (request.POST.get('materials_needed') or '').strip()
        job_date = request.POST.get('job_date')
        job_time = request.POST.get('job_time')
        try:
            duration_hours = int(request.POST.get('duration_hours') or 4)
        except (TypeError, ValueError):
            duration_hours = 4
        multi_day, job_days = _parse_job_days(request)

        digits = re.sub(r'\D', '', phone_raw)
        if not name or not digits or not job_date or not job_time:
            messages.error(request, 'Enter at least a customer name, phone number, date and time.')
            return render(request, 'bot/pages/create_job.html', page_ctx)

        try:
            job_datetime = pytz.timezone('Africa/Johannesburg').localize(
                datetime.strptime(f"{job_date} {job_time}", '%Y-%m-%d %H:%M'))
        except ValueError:
            messages.error(request, 'That date or time is not valid.')
            return render(request, 'bot/pages/create_job.html', page_ctx)

        # The row we will book onto — found by number (so a repeat customer is
        # not duplicated) or created. It supplies the tenant for the overlap
        # check, so validate AFTER we have it.
        phone_stored = format_phone_number_for_storage(digits)
        lead, _created = Appointment.objects.get_or_create_lead(
            phone_stored, tenant=tenant,
            defaults={'status': 'pending', 'customer_name': name})

        job_end, error = _validate_job_slot(
            cfg, job_datetime, duration_hours, job_days,
            appointment=lead, exclude_id=lead.id)
        if error:
            messages.error(request, error)
            return render(request, 'bot/pages/create_job.html', page_ctx)

        # Book the job onto the row. A directly-created job needs no prior site
        # visit, so we write the fields rather than going through
        # `schedule_job_appointment` (which gates on a completed visit).
        if name:
            lead.customer_name = name
        if area:
            lead.customer_area = area
        if description:
            lead.job_description = description
        if materials:
            lead.job_materials_needed = materials
        lead.appointment_type = 'job_appointment'
        lead.job_scheduled_datetime = job_datetime
        lead.job_duration_hours = duration_hours
        lead.job_end_datetime = job_end if multi_day else None
        lead.job_status = 'scheduled'
        lead.status = 'confirmed'
        lead.save()

        try:
            send_job_appointment_notifications(lead)
        except Exception as notify_error:
            print(f"WARNING Notification error: {notify_error}")

        if multi_day:
            msg = (f'Job created for {name} from {job_datetime.strftime("%B %d")} '
                   f'to {job_end.strftime("%B %d, %Y")}')
        else:
            msg = f'Job created for {name} on {job_datetime.strftime("%B %d, %Y at %I:%M %p")}'
        messages.success(request, msg)
        return redirect('appointment_detail', pk=lead.pk)

    return render(request, 'bot/pages/create_job.html', page_ctx)
