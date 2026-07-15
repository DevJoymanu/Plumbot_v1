from django.contrib import admin
from django.urls import path
from . import views
from .views import platform as platform_views
from .views import (
    DashboardView, AppointmentsListView, AppointmentDetailView, PriorityLeadsView,
    settings_view, calendar_settings_view, ai_settings_view,
    update_appointment, send_followup, confirm_appointment,
    complete_lead_appointment,
    cancel_appointment, unbook_appointment, test_whatsapp, export_appointments, CalendarView, handle_whatsapp_media,
    # Import the new document views
    AppointmentDocumentsView, download_document, serve_document,
    # Import the new quotation views
    CreateQuotationView, ViewQuotationView, EditQuotationView, send_quotation,create_quotation_api,
    QuotationsListView, StandaloneQuotationView, create_standalone_quotation_api, appointment_search_api,
    quotation_detail_api, duplicate_quotation, delete_quotation,
    # Import job scheduling views
    complete_site_visit, schedule_job, job_appointments_list, update_job_status, reschedule_job,
    login_view, logout_view, profile_view, change_password_view,appointment_detail_api,
    pause_chatbot, resume_chatbot,
    # Import quotation template views
    QuotationTemplatesListView, CreateQuotationTemplateView,CreateQuotationView, EditQuotationTemplateView,
    QuotationTemplateDetailView, duplicate_template, delete_template, 
    use_template, toggle_template_status, quotation_templates_api, template_items_api, send_image_to_lead, send_portfolio_to_lead, send_pdf_to_lead,
)

from django.conf import settings
from django.conf.urls.static import static
from .whatsapp_webhook import whatsapp_webhook



