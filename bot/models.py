from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from datetime import timedelta, datetime
import pytz
import json
import re
import uuid
from decimal import Decimal



class Appointment(models.Model):
    """Model to store plumbing appointment information and conversation history"""
    
    # Status choices
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('confirmed', 'Confirmed'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('no_show', 'No Show'),
    ]
    
    # Property type choices
    PROPERTY_TYPE_CHOICES = [
        ('house', 'House'),
        ('business', 'Business'),
        ('apartment', 'Apartment'),
        ('condo', 'Condominium'),
        ('townhouse', 'Townhouse'),
        ('commercial', 'Commercial'),
        ('office', 'Office'),
        ('other', 'Other'),
    ]
    
    # Project type choices (updated to match service list)
    PROJECT_TYPE_CHOICES = [
        ('bathroom_renovation', 'Bathroom Renovation'),
        ('new_plumbing_installation', 'New Plumbing Installation'),
        ('kitchen_renovation', 'Kitchen Renovation'),
        ('other', 'Other'),
    ]
    
    # House stage choices
    HOUSE_STAGE_CHOICES = [
        ('foundation', 'Foundation'),
        ('framing', 'Framing'),
        ('roof_level', 'Roof Level'),
        ('rough_plumbing', 'Rough Plumbing'),
        ('electrical', 'Electrical'),
        ('insulation', 'Insulation'),
        ('drywall', 'Drywall'),
        ('not_plastered', 'Not Plastered'),
        ('plastered', 'Plastered'),
        ('painted', 'Painted'),
        ('finished', 'Finished'),
        ('occupied', 'Occupied'),
    ]
    
    # NEW: Appointment type choices for job scheduling
    APPOINTMENT_TYPE_CHOICES = [
        ('site_visit', 'Site Visit'),
        ('job_appointment', 'Job Appointment'),
    ]
    
    # NEW: Job status choices for job scheduling
    JOB_STATUS_CHOICES = [
        ('not_applicable', 'Not Applicable'),
        ('pending_schedule', 'Pending Schedule'),
        ('scheduled', 'Scheduled'),
        ('in_progress', 'In Progress'), 
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    # Basic Information
    phone_number = models.CharField(max_length=50, unique=True, help_text="Customer's WhatsApp number")
    customer_name = models.CharField(max_length=100, blank=True, null=True, help_text="Customer's full name")
    customer_email = models.EmailField(blank=True, null=True, help_text="Customer's email address")
    has_plan = models.BooleanField(default=False, help_text="Customer has existing plans")
    site_visit = models.BooleanField(default=False, help_text="Customer requested site visit")
    
    # Appointment Details
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    scheduled_datetime = models.DateTimeField(blank=True, null=True, help_text="Scheduled appointment date and time")
    end_datetime = models.DateTimeField(blank=True, null=True, help_text="Appointment end date and time")
    duration = models.DurationField(default=timedelta(hours=2), help_text="Appointment duration")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Project Information
    project_type = models.CharField(max_length=50,  blank=True, null=True)
    project_description = models.TextField(blank=True, null=True, help_text="Detailed description of the project")
    customer_area = models.CharField(max_length=100, blank=True, null=True, help_text="Service area (e.g. Harare Hatfield)")
    property_type = models.CharField(max_length=20, choices=PROPERTY_TYPE_CHOICES, blank=True, null=True)
    house_stage = models.CharField(max_length=20, blank=True, null=True)
    
    # Budget and Timeline
    budget = models.CharField(max_length=100, blank=True, null=True, help_text="Customer's budget range")
    timeline = models.CharField(max_length=100, blank=True, null=True, help_text="Customer's preferred timeline")
    needs_estimate = models.BooleanField(default=False, help_text="Customer requested written estimate")

    # Reminders
    reminder_1_day_sent = models.BooleanField(default=False, help_text="1-day reminder sent")
    reminder_morning_sent = models.BooleanField(default=False, help_text="Morning reminder sent")
    reminder_2_hours_sent = models.BooleanField(default=False, help_text="2-hour reminder sent")

    # Conversation and Notes
    conversation_history = models.JSONField(default=list, blank=True, help_text="WhatsApp conversation history")
    internal_notes = models.TextField(blank=True, null=True, help_text="Internal team notes")
    
    # External Integration IDs
    google_calendar_event_id = models.CharField(max_length=200, blank=True, null=True)
    twilio_conversation_sid = models.CharField(max_length=100, blank=True, null=True)
    
    # Additional Information
    is_emergency = models.BooleanField(default=False)
    customer_rating = models.IntegerField(blank=True, null=True, help_text="Customer rating 1-5")
    completion_notes = models.TextField(blank=True, null=True, help_text="Notes after job completion")

    # Plan upload fields
    plan_file = models.FileField(
        upload_to='customer_plans/', 
        null=True, 
        blank=True,
        help_text="Customer's uploaded plan document"
    )
    
    plan_status = models.CharField(
        max_length=50, 
        choices=[
            ('pending_upload', 'Pending Plan Upload'),
            ('plan_uploaded', 'Plan Uploaded'),
            ('plan_reviewed', 'Plan Reviewed by Plumber'),
            ('ready_to_book', 'Ready to Book'),
        ], 
        null=True, 
        blank=True,
        help_text="Current status of plan processing"
    )
    
    plumber_contact_number = models.CharField(
        max_length=20, 
        default='+27610318200',
        help_text="Direct contact number for the assigned plumber"
    )
    
    plan_notes = models.TextField(
        null=True, 
        blank=True,
        help_text="Notes about the uploaded plan from plumber review"
    )
    
    plan_uploaded_at = models.DateTimeField(
        null=True, 
        blank=True,
        help_text="Timestamp when plan was uploaded"
    )
    
    plumber_contacted_at = models.DateTimeField(
        null=True, 
        blank=True,
        help_text="Timestamp when plumber was notified about plan"
    )

    # NEW: Job scheduling fields
    appointment_type = models.CharField(
        max_length=20,
        choices=APPOINTMENT_TYPE_CHOICES,
        default='site_visit',
        help_text="Type of appointment: site visit or job appointment"
    )
    
    # Site visit tracking fields
    site_visit_completed = models.BooleanField(
        default=False,
        help_text="Whether the site visit has been completed"
    )
    site_visit_completed_at = models.DateTimeField(
        null=True, 
        blank=True,
        help_text="When the site visit was marked as completed"
    )
    site_visit_notes = models.TextField(
        blank=True,
        help_text="Notes from the completed site visit"
    )
    plumber_assessment = models.TextField(
        blank=True,
        help_text="Plumber's assessment after site visit"
    )
    
    # Job appointment fields
    parent_site_visit = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='job_appointments',
        help_text="The parent site visit that led to this job appointment"
    )
    
    job_status = models.CharField(
        max_length=20,
        choices=JOB_STATUS_CHOICES,
        default='not_applicable',
        help_text="Status of the job appointment"
    )
    
    job_scheduled_datetime = models.DateTimeField(
        null=True, 
        blank=True,
        help_text="Scheduled date and time for the job appointment"
    )
    job_duration_hours = models.IntegerField(
        default=4,
        help_text="Duration of the job appointment in hours"
    )
    job_description = models.TextField(
        blank=True,
        help_text="Detailed description of the job work to be performed"
    )
    job_materials_needed = models.TextField(
        blank=True,
        help_text="List of materials and tools needed for the job"
    )
    job_completed_at = models.DateTimeField(
        null=True, 
        blank=True,
        help_text="When the job was marked as completed"
    )
    
    # Plumber assignment
    assigned_plumber = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_appointments',
        help_text="Plumber assigned to this job appointment"
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Appointment"
        verbose_name_plural = "Appointments"
        indexes = [
            models.Index(fields=['phone_number']),
            models.Index(fields=['status']),
            models.Index(fields=['scheduled_datetime']),
            models.Index(fields=['created_at']),
            models.Index(fields=['appointment_type']),
            models.Index(fields=['job_status']),
            models.Index(fields=['job_scheduled_datetime']),
        ]

    def save(self, *args, **kwargs):
        """Auto-calculate end_datetime when scheduled_datetime is set"""
        if self.scheduled_datetime and not self.end_datetime:
            self.end_datetime = self.scheduled_datetime + self.duration
        super().save(*args, **kwargs)

    # NEW: Job scheduling methods
    def mark_site_visit_completed(self, notes="", assessment=""):
        """Mark site visit as completed"""
        self.site_visit_completed = True
        self.site_visit_completed_at = timezone.now()
        self.site_visit_notes = notes
        self.plumber_assessment = assessment
        
        # Update job status if this was a site visit
        if self.appointment_type == 'site_visit':
            self.job_status = 'pending_schedule'
        
        self.save()
    
    def can_schedule_job(self):
        """Check if job appointment can be scheduled"""
        return (
            self.appointment_type == 'site_visit' and
            self.site_visit_completed and
            self.job_status == 'pending_schedule'
        )
    
    def create_job_appointment(self, job_datetime, duration_hours=4, 
                             description="", materials="", plumber=None):
        """Create a job appointment after site visit"""
        if not self.can_schedule_job():
            raise ValueError("Cannot schedule job - site visit not completed")

   #     job_phone = f"{self.phone_number}_job_{timezone.now().strftime('%Y%m%d%H%M%S')}"
    


        job_appointment = Appointment.objects.create(
    #       phone_number=self.job_phone,  # Use unique phone number,
            customer_name=self.customer_name,
            customer_area=self.customer_area,
            project_type=self.project_type,
            property_type=self.property_type,
            appointment_type='job_appointment',
            parent_site_visit=self,
            job_scheduled_datetime=job_datetime,
            job_duration_hours=duration_hours,
            job_description=description,
            job_materials_needed=materials,
            assigned_plumber=plumber,
            job_status='scheduled',
            status='confirmed'
        )
        
        # Update parent site visit status
        self.job_status = 'scheduled'
        self.save()
        
        return job_appointment
    
    def get_job_appointments(self):
        """Get all job appointments for this site visit"""
        return self.job_appointments.all()
    
    def has_pending_job_schedule(self):
        """Check if job scheduling is pending"""
        return self.job_status == 'pending_schedule'

    # Plan status methods
    def get_plan_status_display_friendly(self):
        """Get user-friendly plan status"""
        status_map = {
            'pending_upload': 'Waiting for plan upload',
            'plan_uploaded': 'Plan sent to plumber',
            'plan_reviewed': 'Plan reviewed by plumber',
            'ready_to_book': 'Ready for appointment booking',
        }
        return status_map.get(self.plan_status, 'No plan required')

    def is_plan_required(self):
        """Check if this appointment requires a plan"""
        return self.has_plan is True

    def can_book_directly(self):
        """Check if appointment can be booked directly (no plan required)"""
        if self.has_plan is False:
            return True
        elif self.has_plan is True and self.plan_status == 'plan_reviewed':
            return True
        return False

    # ===== AVAILABILITY CHECKING METHODS =====
    
    def check_appointment_availability(self, requested_datetime):
        """Check if the requested datetime is available (no conflicts)"""
        try:
            # Convert to timezone-aware datetime if needed
            if requested_datetime.tzinfo is None:
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                requested_datetime = sa_timezone.localize(requested_datetime)
            
            # Define appointment duration (default 2 hours)
            appointment_duration = timedelta(hours=2)
            requested_end = requested_datetime + appointment_duration
            
            print(f"Checking availability for: {requested_datetime} to {requested_end}")
            
            # 1. Check if it's not in the past
            now = timezone.now()
            if requested_datetime <= now:
                print(f"Requested time is in the past: {requested_datetime} vs {now}")
                return False, "past_time"
            
            # 2. Check business hours (8 AM - 6 PM, Monday to Friday)
            weekday = requested_datetime.weekday()  # 0=Monday, 6=Sunday
            hour = requested_datetime.hour
            
            # Check if it's a weekend
            if weekday >= 5:  # Saturday=5, Sunday=6
                print(f"Requested time is on weekend: weekday {weekday}")
                return False, "weekend"
            
            # Check business hours
            if hour < 8 or hour >= 18:
                print(f"Outside business hours: {hour}:00 (business hours: 8 AM - 6 PM)")
                return False, "outside_business_hours"
            
            # Check if appointment would end after business hours
            if requested_end.hour > 18:
                print(f"Appointment would end after business hours: {requested_end}")
                return False, "ends_after_hours"
            
            # 3. Check for conflicts with other confirmed appointments
            conflicting_appointments = Appointment.objects.filter(
                status='confirmed',
                scheduled_datetime__isnull=False
            ).exclude(
                id=self.id  # Exclude current appointment for reschedules
            )
            
            for existing_appt in conflicting_appointments:
                if existing_appt.scheduled_datetime.tzinfo is None:
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    existing_start = sa_timezone.localize(existing_appt.scheduled_datetime)
                else:
                    existing_start = existing_appt.scheduled_datetime
                    
                existing_end = existing_start + appointment_duration
                
                # Check for time overlap
                # Appointments overlap if: start1 < end2 AND start2 < end1
                if (requested_datetime < existing_end and requested_end > existing_start):
                    print(f"Conflict found with appointment {existing_appt.id}")
                    print(f"Existing: {existing_start} to {existing_end}")
                    print(f"Requested: {requested_datetime} to {requested_end}")
                    return False, existing_appt
            
            # 4. Check for minimum advance booking (optional - at least 2 hours notice)
            min_advance_time = now + timedelta(hours=2)
            if requested_datetime < min_advance_time:
                print(f"Too short notice: {requested_datetime} vs minimum {min_advance_time}")
                return False, "insufficient_notice"
            
            # 5. Check for reasonable booking window (optional - not more than 3 months ahead)
            max_advance_time = now + timedelta(days=90)
            if requested_datetime > max_advance_time:
                print(f"Too far in advance: {requested_datetime} vs maximum {max_advance_time}")
                return False, "too_far_ahead"
            
            print(f"Time slot is available: {requested_datetime}")
            return True, None
            
        except Exception as e:
            print(f"Error checking availability: {str(e)}")
            return False, "error"

    def get_availability_error_message(self, error_type, conflict_appointment=None):
        """Generate user-friendly error messages for availability issues"""
        try:
            if error_type == "past_time":
                return "That time has already passed. Please choose a future time."
            
            elif error_type == "weekend":
                return "We're closed on weekends. Please choose Monday through Friday."
            
            elif error_type == "outside_business_hours":
                return "We're only available 8 AM to 6 PM, Monday through Friday. Please choose a time within business hours."
            
            elif error_type == "ends_after_hours":
                return "That appointment would run past our closing time (6 PM). Please choose an earlier time slot."
            
            elif error_type == "insufficient_notice":
                return "We need at least 2 hours advance notice for appointments. Please choose a time further in the future."
            
            elif error_type == "too_far_ahead":
                return "We can only book appointments up to 3 months in advance. Please choose a sooner date."
            
            elif error_type == "error":
                return "There was a technical issue checking availability. Please try a different time or call us."
            
            elif isinstance(conflict_appointment, Appointment):
                conflict_time = conflict_appointment.scheduled_datetime.strftime('%I:%M %p')
                customer_name = conflict_appointment.customer_name or "another customer"
                return f"That time conflicts with an appointment for {customer_name} at {conflict_time}."
            
            else:
                return "That time slot isn't available. Please choose a different time."
                
        except Exception as e:
            print(f"Error generating availability message: {str(e)}")
            return "That time isn't available. Please choose a different time."

    def find_next_available_slots(self, preferred_datetime, num_suggestions=4):
        """Find the next available appointment slots after the preferred time"""
        try:
            suggestions = []
            current_check = preferred_datetime
            max_days_ahead = 14  # Look up to 2 weeks ahead
            
            # Time slots to check (every 2 hours during business hours)
            business_hours = [8, 10, 12, 14, 16]  # 8am, 10am, 12pm, 2pm, 4pm
            
            days_checked = 0
            while len(suggestions) < num_suggestions and days_checked < max_days_ahead:
                check_date = current_check.date()
                
                # Skip weekends
                if check_date.weekday() < 5:  # Monday=0 to Friday=4
                    for hour in business_hours:
                        check_datetime = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        check_datetime = sa_timezone.localize(check_datetime)
                        
                        # Only check times after the preferred time
                        if check_datetime > preferred_datetime:
                            is_available, conflict = self.check_appointment_availability(check_datetime)
                            
                            if is_available:
                                suggestions.append({
                                    'datetime': check_datetime,
                                    'display': check_datetime.strftime('%A, %B %d at %I:%M %p'),
                                    'day_type': 'weekday'
                                })
                                
                                if len(suggestions) >= num_suggestions:
                                    break
                
                # Move to next day
                current_check += timedelta(days=1)
                days_checked += 1
            
            return suggestions
            
        except Exception as e:
            print(f"Error finding available slots: {str(e)}")
            return []

    def is_business_day(self, check_date):
        """Check if a given date is a business day (Monday-Friday)"""
        return check_date.weekday() < 5

    def is_business_hours(self, check_time):
        """Check if a given time is within business hours (8 AM - 6 PM)"""
        hour = check_time.hour
        return 8 <= hour < 18

    def get_business_day_name(self, date_obj):
        """Get user-friendly day name with business context"""
        weekday = date_obj.weekday()
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
        if weekday < 5:
            return day_names[weekday]
        else:
            return f"{day_names[weekday]} (Weekend - Closed)"

    def get_alternative_time_suggestions(self, requested_datetime):
        """Get alternative available time slots near the requested time"""
        try:
            suggestions = []
            
            # Get the requested date and time
            requested_date = requested_datetime.date()
            requested_hour = requested_datetime.hour
            
            # Time slots to suggest (business hours: 8, 10, 12, 14, 16)
            business_time_slots = [8, 10, 12, 14, 16]
            
            print(f"Looking for alternatives near {requested_datetime}")
            
            # 1. First try same day, different times
            if self.is_business_day(requested_date):
                for hour in business_time_slots:
                    # Skip the exact requested time
                    if hour == requested_hour:
                        continue
                        
                    candidate_time = datetime.combine(requested_date, datetime.min.time().replace(hour=hour))
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    candidate_datetime = sa_timezone.localize(candidate_time)
                    
                    # Only suggest future times
                    if candidate_datetime > timezone.now():
                        is_available, conflict = self.check_appointment_availability(candidate_datetime)
                        if is_available:
                            suggestions.append({
                                'datetime': candidate_datetime,
                                'display': candidate_datetime.strftime('%A, %B %d at %I:%M %p'),
                                'day_type': 'same_day',
                                'priority': 1  # Same day gets highest priority
                            })
            
            # 2. If we need more suggestions, try next business days
            if len(suggestions) < 4:
                days_to_check = 7  # Check next week
                for day_offset in range(1, days_to_check + 1):
                    check_date = requested_date + timedelta(days=day_offset)
                    
                    # Only check business days
                    if not self.is_business_day(check_date):
                        continue
                    
                    # Try to find slots on this day
                    for hour in business_time_slots:
                        candidate_time = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        candidate_datetime = sa_timezone.localize(candidate_time)
                        
                        is_available, conflict = self.check_appointment_availability(candidate_datetime)
                        if is_available:
                            suggestions.append({
                                'datetime': candidate_datetime,
                                'display': candidate_datetime.strftime('%A, %B %d at %I:%M %p'),
                                'day_type': 'next_days',
                                'priority': 2  # Next days get lower priority
                            })
                            
                            # Limit suggestions per day to avoid overwhelming
                            break
                    
                    # Stop if we have enough suggestions
                    if len(suggestions) >= 4:
                        break
            
            # 3. Sort suggestions by priority and time
            suggestions.sort(key=lambda x: (x['priority'], x['datetime']))
            
            # Return max 4 suggestions
            final_suggestions = suggestions[:4]
            
            print(f"Found {len(final_suggestions)} alternative time suggestions")
            for sugg in final_suggestions:
                print(f"  - {sugg['display']} ({sugg['day_type']})")
            
            return final_suggestions
            
        except Exception as e:
            print(f"Error getting alternative suggestions: {str(e)}")
            return []

    def format_availability_response(self, alternatives, requested_time_str=None):
        """Format alternative time suggestions into a user-friendly message"""
        try:
            if not alternatives:
                return "I'm having trouble finding available alternatives. Could you suggest a different day or time? Our hours are 8 AM - 6 PM, Monday to Friday."
            
            # Group by day type for better formatting
            same_day = [alt for alt in alternatives if alt['day_type'] == 'same_day']
            next_days = [alt for alt in alternatives if alt['day_type'] == 'next_days']
            
            message_parts = []
            
            if requested_time_str:
                message_parts.append(f"That time ({requested_time_str}) isn't available.")
            else:
                message_parts.append("That time isn't available.")
            
            message_parts.append("\nHere are some alternatives:")
            
            # Format same day options
            if same_day:
                message_parts.append("\n📅 Same day options:")
                for alt in same_day:
                    time_only = alt['datetime'].strftime('%I:%M %p')
                    message_parts.append(f"• {time_only}")
            
            # Format next days options  
            if next_days:
                message_parts.append("\n📅 Other days:")
                for alt in next_days:
                    message_parts.append(f"• {alt['display']}")
            
            message_parts.append("\nWhich time works best for you?")
            
            return "".join(message_parts)
            
        except Exception as e:
            print(f"Error formatting availability response: {str(e)}")
            return "That time isn't available. Please suggest another time."

    # ===== EXISTING MODEL METHODS =====
    
    def __str__(self):
        name = self.customer_name or 'Unknown Customer'
        if self.scheduled_datetime:
            return f"{name} - {self.scheduled_datetime.strftime('%Y-%m-%d %H:%M')}"
        return f"{name} - {self.get_status_display()}"

    def get_formatted_phone(self):
        """Format phone number for display"""
        if self.phone_number:
            # Remove all non-digit characters
            cleaned = re.sub(r'\D', '', self.phone_number)
            if len(cleaned) == 10:
                return f"({cleaned[:3]}) {cleaned[3:6]}-{cleaned[6:]}"
        return self.phone_number
    
    def get_project_summary(self):
        """Get a summary of the project"""
        project_display = dict(self.PROJECT_TYPE_CHOICES).get(self.project_type, 'Other')
        if self.is_emergency:
            return f"🚨 EMERGENCY: {project_display}"
        return project_display
    
    @property
    def is_today(self):
        """Check if appointment is today"""
        return self.scheduled_datetime and self.scheduled_datetime.date() == timezone.now().date()
    
    @property
    def is_upcoming(self):
        """Check if appointment is in the future"""
        return self.scheduled_datetime and self.scheduled_datetime > timezone.now()

    @property
    def is_scheduled(self):
        """Check if appointment has a scheduled date/time"""
        return self.scheduled_datetime is not None

    @property
    def is_overdue(self):
        """Check if scheduled appointment is overdue"""
        if not self.scheduled_datetime:
            return False
        return timezone.now() > self.scheduled_datetime and self.status in ['pending', 'confirmed']

    @property
    def conversation_summary(self):
        """Get a summary of the conversation"""
        if not self.conversation_history:
            return "No conversation yet"
        
        messages = len(self.conversation_history)
        last_message = self.conversation_history[-1].get('content', '')[:50] + '...' if self.conversation_history else ''
        return f"{messages} messages. Last: {last_message}"

    def add_conversation_message(self, role, content):
        """Add a message to conversation history"""
        if not isinstance(self.conversation_history, list):
            self.conversation_history = []
        
        self.conversation_history.append({
            'role': role,
            'content': content,
            'timestamp': timezone.now().isoformat()
        })
        self.save()

    def get_customer_info_completeness(self):
        """Return percentage of customer information that's been collected"""
        fields_to_check = [
            'customer_name', 'customer_area', 'project_type',
            'property_type', 'budget', 'timeline', 'house_stage'
        ]
        
        completed_fields = sum(1 for field in fields_to_check if getattr(self, field))
        return (completed_fields / len(fields_to_check)) * 100

    def mark_as_confirmed(self):
        """Mark appointment as confirmed"""
        self.status = 'confirmed'
        self.save()

    def mark_as_completed(self):
        """Mark appointment as completed"""
        self.status = 'completed'
        self.save()

    def cancel_appointment(self, reason=None):
        """Cancel the appointment"""
        self.status = 'cancelled'
        if reason:
            self.internal_notes = f"Cancelled: {reason}\n{self.internal_notes or ''}"
        self.save()

    def get_phone_without_whatsapp_prefix(self):
        """Get phone number without WhatsApp prefix"""
        if self.phone_number:
            return self.phone_number.replace('whatsapp:', '')
        return 'No phone number'
    
    def is_ready_for_booking(self):
        """Check if appointment has enough information for booking"""
        required_info = [
            self.customer_name,
            self.customer_area,
            self.project_type,
        ]
        return all(required_info)
    
    def get_project_details_summary(self):
        """Get a summary of the project details"""
        parts = []
        
        if self.project_type:
            parts.append(f"{self.project_type}")
            
        if self.property_type:
            parts.append(f"({self.get_property_type_display()})")
            
        if self.house_stage:
            parts.append(f"- {self.get_house_stage_display()}")
            
        if self.budget:
            parts.append(f"Budget: {self.budget}")
            
        return ' '.join(parts) if parts else 'No project details'

    def get_conversation_history(self):
        """Return formatted conversation history"""
        if self.conversation_history:
            try:
                return self.conversation_history
            except:
                return []
        return []
    
    def get_last_message(self):
        """Get the last message in the conversation"""
        if self.conversation_history:
            return self.conversation_history[-1] if self.conversation_history else None
        return None
    
    def get_appointment_time_range(self):
        """Get formatted appointment time range"""
        if self.scheduled_datetime and self.end_datetime:
            start = self.scheduled_datetime.strftime('%I:%M %p')
            end = self.end_datetime.strftime('%I:%M %p')
            return f"{start} - {end}"
        elif self.scheduled_datetime:
            return self.scheduled_datetime.strftime('%I:%M %p')
        return "Not scheduled"
    
    def get_appointment_date(self):
        """Get formatted appointment date"""
        if self.scheduled_datetime:
            return self.scheduled_datetime.strftime('%A, %B %d, %Y')
        return "Not scheduled"

    def check_time_slot_availability(self, new_datetime=None):
        """Check if the time slot (2-hour block) is available"""
        from django.db.models import Q
        
        check_datetime = new_datetime or self.scheduled_datetime
        if not check_datetime:
            return True
            
        check_end = check_datetime + self.duration
        
        # Check for overlapping appointments (excluding current appointment)
        overlapping = Appointment.objects.filter(
            Q(scheduled_datetime__lt=check_end) &
            Q(end_datetime__gt=check_datetime) &
            Q(status__in=['confirmed', 'in_progress']) &
            ~Q(pk=self.pk)  # Exclude current appointment
        ).exists()
        
        return not overlapping

    def has_uploaded_documents(self):
        """Check if any documents have been uploaded"""
        return bool(self.plan_file)
    
    def get_uploaded_documents(self):
        """Get all uploaded documents associated with this appointment"""
        documents = []
        
        # Check for plan file
        if self.plan_file:
            documents.append({
                'type': 'Plan File',
                'file': self.plan_file,
                'uploaded_at': self.plan_uploaded_at,
                'description': 'Customer uploaded plan/blueprint'
            })
        
        return documents
    
    def get_document_count(self):
        """Get the number of uploaded documents"""
        return len(self.get_uploaded_documents())
