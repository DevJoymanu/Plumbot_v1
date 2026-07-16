from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from datetime import timedelta, datetime, time as dt_time
import pytz
import json
import re
import uuid
from decimal import Decimal, InvalidOperation


class LeadQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        """Rows owned by `tenant` (a Tenant instance or pk). THE tenant-scoping
        entry point — views and crons must never query across tenants without
        it (docs/MULTI_TENANT_PLAN.md §6). Accepts None only during the
        Phase-0 transition while legacy rows are being backfilled."""
        return self.filter(tenant=tenant)

    def for_tenant_or_seed(self, tenant):
        """for_tenant with the homebase-seed fallback for tenant=None — the
        view/helper scoping entry point (request.tenant is always set by the
        middleware, but request-less callers like the nav-badge context
        processor may pass None during the transition)."""
        if tenant is None:
            return self.filter(tenant_id=get_default_tenant_id())
        return self.filter(tenant=tenant)

    def get_or_create_lead(self, phone_number, tenant=None, defaults=None):
        """Tenant-aware lead identity (Phase 1). Phone numbers are unique PER
        TENANT (the same customer can talk to two companies), so the tenant is
        part of the lookup key — a bare get_or_create(phone_number=…) would
        match another tenant's lead. `tenant` may be a Tenant, a pk, or None
        (→ the homebase seed, for callers that predate tenant threading)."""
        tenant_id = tenant.pk if hasattr(tenant, 'pk') else tenant
        if tenant_id is None:
            tenant_id = get_default_tenant_id()
        return self.get_or_create(
            phone_number=phone_number,
            tenant_id=tenant_id,
            defaults=defaults or {'status': 'pending'},
        )

    def real(self):
        """Exclude console/scenario test lines (999-prefixed — the ITU-reserved
        range used by the test console and Scenario Lab; see bot/test_console).
        Client-facing pages list only real leads."""
        return self.exclude(phone_number__startswith='whatsapp:+999')

    def test_lines(self):
        """Only console/scenario test lines (the staff-only Test Leads page)."""
        return self.filter(phone_number__startswith='whatsapp:+999')

    def with_last_inbound(self):
        return self.exclude(last_inbound_at__isnull=True)

    def responded_since(self, delta):
        cutoff = timezone.now() - delta
        return self.with_last_inbound().filter(last_inbound_at__gte=cutoff)

    def last_1_week(self):
        return self.responded_since(timedelta(weeks=1))

    def last_2_weeks(self):
        return self.responded_since(timedelta(weeks=2))

    def last_3_weeks(self):
        return self.responded_since(timedelta(weeks=3))

    def last_1_month(self):
        return self.responded_since(timedelta(days=30))


class LeadStatus(models.TextChoices):
    COLD = 'cold', 'Cold'
    WARM = 'warm', 'Warm'
    HOT = 'hot', 'Hot'
    VERY_HOT = 'very_hot', 'Very Hot'


class LeadFollowUpStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    IN_PROGRESS = 'in_progress', 'In Progress'
    WAITING_CUSTOMER = 'waiting_customer', 'Waiting Customer'
    COMPLETED = 'completed', 'Completed'
    CLOSED_LOST = 'closed_lost', 'Closed Lost'


class LeadActivityType(models.TextChoices):
    CALL = 'call', 'Call'
    WHATSAPP_INBOUND = 'whatsapp_inbound', 'WhatsApp Inbound'
    WHATSAPP_OUTBOUND = 'whatsapp_outbound', 'WhatsApp Outbound'
    NOTE = 'note', 'Note'
    STATUS_CHANGE = 'status_change', 'Status Change'
    BOT_PAUSED = 'bot_paused', 'Bot Paused'
    BOT_RESUMED = 'bot_resumed', 'Bot Resumed'
    APPOINTMENT = 'appointment', 'Appointment'


class CallOutcome(models.TextChoices):
    NO_ANSWER = 'no_answer', 'No Answer'
    INTERESTED = 'interested', 'Interested'
    NOT_INTERESTED = 'not_interested', 'Not Interested'
    BOOKED = 'booked', 'Booked'
    FOLLOW_UP_LATER = 'follow_up_later', 'Follow Up Later'



# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenancy (Phase 0 — docs/MULTI_TENANT_PLAN.md)
# Row-level tenancy: every business table carries a nullable `tenant` FK that
# defaults to the seeded `homebase` tenant, so all existing code paths keep
# working unchanged while rows acquire an owner. The FK goes non-null in a
# later Phase-0 deploy once the production backfill is verified.
# ═══════════════════════════════════════════════════════════════════════════

class Tenant(models.Model):
    """A plumbing company on the platform. Homebase Plumbers is tenant #1,
    seeded by migration 0041 from today's hardcoded values."""
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=60, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


def _inherit_tenant(instance, parent):
    """Child business records always belong to their parent lead's tenant —
    regardless of who created them (dashboard actions default the FK to the
    homebase seed, which is wrong for another tenant's lead). Called from
    save() on every child model."""
    if parent is not None and parent.tenant_id and instance.tenant_id != parent.tenant_id:
        instance.tenant_id = parent.tenant_id


def get_default_tenant_id():
    """Owner for rows created by code paths that don't yet pass a tenant
    (everything before Phase 1 threads it through the webhook). Resolves the
    `homebase` seed tenant per call — a tiny indexed lookup, deliberately
    uncached: a process-level cache goes stale across test-DB rollbacks and
    would pin a deleted pk (same stale-state class as the shared-Appointment
    -instance rule). Returns None when the seed doesn't exist yet (fresh test
    DBs, mid-migration) — the FK is nullable so that's safe."""
    try:
        tenant = Tenant.objects.filter(slug='homebase').only('pk').first()
    except Exception:
        return None
    return tenant.pk if tenant is not None else None


def _tenant_fk(**overrides):
    """The standard tenant column (docs/MULTI_TENANT_PLAN.md §3.1). PROTECT on
    purpose: deleting a tenant must never cascade business data away —
    off-boarding is an explicit archive-then-delete flow (Phase 6).

    NON-NULL since Phase 0.2 (prod backfill verified 2026-07-15): every row
    must have an owner. Writes without a tenant resolve to the homebase seed
    via the default; if no seed exists the insert fails loudly — an ownerless
    row is a bug, never a fallback."""
    options = dict(
        to='bot.Tenant',
        null=False,
        blank=True,
        default=get_default_tenant_id,
        on_delete=models.PROTECT,
    )
    options.update(overrides)
    to = options.pop('to')
    return models.ForeignKey(to, **options)


