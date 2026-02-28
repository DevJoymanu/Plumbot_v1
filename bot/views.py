# Update the imports section in your views.py file

from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect
from twilio.rest import Client
from .models import (
    Appointment,
    Quotation,
    QuotationItem,
    QuotationTemplate,
    QuotationTemplateItem,
)
import requests
import datetime
import pytz
import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from django.conf import settings
from openai import OpenAI
import re
from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, TemplateView, CreateView, UpdateView, DetailView
from django.contrib import messages
from django.urls import reverse
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.db.models import Count
from django.conf import settings
from django.forms import modelformset_factory
from .models import Appointment, ConversationMessage
from .forms import AppointmentForm, SettingsForm, CalendarSettingsForm, AISettingsForm, QuotationForm, QuotationItemFormSet, QuotationTemplateForm, QuotationTemplateItemFormSet
from datetime import datetime, timedelta
from django.utils import timezone
from django.views import View
import os
import requests
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.contrib import messages
from django.urls import reverse
from django.views.decorators.http import require_http_methods

# Import our custom decorators and authentication views
from .decorators import staff_required, anonymous_required, StaffRequiredMixin
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth import update_session_auth_hash

from django.views.generic import ListView, CreateView, UpdateView, DeleteView, DetailView
from django.contrib import messages
from django.urls import reverse, reverse_lazy
from django.db.models import Q
from .decorators import staff_required
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_GET
from .whatsapp_cloud_api import whatsapp_api
from .services.lead_scoring import refresh_lead_score, calculate_lead_score

import logging
logger = logging.getLogger(__name__)


def _append_admin_note(appointment, message):
    timestamp = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M')
    existing = appointment.admin_notes or ''
    appointment.admin_notes = f"[{timestamp}] {message}\n{existing}".strip()
    appointment.save(update_fields=['admin_notes'])




# Helper function for phone number formatting
def clean_phone_number(phone):
    """Convert phone number to WhatsApp Cloud API format (no prefix, no +)"""
    return phone.replace('whatsapp:', '').replace('+', '').strip()

def format_phone_number_for_storage(phone):
    """Format phone number for database storage (keep whatsapp: prefix for compatibility)"""
    if not phone.startswith('whatsapp:'):
        # If it's just numbers, add whatsapp:+
        return f"whatsapp:+{phone}"
    return phone


@anonymous_required
def login_view(request):
    """Handle user login"""
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            
            if user is not None:
                # Check if user is staff
                if user.is_staff:
                    login(request, user)
                    messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
                    
                    # Redirect to next parameter or dashboard
                    next_page = request.GET.get('next', 'dashboard')
                    return HttpResponseRedirect(next_page)
                else:
                    messages.error(request, 'Staff access required. Contact your administrator.')
            else:
                messages.error(request, 'Invalid username or password.')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = AuthenticationForm()
    
    return render(request, 'registration/login.html', {
        'form': form,
        'title': 'Staff Login'
    })


@login_required
def logout_view(request):
    """Handle user logout"""
    user_name = request.user.get_full_name() or request.user.username
    logout(request)
    messages.info(request, f'You have been logged out. Thank you, {user_name}!')
    return redirect('login')


@staff_required
def profile_view(request):
    """Display user profile"""
    context = {
        'user': request.user,
        'title': 'My Profile'
    }
    return render(request, 'registration/profile.html', context)


@staff_required
def change_password_view(request):
    """Handle password change"""
    from django.contrib.auth.forms import PasswordChangeForm
    from django.contrib.auth import update_session_auth_hash
    
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # Keep user logged in
            messages.success(request, 'Your password was successfully updated!')
            return redirect('profile')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = PasswordChangeForm(request.user)
    
    return render(request, 'registration/change_password.html', {
        'form': form,
        'title': 'Change Password'
    })

# ====== Twilio Setup ======
import os

# Load environment variables
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')




# Initialize clients
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")

# Initialize OpenAI client for DeepSeek
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"
)

# ====== Google Calendar Setup ======
# Add your Google Calendar credentials here
GOOGLE_CALENDAR_CREDENTIALS = {
    # Your service account credentials
}


# Decorator to ensure user is logged in
staff_required = login_required