# Keep your other models (ConversationMessage, AppointmentNote, AppointmentReminder, ServiceArea) unchanged

class ConversationMessage(models.Model):
    ROLE_CHOICES = [
        ('user', 'Customer'),
        ('assistant', 'Bot')
    ]
    
    appointment = models.ForeignKey(
        Appointment,
        on_delete=models.CASCADE,
        related_name='conversation_messages'
    )
    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES
    )
    content = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now)
    
    class Meta:
        ordering = ['timestamp']
    
    def __str__(self):
        return f"{self.get_role_display()} message at {self.timestamp}"        

class AppointmentNote(models.Model):
    """Additional notes for appointments"""
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='notes')
    note = models.TextField()
    created_by = models.CharField(max_length=100, help_text="Who created this note")
    created_at = models.DateTimeField(default=timezone.now)
    is_customer_visible = models.BooleanField(default=False, help_text="Can customer see this note?")
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Note for {self.appointment} by {self.created_by}"


class AppointmentReminder(models.Model):
    """Track reminders sent for appointments"""
    REMINDER_TYPE_CHOICES = [
        ('sms', 'SMS'),
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('call', 'Phone Call'),
    ]
    
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='reminders')
    reminder_type = models.CharField(max_length=20, choices=REMINDER_TYPE_CHOICES)
    sent_at = models.DateTimeField(default=timezone.now)
    scheduled_for = models.DateTimeField(help_text="When reminder was scheduled to be sent")
    was_successful = models.BooleanField(default=True)
    error_message = models.TextField(blank=True, null=True)
    
    class Meta:
        ordering = ['-sent_at']
    
    def __str__(self):
        return f"{self.reminder_type} reminder for {self.appointment}"