class TenantWhatsAppChannel(models.Model):
    """A tenant's WhatsApp number. `phone_number_id` is the webhook routing
    key (Meta sends it on every inbound event). Numbers are registered under
    the platform's Business Manager (plan §13); the access token may be the
    shared platform token or per-channel."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='whatsapp_channels')
    phone_number_id = models.CharField(max_length=64, unique=True)
    business_account_id = models.CharField(max_length=64, blank=True, default='')
    # Fernet-encrypted at rest (bot/services/secrets.py); save() encrypts,
    # decrypted_access_token() decrypts. Legacy plaintext rows pass through
    # and get encrypted on their next save. NEVER log or display this value.
    access_token = models.TextField(blank=True, default='')
    verify_token = models.CharField(max_length=128, blank=True, default='')
    display_number = models.CharField(max_length=32, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        from .services.secrets import encrypt_secret
        self.access_token = encrypt_secret(self.access_token)
        super().save(*args, **kwargs)

    def decrypted_access_token(self) -> str:
        from .services.secrets import decrypt_secret
        return decrypt_secret(self.access_token)

    def __str__(self):
        return f"{self.tenant.slug} · {self.display_number or self.phone_number_id}"


class TenantProfile(models.Model):
    """Everything the code currently hardcodes for Homebase (plan §2.2),
    per tenant. Every field is optional (nullability rule,
    docs/CLIENT_ONBOARDING_CHECKLIST.md): business facts have NO fallback —
    absent means the bot gracefully omits the topic, never borrows another
    tenant's value. Generic copy falls back to platform defaults."""
    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name='profile')
    # identity
    plumber_name = models.CharField(max_length=100, blank=True, default='')
    plumber_contact = models.CharField(max_length=32, blank=True, default='')
    business_whatsapp = models.CharField(max_length=32, blank=True, default='')
    location_line = models.CharField(max_length=255, blank=True, default='')
    location_area = models.CharField(max_length=80, blank=True, default='')   # "Hatfield"
    location_city = models.CharField(max_length=80, blank=True, default='')   # "Harare"
    business_hours = models.JSONField(null=True, blank=True)   # {"days": "Sun–Fri", "open": "08:00", "close": "18:00", "closed": ["sat"]}
    timezone_name = models.CharField(max_length=64, blank=True, default='Africa/Johannesburg')
    excluded_areas = models.JSONField(default=list, blank=True)
    # sales
    currency = models.CharField(max_length=8, blank=True, default='US$')
    packages = models.JSONField(default=list, blank=True)
    sales_profile_md = models.TextField(blank=True, default='')
    faq_facts = models.JSONField(default=dict, blank=True)
    scripts = models.JSONField(default=dict, blank=True)
    # the "licensed and registered" claim is gated on certification docs on file
    licensed_claim_enabled = models.BooleanField(default=False)
    # email
    email_from_name = models.CharField(max_length=100, blank=True, default='')
    email_sender = models.EmailField(blank=True, default='')

    def __str__(self):
        return f"Profile · {self.tenant.slug}"


class TenantPriceItem(models.Model):
    """One priceable thing a tenant sells (docs/MULTI_TENANT_PLAN.md §3.1) —
    replaces the hardcoded price tables in response_mixin (Phase 2.3).

    Numbers only — customer-facing sentences are rendered by platform copy
    from these figures. All money fields are optional "from" rates in the
    tenant's currency: `supply`+`labour` for split-priced fittings, `flat`
    when the business quotes a single figure with no split (never invent
    one), `allin` for the headline supply+install rate. `parts` itemises
    multi-component builds (e.g. freestanding tub = tub + mixer + install).
    """
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='price_items')
    family = models.CharField(max_length=40)           # shower / tub / geyser / … / renovation / package / repair
    variant = models.CharField(max_length=40, blank=True, default='')  # '' = the default build (e.g. built-in tub)
    label = models.CharField(max_length=120, blank=True, default='')   # "vanity unit" (full noun, breakdown lines)
    short_label = models.CharField(max_length=120, blank=True, default='')  # "vanity" (headline price lines); falls back to label
    supply = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    labour = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    flat = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    allin = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    parts = models.JSONField(default=list, blank=True)  # [{"name": "mixer", "amount": 150}, …]
    sizes = models.JSONField(default=list, blank=True)  # measurement/spec blocks
    keywords = models.JSONField(default=list, blank=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['sort_order', 'family', 'variant']
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'family', 'variant'],
                name='uniq_price_item_per_tenant',
            ),
        ]

    def __str__(self):
        return f"{self.tenant.slug} · {self.family}/{self.variant or 'default'}"


class TenantPortfolioItem(models.Model):
    """One previous-work piece a tenant can show (docs/MULTI_TENANT_PLAN.md
    §3.1) — replaces bot/portfolio_catalog.PORTFOLIO_ITEMS (Phase 2.5).
    `filename` is relative to the portfolio images dir (homebase's bundled
    photos); wizard-uploaded tenant photos land under a per-tenant subdir."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='portfolio_items')
    item_id = models.SlugField(max_length=80)            # stable slug (logs/dedup)
    filename = models.CharField(max_length=255)
    title = models.CharField(max_length=120)
    price_line = models.CharField(max_length=200, blank=True, default='')  # "kitchen renovation from US$600"
    description = models.TextField(blank=True, default='')
    story = models.TextField(blank=True, default='')
    keywords = models.JSONField(default=list, blank=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['sort_order', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'item_id'],
                                    name='uniq_portfolio_item_per_tenant'),
        ]

    def as_catalog_dict(self) -> dict:
        """The dict shape bot/portfolio_catalog's matching/caption logic uses."""
        return {
            'id': self.item_id,
            'filename': self.filename,
            'title': self.title,
            'price': self.price_line,
            'description': self.description,
            'story': self.story,
            'keywords': list(self.keywords or []),
        }

    def __str__(self):
        return f"{self.tenant.slug} · {self.item_id}"


class TenantIntake(models.Model):
    """An owner-filled config submission (decision #2): the admin sends the
    owner a token link; the owner fills profile/FAQ/prices; the submission
    lands here as a DRAFT and only an admin approval applies it to the live
    TenantProfile/TenantPriceItem config. Nothing an owner types goes live
    unreviewed."""
    STATUS_CHOICES = [
        ('pending', 'Awaiting owner'),
        ('submitted', 'Submitted — awaiting review'),
        ('approved', 'Approved & applied'),
        ('rejected', 'Rejected'),
    ]
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='intakes')
    token = models.CharField(max_length=64, unique=True, default=uuid.uuid4)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='pending', db_index=True)
    data = models.JSONField(default=dict, blank=True)      # the owner's draft
    review_note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Intake {self.pk} · {self.tenant.slug} · {self.status}"