@staff_required
@require_GET
def quotation_templates_api(request):
    """API endpoint to fetch templates for the selector modal"""
    try:
        # Get query parameters
        project_type = request.GET.get('project_type')
        search = request.GET.get('search')
        active_only = request.GET.get('active_only', 'true').lower() == 'true'
        
        # Build queryset
        templates = QuotationTemplate.objects.all()
        
        if active_only:
            templates = templates.filter(is_active=True)
        
        if project_type:
            templates = templates.filter(project_type=project_type)
        
        if search:
            templates = templates.filter(
                Q(name__icontains=search) | 
                Q(description__icontains=search)
            )
        
        # Order by usage and recency
        templates = templates.order_by('-use_count', '-updated_at')
        
        # Serialize templates
        templates_data = []
        for template in templates:
            templates_data.append({
                'id': template.id,
                'name': template.name,
                'description': template.description,
                'project_type': template.project_type,
                'project_type_display': template.get_project_type_display(),
                'items_count': template.items.count(),
                'use_count': template.use_count,
                'estimated_cost': float(template.get_total_estimated_cost()),
                'labor_cost': float(template.default_labor_cost),
                'transport_cost': float(template.default_transport_cost),
                'is_active': template.is_active,
                'created_at': template.created_at.isoformat(),
                'updated_at': template.updated_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'templates': templates_data,
            'count': len(templates_data)
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@method_decorator(staff_required, name='dispatch')
class QuotationTemplatesListView(ListView):
    """List all quotation templates"""
    model = QuotationTemplate
    template_name = 'quotation_templates_list.html'
    context_object_name = 'templates'
    paginate_by = 20
    
    def get_queryset(self):
        queryset = QuotationTemplate.objects.all()
        
        # Filter by project type
        project_type = self.request.GET.get('project_type')
        if project_type:
            queryset = queryset.filter(project_type=project_type)
        
        # Filter by active status
        status = self.request.GET.get('status')
        if status == 'active':
            queryset = queryset.filter(is_active=True)
        elif status == 'inactive':
            queryset = queryset.filter(is_active=False)
        
        # Search
        search = self.request.GET.get('search')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | 
                Q(description__icontains=search)
            )
        
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['total_templates'] = QuotationTemplate.objects.count()
        context['active_templates'] = QuotationTemplate.objects.filter(is_active=True).count()
        return context


@method_decorator(staff_required, name='dispatch')
class CreateQuotationTemplateView(CreateView):
    """Create a new quotation template"""
    model = QuotationTemplate
    form_class = QuotationTemplateForm
    template_name = 'create_quotation_template.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['formset'] = QuotationTemplateItemFormSet(self.request.POST)
        else:
            context['formset'] = QuotationTemplateItemFormSet()
        return context
    
    def form_valid(self, form):
        context = self.get_context_data()
        formset = context['formset']
        
        form.instance.created_by = self.request.user
        
        if formset.is_valid():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            
            messages.success(self.request, f'Template "{self.object.name}" created successfully!')
            return redirect('quotation_templates_list')
        else:
            return self.render_to_response(self.get_context_data(form=form))
    
    def get_success_url(self):
        return reverse('quotation_templates_list')


@method_decorator(staff_required, name='dispatch')
class EditQuotationTemplateView(UpdateView):
    """Edit an existing quotation template"""
    model = QuotationTemplate
    form_class = QuotationTemplateForm
    template_name = 'edit_quotation_template.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['formset'] = QuotationTemplateItemFormSet(self.request.POST, instance=self.object)
        else:
            context['formset'] = QuotationTemplateItemFormSet(instance=self.object)
        return context
    
    def form_valid(self, form):
        context = self.get_context_data()
        formset = context['formset']
        
        if formset.is_valid():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            
            messages.success(self.request, f'Template "{self.object.name}" updated successfully!')
            return redirect('quotation_templates_list')
        else:
            return self.render_to_response(self.get_context_data(form=form))
    
    def get_success_url(self):
        return reverse('quotation_templates_list')


@method_decorator(staff_required, name='dispatch')
class QuotationTemplateDetailView(DetailView):
    """View template details"""
    model = QuotationTemplate
    template_name = 'quotation_template_detail.html'
    context_object_name = 'template'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['items'] = self.object.items.all()
        context['total_cost'] = self.object.get_total_estimated_cost()
        return context


@staff_required
def duplicate_template(request, pk):
    """Duplicate an existing template"""
    template = get_object_or_404(QuotationTemplate, pk=pk)
    
    if request.method == 'POST':
        new_name = request.POST.get('new_name', f"{template.name} (Copy)")
        new_template = template.duplicate(new_name=new_name)
        
        messages.success(request, f'Template duplicated as "{new_template.name}"')
        return redirect('edit_quotation_template', pk=new_template.pk)
    
    return render(request, 'duplicate_template.html', {
        'template': template
    })


@staff_required
def delete_template(request, pk):
    """Delete a template"""
    template = get_object_or_404(QuotationTemplate, pk=pk)
    
    if request.method == 'POST':
        template_name = template.name
        template.delete()
        messages.success(request, f'Template "{template_name}" deleted successfully')
        return redirect('quotation_templates_list')
    
    return render(request, 'delete_template.html', {
        'template': template
    })


@staff_required
def use_template(request, template_pk, appointment_pk=None):
    """Create a quotation from a template"""
    template = get_object_or_404(QuotationTemplate, pk=template_pk)
    
    # Increment use count
    template.use_count += 1
    template.save()
    
    # Create new quotation from template
    quotation = Quotation.objects.create(
        appointment_id=appointment_pk if appointment_pk else None,
        labor_cost=template.default_labor_cost,
        materials_cost=sum(item.get_line_total() for item in template.items.filter(category='materials')),
        notes=f"Created from template: {template.name}\n\n{template.description}",
        status='draft'
    )
    
    # Copy items from template
    for template_item in template.items.all():
        QuotationItem.objects.create(
            quotation=quotation,
            description=template_item.description,
            quantity=template_item.quantity,
            unit_price=template_item.unit_price
        )
    
    messages.success(request, f'Quotation created from template "{template.name}"')
    return redirect('edit_quotation', pk=quotation.pk)


@staff_required
def toggle_template_status(request, pk):
    """Toggle template active status"""
    if request.method == 'POST':
        template = get_object_or_404(QuotationTemplate, pk=pk)
        template.is_active = not template.is_active
        template.save()
        
        status_text = "activated" if template.is_active else "deactivated"
        messages.success(request, f'Template "{template.name}" {status_text}')
        
        return JsonResponse({
            'success': True,
            'is_active': template.is_active
        })
    
    return JsonResponse({'success': False}, status=400)


@csrf_exempt
@require_http_methods(["POST"])
@staff_required
def appointment_detail_api(request, appointment_id):
    """API endpoint to get appointment details"""
    try:
        appointment = get_object_or_404(Appointment, id=appointment_id)
        
        data = {
            'id': appointment.id,
            'customer_name': appointment.customer_name or '',
            'customer_email': appointment.customer_email or '',
            'phone_number': appointment.phone_number or '',
            'customer_area': appointment.customer_area or '',
            'project_type': appointment.project_type or '',
            'property_type': appointment.property_type or '',
            'house_stage': appointment.house_stage or '',
            'project_description': appointment.project_description or '',
            'timeline': appointment.timeline or '',
            'plan_file': appointment.plan_file or '',
            'plan_file_urls': appointment.all_plan_file_urls,   # list of all URLs
            'document_count': appointment.uploaded_file_count,  # correct count
        }
        
        return JsonResponse(data)
        
    except Exception as e:
        return JsonResponse({
            'error': str(e)
        }, status=404)


@staff_required
def use_template(request, template_pk, appointment_pk=None):
    """Create a quotation from a template - ENHANCED"""
    template = get_object_or_404(QuotationTemplate, pk=template_pk)
    
    # Increment use count
    template.use_count += 1
    template.save()
    
    # Get appointment if provided
    appointment = None
    if appointment_pk:
        appointment = get_object_or_404(Appointment, pk=appointment_pk)
    
    # Create new quotation from template
    quotation = Quotation.objects.create(
        appointment=appointment,
        labor_cost=template.default_labor_cost,
        transport_cost=template.default_transport_cost,
        materials_cost=0,  # Will be calculated from items
        notes=f"Created from template: {template.name}\n\n{template.description}",
        status='draft'
    )
    
    # Copy items from template
    for template_item in template.items.all():
        QuotationItem.objects.create(
            quotation=quotation,
            description=template_item.description,
            quantity=template_item.quantity,
            unit_price=template_item.unit_price
        )
    
    # Recalculate totals
    quotation.save()
    
    messages.success(request, f'Quotation created from template "{template.name}"')
    
    # Redirect to edit page
    return redirect('edit_quotation', pk=quotation.pk)


@staff_required
def template_items_api(request, template_id):
    """Get template items for loading into quotation form"""
    try:
        template = get_object_or_404(QuotationTemplate, id=template_id)
        
        items = []
        for item in template.items.all():
            items.append({
                'description': item.description,
                'quantity': float(item.quantity),
                'unit_price': float(item.unit_price),
                'category': item.category,
                'is_optional': item.is_optional,
                'notes': item.notes or ''
            })
        
        return JsonResponse({
            'success': True,
            'items': items
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=404)



# Alternative: Update your existing CreateQuotationView to handle both cases
# Replace your CreateQuotationView with this fixed version:

@method_decorator(staff_required, name='dispatch')
class CreateQuotationView(CreateView):
    model = Quotation
    form_class = QuotationForm
    template_name = 'create_quotation.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Get appointment if pk is provided
        appointment = None
        if 'pk' in self.kwargs:
            appointment = get_object_or_404(Appointment, pk=self.kwargs['pk'])
            context['appointment'] = appointment
        
        # Add formset
        if self.request.POST:
            context['formset'] = QuotationItemFormSet(self.request.POST)
        else:
            context['formset'] = QuotationItemFormSet()
        
        return context
    
    def form_valid(self, form):
        context = self.get_context_data()
        formset = context['formset']
        
        # Get appointment if pk provided
        if 'pk' in self.kwargs:
            appointment = get_object_or_404(Appointment, pk=self.kwargs['pk'])
            form.instance.appointment = appointment
        
        if formset.is_valid():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            
            messages.success(self.request, 'Quotation created successfully!')
            
            if 'pk' in self.kwargs:
                return redirect('appointment_detail', pk=self.kwargs['pk'])
            else:
                return redirect('view_quotation', pk=self.object.pk)
        else:
            return self.render_to_response(self.get_context_data(form=form))
    
    def get_success_url(self):
        if 'pk' in self.kwargs:
            return reverse('appointment_detail', kwargs={'pk': self.kwargs['pk']})
        return reverse('view_quotation', kwargs={'pk': self.object.pk})


@csrf_exempt
@require_http_methods(["POST"])
def create_quotation_api(request):
    """API endpoint for creating quotations from the quotation generator page"""
    logger.info("🔹 Received request to create a new quotation")

    try:
        data = json.loads(request.body)
        logger.debug(f"📦 Parsed request data: {data}")

        appointment_id = data.get('appointment_id')
        if not appointment_id:
            logger.error("❌ No appointment_id provided")
            return JsonResponse({'success': False, 'error': 'appointment_id is required'}, status=400)

        try:
            appointment = Appointment.objects.get(id=appointment_id)
        except Appointment.DoesNotExist:
            return JsonResponse({'success': False, 'error': f'Appointment with ID {appointment_id} not found'}, status=404)
        
        quotation = Quotation.objects.create(
            appointment=appointment,
            labor_cost=data.get('labour_cost', 0),
            transport_cost=data.get('transport_cost', 0),
            materials_cost=data.get('materials_cost', 0),
            notes=data.get('notes', ''),
            status='draft'
        )

        items_created = 0
        for item_data in data.get('items', []):
            if item_data.get('name'):
                QuotationItem.objects.create(
                    quotation=quotation,
                    description=item_data.get('name', ''),
                    quantity=item_data.get('qty', 1),
                    unit_price=item_data.get('unit', 0)
                )
                items_created += 1

        quotation.save()

        return JsonResponse({
            'success': True,
            'message': 'Quotation created successfully',
            'quotation_id': quotation.id,
            'quotation_number': quotation.quotation_number,
            'appointment_id': appointment.id,
            'items_created': items_created,
            'total_amount': float(quotation.total_amount)
        })

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@method_decorator(staff_required, name='dispatch')
class ViewQuotationView(DetailView):
    model = Quotation
    template_name = 'view_quotation.html'
    context_object_name = 'quotation'

@method_decorator(staff_required, name='dispatch')
class EditQuotationView(UpdateView):
    model = Quotation
    form_class = QuotationForm
    template_name = 'edit_quotation.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['formset'] = QuotationItemFormSet(self.request.POST, instance=self.object)
        else:
            context['formset'] = QuotationItemFormSet(instance=self.object)
        return context
    
    def form_valid(self, form):
        context = self.get_context_data()
        formset = context['formset']
        
        if formset.is_valid():
            response = super().form_valid(form)
            formset.instance = self.object
            formset.save()
            messages.success(self.request, 'Quotation updated successfully!')
            return response
        else:
            return self.render_to_response(self.get_context_data(form=form))

@staff_required
def send_quotation(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    try:
        message = format_quotation_message(quotation)
        client = Client(ACCOUNT_SID, AUTH_TOKEN)
        whatsapp_message = client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=quotation.appointment.phone_number
        )
        quotation.status = 'sent'
        quotation.sent_via_whatsapp = True
        quotation.sent_at = timezone.now()
        quotation.save()
        ConversationMessage.objects.create(
            appointment=quotation.appointment,
            role='assistant',
            content=f"Quotation #{quotation.quotation_number} sent to customer via WhatsApp",
            timestamp=timezone.now()
        )
        messages.success(request, 'Quotation sent successfully via WhatsApp!')
    except Exception as e:
        messages.error(request, f'Failed to send quotation: {str(e)}')
    return redirect('appointment_detail', pk=quotation.appointment.pk)

def format_quotation_message(quotation):
    items_text = ""
    for i, item in enumerate(quotation.items.all(), 1):
        items_text += f"{i}. {item.description}\n   Qty: {item.quantity} × R{item.unit_price} = R{item.total_price}\n"
    message = f"""🔧 QUOTATION #{quotation.quotation_number}

Dear {quotation.appointment.customer_name or 'Customer'},

Here is your quotation for plumbing services:

{items_text}
---
Labor: R{quotation.labor_cost}
Materials: R{quotation.materials_cost}
TOTAL: R{quotation.total_amount}

📝 Notes:
{quotation.notes or 'No additional notes'}

This quotation is valid for 30 days. To accept, please reply "ACCEPT" or contact us to discuss.

Thank you for considering our services!
- {quotation.plumber.get_full_name() or 'Plumbing Team'}"""
    return message


@method_decorator(staff_required, name='dispatch')
class DashboardView(TemplateView):
    template_name = 'dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.now().date()
        tomorrow = today + timedelta(days=1)
        appointments = Appointment.objects.all()
        context.update({
            'total_appointments': appointments.count(),
            'pending_appointments': appointments.filter(status='pending').count(),
            'confirmed_appointments': appointments.filter(status='confirmed').count(),
            'recent_appointments': appointments.order_by('-created_at')[:5],
            'todays_confirmed_appointments': Appointment.objects.filter(
                status='confirmed', scheduled_datetime__date=today
            ).order_by('scheduled_datetime'),
            'tomorrows_confirmed_appointments': Appointment.objects.filter(
                status='confirmed', scheduled_datetime__date=tomorrow
            ).order_by('scheduled_datetime'),
            'calendar_status': 'Connected' if hasattr(settings, 'GOOGLE_CALENDAR_CREDENTIALS') else 'Not configured'
        })
        return context


@method_decorator(staff_required, name='dispatch')
class AppointmentsListView(ListView):
    template_name = 'appointments_list.html'
    model = Appointment
    context_object_name = 'appointments'
    paginate_by = 20
    ordering = ['-updated_at']

    def get_queryset(self):
        from django.db.models import Case, IntegerField, Q, Value, When
        has_project_type = Case(When(Q(project_type__isnull=False) & ~Q(project_type=''), then=Value(1)), default=Value(0), output_field=IntegerField())
        has_property_type = Case(When(Q(property_type__isnull=False) & ~Q(property_type=''), then=Value(1)), default=Value(0), output_field=IntegerField())
        has_area = Case(When(Q(customer_area__isnull=False) & ~Q(customer_area=''), then=Value(1)), default=Value(0), output_field=IntegerField())
        has_timeline = Case(When(Q(timeline__isnull=False) & ~Q(timeline=''), then=Value(1)), default=Value(0), output_field=IntegerField())
        has_site_visit = Case(When(scheduled_datetime__isnull=False, then=Value(1)), default=Value(0), output_field=IntegerField())
        completed_fields = has_project_type + has_property_type + has_area + has_timeline + has_site_visit
        return (
            Appointment.objects.annotate(
                computed_score=Case(
                    When(scheduled_datetime__isnull=False, then=Value(100)),
                    default=completed_fields * Value(20),
                    output_field=IntegerField(),
                ),
            ).annotate(
                computed_status=Case(
                    When(scheduled_datetime__isnull=False, then=Value('very_hot')),
                    When(computed_score__lte=20, then=Value('cold')),
                    When(computed_score__lte=60, then=Value('warm')),
                    When(computed_score=80, then=Value('hot')),
                    default=Value('very_hot'),
                ),
                computed_status_label=Case(
                    When(scheduled_datetime__isnull=False, then=Value('Very Hot')),
                    When(computed_score__lte=20, then=Value('Cold')),
                    When(computed_score__lte=60, then=Value('Warm')),
                    When(computed_score=80, then=Value('Hot')),
                    default=Value('Very Hot'),
                ),
            ).order_by('-updated_at')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.now().date()
        todays_confirmed_appointments = Appointment.objects.filter(status='confirmed', scheduled_datetime__date=today).order_by('scheduled_datetime')
        context['status_counts'] = {
            'total': Appointment.objects.count(),
            'pending': Appointment.objects.filter(status='pending').count(),
            'confirmed': Appointment.objects.filter(status='confirmed').count(),
            'cancelled': Appointment.objects.filter(status='cancelled').count(),
            'todays_confirmed_appointments': todays_confirmed_appointments,
        }
        return context


@method_decorator(staff_required, name='dispatch')
class PriorityLeadsView(TemplateView):
    template_name = 'priority_leads_dashboard.html'

    def _group_leads_by_date(self, leads_qs):
        from collections import OrderedDict
        grouped = OrderedDict()
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        for lead in leads_qs:
            activity = lead.recent_activity
            if not activity:
                label = "No Activity Date"
            else:
                local_activity = timezone.localtime(activity)
                activity_date = local_activity.date()
                if activity_date == today:
                    label = "Today"
                elif activity_date == yesterday:
                    label = "Yesterday"
                else:
                    label = local_activity.strftime("%b %d, %Y")
            grouped.setdefault(label, []).append(lead)
        return [{"label": label, "leads": items} for label, items in grouped.items()]

    def _recommended_action(self, lead):
        if lead.computed_status == 'very_hot':
            if lead.manual_followup_done:
                return "Finalize booking time and send confirmation details."
            return "Call now and lock in the site visit time."
        if lead.computed_status == 'hot':
            if lead.manual_followup_done:
                return "Follow up on quote details and push to booking."
            return "Call within 30 minutes to complete missing details."
        if lead.computed_status == 'warm':
            if lead.manual_followup_done:
                return "Set a reminder and wait for customer response."
            return "Send a WhatsApp check-in for missing project info."
        if lead.computed_score == 20:
            return "Send a quick nudge to re-engage this lead."
        return "Move to nurture sequence or close as cold lead."

    def _enrich_leads(self, leads_qs):
        leads = list(leads_qs)
        for lead in leads:
            lead.recommended_action = self._recommended_action(lead)
        return leads

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.db.models import Case, F, IntegerField, Q, Value, When
        from django.db.models.functions import Coalesce

        has_project_type = Case(When(Q(project_type__isnull=False) & ~Q(project_type=''), then=Value(1)), default=Value(0), output_field=IntegerField())
        has_property_type = Case(When(Q(property_type__isnull=False) & ~Q(property_type=''), then=Value(1)), default=Value(0), output_field=IntegerField())
        has_area = Case(When(Q(customer_area__isnull=False) & ~Q(customer_area=''), then=Value(1)), default=Value(0), output_field=IntegerField())
        has_timeline = Case(When(Q(timeline__isnull=False) & ~Q(timeline=''), then=Value(1)), default=Value(0), output_field=IntegerField())
        has_site_visit = Case(When(scheduled_datetime__isnull=False, then=Value(1)), default=Value(0), output_field=IntegerField())

        response_age = self.request.GET.get('response_age', '').strip()
        age_map_plus = {'1w': timedelta(weeks=1), '2w': timedelta(weeks=2), '3w': timedelta(weeks=3), '1m': timedelta(days=30)}
        age_map_minus = {'1w_minus': timedelta(weeks=1), '2w_minus': timedelta(weeks=2), '3w_minus': timedelta(weeks=3), '4w_minus': timedelta(weeks=4)}

        leads = (
            Appointment.objects.annotate(
                completed_fields=has_project_type + has_property_type + has_area + has_timeline + has_site_visit,
                computed_score=Case(
                    When(scheduled_datetime__isnull=False, then=Value(100)),
                    default=(has_project_type + has_property_type + has_area + has_timeline + has_site_visit) * Value(20),
                    output_field=IntegerField(),
                ),
            ).annotate(
                computed_status=Case(
                    When(scheduled_datetime__isnull=False, then=Value('very_hot')),
                    When(completed_fields__lte=1, then=Value('cold')),
                    When(completed_fields__lte=3, then=Value('warm')),
                    When(completed_fields=4, then=Value('hot')),
                    default=Value('very_hot'),
                ),
                computed_status_label=Case(
                    When(scheduled_datetime__isnull=False, then=Value('Very Hot')),
                    When(completed_fields__lte=1, then=Value('Cold')),
                    When(completed_fields__lte=3, then=Value('Warm')),
                    When(completed_fields=4, then=Value('Hot')),
                    default=Value('Very Hot'),
                ),
            ).annotate(
                status_rank=Case(
                    When(computed_status='very_hot', then=Value(0)),
                    When(computed_status='hot', then=Value(1)),
                    When(computed_status='warm', then=Value(2)),
                    default=Value(3),
                    output_field=IntegerField(),
                ),
                recent_activity=Coalesce('last_inbound_at', 'updated_at'),
                last_response_at=Coalesce('last_customer_response', 'created_at'),
            )
            .filter(is_lead_active=True)
            .exclude(status__in=['completed', 'cancelled'])
            .order_by('status_rank', F('recent_activity').desc(nulls_last=True), '-computed_score')
        )

        if response_age in age_map_minus:
            cutoff = timezone.now() - age_map_minus[response_age]
            leads = leads.filter(last_response_at__gte=cutoff)
        elif response_age in age_map_plus:
            cutoff = timezone.now() - age_map_plus[response_age]
            leads = leads.filter(last_response_at__lte=cutoff)

        very_hot_leads = leads.filter(computed_status='very_hot')
        hot_leads = leads.filter(computed_status='hot')
        warm_leads = leads.filter(computed_status='warm')
        luke_warm_leads = leads.filter(computed_status='cold', computed_score=20)
        cold_leads = leads.filter(computed_status='cold', computed_score=0)

        sections = [
            {'id': 'sec-vh', 'title': 'Very Hot Leads', 'icon': 'fire', 'css': 'sec-vh', 'status_bg': '#fee2e2', 'status_fg': '#991b1b', 'border': '#dc2626', 'empty_label': 'No very hot leads.', 'recommended_action': 'Call now and lock in the site visit time.', 'count': very_hot_leads.count(), 'pending_count': very_hot_leads.filter(manual_followup_done=False).count(), 'done_count': very_hot_leads.filter(manual_followup_done=True).count(), 'pending_by_date': self._group_leads_by_date(self._enrich_leads(very_hot_leads.filter(manual_followup_done=False))), 'done_by_date': self._group_leads_by_date(self._enrich_leads(very_hot_leads.filter(manual_followup_done=True)))},
            {'id': 'sec-hot', 'title': 'Hot Leads', 'icon': 'exclamation-triangle', 'css': 'sec-hot', 'status_bg': '#fef3c7', 'status_fg': '#92400e', 'border': '#f59e0b', 'empty_label': 'No hot leads.', 'recommended_action': 'Call within 30 minutes to complete missing details.', 'count': hot_leads.count(), 'pending_count': hot_leads.filter(manual_followup_done=False).count(), 'done_count': hot_leads.filter(manual_followup_done=True).count(), 'pending_by_date': self._group_leads_by_date(self._enrich_leads(hot_leads.filter(manual_followup_done=False))), 'done_by_date': self._group_leads_by_date(self._enrich_leads(hot_leads.filter(manual_followup_done=True)))},
            {'id': 'sec-warm', 'title': 'Warm Leads', 'icon': 'sun', 'css': 'sec-warm', 'status_bg': '#d1fae5', 'status_fg': '#065f46', 'border': '#10b981', 'empty_label': 'No warm leads.', 'recommended_action': 'Send a WhatsApp check-in for missing project info.', 'count': warm_leads.count(), 'pending_count': warm_leads.filter(manual_followup_done=False).count(), 'done_count': warm_leads.filter(manual_followup_done=True).count(), 'pending_by_date': self._group_leads_by_date(self._enrich_leads(warm_leads.filter(manual_followup_done=False))), 'done_by_date': self._group_leads_by_date(self._enrich_leads(warm_leads.filter(manual_followup_done=True)))},
            {'id': 'sec-luke', 'title': 'Luke-warm Leads', 'icon': 'temperature-low', 'css': 'sec-luke', 'status_bg': '#dbeafe', 'status_fg': '#1e3a8a', 'border': '#0ea5e9', 'empty_label': 'No luke-warm leads.', 'recommended_action': 'Send a quick nudge to re-engage this lead.', 'count': luke_warm_leads.count(), 'pending_count': luke_warm_leads.filter(manual_followup_done=False).count(), 'done_count': luke_warm_leads.filter(manual_followup_done=True).count(), 'pending_by_date': self._group_leads_by_date(self._enrich_leads(luke_warm_leads.filter(manual_followup_done=False))), 'done_by_date': self._group_leads_by_date(self._enrich_leads(luke_warm_leads.filter(manual_followup_done=True)))},
            {'id': 'sec-cold', 'title': 'Cold Leads', 'icon': 'snowflake', 'css': 'sec-cold', 'status_bg': '#e5e7eb', 'status_fg': '#374151', 'border': '#6b7280', 'empty_label': 'No cold leads.', 'recommended_action': 'Move to nurture sequence or close as cold lead.', 'count': cold_leads.count(), 'pending_count': cold_leads.filter(manual_followup_done=False).count(), 'done_count': cold_leads.filter(manual_followup_done=True).count(), 'pending_by_date': self._group_leads_by_date(self._enrich_leads(cold_leads.filter(manual_followup_done=False))), 'done_by_date': self._group_leads_by_date(self._enrich_leads(cold_leads.filter(manual_followup_done=True)))},
        ]

        context.update({
            'very_hot_leads': very_hot_leads, 'hot_leads': hot_leads, 'warm_leads': warm_leads,
            'luke_warm_leads': luke_warm_leads, 'cold_leads': cold_leads,
            'very_hot_by_date': self._group_leads_by_date(very_hot_leads),
            'hot_by_date': self._group_leads_by_date(hot_leads),
            'warm_by_date': self._group_leads_by_date(warm_leads),
            'luke_warm_by_date': self._group_leads_by_date(luke_warm_leads),
            'cold_by_date': self._group_leads_by_date(cold_leads),
            'total_leads': leads.count(),
            'selected_response_age': response_age,
            'manual_followup_pending_count': leads.filter(manual_followup_done=False).count(),
            'manual_followup_done_count': leads.filter(manual_followup_done=True).count(),
            'sections': sections,
            'follow_up_status_choices': Appointment._meta.get_field('follow_up_status').choices,
        })
        return context


@staff_required
@require_POST
def update_priority_lead_card(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    now = timezone.now()
    next_url = request.POST.get('next') or reverse('priority_leads')
    if not next_url.startswith('/'):
        next_url = reverse('priority_leads')

    update_fields = []
    manual_state = request.POST.get('manual_followup_state', '').strip()
    follow_up_status = request.POST.get('follow_up_status', '').strip()
    note = request.POST.get('note', '').strip()
    notes_to_prepend = []

    if manual_state in {'done', 'pending'}:
        appointment.manual_followup_done = manual_state == 'done'
        appointment.manual_followup_updated_at = now
        update_fields.extend(['manual_followup_done', 'manual_followup_updated_at'])
        notes_to_prepend.append(f"[{timezone.localtime(now).strftime('%Y-%m-%d %H:%M')}] {request.user.username}: manual follow-up marked as {'done' if appointment.manual_followup_done else 'pending'} from priority dashboard.")

    valid_statuses = {choice[0] for choice in Appointment._meta.get_field('follow_up_status').choices}
    if follow_up_status in valid_statuses:
        appointment.follow_up_status = follow_up_status
        update_fields.append('follow_up_status')

    if note:
        timestamp = timezone.localtime(now).strftime('%Y-%m-%d %H:%M')
        notes_to_prepend.append(f"[Priority Dashboard {timestamp}] {note}")

    if notes_to_prepend:
        existing_notes = appointment.admin_notes or ''
        appointment.admin_notes = "\n".join(notes_to_prepend + ([existing_notes] if existing_notes else []))
        update_fields.append('admin_notes')

    if update_fields:
        appointment.save(update_fields=sorted(set(update_fields)))
        messages.success(request, 'Priority lead updated.')
    else:
        messages.info(request, 'No changes were submitted.')

    return redirect(next_url)


@method_decorator(staff_required, name='dispatch')
class AppointmentDetailView(DetailView):
    template_name = 'appointment_detail.html'
    model = Appointment
    context_object_name = 'appointment'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_object()
        computed_score, computed_status = calculate_lead_score(appointment)
        conversation_history = appointment.conversation_history
        uploaded_files = appointment.get_all_uploaded_files()
        context.update({
            'conversation_history': conversation_history,
            'completeness': appointment.get_customer_info_completeness(),
            'documents': uploaded_files,
            'has_documents': appointment.has_uploaded_documents(),
            'document_count': len(uploaded_files),
            'uploaded_images': [f for f in uploaded_files if f['type'] in ('image', 'video')],
            'computed_lead_score': computed_score,
            'computed_lead_status': computed_status,
            'computed_lead_status_label': dict(Appointment._meta.get_field('lead_status').choices).get(computed_status, 'Cold'),
        })
        return context

    def post(self, request, *args, **kwargs):
        appointment = self.get_object()
        try:
            appointment.customer_name = request.POST.get('customer_name', appointment.customer_name)
            appointment.project_type = request.POST.get('project_type', appointment.project_type)
            appointment.property_type = request.POST.get('property_type', appointment.property_type)
            appointment.customer_area = request.POST.get('customer_area', appointment.customer_area)
            appointment.timeline = request.POST.get('timeline', appointment.timeline)
            appointment.follow_up_status = request.POST.get('follow_up_status', appointment.follow_up_status)
            appointment.admin_notes = request.POST.get('admin_notes', appointment.admin_notes)

            next_follow_up_raw = request.POST.get('next_follow_up_at')
            if next_follow_up_raw:
                next_dt = datetime.fromisoformat(next_follow_up_raw)
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                if next_dt.tzinfo is None:
                    next_dt = sa_timezone.localize(next_dt)
                appointment.next_follow_up_at = next_dt

            if appointment.appointment_type == 'job_appointment':
                job_datetime = request.POST.get('job_scheduled_datetime')
                if job_datetime:
                    dt = datetime.strptime(job_datetime, "%Y-%m-%d %H:%M")
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    appointment.job_scheduled_datetime = sa_timezone.localize(dt)
            else:
                scheduled_datetime = request.POST.get('scheduled_datetime')
                if scheduled_datetime:
                    dt = datetime.fromisoformat(scheduled_datetime)
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    if dt.tzinfo is None:
                        dt = sa_timezone.localize(dt)
                    appointment.scheduled_datetime = dt

            appointment.save()
            refresh_lead_score(appointment)
            messages.success(request, 'Appointment updated successfully!')
        except Exception as e:
            messages.error(request, f'Error updating appointment: {str(e)}')
        return redirect('appointment_detail', pk=appointment.pk)

@method_decorator(staff_required, name='dispatch')
class AppointmentDocumentsView(DetailView):
    template_name = 'appointment_documents.html'
    model = Appointment
    context_object_name = 'appointment'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_object()
        documents = appointment.get_all_uploaded_files()
        context.update({'documents': documents, 'document_count': len(documents)})
        return context

@staff_required
def download_document(request, pk, document_type):
    appointment = get_object_or_404(Appointment, pk=pk)
    if document_type == 'plan_file' and appointment.plan_file:
        try:
            response = HttpResponse(appointment.plan_file.read(), content_type='application/octet-stream')
            filename = f"plan_{appointment.customer_name or 'customer'}_{appointment.id}{os.path.splitext(appointment.plan_file.name)[1]}"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
        except Exception as e:
            messages.error(request, f'Error downloading file: {str(e)}')
    messages.error(request, 'Document not found')
    return redirect('appointment_documents', pk=appointment.pk)

@staff_required
def settings_view(request):
    if request.method == 'POST':
        form = SettingsForm(request.POST)
        if form.is_valid():
            messages.success(request, 'Settings updated successfully')
            return redirect('settings')
    else:
        initial_data = {
            'twilio_account_sid': getattr(settings, 'TWILIO_ACCOUNT_SID', ''),
            'twilio_auth_token': getattr(settings, 'TWILIO_AUTH_TOKEN', ''),
            'twilio_whatsapp_number': getattr(settings, 'TWILIO_WHATSAPP_NUMBER', ''),
            'team_numbers': '\n'.join(getattr(settings, 'TEAM_NUMBERS', [])),
        }
        form = SettingsForm(initial=initial_data)
    return render(request, 'settings.html', {'form': form, 'active_tab': 'general'})

@staff_required
def calendar_settings_view(request):
    if request.method == 'POST':
        form = CalendarSettingsForm(request.POST)
        if form.is_valid():
            messages.success(request, 'Calendar settings updated successfully')
            return redirect('calendar_settings')
    else:
        initial_data = {
            'google_calendar_credentials': json.dumps(getattr(settings, 'GOOGLE_CALENDAR_CREDENTIALS', {}), indent=2),
            'calendar_id': getattr(settings, 'GOOGLE_CALENDAR_ID', 'primary'),
        }
        form = CalendarSettingsForm(initial=initial_data)
    return render(request, 'settings.html', {'form': form, 'active_tab': 'calendar'})

@staff_required
def ai_settings_view(request):
    if request.method == 'POST':
        form = AISettingsForm(request.POST)
        if form.is_valid():
            messages.success(request, 'AI settings updated successfully')
            return redirect('ai_settings')
    else:
        initial_data = {
            'deepseek_api_key': getattr(settings, 'DEEPSEEK_API_KEY', ''),
            'ai_temperature': getattr(settings, 'AI_TEMPERATURE', 0.7),
        }
        form = AISettingsForm(initial=initial_data)
    return render(request, 'settings.html', {'form': form, 'active_tab': 'ai'})

@staff_required
def update_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    has_documents = appointment.has_uploaded_documents()
    document_count = appointment.get_document_count()
    conversation_history = appointment.conversation_messages.all()
    if request.method == 'POST':
        form = AppointmentForm(request.POST, request.FILES, instance=appointment)
        if form.is_valid():
            form.save()
            messages.success(request, 'Appointment updated successfully')
            return redirect('appointment_detail', pk=appointment.pk)
    else:
        form = AppointmentForm(instance=appointment)
    return render(request, 'appointment_detail.html', {
        'appointment': appointment, 'form': form,
        'has_documents': has_documents, 'document_count': document_count,
        'conversation_history': conversation_history,
    })

@staff_required
def send_followup(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        message = request.POST.get('message', '').strip()
        if message:
            try:
                client = Client(ACCOUNT_SID, AUTH_TOKEN)
                response = client.messages.create(body=message, from_=TWILIO_WHATSAPP_NUMBER, to=appointment.phone_number)
                ConversationMessage.objects.create(appointment=appointment, role='assistant', content=message, timestamp=datetime.now())
                messages.success(request, 'Follow-up message sent successfully')
            except Exception as e:
                messages.error(request, f'Failed to send message: {str(e)}')
        else:
            messages.error(request, 'Message cannot be empty')
    return redirect('appointment_detail', pk=appointment.pk)

@staff_required
def confirm_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.status = 'confirmed'
    appointment.save()
    messages.success(request, 'Appointment confirmed successfully')
    return redirect('appointment_detail', pk=appointment.pk)


@staff_required
@require_POST
def complete_lead_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.status = 'completed'
    appointment.follow_up_status = 'completed'
    appointment.is_lead_active = False
    appointment.lead_marked_inactive_at = timezone.now()
    appointment.chatbot_paused = False
    appointment.save(update_fields=['status', 'follow_up_status', 'is_lead_active', 'lead_marked_inactive_at', 'chatbot_paused', 'updated_at'])
    _append_admin_note(appointment, f"{request.user.username}: lead marked complete from appointment detail.")
    messages.success(request, 'Lead marked as complete and removed from Priority Leads.')
    return redirect('appointment_detail', pk=appointment.pk)


@staff_required
def cancel_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.status = 'cancelled'
    appointment.save()
    messages.success(request, 'Appointment cancelled')
    return redirect('appointment_detail', pk=appointment.pk)

@staff_required
def test_whatsapp(request):
    results = None
    if request.method == 'POST':
        try:
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            test_message = """🧪 TEST NOTIFICATION\n\nThis is a test message to verify WhatsApp notifications are working.\n\nIf you receive this, notifications are working! ✅"""
            team_numbers = getattr(settings, 'TEAM_NUMBERS', [])
            results = {'success': True, 'results': []}
            for number in team_numbers:
                try:
                    message = client.messages.create(body=test_message, from_=settings.TWILIO_WHATSAPP_NUMBER, to=number)
                    results['results'].append({'number': number, 'status': 'success', 'sid': message.sid, 'error': None})
                except Exception as e:
                    results['results'].append({'number': number, 'status': 'failed', 'sid': None, 'error': str(e)})
        except Exception as e:
            results = {'success': False, 'error': str(e)}
    return render(request, 'test_whatsapp.html', {'results': results})

@staff_required
def export_appointments(request):
    from django.http import HttpResponse
    import csv
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="plumbing_appointments.csv"'
    writer = csv.writer(response)
    writer.writerow(['Name', 'Phone', 'Service', 'Property Type', 'Area', 'Timeline', 'Status', 'Appointment Date', 'Created At'])
    for appointment in Appointment.objects.all().order_by('-created_at'):
        writer.writerow([
            appointment.customer_name or '', appointment.phone_number,
            appointment.project_type() or '', appointment.customer_area or '',
            appointment.timeline or '', appointment.get_status_display(),
            appointment.scheduled_datetime.strftime('%Y-%m-%d %H:%M') if appointment.scheduled_datetime else '',
            appointment.created_at.strftime('%Y-%m-%d %H:%M')
        ])
    return response

@staff_required
def complete_site_visit(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    if appointment.appointment_type != 'site_visit':
        messages.error(request, 'This is not a site visit appointment')
        return redirect('appointment_detail', pk=appointment.pk)
    if request.method == 'POST':
        site_visit_notes = request.POST.get('site_visit_notes', '')
        plumber_assessment = request.POST.get('plumber_assessment', '')
        appointment.mark_site_visit_completed(notes=site_visit_notes, assessment=plumber_assessment)
        messages.success(request, 'Site visit marked as completed. You can now schedule the job appointment.')
        return redirect('schedule_job', pk=appointment.pk)
    return render(request, 'complete_site_visit.html', {'appointment': appointment})


@staff_required
def schedule_job(request, pk):
    site_visit = get_object_or_404(Appointment, pk=pk)
    if site_visit.appointment_type == 'job' or site_visit.status != 'confirmed':
        messages.error(request, 'Cannot schedule job for this appointment')
        return redirect('appointment_detail', pk=site_visit.pk)
    if request.method == 'POST':
        try:
            job_date = request.POST.get('job_date')
            job_time = request.POST.get('job_time')
            duration_hours = int(request.POST.get('duration_hours', 4))
            job_description = request.POST.get('job_description', '')
            materials_needed = request.POST.get('materials_needed', '')
            if not job_date or not job_time:
                messages.error(request, 'Please provide both date and time')
                return render(request, 'schedule_job.html', {'site_visit': site_visit})
            job_datetime_str = f"{job_date} {job_time}"
            job_datetime = datetime.strptime(job_datetime_str, '%Y-%m-%d %H:%M')
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            job_datetime = sa_timezone.localize(job_datetime)
            if job_datetime <= timezone.now():
                messages.error(request, 'Job time must be in the future')
                return render(request, 'schedule_job.html', {'site_visit': site_visit})
            if job_datetime.weekday() == 5:
                messages.error(request, 'Jobs can only be scheduled Sunday-Friday (closed Saturdays)')
                return render(request, 'schedule_job.html', {'site_visit': site_visit})
            if job_datetime.hour < 8 or job_datetime.hour >= 18:
                messages.error(request, 'Jobs must be scheduled between 8 AM and 6 PM')
                return render(request, 'schedule_job.html', {'site_visit': site_visit})
            import uuid
            job_appointment = Appointment.objects.update(
                customer_name=site_visit.customer_name, customer_email=site_visit.customer_email or '',
                customer_area=site_visit.customer_area, project_type=site_visit.project_type,
                property_type=site_visit.property_type,
                project_description=job_description or site_visit.project_description,
                scheduled_datetime=job_datetime, appointment_type='job', status='scheduled',
                has_plan=site_visit.has_plan, timeline=f'{duration_hours} hours',
            )
            try:
                send_job_notifications(job_appointment, materials_needed)
            except Exception as notify_error:
                print(f"⚠️ Notification error: {notify_error}")
            messages.success(request, f'Job scheduled for {job_datetime.strftime("%B %d, %Y at %I:%M %p")}')
            return redirect('appointment_detail', pk=job_appointment.pk)
        except ValueError as e:
            messages.error(request, f'Invalid date/time format: {str(e)}')
        except Exception as e:
            messages.error(request, f'Error scheduling job: {str(e)}')
    return render(request, 'schedule_job.html', {'site_visit': site_visit})

@staff_required
def job_appointments_list(request):
    job_appointments = Appointment.objects.filter(appointment_type='job_appointment').order_by('-job_scheduled_datetime')
    status_filter = request.GET.get('status')
    if status_filter:
        job_appointments = job_appointments.filter(job_status=status_filter)
    plumber_filter = request.GET.get('plumber')
    if plumber_filter:
        job_appointments = job_appointments.filter(assigned_plumber_id=plumber_filter)
    context = {
        'job_appointments': job_appointments, 'status_choices': Appointment.JOB_STATUS_CHOICES,
        'selected_status': status_filter, 'selected_plumber': plumber_filter,
    }
    return render(request, 'job_appointments_list.html', context)

@require_POST
@staff_required
def update_job_status(request, pk):
    job_appointment = get_object_or_404(Appointment, pk=pk)
    if job_appointment.appointment_type != 'job_appointment':
        return JsonResponse({'success': False, 'error': 'Not a job appointment'})
    new_status = request.POST.get('status')
    if new_status not in dict(Appointment.JOB_STATUS_CHOICES):
        return JsonResponse({'success': False, 'error': 'Invalid status'})
    job_appointment.job_status = new_status
    if new_status == 'completed':
        job_appointment.job_completed_at = timezone.now()
    job_appointment.save()
    send_job_status_update_notification(job_appointment, new_status)
    return JsonResponse({'success': True, 'message': f'Job status updated to {job_appointment.get_job_status_display()}'})

def check_job_availability(job_datetime, duration_hours, exclude_appointment_id=None):
    try:
        job_end_time = job_datetime + timedelta(hours=duration_hours)
        overlapping_jobs = Appointment.objects.filter(appointment_type='job_appointment', job_status__in=['scheduled', 'in_progress'], job_scheduled_datetime__isnull=False)
        if exclude_appointment_id:
            overlapping_jobs = overlapping_jobs.exclude(id=exclude_appointment_id)
        for job in overlapping_jobs:
            existing_end = job.job_scheduled_datetime + timedelta(hours=job.job_duration_hours)
            if (job_datetime < existing_end and job_end_time > job.job_scheduled_datetime):
                return False
        if job_datetime.weekday() == 5:
            return False
        if job_datetime.hour < 8 or job_end_time.hour > 18:
            return False
        if job_datetime <= timezone.now():
            return False
        return True
    except Exception as e:
        print(f"Error checking job availability: {str(e)}")
        return False


def send_job_appointment_notifications(job_appointment):
    try:
        job_date = job_appointment.job_scheduled_datetime.strftime('%A, %B %d, %Y')
        job_time = job_appointment.job_scheduled_datetime.strftime('%I:%M %p')
        duration = job_appointment.job_duration_hours
        customer_message = f"""🔧 JOB APPOINTMENT SCHEDULED\n\nHi {job_appointment.customer_name or 'Customer'},\n\nYour plumbing job has been scheduled:\n\n📅 Date: {job_date}\n🕐 Time: {job_time}\n⏱️ Duration: {duration} hours\n📍 Location: {job_appointment.customer_area}\n🔨 Work: {job_appointment.job_description or job_appointment.project_type}\n\nOur plumber will contact you before arrival.\n\n{f"Materials needed: {job_appointment.job_materials_needed}" if job_appointment.job_materials_needed else ""}\n\nQuestions? Reply to this message.\n\n- Plumbing Team"""
        clean_phone = clean_phone_number(job_appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, customer_message)
        plumber_name = job_appointment.assigned_plumber.get_full_name() if job_appointment.assigned_plumber else "Unassigned"
        team_message = f"""👷 NEW JOB SCHEDULED\n\nCustomer: {job_appointment.customer_name}\nPhone: {job_appointment.phone_number.replace('whatsapp:', '')}\nDate/Time: {job_date} at {job_time}\nDuration: {duration} hours\nLocation: {job_appointment.customer_area}\nAssigned to: {plumber_name}\n\nJob Description:\n{job_appointment.job_description or job_appointment.project_type}\n\n{f"Materials: {job_appointment.job_materials_needed}" if job_appointment.job_materials_needed else ""}\n\nView details: http://127.0.0.1:8000/appointments/{job_appointment.id}/"""
        TEAM_NUMBERS = ['0610318200']
        for number in TEAM_NUMBERS:
            try:
                whatsapp_api.send_text_message(number, team_message)
            except Exception as e:
                print(f"Failed to send team notification: {str(e)}")
    except Exception as e:
        print(f"Error sending job appointment notifications: {str(e)}")

def send_job_status_update_notification(job_appointment, new_status):
    try:
        status_messages = {
            'in_progress': f"🔧 Your plumbing job at {job_appointment.customer_area} has started.",
            'completed': f"✅ Your plumbing job at {job_appointment.customer_area} has been completed!",
            'cancelled': f"❌ Your scheduled plumbing job has been cancelled.",
        }
        if new_status in status_messages:
            twilio_client.messages.create(body=status_messages[new_status], from_=TWILIO_WHATSAPP_NUMBER, to=job_appointment.phone_number)
    except Exception as e:
        print(f"Error sending status update: {str(e)}")

@staff_required
def reschedule_job(request, pk):
    job_appointment = get_object_or_404(Appointment, pk=pk)
    if job_appointment.appointment_type != 'job_appointment':
        messages.error(request, 'This is not a job appointment')
        return redirect('appointment_detail', pk=job_appointment.pk)
    if request.method == 'POST':
        try:
            job_date = request.POST.get('job_date')
            job_time = request.POST.get('job_time')
            job_datetime_str = f"{job_date} {job_time}"
            new_datetime = datetime.strptime(job_datetime_str, '%Y-%m-%d %H:%M')
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            new_datetime = sa_timezone.localize(new_datetime)
            is_available = check_job_availability(new_datetime, job_appointment.job_duration_hours, exclude_appointment_id=job_appointment.id)
            if not is_available:
                messages.error(request, 'Selected time slot is not available')
                return render(request, 'reschedule_job.html', {'job_appointment': job_appointment})
            old_datetime = job_appointment.job_scheduled_datetime
            job_appointment.job_scheduled_datetime = new_datetime
            job_appointment.save()
            send_job_reschedule_notification(job_appointment, old_datetime, new_datetime)
            messages.success(request, f'Job rescheduled to {new_datetime.strftime("%B %d, %Y at %I:%M %p")}')
            return redirect('appointment_detail', pk=job_appointment.pk)
        except Exception as e:
            messages.error(request, f'Error rescheduling job: {str(e)}')
    return render(request, 'reschedule_job.html', {'job_appointment': job_appointment})

def send_job_reschedule_notification(job_appointment, old_datetime, new_datetime):
    try:
        old_date_str = old_datetime.strftime('%A, %B %d at %I:%M %p')
        new_date_str = new_datetime.strftime('%A, %B %d at %I:%M %p')
        message = f"""📅 JOB RESCHEDULED\n\nHi {job_appointment.customer_name},\n\nYour plumbing job has been rescheduled:\n\n❌ Previous: {old_date_str}\n✅ New: {new_date_str}\n\n📍 Location: {job_appointment.customer_area}\n🔨 Work: {job_appointment.job_description or job_appointment.project_type}\n\nOur plumber will contact you before the new appointment time.\n\nQuestions? Reply to this message.\n\n- Plumbing Team"""
        twilio_client.messages.create(body=message, from_=TWILIO_WHATSAPP_NUMBER, to=job_appointment.phone_number)
    except Exception as e:
        print(f"Error sending reschedule notification: {str(e)}")


@staff_required
def job_appointments_list(request):
    job_appointments = Appointment.objects.filter(appointment_type='job').order_by('-scheduled_datetime')
    total_jobs = job_appointments.count()
    scheduled_jobs = job_appointments.filter(status='scheduled').count()
    in_progress_jobs = job_appointments.filter(status='in_progress').count()
    completed_jobs = job_appointments.filter(status='completed').count()
    status_filter = request.GET.get('status')
    if status_filter:
        job_appointments = job_appointments.filter(status=status_filter)
    plumber_filter = request.GET.get('plumber')
    if plumber_filter:
        job_appointments = job_appointments.filter(assigned_plumber_id=plumber_filter)
    date_filter = request.GET.get('date')
    if date_filter:
        job_appointments = job_appointments.filter(scheduled_datetime__date=date_filter)
    context = {
        'job_appointments': job_appointments, 'status_choices': ['scheduled', 'in_progress', 'completed', 'cancelled'],
        'selected_status': status_filter, 'selected_plumber': plumber_filter, 'selected_date': date_filter,
        'total_jobs': total_jobs, 'scheduled_jobs': scheduled_jobs, 'in_progress_jobs': in_progress_jobs, 'completed_jobs': completed_jobs,
    }
    return render(request, 'job_appointments_list.html', context)


class CalendarView(View):
    template_name = 'calendar.html'
    def get(self, request):
        return render(request, self.template_name)


def appointment_data(request):
    service_filter = request.GET.get('service')
    appointments = Appointment.objects.all()
    if service_filter and service_filter != "all":
        appointments = appointments.filter(project_type__icontains=service_filter)
    data = []
    for appt in appointments:
        if appt.scheduled_datetime:
            data.append({
                "id": appt.id, "customerName": appt.customer_name or "Unknown",
                "phone": appt.phone_number, "date": appt.scheduled_datetime.date().isoformat(),
                "time": appt.scheduled_datetime.time().strftime("%H:%M"),
                "service": map_project_type_to_service_key(appt.project_type),
                "area": appt.customer_area or "N/A", "status": appt.status,
                "propertyType": appt.property_type or "N/A", "timeline": appt.timeline or "N/A", "hasPlan": appt.has_plan
            })
    return JsonResponse(data, safe=False)


def map_project_type_to_service_key(project_type):
    mapping = {"bathroom_renovation": "bathroom", "kitchen_renovation": "kitchen", "new_plumbing_installation": "installation"}
    return mapping.get(project_type, "other")

@csrf_exempt
def whatsapp_webhook(request):
    """Handle incoming WhatsApp messages - UPDATED WITH FOLLOW-UP TRACKING"""
    if request.method == 'POST':
        try:
            incoming_message = request.POST.get('Body', '').strip()
            sender = request.POST.get('From', '')
            if not incoming_message or not sender:
                return HttpResponse(status=200)
            print(f"📥 Incoming from {sender}: {incoming_message}")
            appointment, created = Appointment.objects.get_or_create(phone_number=sender, defaults={'status': 'pending'})
            appointment.mark_customer_response()
            opt_out_keywords = ['stop', 'unsubscribe', 'opt out', 'no more', 'leave me alone']
            if any(keyword in incoming_message.lower() for keyword in opt_out_keywords):
                appointment.mark_as_inactive_lead(reason='customer_opted_out')
                opt_out_message = """Understood. I've removed you from our follow-up list.\n\nIf you change your mind in the future, just send us a message and we'll be happy to help!\n\nThanks,\n- Homebase Plumbers"""
                clean_phone = clean_phone_number(sender)
                whatsapp_api.send_text_message(clean_phone, opt_out_message)
                return HttpResponse(status=200)
            delay_keywords = ['later', 'not now', 'busy', 'call me later', 'in a few weeks']
            if any(keyword in incoming_message.lower() for keyword in delay_keywords):
                appointment.followup_stage = 'week_2'
                appointment.save()
                delay_message = """No problem at all! I understand timing isn't right at the moment.\n\nI'll check back with you in a couple of weeks.\n\nIn the meantime, if you need anything, just message us!\n\nThanks,\n- Homebase Plumbers"""
                clean_phone = clean_phone_number(sender)
                whatsapp_api.send_text_message(clean_phone, delay_message)
                return HttpResponse(status=200)
            plumbot = Plumbot(sender)
            reply = plumbot.generate_response(incoming_message)
            clean_phone = clean_phone_number(sender)
            whatsapp_api.send_text_message(clean_phone, reply)
            print(f"✅ Sent reply to {sender}")
            return HttpResponse(status=200)
        except Exception as e:
            print(f"❌ Webhook error: {str(e)}")
            import traceback
            traceback.print_exc()
            return HttpResponse(status=500)
    return HttpResponse(status=405)


@staff_required
def followup_dashboard(request):
    from datetime import timedelta
    now = timezone.now()
    total_active_leads = Appointment.objects.filter(is_lead_active=True, status='pending').count()
    stage_counts = {}
    for stage_code, stage_name in Appointment._meta.get_field('followup_stage').choices:
        count = Appointment.objects.filter(is_lead_active=True, followup_stage=stage_code).count()
        if count > 0:
            stage_counts[stage_name] = count
    leads_needing_followup = Appointment.objects.filter(is_lead_active=True, status='pending').exclude(followup_stage='completed').exclude(followup_stage='responded')
    ready_for_followup = [lead for lead in leads_needing_followup if lead.should_send_followup_now()]
    recent_responses = Appointment.objects.filter(last_customer_response__gte=now - timedelta(days=7), is_lead_active=True).order_by('-last_customer_response')[:10]
    recent_inactive = Appointment.objects.filter(is_lead_active=False, lead_marked_inactive_at__gte=now - timedelta(days=30)).order_by('-lead_marked_inactive_at')[:10]
    context = {
        'total_active_leads': total_active_leads, 'stage_counts': stage_counts,
        'ready_count': len(ready_for_followup), 'ready_leads': ready_for_followup[:20],
        'recent_responses': recent_responses, 'recent_inactive': recent_inactive,
    }
    return render(request, 'followup_dashboard.html', context)


@staff_required
def mark_lead_inactive(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        reason = request.POST.get('reason', 'manual')
        appointment.mark_as_inactive_lead(reason=reason)
        messages.success(request, f'Lead for {appointment.customer_name or appointment.phone_number} marked as inactive')
        return redirect('appointments_list')
    return render(request, 'confirm_mark_inactive.html', {'appointment': appointment})


@staff_required
def reactivate_lead(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        appointment.is_lead_active = True
        appointment.followup_stage = 'none'
        appointment.lead_marked_inactive_at = None
        appointment.save()
        messages.success(request, f'Lead reactivated for {appointment.customer_name or appointment.phone_number}')
        return redirect('appointment_detail', pk=appointment.pk)
    return render(request, 'confirm_reactivate.html', {'appointment': appointment})


@staff_required
def test_followup_message(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        stage = request.POST.get('stage', 'day_1')
        from bot.management.commands.send_followups import Command
        cmd = Command()
        message = cmd.generate_followup_message(appointment, stage)
        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, message)
        messages.success(request, f'Test {stage} follow-up sent to {appointment.phone_number}')
        return redirect('appointment_detail', pk=appointment.pk)
    return render(request, 'test_followup.html', {'appointment': appointment, 'stages': ['day_1', 'day_3', 'week_1', 'week_2', 'month_1']})


@staff_required
@require_POST
def manual_followup_check(request):
    from django.core.management import call_command
    from io import StringIO
    try:
        out = StringIO()
        call_command('send_followups', stdout=out)
        output = out.getvalue()
        messages.success(request, 'Follow-up check completed successfully')
        for line in output.split('\n'):
            if 'Sent:' in line or 'Skipped:' in line or 'Errors:' in line:
                messages.info(request, line.strip())
    except Exception as e:
        messages.error(request, f'Error running follow-up check: {str(e)}')
    return redirect('followup_dashboard')


@staff_required
@require_POST
def pause_chatbot(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.pause_chatbot()
    _append_admin_note(appointment, f"{request.user.username}: chatbot paused.")
    messages.success(request, 'Chatbot paused for this lead.')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def resume_chatbot(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.resume_chatbot()
    _append_admin_note(appointment, f"{request.user.username}: chatbot resumed.")
    messages.success(request, 'Chatbot resumed for this lead.')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def pause_auto_followup(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    pause_duration = request.POST.get('pause_duration')
    if pause_duration == 'permanent':
        appointment.manual_followup_paused = True
        appointment.manual_followup_paused_until = None
        pause_msg = "permanently"
    else:
        hours = int(pause_duration)
        pause_until = timezone.now() + timedelta(hours=hours)
        appointment.manual_followup_paused = True
        appointment.manual_followup_paused_until = pause_until
        if hours == 24: pause_msg = "for 24 hours"
        elif hours == 48: pause_msg = "for 48 hours"
        elif hours == 168: pause_msg = "for 1 week"
        elif hours == 720: pause_msg = "for 1 month"
        else: pause_msg = f"for {hours} hours"
    appointment.save()
    messages.success(request, f'⏸️ Automatic follow-ups paused {pause_msg}')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def resume_auto_followup(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.manual_followup_paused = False
    appointment.manual_followup_paused_until = None
    appointment.save()
    messages.success(request, '▶️ Automatic follow-ups resumed')
    return redirect('appointment_detail', pk=pk)


@staff_required
def send_followup(request, pk):
    """Send MANUAL follow-up message via WhatsApp"""
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        message = request.POST.get('message', '').strip()
        if not message:
            messages.error(request, 'Message cannot be empty')
            return redirect('appointment_detail', pk=appointment.pk)
        try:
            customer_name = appointment.customer_name or "there"
            personalized_message = message.replace('{name}', customer_name)
            clean_phone = clean_phone_number(appointment.phone_number)
            result = whatsapp_api.send_text_message(clean_phone, personalized_message)
            appointment.add_conversation_message('assistant', f"[MANUAL FOLLOW-UP] {personalized_message}")
            appointment.last_followup_sent = timezone.now()
            appointment.last_manual_followup_sent = timezone.now()
            appointment.followup_count = (appointment.followup_count or 0) + 1
            appointment.manual_followup_count = (appointment.manual_followup_count or 0) + 1
            appointment.is_automatic_followup = False
            appointment.followup_stage = 'responded'
            pause_until = timezone.now() + timedelta(hours=48)
            appointment.manual_followup_paused = True
            appointment.manual_followup_paused_until = pause_until
            appointment.save()
            messages.success(request, f'✅ Manual follow-up sent to {clean_phone}! Auto follow-ups paused for 48 hours.')
        except Exception as e:
            messages.error(request, f'Failed to send message: {str(e)}')
    return redirect('appointment_detail', pk=appointment.pk)


@staff_required
def send_bulk_followup(request):
    if request.method == 'POST':
        lead_ids = request.POST.getlist('lead_ids')
        message_template = request.POST.get('message_template', '').strip()
        pause_duration = int(request.POST.get('pause_duration', 48))
        if not lead_ids or not message_template:
            messages.error(request, 'Please select leads and provide a message')
            return redirect('followup_dashboard')
        results = {'sent': 0, 'failed': 0, 'errors': []}
        for lead_id in lead_ids:
            try:
                appointment = Appointment.objects.get(id=lead_id)
                customer_name = appointment.customer_name or "there"
                personalized_message = message_template.replace('{name}', customer_name)
                clean_phone = clean_phone_number(appointment.phone_number)
                whatsapp_api.send_text_message(clean_phone, personalized_message)
                appointment.add_conversation_message('assistant', f"[BULK MANUAL FOLLOW-UP] {personalized_message}")
                appointment.last_followup_sent = timezone.now()
                appointment.last_manual_followup_sent = timezone.now()
                appointment.followup_count = (appointment.followup_count or 0) + 1
                appointment.manual_followup_count = (appointment.manual_followup_count or 0) + 1
                appointment.is_automatic_followup = False
                appointment.followup_stage = 'responded'
                pause_until = timezone.now() + timedelta(hours=pause_duration)
                appointment.manual_followup_paused = True
                appointment.manual_followup_paused_until = pause_until
                appointment.save()
                results['sent'] += 1
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"Lead {lead_id}: {str(e)}")
        if results['sent'] > 0:
            messages.success(request, f"✅ Sent {results['sent']} manual follow-ups (auto follow-ups paused)")
        if results['failed'] > 0:
            messages.warning(request, f"⚠️ Failed to send {results['failed']} messages")
        return redirect('followup_dashboard')
    active_leads = Appointment.objects.filter(is_lead_active=True, status='pending').order_by('-last_customer_response')
    return render(request, 'bulk_followup.html', {'leads': active_leads})


class Plumbot:
    def __init__(self, phone_number):
        self.phone_number = phone_number
        self.appointment, _ = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )

    
    #
    # ── FIX 3 helpers ────────────────────────────────────────────────────────

    def _is_delay_or_exit_signal(self, message: str) -> bool:
        msg = message.lower().strip()
        short_acks = {
            'ok', 'okay', 'ok.', 'okay.', 'ok thanks', 'ok thank you',
            'thanks', 'thank you', 'thank u', 'thx', 'thnx',
            'noted', 'got it', 'alright', 'cool', 'nice', 'great',
            '👍', '🙏', '✅', '😊', 'ooh ok', 'ooh okay',
        }
        if msg in short_acks:
            return True
        delay_phrases = [
            "i'll talk", "i will talk", "talk later", "will contact",
            "contact later", "i'll be in touch", "get back to you",
            "busy now", "busy at the moment", "not right now",
            "will let you know", "will come back", "come back to you",
            "in a bit", "later today", "i'll get back",
            "let me think", "need to think", "thinking about it",
            "no rush", "no problem", "no worries",
            "i will talk to you later", "talk to you later",
        ]
        if any(phrase in msg for phrase in delay_phrases):
            return True
        return False

    def _get_delay_acknowledgment(self) -> str:
        return (
            "No problem at all! 😊 Whenever you're ready, just drop us a message and "
            "we'll pick up right where we left off.\n\n"
            "— Homebase Plumbers"
        )

    # ── Problem 3 fix: Hook opener helpers ───────────────────────────────────

    def _is_generic_opener(self, message: str) -> bool:
        """
        Return True if the message is a vague first-contact opener.
        Only fires when we have NO prior bot turns in the conversation.
        """
        history = self.appointment.conversation_history or []
        prior_bot_turns = sum(1 for m in history if m.get("role") == "assistant")
        if prior_bot_turns > 0:
            return False

        msg = message.lower().strip().rstrip("?.!")

        generic_phrases = {
            "hello", "hi", "hey", "hie", "heyy", "heyyy",
            "good morning", "good afternoon", "good evening",
            "morning", "afternoon", "evening",
            "more info", "more information", "i need more info",
            "i need more information", "info please", "info pls",
            "can i get more info on this", "can i get more info",
            "i saw your ad", "saw your ad", "facebook ad", "i saw an ad",
            "interested", "i am interested", "i'm interested",
            "is this available", "are you available",
            "enquiry", "inquiry", "i have an enquiry", "i have a question",
            "can you help", "can you help me", "help",
            "what do you offer", "what services do you offer",
            "tell me more", "please tell me more",
        }

        if msg in generic_phrases:
            return True

        starters = [
            "hello ", "hi ", "hey ", "good morning", "good afternoon",
            "more info", "i need more info", "i saw your", "can i get more",
        ]
        if any(msg.startswith(s) for s in starters):
            return True

        return False

    def _get_hook_response(self) -> str:
        """Value-first hook message for brand-new generic contacts."""
        return (
            "Sharp! 👋 We do bathroom renovations, kitchen renos and new "
            "plumbing installations — all across Harare.\n\n"
            "Most of our clients start with a *free site visit* so we can "
            "see exactly what needs doing and give an accurate quote — no "
            "guessing.\n\n"
            "What are you thinking of getting sorted? 😊"
        )

    def generate_response(self, incoming_message):
        """Check service inquiries ONLY when not mid-conversation."""
        try:
            current_question = self.get_next_question_to_ask()

            # ── Problem 3 fix: intercept generic openers on first contact ──
            if self._is_generic_opener(incoming_message):
                print("🪝 Problem 3 fix: Generic opener detected — sending hook response")
                reply = self._get_hook_response()
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reply)
                return reply

            #
            any_pricing_sent = (
                getattr(self.appointment, 'pricing_overview_sent', False) or
                bool(getattr(self.appointment, 'sent_pricing_intents', None))
            )
            any_pricing_sent = (
                getattr(self.appointment, 'pricing_overview_sent', False) or
                bool(getattr(self.appointment, 'sent_pricing_intents', None))
            )
            mid_conversation = (
                any_pricing_sent or
                self.appointment.project_type is not None
            )

            if not mid_conversation:
                inquiry = self.detect_service_inquiry(incoming_message)
                if inquiry.get('intent') != 'none' and inquiry.get('confidence') == 'HIGH':
                    intent = inquiry['intent']
                    sent = list(getattr(self.appointment, 'sent_pricing_intents', None) or [])
                    if intent in sent:
                        print(f"⏭️ Skipping already-sent service inquiry: {intent}")
                    else:
                        print(f"💡 Handling service inquiry: {intent}")
                        reply = self.handle_service_inquiry(intent, incoming_message)
                        sent.append(intent)
                        self.appointment.sent_pricing_intents = sent
                        self.appointment.save(update_fields=['sent_pricing_intents'])
                        self.appointment.add_conversation_message("user", incoming_message)
                        self.appointment.add_conversation_message("assistant", reply)
                        return reply

            if (self.appointment.has_plan is True and
                    self.appointment.plan_status == 'pending_upload'):
                return self.handle_plan_upload_flow(incoming_message)

            if (self.appointment.has_plan is True and
                    self.appointment.plan_status == 'plan_uploaded'):
                return self.handle_post_upload_messages(incoming_message)

            if (self.appointment.has_plan is True and 
                self.appointment.plan_status == 'plan_uploaded'):
                return self.handle_post_upload_messages(incoming_message)

            if (self.appointment.status == 'pending' and 
                self.appointment.project_type and 
                self.appointment.customer_area and 
                self.appointment.timeline and 
                self.appointment.property_type and 
                not self.appointment.customer_name):
                
                selected_time = self.process_alternative_time_selection(incoming_message)
                
                if selected_time:
                    print(f"🎯 Customer selecting alternative time: {selected_time}")
                    booking_result = self.book_appointment_with_selected_time(selected_time)
                    if booking_result['success']:
                        reply = f"Perfect! I've booked your appointment for {booking_result['datetime']}. To complete your booking, may I have your full name?"
                    else:
                        alternatives = booking_result.get('alternatives', [])
                        if alternatives:
                            alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                            reply = f"That time isn't available either. Here are some other options:\n{alt_text}\n\nWhich works better for you?"
                        else:
                            reply = "I'm having trouble finding available times. Could you suggest a completely different day? Our hours are 8 AM - 6 PM, Monday to Friday."
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply
            
            
            if self._is_delay_or_exit_signal(incoming_message):
                print(f"⏸️ FIX 3: Delay/exit signal detected — not pushing further")
                reply = self._get_delay_acknowledgment()
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reply)
                return reply

            extracted_data = self.extract_all_available_info_with_ai(incoming_message)
            
            if self.handle_plan_later_response(incoming_message):
                next_question = self.get_next_question_to_ask()
                if next_question != "complete":
                    acknowledgment = "Perfect! You can send your plan whenever you're ready. "
                    reply = self.generate_contextual_response(incoming_message, next_question, ['plan_status'])
                    reply = acknowledgment + reply
                    return reply
            
            updated_fields = self.update_appointment_with_extracted_data(extracted_data)
            
            if (self.appointment.status == 'confirmed' and 
                self.appointment.scheduled_datetime and 
                self.detect_reschedule_request_with_ai(incoming_message)):
                print("🤖 AI detected reschedule request, handling...")
                reschedule_response = self.handle_reschedule_request_with_ai(incoming_message)
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reschedule_response)
                return reschedule_response
            
            next_question = self.get_next_question_to_ask()
            booking_status = self.smart_booking_check()
            
            if (booking_status['ready_to_book'] and 
                self.appointment.status != 'confirmed' and
                self.appointment.has_plan is False):
                
                booking_result = self.book_appointment(incoming_message)
                
                if booking_result['success']:
                    reply = f"Perfect! I've booked your appointment for {booking_result['datetime']}. To complete your booking, may I have your full name?"
                else:
                    error = booking_result.get('error', '')
                    alternatives = booking_result.get('alternatives', [])
                    if 'saturday' in error.lower() or not alternatives:
                        alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives]) if alternatives else ""
                        reply = "We unfortunately don't operate on Saturdays. 😊\n\nOur working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                        if alt_text:
                            reply += f"Here are some available slots:\n{alt_text}\n\nOr feel free to suggest a different date and time!"
                        else:
                            reply += "Could you please suggest a different date and time that works for you?"
                    else:
                        alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                        reply = f"That time isn't available either. Here are some other options:\n{alt_text}\n\nWhich works better for you?"
            else:
                reply = self.generate_contextual_response(incoming_message, next_question, updated_fields)
            
            self.appointment.add_conversation_message("user", incoming_message)
            self.appointment.add_conversation_message("assistant", reply)
            return reply

        except Exception as e:
            print(f"❌ API Error: {str(e)}")
            return "I'm having some trouble connecting to our system. Could you try again in a moment?"


    def validate_plan_status_with_ai(self, extracted_status: str, original_message: str) -> tuple:
        try:
            validation_prompt = f"""You are a plan status validation assistant for an appointment booking system.

    CONTEXT:
    We asked the customer: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"

    CUSTOMER'S RESPONSE: "{original_message}"

    AI EXTRACTED VALUE: "{extracted_status}"

    TASK:
    Analyze the customer's response and determine:
    1. Did they answer the plan question?
    2. Do they HAVE a plan or do they NEED a site visit?
    3. How confident are you in this interpretation?

    RESPONSE FORMAT (CRITICAL - FOLLOW EXACTLY):
    Return ONLY a JSON object with this exact structure:
    {{
        "answer_provided": true/false,
        "interpretation": "HAS_PLAN" or "NEEDS_VISIT" or "UNCLEAR" or "OFF_TOPIC",
        "confidence": "HIGH" or "MEDIUM" or "LOW",
        "reasoning": "Brief explanation of your analysis"
    }}"""

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a precise validation assistant. Return ONLY valid JSON with no additional text or formatting."},
                    {"role": "user", "content": validation_prompt}
                ],
                temperature=0.2,
                max_tokens=150
            )
            
            ai_response = response.choices[0].message.content.strip()
            ai_response = ai_response.replace('```json', '').replace('```', '').strip()
            
            try:
                validation_result = json.loads(ai_response)
            except json.JSONDecodeError:
                return (False, None, "ERROR")
            
            answer_provided = validation_result.get('answer_provided', False)
            interpretation = validation_result.get('interpretation', 'UNCLEAR')
            confidence = validation_result.get('confidence', 'LOW')
            
            if not answer_provided or confidence == 'LOW':
                return (False, None, confidence)
            
            if interpretation == 'HAS_PLAN':
                return (True, True, confidence)
            elif interpretation == 'NEEDS_VISIT':
                return (True, False, confidence)
            else:
                return (False, None, "ERROR")
            
        except Exception as e:
            print(f"❌ AI validation error: {str(e)}")
            return (False, None, "ERROR")


    def generate_clarifying_question_for_plan_status(self, retry_count: int) -> str:
        try:
            clarification_prompt = f"""You are a professional appointment assistant.
    Generate a clarifying question for retry attempt #{retry_count + 1} about whether customer has plans.
    Current retry: {retry_count}
    Generate the clarifying question:"""
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a helpful appointment assistant."},
                    {"role": "user", "content": clarification_prompt}
                ],
                temperature=0.8,
                max_tokens=150
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            fallbacks = [
                "Just to confirm - do you have plans already, or would you like us to do a site visit?",
                "I need to know: do you have existing blueprints/plans, or should we visit your property first?",
                "Simple question: Do you have plans? Reply YES or NO.",
                "Option A: I have plans to send. Option B: I need a site visit. Which one - A or B?"
            ]
            return fallbacks[min(retry_count, len(fallbacks) - 1)]


    def _plan_question_already_pending(self) -> bool:
        try:
            history = self.appointment.conversation_history or []
            for msg in reversed(history):
                if msg.get('role') == 'assistant':
                    content = msg.get('content', '').lower()
                    plan_phrases = ['do you have a plan', 'have a plan', 'site visit', 'picture or pdf', 'plan already', 'plan or visit', 'photo/plan', 'photo or plan']
                    return any(phrase in content for phrase in plan_phrases)
            return False
        except Exception:
            return False

    def handle_plan_later_response(self, message):
        try:
            if self.appointment.has_plan is not None:
                return False
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are an intent classifier for a plumbing appointment system in Zimbabwe. Reply with ONLY 'YES' or 'NO'."},
                    {"role": "user", "content": f"""We asked: "Do you have a plan already, or would you like us to do a site visit?"
Is the customer saying they HAVE a plan and will send it LATER (not now)?
Customer message: "{message}"
Reply YES or NO only."""}
                ],
                temperature=0.1,
                max_tokens=5
            )
            result = response.choices[0].message.content.strip().upper()
            is_plan_later = result == "YES"
            if is_plan_later:
                self.appointment.has_plan = True
                self.appointment.save()
            return is_plan_later
        except Exception as e:
            return False

    def has_basic_info_for_plan_upload(self):
        return (self.appointment.project_type and self.appointment.customer_area and self.appointment.property_type)

    def initiate_plan_upload_flow(self):
        try:
            self.appointment.plan_status = 'pending_upload'
            self.appointment.save()
            service_name = self.appointment.project_type.replace('_', ' ').title()
            upload_message = f"""Perfect! Since you have a plan for your {service_name}, I'll need you to send it to me so our plumber can review it.

📋 PLAN UPLOAD INSTRUCTIONS:

1. Take clear photos of your plan/blueprint
2. Send them as images in this chat (one by one)
3. Or send as a PDF document

Make sure the plan shows:
• Room dimensions
• Fixture locations  
• Plumbing connections
• Any special requirements

Once you send the plan, I'll forward it to our plumber immediately. Send your first image or document now."""
            return upload_message
        except Exception as e:
            return "I'd like to help you with your plan, but I'm having a technical issue. Could you try again in a moment?"

    def handle_plan_upload_flow(self, message):
        try:
            completion_indicators = ['done', 'finished', 'complete', "that's all", 'no more', 'all sent']
            message_lower = message.lower()
            if any(indicator in message_lower for indicator in completion_indicators):
                return self.complete_plan_upload()
            if any(word in message_lower for word in ['more', 'another', 'next', 'additional']):
                return "Great! Please send the next image or document."
            if '?' in message or any(word in message_lower for word in ['help', 'how', 'what', 'problem', 'issue']):
                return self.handle_plan_upload_question(message)
            return """Thanks! I can see you're sending the plan materials. \n\nIf you have more images or documents to send, please continue. \n\nWhen you're finished sending everything, just type "done" or "finished" and I'll send it all to the plumber."""
        except Exception as e:
            return "I'm processing your plan. If you have more to send, please continue. Type 'done' when finished."

    def handle_plan_upload_question(self, message):
        try:
            question_lower = message.lower()
            if 'format' in question_lower or 'type' in question_lower:
                return "You can send: JPG/PNG images, PDF documents, or even hand-drawn sketches. Just make sure they're clear and readable."
            elif 'size' in question_lower or 'large' in question_lower:
                return "File size shouldn't be an issue through WhatsApp. If a file is too large, try taking separate photos of different sections."
            elif 'quality' in question_lower or 'clear' in question_lower:
                return "Make sure the text and measurements are readable. Good lighting helps. If a photo is blurry, feel free to retake it."
            elif 'how many' in question_lower or 'pages' in question_lower:
                return "Send as many images/pages as needed to show the complete plan. Most customers send 2-5 images."
            else:
                return "I'm here to help with your plan upload. Send your images/documents and type 'done' when finished."
        except Exception as e:
            return "Please continue sending your plan materials. Type 'done' when you've sent everything."

    def complete_plan_upload(self):
        try:
            self.appointment.plan_status = 'plan_uploaded'
            self.appointment.save()
            plumber_number = getattr(self.appointment, 'plumber_contact_number', '+263610318200')
            self.notify_plumber_about_plan()
            service_name = self.appointment.project_type.replace('_', ' ').title()
            customer_name = self.appointment.customer_name
            if customer_name:
                intro_message = f"Hi {customer_name}, I've forwarded your {service_name} plan to our plumber for review."
            else:
                intro_message = f"Thanks! I've forwarded your {service_name} plan to our plumber for review."
            completion_message = f"""✅ PLAN SENT SUCCESSFULLY!

    {intro_message}

    📞 NEXT STEPS:
    • Our plumber will review your plan within 24 hours
    • They'll contact you directly on this number: {self.phone_number.replace('whatsapp:', '')}
    • They'll discuss the project details and provide a quote
    • Once approved, they'll book your appointment or message you to complete booking

    🔧 PLUMBER DIRECT CONTACT:
    If you need to reach them directly: {plumber_number.replace('+27', '0').replace('+', '')}

    You don't need to do anything now — just wait for their call. They're very responsive!

    Questions? Feel free to ask here anytime 😊
    """
            return completion_message
        except Exception as e:
            return "Your plan has been uploaded successfully. Our plumber will review it and contact you within 24 hours."


    def detect_service_inquiry(self, message):
        try:
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are an intent classifier for a Zimbabwean plumbing company. Return ONLY valid JSON, no markdown."},
                    {"role": "user", "content": f"""Classify the customer's message into ONE of these intents.

    Customer message: "{message}"

    INTENTS:
    - tub_sales: asking if we sell tubs or about small bathroom tubs
    - standalone_tub: asking about standalone/freestanding tub price or availability
    - geyser: asking about geyser installation or pricing
    - shower_cubicle: asking about shower cubicles, pricing, installation
    - vanity: asking about vanity units, custom vanity
    - bathtub_installation: asking about installing a bathtub, wall finishing around tub
    - toilet: asking about toilet supply or installation
    - chamber: asking about side chamber, chamber supply or installation
    - facebook_package: referencing a Facebook ad or package deal
    - location_ask: customer is ONLY asking where we are located or for our address
    - location_visit: customer wants to physically come IN PERSON to our office or showroom
    - previous_quotation: saying we sent them a quotation before
    - pictures: asking to see product pictures (not previous work photos)
    - none: none of the above

    Return ONLY this JSON:
    {{
        "intent": "one of the intents above",
        "confidence": "HIGH or LOW"
    }}"""}
                ],
                temperature=0.1,
                max_tokens=50
            )
            ai_response = response.choices[0].message.content.strip()
            ai_response = ai_response.replace('```json', '').replace('```', '').strip()
            result = json.loads(ai_response)
            print(f"🤖 Service inquiry detection: '{message}' → {result}")
            return result
        except Exception as e:
            print(f"❌ Service inquiry detection error: {str(e)}")
            return {"intent": "none", "confidence": "LOW"}


    def handle_service_inquiry(self, intent, message):
        """Generate response for product/service/pricing inquiries in English or Shona."""
        try:
            lang_response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Detect the language of this message. Reply with ONLY 'shona', 'english', or 'mixed'."},
                    {"role": "user", "content": message}
                ],
                temperature=0.1,
                max_tokens=5
            )
            language = lang_response.choices[0].message.content.strip().lower()

            plumber_number = self.appointment.plumber_contact_number or '+263610318200'

            pricing_info = {
                "tub_sales": {
                    "en": f"We don't operate as a retail store, but we can supply and install tubs as part of a renovation project. 🛁 Standalone tubs start from US$400 depending on the design and quality.\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWould you like us to assist with supply and installation? 😊",
                    "sn": f"Hatitengesi setshopu, asi tinogona kuita supply neinstallation yetub sebhizimisi rekuvandudza imba yako. 🛁 Tubs dzinotangira kuUS$400 zvichienda nedhizaini nequality.\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nUnoda here kuti tikubatsire? 😊"
                },
                "standalone_tub": {
                    "en": f"Standalone tubs start from US$400, depending on the design and quality. 🛁\n\nFor bathtub installation:\n• Ordinary tub (with wall finishing) starts from US$80\n• Free-standing tub supply starts from US$450\n• Free-standing mixer starts from US$150\n• Mixer installation: US$120\n• Side chamber: US$130 (installation US$30)\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWould you like installation included as well? 🔧✨",
                    "sn": f"Tubs dzinomira dzega dzinotangira kuUS$400. 🛁\n\nNeinstallation:\n• Tub yakajairwa: inotangira kuUS$80\n• Free-standing tub: inotangira kuUS$450\n• Free-standing mixer: US$150\n• Kuisa mixer: US$120\n• Side chamber: US$130 (installation US$30)\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸.\n\nUnoda here installation zvakare? 🔧"
                },
                "geyser": {
                    "en": f"Yes, we do geyser installations! 🔥\n\nGeyser installation starts from US$80, depending on the type and size of the geyser.\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWhat size geyser are you installing?",
                    "sn": f"Hongu, tinoisa ma geyser! 🔥\n\nKuisa geyser kunotangira kuUS$80.\n\n⚠️ Mutengo uyu wakangofanana. Tumira photo kana plan 📸.\n\nGeyser yekuisa yakura zvakadini?"
                },
                "shower_cubicle": {
                    "en": f"We supply and install shower cubicles! 🚿\n\n• Ordinary shower cubicle (900mm x 900mm): starts from US$130\n• Installation: starts from US$40\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWould you like supply and installation together?",
                    "sn": f"Tinopa uye tinoisa ma shower cubicle! 🚿\n\n• Shower cubicle yakajairwa: inotangira kuUS$130\n• Kuisa: inotangira kuUS$40\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸.\n\nUnoda here supply neinstallation pamwechete?"
                },
                "vanity": {
                    "en": f"Yes, we do custom-made vanity units! 🪞\n\n• Vanity units start from US$150\n• Labour starts from US$30\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWhat size are you looking for?",
                    "sn": f"Hongu, tinoita ma vanity unit akagadzirwa zvaunoda! 🪞\n\n• Ma vanity unit anotangira kuUS$150\n• Kubhadhara vashandi kunotangira kuUS$30\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸.\n\nUnoda ukuru wakaita sei?"
                },
                "bathtub_installation": {
                    "en": f"Here are our bathtub installation prices: 🛁\n\n• Ordinary tub installation (with wall finishing): from US$80\n• Free-standing tub supply: from US$450\n• Free-standing mixer: from US$150\n• Mixer installation: US$120\n• Side chamber: US$130\n• Side chamber installation: US$30\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWhat type of bathtub are you interested in?",
                    "sn": f"Mitengo yedu yekuisa ma bathtub: 🛁\n\n• Tub yakajairwa: kubva kuUS$80\n• Free-standing tub: kubva kuUS$450\n• Free-standing mixer: kubva kuUS$150\n• Kuisa mixer: US$120\n• Side chamber: US$130\n• Kuisa side chamber: US$30\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸.\n\nUnoda mhando ipi yebathtub?"
                },
                "toilet": {
                    "en": f"We supply and install toilets and side chambers!\n\n• Close-coupled toilet supply: starts from US$50\n• New toilet seat installation: starts from US$20\n• Side chamber: US$130\n• Side chamber installation: US$30\n\nThese are approximate prices. For a more accurate quotation, please send a plan/photo or schedule a site visit.\n\nWould you like supply and installation?",
                    "sn": f"Tinopa uye tinoisa ma toilet nema side chamber!\n\n• Close-coupled toilet: inotangira kuUS$50\n• Kuisa chigaro chitsva: inotangira kuUS$20\n• Side chamber: US$130\n• Kuisa side chamber: US$30\n\nMitengo iyi yakangofanana. Tumira photo kana plan kana tibvumirene visit.\n\nUnoda here supply neinstallation?",
                },
                "chamber": {
                    "en": f"We supply and install side chambers and toilets!\n\n• Side chamber: US$130\n• Side chamber installation: US$30\n• Close-coupled toilet supply: starts from US$50\n• New toilet seat installation: starts from US$20\n\nThese are approximate prices. For a more accurate quotation, please send a plan/photo or schedule a site visit.\n\nWould you like supply and installation?",
                    "sn": f"Tinopa uye tinoisa ma side chamber nema toilet!\n\n• Side chamber: US$130\n• Kuisa side chamber: US$30\n• Close-coupled toilet: inotangira kuUS$50\n• Kuisa chigaro chitsva: inotangira kuUS$20\n\nMitengo iyi yakangofanana. Tumira photo kana plan kana tibvumirene visit.\n\nUnoda here supply neinstallation?",
                },
                "facebook_package": {
                    "en": f"The bathroom package shown on our Facebook ad starts from US$600. 📢\n\n⚠️ This is an approximate price and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWould you like us to assess your space first?",
                    "sn": f"Package yebathroom yatakaiswa pa Facebook inotangira kuUS$600. 📢\n\n⚠️ Mutengo uyu wakangofanana. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nUnoda here kuti tiuye titarise nzvimbo yako?"
                },
                "location_ask": {
                    "en": "We are based in Hatfield, Harare. 📍\n\n",
                    "sn": "Tiri muHatfield, Harare. 📍\n\n"
                },
                "location_visit": {
                    "en": "We operate by appointment rather than walk-ins. 📍 We're based in Hatfield, Harare.\n\nWould you like us to arrange a site visit to your place instead?",
                    "sn": "Tinoshandisa ne appointment, hatisi kushanda ne walk-ins. 📍 Tiri muHatfield, Harare.\n\nUnoda here kuti tiuye kwauri?"
                },
                "previous_quotation": {
                    "en": f"Kindly contact our plumber directly and they will assist you with your previous quotation. 📄\n\nYou can reach them on: {plumber_number}",
                    "sn": f"Ndapota taura neplumber yedu directly uye vachakubatsira nequotation yako yekare. 📄\n\nUnogona kubata: {plumber_number}"
                },
                "pictures": {
                    "en": (
                        "Those are some of our recent jobs 💪\n\n"
                        "Anything there you'd like for your space? We can do a *free site visit* "
                        "to show you exactly what's possible — and give you an accurate price.\n\n"
                        "Just let us know your area and when suits you! 😊"
                    ),
                    "sn": (
                        "Idzo ndeimwe yemabasa edu achangopera 💪\n\n"
                        "Pane chimwe chaunoda mune nzvimbo yako? Tinogona kuita *free site visit* "
                        "kuti tikuratidze zvinogoneka — uye tikupe mutengo wakakwana.\n\n"
                        "Tiudze area yako uye nguva inokubatsira! 😊"
                    ),
                },
            }

            responses = pricing_info.get(intent, {})
            if language == 'shona':
                reply = responses.get('sn', responses.get('en', ''))
            else:
                reply = responses.get('en', '')

            if not reply:
                reply = self.generate_contextual_response(message, self.get_next_question_to_ask(), [])

            # ── FIX 2: Append site-visit close if no clear next-step present ──
            site_visit_triggers = [
                'site visit', 'send a plan', 'send plan', 'accurate quotation',
                'accurate quote', 'would you like', 'shall we',
                'book an appointment', 'free site',
            ]
            if not any(t in reply.lower() for t in site_visit_triggers):
                reply += (
                    "\n\nWould you like us to come do a *free site visit* and give you "
                    "an exact price? Just let us know your area and when suits you 😊"
                )

            return reply

        except Exception as e:
            print(f"❌ Error handling service inquiry: {str(e)}")
            return self.generate_contextual_response(message, self.get_next_question_to_ask(), [])

    def generate_pricing_overview(self, message):
        inquiry = self.detect_service_inquiry(message)
        if inquiry.get('intent') != 'none' and inquiry.get('confidence') == 'HIGH':
            return self.handle_service_inquiry(inquiry['intent'], message)
        return """Here are our approximate prices 😊

🛁 *Bathroom Renovation*
- Full renovation: from US$600
- Bathtub installation (with wall finishing): from US$80
- Standalone/freestanding tub: from US$450
- Free-standing mixer: from US$150

🚿 *Shower*
- Shower cubicle (900x900mm): from US$130
- Installation: from US$40

🚽 *Toilet & Chamber*
- Close-coupled toilet supply: from US$50
- Toilet installation: from US$20
- Side chamber: US$130 (installation US$30)

🔥 *Geyser*
- Installation: from US$80

🪞 *Vanity Units*
- Custom vanity: from US$150

⚠️ These are approximate prices and may vary depending on the scope of work on site. For an accurate quote, we can do a *site visit* or you can send us a *photo/plan*.

Which were you looking at — supply only or supply + install??"""

    def notify_plumber_about_plan(self):
        try:
            base_url = os.getenv("SITE_URL", "http://127.0.0.1:8000")
            service_name = self.appointment.project_type.replace('_', ' ').title()
            customer_name = self.appointment.customer_name or "Customer"
            customer_phone = self.phone_number.replace('whatsapp:', '')
            details_url = f"{base_url}/appointments/{self.appointment.id}/documents/"
            plumber_message = f"""📋 NEW PLAN RECEIVED!\n\nCustomer: {customer_name}\nPhone: {customer_phone}\nService: {service_name}\nArea: {self.appointment.customer_area}\nProperty: {self.appointment.property_type}\nTimeline: {self.appointment.timeline}\n\n🔗 View full details:\n{details_url}\n\nStatus: Plan uploaded — awaiting your review"""
            plumber_numbers = ['263610318200']
            for number in plumber_numbers:
                whatsapp_api.send_text_message(number, plumber_message)
        except Exception as e:
            print(f"❌ Error notifying plumber: {str(e)}")

    def handle_post_upload_messages(self, message):
        try:
            message_lower = message.lower()
            if any(word in message_lower for word in ['status', 'update', 'heard', 'contact', 'call']):
                return self.provide_plan_status_update()
            if any(word in message_lower for word in ['change', 'update', 'modify', 'different', 'new plan']):
                return self.handle_plan_change_request()
            if any(word in message_lower for word in ['urgent', 'asap', 'emergency', 'rush']):
                return self.handle_urgent_plan_request()
            return """Your plan has been sent to our plumber and they'll contact you within 24 hours.\n\nIf you need immediate assistance:\n📞 Call directly: 0610318200\n\nOtherwise, please wait for their review and call. They're very reliable!\n\nNeed to change something about your plan? Let me know."""
        except Exception as e:
            return "Your plan is with our plumber for review. They'll contact you within 24 hours."

    def provide_plan_status_update(self):
        upload_time = self.appointment.updated_at
        hours_since = (timezone.now() - upload_time).total_seconds() / 3600
        if hours_since < 24:
            remaining_hours = int(24 - hours_since)
            return f"""📋 PLAN STATUS UPDATE:\n\nYour plan was sent {int(hours_since)} hours ago. Our plumber typically responds within 24 hours.\n\nExpected contact: Within the next {remaining_hours} hours\n\nIf it's urgent, you can call directly: 0610318200"""
        else:
            return """I see it's been over 24 hours since your plan was sent.\n\nPlease call our plumber directly at 0610318200 - they may have tried to reach you already."""

    def handle_plan_change_request(self):
        self.appointment.plan_status = 'pending_upload'
        self.appointment.save()
        return """No problem! I can help you send an updated plan.\n\nPlease send your revised plan materials now (images or PDF).\n\nI'll make sure the plumber gets the updated version."""

    def handle_urgent_plan_request(self):
        try:
            urgent_message = f"""🚨 URGENT PLAN REVIEW REQUEST\n\nCustomer: {self.appointment.customer_name or 'Customer'}\nPhone: {self.phone_number.replace('whatsapp:', '')}\nProject: {self.appointment.project_type}\n\nCustomer is requesting urgent review.\n\nPlease contact ASAP: {self.phone_number.replace('whatsapp:', '')}"""
            twilio_client.messages.create(body=urgent_message, from_=TWILIO_WHATSAPP_NUMBER, to='whatsapp:+0610318200')
            return """🚨 I've marked your plan review as URGENT and notified our plumber immediately.\n\nThey should contact you within the next few hours.\n\nFor immediate assistance, you can also call: 0610318200"""
        except Exception as e:
            return "I've noted this is urgent. Please call our plumber directly at 0610318200 for immediate assistance."


    def get_alternative_time_suggestions(self, requested_datetime):
        try:
            suggestions = []
            requested_date = requested_datetime.date()
            business_time_slots = [8, 10, 12, 14, 16]
            for day_offset in range(0, 7):
                check_date = requested_date + timedelta(days=day_offset)
                if check_date.weekday() == 5:
                    continue
                for hour in business_time_slots:
                    candidate_time = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    candidate_datetime = sa_timezone.localize(candidate_time)
                    if candidate_datetime <= timezone.now():
                        continue
                    if candidate_datetime == requested_datetime:
                        continue
                    is_available, conflict = self.check_appointment_availability(candidate_datetime)
                    if is_available:
                        day_type = 'same_day' if day_offset == 0 else 'next_days'
                        suggestions.append({'datetime': candidate_datetime, 'display': candidate_datetime.strftime('%A, %B %d at %I:%M %p'), 'day_type': day_type})
                        if len(suggestions) >= 4:
                            break
                if len(suggestions) >= 4:
                    break
            return suggestions
        except Exception as e:
            return []


    def get_appointment_context(self):
        try:
            context_parts = []
            context_parts.append(f"Customer Name: {self.appointment.customer_name or 'Not provided yet'}")
            context_parts.append(f"Area: {self.appointment.customer_area or 'Not provided yet'}")
            context_parts.append(f"Service Type: {self.appointment.project_type or 'Not specified yet'}")
            if self.appointment.has_plan is True:
                context_parts.append("Plan Status: Customer has existing plan")
            elif self.appointment.has_plan is False:
                context_parts.append("Plan Status: Customer wants site visit")
            else:
                context_parts.append("Plan Status: Not specified yet")
            context_parts.append(f"Property Type: {self.appointment.property_type or 'Not specified yet'}")
            context_parts.append(f"Timeline: {self.appointment.timeline or 'Not specified yet'}")
            context_parts.append(f"Current Status: {self.appointment.get_status_display()}")
            if self.appointment.scheduled_datetime:
                try:
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    sa_time = self.appointment.scheduled_datetime.astimezone(sa_timezone)
                    formatted_datetime = sa_time.strftime('%A, %B %d, %Y at %I:%M %p')
                    context_parts.append(f"Scheduled: {formatted_datetime}")
                    context_parts.append(f"⚠️ CRITICAL: When mentioning appointment time, ALWAYS use: {formatted_datetime}")
                except Exception as dt_error:
                    context_parts.append("Scheduled: Error reading datetime")
            else:
                context_parts.append("Scheduled: No appointment time set yet")
            next_question = self.get_next_question_to_ask()
            context_parts.append(f"Next Question Needed: {next_question}")
            retry_count = getattr(self.appointment, 'retry_count', 0)
            context_parts.append(f"Question Retry Count: {retry_count}")
            completeness = self.appointment.get_customer_info_completeness()
            context_parts.append(f"Info Completeness: {completeness:.0f}%")
            return "\n".join(context_parts)
        except Exception as e:
            return "Unable to load appointment context"


    def verify_plan_question_not_asked_recently(self):
        try:
            if not self.appointment.conversation_history:
                return False
            recent_messages = self.appointment.conversation_history[-5:]
            plan_keywords = ['have a plan', 'site visit', 'existing plan', 'Do you have']
            for msg in recent_messages:
                if msg.get('role') == 'assistant':
                    content = msg.get('content', '').lower()
                    if any(keyword.lower() in content for keyword in plan_keywords):
                        return True
            return False
        except Exception as e:
            return False

    def update_appointment_with_extracted_data(self, extracted_data):
        try:
            updated_fields = []
            next_question = self.get_next_question_to_ask()
            
            if (extracted_data.get('service_type') and extracted_data.get('service_type') != 'null' and not self.appointment.project_type):
                self.appointment.project_type = extracted_data['service_type']
                updated_fields.append('service_type')
            
            if extracted_data.get('plan_status') and extracted_data.get('plan_status') != 'null':
                if self.appointment.has_plan is not None:
                    print(f"🛡️ SAFETY: Blocked plan_status update - already set to {self.appointment.has_plan}")
                elif next_question != "plan_or_visit":
                    print(f"🛡️ SAFETY: Blocked plan_status update - not currently asking about plans")
                else:
                    old_value = self.appointment.has_plan
                    plan_status = str(extracted_data['plan_status']).lower().strip()
                    has_plan_indicators = ['has_plan', 'has plan', 'have plan', 'got plan', 'yes', 'yep', 'yeah', 'yup', 'true', 'have it', 'got it', 'i do', 'i have']
                    needs_visit_indicators = ['needs_visit', 'needs visit', 'need visit', 'site visit', 'site_visit', 'no', 'nope', 'nah', 'false', 'no plan', 'dont have', "don't have", 'visit', 'prefer visit']
                    if any(indicator in plan_status for indicator in has_plan_indicators):
                        self.appointment.has_plan = True
                        updated_fields.append('plan_status')
                    elif any(indicator in plan_status for indicator in needs_visit_indicators):
                        self.appointment.has_plan = False
                        updated_fields.append('plan_status')
            
            if (extracted_data.get('area') and extracted_data.get('area') != 'null' and not self.appointment.customer_area):
                self.appointment.customer_area = extracted_data['area']
                updated_fields.append('area')
            
            if (extracted_data.get('timeline') and extracted_data.get('timeline') != 'null' and not self.appointment.timeline):
                timeline_value = extracted_data['timeline']
                saturday_indicators = ['saturday', 'sat']
                if any(s in timeline_value.lower() for s in saturday_indicators):
                    extracted_data['timeline'] = None
                else:
                    self.appointment.timeline = timeline_value
                    updated_fields.append('timeline')
            
            if (extracted_data.get('property_type') and extracted_data.get('property_type') != 'null' and not self.appointment.property_type):
                self.appointment.property_type = extracted_data['property_type']
                updated_fields.append('property_type')
            
            if (extracted_data.get('availability') and extracted_data.get('availability') != 'null'):
                try:
                    parsed_dt = datetime.strptime(extracted_data['availability'], '%Y-%m-%dT%H:%M')
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    localized_dt = sa_timezone.localize(parsed_dt)
                    self.appointment.scheduled_datetime = localized_dt
                    updated_fields.append('availability')
                    if not self.appointment.timeline:
                        self.appointment.timeline = localized_dt.strftime('%A, %B %d')
                        updated_fields.append('timeline')
                except ValueError as e:
                    print(f"❌ Failed to parse AI datetime: {extracted_data['availability']} — {e}")
            
            if (extracted_data.get('customer_name') and extracted_data.get('customer_name') != 'null' and not self.appointment.customer_name):
                if self.is_valid_name(extracted_data['customer_name']):
                    self.appointment.customer_name = extracted_data['customer_name']
                    updated_fields.append('customer_name')
            
            if updated_fields:
                self.appointment.save()
                refresh_lead_score(self.appointment)
            
            return updated_fields
        except Exception as e:
            print(f"❌ Error updating appointment: {str(e)}")
            return []

    def get_information_summary(self):
        try:
            return {
                'service_type': self.appointment.project_type,
                'has_plan': self.appointment.has_plan,
                'area': self.appointment.customer_area,
                'timeline': self.appointment.timeline,
                'property_type': self.appointment.property_type,
                'scheduled_datetime': self.appointment.scheduled_datetime.isoformat() if self.appointment.scheduled_datetime else None,
                'customer_name': self.appointment.customer_name,
                'status': self.appointment.status,
                'completion_percentage': self.smart_booking_check()['completion_percentage']
            }
        except Exception as e:
            return {}


    def process_alternative_time_selection(self, message):
        try:
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            now = timezone.now().astimezone(sa_timezone)
            day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            next_days = {}
            for i, name in enumerate(day_names):
                days_ahead = (i - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                next_days[name] = (now + timedelta(days=days_ahead)).strftime('%B %d, %Y')

            prompt = f"""You are a datetime extraction assistant.
    CURRENT DATETIME: {now.strftime('%Y-%m-%d %H:%M')} (Africa/Johannesburg)
    WORKING DAYS: Sunday–Friday (Saturday CLOSED)
    NEXT OCCURRENCE OF EACH DAY: Monday: {next_days['monday']}, Tuesday: {next_days['tuesday']}, Wednesday: {next_days['wednesday']}, Thursday: {next_days['thursday']}, Friday: {next_days['friday']}, Saturday: {next_days['saturday']} ← CLOSED, Sunday: {next_days['sunday']}, Tomorrow: {(now + timedelta(days=1)).strftime('%B %d, %Y')}
    CUSTOMER MESSAGE: "{message}"
    Return ONLY one of: YYYY-MM-DDTHH:MM, SATURDAY_CLOSED, or NOT_FOUND"""

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Return only a datetime string YYYY-MM-DDTHH:MM, SATURDAY_CLOSED, or NOT_FOUND."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=25
            )
            ai_response = response.choices[0].message.content.strip()
            if ai_response in ("SATURDAY_CLOSED", "NOT_FOUND"):
                return None
            parsed_dt = datetime.strptime(ai_response, '%Y-%m-%dT%H:%M')
            localized_dt = sa_timezone.localize(parsed_dt)
            return localized_dt
        except Exception as e:
            return None


    def book_appointment_with_selected_time(self, selected_datetime):
        try:
            is_available, conflict_info = self.check_appointment_availability(selected_datetime)
            if is_available:
                self.appointment.scheduled_datetime = selected_datetime
                self.appointment.save(update_fields=['scheduled_datetime'])
                result = self.book_appointment(message=None)
                if result['success']:
                    return result
                alternatives = self.get_alternative_time_suggestions(selected_datetime)
                return {'success': False, 'error': 'Time became unavailable', 'alternatives': alternatives}
            else:
                alternatives = self.get_alternative_time_suggestions(selected_datetime)
                return {'success': False, 'error': 'Selected time not available', 'alternatives': alternatives}
        except Exception as e:
            return {'success': False, 'error': str(e)}


    def extract_appointment_details(self):
        try:
            details = {}
            if self.appointment.customer_name: details['name'] = self.appointment.customer_name
            if self.appointment.customer_area: details['area'] = self.appointment.customer_area
            if self.appointment.project_type: details['project_type'] = self.appointment.project_type
            if self.appointment.property_type: details['property_type'] = self.appointment.property_type
            if self.appointment.timeline: details['timeline'] = self.appointment.timeline
            if self.appointment.has_plan is not None: details['has_plan'] = self.appointment.has_plan
            return details
        except Exception as e:
            return {}


    def extract_all_available_info_with_ai(self, message):
        try:
            current_context = self.get_appointment_context()
            next_question = self.get_next_question_to_ask()
            current_time = timezone.now().strftime('%Y-%m-%d %H:%M')
            
            extraction_prompt = f"""
            You are a comprehensive data extraction assistant for a plumbing appointment system.
            CRITICAL: Return ONLY a valid JSON object with no markdown formatting.
            
            CURRENT APPOINTMENT STATE:
            {current_context}
            
            NEXT QUESTION WE NEED: {next_question}
            CUSTOMER MESSAGE: "{message}"
            
            EXTRACTION RULES:
            1. ONLY extract information CLEARLY present in the message
            2. DO NOT GUESS - if not explicit, return null
            3. For plan_status: ONLY extract if next_question = "plan_or_visit"
            
            CURRENT QUESTION CHECK: {next_question}
            IF next_question IS NOT "plan_or_visit": ALWAYS return null for plan_status
            
            Return EXACTLY this JSON:
            {{
                "service_type": "extracted_value_or_null",
                "plan_status": "extracted_value_or_null", 
                "area": "extracted_value_or_null",
                "timeline": "extracted_value_or_null",
                "property_type": "extracted_value_or_null",
                "availability": "extracted_value_or_null",
                "customer_name": "extracted_value_or_null"
            }}
            
            CURRENT DATE: {current_time}
            Extract from: "{message}"
            """
            
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Return ONLY valid JSON. NEVER extract plan_status unless actively asking about it."},
                    {"role": "user", "content": extraction_prompt}
                ],
                temperature=0.1,
                max_tokens=200
            )
            
            ai_response = response.choices[0].message.content.strip()
            ai_response = ai_response.replace('```json', '').replace('```', '').strip()
            
            try:
                extracted_data = json.loads(ai_response)
                if self.appointment.has_plan is not None and extracted_data.get('plan_status'):
                    extracted_data['plan_status'] = None
                return extracted_data
            except json.JSONDecodeError:
                return {}
        except Exception as e:
            return {}


    def get_next_question_to_ask(self):
        """Determine which question to ask next - FIXED for early uploads"""
        
        # ── FIX 6: Infer project_type from sent_pricing_intents ──────────────
        if not self.appointment.project_type:
            _intent_map = {
                "toilet":               "bathroom_renovation",
                "chamber":              "bathroom_renovation",
                "standalone_tub":       "bathroom_renovation",
                "bathtub_installation": "bathroom_renovation",
                "tub_sales":            "bathroom_renovation",
                "shower_cubicle":       "bathroom_renovation",
                "vanity":               "bathroom_renovation",
                "geyser":               "bathroom_renovation",
            }
            _sent = list(getattr(self.appointment, 'sent_pricing_intents', None) or [])
            for _intent in _sent:
                if _intent in _intent_map:
                    self.appointment.project_type = _intent_map[_intent]
                    self.appointment.save(update_fields=["project_type"])
                    print(f"✅ FIX 6: Inferred project_type='{self.appointment.project_type}' from sent_pricing_intents")
                    break

        if not self.appointment.project_type:
            return "service_type"
        
        if self.appointment.has_plan is None:
            if not self.appointment.plan_file:
                return "plan_or_visit"
            else:
                print(f"⏭️ Skipping plan question - customer already uploaded plan")
                self.appointment.has_plan = True
                self.appointment.save()
        
        if self.appointment.has_plan is True:
            if not self.appointment.plan_file and self.appointment.plan_status not in ('plan_uploaded', 'plan_reviewed', 'ready_to_book'):
                return "initiate_plan_upload"
            if self.appointment.plan_status == 'pending_upload' and self.appointment.plan_file:
                return "awaiting_plan_upload"
            if self.appointment.plan_status == 'plan_uploaded':
                return "plan_with_plumber"
            if not self.appointment.customer_area:
                return "area"
            if not self.appointment.property_type:
                return "property_type"

        if self.appointment.has_plan is False:
            if not self.appointment.customer_area:
                return "area"
            if not self.appointment.timeline:
                return "timeline"
            if not self.appointment.property_type:
                return "property_type"
            if not self.appointment.scheduled_datetime:
                return "availability"
            if not self.appointment.customer_name and self.appointment.status == 'confirmed':
                return "name"
        
        return "complete"


    def smart_booking_check(self):
        required_for_booking = [
            self.appointment.project_type,
            self.appointment.has_plan is not None,
            self.appointment.customer_area,
            self.appointment.timeline,
            self.appointment.property_type,
            self.appointment.scheduled_datetime
        ]
        has_all_required = all(required_for_booking)
        missing_fields = []
        if not self.appointment.project_type: missing_fields.append("service type")
        if self.appointment.has_plan is None: missing_fields.append("plan preference")
        if not self.appointment.customer_area: missing_fields.append("area")
        if not self.appointment.timeline: missing_fields.append("timeline")
        if not self.appointment.property_type: missing_fields.append("property type")
        if not self.appointment.scheduled_datetime: missing_fields.append("availability")
        return {'ready_to_book': has_all_required, 'missing_fields': missing_fields, 'completion_percentage': ((6 - len(missing_fields)) / 6) * 100}


    def check_appointment_availability(self, requested_datetime):
        try:
            if requested_datetime.tzinfo is None:
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                requested_datetime = sa_timezone.localize(requested_datetime)
            appointment_duration = timedelta(hours=2)
            requested_end = requested_datetime + appointment_duration
            now = timezone.now()
            min_booking_time = now + timedelta(hours=1)
            if requested_datetime <= min_booking_time:
                return False, "too_soon"
            weekday = requested_datetime.weekday()
            if weekday == 5:
                self.appointment.scheduled_datetime = None
                self.appointment.save()
                return False, "saturday_closed"
            hour = requested_datetime.hour
            if hour < 8 or hour >= 18:
                return False, "outside_business_hours"
            if requested_end.hour > 18 or (requested_end.hour == 18 and requested_end.minute > 0):
                return False, "ends_after_hours"
            conflicting_appointments = Appointment.objects.filter(status='confirmed', scheduled_datetime__isnull=False).exclude(id=self.appointment.id)
            for existing_appt in conflicting_appointments:
                if existing_appt.scheduled_datetime.tzinfo is None:
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    existing_start = sa_timezone.localize(existing_appt.scheduled_datetime)
                else:
                    existing_start = existing_appt.scheduled_datetime
                existing_end = existing_start + appointment_duration
                if (requested_datetime < existing_end and requested_end > existing_start):
                    return False, existing_appt
            max_advance_time = now + timedelta(days=90)
            if requested_datetime > max_advance_time:
                return False, "too_far_ahead"
            return True, None
        except Exception as e:
            return False, "error"


    def format_datetime_for_display(self, dt):
        try:
            if dt.tzinfo is None:
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                dt = sa_timezone.localize(dt)
            else:
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                dt = dt.astimezone(sa_timezone)
            return dt
        except Exception as e:
            return dt


    def send_confirmation_message(self, appointment_info, appointment_datetime):
        try:
            display_datetime = self.format_datetime_for_display(appointment_datetime)
            service_name = appointment_info.get('project_type', 'Plumbing service')
            if service_name:
                service_map = {'bathroom_renovation': 'Bathroom Renovation', 'new_plumbing_installation': 'New Plumbing Installation', 'kitchen_renovation': 'Kitchen Renovation'}
                service_name = service_map.get(service_name, service_name.replace('_', ' ').title())
            confirmation_message = f"""🔧 APPOINTMENT CONFIRMED! 🔧\n\nHi {appointment_info.get('name', 'there')},\n\nYour plumbing appointment is confirmed:\n📅 Date: {display_datetime.strftime('%A, %B %d, %Y')}\n🕐 Time: {display_datetime.strftime('%I:%M %p')}\n📍 Area: {appointment_info.get('area', 'Your area')}\n🔨 Service: {service_name}\n\nOur team will contact you before arrival.\n\nQuestions? Reply to this message.\n\nThank you for choosing us.\n- Homebase Plumbers"""
            clean_phone = clean_phone_number(self.phone_number)
            whatsapp_api.send_text_message(clean_phone, confirmation_message)
        except Exception as e:
            print(f"❌ Confirmation message error: {str(e)}")


    def notify_team(self, appointment_info, appointment_datetime):
        try:
            import os
            display_datetime = self.format_datetime_for_display(appointment_datetime)
            service_name = appointment_info.get('project_type', 'Plumbing service')
            if service_name:
                service_map = {'bathroom_renovation': 'Bathroom Renovation', 'new_plumbing_installation': 'New Plumbing Installation', 'kitchen_renovation': 'Kitchen Renovation'}
                service_name = service_map.get(service_name, service_name.replace('_', ' ').title())
            plan_status = "Not specified"
            if appointment_info.get('has_plan') is not None:
                plan_status = "Has existing plan" if appointment_info['has_plan'] else "Needs site visit"
            from bot.whatsapp_webhook import generate_conversation_summary
            ai_summary = generate_conversation_summary(self.appointment)
            customer_phone = self.phone_number.replace('whatsapp:+', '').replace('whatsapp:', '').replace('+', '')
            team_message = (
                f"🚨 NEW APPOINTMENT BOOKED!\n\n"
                f"👤 Customer: {appointment_info.get('name', 'Unknown')}\n"
                f"📞 Phone: +{customer_phone}\n"
                f"💬 WhatsApp: wa.me/{customer_phone}\n\n"
                f"📋 APPOINTMENT DETAILS:\n"
                f"  📅 Date/Time: {display_datetime.strftime('%A, %B %d at %I:%M %p')}\n"
                f"  🔧 Service: {service_name}\n"
                f"  📍 Area: {appointment_info.get('area', 'Not provided')}\n"
                f"  🏠 Property: {appointment_info.get('property_type', 'Not specified')}\n"
                f"  ⏰ Timeline: {appointment_info.get('timeline', 'Not specified')}\n"
                f"  📐 Plan: {plan_status}\n\n"
                f"🤖 AI SUMMARY:\n{ai_summary}\n\n"
                f"🔗 View: https://plumbotv1-production.up.railway.app/appointments/{self.appointment.id}/"
            )
            team_numbers = []
            env_numbers = os.environ.get('TEAM_NUMBERS', '')
            for n in env_numbers.replace('\n', ',').split(','):
                n = n.strip().replace('whatsapp:', '').replace('+', '')
                if n:
                    team_numbers.append(n)
            plumber_contact = getattr(self.appointment, 'plumber_contact_number', None)
            if plumber_contact:
                n = plumber_contact.replace('whatsapp:', '').replace('+', '').strip()
                if n and n not in team_numbers:
                    team_numbers.append(n)
            if not team_numbers:
                team_numbers = ['263610318200']
            for number in team_numbers:
                try:
                    whatsapp_api.send_text_message(number, team_message)
                except Exception as msg_error:
                    print(f"❌ Failed to send to {number}: {msg_error}")
        except Exception as e:
            print(f"❌ Team notification error: {str(e)}")

                
    def add_to_google_calendar(self, appointment_info, appointment_datetime):
        try:
            if not GOOGLE_CALENDAR_CREDENTIALS:
                return None
            credentials = service_account.Credentials.from_service_account_info(GOOGLE_CALENDAR_CREDENTIALS, scopes=['https://www.googleapis.com/auth/calendar'])
            service = build('calendar', 'v3', credentials=credentials)
            description_parts = []
            if appointment_info.get('project_type'): description_parts.append(f"Service: {appointment_info['project_type']}")
            if appointment_info.get('area'): description_parts.append(f"Area: {appointment_info['area']}")
            if appointment_info.get('property_type'): description_parts.append(f"Property: {appointment_info['property_type']}")
            if appointment_info.get('timeline'): description_parts.append(f"Timeline: {appointment_info['timeline']}")
            if appointment_info.get('has_plan') is not None:
                description_parts.append(f"Plan Status: {'Has existing plan' if appointment_info['has_plan'] else 'Needs site visit'}")
            description_parts.append(f"Phone: {self.phone_number}")
            event = {
                'summary': f"Plumbing Appointment - {appointment_info.get('name', 'Customer')}",
                'description': "\n".join(description_parts),
                'start': {'dateTime': appointment_datetime.isoformat(), 'timeZone': 'Africa/Johannesburg'},
                'end': {'dateTime': (appointment_datetime + timedelta(hours=2)).isoformat(), 'timeZone': 'Africa/Johannesburg'},
                'reminders': {'useDefault': False, 'overrides': [{'method': 'email', 'minutes': 24 * 60}, {'method': 'popup', 'minutes': 30}]},
            }
            event_result = service.events().insert(calendarId='primary', body=event).execute()
            return event_result
        except Exception as e:
            print(f"❌ Google Calendar Error: {str(e)}")
            return None


    def book_appointment(self, message):
        try:
            appointment_datetime = self.appointment.scheduled_datetime
            if not appointment_datetime:
                return {'success': False, 'error': 'No appointment time set'}
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            if appointment_datetime.tzinfo is None:
                appointment_datetime = sa_timezone.localize(appointment_datetime)
            else:
                appointment_datetime = appointment_datetime.astimezone(sa_timezone)
            is_available, conflict_info = self.check_appointment_availability(appointment_datetime)
            if not is_available:
                alternatives = self.get_alternative_time_suggestions(appointment_datetime)
                return {'success': False, 'error': 'Time not available', 'alternatives': alternatives}
            self.appointment.status = 'confirmed'
            self.appointment.scheduled_datetime = appointment_datetime
            self.appointment.save()
            appointment_details = self.extract_appointment_details()
            try:
                self.send_confirmation_message(appointment_details, appointment_datetime)
                self.notify_team(appointment_details, appointment_datetime)
            except Exception as notify_error:
                print(f"⚠️ Notification error: {notify_error}")
            try:
                if GOOGLE_CALENDAR_CREDENTIALS:
                    self.add_to_google_calendar(appointment_details, appointment_datetime)
            except Exception as cal_error:
                print(f"⚠️ Calendar error: {cal_error}")
            display_datetime = self.format_datetime_for_display(appointment_datetime)
            return {'success': True, 'datetime': display_datetime.strftime('%B %d, %Y at %I:%M %p')}
        except Exception as e:
            print(f"❌ Booking Error: {str(e)}")
            return {'success': False, 'error': str(e)}


    def detect_reschedule_request_with_ai(self, message):
        try:
            if self.appointment.status != 'confirmed' or not self.appointment.scheduled_datetime:
                return False
            current_appt = self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')
            detection_prompt = f"""Is the customer's message requesting to reschedule their existing appointment?
CONTEXT: Customer has appointment: {current_appt}
CUSTOMER MESSAGE: "{message}"
Reply with ONLY: YES, NO, or MAYBE"""
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Reply with only YES, NO, or MAYBE."},
                    {"role": "user", "content": detection_prompt}
                ],
                temperature=0.1, max_tokens=10
            )
            ai_response = response.choices[0].message.content.strip().upper()
            return ai_response in ["YES", "MAYBE"]
        except Exception as e:
            return False


    def generate_contextual_response(self, incoming_message, next_question, updated_fields):
        try:
            if next_question == "initiate_plan_upload":
                return self.initiate_plan_upload_flow()
            if next_question == "awaiting_plan_upload":
                return "I'm waiting for your plan. Please send your images or PDF documents now."
            if next_question == "plan_with_plumber":
                return "Your plan has been sent to our plumber. They'll contact you within 24 hours to discuss the project and provide a quote."
            
            appointment_context = self.get_appointment_context()
            retry_count = getattr(self.appointment, 'retry_count', 0)
            depends_phrases = [
                'depends on', 'depend on', 'depending on', 'subject to', 'based on',
                'after the quote', 'after quote', 'after site visit', 'after assessment',
                'after seeing the work', 'once i see', 'once we see', 'wait for quote',
                'wait for the quote', 'when i get the', 'when i have the', 'after i get', 'scope of work',
            ]
            if (next_question == "timeline" and any(p in incoming_message.lower() for p in depends_phrases)):
                print("✅ FIX 4: 'Depends on quote' accepted as timeline — moving on")
                self.appointment.timeline = "After site visit / quote"
                self.appointment.save(update_fields=["timeline"])
                refresh_lead_score(self.appointment)
                next_question = self.get_next_question_to_ask()

            is_retry = retry_count > 0
            acknowledgments = []
            if 'service_type' in updated_fields:
                service_display = self.appointment.project_type.replace('_', ' ').title()
                acknowledgments.append(f"service: {service_display}")

            if next_question == "plan_or_visit" and self._plan_question_already_pending():
                clarifying_question = self.generate_clarifying_question_for_plan_status(retry_count)
                return clarifying_question

            if 'plan_status' in updated_fields:
                plan_text = "you have a plan" if self.appointment.has_plan else "you'd like a site visit"
                acknowledgments.append(f"plan status: {plan_text}")
            if 'area' in updated_fields:
                acknowledgments.append(f"area: {self.appointment.customer_area}")
            if 'property_type' in updated_fields:
                acknowledgments.append(f"property type: {self.appointment.property_type}")
            
            saturday_indicators = ['saturday', 'sat']
            if any(s in incoming_message.lower() for s in saturday_indicators):
                alternatives = self.get_alternative_time_suggestions(timezone.now() + timedelta(days=1))
                alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives]) if alternatives else ""
                reply = "We unfortunately don't operate on Saturdays. 😊\n\nOur working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                if alt_text:
                    reply += f"Here are some available slots:\n{alt_text}\n\nOr feel free to suggest a different date and time!"
                else:
                    reply += "Could you please choose a different day that works for you?"
                return reply

            system_prompt = f"""
            You are a professional appointment assistant for a luxury plumbing company in Zimbabwe.

            LANGUAGE RULES - CRITICAL:
            - DEFAULT language is English. Always respond in English unless the customer clearly uses Shona.
            - If the customer writes ONLY in Shona, respond in Shona. Otherwise use English.

            SITUATION ANALYSIS:
            - Customer provided new information: {updated_fields if updated_fields else 'None'}
            - Next question needed: {next_question}
            - Retry attempt: {retry_count}
            
            CURRENT APPOINTMENT STATE:
            {appointment_context}
                        
            CRITICAL CONTEXT PRESERVATION RULES:
            1. NEVER ask for information already in appointment context
            2. Only ask for genuinely missing information

            QUESTION TEMPLATES:
            - service_type: "Which service are you interested in? We offer: Bathroom Renovation, New Plumbing Installation, or Kitchen Renovation"
            - plan_or_visit: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"
            - area: "Which area are you located in? (e.g. Harare Hatfield, Harare Avondale)"
            - timeline: "When were you hoping to get this done?"
            - property_type: "Is this for a house, apartment, or business?"
            - availability: "When would you be available for an appointment? Please provide both the day and time (e.g., Monday at 2pm, tomorrow at 10am)"
            - name: "To complete your booking, may I have your full name?"
            
            Generate response:"""
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Customer message: '{incoming_message}'"}
            ]
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.7,
                max_tokens=150
            )
            reply = response.choices[0].message.content.strip()
            if updated_fields:
                self.appointment.retry_count = 0
                self.appointment.save()
            else:
                self.appointment.retry_count = getattr(self.appointment, 'retry_count', 0) + 1
                self.appointment.save()
            return reply
        except Exception as e:
            return "I understand. Let me ask you about the next detail we need for your appointment."


    def smart_booking_check(self):
        required_for_booking = [
            self.appointment.project_type,
            self.appointment.has_plan is not None,
            self.appointment.customer_area,
            self.appointment.timeline,
            self.appointment.property_type,
            self.appointment.scheduled_datetime
        ]
        has_all_required = all(required_for_booking)
        missing_fields = []
        if not self.appointment.project_type: missing_fields.append("service type")
        if self.appointment.has_plan is None: missing_fields.append("plan preference")
        if not self.appointment.customer_area: missing_fields.append("area")
        if not self.appointment.timeline: missing_fields.append("timeline")
        if not self.appointment.property_type: missing_fields.append("property type")
        if not self.appointment.scheduled_datetime: missing_fields.append("availability")
        return {'ready_to_book': has_all_required, 'missing_fields': missing_fields, 'completion_percentage': ((6 - len(missing_fields)) / 6) * 100}

    def is_business_day(self, check_date):
        return check_date.weekday() != 5

    def is_business_hours(self, check_time):
        return 8 <= check_time.hour < 18

    def handle_reschedule_request_with_ai(self, message):
        try:
            current_appt = self.appointment.scheduled_datetime
            current_appt_str = current_appt.strftime('%A, %B %d at %I:%M %p')
            new_datetime = self.parse_datetime_with_ai(message)
            if new_datetime:
                is_available, conflict = self.check_appointment_availability(new_datetime)
                if is_available:
                    return self.process_successful_reschedule(current_appt, new_datetime)
                else:
                    return self.handle_unavailable_reschedule_with_ai(new_datetime, message)
            else:
                return self.request_reschedule_clarification_with_ai(current_appt_str, message)
        except Exception as e:
            return "I'd like to help you reschedule. Could you call us at 0610318200 to reschedule?"

    def parse_datetime_with_ai(self, message):
        try:
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            current_time = timezone.now().astimezone(sa_timezone)
            tomorrow_date_str = (current_time + timedelta(days=1)).strftime('%B %d, %Y')
            today_date_str = current_time.strftime('%B %d, %Y')
            day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            next_days = {}
            for i, name in enumerate(day_names):
                days_ahead = (i - current_time.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                next_days[name] = (current_time + timedelta(days=days_ahead)).strftime('%B %d, %Y')

            datetime_extraction_prompt = f"""Extract a complete date and time from the customer's message.
    CURRENT: {current_time.strftime('%Y-%m-%d %H:%M')} (Africa/Johannesburg)
    Working days: Sunday through Friday (Saturday CLOSED)
    Tomorrow: {tomorrow_date_str}
    Next Mon: {next_days['monday']}, Tue: {next_days['tuesday']}, Wed: {next_days['wednesday']}, Thu: {next_days['thursday']}, Fri: {next_days['friday']}, Sun: {next_days['sunday']}
    Return ONLY: YYYY-MM-DDTHH:MM, SATURDAY_CLOSED, PARTIAL_INFO, or NOT_FOUND
    CUSTOMER MESSAGE: "{message}"
    EXTRACTED DATETIME:"""

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Return ONLY: YYYY-MM-DDTHH:MM, SATURDAY_CLOSED, PARTIAL_INFO, or NOT_FOUND."},
                    {"role": "user", "content": datetime_extraction_prompt}
                ],
                temperature=0.1, max_tokens=30
            )
            ai_response = response.choices[0].message.content.strip()
            if ai_response in ("SATURDAY_CLOSED", "PARTIAL_INFO", "NOT_FOUND"):
                return None
            parsed_dt = datetime.strptime(ai_response, '%Y-%m-%dT%H:%M')
            localized_dt = sa_timezone.localize(parsed_dt)
            return localized_dt
        except Exception as e:
            return None

    def handle_unavailable_reschedule_with_ai(self, requested_datetime, original_message):
        try:
            alternatives = self.get_alternative_time_suggestions(requested_datetime)
            if alternatives:
                alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                return f"That time isn't available. Here are some alternatives:\n{alt_text}\n\nWhich works better for you?"
            else:
                return "That time isn't available. Could you suggest another time? Our hours are 8 AM - 6 PM, Monday to Friday."
        except Exception as e:
            return "That time isn't available. Please suggest another time."

    def request_reschedule_clarification_with_ai(self, current_appt_str, message):
        return f"I understand you'd like to reschedule your appointment currently scheduled for {current_appt_str}. When would you prefer to reschedule to? Please provide both the day and time (e.g., 'Monday at 2pm', 'tomorrow at 10am')."

    def process_successful_reschedule(self, old_datetime, new_datetime):
        try:
            self.appointment.scheduled_datetime = new_datetime
            self.appointment.save()
            return f"✅ Appointment rescheduled to {new_datetime.strftime('%A, %B %d at %I:%M %p')}. Our team will contact you before arrival."
        except Exception as e:
            return f"✅ Appointment rescheduled to {new_datetime.strftime('%A, %B %d at %I:%M %p')}."

    def get_availability_error_message(self, error_type, conflict_appointment=None):
        if error_type == "saturday_closed":
            return "We're closed on Saturdays. Please choose Sunday through Friday."
        elif error_type == "outside_business_hours":
            return "We're only available 8 AM to 6 PM. Please choose a time within business hours."
        else:
            return "That time slot isn't available. Please choose a different time."

    def find_next_available_slots(self, preferred_datetime, num_suggestions=4):
        try:
            suggestions = []
            current_check = preferred_datetime
            business_hours = [8, 10, 12, 14, 16]
            days_checked = 0
            while len(suggestions) < num_suggestions and days_checked < 14:
                check_date = current_check.date()
                if check_date.weekday() != 5:
                    for hour in business_hours:
                        check_datetime = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        check_datetime = sa_timezone.localize(check_datetime)
                        if check_datetime > preferred_datetime:
                            is_available, conflict = self.check_appointment_availability(check_datetime)
                            if is_available:
                                suggestions.append({'datetime': check_datetime, 'display': check_datetime.strftime('%A, %B %d at %I:%M %p'), 'day_type': 'weekday'})
                                if len(suggestions) >= num_suggestions:
                                    break
                current_check += timedelta(days=1)
                days_checked += 1
            return suggestions
        except Exception as e:
            return []

    def send_message(self, message_text):
        try:
            clean_phone = clean_phone_number(self.phone_number)
            result = whatsapp_api.send_text_message(clean_phone, message_text)
            return result
        except Exception as e:
            raise

    def is_valid_name(self, name):
        if not name or len(name.strip()) < 2:
            return False
        name_clean = name.strip().lower()
        invalid_words = ['yes', 'no', 'ok', 'sure', 'thanks', 'hello', 'hi', 'good', 'fine']
        if name_clean in invalid_words:
            return False
        if not re.match(r'^[a-zA-Z\s]+$', name):
            return False
        return True

    def extract_appointment_data_with_ai(self, message):
        try:
            next_question = self.get_next_question_to_ask()
            extraction_prompt = f"""Extract appointment info from: "{message}"
    Current question: {next_question}
    Return the extracted value or NOT_FOUND."""
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a precise data extraction assistant."},
                    {"role": "user", "content": extraction_prompt}
                ],
                temperature=0.1, max_tokens=100
            )
            extracted_value = response.choices[0].message.content.strip()
            if extracted_value and extracted_value not in ["NOT_FOUND", "PARTIAL_INFO"]:
                result = self.process_extracted_data(next_question, extracted_value, message)
                if result == "BOOK_APPOINTMENT":
                    return "BOOK_APPOINTMENT"
            return extracted_value
        except Exception as e:
            return self.fallback_manual_extraction(message)

    def process_extracted_data(self, question_type, extracted_value, original_message):
        try:
            if question_type == "service_type" and not self.appointment.project_type:
                if extracted_value in ['bathroom renovation', 'kitchen renovation', 'new plumbing installation']:
                    self.appointment.project_type = extracted_value.replace(' ', '_')
            elif question_type == "plan_or_visit" and self.appointment.has_plan is None:
                if extracted_value == "has_plan":
                    self.appointment.has_plan = True
                elif extracted_value == "needs_visit":
                    self.appointment.has_plan = False
            elif question_type == "area" and not self.appointment.customer_area:
                self.appointment.customer_area = extracted_value
            elif question_type == "timeline" and not self.appointment.timeline:
                self.appointment.timeline = extracted_value
            elif question_type == "property_type" and not self.appointment.property_type:
                if extracted_value in ['house', 'apartment', 'business']:
                    self.appointment.property_type = extracted_value
            elif question_type == "name" and not self.appointment.customer_name:
                if self.is_valid_name(extracted_value):
                    self.appointment.customer_name = extracted_value
            elif question_type == "availability" and not self.appointment.scheduled_datetime:
                if extracted_value not in ["PARTIAL_INFO", "NOT_FOUND"]:
                    try:
                        parsed_dt = datetime.strptime(extracted_value, '%Y-%m-%dT%H:%M')
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        localized_dt = sa_timezone.localize(parsed_dt)
                        self.appointment.scheduled_datetime = localized_dt
                        self.appointment.save()
                        return "BOOK_APPOINTMENT"
                    except ValueError as e:
                        pass
            self.appointment.save()
        except Exception as e:
            print(f"❌ Error processing extracted data: {str(e)}")

    def fallback_manual_extraction(self, message):
        try:
            message_lower = message.lower()
            next_question = self.get_next_question_to_ask()
            retry_count = getattr(self.appointment, 'retry_count', 0)
            be_generous = retry_count > 0
            if next_question == "plan_or_visit" and self.appointment.has_plan is None:
                yes_patterns = ['yes', 'yeah', 'yep', 'yup', 'sure', 'have plan', 'got plan', 'have a plan', 'got a plan', 'already have', 'existing plan', 'i do', 'i have', 'yes i do', 'yes i have', 'i got']
                no_patterns = ['no', 'nope', 'nah', "don't have", "dont have", 'no plan', 'need visit', 'site visit', 'visit first', "don't", "i don't", 'visit please', 'no i', 'i need']
                for pattern in yes_patterns:
                    if pattern in message_lower:
                        self.appointment.has_plan = True
                        self.appointment.save()
                        return "has_plan"
                for pattern in no_patterns:
                    if pattern in message_lower:
                        self.appointment.has_plan = False
                        self.appointment.save()
                        return "needs_visit"
            if next_question == "property_type" and not self.appointment.property_type:
                property_keywords = {'house': ['house', 'home', 'residential'], 'apartment': ['apartment', 'flat', 'unit', 'complex'], 'business': ['business', 'commercial', 'office', 'shop', 'store', 'company']}
                for prop_type, keywords in property_keywords.items():
                    if any(keyword in message_lower for keyword in keywords):
                        self.appointment.property_type = prop_type
                        self.appointment.save()
                        return prop_type
            return "NOT_FOUND"
        except Exception as e:
            return "NOT_FOUND"

    def update_appointment_from_conversation(self, message):
        try:
            next_question = self.get_next_question_to_ask()
            retry_count = getattr(self.appointment, 'retry_count', 0)
            extracted_result = self.extract_appointment_data_with_ai(message)
            if extracted_result and extracted_result not in ["NOT_FOUND", "ERROR"]:
                self.appointment.retry_count = 0
                self.appointment.save()
                return extracted_result
            else:
                self.appointment.retry_count = retry_count + 1
                self.appointment.save()
                return "RETRY_NEEDED"
            if extracted_result == "BOOK_APPOINTMENT":
                return "BOOK_APPOINTMENT"
        except Exception as e:
            return "ERROR"