class ServiceArea(models.Model):
    """Define service areas for the plumbing company"""
    name = models.CharField(max_length=100)
    postal_codes = models.TextField(help_text="Comma-separated postal codes")
    is_active = models.BooleanField(default=True)
    travel_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    
    def __str__(self):
        return self.name
    
    def get_postal_codes_list(self):
        return [code.strip() for code in self.postal_codes.split(',') if code.strip()]

from django.db import models
from django.utils import timezone

class ConversationMessage(models.Model):
    ROLE_CHOICES = [
        ('user', 'Customer'),
        ('assistant', 'Bot')
    ]
    
    appointment = models.ForeignKey(
        'Appointment',
        on_delete=models.CASCADE,
        related_name='conversation_messages'
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now)
    
    class Meta:
        ordering = ['timestamp']
    
    def __str__(self):
        return f"{self.get_role_display()} message at {self.timestamp}"

class Job(models.Model):
    site_visit = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='jobs')
    scheduled_datetime = models.DateTimeField()
    duration_hours = models.IntegerField(default=4)
    description = models.TextField()
    materials_needed = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=[
        ('scheduled', 'Scheduled'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ])
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-scheduled_datetime']


class Quotation(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
    ]
    
    appointment = models.ForeignKey('Appointment', on_delete=models.CASCADE, related_name='quotations')
    plumber = models.ForeignKey('auth.User', on_delete=models.CASCADE, null=True, blank=True)
    quotation_number = models.CharField(max_length=20, unique=True, blank=True)
    
    # Costs
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    labor_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    materials_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    transport_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # ADDED
    
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    
    # Tracking
    sent_via_whatsapp = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        # Generate quotation number if not exists
        if not self.quotation_number:
            today = timezone.now().date()
            quote_count = Quotation.objects.filter(
                created_at__date=today
            ).count() + 1
            self.quotation_number = f"Q{today.strftime('%Y%m%d')}{quote_count:03d}"
        
        # Calculate total - handle both new and existing quotations
        if self.pk:
            # Existing quotation - can access items
            items_total = sum(item.total_price for item in self.items.all())
        else:
            # New quotation - no items yet
            items_total = Decimal('0.00')
        
        self.total_amount = items_total + self.labor_cost + self.materials_cost + self.transport_cost
        
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"Quotation #{self.quotation_number} - {self.appointment.customer_name}"