class TenantMembership(models.Model):
    """User → tenant link with a role. Platform admins are superusers (no
    membership needed — they get the tenant switcher)."""
    ROLE_CHOICES = [
        ('owner', 'Owner'),
        ('staff', 'Staff'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tenant_memberships')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='memberships')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='staff')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('user', 'tenant')]

    def __str__(self):
        return f"{self.user.username} @ {self.tenant.slug} ({self.role})"


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
        ('bathroom_installation', 'Bathroom Installation'),
        ('kitchen_renovation', 'Kitchen Renovation'),
        ('kitchen_installation', 'Kitchen Installation'),
        ('bathroom_and_kitchen_renovation', 'Bathroom & Kitchen Renovation'),
        ('new_plumbing_installation', 'New Plumbing Installation'),
        ('drain_unblocking', 'Drain Unblocking'),
        ('pipe_repair', 'Pipe Repair'),
        ('geyser_repair', 'Geyser Repair'),
        ('toilet_repair', 'Toilet Repair'),
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
    tenant = _tenant_fk(related_name='appointments')

    objects = LeadQuerySet.as_manager()

    # Unique PER TENANT, not globally (plan §6.4): the same customer may talk
    # to two companies on the platform. Enforced by the UniqueConstraint in
    # Meta; NULL-tenant rows exist only mid-Phase-0 (backfill assigns homebase).
    phone_number = models.CharField(max_length=50, help_text="Customer's WhatsApp number")
    customer_name = models.CharField(max_length=100, blank=True, null=True, help_text="Customer's full name")
    customer_email = models.EmailField(blank=True, null=True, help_text="Customer's email address")
    has_plan = models.BooleanField(
        null=True,           # ✅ Allow NULL
        blank=True,          # ✅ Allow blank in forms
        default=None         # ✅ Default to None
    )
    site_visit = models.BooleanField(default=False, help_text="Customer requested site visit")
    
    # Appointment Details
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    booked_at = models.DateTimeField(
        blank=True, null=True, db_index=True,
        help_text="When the lead converted to a booking (status first became confirmed)",
    )
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
    
    # Click-to-WhatsApp (CTWA) ad attribution. Set when the lead's first message
    # carries a referral object (source_type == 'ad'). ctwa_entry_at marks the start
    # of the 72-hour free-form messaging window; ctwa_referral keeps the ad headline/
    # body/source for reference.
    ctwa_source_id = models.CharField(max_length=64, blank=True, default='', help_text="Meta ad source_id for a CTWA lead")
    ctwa_entry_at = models.DateTimeField(blank=True, null=True, help_text="Start of the 72h CTWA messaging window")
    ctwa_referral = models.JSONField(blank=True, null=True, help_text="Full CTWA referral object from the ad click")

    # External Integration IDs
    google_calendar_event_id = models.CharField(max_length=200, blank=True, null=True)
    twilio_conversation_sid = models.CharField(max_length=100, blank=True, null=True)
    
    # Additional Information
    is_emergency = models.BooleanField(default=False)
    customer_rating = models.IntegerField(blank=True, null=True, help_text="Customer rating 1-5")
    completion_notes = models.TextField(blank=True, null=True, help_text="Notes after job completion")


    pricing_overview_sent = models.BooleanField(default=False)

    sent_pricing_intents = models.JSONField(
        default=list,
        blank=True,
        help_text="List of pricing intents already sent to this customer"
    )
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
    
    # Per-LEAD override only (a specific plumber assigned to this job). The
    # tenant-wide number lives in TenantProfile.plumber_contact — read through
    # plumber_contact(), never this field directly (Phase 2.2).
    plumber_contact_number = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text="Direct contact number for the assigned plumber (overrides the tenant default)"
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

    
    def plumber_contact(self) -> str:
        """Direct line for this lead's plumber: the per-lead override when a
        specific plumber is assigned, else the tenant profile's number.
        '' when neither is set — callers omit the call-us offer (nullability
        rule: never another tenant's number)."""
        if self.plumber_contact_number:
            return self.plumber_contact_number
        from .tenant_config import get_config
        return get_config(self.tenant).plumber_contact

    def plumber_display_name(self) -> str:
        """The plumber's name for customer-facing copy; the generic 'the
        plumber' when the tenant hasn't provided one (generic copy may use a
        platform default — a NAME is a business fact and never borrowed)."""
        from .tenant_config import get_config
        return get_config(self.tenant).plumber_name or 'the plumber'

    class Meta:
        # NOTE: this Meta is DEAD — a second `class Meta` further down this
        # (very long) class body overrides it at class creation. Effective
        # options live there; don't add anything here.
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
        """Auto-calculate end_datetime when scheduled_datetime is set, and stamp
        booked_at the first time a lead converts to a confirmed booking."""
        if self.scheduled_datetime and not self.end_datetime:
            self.end_datetime = self.scheduled_datetime + self.duration
        # Stamp the conversion time once, on any path that confirms the booking
        # (booking_mixin, mark_as_confirmed, admin, dashboard edits). Never reset
        # it if the appointment is later re-saved.
        if self.status == 'confirmed' and self.booked_at is None:
            self.booked_at = timezone.now()
            update_fields = kwargs.get('update_fields')
            if update_fields is not None and 'booked_at' not in update_fields:
                kwargs['update_fields'] = list(update_fields) + ['booked_at']
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
            
            # Check if it's Saturday (closed day)
            if weekday == 5:  # Saturday only
                print(f"Requested time is on Saturday (closed): weekday {weekday}")
                return False, "saturday_closed"            
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
                
                # Skip Saturday only (Sunday=6 is a working day)
                if check_date.weekday() != 5:  # 5=Saturday
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
        """Check if a given date is a business day (Sunday–Friday, Saturday closed)"""
        return check_date.weekday() != 5  # 5 = Saturday
    
    def is_business_hours(self, check_time):
        """Check if a given time is within business hours (8 AM - 6 PM)"""
        hour = check_time.hour
        return 8 <= hour < 18

    def get_business_day_name(self, date_obj):
        """Get user-friendly day name — only Saturday is closed"""
        weekday = date_obj.weekday()
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        if weekday == 5:
            return f"{day_names[weekday]} (Closed)"
        return day_names[weekday]
        
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
                    
                    # Skip Saturday only
                    if check_date.weekday() == 5:  # Saturday closed
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
        name = self.customer_name or (self.phone_number or '').replace('whatsapp:', '').strip() or 'Unknown Customer'
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

    def get_project_type_display(self):
        """
        Compatibility helper for code paths expecting Django's auto-generated
        get_FOO_display() (project_type field is a plain CharField).
        """
        if not self.project_type:
            return ''
        return dict(self.PROJECT_TYPE_CHOICES).get(self.project_type, self.project_type)
    
    @property
    def is_today(self):
        """Check if appointment is today (compared in the configured TIME_ZONE)."""
        return bool(
            self.scheduled_datetime
            and timezone.localtime(self.scheduled_datetime).date() == timezone.localdate()
        )
    
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

    def add_conversation_message(self, role, content, message_id=None, quoted=None):
        """Add a message to conversation history.

        message_id — the WhatsApp WAMID for this message. Stored so that a later
        inbound reply quoting this message (Cloud API `context.id`) can be
        resolved back to its text via ``resolve_quoted_message``.
        quoted — text of the earlier message this one is replying to (when the
        customer used WhatsApp's reply-to feature). Kept as transcript metadata.
        """
        try:
            # Ensure conversation_history is a list
            if not isinstance(self.conversation_history, list):
                self.conversation_history = []

            # Idempotency guard (conv 369): the webhook logs the inbound user
            # message on arrival and generate_response logs it again on its reply
            # paths. Skip a back-to-back duplicate of the same role+content so one
            # inbound line is never doubled in the transcript. Genuine repeats are
            # separated by the assistant's reply, so they are preserved.
            if self.conversation_history:
                last = self.conversation_history[-1]
                if isinstance(last, dict) and last.get("role") == role and last.get("content") == content:
                    # Backfill the WAMID/quote onto the existing entry if the
                    # arrival log had them and this re-log carries new detail.
                    _changed = False
                    if message_id and not last.get("message_id"):
                        last["message_id"] = message_id
                        _changed = True
                    if quoted and not last.get("quoted"):
                        last["quoted"] = quoted
                        _changed = True
                    if _changed:
                        self.save(update_fields=["conversation_history"])
                    print(f"↩️  Skipped duplicate {role} message in conversation_history")
                    return

            # Create message object
            message = {
                "role": role,
                "content": content,
                "timestamp": timezone.now().isoformat()
            }
            if message_id:
                message["message_id"] = message_id
            if quoted:
                message["quoted"] = quoted

            # Append to history
            self.conversation_history.append(message)

            # Save to database
            self.save(update_fields=["conversation_history"])

            print(f"✅ Saved {role} message to conversation_history")

        except Exception as e:
            print(f"❌ Error adding conversation message: {str(e)}")
            import traceback
            traceback.print_exc()
            raise  # Re-raise so errors aren't silently ignored

    def attach_message_id(self, role, content, message_id):
        """Stamp an outbound WAMID onto the matching conversation entry.

        Assistant replies are logged to history before they are actually sent,
        so the WAMID only becomes known once the send returns. Find the most
        recent entry with this role+content that has no WAMID yet and set it,
        enabling later quoted-reply resolution against the bot's own messages.
        """
        if not message_id or not isinstance(self.conversation_history, list):
            return
        try:
            for entry in reversed(self.conversation_history):
                if (
                    isinstance(entry, dict)
                    and entry.get("role") == role
                    and entry.get("content") == content
                    and not entry.get("message_id")
                ):
                    entry["message_id"] = message_id
                    self.save(update_fields=["conversation_history"])
                    return
        except Exception as e:
            print(f"⚠️ Could not attach message_id {message_id}: {e}")

    def record_sent_media(self, media_map, summary):
        """Log a batch of sent images with a {wamid: description} index.

        Each image we send has its own WAMID; storing wamid→description lets a
        later customer reply that quotes a specific image ("this one how much")
        resolve back to what that image shows. Stored as a single transcript
        entry so the per-image WAMIDs don't bloat the visible conversation.
        """
        if not media_map:
            return
        try:
            if not isinstance(self.conversation_history, list):
                self.conversation_history = []
            self.conversation_history.append({
                "role": "assistant",
                "content": summary,
                "timestamp": timezone.now().isoformat(),
                "media_index": media_map,
            })
            self.save(update_fields=["conversation_history"])
            print(f"🖼️  Recorded {len(media_map)} sent image WAMID(s) for reply resolution")
        except Exception as e:
            print(f"⚠️ Could not record sent media index: {e}")

    def resolve_quoted_message(self, message_id):
        """Return the text/description of a previously stored message by its WAMID.

        Used to turn a quoted-reply's `context.id` into the actual text the
        customer is replying to — including a specific image they highlighted,
        resolved via a batch's media index. Returns None when the quoted message
        predates WAMID storage or isn't in this conversation's history.
        """
        if not message_id or not isinstance(self.conversation_history, list):
            return None
        for entry in reversed(self.conversation_history):
            if not isinstance(entry, dict):
                continue
            if entry.get("message_id") == message_id:
                return entry.get("content")
            media_index = entry.get("media_index")
            if isinstance(media_index, dict) and message_id in media_index:
                return media_index[message_id]
        return None


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
        if self.plan_file:
            return True
        return bool(re.search(r'\[(FILE|VIDEO) UPLOADED\]', self.internal_notes or ''))

    def get_uploaded_documents(self):
        """Get all uploaded files — plan_file + everything in internal_notes"""
        return self.get_all_uploaded_files()

    def get_document_count(self):
        return self.uploaded_file_count
# Keep your other models (ConversationMessage, AppointmentNote, AppointmentReminder, ServiceArea) unchanged

    def mark_delayed(self, source_message='', save=True):
        # Flags the lead as deferring. Deliberately does NOT fabricate a
        # follow-up date — delay_followup_due_at is set only once the
        # conversation flow detects (or asks for) the customer's actual
        # timeframe. A null due date means the reactivation cron stays quiet
        # until a real date is captured (no arbitrary 14-day default).
        if self.is_delayed:
            return False
        from django.utils import timezone
        now = timezone.now()
        self.is_delayed = True
        self.delay_signal_detected_at = now
        notes = (self.internal_notes or '').strip()
        if '[DELAY_SIGNAL]' not in notes:
            self.internal_notes = f"{notes}\n[DELAY_SIGNAL]".strip()
        if save:
            self.save(update_fields=['is_delayed','delay_signal_detected_at',
                                    'internal_notes'])
        return True

    def clear_delayed(self, save=True):
        self.is_delayed = False
        notes = self.internal_notes or ''
        if '[DELAY_SIGNAL]' in notes:
            self.internal_notes = '\n'.join(
                l for l in notes.splitlines() if '[DELAY_SIGNAL]' not in l).strip()
        if save:
            self.save(update_fields=['is_delayed','internal_notes'])

    # ── Follow-up suppression states (internal_notes-backed, no migration) ──────
    HANDOFF_TAG = '[HANDED_OFF]'   # handed-off / awaiting-human
    PARKED_TAG  = '[PARKED]'       # parked / soft brush-off

    def _add_notes_tag(self, tag, save=True):
        notes = (self.internal_notes or '').strip()
        if tag in notes:
            return False
        self.internal_notes = f"{notes}\n{tag}".strip()
        if save:
            self.save(update_fields=['internal_notes'])
        return True

    def _remove_notes_tag(self, tag, save=True):
        notes = self.internal_notes or ''
        if tag not in notes:
            return False
        self.internal_notes = '\n'.join(
            l for l in notes.splitlines() if tag not in l).strip()
        if save:
            self.save(update_fields=['internal_notes'])
        return True

    def unpark(self, save=True):
        """Lift the parked/soft-brush-off suppression — e.g. once the customer
        re-engages and agrees a concrete follow-up date."""
        return self._remove_notes_tag(self.PARKED_TAG, save=save)

    def mark_handed_off(self, save=True):
        """Routed to a human (Tinashe): suppress proactive follow-ups. The bot
        still answers if the customer replies."""
        return self._add_notes_tag(self.HANDOFF_TAG, save=save)

    def mark_parked(self, save=True):
        """Customer asked to be left alone / soft brush-off: suppress follow-ups
        until they re-engage."""
        return self._add_notes_tag(self.PARKED_TAG, save=save)

    @property
    def delay_days_remaining(self):
        if not self.delay_followup_due_at:
            return None
        from django.utils import timezone
        return (self.delay_followup_due_at - timezone.now()).days

    @property
    def delay_is_overdue(self):
        r = self.delay_days_remaining
        return r is not None and r < 0

    @property
    def delay_pct_elapsed(self):
        if not self.delay_signal_detected_at or not self.delay_followup_due_at:
            return 0
        from django.utils import timezone
        total = (self.delay_followup_due_at - self.delay_signal_detected_at).total_seconds()
        elapsed = (timezone.now() - self.delay_signal_detected_at).total_seconds()
        return min(100, int((elapsed / total) * 100)) if total > 0 else 100


    # New follow-up tracking fields (add these after running the migration)
    last_customer_response = models.DateTimeField(
        null=True, 
        blank=True, 
        help_text='Last time customer sent a message'
    )
    last_followup_sent = models.DateTimeField(
        null=True, 
        blank=True, 
        help_text='Last time we sent a follow-up message'
    )
    followup_count = models.IntegerField(
        default=0, 
        help_text='Number of follow-ups sent'
    )
    retry_count = models.IntegerField(
        default=0,
        help_text='Number of retries for current question'
    )
    followup_stage = models.CharField(
        max_length=20,
        choices=[
            ('none', 'No Follow-up Needed'),
            ('day_1', '1 Day Follow-up'),
            ('day_3', '3 Day Follow-up'),
            ('week_1', '1 Week Follow-up'),
            ('week_2', '2 Week Follow-up'),
            ('month_1', '1 Month Follow-up'),
            ('completed', 'Follow-up Completed'),
            ('responded', 'Customer Responded'),
        ],
        default='none',
        help_text='Current follow-up stage'
    )
    is_lead_active = models.BooleanField(
        default=True, 
        help_text='Whether this lead is still active for follow-ups'
    )
    lead_marked_inactive_at = models.DateTimeField(
        null=True, 
        blank=True, 
        help_text='When lead was marked as inactive'
    )
    lead_score = models.IntegerField(default=0, db_index=True)
    lead_status = models.CharField(
        max_length=20,
        choices=LeadStatus.choices,
        default=LeadStatus.COLD,
        db_index=True,
    )
    chatbot_paused = models.BooleanField(default=False, db_index=True)
    follow_up_status = models.CharField(
        max_length=30,
        choices=LeadFollowUpStatus.choices,
        default=LeadFollowUpStatus.PENDING,
        db_index=True,
    )
    manual_followup_done = models.BooleanField(default=False, db_index=True)
    manual_followup_updated_at = models.DateTimeField(null=True, blank=True)
    last_priority_alert_summary = models.TextField(blank=True)
    last_priority_alert_sent_at = models.DateTimeField(null=True, blank=True)
    last_unconfirmed_summary_text = models.TextField(blank=True)
    last_unconfirmed_summary_at = models.DateTimeField(null=True, blank=True)
    admin_notes = models.TextField(blank=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_follow_up_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True, db_index=True)
    previous_work_photos_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When previous work photos were last sent to this customer'
    )
    
    is_delayed = models.BooleanField(default=False, db_index=True,
        help_text='True when customer signalled not ready yet.')
    delay_signal_detected_at = models.DateTimeField(null=True, blank=True,
        help_text='When the delay signal was first detected.')
    delay_followup_due_at = models.DateTimeField(null=True, blank=True, db_index=True,
        help_text='14 days after delay_signal_detected_at — plumber follow-up deadline.')    

    
    def mark_customer_response(self):
        """Mark that customer has responded - resets follow-up stage"""
        self.last_customer_response = timezone.now()
        self.last_inbound_at = self.last_customer_response
        self.followup_stage = 'responded'
        self.is_lead_active = True
        self.retry_count = 0  # ADD THIS LINE
        # A fresh inbound reopens the WhatsApp free-form window on Meta's side, so
        # clear any earlier "window closed" flag (set when a send hit 131047).
        notes = self.internal_notes or ''
        if self.FREEFORM_CLOSED_TAG in notes:
            self.internal_notes = '\n'.join(
                l for l in notes.splitlines() if self.FREEFORM_CLOSED_TAG not in l
            ).strip()
        self.save()

    # ----- Click-to-WhatsApp (CTWA) ad window ---------------------------------
    CTWA_WINDOW_HOURS = 72

    def record_ctwa_referral(self, referral):
        """Mark this lead as originating from a CTWA ad and (re)start the 72h window.

        A new ad click is a fresh free-entry point, so the window restarts each time
        a referral with source_type == 'ad' arrives. Returns True if recorded.
        """
        if not referral or referral.get('source_type') != 'ad':
            return False
        self.ctwa_source_id = str(referral.get('source_id') or '')[:64]
        self.ctwa_referral = referral
        self.ctwa_entry_at = timezone.now()
        self.save(update_fields=['ctwa_source_id', 'ctwa_referral', 'ctwa_entry_at'])
        return True

    @property
    def ctwa_window_expires_at(self):
        """When the 72h free-form messaging window closes (None if not a CTWA lead)."""
        if not self.ctwa_entry_at:
            return None
        return self.ctwa_entry_at + timedelta(hours=self.CTWA_WINDOW_HOURS)

    @property
    def ctwa_window_open(self):
        """True while the lead is still inside its 72h CTWA window."""
        expires = self.ctwa_window_expires_at
        return bool(expires and expires > timezone.now())

    # ----- WhatsApp free-form messaging window (24h standard / 72h for ads) ----
    @property
    def messaging_window_kind(self):
        """'72h' for CTWA ad leads (extended free-form window), else the
        standard '24h' WhatsApp customer-service window."""
        return '72h' if self.ctwa_entry_at else '24h'

    @property
    def messaging_window_closes_at(self):
        """When the free-form (no-template) messaging window closes.

        Standard rule: 24h from the customer's last message. For CTWA ad leads it
        is extended to the 72h window from the ad entry point — whichever is
        later. Anchored to the lead's last message so it reflects re-engagement.
        """
        candidates = []
        last_msg = self.last_inbound_at or self.last_customer_response
        if last_msg:
            candidates.append(last_msg + timedelta(hours=24))
        if self.ctwa_entry_at:
            candidates.append(self.ctwa_entry_at + timedelta(hours=self.CTWA_WINDOW_HOURS))
        return max(candidates) if candidates else None

    # Set when a send is rejected with Meta error 131047 ("Re-engagement
    # message"): the free-form window is closed on Meta's side regardless of our
    # computed 24h/72h window. Cleared in mark_customer_response when the
    # customer messages again (which reopens the window).
    FREEFORM_CLOSED_TAG = '[FREEFORM_WINDOW_CLOSED]'

    def mark_freeform_window_closed(self, save=True):
        """Record Meta's authoritative 131047 verdict so we stop attempting
        free-form sends (which would keep bouncing) until the customer replies.
        We deliberately do NOT fall back to a paid template."""
        notes = (self.internal_notes or '').strip()
        if self.FREEFORM_CLOSED_TAG in notes:
            return False
        self.internal_notes = f"{notes}\n{self.FREEFORM_CLOSED_TAG}".strip()
        if save:
            self.save(update_fields=['internal_notes'])
        return True

    @property
    def messaging_window_open(self):
        """True while free-form replies (no template) are still allowed.

        Meta is authoritative: a prior 131047 (recorded via the closed flag)
        overrides our computed window — our 72h CTWA window is only a local
        assumption and Meta may grant just the standard 24h."""
        if self.FREEFORM_CLOSED_TAG in (self.internal_notes or ''):
            return False
        closes = self.messaging_window_closes_at
        return bool(closes and closes > timezone.now())

    def recalculate_lead_scoring(self, persist=True):
        """Recalculate lead score and status from collected qualification fields."""
        from .services.lead_scoring import calculate_lead_score
        score, classification = calculate_lead_score(self)
        self.lead_score = score
        self.lead_status = classification
        if classification == LeadStatus.VERY_HOT:
            self.chatbot_paused = True
        if persist:
            self.save(update_fields=['lead_score', 'lead_status', 'chatbot_paused'])
        return score, classification

    def pause_chatbot(self, save=True):
        self.chatbot_paused = True
        if save:
            self.save(update_fields=['chatbot_paused'])

    def resume_chatbot(self, save=True):
        self.chatbot_paused = False
        if save:
            self.save(update_fields=['chatbot_paused'])
    
    def mark_as_inactive_lead(self, reason='customer_requested'):
        """Mark lead as inactive (no more follow-ups)"""
        self.is_lead_active = False
        self.lead_marked_inactive_at = timezone.now()
        self.followup_stage = 'completed'
        self.save()
    
    def get_days_since_last_contact(self):
        """Get number of days since last customer contact"""
        if self.last_customer_response:
            delta = timezone.now() - self.last_customer_response
            return delta.days
        elif self.created_at:
            delta = timezone.now() - self.created_at
            return delta.days
        return 0
    
    # Marker prefix → (human label, channel) for follow-ups logged into
    # conversation_history. Drives the follow-up log's per-channel stamps and
    # kind labels. Channel is 'whatsapp' or 'email'. Order matters: more
    # specific prefixes (e.g. BULK) must precede their shorter relatives.
    FOLLOWUP_MARKERS = (
        ('[BULK MANUAL FOLLOW-UP]', 'Bulk',            'whatsapp'),
        ('[MANUAL FOLLOW-UP]',      'Manual',          'whatsapp'),
        ('[AUTO FOLLOW-UP]',        'Automatic',       'whatsapp'),
        ('[DELAY NUDGE',            'Delay nudge',     'whatsapp'),
        ('[PARKED NUDGE',           'Parked nudge',    'whatsapp'),
        ('[DELAY REACTIVATION]',    'Reactivation',    'whatsapp'),
        ('[DELAY ACCESS CHECK-IN]', 'Access check-in', 'whatsapp'),
        ('[DELAY LAST CHECK]',      'Last-check',      'email'),
        ('[SCHEDULED FOLLOW-UP]',   'Scheduled',       'whatsapp'),
        ('[SCHEDULED EMAIL]',       'Scheduled',       'email'),
        ('[EMAIL FOLLOW-UP]',       'Email',           'email'),
        ('[IMAGE SENT]',            'Image',           'whatsapp'),
        ('[PDF SENT]',              'PDF',             'whatsapp'),
    )

    def get_followup_log(self):
        """Structured follow-up events parsed from conversation_history.

        Returns a list (newest first) of dicts:
          {index, channel, channel_label, kind, text, timestamp, edited,
           within_window, hours_since_inbound}
        `index` is the position in conversation_history, used to edit the entry
        in place. `timestamp` is an aware datetime or None. `text` has the
        ``[MARKER]`` prefix stripped for display; the marker is preserved on
        save so channel/kind detection keeps working.

        For WhatsApp events, `within_window` reports whether the message was
        sent inside the 24-hour customer-service window (i.e. within 24h of the
        customer's last inbound message before it). A free-form WhatsApp message
        sent outside that window is rejected by the Cloud API and never reaches
        the customer, so the UI flags those as not delivered. `within_window` is
        None when it can't be determined (no prior inbound / missing timestamp),
        and is always None for email events (the window doesn't apply).
        """
        history = self.conversation_history if isinstance(self.conversation_history, list) else []
        events = []
        last_inbound_ts = None  # most recent customer message timestamp seen so far
        for idx, entry in enumerate(history):
            if not isinstance(entry, dict):
                continue
            role = entry.get('role')
            ts_raw = entry.get('timestamp')
            dt = None
            if ts_raw:
                try:
                    dt = datetime.fromisoformat(ts_raw)
                except (ValueError, TypeError):
                    dt = None

            if role == 'user':
                if dt is not None:
                    last_inbound_ts = dt
                continue
            if role != 'assistant':
                continue

            content = (entry.get('content') or '').strip()
            matched = None
            for prefix, label, channel in self.FOLLOWUP_MARKERS:
                if content.startswith(prefix):
                    matched = (label, channel)
                    break
            if not matched:
                continue
            label, channel = matched
            # Strip the full bracketed marker (handles e.g. "[DELAY NUDGE 2]").
            close = content.find(']')
            body = content[close + 1:].strip() if close != -1 else content

            within_window = None
            hours_since_inbound = None
            if channel == 'whatsapp' and dt is not None and last_inbound_ts is not None:
                hours_since_inbound = (dt - last_inbound_ts).total_seconds() / 3600
                within_window = hours_since_inbound <= 24

            events.append({
                'index': idx,
                'channel': channel,
                'channel_label': 'Email' if channel == 'email' else 'WhatsApp',
                'kind': label,
                'text': body,
                'timestamp': dt,
                'edited': bool(entry.get('edited_at')),
                'within_window': within_window,
                'hours_since_inbound': hours_since_inbound,
            })
        events.reverse()  # newest first
        return events

    def get_whatsapp_followups(self):
        """WhatsApp follow-up events only (newest first)."""
        return [e for e in self.get_followup_log() if e['channel'] == 'whatsapp']

    def get_email_followups(self):
        """Email follow-up events only (newest first)."""
        return [e for e in self.get_followup_log() if e['channel'] == 'email']

    def scheduled_whatsapp_followups(self):
        """Staff-queued WhatsApp follow-ups still to go out (pending/failed)."""
        return self.scheduled_followups.filter(
            channel='whatsapp', status__in=['pending', 'failed']
        ).order_by('scheduled_for')

    def scheduled_email_followups(self):
        """Staff-queued email follow-ups still to go out (pending/failed)."""
        return self.scheduled_followups.filter(
            channel='email', status__in=['pending', 'failed']
        ).order_by('scheduled_for')

    def pending_reminders(self):
        """Staff-queued reminders still to go out (pending/failed), any target/channel."""
        return self.scheduled_reminders.filter(
            status__in=['pending', 'failed']
        ).order_by('scheduled_for')

    def get_upcoming_emails(self):
        """Scheduled customer-facing emails for this lead, with send dates/times.

        Returns {'has_email': bool, 'items': [...]} where each item is
        {label, scheduled_for (aware datetime), status, note}, sorted by time.
        status is 'sent' (real evidence of a send), 'pending' (future), or
        'overdue' (past with no send recorded — e.g. the cron hasn't fired).

        Covers two sequences:
          • Delay re-engagement (keyed off delay_followup_due_at): the
            re-engagement email, then the last-check email 4 days later.
          • Pre-appointment reminders (keyed off scheduled_datetime). These go
            out by email only when the WhatsApp 24h window is closed at send
            time — noted on each reminder item.
        """
        now = timezone.now()
        sast = pytz.timezone('Africa/Johannesburg')
        items = []

        def _status(scheduled_for, sent):
            if sent:
                return 'sent'
            return 'pending' if scheduled_for >= now else 'overdue'

        # Evidence of delay emails already sent (markers logged by the cron).
        history_text = ''
        if isinstance(self.conversation_history, list):
            history_text = '\n'.join(
                (e.get('content') or '') for e in self.conversation_history
                if isinstance(e, dict) and e.get('role') == 'assistant'
            )
        reengaged = '[DELAY REACTIVATION]' in history_text or '[DELAY ACCESS CHECK-IN]' in history_text
        last_checked = '[DELAY LAST CHECK]' in history_text

        if self.delay_followup_due_at:
            due = self.delay_followup_due_at
            items.append({
                'label': 'Delay re-engagement email',
                'scheduled_for': due,
                'status': _status(due, reengaged),
                'note': 'Sent on the agreed re-contact date.',
                'source': 'delay',
            })
            last_check = due + timedelta(hours=96)  # DELAY_SECOND_TOUCH_HOURS
            items.append({
                'label': 'Last-check email',
                'scheduled_for': last_check,
                'status': _status(last_check, last_checked),
                'note': 'Final check-in, 4 days after the re-engagement.',
                'source': 'delay',
            })

        if self.scheduled_datetime:
            sd_sast = self.scheduled_datetime.astimezone(sast)

            def _at(d, hour, minute=0):
                return sast.localize(datetime.combine(d, dt_time(hour, minute)))

            reminders = [
                ('Reminder — 2 days before',  _at((sd_sast - timedelta(days=2)).date(), 18, 0), self.reminder_1_day_sent),
                ('Reminder — 1 day before',   _at((sd_sast - timedelta(days=1)).date(), 18, 0), self.reminder_1_day_sent),
                ('Reminder — morning of',     _at(sd_sast.date(), 7, 0),                        self.reminder_morning_sent),
                ('Reminder — 2 hours before', self.scheduled_datetime - timedelta(hours=2),     self.reminder_2_hours_sent),
            ]
            for label, when, sent in reminders:
                items.append({
                    'label': label,
                    'scheduled_for': when,
                    'status': _status(when, sent),
                    'note': 'Email used only if the WhatsApp 24h window is closed.',
                    'source': 'reminder',
                })

        # Limit overdue items to the last 7 days — drop ones that have been
        # missed for longer so stale follow-ups don't pile up indefinitely.
        overdue_floor = now - timedelta(days=7)
        items = [
            it for it in items
            if it['status'] != 'overdue' or it['scheduled_for'] >= overdue_floor
        ]

        items.sort(key=lambda x: x['scheduled_for'])
        return {'has_email': bool(self.customer_email), 'items': items}

    @property
    def last_followup_event(self):
        """Most recent follow-up event (dict) or None — for list-row stamps."""
        log = self.get_followup_log()
        return log[0] if log else None

    @property
    def last_followup_channel(self):
        """'whatsapp' | 'email' | None — channel of the most recent follow-up."""
        event = self.last_followup_event
        return event['channel'] if event else None

    def get_followup_status_display_verbose(self):
        """Get detailed follow-up status for admin"""
        if not self.is_lead_active:
            return f"Inactive since {self.lead_marked_inactive_at.strftime('%Y-%m-%d')}"
        
        days_since = self.get_days_since_last_contact()
        stage_display = self.get_followup_stage_display()
        
        if self.followup_count == 0:
            return f"No follow-ups yet ({days_since} days since last contact)"
        else:
            return f"{stage_display} - {self.followup_count} follow-ups sent ({days_since} days)"
    
    def should_send_followup_now(self):
        """Check if this lead needs a follow-up right now"""
        if not self.is_lead_active or self.status == 'confirmed':
            return False
        
        now = timezone.now()
        
        # Don't send if customer responded recently (within 12 hours)
        if self.last_customer_response:
            hours_since = (now - self.last_customer_response).total_seconds() / 3600
            if hours_since < 12:
                return False
        
        # Don't send if we already sent a follow-up today
        if self.last_followup_sent:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self.last_followup_sent >= today_start:
                return False
        
        # Check if it's time based on follow-up stage
        return self._is_ready_for_next_followup(now)
    

    def get_all_uploaded_files(self) -> list:
        files = []
        seen_paths = set()

        if self.plan_file:
            path = str(self.plan_file)
            try:
                url = default_storage.url(path)
            except Exception:
                url = path
            ext = path.lower()
            files.append({
                'path': path,
                'url': url,
                'label': path.split('/')[-1],
                'type': 'video' if 'video' in ext else ('image' if any(e in ext for e in ['.jpg','.jpeg','.png','.webp','.gif']) else 'document'),
                'uploaded_at': self.plan_uploaded_at,
            })
            seen_paths.add(path)

        pattern = re.compile(r'\[(FILE|VIDEO) UPLOADED\] (.+?) \| URL: (.+?) \|')
        for match in pattern.finditer(self.internal_notes or ''):
            kind, path, url = match.group(1), match.group(2).strip(), match.group(3).strip()
            if path in seen_paths:
                continue
            seen_paths.add(path)
            # The URL in the note is a snapshot from upload time — on R2 that's
            # a presigned link that expires (~1h), so stored URLs go dead.
            # Regenerate from the path at render time; keep the snapshot only
            # if the storage backend can't produce one.
            try:
                url = default_storage.url(path)
            except Exception:
                pass
            ext = path.lower()
            if kind == 'VIDEO':
                ftype = 'video'
            elif any(e in ext for e in ['.jpg', '.jpeg', '.png', '.webp', '.gif']):
                ftype = 'image'
            else:
                ftype = 'document'
            files.append({
                'path': path,
                'url': url,
                'label': path.split('/')[-1],
                'type': ftype,
                'uploaded_at': None,
            })

        return files

    @property
    def uploaded_file_count(self) -> int:
        """Count of all uploaded files (plan_file + extras in internal_notes)."""
        import re
        count = 1 if self.plan_file else 0
        pattern = re.compile(r'\[(FILE|VIDEO) UPLOADED\]')
        for match in pattern.finditer(self.internal_notes or ''):
            path_start = self.internal_notes.index(match.group(0)) + len(match.group(0))
            path_end = self.internal_notes.index(' | URL:', path_start)
            path = self.internal_notes[path_start:path_end].strip()
            if path != self.plan_file:
                count += 1
        return count

    @property
    def all_plan_file_urls(self) -> list[str]:
        """All uploaded file URLs in order received."""
        import re
        from django.core.files.storage import default_storage
        urls = []
        if self.plan_file:
            try:
                urls.append(default_storage.url(self.plan_file))
            except Exception:
                urls.append(self.plan_file)
        seen_paths = {str(self.plan_file)} if self.plan_file else set()
        pattern = re.compile(r'\[(FILE|VIDEO) UPLOADED\] (.+?) \| URL: (.+?) \|')
        for match in pattern.finditer(self.internal_notes or ''):
            path, url = match.group(2).strip(), match.group(3).strip()
            if path in seen_paths:
                continue
            seen_paths.add(path)
            # Regenerate from the path — stored URLs are expiring presigned
            # snapshots (see get_all_uploaded_files).
            try:
                url = default_storage.url(path)
            except Exception:
                pass
            if url not in urls:
                urls.append(url)
        return urls


    def _is_ready_for_next_followup(self, now):
        """Internal method to check if ready for next follow-up stage"""
        if self.followup_stage == 'none' or self.followup_stage == 'responded':
            # First follow-up: 1 day after last contact
            reference_time = self.last_customer_response or self.created_at
            if reference_time:
                days_since = (now - reference_time).total_seconds() / (3600 * 24)
                return days_since >= 1
        
        elif self.last_followup_sent:
            days_since_followup = (now - self.last_followup_sent).total_seconds() / (3600 * 24)
            
            stage_delays = {
                'day_1': 3,      # 3 days after first follow-up
                'day_3': 7,      # 1 week after second follow-up
                'week_1': 14,    # 2 weeks after third follow-up
                'week_2': 30,    # 1 month after fourth follow-up
            }
            
            required_delay = stage_delays.get(self.followup_stage)
            if required_delay:
                return days_since_followup >= required_delay
        
        return False
    
    class Meta:
        # The EFFECTIVE Meta for Appointment (an earlier `class Meta` higher up
        # is shadowed by this one — Python keeps the last definition).
        # Add index for follow-up queries
        indexes = [
            models.Index(fields=['is_lead_active', 'followup_stage', 'last_customer_response']),
            models.Index(fields=['last_followup_sent']),
        ]
        constraints = [
            # Plan §6.4: same customer, two tenants = two independent leads —
            # phone uniqueness is per tenant, not global.
            models.UniqueConstraint(
                fields=['tenant', 'phone_number'],
                name='uniq_phone_per_tenant',
            ),
        ]


class ConversationMessage(models.Model):
    ROLE_CHOICES = [
        ('user', 'Customer'),
        ('assistant', 'Bot')
    ]
    
    tenant = _tenant_fk()
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
    

    def save(self, *args, **kwargs):
        _inherit_tenant(self, getattr(self, 'appointment', None))
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['timestamp']
    
    def __str__(self):
        return f"{self.get_role_display()} message at {self.timestamp}"


class ScheduledFollowup(models.Model):
    """A staff-queued follow-up to be sent at a chosen date/time.

    Dispatched by the ``send_scheduled_followups`` management command (also
    invoked at the start of ``send_followups``), which sends due pending rows
    via WhatsApp or email and logs them onto the appointment's conversation
    history so they appear in the follow-up tabs.
    """
    CHANNEL_CHOICES = [
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    tenant = _tenant_fk()
    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name='scheduled_followups'
    )
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, db_index=True)
    scheduled_for = models.DateTimeField(db_index=True)
    template_key = models.CharField(
        max_length=50, blank=True, default='',
        help_text="email_catalog key — when set, the dispatcher renders that "
                  "template fresh at send time instead of using subject/message.",
    )
    subject = models.CharField(max_length=255, blank=True, default='', help_text="Email subject (email only)")
    message = models.TextField(blank=True, default='')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', db_index=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default='')


    def save(self, *args, **kwargs):
        _inherit_tenant(self, getattr(self, 'appointment', None))
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['scheduled_for']
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
        ]

    def __str__(self):
        return f"{self.get_channel_display()} follow-up for apt {self.appointment_id} @ {self.scheduled_for}"


