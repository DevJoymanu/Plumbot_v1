from django.db.utils import OperationalError, ProgrammingError


NAV_MAP = {
    "dashboard": "dashboard",
    "appointments_list": "appointments",
    "appointment_detail": "appointments",
    "priority_leads": "leads",
    "update_priority_lead_card": "leads",
    "followup_dashboard": "followups",
    "manual_followup_check": "followups",
    "job_appointments_list": "jobs",
    "schedule_job": "jobs",
    "reschedule_job": "jobs",
    "quotation_templates_list": "templates",
    "quotation_template_detail": "templates",
    "create_quotation_template": "templates",
    "edit_quotation_template": "templates",
    "duplicate_template": "templates",
    "delete_template": "templates",
    "standalone_quotation": "new_quote",
    "create_quotation_standalone": "new_quote",
    "settings": "settings",
    "calendar_settings": "settings",
    "ai_settings": "settings",
    "profile": "profile",
    "change_password": "profile",
}


def plumbot_shell(request):
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", "") or ""

    counts = {
        "active_nav": NAV_MAP.get(url_name, ""),
        "hot_lead_count": 0,
        "pending_followup_count": 0,
    }

    try:
        # Use the same canonical definitions as the dashboard so the nav badges
        # match every other surface: hot leads = priority leads from the last week
        # that haven't booked; follow-ups = leads actually DUE to be contacted now
        # (mirrors the send_followups cron), not every follow_up_status='pending'.
        from .views.dashboard import priority_lead_count, _due_followup_leads
        _tenant = getattr(request, "tenant", None)
        counts["hot_lead_count"] = priority_lead_count(_tenant)
        counts["pending_followup_count"] = len(_due_followup_leads(tenant=_tenant))
    except (OperationalError, ProgrammingError):
        pass

    # Tenant switcher (superusers only — plan §3.4). request.tenant is pinned
    # by TenantMiddleware; the switcher lists the alternatives.
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_superuser", False):
        try:
            from .models import Tenant
            counts["platform_tenants"] = list(
                Tenant.objects.filter(is_active=True).order_by("name")
            )
        except (OperationalError, ProgrammingError):
            pass

    return counts