class QuotationItem(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='items')
    description = models.TextField()
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    
    def save(self, *args, **kwargs):
        self.total_price = self.quantity * self.unit_price
        super().save(*args, **kwargs)
        
        # Update parent quotation total
        if self.quotation:
            self.quotation.save()
    
    def __str__(self):
        return f"{self.description} - R{self.total_price}"



class QuotationTemplate(models.Model):
    """Template for creating quotations quickly"""
    name = models.CharField(max_length=200, help_text="Template name (e.g., 'Standard Bathroom Renovation')")
    description = models.TextField(blank=True, help_text="Description of what this template is for")
    project_type = models.CharField(
        max_length=50,
        choices=[
            ('bathroom_renovation', 'Bathroom Renovation'),
            ('kitchen_renovation', 'Kitchen Renovation'),
            ('new_plumbing_installation', 'New Plumbing Installation'),
            ('general', 'General Plumbing'),
        ],
        default='general'
    )
    
    # Default costs
    default_labor_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    default_transport_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Template metadata
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_templates')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    use_count = models.IntegerField(default=0, help_text="Number of times this template has been used")
    
    class Meta:
        ordering = ['-use_count', '-updated_at']
        
    def __str__(self):
        return f"{self.name} ({self.get_project_type_display()})"
    
    def get_total_estimated_cost(self):
        """Calculate estimated total cost from template items"""
        items_total = sum(item.get_line_total() for item in self.items.all())
        return self.default_labor_cost + self.default_transport_cost + items_total
    
    def duplicate(self, new_name=None):
        """Create a copy of this template"""
        new_template = QuotationTemplate.objects.create(
            name=new_name or f"{self.name} (Copy)",
            description=self.description,
            project_type=self.project_type,
            default_labor_cost=self.default_labor_cost,
            default_transport_cost=self.default_transport_cost,
            created_by=self.created_by
        )
        
        # Copy all items
        for item in self.items.all():
            QuotationTemplateItem.objects.create(
                template=new_template,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                category=item.category,
                is_optional=item.is_optional,
                notes=item.notes,
                sort_order=item.sort_order
            )
        
        return new_template


class QuotationTemplateItem(models.Model):
    """Individual items in a quotation template"""
    CATEGORY_CHOICES = [
        ('fixtures', 'Fixtures'),
        ('pipes', 'Pipes & Fittings'),
        ('labor', 'Labor'),
        ('materials', 'Materials'),
        ('hardware', 'Hardware'),
        ('other', 'Other'),
    ]
    
    template = models.ForeignKey(QuotationTemplate, on_delete=models.CASCADE, related_name='items')
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='materials')
    is_optional = models.BooleanField(default=False, help_text="Mark if this item is optional")
    notes = models.TextField(blank=True, help_text="Internal notes about this item")
    sort_order = models.IntegerField(default=0)
    
    class Meta:
        ordering = ['sort_order', 'category', 'description']
    
    def __str__(self):
        return f"{self.description} - R{self.unit_price} x {self.quantity}"
    
    def get_line_total(self):
        """Calculate line total"""
        return self.quantity * self.unit_price