class ScheduledReminder(models.Model):
    """A staff-queued one-off reminder to be sent at a chosen date/time.

    Mirrors ScheduledFollowup, but the recipient is selectable per reminder:
      • target='customer' → sends to the lead (WhatsApp/email), like a follow-up.
      • target='plumber'  → sends to the plumber team (notification emails, or
        WhatsApp to PLUMBER_PHONE_NUMBER).
    Dispatched by ``send_scheduled_reminders`` (also invoked at the start of
    ``send_reminders``).
    """
    TARGET_CHOICES = [
        ('customer', 'Customer'),
        ('plumber', 'Plumber / team'),
    ]
    CHANNEL_CHOICES = [
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    tenant = _tenant_fk()
    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name='scheduled_reminders'
    )
    target = models.CharField(max_length=10, choices=TARGET_CHOICES, default='plumber', db_index=True)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, db_index=True)
    scheduled_for = models.DateTimeField(db_index=True)
    subject = models.CharField(max_length=255, blank=True, default='', help_text="Email subject (email only)")
    message = models.TextField(blank=True, default='')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', db_index=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default='')


    def save(self, *args, **kwargs):
        _inherit_tenant(self, getattr(self, 'appointment', None))
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['scheduled_for']
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
        ]

    def __str__(self):
        return (
            f"{self.get_target_display()} {self.get_channel_display()} reminder "
            f"for apt {self.appointment_id} @ {self.scheduled_for}"
        )