urlpatterns = [
    path('admin/', admin.site.urls),

    #path('webhook/', bot, name='whatsapp_webhook'),
    path('webhook/', whatsapp_webhook, name='whatsapp_webhook'),

    # Public click-to-call bridge for the portfolio PDF's Call button.
    path('call/', views.call_redirect, name='call_redirect'),

    # Authentication URLs
    path('', login_view, name='login'),
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('profile/', profile_view, name='profile'),
    path('change-password/', change_password_view, name='change_password'),

    # Dashboard
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),

    # Conversations (lead inbox — formerly the appointments list)
    path('conversations/', views.ConversationsView.as_view(), name='conversations_list'),
    path('conversations/<int:pk>/', views.ConversationDetailView.as_view(), name='conversation_detail'),

    # Appointments (month calendar + booked list)
    path('appointments/', AppointmentsListView.as_view(), name='appointments_list'),
    path('leads/priority/', PriorityLeadsView.as_view(), name='priority_leads'),
    path('leads/priority/<int:pk>/update/', views.update_priority_lead_card, name='update_priority_lead_card'),
    path('appointments/<int:pk>/', AppointmentDetailView.as_view(), name='appointment_detail'),
    path('appointments/<int:pk>/update/', update_appointment, name='update_appointment'),
    path('appointments/<int:pk>/confirm/', confirm_appointment, name='confirm_appointment'),
    path('appointments/<int:pk>/complete-lead/', complete_lead_appointment, name='complete_lead_appointment'),
    path('appointments/<int:pk>/cancel/', cancel_appointment, name='cancel_appointment'),
    path('appointments/<int:pk>/unbook/', unbook_appointment, name='unbook_appointment'),
    path('appointments/<int:pk>/send-image/', views.send_image_to_lead, name='send_image_to_lead'),
    path('appointments/<int:pk>/send-portfolio/',views.send_portfolio_to_lead,name='send_portfolio_to_lead'),
    path('appointments/<int:pk>/send-pdf/', views.send_pdf_to_lead, name='send_pdf_to_lead'),

        # API endpoints for fetching appointment data
    path('api/appointments/<int:appointment_id>/', appointment_detail_api, name='appointment_detail_api'),

    
    # Job scheduling URLs
    path('appointments/<int:pk>/complete-site-visit/', complete_site_visit, name='complete_site_visit'),
    path('appointments/<int:pk>/schedule-job/', schedule_job, name='schedule_job'),
    path('jobs/', job_appointments_list, name='job_appointments_list'),
    path('jobs/<int:pk>/update-status/', update_job_status, name='update_job_status'),
    path('jobs/<int:pk>/reschedule/', reschedule_job, name='reschedule_job'),
    
    # Document Routes
    path('appointments/<int:pk>/documents/', AppointmentDocumentsView.as_view(), name='appointment_documents'),
    path('appointments/<int:pk>/documents/file/<int:idx>/', serve_document, name='appointment_document_file'),
    path('appointments/<int:pk>/download/<str:document_type>/', download_document, name='download_document'),
    
    path('api/quotations/create/', create_quotation_api, name='create_quotation_api'),
    path('api/quotations/<int:pk>/', quotation_detail_api, name='quotation_detail_api'),
    
    # Quotation Views
    path('quotations/', QuotationsListView.as_view(), name='quotations_list'),
    path('appointments/<int:pk>/create-quotation/', CreateQuotationView.as_view(), name='create_quotation'),
    path('quotations/create/', CreateQuotationView.as_view(), name='create_quotation_standalone'),
    path('quotations/<int:pk>/', ViewQuotationView.as_view(), name='view_quotation'),
    path('quotations/<int:pk>/preview/', ViewQuotationView.as_view(), name='preview_quotation'),
    path('quotations/<int:pk>/edit/', EditQuotationView.as_view(), name='edit_quotation'),
    path('quotations/<int:pk>/send/', send_quotation, name='send_quotation'),
    path('quotations/<int:pk>/duplicate/', duplicate_quotation, name='duplicate_quotation'),
    path('quotations/<int:pk>/delete/', delete_quotation, name='delete_quotation'),
    path('quotations/new/', StandaloneQuotationView.as_view(), name='standalone_quotation'),
    path('api/quotations/create-standalone/', create_standalone_quotation_api, name='create_standalone_quotation_api'),
    path('api/appointments/search/', appointment_search_api, name='appointment_search_api'),


    # ===== NEW: Quotation Templates URLs =====
    path('templates/', QuotationTemplatesListView.as_view(), name='quotation_templates_list'),
    path('templates/create/', CreateQuotationTemplateView.as_view(), name='create_quotation_template'),
    path('templates/<int:pk>/', QuotationTemplateDetailView.as_view(), name='quotation_template_detail'),
    path('templates/<int:pk>/edit/', EditQuotationTemplateView.as_view(), name='edit_quotation_template'),
    path('templates/<int:pk>/duplicate/', duplicate_template, name='duplicate_template'),
    path('templates/<int:pk>/delete/', delete_template, name='delete_template'),
    path('templates/<int:template_pk>/use/', use_template, name='use_template'),
    path('templates/<int:template_pk>/use/<int:appointment_pk>/', use_template, name='use_template_for_appointment'),
    path('templates/<int:pk>/toggle-status/', toggle_template_status, name='toggle_template_status'),
    
    # Template API endpoint
    path('api/quotation-templates/', quotation_templates_api, name='quotation_templates_api'),

    # Settings
    path('settings/', settings_view, name='settings'),
    path('settings/calendar/', calendar_settings_view, name='calendar_settings'),
    path('settings/ai/', ai_settings_view, name='ai_settings'),
    
    # Tools
    path('test-whatsapp/', test_whatsapp, name='test_whatsapp'),
    path('export-appointments/', export_appointments, name='export_appointments'),

    # Platform console (multi-tenant, §3.4) — superuser-only operator screens
    path('platform/', platform_views.platform_console, name='platform_console'),
    path('platform/switch-tenant/', platform_views.switch_tenant, name='switch_tenant'),
    path('platform/tenants/create/', platform_views.platform_create_tenant, name='platform_create_tenant'),
    path('platform/tenants/<slug:slug>/toggle/', platform_views.platform_toggle_tenant, name='platform_toggle_tenant'),
    path('platform/tenants/<slug:slug>/config/', platform_views.platform_tenant_config, name='platform_tenant_config'),

    # Web message-testing console (chat with the bot without a real device)
    path('test-console/', views.test_console_view, name='test_console'),
    path('test-console/send/', views.test_console_send, name='test_console_send'),
    path('test-console/poll/', views.test_console_poll, name='test_console_poll'),
    path('test-console/reset/', views.test_console_reset, name='test_console_reset'),

    # Test leads (999-prefixed console/scenario lines) — staff-only, URL-only,
    # behind the test-console password gate; excluded from the appointments page.
    path('test-leads/', views.test_leads_view, name='test_leads'),
    path('test-leads/purge/', views.test_leads_purge, name='test_leads_purge'),

    # Scenario Lab — run the conversation scenario suite from the browser
    path('scenario-lab/', views.scenario_lab_view, name='scenario_lab'),
    path('scenario-lab/run/', views.scenario_lab_run, name='scenario_lab_run'),
    path('scenario-lab/status/', views.scenario_lab_status, name='scenario_lab_status'),
    path('scenario-lab/save/', views.scenario_lab_save, name='scenario_lab_save'),
    path('scenario-lab/delete/', views.scenario_lab_delete, name='scenario_lab_delete'),
    path('scenario-lab/<int:pk>/', views.scenario_lab_detail, name='scenario_lab_detail'),

    path('calendar/', CalendarView.as_view(), name='calendar'),
    path('media/', handle_whatsapp_media, name='whatsapp_media'),

        # Template Items API
    path('api/quotation-templates/<int:template_id>/items/', 
         template_items_api, 
         name='template_items_api'),

    # Follow-up Management URLs
    path('followups/', views.followup_dashboard, name='followup_dashboard'),
    path('followups/check/', views.manual_followup_check, name='manual_followup_check'),
    path('appointments/<int:pk>/mark-inactive/', views.mark_lead_inactive, name='mark_lead_inactive'),
    path('appointments/<int:pk>/reactivate/', views.reactivate_lead, name='reactivate_lead'),
    path('appointments/<int:pk>/test-followup/', views.test_followup_message, name='test_followup_message'),
    path('followups/test-email/', views.test_followup_email, name='test_followup_email'),
    path('followups/test-suite/', views.followup_test_suite, name='followup_test_suite'),

    # Manual vs Automatic Follow-up Management
    path('appointments/<int:pk>/send-followup/', views.send_followup, name='send_followup'),
    path('appointments/<int:pk>/edit-followup-log/', views.edit_followup_log, name='edit_followup_log'),
    path('appointments/<int:pk>/update-followup-schedule/', views.update_followup_schedule, name='update_followup_schedule'),
    path('appointments/<int:pk>/schedule-followup/', views.schedule_followup, name='schedule_followup'),
    path('scheduled-followups/<int:sf_id>/edit/', views.edit_scheduled_followup, name='edit_scheduled_followup'),
    path('scheduled-followups/<int:sf_id>/cancel/', views.cancel_scheduled_followup, name='cancel_scheduled_followup'),
    path('appointments/<int:pk>/schedule-reminder/', views.schedule_reminder, name='schedule_reminder'),
    path('scheduled-reminders/<int:r_id>/edit/', views.edit_scheduled_reminder, name='edit_scheduled_reminder'),
    path('scheduled-reminders/<int:r_id>/cancel/', views.cancel_scheduled_reminder, name='cancel_scheduled_reminder'),
    path('appointments/<int:pk>/email-preview/', views.lead_email_preview, name='lead_email_preview'),
    path('appointments/<int:pk>/email-edit-data/', views.lead_email_edit_data, name='lead_email_edit_data'),
    path('appointments/<int:pk>/send-catalog-emails/', views.lead_send_catalog_emails, name='lead_send_catalog_emails'),
    path('appointments/<int:pk>/schedule-catalog-emails/', views.lead_schedule_catalog_emails, name='lead_schedule_catalog_emails'),
    path('appointments/<int:pk>/send-email-now/', views.lead_send_email_now, name='lead_send_email_now'),
    path('appointments/<int:pk>/update-email/', views.update_lead_email, name='update_lead_email'),
    path('appointments/<int:pk>/pause-bot/', pause_chatbot, name='pause_chatbot'),
    path('appointments/<int:pk>/resume-bot/', resume_chatbot, name='resume_chatbot'),
    path('appointments/<int:pk>/pause-auto-followup/', views.pause_auto_followup, name='pause_auto_followup'),
    path('appointments/<int:pk>/resume-auto-followup/', views.resume_auto_followup, name='resume_auto_followup'),
    path('bulk-followup/', views.send_bulk_followup, name='send_bulk_followup'),

]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