def send_reminder_message(appointment, reminder_type):
    try:
        customer_name = appointment.customer_name or "there"
        appt_date = appointment.scheduled_datetime.strftime('%A, %B %d, %Y')
        appt_time = appointment.scheduled_datetime.strftime('%I:%M %p')
        if reminder_type == '1_day':
            message = f"""🔧 APPOINTMENT REMINDER\n\nHi {customer_name},\n\nJust a friendly reminder about your plumbing appointment:\n\n📅 Tomorrow: {appt_date}\n🕐 Time: {appt_time}\n📍 Area: {appointment.customer_area or 'Your location'}\n\nOur team will contact you before arrival to confirm timing.\n\nNeed to reschedule? Reply to this message.\n\nSee you tomorrow!\n- Homebase Plumbers"""
        elif reminder_type == 'morning':
            message = f"""🌅 GOOD MORNING REMINDER\n\nHi {customer_name},\n\nToday's your plumbing appointment:\n\n📅 Today: {appt_date}\n🕐 Time: {appt_time}\n📍 Area: {appointment.customer_area or 'Your location'}\n\nOur team will call you 30 minutes before arrival.\n\n- Homebase Plumbers"""
        elif reminder_type == '2_hours':
            message = f"""⏰ APPOINTMENT IN 2 HOURS\n\nHi {customer_name},\n\nYour plumbing appointment is coming up:\n\n🕐 In 2 hours: {appt_time}\n📍 Area: {appointment.customer_area or 'Your location'}\n\nOur team will call you in about 30 minutes.\n\n- Homebase Plumbers"""
        else:
            return False
        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, message)
        return True
    except Exception as e:
        return False