class AppointmentNote(models.Model):
    """Additional notes for appointments"""
    tenant = _tenant_fk()
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='notes')
    note = models.TextField()
    created_by = models.CharField(max_length=100, help_text="Who created this note")
    created_at = models.DateTimeField(default=timezone.now)
    is_customer_visible = models.BooleanField(default=False, help_text="Can customer see this note?")
    

    def save(self, *args, **kwargs):
        _inherit_tenant(self, getattr(self, 'appointment', None))
        super().save(*args, **kwargs)

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


class WhatsAppInboundEvent(models.Model):
    tenant = _tenant_fk()
    message_id = models.CharField(max_length=128, unique=True)
    sender = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Inspection / CTWA-ad attribution. message_type and referral let us see at a
    # glance whether a chat originated from a Click-to-WhatsApp ad (referral.source_type
    # == 'ad'); raw_payload keeps the full inbound message object for debugging.
    message_type = models.CharField(max_length=32, blank=True)
    referral = models.JSONField(null=True, blank=True)
    raw_payload = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class ServiceArea(models.Model):
    """Define service areas for the plumbing company"""
    tenant = _tenant_fk()
    name = models.CharField(max_length=100)
    postal_codes = models.TextField(help_text="Comma-separated postal codes")
    is_active = models.BooleanField(default=True)
    travel_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0.00)
    
    def __str__(self):
        return self.name
    
    def get_postal_codes_list(self):
        return [code.strip() for code in self.postal_codes.split(',') if code.strip()]


