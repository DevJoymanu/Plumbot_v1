from django.db.utils import OperationalError, ProgrammingError

from .models import Appointment, LeadFollowUpStatus


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
        # Use the same canonical priority-leads definition as the dashboard and
        # the priority-leads page (computed status), so the nav badge matches them.
        from .views.dashboard import priority_lead_count
        counts["hot_lead_count"] = priority_lead_count()
        counts["pending_followup_count"] = Appointment.objects.filter(
            is_lead_active=True,
            follow_up_status=LeadFollowUpStatus.PENDING,
        ).count()
    except (OperationalError, ProgrammingError):
        pass

    return counts
