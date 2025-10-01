from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from datetime import timedelta, datetime
import pytz
import json
import re
import uuid

# Define all choices at module level before models
STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('in_progress', 'In Progress'),
    ('confirmed', 'Confirmed'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
    ('no_show', 'No Show'),
]

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

PROJECT_TYPE_CHOICES = [
    ('bathroom_renovation', 'Bathroom Renovation'),
    ('new_plumbing_installation', 'New Plumbing Installation'),
    ('kitchen_renovation', 'Kitchen Renovation'),
    ('other', 'Other'),
]

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

APPOINTMENT_TYPE_CHOICES = [
    ('site_visit', 'Site Visit'),
    ('job_appointment', 'Job Appointment'),
]

JOB_STATUS_CHOICES = [
    ('not_applicable', 'Not Applicable'),
    ('pending_schedule', 'Pending Schedule'),
    ('scheduled', 'Scheduled'),
    ('in_progress', 'In Progress'), 
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
]

PLAN_STATUS_CHOICES = [
    ('pending_upload', 'Pending Plan Upload'),
    ('plan_uploaded', 'Plan Uploaded'),
    ('plan_reviewed', 'Plan Reviewed by Plumber'),
    ('ready_to_book', 'Ready to Book'),
]

QUOTATION_STATUS_CHOICES = [
    ('draft', 'Draft'),
    ('sent', 'Sent'),
    ('accepted', 'Accepted'),
    ('rejected', 'Rejected'),
]


class Appointment(models.Model):
    """Model to store plumbing appointment information and conversation history"""
    
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
    project_type = models.CharField(max_length=50, blank=True, null=True)
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
        choices=PLAN_STATUS_CHOICES,
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

    # Job scheduling fields
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
    
    # Add the choices as class attributes for template access
    STATUS_CHOICES = STATUS_CHOICES
    PROPERTY_TYPE_CHOICES = PROPERTY_TYPE_CHOICES
    PROJECT_TYPE_CHOICES = PROJECT_TYPE_CHOICES
    HOUSE_STAGE_CHOICES = HOUSE_STAGE_CHOICES
    APPOINTMENT_TYPE_CHOICES = APPOINTMENT_TYPE_CHOICES
    JOB_STATUS_CHOICES = JOB_STATUS_CHOICES
    
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

    # Job scheduling methods
    def mark_site_visit_completed(self, notes="", assessment=""):
        """Mark site visit as completed"""
        self.site_visit_completed = True
        self.site_visit_completed_at = timezone.now()
        self.site_visit_notes = notes
        self.plumber_assessment = assessment
        
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

        job_appointment = Appointment.objects.create(
            phone_number=f"{self.phone_number}_job_{timezone.now().strftime('%Y%m%d%H%M%S')}",
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
        
        self.job_status = 'scheduled'
        self.save()
        
        return job_appointment
    
    def get_job_appointments(self):
        """Get all job appointments for this site visit"""
        return self.job_appointments.all()
    
    def has_pending_job_schedule(self):
        """Check if job scheduling is pending"""
        return self.job_status == 'pending_schedule'

    # ... (keep all your other existing methods - I'm omitting them for brevity but they should all remain)
    
    def __str__(self):
        name = self.customer_name or 'Unknown Customer'
        if self.scheduled_datetime:
            return f"{name} - {self.scheduled_datetime.strftime('%Y-%m-%d %H:%M')}"
        return f"{name} - {self.get_status_display()}"

    def has_uploaded_documents(self):
        """Check if any documents have been uploaded"""
        return bool(self.plan_file)
    
    def get_uploaded_documents(self):
        """Get all uploaded documents associated with this appointment"""
        documents = []
        
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


class Quotation(models.Model):
    appointment = models.ForeignKey('Appointment', on_delete=models.CASCADE, related_name='quotations')
    plumber = models.ForeignKey('auth.User', on_delete=models.CASCADE, null=True, blank=True)
    quotation_number = models.CharField(max_length=20, unique=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    labor_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    materials_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    transport_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=QUOTATION_STATUS_CHOICES, default='draft')
    sent_via_whatsapp = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
   
    def save(self, *args, **kwargs):
        if not self.quotation_number:
            today = timezone.now().date()
            quote_count = Quotation.objects.filter(
                created_at__date=today
            ).count() + 1
            self.quotation_number = f"Q{today.strftime('%Y%m%d')}{quote_count:03d}"
        
        # Calculate total amount
        if self.pk:
            items_total = sum(item.total_price for item in self.items.all())
        else:
            items_total = 0
        
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
        
        if self.quotation:
            self.quotation.save()
    
    def __str__(self):
        return f"{self.description} - R{self.total_price}"


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