class Job(models.Model):
    tenant = _tenant_fk()
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
    

    def save(self, *args, **kwargs):
        _inherit_tenant(self, getattr(self, 'site_visit', None))
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-scheduled_datetime']


class Quotation(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
    ]
    
    tenant = _tenant_fk()
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

    def get_display_name(self):
        service = ''
        if self.appointment:
            if self.appointment.project_type:
                try:
                    service = self.appointment.get_project_type_display()
                except Exception:
                    service = self.appointment.project_type
            client = (self.appointment.customer_name or '').strip() or (self.appointment.phone_number or '').strip()
        else:
            client = ''

        service = service or 'Service'
        client = client or 'Unknown Client'
        return f"{service} for {client}"

    @staticmethod
    def _safe_decimal(value):
        """Convert potentially dirty numeric values to Decimal safely."""
        if value in (None, ''):
            return Decimal('0.00')
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value).replace(',', ''))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal('0.00')
    
    def save(self, *args, **kwargs):
        _inherit_tenant(self, getattr(self, 'appointment', None))
        is_new = self.pk is None

        # Generate quotation number if not exists
        if not self.quotation_number:
            today = timezone.localdate()
            quote_count = Quotation.objects.filter(
                created_at__date=today
            ).count() + 1
            self.quotation_number = f"Q{today.strftime('%Y%m%d')}{quote_count:03d}"
        
        # Calculate total
        if not is_new:
            # Existing quotation - can access items
            items_total = sum((item.total_price for item in self.items.all()), Decimal('0.00'))
        else:
            # New quotation - persist first so PK exists, then update totals
            super().save(*args, **kwargs)
            items_total = Decimal('0.00')
            kwargs = kwargs.copy()
            kwargs.pop('force_insert', None)
            kwargs.pop('force_update', None)

        labor = self._safe_decimal(self.labor_cost)
        materials = self._safe_decimal(self.materials_cost)
        transport = self._safe_decimal(self.transport_cost)

        self.labor_cost = labor
        self.materials_cost = materials
        self.transport_cost = transport
        self.total_amount = items_total + labor + materials + transport
        
        super().save(*args, **kwargs)


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
        return f"{self.description} - US${self.total_price}"



