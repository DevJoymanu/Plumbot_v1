from django.contrib import admin
from django.urls import path
from . import views
from .views import (
    DashboardView, AppointmentsListView, AppointmentDetailView,
    settings_view, calendar_settings_view, ai_settings_view,
    update_appointment, send_followup, confirm_appointment,
    cancel_appointment, test_whatsapp, export_appointments, CalendarView, handle_whatsapp_media,
    # Import the new document views
    AppointmentDocumentsView, download_document,
    # Import the new quotation views
    CreateQuotationView, ViewQuotationView, EditQuotationView, send_quotation,create_quotation_api,
    # Import job scheduling views
    complete_site_visit, schedule_job, job_appointments_list, update_job_status, reschedule_job,
    login_view, logout_view, profile_view, change_password_view,appointment_detail_api,
    # Import quotation template views
    QuotationTemplatesListView, CreateQuotationTemplateView,CreateQuotationView, EditQuotationTemplateView,
    QuotationTemplateDetailView, duplicate_template, delete_template, 
    use_template, toggle_template_status, quotation_templates_api, template_items_api
)

from django.conf import settings
from django.conf.urls.static import static
from .whatsapp_webhook import whatsapp_webhook



urlpatterns = [
    path('admin/', admin.site.urls),

    #path('webhook/', bot, name='whatsapp_webhook'),
    path('webhook/', whatsapp_webhook, name='whatsapp_webhook'),

    # Authentication URLs
    path('', login_view, name='login'),
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('profile/', profile_view, name='profile'),
    path('change-password/', change_password_view, name='change_password'),

    # Dashboard
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    
    # Appointments
    path('appointments/', AppointmentsListView.as_view(), name='appointments_list'),
    path('appointments/<int:pk>/', AppointmentDetailView.as_view(), name='appointment_detail'),
    path('appointments/<int:pk>/update/', update_appointment, name='update_appointment'),
    path('appointments/<int:pk>/followup/', send_followup, name='send_followup'),
    path('appointments/<int:pk>/confirm/', confirm_appointment, name='confirm_appointment'),
    path('appointments/<int:pk>/cancel/', cancel_appointment, name='cancel_appointment'),

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
    path('appointments/<int:pk>/download/<str:document_type>/', download_document, name='download_document'),
    
    path('api/quotations/create/', create_quotation_api, name='create_quotation_api'),
    
    # Quotation Views
    path('appointments/<int:pk>/create-quotation/', CreateQuotationView.as_view(), name='create_quotation'),
    path('quotations/<int:pk>/', ViewQuotationView.as_view(), name='view_quotation'),
    path('quotations/<int:pk>/edit/', EditQuotationView.as_view(), name='edit_quotation'),
    path('quotations/<int:pk>/send/', send_quotation, name='send_quotation'),

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

    # Manual vs Automatic Follow-up Management
    path('appointments/<int:pk>/send-followup/', viewssend_followup, name='send_followup'),
    path('appointments/<int:pk>/pause-auto-followup/', views.pause_auto_followup, name='pause_auto_followup'),
    path('appointments/<int:pk>/resume-auto-followup/', views.resume_auto_followup, name='resume_auto_followup'),
    path('bulk-followup/', views.send_bulk_followup, name='send_bulk_followup'),

]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)