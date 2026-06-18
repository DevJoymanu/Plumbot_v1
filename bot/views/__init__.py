# Public surface area for bot.views — every name imported by urls.py or
# external code is re-exported here so no other file needs to change.

from .quotation_templates import (
    quotation_templates_api,
    StandaloneQuotationView,
    create_standalone_quotation_api,
    appointment_search_api,
    QuotationTemplatesListView,
    CreateQuotationTemplateView,
    EditQuotationTemplateView,
    QuotationTemplateDetailView,
    duplicate_template,
    delete_template,
    use_template,
    toggle_template_status,
    appointment_detail_api,
    template_items_api,
)

from .quotations import (
    CreateQuotationView,
    create_quotation_api,
    ViewQuotationView,
    QuotationsListView,
    quotation_detail_api,
    EditQuotationView,
    duplicate_quotation,
    delete_quotation,
    send_quotation,
    format_quotation_message,
    build_quotation_pdf_file,
)

from .dashboard import DashboardView

from .public_call import call_redirect

from .appointments import (
    AppointmentsListView,
    PriorityLeadsView,
    update_priority_lead_card,
    AppointmentDetailView,
    AppointmentDocumentsView,
    download_document,
    update_appointment,
    confirm_appointment,
    complete_lead_appointment,
    cancel_appointment,
    export_appointments,
    complete_site_visit,
    handle_whatsapp_media,
    download_and_save_media,
)

from .settings_views import (
    settings_view,
    calendar_settings_view,
    ai_settings_view,
    test_whatsapp,
)

from .jobs import (
    schedule_job,
    update_job_status,
    check_job_availability,
    send_job_appointment_notifications,
    send_job_status_update_notification,
    reschedule_job,
    send_job_reschedule_notification,
    job_appointments_list,
)

from .calendar_views import (
    CalendarView,
    appointment_data,
    map_project_type_to_service_key,
)

from .followups import (
    followup_dashboard,
    mark_lead_inactive,
    reactivate_lead,
    test_followup_message,
    test_followup_email,
    followup_test_suite,
    manual_followup_check,
    pause_chatbot,
    resume_chatbot,
    pause_auto_followup,
    resume_auto_followup,
    send_followup,
    send_portfolio_to_lead,
    send_image_to_lead,
    send_pdf_to_lead,
    send_bulk_followup,
    edit_followup_log,
    update_followup_schedule,
)

from .plumbot import Plumbot

# Auth views
from ..auth_views import login_view, logout_view, profile_view, change_password_view

# Shared clients re-exported for management commands that do:
#   from bot.views import twilio_client, TWILIO_WHATSAPP_NUMBER
from ..services.clients import (
    twilio_client,
    deepseek_client,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_NUMBER,
    ACCOUNT_SID,
    AUTH_TOKEN,
    GOOGLE_CALENDAR_CREDENTIALS,
    DEEPSEEK_API_KEY,
)

# Utilities re-exported for any code that imports from bot.views
from ..utils import (
    _to_decimal,
    _to_float,
    _safe_logo_url,
    _safe_logo_data_uri,
    _reset_pk_sequence,
    _append_admin_note,
    clean_phone_number,
    format_phone_number_for_storage,
)