class QuotationTemplate(models.Model):
    """Template for creating quotations quickly"""
    tenant = _tenant_fk()
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
            tenant=self.tenant,  # a copy stays with its owner (Phase 3.1)
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
        return f"{self.description} - US${self.unit_price} x {self.quantity}"
    
    def get_line_total(self):
        """Calculate line total"""
        return self.quantity * self.unit_price


class TestScenario(models.Model):
    """A conversation test scenario runnable from the Scenario Lab web page
    (or the run_scenarios CLI). Content uses the scenario text format parsed by
    bot/scenario_runner.py: '>' customer messages with per-turn 'expect:' /
    'reject:' assertion lines. Stored in the DB so use cases added from the
    browser survive Railway redeploys."""
    tenant = _tenant_fk()
    # Unique PER TENANT (Phase 5): the golden pack is cloned to every new
    # tenant under the same names, so global uniqueness would break cloning.
    name = models.CharField(max_length=120)
    category = models.CharField(
        max_length=60, default='General',
        help_text="Grouping shown in the Scenario Lab (e.g. Pricing, Booking flow, Objections)",
    )
    description = models.CharField(max_length=300, blank=True, default='')
    content = models.TextField(
        help_text="Scenario text: '>' customer lines, 'expect:'/'reject:' assertions",
    )
    is_active = models.BooleanField(default=True)
    # Result of the most recent run: {passed, failed, ran_at, turns: [...]}
    last_result = models.JSONField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'name']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'name'],
                                    name='uniq_scenario_per_tenant'),
        ]

    def __str__(self):
        return f"{self.category} / {self.name}"

    @property
    def last_status(self):
        """'pass' | 'fail' | 'never'"""
        if not self.last_result:
            return 'never'
        return 'fail' if self.last_result.get('failed') else 'pass'