@csrf_exempt
def handle_whatsapp_media(request):
    if request.method == 'POST':
        try:
            sender = request.POST.get('From', '')
            num_media = int(request.POST.get('NumMedia', 0))
            if not sender or num_media == 0:
                return HttpResponse(status=200)
            try:
                appointment = Appointment.objects.get(phone_number=sender)
            except Appointment.DoesNotExist:
                twilio_client.messages.create(body="I don't have an active appointment for this number. Please start by telling me about your plumbing needs.", from_=TWILIO_WHATSAPP_NUMBER, to=sender)
                return HttpResponse(status=200)
            plumbot = Plumbot(sender)
            if (appointment.has_plan is True and appointment.customer_area and appointment.property_type and appointment.plan_status is None):
                appointment.plan_status = 'pending_upload'
                appointment.save()
            if appointment.plan_status != 'pending_upload':
                if appointment.has_plan is True:
                    response_msg = "I'll need you to send your plan once we collect some basic information first. Let me continue with a few questions."
                else:
                    response_msg = "I see you sent a file, but I'm not currently expecting any documents. Let me continue with your appointment details."
                twilio_client.messages.create(body=response_msg, from_=TWILIO_WHATSAPP_NUMBER, to=sender)
                return HttpResponse(status=200)
            uploaded_files = []
            for i in range(num_media):
                media_url = request.POST.get(f'MediaUrl{i}', '')
                media_content_type = request.POST.get(f'MediaContentType{i}', '')
                if media_url:
                    file_info = download_and_save_media(media_url, media_content_type, appointment, i)
                    if file_info:
                        uploaded_files.append(file_info)
            if uploaded_files:
                appointment.plan_uploaded_at = timezone.now()
                appointment.save()
                ack_message = plumbot.handle_plan_upload_flow("file received")
                twilio_client.messages.create(body=ack_message, from_=TWILIO_WHATSAPP_NUMBER, to=sender)
            return HttpResponse(status=200)
        except Exception as e:
            return HttpResponse(status=500)
    return HttpResponse(status=405)


def download_and_save_media(media_url, content_type, appointment, file_index):
    try:
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        response = requests.get(media_url, auth=auth)
        if response.status_code != 200:
            return None
        extension_map = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'application/pdf': '.pdf', 'image/gif': '.gif'}
        extension = extension_map.get(content_type, '.bin')
        timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
        customer_name = appointment.customer_name or 'customer'
        safe_name = ''.join(c for c in customer_name if c.isalnum())
        filename = f"plan_{safe_name}_{appointment.id}_{timestamp}_{file_index}{extension}"
        file_path = f"customer_plans/{filename}"
        file_content = ContentFile(response.content, name=filename)
        saved_path = default_storage.save(file_path, file_content)
        if not getattr(appointment, 'plan_file', None):
            appointment.plan_file = saved_path
            appointment.save()
        return {'name': filename, 'path': saved_path, 'size': len(response.content), 'content_type': content_type}
    except Exception as e:
        return None