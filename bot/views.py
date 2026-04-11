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
import tempfile
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
from django.db import IntegrityError, connection, transaction
from decimal import Decimal, InvalidOperation
from django.templatetags.static import static
import base64

import logging
logger = logging.getLogger(__name__)



#DELETE FROM bot_appointment
#WHERE phone_number = 'whatsapp:+27610318200';


def _to_decimal(value, default='0.00'):
    """Convert API numeric inputs to Decimal safely."""
    if value in (None, ''):
        return Decimal(default)
    try:
        cleaned = str(value).strip()
        cleaned = (
            cleaned
            .replace('US$', '')
            .replace('$', '')
            .replace(',', '')
            .replace(' ', '')
        )
        return Decimal(cleaned)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _to_float(value, default=0.0):
    """Safe float conversion using decimal normalizer."""
    try:
        return float(_to_decimal(value, default=str(default)))
    except Exception:
        return float(default)


def _safe_logo_url():
    """Return static logo URL without crashing when manifest entry is missing."""
    for path in ('images/logo.jpg', 'logo.jpg'):
        try:
            return static(path)
        except ValueError:
            continue
    return '/static/images/logo.jpg'


def _safe_logo_data_uri():
    """Return inline data URI for logo when static serving is unavailable."""
    logo_candidates = [
        os.path.join(settings.BASE_DIR, 'bot', 'static', 'images', 'logo.jpg'),
        os.path.join(settings.BASE_DIR, 'bot', 'static', 'logo.jpg'),
        os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.jpg'),
    ]
    logo_path = next((p for p in logo_candidates if os.path.exists(p)), None)
    if not logo_path:
        return ''
    try:
        with open(logo_path, 'rb') as f:
            encoded = base64.b64encode(f.read()).decode('ascii')
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return ''


def _reset_pk_sequence(model):
    """Reset Postgres PK sequence to current MAX(id) for a model table."""
    if connection.vendor != 'postgresql':
        return False

    table_name = model._meta.db_table
    pk_column = model._meta.pk.column
    quoted_table = connection.ops.quote_name(table_name)
    quoted_pk = connection.ops.quote_name(pk_column)

    sql = (
        f"SELECT setval(pg_get_serial_sequence('{table_name}', '{pk_column}'), "
        f"COALESCE(MAX({quoted_pk}), 1), true) FROM {quoted_table};"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql)
    return True


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
                    next_page = request.GET.get('next', 'dashboard/')
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

# Backward-compatible aliases used across older code paths.
ACCOUNT_SID = TWILIO_ACCOUNT_SID
AUTH_TOKEN = TWILIO_AUTH_TOKEN





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
@require_http_methods(["GET", "POST"])
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
        context['logo_url'] = _safe_logo_url()
        context['logo_data_uri'] = _safe_logo_data_uri()
        
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





            
# Add this separate view for API-based quotation creation
@csrf_exempt
@require_http_methods(["POST"])
def create_quotation_api(request):
    """API endpoint for creating quotations from the quotation generator page"""
    logger.info("🔹 Received request to create a new quotation")

    try:
        data = json.loads(request.body)
        logger.debug(f"📦 Parsed request data: {data}")

        # Get appointment - this is REQUIRED
        appointment_id = data.get('appointment_id')
        if not appointment_id:
            logger.error("❌ No appointment_id provided")
            return JsonResponse({
                'success': False,
                'error': 'appointment_id is required'
            }, status=400)

        logger.debug(f"🔍 Looking up Appointment with ID: {appointment_id}")
        try:
            appointment = Appointment.objects.get(id=appointment_id)
            logger.info(f"✅ Found Appointment: {appointment}")
        except Appointment.DoesNotExist:
            logger.error(f"❌ Appointment with ID {appointment_id} not found")
            return JsonResponse({
                'success': False,
                'error': f'Appointment with ID {appointment_id} not found'
            }, status=404)
        
        # Create the quotation
        logger.debug("🧾 Creating Quotation record...")
        quotation = None
        for attempt in range(2):
            try:
                with transaction.atomic():
                    quotation = Quotation.objects.create(
                        appointment=appointment,  # This is now guaranteed to exist
                        labor_cost=_to_decimal(data.get('labour_cost', 0)),
                        transport_cost=_to_decimal(data.get('transport_cost', 0)),
                        materials_cost=_to_decimal(data.get('materials_cost', 0)),
                        notes=data.get('notes', ''),
                        status='draft'
                    )
                break
            except IntegrityError as e:
                error_text = str(e).lower()
                is_sequence_collision = (
                    "bot_quotation_pkey" in error_text
                    and "key (id)=" in error_text
                )
                if attempt == 0 and is_sequence_collision and _reset_pk_sequence(Quotation):
                    logger.warning("Reset bot_quotation id sequence after PK collision; retrying insert once.")
                    continue
                raise

        if quotation is None:
            raise RuntimeError("Failed to create quotation after retry")
        logger.info(f"✅ Quotation created with ID: {quotation.id}")

        # Create quotation items
        items_created = 0
        items_data = data.get('items', [])
        logger.debug(f"🧩 Creating {len(items_data)} quotation items...")
        for idx, item_data in enumerate(items_data, start=1):
            logger.debug(f"➡️ Processing item {idx}: {item_data}")
            if item_data.get('name'):
                QuotationItem.objects.create(
                    quotation=quotation,
                    description=item_data.get('name', ''),
                    quantity=_to_decimal(item_data.get('qty', 1), default='1.00'),
                    unit_price=_to_decimal(item_data.get('unit', 0))
                )
                items_created += 1
                logger.debug(f"✅ Created item {idx} successfully")
            else:
                logger.warning(f"⚠️ Skipped item {idx} due to missing 'name' field")

        # Recalculate total
        quotation.save()
        logger.info(f"💰 Quotation total recalculated: {quotation.total_amount}")

        response_data = {
            'success': True,
            'message': 'Quotation created successfully',
            'quotation_id': quotation.id,
            'quotation_number': quotation.quotation_number,
            'quotation_name': quotation.get_display_name(),
            'appointment_id': appointment.id,
            'items_created': items_created,
            'total_amount': float(quotation.total_amount)
        }
        logger.debug(f"📤 Response data: {response_data}")

        return JsonResponse(response_data)

    except json.JSONDecodeError:
        logger.error("❌ Failed to decode JSON from request body", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': 'Invalid JSON data'
        }, status=400)

    except Exception as e:
        logger.exception(f"❌ Unexpected error while creating quotation: {e}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)




@method_decorator(staff_required, name='dispatch')
class ViewQuotationView(DetailView):
    model = Quotation
    template_name = 'view_quotation.html'
    context_object_name = 'quotation'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['logo_url'] = _safe_logo_url()
        context['logo_data_uri'] = _safe_logo_data_uri()
        return context


@method_decorator(staff_required, name='dispatch')
class QuotationsListView(ListView):
    model = Quotation
    template_name = 'quotations_list.html'
    context_object_name = 'quotations'
    paginate_by = 25
    ordering = ['-created_at']


@staff_required
@require_http_methods(["GET"])
def quotation_detail_api(request, pk):
    """Return quotation payload used by edit_quotation.html."""
    quotation = get_object_or_404(Quotation, pk=pk)
    appointment = quotation.appointment

    items = [
        {
            'id': item.id,
            'description': item.description,
            'quantity': _to_float(item.quantity),
            'unit_price': _to_float(item.unit_price),
            'total_price': _to_float(item.total_price),
        }
        for item in quotation.items.all().order_by('id')
    ]

    return JsonResponse({
        'id': quotation.id,
        'quotation_number': quotation.quotation_number,
        'quotation_name': quotation.get_display_name(),
        'status': quotation.status,
        'notes': quotation.notes or '',
        'labor_cost': _to_float(quotation.labor_cost),
        'materials_cost': _to_float(quotation.materials_cost),
        'transport_cost': _to_float(quotation.transport_cost),
        'total_amount': _to_float(quotation.total_amount),
        'created_at': quotation.created_at.isoformat() if quotation.created_at else None,
        'updated_at': quotation.updated_at.isoformat() if quotation.updated_at else None,
        'appointment': {
            'id': appointment.id,
            'customer_name': appointment.customer_name or '',
            'customer_email': appointment.customer_email or '',
            'phone_number': appointment.phone_number or '',
            'customer_area': appointment.customer_area or '',
            'project_type': appointment.project_type or '',
            'project_type_display': appointment.get_project_type_display() if hasattr(appointment, 'get_project_type_display') else (appointment.project_type or ''),
            'project_description': appointment.project_description or '',
        },
        'items': items,
    })

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

    def post(self, request, *args, **kwargs):
        # JSON API update path used by edit_quotation.html
        content_type = (request.content_type or '').lower()
        if 'application/json' in content_type:
            quotation = self.get_object()
            try:
                data = json.loads(request.body or '{}')
            except json.JSONDecodeError:
                return JsonResponse({'success': False, 'error': 'Invalid JSON body'}, status=400)

            appointment = quotation.appointment
            appointment.customer_name = data.get('client_name', appointment.customer_name)
            appointment.customer_email = data.get('client_email', appointment.customer_email)
            appointment.phone_number = data.get('client_phone', appointment.phone_number)
            appointment.customer_area = data.get('client_address', appointment.customer_area)
            appointment.project_type = data.get('project_type', appointment.project_type)
            appointment.project_description = data.get('project_notes', appointment.project_description)
            appointment.save()

            quotation.notes = data.get('project_notes', quotation.notes or '')
            quotation.labor_cost = _to_decimal(data.get('labour_cost', quotation.labor_cost))
            quotation.transport_cost = _to_decimal(data.get('transport_cost', quotation.transport_cost))
            quotation.materials_cost = _to_decimal(data.get('materials_cost', quotation.materials_cost))
            quotation.save()

            items_data = data.get('items', [])
            if isinstance(items_data, list):
                quotation.items.all().delete()
                for item in items_data:
                    name = (item or {}).get('name', '')
                    if not name:
                        continue
                    qty = _to_decimal((item or {}).get('qty', 1), default='1.00')
                    unit = _to_decimal((item or {}).get('unit', 0))
                    QuotationItem.objects.create(
                        quotation=quotation,
                        description=name,
                        quantity=qty,
                        unit_price=unit,
                    )
                quotation.save()

            return JsonResponse({
                'success': True,
                'quotation_id': quotation.id,
                'quotation_number': quotation.quotation_number,
                'quotation_name': quotation.get_display_name(),
                'total_amount': float(quotation.total_amount),
                'updated_at': quotation.updated_at.isoformat() if quotation.updated_at else None,
            })

        return super().post(request, *args, **kwargs)


@staff_required
@require_http_methods(["POST"])
def duplicate_quotation(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    new_quote = Quotation.objects.create(
        appointment=quotation.appointment,
        plumber=quotation.plumber,
        labor_cost=quotation.labor_cost,
        materials_cost=quotation.materials_cost,
        transport_cost=quotation.transport_cost,
        notes=quotation.notes,
        status='draft',
    )
    for item in quotation.items.all():
        QuotationItem.objects.create(
            quotation=new_quote,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
        )
    new_quote.save()
    payload = {
        'success': True,
        'new_quotation_id': new_quote.id,
        'quotation_name': new_quote.get_display_name(),
    }
    wants_json = 'application/json' in (request.headers.get('Accept', '').lower() + (request.content_type or '').lower())
    if wants_json:
        return JsonResponse(payload)
    messages.success(request, 'Quotation duplicated successfully.')
    return redirect('edit_quotation', pk=new_quote.id)


@staff_required
@require_http_methods(["POST"])
def delete_quotation(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    appointment_id = quotation.appointment_id
    quotation_name = quotation.get_display_name()
    quotation.delete()
    payload = {
        'success': True,
        'appointment_id': appointment_id,
        'redirect_url': reverse('quotations_list'),
    }
    wants_json = 'application/json' in (request.headers.get('Accept', '').lower() + (request.content_type or '').lower())
    if wants_json:
        return JsonResponse(payload)
    messages.success(request, f'Deleted quotation: {quotation_name}')
    if appointment_id:
        return redirect('appointment_detail', pk=appointment_id)
    return redirect('quotations_list')

@staff_required
def send_quotation(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    temp_doc_path = None
    content_type = (request.content_type or '').lower()
    wants_json = (
        request.method == 'POST'
        and 'application/json' in (request.headers.get('Accept', '').lower() + content_type)
    )
    
    try:
        # Backfill plumber for legacy quotations created without an assignee.
        if quotation.plumber is None and getattr(request.user, 'is_authenticated', False):
            quotation.plumber = request.user

        # Build and send as a PDF document
        quotation_name = quotation.get_display_name()
        safe_name = re.sub(r'[^A-Za-z0-9 _-]+', '', quotation_name).strip().replace(' ', '_')
        safe_name = safe_name[:80] or f"Quotation-{quotation.quotation_number}"
        temp_doc_path = build_quotation_pdf_file(quotation)
        whatsapp_api.send_local_document(
            quotation.appointment.phone_number,
            temp_doc_path,
            caption=quotation_name,
            filename=f"{safe_name}.pdf"
        )
        
        # Update quotation status
        quotation.status = 'sent'
        quotation.sent_via_whatsapp = True
        quotation.sent_at = timezone.now()
        quotation.save()
        
        # Add to conversation history
        ConversationMessage.objects.create(
            appointment=quotation.appointment,
            role='assistant',
            content=f"{quotation_name} sent to customer via WhatsApp",
            timestamp=timezone.now()
        )
        
        messages.success(request, 'Quotation sent successfully via WhatsApp!')
        if wants_json:
            return JsonResponse({
                'success': True,
                'quotation_id': quotation.id,
                'status': quotation.status,
                'sent_at': quotation.sent_at.isoformat() if quotation.sent_at else None,
            })
        
    except Exception as e:
        messages.error(request, f'Failed to send quotation: {str(e)}')
        if wants_json:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    finally:
        if temp_doc_path and os.path.exists(temp_doc_path):
            try:
                os.remove(temp_doc_path)
            except Exception:
                pass
    
    return redirect('appointment_detail', pk=quotation.appointment.pk)

def format_quotation_message(quotation):
    """Format quotation for WhatsApp message"""
    items_text = ""
    for i, item in enumerate(quotation.items.all(), 1):
        items_text += f"{i}. {item.description}\n   Qty: {item.quantity} x US${item.unit_price} = US${item.total_price}\n"
    
    message = f"""🔧 QUOTATION: {quotation.get_display_name()}

Dear {quotation.appointment.customer_name or 'Customer'},

Here is your quotation for plumbing services:

{items_text}
---
Labor: US${quotation.labor_cost}
Materials: US${quotation.materials_cost}
TOTAL: US${quotation.total_amount}

📝 Notes:
{quotation.notes or 'No additional notes'}

This quotation is valid for 30 days. To accept, please reply "ACCEPT" or contact us to discuss.

Thank you for considering our services!
- {(quotation.plumber.get_full_name() if quotation.plumber else '') or (quotation.plumber.username if quotation.plumber else '') or 'Plumbing Team'}"""

    return message


def build_quotation_pdf_file(quotation):
    """Generate quotation PDF using the same visual structure as preview."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        pdf_path = tmp.name

    c = canvas.Canvas(pdf_path, pagesize=A4)
    page_width, page_height = A4
    left = 40
    right = page_width - 40
    y = page_height - 45

    def _new_page():
        c.showPage()
        return page_height - 45

    def _money(value):
        return f"US${value}"

    # Header card
    c.setFillColor(colors.whitesmoke)
    c.roundRect(left - 8, y - 105, right - left + 16, 95, 8, stroke=0, fill=1)

    # Logo with fallbacks
    logo_candidates = [
        os.path.join(settings.BASE_DIR, 'bot', 'static', 'logo.jpg'),
        os.path.join(settings.BASE_DIR, 'bot', 'static', 'images', 'logo.jpg'),
        os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.jpg'),
    ]
    logo_path = next((p for p in logo_candidates if os.path.exists(p)), None)
    if logo_path:
        try:
            c.drawImage(logo_path, left, y - 82, width=70, height=70, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(left + 85, y - 20, "HOMEBASE CONSTRUCTION")
    c.setFont("Helvetica-Oblique", 10)
    c.setFillColor(colors.grey)
    c.drawString(left + 85, y - 38, '"Quality Is Our Qualification"')
    c.setFont("Helvetica", 9)
    c.drawString(left + 85, y - 55, "141 Pritchard St, 2001, Johannesburg")
    c.drawString(left + 85, y - 69, "Cell: +263774819901")
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(colors.black)
    c.drawString(left + 85, y - 86, quotation.get_display_name()[:80])
    y -= 125

    # Client info block
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(colors.black)
    c.drawString(left, y, "Client Information")
    y -= 16
    c.setFont("Helvetica", 10)
    c.drawString(left, y, quotation.appointment.customer_name or "Customer")
    y -= 14
    c.drawString(left, y, quotation.appointment.customer_area or "")
    y -= 24

    # Project details block (preview style)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "Project Details")
    y -= 16
    c.setFont("Helvetica", 10)
    c.drawString(left, y, quotation.appointment.get_project_type_display() if quotation.appointment.project_type else "")
    y -= 14
    c.drawString(left, y, quotation.appointment.customer_area or "")
    y -= 14
    notes_preview = (quotation.notes or "No additional notes").splitlines()
    for line in notes_preview[:3]:
        c.drawString(left, y, line[:100])
        y -= 14
    y -= 8

    # Items table header
    if y < 180:
        y = _new_page()
    table_x = left
    col_item = table_x
    col_qty = table_x + 260
    col_price = table_x + 330
    col_total = table_x + 420

    c.setFillColor(colors.lightgrey)
    c.rect(table_x, y - 18, right - left, 18, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(col_item + 4, y - 12, "Item")
    c.drawString(col_qty + 4, y - 12, "Qty")
    c.drawString(col_price + 4, y - 12, "Price")
    c.drawString(col_total + 4, y - 12, "Total")
    y -= 22

    # Items rows
    c.setFont("Helvetica", 9)
    for item in quotation.items.all():
        if y < 90:
            y = _new_page()
            c.setFillColor(colors.lightgrey)
            c.rect(table_x, y - 18, right - left, 18, stroke=0, fill=1)
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(col_item + 4, y - 12, "Item")
            c.drawString(col_qty + 4, y - 12, "Qty")
            c.drawString(col_price + 4, y - 12, "Price")
            c.drawString(col_total + 4, y - 12, "Total")
            y -= 22
            c.setFont("Helvetica", 9)

        c.setStrokeColor(colors.HexColor("#dddddd"))
        c.line(table_x, y - 2, right, y - 2)
        c.drawString(col_item + 4, y - 14, str(item.description)[:48])
        c.drawRightString(col_qty + 44, y - 14, str(item.quantity))
        c.drawRightString(col_price + 74, y - 14, _money(item.unit_price))
        c.drawRightString(col_total + 94, y - 14, _money(item.total_price))
        y -= 20

    # Totals card
    if y < 130:
        y = _new_page()
    c.setFillColor(colors.whitesmoke)
    c.roundRect(right - 220, y - 88, 220, 88, 8, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    c.drawString(right - 208, y - 18, "Material cost:")
    c.drawRightString(right - 12, y - 18, _money(quotation.materials_cost))
    c.drawString(right - 208, y - 34, "Labour:")
    c.drawRightString(right - 12, y - 34, _money(quotation.labor_cost))
    c.drawString(right - 208, y - 50, "Transport:")
    c.drawRightString(right - 12, y - 50, _money(quotation.transport_cost))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(right - 208, y - 70, "Total Amount:")
    c.drawRightString(right - 12, y - 70, _money(quotation.total_amount))

    c.save()
    return pdf_path


@method_decorator(staff_required, name='dispatch')
class DashboardView(TemplateView):
    template_name = 'dashboard.html'


    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        today = timezone.now().date()
        tomorrow = today + timedelta(days=1)
        now = timezone.now()
        response_age = self.request.GET.get('response_age', '').strip()
        if not response_age:
            response_age = '1w_minus'

        age_map_minus = {
            '1w_minus': timedelta(weeks=1),
            '4w_minus': timedelta(weeks=4),
        }

        # Add stats to context
        appointments = Appointment.objects.all()
        if response_age != 'all' and response_age in age_map_minus:
            cutoff = now - age_map_minus[response_age]
            appointments = appointments.filter(last_customer_response__gte=cutoff)

        context.update({
            'selected_response_age': response_age,
            'total_appointments': appointments.count(),
            'pending_appointments': appointments.filter(status='pending').count(),
            'confirmed_appointments': appointments.filter(status='confirmed').count(),
            'recent_appointments': appointments.order_by('-created_at')[:5],
            'todays_confirmed_appointments': appointments.filter(
                status='confirmed',
                scheduled_datetime__date=today
            ).order_by('scheduled_datetime'),
            'tomorrows_confirmed_appointments': appointments.filter(
                status='confirmed',
                scheduled_datetime__date=tomorrow
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

        response_age = self.request.GET.get('response_age', '').strip()
        if not response_age:
            response_age = '1w_minus'

        age_map_minus = {
            '1w_minus': timedelta(weeks=1),
            '4w_minus': timedelta(weeks=4),
        }

        has_project_type = Case(
            When(Q(project_type__isnull=False) & ~Q(project_type=''), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_property_type = Case(
            When(Q(property_type__isnull=False) & ~Q(property_type=''), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_area = Case(
            When(Q(customer_area__isnull=False) & ~Q(customer_area=''), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_timeline = Case(
            When(Q(timeline__isnull=False) & ~Q(timeline=''), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_site_visit = Case(
            When(scheduled_datetime__isnull=False, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )

        completed_fields = has_project_type + has_property_type + has_area + has_timeline + has_site_visit
        queryset = (
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

        if response_age != 'all' and response_age in age_map_minus:
            cutoff = timezone.now() - age_map_minus[response_age]
            queryset = queryset.filter(last_customer_response__gte=cutoff)

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        response_age = self.request.GET.get('response_age', '').strip()
        if not response_age:
            response_age = '1w_minus'

        age_map_minus = {
            '1w_minus': timedelta(weeks=1),
            '4w_minus': timedelta(weeks=4),
        }

        base_qs = Appointment.objects.all()
        if response_age != 'all' and response_age in age_map_minus:
            cutoff = timezone.now() - age_map_minus[response_age]
            base_qs = base_qs.filter(last_customer_response__gte=cutoff)

        today = timezone.now().date()
        todays_confirmed_appointments = base_qs.filter(
            status='confirmed',
            scheduled_datetime__date=today
        ).order_by('scheduled_datetime')


        context['status_counts'] = {
            'total': base_qs.count(),
            'pending': base_qs.filter(status='pending').count(),
            'confirmed': base_qs.filter(status='confirmed').count(),
            'cancelled': base_qs.filter(status='cancelled').count(),
            'todays_confirmed_appointments': todays_confirmed_appointments,

        }
        context['selected_response_age'] = response_age
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

        has_project_type = Case(
            When(Q(project_type__isnull=False) & ~Q(project_type=''), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_property_type = Case(
            When(Q(property_type__isnull=False) & ~Q(property_type=''), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_area = Case(
            When(Q(customer_area__isnull=False) & ~Q(customer_area=''), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_timeline = Case(
            When(Q(timeline__isnull=False) & ~Q(timeline=''), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_site_visit = Case(
            When(scheduled_datetime__isnull=False, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )

        response_age = self.request.GET.get('response_age', '').strip()
        if not response_age:
            response_age = '1w_minus'
        age_map_plus = {
            '1w': timedelta(weeks=1),
            '2w': timedelta(weeks=2),
            '3w': timedelta(weeks=3),
            '1m': timedelta(days=30),
        }
        age_map_minus = {
            '1w_minus': timedelta(weeks=1),
            '2w_minus': timedelta(weeks=2),
            '3w_minus': timedelta(weeks=3),
            '4w_minus': timedelta(weeks=4),
        }

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

        if response_age == 'all':
            pass
        elif response_age in age_map_minus:
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
            {
                'id': 'sec-vh',
                'title': 'Very Hot Leads',
                'icon': 'fire',
                'css': 'sec-vh',
                'status_bg': '#fee2e2',
                'status_fg': '#991b1b',
                'border': '#dc2626',
                'empty_label': 'No very hot leads.',
                'recommended_action': 'Call now and lock in the site visit time.',
                'count': very_hot_leads.count(),
                'pending_count': very_hot_leads.filter(manual_followup_done=False).count(),
                'done_count': very_hot_leads.filter(manual_followup_done=True).count(),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads(very_hot_leads.filter(manual_followup_done=False))),
                'done_by_date': self._group_leads_by_date(self._enrich_leads(very_hot_leads.filter(manual_followup_done=True))),
            },
            {
                'id': 'sec-hot',
                'title': 'Hot Leads',
                'icon': 'exclamation-triangle',
                'css': 'sec-hot',
                'status_bg': '#fef3c7',
                'status_fg': '#92400e',
                'border': '#f59e0b',
                'empty_label': 'No hot leads.',
                'recommended_action': 'Call within 30 minutes to complete missing details.',
                'count': hot_leads.count(),
                'pending_count': hot_leads.filter(manual_followup_done=False).count(),
                'done_count': hot_leads.filter(manual_followup_done=True).count(),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads(hot_leads.filter(manual_followup_done=False))),
                'done_by_date': self._group_leads_by_date(self._enrich_leads(hot_leads.filter(manual_followup_done=True))),
            },
            {
                'id': 'sec-warm',
                'title': 'Warm Leads',
                'icon': 'sun',
                'css': 'sec-warm',
                'status_bg': '#d1fae5',
                'status_fg': '#065f46',
                'border': '#10b981',
                'empty_label': 'No warm leads.',
                'recommended_action': 'Send a WhatsApp check-in for missing project info.',
                'count': warm_leads.count(),
                'pending_count': warm_leads.filter(manual_followup_done=False).count(),
                'done_count': warm_leads.filter(manual_followup_done=True).count(),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads(warm_leads.filter(manual_followup_done=False))),
                'done_by_date': self._group_leads_by_date(self._enrich_leads(warm_leads.filter(manual_followup_done=True))),
            },
            {
                'id': 'sec-luke',
                'title': 'Luke-warm Leads',
                'icon': 'temperature-low',
                'css': 'sec-luke',
                'status_bg': '#dbeafe',
                'status_fg': '#1e3a8a',
                'border': '#0ea5e9',
                'empty_label': 'No luke-warm leads.',
                'recommended_action': 'Send a quick nudge to re-engage this lead.',
                'count': luke_warm_leads.count(),
                'pending_count': luke_warm_leads.filter(manual_followup_done=False).count(),
                'done_count': luke_warm_leads.filter(manual_followup_done=True).count(),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads(luke_warm_leads.filter(manual_followup_done=False))),
                'done_by_date': self._group_leads_by_date(self._enrich_leads(luke_warm_leads.filter(manual_followup_done=True))),
            },
            {
                'id': 'sec-cold',
                'title': 'Cold Leads',
                'icon': 'snowflake',
                'css': 'sec-cold',
                'status_bg': '#e5e7eb',
                'status_fg': '#374151',
                'border': '#6b7280',
                'empty_label': 'No cold leads.',
                'recommended_action': 'Move to nurture sequence or close as cold lead.',
                'count': cold_leads.count(),
                'pending_count': cold_leads.filter(manual_followup_done=False).count(),
                'done_count': cold_leads.filter(manual_followup_done=True).count(),
                'pending_by_date': self._group_leads_by_date(self._enrich_leads(cold_leads.filter(manual_followup_done=False))),
                'done_by_date': self._group_leads_by_date(self._enrich_leads(cold_leads.filter(manual_followup_done=True))),
            },
        ]

        context.update(
            {
                'very_hot_leads': very_hot_leads,
                'hot_leads': hot_leads,
                'warm_leads': warm_leads,
                'luke_warm_leads': luke_warm_leads,
                'cold_leads': cold_leads,
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
            }
        )
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
        notes_to_prepend.append(
            f"[{timezone.localtime(now).strftime('%Y-%m-%d %H:%M')}] "
            f"{request.user.username}: manual follow-up marked as "
            f"{'done' if appointment.manual_followup_done else 'pending'} from priority dashboard."
        )

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
    #
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_object()
        computed_score, computed_status = calculate_lead_score(appointment)
        conversation_history = appointment.conversation_history
        uploaded_files = appointment.get_all_uploaded_files()   # ← NEW

        context.update({
            'conversation_history': conversation_history,
            'completeness': appointment.get_customer_info_completeness(),
            'documents': uploaded_files,
            'has_documents': appointment.has_uploaded_documents(),
            'document_count': len(uploaded_files),
            'uploaded_images': [f for f in uploaded_files if f['type'] in ('image', 'video')],  # ← NEW
            'computed_lead_score': computed_score,
            'computed_lead_status': computed_status,
            'computed_lead_status_label': dict(Appointment._meta.get_field('lead_status').choices).get(computed_status, 'Cold'),
        })
        return context
    def post(self, request, *args, **kwargs):
        """Handle form submission for updating appointment"""
        appointment = self.get_object()
        
        try:
            # Update fields from POST data
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

            # Handle datetime fields based on appointment type
            if appointment.appointment_type == 'job_appointment':
                job_datetime = request.POST.get('job_scheduled_datetime')
                if job_datetime:
                    # Parse string into datetime object
                    dt = datetime.strptime(job_datetime, "%Y-%m-%d %H:%M")
                    # Make timezone aware
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
        documents = appointment.get_all_uploaded_files()   # ← was get_uploaded_documents()

        context.update({
            'documents': documents,
            'document_count': len(documents),
        })
        return context
        
@staff_required
def download_document(request, pk, document_type):
    """View to download specific documents"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if document_type == 'plan_file' and appointment.plan_file:
        try:
            # Serve the file for download
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
            # Save settings to database or configuration
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
    
    return render(request, 'settings.html', {
        'form': form,
        'active_tab': 'general'
    })

@staff_required
def calendar_settings_view(request):
    if request.method == 'POST':
        form = CalendarSettingsForm(request.POST)
        if form.is_valid():
            # Save calendar settings
            messages.success(request, 'Calendar settings updated successfully')
            return redirect('calendar_settings')
    else:
        initial_data = {
            'google_calendar_credentials': json.dumps(
                getattr(settings, 'GOOGLE_CALENDAR_CREDENTIALS', {}),
                indent=2
            ),
            'calendar_id': getattr(settings, 'GOOGLE_CALENDAR_ID', 'primary'),
        }
        form = CalendarSettingsForm(initial=initial_data)
    
    return render(request, 'settings.html', {
        'form': form,
        'active_tab': 'calendar'
    })

@staff_required
def ai_settings_view(request):
    if request.method == 'POST':
        form = AISettingsForm(request.POST)
        if form.is_valid():
            # Save AI settings
            messages.success(request, 'AI settings updated successfully')
            return redirect('ai_settings')
    else:
        initial_data = {
            'deepseek_api_key': getattr(settings, 'DEEPSEEK_API_KEY', ''),
            'ai_temperature': getattr(settings, 'AI_TEMPERATURE', 0.7),
        }
        form = AISettingsForm(initial=initial_data)
    
    return render(request, 'settings.html', {
        'form': form,
        'active_tab': 'ai'
    })

@staff_required
def update_appointment(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)

    # ✅ Documents (use helper methods)
    has_documents = appointment.has_uploaded_documents()
    document_count = appointment.get_document_count()

    # ✅ Conversation messages (use related_name)
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
        'appointment': appointment,
        'form': form,
        'has_documents': has_documents,
        'document_count': document_count,
        'conversation_history': conversation_history,
    })

@staff_required
def send_followup(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        message = request.POST.get('message', '').strip()
        if message:
            try:
                client = Client(
                    ACCOUNT_SID,
                    AUTH_TOKEN
                )
                response = client.messages.create(
                    body=message,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=appointment.phone_number
                )
                
                # Save to conversation history
                ConversationMessage.objects.create(
                    appointment=appointment,
                    role='assistant',
                    content=message,
                    timestamp=datetime.now()
                )
                
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
    try:
        if appointment.scheduled_datetime:
            plumbot = Plumbot(appointment.phone_number)
            appointment_details = plumbot.extract_appointment_details()
            plumbot.send_confirmation_message(appointment_details, appointment.scheduled_datetime)
    except Exception as exc:
        print(f"Failed to send confirmation message for appointment {appointment.pk}: {exc}")
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
    appointment.save(
        update_fields=[
            'status',
            'follow_up_status',
            'is_lead_active',
            'lead_marked_inactive_at',
            'chatbot_paused',
            'updated_at',
        ]
    )
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
            client = Client(
                settings.TWILIO_ACCOUNT_SID,
                settings.TWILIO_AUTH_TOKEN
            )
            
            test_message = """🧪 TEST NOTIFICATION

This is a test message to verify WhatsApp notifications are working.
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

If you receive this, notifications are working! ✅"""

            team_numbers = getattr(settings, 'TEAM_NUMBERS', [])
            results = {
                'success': True,
                'results': []
            }
            
            for number in team_numbers:
                try:
                    message = client.messages.create(
                        body=test_message,
                        from_=settings.TWILIO_WHATSAPP_NUMBER,
                        to=number
                    )
                    results['results'].append({
                        'number': number,
                        'status': 'success',
                        'sid': message.sid,
                        'error': None
                    })
                except Exception as e:
                    results['results'].append({
                        'number': number,
                        'status': 'failed',
                        'sid': None,
                        'error': str(e)
                    })
            
        except Exception as e:
            results = {
                'success': False,
                'error': str(e)
            }
    
    return render(request, 'test_whatsapp.html', {
        'results': results
    })

@staff_required
def export_appointments(request):
    from django.http import HttpResponse
    import csv
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="plumbing_appointments.csv"'
    
    writer = csv.writer(response)
    writer.writerow([
        'Name', 'Phone', 'Service', 'Property Type', 'Area', 
        'Timeline', 'Status', 'Appointment Date', 'Created At'
    ])
    
    for appointment in Appointment.objects.all().order_by('-created_at'):
        writer.writerow([
            appointment.customer_name or '',
            appointment.phone_number,
#            appointment.get_project_type_display() or '',
            appointment.project_type() or '',
            appointment.customer_area or '',
            appointment.timeline or '',
            appointment.get_status_display(),
            appointment.scheduled_datetime.strftime('%Y-%m-%d %H:%M') if appointment.scheduled_datetime else '',
            appointment.created_at.strftime('%Y-%m-%d %H:%M')
        ])
    
    return response

@staff_required
def complete_site_visit(request, pk):
    """Mark site visit as completed and prepare for job scheduling"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if appointment.appointment_type != 'site_visit':
        messages.error(request, 'This is not a site visit appointment')
        return redirect('appointment_detail', pk=appointment.pk)
    
    if request.method == 'POST':
        site_visit_notes = request.POST.get('site_visit_notes', '')
        plumber_assessment = request.POST.get('plumber_assessment', '')
        
        # Mark site visit as completed
        appointment.mark_site_visit_completed(
            notes=site_visit_notes,
            assessment=plumber_assessment
        )
        
        messages.success(request, 'Site visit marked as completed. You can now schedule the job appointment.')
        return redirect('schedule_job', pk=appointment.pk)
    
    return render(request, 'complete_site_visit.html', {
        'appointment': appointment
    })


@staff_required
def schedule_job(request, pk):
    """Schedule job appointment after site visit"""
    site_visit = get_object_or_404(Appointment, pk=pk)
    
    # Check if this appointment can have a job scheduled
    if site_visit.appointment_type == 'job' or site_visit.status != 'confirmed':
        messages.error(request, 'Cannot schedule job for this appointment')
        return redirect('appointment_detail', pk=site_visit.pk)
    
    if request.method == 'POST':
        try:
            # Get form data
            job_date = request.POST.get('job_date')
            job_time = request.POST.get('job_time')
            duration_hours = int(request.POST.get('duration_hours', 4))
            job_description = request.POST.get('job_description', '')
            materials_needed = request.POST.get('materials_needed', '')
            
            # Validate required fields
            if not job_date or not job_time:
                messages.error(request, 'Please provide both date and time')
                return render(request, 'schedule_job.html', {
                    'site_visit': site_visit,
                })
            
            # Parse datetime
            job_datetime_str = f"{job_date} {job_time}"
            job_datetime = datetime.strptime(job_datetime_str, '%Y-%m-%d %H:%M')
            
            # Localize to South Africa timezone
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            job_datetime = sa_timezone.localize(job_datetime)
            
            # Check if time is in the future
            if job_datetime <= timezone.now():
                messages.error(request, 'Job time must be in the future')
                return render(request, 'schedule_job.html', {
                    'site_visit': site_visit,
                })
            
            # Check business hours (8 AM - 6 PM, Monday-Friday)
            if job_datetime.weekday() == 5:  # Saturday only
                messages.error(request, 'Jobs can only be scheduled Sunday-Friday (closed Saturdays)')
                return render(request, 'schedule_job.html', {
                    'site_visit': site_visit,
                })
            
            if job_datetime.hour < 8 or job_datetime.hour >= 18:
                messages.error(request, 'Jobs must be scheduled between 8 AM and 6 PM')
                return render(request, 'schedule_job.html', {
                    'site_visit': site_visit,
                })
            
            # FIXED: Create job appointment properly
            # Generate unique phone number for job (since phone_number is unique)
            import uuid
            job_phone = f"job_{uuid.uuid4().hex[:8]}_{site_visit.phone_number}"
            
            job_appointment = Appointment.objects.update(
 #               phone_number=site_visit.phone_number,  # Unique identifier for the job
                customer_name=site_visit.customer_name,
                customer_email=site_visit.customer_email or '',
                customer_area=site_visit.customer_area,
                project_type=site_visit.project_type,
                property_type=site_visit.property_type,
                project_description=job_description or site_visit.project_description,
                scheduled_datetime=job_datetime,
                appointment_type='job',  # Mark as job appointment
                status='scheduled',
                has_plan=site_visit.has_plan,
                timeline=f'{duration_hours} hours',
            )
            
            # Store reference to original site visit if you have a field for it
            # job_appointment.related_site_visit_id = site_visit.id
            # job_appointment.save()
            
            # Send notifications
            try:
                send_job_notifications(job_appointment, materials_needed)
            except Exception as notify_error:
                print(f"⚠️ Notification error: {notify_error}")
            
            messages.success(
                request, 
                f'Job scheduled for {job_datetime.strftime("%B %d, %Y at %I:%M %p")}'
            )
            return redirect('appointment_detail', pk=job_appointment.pk)
            
        except ValueError as e:
            messages.error(request, f'Invalid date/time format: {str(e)}')
        except Exception as e:
            messages.error(request, f'Error scheduling job: {str(e)}')
            print(f"❌ Schedule job error: {str(e)}")
    
    return render(request, 'schedule_job.html', {
        'site_visit': site_visit,
    })

@staff_required
def job_appointments_list(request):
    """List all job appointments"""
    job_appointments = Appointment.objects.filter(
        appointment_type='job_appointment'
    ).order_by('-job_scheduled_datetime')
    
    # Filter by status if provided
    status_filter = request.GET.get('status')
    if status_filter:
        job_appointments = job_appointments.filter(job_status=status_filter)
    
    # Filter by plumber if provided
    plumber_filter = request.GET.get('plumber')
    if plumber_filter:
        job_appointments = job_appointments.filter(assigned_plumber_id=plumber_filter)
    
    context = {
        'job_appointments': job_appointments,
        'plumbers': User.objects.filter(groups__name='Plumbers'),
        'status_choices': Appointment.JOB_STATUS_CHOICES,
        'selected_status': status_filter,
        'selected_plumber': plumber_filter,
    }
    
    return render(request, 'job_appointments_list.html', context)

@require_POST
@staff_required
def update_job_status(request, pk):
    """Update job appointment status"""
    job_appointment = get_object_or_404(Appointment, pk=pk)
    
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

def check_job_availability(job_datetime, duration_hours, exclude_appointment_id=None):
    """Check if job time slot is available"""
    try:
        # Calculate job end time
        job_end_time = job_datetime + timedelta(hours=duration_hours)
        
        # Check for overlapping job appointments
        overlapping_jobs = Appointment.objects.filter(
            appointment_type='job_appointment',
            job_status__in=['scheduled', 'in_progress'],
            job_scheduled_datetime__isnull=False,
        )
        
        if exclude_appointment_id:
            overlapping_jobs = overlapping_jobs.exclude(id=exclude_appointment_id)
        
        for job in overlapping_jobs:
            existing_end = job.job_scheduled_datetime + timedelta(hours=job.job_duration_hours)
            
            # Check for overlap
            if (job_datetime < existing_end and job_end_time > job.job_scheduled_datetime):
                return False
        
        # Check business hours (8 AM - 6 PM, Sunday-Friday)
        if job_datetime.weekday() == 5:  # Saturday only
            return False

        if job_datetime.hour < 8 or job_end_time.hour > 18:
            return False
        
        # Check if it's not in the past
        if job_datetime <= timezone.now():
            return False
        
        return True
        
    except Exception as e:
        print(f"Error checking job availability: {str(e)}")
        return False


def send_job_appointment_notifications(job_appointment):
    """Send notifications about new job appointment - UPDATED"""
    try:
        job_date = job_appointment.job_scheduled_datetime.strftime('%A, %B %d, %Y')
        job_time = job_appointment.job_scheduled_datetime.strftime('%I:%M %p')
        duration = job_appointment.job_duration_hours
        
        # Customer notification
        customer_message = f"""🔧 JOB APPOINTMENT SCHEDULED

Hi {job_appointment.customer_name or 'Customer'},

Your plumbing job has been scheduled:

📅 Date: {job_date}
🕐 Time: {job_time}
⏱️ Duration: {duration} hours
📍 Location: {job_appointment.customer_area}
🔨 Work: {job_appointment.job_description or job_appointment.project_type}

Our plumber will contact you before arrival.

{f"Materials needed: {job_appointment.job_materials_needed}" if job_appointment.job_materials_needed else ""}

Questions? Reply to this message.

- Plumbing Team"""
        
        # Send to customer
        clean_phone = clean_phone_number(job_appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, customer_message)
        
        # Team notification
        plumber_name = job_appointment.assigned_plumber.get_full_name() if job_appointment.assigned_plumber else "Unassigned"
        
        team_message = f"""👷 NEW JOB SCHEDULED

Customer: {job_appointment.customer_name}
Phone: {job_appointment.phone_number.replace('whatsapp:', '')}
Date/Time: {job_date} at {job_time}
Duration: {duration} hours
Location: {job_appointment.customer_area}
Assigned to: {plumber_name}

Job Description:
{job_appointment.job_description or job_appointment.project_type}

{f"Materials: {job_appointment.job_materials_needed}" if job_appointment.job_materials_needed else ""}

View details: http://127.0.0.1:8000/appointments/{job_appointment.id}/"""
        
        # Send to team
        TEAM_NUMBERS = ['0774819901']
        for number in TEAM_NUMBERS:
            try:
                whatsapp_api.send_text_message(number, team_message)
            except Exception as e:
                print(f"Failed to send team notification: {str(e)}")
        
    except Exception as e:
        print(f"Error sending job appointment notifications: {str(e)}")

def send_job_status_update_notification(job_appointment, new_status):
    """Send notification when job status changes"""
    try:
        status_messages = {
            'in_progress': f"🔧 Your plumbing job at {job_appointment.customer_area} has started. Our plumber is on-site working on your {job_appointment.project_type}.",
            'completed': f"✅ Your plumbing job at {job_appointment.customer_area} has been completed! Thank you for choosing our services. If you have any questions, please let us know.",
            'cancelled': f"❌ Your scheduled plumbing job for {job_appointment.job_scheduled_datetime.strftime('%B %d, %Y')} has been cancelled. We'll contact you to reschedule.",
        }
        
        if new_status in status_messages:
            twilio_client.messages.create(
                body=status_messages[new_status],
                from_=TWILIO_WHATSAPP_NUMBER,
                to=job_appointment.phone_number
            )
            
    except Exception as e:
        print(f"Error sending status update: {str(e)}")

@staff_required 
def reschedule_job(request, pk):
    """Reschedule a job appointment"""
    job_appointment = get_object_or_404(Appointment, pk=pk)
    
    if job_appointment.appointment_type != 'job_appointment':
        messages.error(request, 'This is not a job appointment')
        return redirect('appointment_detail', pk=job_appointment.pk)
    
    if request.method == 'POST':
        try:
            # Get new datetime
            job_date = request.POST.get('job_date')
            job_time = request.POST.get('job_time')
            
            job_datetime_str = f"{job_date} {job_time}"
            new_datetime = datetime.strptime(job_datetime_str, '%Y-%m-%d %H:%M')
            
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            new_datetime = sa_timezone.localize(new_datetime)
            
            # Check availability (excluding current appointment)
            is_available = check_job_availability(
                new_datetime, 
                job_appointment.job_duration_hours,
                exclude_appointment_id=job_appointment.id
            )
            
            if not is_available:
                messages.error(request, 'Selected time slot is not available')
                return render(request, 'reschedule_job.html', {'job_appointment': job_appointment})
            
            # Update appointment
            old_datetime = job_appointment.job_scheduled_datetime
            job_appointment.job_scheduled_datetime = new_datetime
            job_appointment.save()
            
            # Send notifications
            send_job_reschedule_notification(job_appointment, old_datetime, new_datetime)
            
            messages.success(request, f'Job rescheduled to {new_datetime.strftime("%B %d, %Y at %I:%M %p")}')
            return redirect('appointment_detail', pk=job_appointment.pk)
            
        except Exception as e:
            messages.error(request, f'Error rescheduling job: {str(e)}')
    
    return render(request, 'reschedule_job.html', {
        'job_appointment': job_appointment
    })

def send_job_reschedule_notification(job_appointment, old_datetime, new_datetime):
    """Send notification about job reschedule"""
    try:
        old_date_str = old_datetime.strftime('%A, %B %d at %I:%M %p')
        new_date_str = new_datetime.strftime('%A, %B %d at %I:%M %p')
        
        message = f"""📅 JOB RESCHEDULED

Hi {job_appointment.customer_name},

Your plumbing job has been rescheduled:

❌ Previous: {old_date_str}
✅ New: {new_date_str}

📍 Location: {job_appointment.customer_area}
🔨 Work: {job_appointment.job_description or job_appointment.project_type}

Our plumber will contact you before the new appointment time.

Questions? Reply to this message.

- Plumbing Team"""
        
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=job_appointment.phone_number
        )
        
    except Exception as e:
        print(f"Error sending reschedule notification: {str(e)}")


@staff_required
def job_appointments_list(request):
    """List all job appointments"""
    # Get all job appointments
    job_appointments = Appointment.objects.filter(
        appointment_type='job'
    ).order_by('-scheduled_datetime')
    
    # Calculate statistics
    total_jobs = job_appointments.count()
    scheduled_jobs = job_appointments.filter(status='scheduled').count()
    in_progress_jobs = job_appointments.filter(status='in_progress').count()
    completed_jobs = job_appointments.filter(status='completed').count()
    
    # Filter by status if provided
    status_filter = request.GET.get('status')
    if status_filter:
        job_appointments = job_appointments.filter(status=status_filter)
    
    # Filter by plumber if provided
    plumber_filter = request.GET.get('plumber')
    if plumber_filter:
        job_appointments = job_appointments.filter(assigned_plumber_id=plumber_filter)
    
    # Filter by date if provided
    date_filter = request.GET.get('date')
    if date_filter:
        job_appointments = job_appointments.filter(scheduled_datetime__date=date_filter)
    
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
    
    return render(request, 'job_appointments_list.html', context)


class CalendarView(View):
    template_name = 'calendar.html'  # Update to your actual template path

    def get(self, request):
        return render(request, self.template_name)


def appointment_data(request):
    """
    Return all appointments as JSON data
    Optional filter: ?service=bathroom or kitchen or installation
    """
    service_filter = request.GET.get('service')
    
    appointments = Appointment.objects.all()
    if service_filter and service_filter != "all":
        appointments = appointments.filter(project_type__icontains=service_filter)

    data = []
    for appt in appointments:
        if appt.scheduled_datetime:
            data.append({
                "id": appt.id,
                "customerName": appt.customer_name or "Unknown",
                "phone": appt.phone_number,
                "date": appt.scheduled_datetime.date().isoformat(),
                "time": appt.scheduled_datetime.time().strftime("%H:%M"),
                "service": map_project_type_to_service_key(appt.project_type),
                "serviceLabel": appt.get_project_type_display() if appt.project_type else "No service",
                "area": appt.customer_area or "N/A",
                "status": appt.status,
                "statusLabel": "Booked" if appt.status == "confirmed" else appt.get_status_display(),
                "projectDescription": appt.project_description or "No project description yet",
            })

    return JsonResponse(data, safe=False)


def map_project_type_to_service_key(project_type):
    """Map full project_type to frontend's JS service keys"""
    mapping = {
        "bathroom_renovation": "bathroom",
        "kitchen_renovation": "kitchen",
        "new_plumbing_installation": "installation",
    }
    return mapping.get(project_type, "other")

# ====== UPDATE THE WEBHOOK HANDLER ======
# Update your existing @csrf_exempt webhook function to mark customer responses

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
            
            # Create or get appointment
            appointment, created = Appointment.objects.get_or_create(
                phone_number=sender,
                defaults={'status': 'pending'}
            )
            
            # ✅ NEW: Mark that customer has responded
            appointment.mark_customer_response()
            
            # Check for opt-out requests
            opt_out_keywords = ['stop', 'unsubscribe', 'opt out', 'no more', 'leave me alone']
            if any(keyword in incoming_message.lower() for keyword in opt_out_keywords):
                appointment.mark_as_inactive_lead(reason='customer_opted_out')
                
                opt_out_message = """Understood. I've removed you from our follow-up list.

If you change your mind in the future, just send us a message and we'll be happy to help!

Thanks,
- Homebase Plumbers"""
                
                clean_phone = clean_phone_number(sender)
                whatsapp_api.send_text_message(clean_phone, opt_out_message)
                
                print(f"🚫 Customer {sender} opted out")
                return HttpResponse(status=200)
            
            # Check for "LATER" or "NOT NOW" requests
            delay_keywords = ['later', 'not now', 'busy', 'call me later', 'in a few weeks']
            if any(keyword in incoming_message.lower() for keyword in delay_keywords):
                appointment.followup_stage = 'week_2'  # Fast-forward to 2-week follow-up
                appointment.save()
                
                delay_message = """No problem at all! I understand timing isn't right at the moment.

I'll check back with you in a couple of weeks. 

In the meantime, if you need anything, just message us!

Thanks,
- Homebase Plumbers"""
                
                clean_phone = clean_phone_number(sender)
                whatsapp_api.send_text_message(clean_phone, delay_message)
                
                print(f"⏰ Customer {sender} requested delay")
                return HttpResponse(status=200)
            
            # Normal message processing with Plumbot
            plumbot = Plumbot(sender)
            reply = plumbot.generate_response(incoming_message)
            
            # Send reply
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


# ====== NEW VIEWS FOR FOLLOW-UP MANAGEMENT ======

@staff_required
def followup_dashboard(request):
    """Dashboard showing follow-up statistics and leads"""
    from datetime import timedelta
    
    now = timezone.now()
    response_age = request.GET.get('response_age', '').strip()
    if not response_age:
        response_age = '1w_minus'

    age_map_minus = {
        '1w_minus': timedelta(weeks=1),
        '4w_minus': timedelta(weeks=4),
    }
    cutoff = None
    if response_age != 'all' and response_age in age_map_minus:
        cutoff = now - age_map_minus[response_age]
    
    # Get statistics
    base_active = Appointment.objects.filter(
        is_lead_active=True,
        status='pending'
    )
    if cutoff:
        base_active = base_active.filter(last_customer_response__gte=cutoff)
    total_active_leads = base_active.count()
    
    # Leads by follow-up stage
    stage_counts = {}
    for stage_code, stage_name in Appointment._meta.get_field('followup_stage').choices:
        stage_qs = Appointment.objects.filter(
            is_lead_active=True,
            followup_stage=stage_code
        )
        if cutoff:
            stage_qs = stage_qs.filter(last_customer_response__gte=cutoff)
        count = stage_qs.count()
        if count > 0:
            stage_counts[stage_name] = count
    
    # Leads needing follow-up today
    leads_needing_followup = Appointment.objects.filter(
        is_lead_active=True,
        status='pending'
    ).exclude(
        followup_stage='completed'
    ).exclude(
        followup_stage='responded'
    )
    if cutoff:
        leads_needing_followup = leads_needing_followup.filter(last_customer_response__gte=cutoff)
    
    # Filter to those actually ready for follow-up
    ready_for_followup = [
        lead for lead in leads_needing_followup 
        if lead.should_send_followup_now()
    ]
    
    # Recent responses (based on filter)
    recent_responses = Appointment.objects.filter(
        last_customer_response__isnull=False,
        is_lead_active=True
    )
    if cutoff:
        recent_responses = recent_responses.filter(last_customer_response__gte=cutoff)
    recent_responses = recent_responses.order_by('-last_customer_response')[:10]
    
    # Inactive leads (last 30 days)
    recent_inactive = Appointment.objects.filter(
        is_lead_active=False,
        lead_marked_inactive_at__gte=now - timedelta(days=30)
    ).order_by('-lead_marked_inactive_at')[:10]
    
    context = {
        'total_active_leads': total_active_leads,
        'stage_counts': stage_counts,
        'ready_count': len(ready_for_followup),
        'ready_leads': ready_for_followup[:20],  # First 20
        'recent_responses': recent_responses,
        'recent_inactive': recent_inactive,
        'selected_response_age': response_age,
        'response_age_label': 'All-time' if response_age == 'all' else (
            'Last 30 Days' if response_age == '4w_minus' else 'Last 7 Days'
        ),
    }
    
    return render(request, 'followup_dashboard.html', context)


@staff_required
def mark_lead_inactive(request, pk):
    """Manually mark a lead as inactive"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        reason = request.POST.get('reason', 'manual')
        appointment.mark_as_inactive_lead(reason=reason)
        
        messages.success(request, f'Lead for {appointment.customer_name or appointment.phone_number} marked as inactive')
        return redirect('appointments_list')
    
    return render(request, 'confirm_mark_inactive.html', {
        'appointment': appointment
    })


@staff_required
def reactivate_lead(request, pk):
    """Reactivate an inactive lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        appointment.is_lead_active = True
        appointment.followup_stage = 'none'
        appointment.lead_marked_inactive_at = None
        appointment.save()
        
        messages.success(request, f'Lead reactivated for {appointment.customer_name or appointment.phone_number}')
        return redirect('appointment_detail', pk=appointment.pk)
    
    return render(request, 'confirm_reactivate.html', {
        'appointment': appointment
    })


@staff_required  
def test_followup_message(request, pk):
    """Send a test follow-up message for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        stage = request.POST.get('stage', 'day_1')
        
        # Import the management command to use its message generator
        from django.core.management import call_command
        from io import StringIO
        
        # You can manually craft a test message or use the generator
        from bot.management.commands.send_followups import Command
        cmd = Command()
        message = cmd.generate_followup_message(appointment, stage)
        
        # Send it
        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, message)
        
        messages.success(request, f'Test {stage} follow-up sent to {appointment.phone_number}')
        return redirect('appointment_detail', pk=appointment.pk)
    
    return render(request, 'test_followup.html', {
        'appointment': appointment,
        'stages': ['day_1', 'day_3', 'week_1', 'week_2', 'month_1']
    })


@staff_required
@require_POST
def manual_followup_check(request):
    """Manually trigger follow-up check (for testing/debugging)"""
    from django.core.management import call_command
    from io import StringIO
    
    try:
        out = StringIO()
        call_command('send_followups', stdout=out)
        output = out.getvalue()
        
        messages.success(request, 'Follow-up check completed successfully')
        
        # Show summary
        for line in output.split('\n'):
            if 'Sent:' in line or 'Skipped:' in line or 'Errors:' in line:
                messages.info(request, line.strip())
        
    except Exception as e:
        messages.error(request, f'Error running follow-up check: {str(e)}')
    
    return redirect('followup_dashboard')

# UPDATED send_followup function for bot/views.py
# This handles MANUAL follow-ups from the admin interface

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
    """Pause automatic follow-ups for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    pause_duration = request.POST.get('pause_duration')
    
    if pause_duration == 'permanent':
        # Pause indefinitely
        appointment.manual_followup_paused = True
        appointment.manual_followup_paused_until = None
        pause_msg = "permanently"
    else:
        # Pause for specified hours
        hours = int(pause_duration)
        pause_until = timezone.now() + timedelta(hours=hours)
        appointment.manual_followup_paused = True
        appointment.manual_followup_paused_until = pause_until
        
        # Human-friendly duration
        if hours == 24:
            pause_msg = "for 24 hours"
        elif hours == 48:
            pause_msg = "for 48 hours"
        elif hours == 168:
            pause_msg = "for 1 week"
        elif hours == 720:
            pause_msg = "for 1 month"
        else:
            pause_msg = f"for {hours} hours"
    
    appointment.save()
    
    messages.success(request, f'⏸️ Automatic follow-ups paused {pause_msg}')
    logger.info(f"Auto follow-ups paused {pause_msg} for appointment {pk} by {request.user.username}")
    
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def resume_auto_followup(request, pk):
    """Resume automatic follow-ups for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    appointment.manual_followup_paused = False
    appointment.manual_followup_paused_until = None
    appointment.save()
    
    messages.success(request, '▶️ Automatic follow-ups resumed')
    logger.info(f"Auto follow-ups resumed for appointment {pk} by {request.user.username}")
    
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
            # Personalize message with customer name
            customer_name = appointment.customer_name or "there"
            personalized_message = message.replace('{name}', customer_name)
            
            # Clean phone number for WhatsApp Cloud API
            clean_phone = clean_phone_number(appointment.phone_number)
            
            # Send message using WhatsApp Cloud API
            result = whatsapp_api.send_text_message(clean_phone, personalized_message)
            
            # Save to conversation history with MANUAL tag
            appointment.add_conversation_message('assistant', f"[MANUAL FOLLOW-UP] {personalized_message}")
            
            # Update follow-up tracking - mark as MANUAL follow-up
            appointment.last_followup_sent = timezone.now()
            appointment.last_manual_followup_sent = timezone.now()
            appointment.followup_count = (appointment.followup_count or 0) + 1
            appointment.manual_followup_count = (appointment.manual_followup_count or 0) + 1
            appointment.is_automatic_followup = False
            
            # Reset followup stage to 'responded' since admin is manually engaging
            appointment.followup_stage = 'responded'
            
            # AUTOMATICALLY pause automatic follow-ups for 48 hours when manual message sent
            pause_until = timezone.now() + timedelta(hours=48)
            appointment.manual_followup_paused = True
            appointment.manual_followup_paused_until = pause_until
            
            appointment.save()
            
            messages.success(request, f'✅ Manual follow-up sent to {clean_phone}! Auto follow-ups paused for 48 hours.')
            logger.info(f"✅ MANUAL follow-up sent by {request.user.username} to {clean_phone}")
            
        except Exception as e:
            error_msg = f'Failed to send message: {str(e)}'
            messages.error(request, error_msg)
            logger.error(f"❌ MANUAL follow-up error: {error_msg}")
    
    return redirect('appointment_detail', pk=appointment.pk)


@staff_required
def send_bulk_followup(request):
    """Send manual follow-up to multiple leads at once"""
    if request.method == 'POST':
        lead_ids = request.POST.getlist('lead_ids')
        message_template = request.POST.get('message_template', '').strip()
        pause_duration = int(request.POST.get('pause_duration', 48))
        
        if not lead_ids or not message_template:
            messages.error(request, 'Please select leads and provide a message')
            return redirect('followup_dashboard')
        
        results = {
            'sent': 0,
            'failed': 0,
            'errors': []
        }
        
        for lead_id in lead_ids:
            try:
                appointment = Appointment.objects.get(id=lead_id)
                
                # Personalize message with customer name
                customer_name = appointment.customer_name or "there"
                personalized_message = message_template.replace('{name}', customer_name)
                
                # Send message
                clean_phone = clean_phone_number(appointment.phone_number)
                whatsapp_api.send_text_message(clean_phone, personalized_message)
                
                # Update tracking
                appointment.add_conversation_message('assistant', f"[BULK MANUAL FOLLOW-UP] {personalized_message}")
                appointment.last_followup_sent = timezone.now()
                appointment.last_manual_followup_sent = timezone.now()
                appointment.followup_count = (appointment.followup_count or 0) + 1
                appointment.manual_followup_count = (appointment.manual_followup_count or 0) + 1
                appointment.is_automatic_followup = False
                appointment.followup_stage = 'responded'
                
                # Pause automatic follow-ups
                pause_until = timezone.now() + timedelta(hours=pause_duration)
                appointment.manual_followup_paused = True
                appointment.manual_followup_paused_until = pause_until
                
                appointment.save()
                
                results['sent'] += 1
                
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"Lead {lead_id}: {str(e)}")
                logger.error(f"Bulk follow-up error for lead {lead_id}: {str(e)}")
        
        # Show results
        if results['sent'] > 0:
            messages.success(request, f"✅ Sent {results['sent']} manual follow-ups (auto follow-ups paused)")
        if results['failed'] > 0:
            messages.warning(request, f"⚠️ Failed to send {results['failed']} messages")
        
        return redirect('followup_dashboard')
    
    # GET request - show bulk follow-up form
    active_leads = Appointment.objects.filter(
        is_lead_active=True,
        status='pending'
    ).order_by('-last_customer_response')
    
    return render(request, 'bulk_followup.html', {
        'leads': active_leads
    })


class Plumbot:
    def __init__(self, phone_number):
        self.phone_number = phone_number
        self.appointment, _ = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )

    # ─────────────────────────────────────────────────────────────────────────────
    # NEW HELPER: _time_confirmed
    # ─────────────────────────────────────────────────────────────────────────────
    
    def _time_confirmed(self) -> bool:
        """
        Returns True when a specific time (not just a date) has been stored on
        scheduled_datetime.  We consider the time confirmed if scheduled_datetime
        has a non-midnight hour OR if the flag TIME_CONFIRMED is present in
        internal_notes.
        """
        dt = self.appointment.scheduled_datetime
        if dt is None:
            return False
        sa_tz = pytz.timezone('Africa/Johannesburg')
        local_dt = dt.astimezone(sa_tz) if dt.tzinfo else sa_tz.localize(dt)
        # Only treat a time as confirmed if it is non-midnight in local time.
        if local_dt.hour != 0 or local_dt.minute != 0:
            return True
        # Fallback flag written when we auto-assign a time
        return 'TIME_CONFIRMED' in (self.appointment.internal_notes or '')
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # NEW HELPER: _mark_time_confirmed
    # ─────────────────────────────────────────────────────────────────────────────
    
    def _mark_time_confirmed(self):
        notes = self.appointment.internal_notes or ''
        if 'TIME_CONFIRMED' not in notes:
            self.appointment.internal_notes = (notes + '\n[TIME_CONFIRMED]').strip()
            self.appointment.save(update_fields=['internal_notes'])

    def _customer_name_declined(self) -> bool:
        return 'NAME_DECLINED' in (self.appointment.internal_notes or '')

    def _mark_customer_name_declined(self):
        notes = self.appointment.internal_notes or ''
        if 'NAME_DECLINED' not in notes:
            self.appointment.internal_notes = (notes + '\n[NAME_DECLINED]').strip()
            self.appointment.save(update_fields=['internal_notes'])

    def _clear_customer_name_declined(self):
        notes = self.appointment.internal_notes or ''
        if 'NAME_DECLINED' in notes:
            cleaned = notes.replace('\n[NAME_DECLINED]', '').replace('[NAME_DECLINED]\n', '').replace('[NAME_DECLINED]', '')
            self.appointment.internal_notes = cleaned.strip()
            self.appointment.save(update_fields=['internal_notes'])

    def _get_question_retry_counts(self) -> dict:
        notes = self.appointment.internal_notes or ''
        pattern = r'\[QUESTION_RETRY_COUNTS\](\{.*?\})'
        match = re.search(pattern, notes, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(1))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_question_retry_counts(self, counts: dict):
        notes = self.appointment.internal_notes or ''
        cleaned = re.sub(r'\n?\[QUESTION_RETRY_COUNTS\]\{.*?\}', '', notes, flags=re.DOTALL).strip()
        payload = f"[QUESTION_RETRY_COUNTS]{json.dumps(counts, sort_keys=True)}"
        self.appointment.internal_notes = f"{cleaned}\n{payload}".strip() if cleaned else payload
        self.appointment.save(update_fields=['internal_notes'])

    def _get_question_retry_count(self, question: str) -> int:
        counts = self._get_question_retry_counts()
        try:
            return max(0, int(counts.get(question, 0)))
        except Exception:
            return 0

    def _set_question_retry_count(self, question: str, count: int):
        counts = self._get_question_retry_counts()
        counts[question] = max(0, int(count))
        self._save_question_retry_counts(counts)

    def _sync_retry_count_field(self, question: str):
        if not self._appointment_has_field('retry_count'):
            return
        current = self._get_question_retry_count(question)
        self.appointment.retry_count = current
        self.appointment.save(update_fields=['retry_count'])

    def _build_retry_context_line(self, updated_fields, next_question) -> str:
        updated_fields = updated_fields or []
        if 'area' in updated_fields and self.appointment.customer_area:
            return (
                f"Thanks for providing your area. We've actually done a number of renovations in "
                f"{self.appointment.customer_area} recently."
            )
        if 'project_description' in updated_fields and self.appointment.project_description:
            return "Thanks for the extra detail. That gives us a much clearer picture of the job."
        if 'service_type' in updated_fields and self.appointment.project_type:
            service_name = self.appointment.project_type.replace('_', ' ').title()
            return f"Thanks for clarifying the service. That helps us point you in the right direction for the {service_name}."
        if 'availability' in updated_fields:
            if next_question == 'availability_time':
                return "Thanks, that day is noted. We just need to lock in the best time for you."
            if next_question == 'area':
                return "Thanks, that time works on our side. We just need your area to finish this off."
        return ""

    def _declines_sharing_name(self, message: str) -> bool:
        msg = (message or '').strip().lower()
        if not msg:
            return False
        decline_phrases = {
            'no', 'nope', 'nah', 'prefer not', 'rather not', 'no thanks',
            'not comfortable', 'dont want to', "don't want to",
            'dont want', "don't want", 'not now'
        }
        return any(phrase in msg for phrase in decline_phrases)

    def _parse_time_only_for_selected_date(self, message: str):
        """
        Parse a time-only reply like '2pm' or '14:00' against the appointment's
        already-selected date.
        """
        base_dt = self.appointment.scheduled_datetime
        if not base_dt:
            return None

        msg = (message or '').strip().lower()
        if not msg:
            return None

        selected_date = self._get_selected_local_date()
        if not selected_date:
            return None

        sa_tz = pytz.timezone('Africa/Johannesburg')

        bare_hour_match = re.fullmatch(r'(\d{1,2})', msg)
        if bare_hour_match:
            chosen_hour = int(bare_hour_match.group(1))
            offered_times = self._get_two_available_times_for_date(selected_date)
            matching_slots = []
            for slot in offered_times:
                local_slot = slot.astimezone(sa_tz) if slot.tzinfo else sa_tz.localize(slot)
                if local_slot.strftime('%I').lstrip('0') == str(chosen_hour):
                    matching_slots.append(local_slot)
            if len(matching_slots) == 1:
                return matching_slots[0]

        time_patterns = [
            r'(\d{1,2}):(\d{2})\s*(am|pm)',
            r'(\d{1,2})\s*(am|pm)',
            r'(\d{1,2}):(\d{2})',
        ]

        for pattern in time_patterns:
            match = re.search(pattern, msg)
            if not match:
                continue

            groups = match.groups()
            if len(groups) >= 3 and groups[2]:
                hour = int(groups[0])
                minute = int(groups[1]) if groups[1] and groups[1].isdigit() else 0
                am_pm = groups[2]
                if am_pm == 'pm' and hour != 12:
                    hour += 12
                elif am_pm == 'am' and hour == 12:
                    hour = 0
            elif len(groups) >= 2 and groups[1] in ['am', 'pm']:
                hour = int(groups[0])
                minute = 0
                am_pm = groups[1]
                if am_pm == 'pm' and hour != 12:
                    hour += 12
                elif am_pm == 'am' and hour == 12:
                    hour = 0
            else:
                hour = int(groups[0])
                minute = int(groups[1]) if len(groups) > 1 and groups[1] else 0

            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return sa_tz.localize(
                    datetime.combine(
                        selected_date,
                        datetime.min.time().replace(hour=hour, minute=minute),
                    )
                )

        return None

    def _get_selected_local_date(self):
        """Return the appointment's selected day in Johannesburg local time."""
        dt = self.appointment.scheduled_datetime
        if not dt:
            return None
        sa_tz = pytz.timezone('Africa/Johannesburg')
        local_dt = dt.astimezone(sa_tz) if dt.tzinfo else sa_tz.localize(dt)
        return local_dt.date()
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # NEW HELPER: _get_next_two_available_days
    # ─────────────────────────────────────────────────────────────────────────────
    
    def _get_next_two_available_days(self) -> list:
        """
        Return the next two calendar dates (as datetime.date objects) that:
        - Are not Saturday (our only closed day)
        - Are in the future (from tomorrow onwards)
        """
        import pytz
        from datetime import timedelta
        sa_tz = pytz.timezone('Africa/Johannesburg')
        today = timezone.now().astimezone(sa_tz).date()
        results = []
        check = today + timedelta(days=1)
        while len(results) < 2:
            if check.weekday() != 5:   # 5 = Saturday
                results.append(check)
            check += timedelta(days=1)
        return results
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # NEW HELPER: _get_two_available_times_for_date
    # ─────────────────────────────────────────────────────────────────────────────
    
    def _get_two_available_times_for_date(self, date_obj) -> list:
        """
        Return two available time slots (as datetime objects, timezone-aware)
        for a given date.  Checks against existing confirmed appointments.
        Prefers 9 AM and 2 PM; falls back to next available business-hours slots.
        """
        import pytz
        from datetime import datetime as dt, timedelta
        sa_tz = pytz.timezone('Africa/Johannesburg')
        preferred_hours = [9, 14, 10, 11, 13, 15, 16]
        results = []
        for h in preferred_hours:
            candidate = sa_tz.localize(dt.combine(date_obj, dt.min.time().replace(hour=h)))
            if candidate <= timezone.now():
                continue
            is_avail, _ = self.check_appointment_availability(candidate)
            if is_avail:
                results.append(candidate)
            if len(results) == 2:
                break
        return results
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # NEW HELPER: _describe_project_context  (used in date question)
    # ─────────────────────────────────────────────────────────────────────────────
    
    def _describe_project_context(self) -> str:
        """Build a short, human-readable visit purpose based on project details."""
        project = (self.appointment.project_type or '').lower().replace('_', ' ')
        desc    = (self.appointment.project_description or '').lower()
    
        # Keyword-based specifics
        if 'drain' in desc or 'pipe' in desc:
            return 'have a quick look at the drains/pipes'
        if 'toilet' in desc or 'chimbuzi' in desc:
            return 'have a quick look at the toilet setup'
        if 'shower' in desc or 'bath' in desc or 'tub' in desc:
            return 'have a quick look at the bathroom space'
        if 'kitchen' in project or 'kitchen' in desc:
            return 'have a quick look at the kitchen plumbing'
        if 'geyser' in desc:
            return 'have a quick look at the geyser setup'
        if 'installation' in project:
            return 'have a quick look at the site for the installation'
    
        # Generic fallback
        return 'have a quick look at the space'
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # NEW HELPER: _format_day  (short human-friendly date label)
    # ─────────────────────────────────────────────────────────────────────────────
    
    def _format_day(self, date_obj) -> str:
        """Return e.g. 'Monday the 7th' or 'Tuesday the 8th'."""
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        day_name  = day_names[date_obj.weekday()]
        day_num   = date_obj.day
        suffix    = 'th' if 11 <= day_num <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day_num % 10, 'th')
        return f"{day_name} the {day_num}{suffix}"

    def _get_pricing_followup_prompt(self, language: str = "english") -> str:
        """Return the next booking question in a natural sales tone."""
        next_question = self.get_next_question_to_ask()
        is_shona = language == "shona"

        if next_question == "service_type":
            return "Uri kuda service ipi chaizvo?" if is_shona else "Which service are you looking at exactly?"
        if next_question == "project_description":
            return (
                "Parizvino, ungandiudza zvishoma kuti chii chaizvo chamunoda kuti chiitwe?"
                if is_shona else
                "For now, could you tell me a bit more about what exactly you want done?"
            )
        if next_question == "availability_date":
            days = self._get_next_two_available_days()
            if len(days) >= 2:
                if is_shona:
                    return (
                        f"Kana muchida, tinogona kutanga nefree on-site assessment. "
                        f"{self._format_day(days[0])} kana {self._format_day(days[1])}, nderipi zuva rinokukodzerai?"
                    )
                return (
                    f"If you'd like, we can do a free on-site assessment first. "
                    f"Would {self._format_day(days[0])} or {self._format_day(days[1])} work better for you?"
                )
            return (
                "Kana muchida, tinogona kutanga nefree on-site assessment. Nderipi zuva rinokukodzerai?"
                if is_shona else
                "If you'd like, we can do a free on-site assessment first. Which day would suit you best?"
            )
        if next_question == "availability_time":
            return "Nguva ipi ingakukodzerai ye free on-site assessment?" if is_shona else "What time would suit you best for the free on-site assessment?"
        if next_question == "area":
            return "Muri munzvimbo ipi kuti tironge kuuya zvakanaka?" if is_shona else "What area are you in so we can plan the visit properly?"
        if next_question == "name":
            return "Tingaisa zita ripi pabhooking?" if is_shona else "What name should we put on the booking?"
        return (
            "Kana muchida, ndinogona kukubatsirai kubhuka free on-site assessment kubva pano."
            if is_shona else
            "If you'd like, I can help you book the free on-site assessment from here."
        )

    def _build_pricing_response(
        self,
        *,
        breakdown_lines,
        total_line: str,
        cheapest_line: str,
        visit_committed: bool = False,
        language: str = "english",
    ) -> str:
        """Build consistent pricing replies with a breakdown, rough total, and booking push."""
        breakdown_text = "\n".join(breakdown_lines)
        if language == "shona":
            depends_line = (
                "Final price inoenderana nesetup uye inogona kugadziriswa kana plumber auya oona nzvimbo yacho."
                if visit_committed else
                "Final price inoenderana nesetup uye inogona kutauriranwa kana tauya kuzoona nzvimbo yacho."
            )
        else:
            depends_line = (
                "Final price depends on setup and can still be adjusted once our plumber sees the space."
                if visit_committed else
                "Final price depends on setup and can be negotiated once we get to come out and see the space."
            )
        return (
            f"{breakdown_text}\n\n"
            f"**{total_line}**\n\n"
            f"{cheapest_line} {depends_line}\n\n"
            f"{self._get_pricing_followup_prompt(language)}"
        )
    #
    # ── FIX 3 helpers ────────────────────────────────────────────────────────

    def _is_delay_or_exit_signal(self, message: str) -> bool:
        """
        Return True if the customer is signalling they want to pause / end
        the conversation for now — without opting out permanently.
        """
        msg = message.lower().strip()

        # Short acks — customer is done reading, not asking a question
        short_acks = {
            'ok', 'okay', 'ok.', 'okay.', 'ok thanks', 'ok thank you',
            'thanks', 'thank you', 'thank u', 'thx', 'thnx',
            'noted', 'got it', 'alright', 'cool', 'nice', 'great',
            '👍', '🙏', '✅', '😊', 'ooh ok', 'ooh okay',
            'sharp', 'sharp!','shap','bo','bho',     
            'oh ok', 'oh okay', 'oh ok thanks', 'oh okay thanks',
            'ooh ok', 'ooh okay',
            'ok bye', 'okay bye', 'bye', 'no worries',
            'i see', 'understood', 'i understand',
            'oh ok', 'oh okay', 'oh ok thanks', 'oh okay thanks',
            'ooh ok', 'ooh okay',
            'ok bye', 'okay bye', 'bye', 'no worries',
            'i see', 'understood', 'i understand', # ← ADD THIS
        }
        if msg in short_acks:
            return True

        # Delay phrases
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
        """Return a warm, pressure-free acknowledgment for delay/exit signals."""
        return (
            "No problem at all! 😊 Whenever you're ready, just drop us a message and "
            "we'll pick up right where we left off."
        )

    def _is_delay_or_exit_signal(self, message: str) -> bool:
        """
        Return True ONLY when the customer is signalling they want to pause/end
        AND one of the following is true:
          1. The appointment is already confirmed (booked)
          2. The customer has explicitly said they will reach out later

        For all other cases — including mid-conversation acks like "oh ok", "sharp",
        "shap", "cool", "noted" — return False so the bot continues naturally.

        Uses DeepSeek to classify intent accurately, with a fast pre-filter to avoid
        burning tokens on obvious non-exit messages.
        """
        msg = (message or '').strip()
        if not msg:
            return False

        msg_lower = msg.lower()

        if len(msg_lower.split()) > 6:
            return False

        if '?' in msg:
            return False

        engagement_signals = (
            'how much', 'price', 'cost', 'quote', 'photo', 'pic', 'picture',
            'bathroom', 'shower', 'toilet', 'tub', 'vanity', 'geyser', 'kitchen',
            'marii', 'mutengo', 'chimbuzi', 'shawa', 'bhavhu', 'kicheni',
            'when', 'where', 'what', 'which', 'who', 'can you', 'do you',
        )
        if any(sig in msg_lower for sig in engagement_signals):
            return False

        obvious_acks = {
            'ok', 'okay', 'k', 'kk', 'oky', 'oh ok', 'oh okay', 'ooh ok',
            'ooh okay', 'sharp', 'shap', 'sho', 'cool', 'nice', 'noted',
            'got it', 'alright', 'great', 'good', 'fine', 'sure', 'yes',
            'yep', 'yeah', 'yup', 'no', 'nope', 'nah', 'ok thanks',
            'ok thank you', 'thanks', 'thank you', 'thank u', 'thx', 'thnx',
            'understood', 'i see', 'ah ok', 'ah okay', 'oh ok thanks',
            'oh okay thanks', 'ok cool', 'ok bye', 'okay bye', 'bye',
            'no worries', '👍', '🙏', '✅', '😊', 'bo', 'bho',
            'hongu', 'zvakanaka', 'maita basa', 'ndatenda',
        }
        explicit_delay_phrases = (
            "i'll talk", "i will talk", "talk later", "will contact",
            "contact later", "i'll be in touch", "get back to you",
            "busy now", "busy at the moment", "not right now",
            "will let you know", "will come back", "come back later",
            "in a bit", "later today", "i'll get back", "let me think",
            "need to think", "thinking about it", "i will reach out",
            "will reach out", "i'll reach out", "ndichatumira",
            "mangwana", "ndichauya",
        )

        is_obvious_ack = msg_lower in obvious_acks
        is_explicit_delay = any(phrase in msg_lower for phrase in explicit_delay_phrases)

        if not is_obvious_ack and not is_explicit_delay:
            return False

        appointment_confirmed = self.appointment.status == 'confirmed'
        customer_said_later = self._customer_said_they_will_reach_out()

        if appointment_confirmed or customer_said_later:
            if is_explicit_delay or is_obvious_ack:
                print(
                    f"✅ Exit signal accepted: confirmed={appointment_confirmed}, "
                    f"said_later={customer_said_later}, msg='{msg}'"
                )
                return True

        if is_obvious_ack and not appointment_confirmed and not customer_said_later:
            return self._deepseek_classify_exit_intent(msg)

        return False

    def _customer_said_they_will_reach_out(self) -> bool:
        """
        Scan recent conversation history for messages where the customer
        explicitly said they will contact us later / in due time.
        Checks the last 10 customer messages.
        """
        history = self.appointment.conversation_history or []
        reach_out_phrases = (
            "i'll reach out", "will reach out", "i'll contact",
            "will contact you", "i'll get back", "get back to you",
            "i'll be in touch", "will be in touch", "contact later",
            "i'll call", "will call you", "reach out later",
            "i'll message", "will message", "come back to this",
            "revisit later", "when i'm ready", "when ready",
            "ndichatumira", "ndichauya", "ndichakubata",
            "mangwana ndichauya", "ill reach out",
        )
        customer_messages = [
            m.get('content', '').lower()
            for m in history[-20:]
            if m.get('role') == 'user'
        ][-10:]

        for content in customer_messages:
            if any(phrase in content for phrase in reach_out_phrases):
                return True
        return False

    def _deepseek_classify_exit_intent(self, message: str) -> bool:
        """
        Use DeepSeek to determine whether a short acknowledgement message
        means the customer wants to END/PAUSE the conversation, or whether
        it is a mid-conversation acknowledgement that expects a bot reply.

        Returns True only if DeepSeek is HIGH confidence the customer is done.
        Defaults to False (keep conversation alive) on any error or LOW confidence.
        """
        try:
            next_question = self.get_next_question_to_ask()
            has_project = bool(self.appointment.project_type)
            has_area = bool(self.appointment.customer_area)
            has_datetime = bool(self.appointment.scheduled_datetime)
            status = self.appointment.status

            context_summary = (
                f"Appointment status: {status}\n"
                f"Service type collected: {'yes' if has_project else 'no'}\n"
                f"Area collected: {'yes' if has_area else 'no'}\n"
                f"Appointment datetime set: {'yes' if has_datetime else 'no'}\n"
                f"Next question bot needs to ask: {next_question}"
            )

            history = self.appointment.conversation_history or []
            recent = []
            for m in history[-6:]:
                role = "Customer" if m.get('role') == 'user' else "Bot"
                content = (m.get('content') or '')[:200].strip()
                if content and not content.startswith('['):
                    recent.append(f"{role}: {content}")
            recent_str = "\n".join(recent) if recent else "No recent messages"

            prompt = f"""You are an intent classifier for a WhatsApp chatbot at a Zimbabwean plumbing company.

CONVERSATION STATE:
{context_summary}

RECENT CONVERSATION:
{recent_str}

CUSTOMER'S LATEST MESSAGE: "{message}"

QUESTION: Is the customer's message a signal that they want to END or PAUSE the conversation right now?

Answer YES only if the customer clearly wants to stop — e.g. they said they'll think about it, they're busy, they'll get back later, or they are done asking questions and expect no reply.

Answer NO if the message is a mid-conversation acknowledgement that naturally expects the bot to continue — e.g. they just said "ok" after receiving information and are waiting for the bot to ask the next question, or the conversation is clearly still in progress with unanswered questions remaining.

IMPORTANT: When there are still questions to ask (next_question is not 'complete') and the appointment is not confirmed, default to NO — keep the conversation alive. A bare "ok" or "sharp" from a customer who hasn't booked yet almost always means "I heard you, continue" not "I'm done".

Reply with ONLY a JSON object:
{{"intent": "exit" or "continue", "confidence": "HIGH" or "LOW"}}"""

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "Return ONLY valid JSON. No markdown, no explanation.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=30,
            )

            raw = response.choices[0].message.content.strip()
            raw = raw.replace('```json', '').replace('```', '').strip()
            result = json.loads(raw)

            intent = result.get('intent', 'continue')
            confidence = result.get('confidence', 'LOW')

            print(
                f"🤖 DeepSeek exit intent: '{message}' → "
                f"intent={intent}, confidence={confidence}"
            )

            return intent == 'exit' and confidence == 'HIGH'

        except Exception as exc:
            print(f"⚠️ DeepSeek exit classification failed: {exc} — defaulting to continue")
            return False

    def _get_delay_acknowledgment(self) -> str:
        """
        Return a warm acknowledgment for genuine exit signals.
        Varies based on whether the appointment is booked or they said they'll reach out.
        """
        if self.appointment.status == 'confirmed' and self.appointment.scheduled_datetime:
            import pytz
            sa_tz = pytz.timezone('Africa/Johannesburg')
            dt = self.appointment.scheduled_datetime.astimezone(sa_tz)
            formatted = dt.strftime('%A, %B %d at %I:%M %p')
            return (
                f"Perfect — see you on {formatted}! "
                "Our plumber will call you 30 minutes before arrival. "
                "Feel free to message anytime if you have questions. 😊"
            )

        if self._customer_said_they_will_reach_out():
            return (
                "No problem at all! Whenever you're ready, just drop us a message and "
                "we'll pick up right where we left off. 😊"
            )

        return (
            "No problem at all! Whenever you're ready, just drop us a message and "
            "we'll pick up right where we left off. 😊"
        )

    def _explicitly_requests_price(self, message: str) -> bool:
        """Return True only when the customer clearly asks about pricing."""
        msg = (message or '').strip().lower()
        if not msg:
            return False

        price_markers = (
            'price', 'pricing', 'cost', 'quote', 'quotation', 'how much',
            'how much is', 'how much are', 'charges', 'charge', 'rate', 'rates',
            'mutengo', 'marii', 'mari', 'zvinodhura', 'inodhura', 'bhadhara',
        )
        return any(marker in msg for marker in price_markers)

    def _looks_like_project_description_reply(self, message: str) -> bool:
        """
        Return True when the message looks like a meaningful description of work
        the customer wants done.
        """
        msg = (message or '').strip()
        msg_lower = msg.lower()
        if not msg:
            return False

        generic_non_answers = {
            'hi', 'hello', 'hey', 'ok', 'okay', 'alright', 'cool', 'sharp',
            'thanks', 'thank you', 'noted', 'yes', 'no', 'bathroom',
            'bathroom renovation', 'kitchen renovation', 'new plumbing installation',
        }
        if msg_lower in generic_non_answers:
            return False

        vague_info_requests = (
            'more information', 'more info', 'tell me more', 'can i get more information',
            'may i get more information', 'need more information',
        )
        if any(phrase in msg_lower for phrase in vague_info_requests):
            return False

        detail_markers = (
            'want', 'need', 'change', 'replace', 'install', 'fix', 'repair',
            'move', 'remove', 'redo', 'renovat', 'upgrade', 'fit',
            'chamber', 'shower', 'toilet', 'geyser', 'basin', 'sink',
            'bath', 'bathtub', 'tub', 'pipe', 'drain', 'tile',
        )
        return any(marker in msg_lower for marker in detail_markers) or len(msg.split()) >= 3

    def _is_product_availability_question(self, message: str) -> bool:
        """
        Return True when the customer is asking whether we HAVE or SELL a product,
        or asking for its price — rather than describing work they want done.

        Examples that return True:
          "And vanitys if you have"
          "do you have tubs"
          "if you have shower cubicles"
          "vanitys?"
          "toilets also?"
          "and geysers"

        Examples that return False (genuine project descriptions):
          "I want to replace my toilet and shower"
          "bathroom renovation with new vanity"
          "need to tile and fit new fixtures"
        """
        msg = (message or '').strip().lower()
        if not msg:
            return False

        availability_patterns = (
            'if you have',
            'do you have',
            'do you sell',
            'you have',
            'you sell',
            'do you do',
            'also?',
            'as well?',
            'too?',
            'and also',
        )
        if any(p in msg for p in availability_patterns):
            return True

        product_words = (
            'vanity', 'vanitys', 'vanities',
            'tub', 'tubs', 'bathtub', 'bathtubs',
            'shower', 'showers', 'cubicle', 'cubicles',
            'toilet', 'toilets', 'chamber', 'chambers',
            'geyser', 'geysers',
            'basin', 'basins', 'sink', 'sinks',
        )
        clean = msg.removeprefix('and ').strip().rstrip('?').strip()
        word_count = len(msg.split())

        if word_count <= 5 and any(clean == p or clean.startswith(p) for p in product_words):
            return True

        return False

    def _appointment_has_field(self, field_name: str) -> bool:
        """Return True only if the Appointment model has this concrete field."""
        return any(f.name == field_name for f in self.appointment._meta.concrete_fields)


    def generate_response(self, incoming_message, precomputed_service_inquiry=None):
        """Check service inquiries ONLY when not mid-conversation."""
        try:
            if self._is_delay_or_exit_signal(incoming_message):
                print(f"⏸️ Exit/delay signal accepted — acknowledging and stopping")
                reply = self._get_delay_acknowledgment()
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reply)
                return reply
            # ── EXIT / DELAY SIGNAL — checked FIRST, before anything else ────
            # Catches: "ok thanks", "noted", "oh ok", "no worries", 👍, etc.
            # Only suppress auto-reply when:
            #   (a) no service type collected yet — truly early / pre-conversation, OR
            #   (b) appointment is already confirmed — customer is just acknowledging
            _no_project_yet = not self.appointment.project_type
            _already_confirmed = self.appointment.status == 'confirmed'
            if False and self._is_delay_or_exit_signal(incoming_message) and (
                _no_project_yet or _already_confirmed
            ):
                print(
                    f"⏸️ Exit/delay signal at conversation start or post-confirm "
                    f"— acknowledging and stopping"
                )
                reply = self._get_delay_acknowledgment()
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reply)
                return reply
 
            current_question = self.get_next_question_to_ask()
            #
            any_pricing_sent = (
                getattr(self.appointment, 'pricing_overview_sent', False) or
                bool(getattr(self.appointment, 'sent_pricing_intents', None))
            )
            #
            # Only consider mid-conversation once we've moved past the first question.
            # Having project_type alone (e.g. auto-classified) is not enough —
            # the customer must have also answered the plan question or provided area.
            mid_conversation = (
                any_pricing_sent or
                (
                    self.appointment.project_type is not None and
                    (
                        self.appointment.has_plan is not None or
                        self.appointment.customer_area is not None
                    )
                )
            )
            if not mid_conversation:
                inquiry = precomputed_service_inquiry or self.detect_service_inquiry(incoming_message)
                PRODUCT_INTENTS = {
                    'tub_sales', 'standalone_tub', 'geyser', 'shower_cubicle',
                    'vanity', 'bathtub_installation', 'toilet', 'chamber',
                    'facebook_package', 'location_ask', 'location_visit',
                    'previous_quotation', 'pictures', 'combined_pricing',
                }
                NON_PRICING_AUTO_REPLY_INTENTS = {
                    'location_ask', 'location_visit', 'previous_quotation', 'pictures',
                    'combined_pricing',
                }
                if inquiry.get('intent') != 'none' and (
                    inquiry.get('confidence') == 'HIGH' or
                    inquiry.get('intent') in PRODUCT_INTENTS
                ):
                    intent = inquiry['intent']
                    price_requested = self._explicitly_requests_price(incoming_message)
                    sent = list(getattr(self.appointment, 'sent_pricing_intents', None) or [])
                    if intent not in NON_PRICING_AUTO_REPLY_INTENTS and not price_requested:
                        print(f"Skipping priced service inquiry: {intent} - no explicit price request")
                    elif intent in sent:
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

            # ✅ THIS BLOCK must be at the same indent level as the if above (8 spaces)
            if (self.appointment.has_plan is True and
                    self.appointment.plan_status == 'pending_upload'):
                return self.handle_plan_upload_flow(incoming_message)

            if (self.appointment.has_plan is True and
                    self.appointment.plan_status == 'plan_uploaded'):
                return self.handle_post_upload_messages(incoming_message)

            # Check if user is awaiting plumber contact after plan upload
            if (self.appointment.has_plan is True and 
                self.appointment.plan_status == 'plan_uploaded'):
                return self.handle_post_upload_messages(incoming_message)

            # STEP 1: Check if this is an alternative time selection
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
                print(f"⏸️ Exit/delay signal accepted mid-conversation — acknowledging and stopping")
                reply = self._get_delay_acknowledgment()
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reply)
                return reply

            if False and self._is_delay_or_exit_signal(incoming_message):
                print(f"⏸️ FIX 3: Delay/exit signal detected — not pushing further")
                reply = self._get_delay_acknowledgment()
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reply)
                return reply

                # ── CONFIRMED + COMPLETE — no more questions ──────────────────────────────
            if (self.appointment.status == 'confirmed' and
                    self.get_next_question_to_ask() == 'complete'):
                # Appointment is fully booked and name collected (or declined).
                # Silently acknowledge any further messages and stop.
                if self._is_delay_or_exit_signal(incoming_message):
                    reply = self._get_delay_acknowledgment()
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply
                # Any other message (e.g. "Complete renovation") → silent, no reply
                self.appointment.add_conversation_message("user", incoming_message)
                return None

            # STEP 2: Extract ALL available information from the message
            extracted_data = self.extract_all_available_info_with_ai(incoming_message)
            
            # ✅ NEW: Check for "I'll send it later" responses BEFORE updating
            if self.handle_plan_later_response(incoming_message):
                # Customer will send plan later - acknowledge and continue
                next_question = self.get_next_question_to_ask()
                
                if next_question != "complete":
                    acknowledgment = "Perfect! You can send your plan whenever you're ready. "
                    
                    # Generate next question
                    reply = self.generate_contextual_response(
                        incoming_message, 
                        next_question, 
                        ['plan_status']  # Indicate that plan status was handled
                    )
                    
                    # Prepend acknowledgment
                    reply = acknowledgment + reply
                    
                    # Update conversation history
                    #self.appointment.add_conversation_message("user", incoming_message)
                    #self.appointment.add_conversation_message("assistant", reply)
                    
                    return reply
            
            # STEP 3: Update appointment with extracted data
            updated_fields = self.update_appointment_with_extracted_data(
                extracted_data,
                incoming_message=incoming_message,
            )

            if (
                'customer_name' in updated_fields and
                self.appointment.status == 'confirmed' and
                self.appointment.scheduled_datetime
            ):
                reply = self._build_named_booking_confirmation()
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reply)
                return reply
            
            # STEP 4: Check for reschedule requests (for confirmed appointments)
            if (self.appointment.status == 'confirmed' and 
                self.appointment.scheduled_datetime and 
                self.detect_reschedule_request_with_ai(incoming_message)):
                
                print("🤖 AI detected reschedule request, handling...")
                reschedule_response = self.handle_reschedule_request_with_ai(incoming_message)
                
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reschedule_response)
                
                return reschedule_response
            
            # ── STEP 5 & 6: Book if ready, otherwise ask next question ───────
# ── STEP 5 & 6: Book if ready, otherwise ask next question ───────
            next_question  = self.get_next_question_to_ask()
            booking_status = self.smart_booking_check()

            if booking_status['ready_to_book'] and self.appointment.status != 'confirmed':

                booking_result = self.book_appointment(incoming_message)

                #
                if booking_result['success']:
                        # send_confirmation_message already fired inside book_appointment().
                        # The bot reply to the customer is ONLY the name question — one message total.
                        reply = (
                            "One last thing — what name should we put on the booking? "
                            "If you'd rather not share it, just say no."
                        )                
                else:
                    error        = booking_result.get('error', '')
                    alternatives = booking_result.get('alternatives', [])
                    if 'saturday' in error.lower() or not alternatives:
                        alt_text = (
                            "\n".join([f"• {alt['display']}" for alt in alternatives])
                            if alternatives else ""
                        )
                        reply = (
                            "We unfortunately don't operate on Saturdays. 😊\n\n"
                            "Our working hours are Sunday to Friday, "
                            "8:00 AM – 6:00 PM.\n\n"
                        )
                        if alt_text:
                            reply += (
                                f"Here are some available slots:\n{alt_text}\n\n"
                                "Or feel free to suggest a different date and time!"
                            )
                        else:
                            reply += "Could you suggest a different day and time?"
                    else:
                        alt_text = "\n".join(
                            [f"• {alt['display']}" for alt in alternatives]
                        )
                        reply = (
                            f"That slot just got taken — here are the next "
                            f"available times:\n{alt_text}\n\n"
                            f"Which works better for you?"
                        )
            else:
                reply = self.generate_contextual_response(
                    incoming_message, next_question, updated_fields
                )

            self.appointment.add_conversation_message("user", incoming_message)
            self.appointment.add_conversation_message("assistant", reply)
            
            return reply

        except Exception as e:
            print(f"❌ API Error: {str(e)}")
            return "I'm having some trouble connecting to our system. Could you try again in a moment?"



    def generate_contextual_response(self, incoming_message, next_question, updated_fields):
        """
        Generate the next bot message.

        retry_count == 0  → exact hardcoded first-pass question, no DeepSeek call.
        retry_count >= 1  → DeepSeek rephrases to match the customer's tone.
                            If the customer provided info this turn, open with a
                            thank-you + one contextual line before the question.
        """
        try:
            import pytz as _pytz

            retry_count = self._get_question_retry_count(next_question)
            sa_tz = _pytz.timezone('Africa/Johannesburg')

            saturday_indicators = ['saturday', 'sat']
            if any(s in incoming_message.lower() for s in saturday_indicators):
                alternatives = self.get_alternative_time_suggestions(
                    timezone.now() + timedelta(days=1)
                )
                alt_text = (
                    "\n".join([f"• {alt['display']}" for alt in alternatives])
                    if alternatives else ""
                )
                reply = (
                    "We unfortunately don't operate on Saturdays. 😊\n\n"
                    "Our working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                )
                if alt_text:
                    reply += (
                        f"Here are some available slots:\n{alt_text}\n\n"
                        "Or feel free to suggest a different date and time!"
                    )
                else:
                    reply += "Could you please choose a different day that works for you?"
                return reply

            all_day_phrases = [
                'available all day', 'whole day', 'all day', 'anytime',
                'any time', 'free all day', 'i am free', 'im free',
            ]
            if (
                next_question in ('availability_time', 'area', 'complete') and
                self.appointment.scheduled_datetime and
                any(p in incoming_message.lower() for p in all_day_phrases)
            ):
                return self._handle_all_day_response()

            if next_question == "name":
                if self.appointment.customer_name and 'customer_name' in (updated_fields or []):
                    return self._build_named_booking_confirmation()
                if self._declines_sharing_name(incoming_message):
                    self._mark_customer_name_declined()
                    return (
                        "No problem at all. Your appointment is still confirmed — "
                        "we'll use this WhatsApp number for updates."
                    )
                return (
                    "One last thing — what name should we put on the booking? "
                    "If you'd rather not share it, just say no."
                )

            if retry_count == 0:
                first_pass = self._get_first_pass_question(next_question)
                if first_pass:
                    self._set_question_retry_count(next_question, 1)
                    return first_pass

            new_retry = retry_count + 1
            self._set_question_retry_count(next_question, new_retry)

            return self._generate_retry_response(
                incoming_message=incoming_message,
                next_question=next_question,
                updated_fields=updated_fields or [],
                retry_count=new_retry,
            )

        except Exception as e:
            print(f"❌ Error generating contextual response: {str(e)}")
            return "I understand. Let me ask you about the next detail we need for your appointment."

    def _get_first_pass_question(self, next_question: str) -> str:
        """
        Return the exact hardcoded first-pass question for a given question key.
        Returns None if the question key is unrecognised.
        These are sent verbatim on retry_count == 0 with no DeepSeek call.
        """
        if next_question == "service_type":
            return (
                "Hello! Happy to help. Which service are you interested in?\n\n"
                "We offer:\n"
                "• Bathroom Renovation\n"
                "• New Plumbing Installation\n"
                "• Kitchen Renovation"
            )

        if next_question == "project_description":
            return (
                "Got it! What exactly do you want done? "
                "The more detail you give, the more accurate we can be with the quote."
            )

        if next_question == "availability_date":
            days = self._get_next_two_available_days()
            day_a = self._format_day(days[0]) if len(days) > 0 else "tomorrow"
            day_b = self._format_day(days[1]) if len(days) > 1 else "the day after"
            visit_desc = self._describe_project_context()
            return (
                f"Great, what works better for you — {day_a} or {day_b} — "
                f"for us to come through and {visit_desc}?"
            )

        if next_question == "availability_time":
            dt = self.appointment.scheduled_datetime
            if dt:
                selected_date = self._get_selected_local_date()
                day_label = self._format_day(selected_date) if selected_date else "that day"
                times = self._get_two_available_times_for_date(selected_date) if selected_date else []
                time_a = times[0].strftime('%I%p').lstrip('0') if len(times) > 0 else "9AM"
                time_b = times[1].strftime('%I%p').lstrip('0') if len(times) > 1 else "2PM"
                return (
                    f"Perfect, for {day_label} — "
                    f"what works better: {time_a} or {time_b}?"
                )
            return "What time works best for you — morning or afternoon?"

        if next_question == "area":
            return "All good, what area are you in?"

        return None

    def _generate_retry_response(
        self,
        incoming_message: str,
        next_question: str,
        updated_fields: list,
        retry_count: int,
    ) -> str:
        """
        Generate a retry response that:
        1. Opens with a thank-you + contextual line if the customer provided info.
        2. Rephrases the next question to match the customer's tone and wording style.
        3. Escalates naturally with each retry (simpler → choices → light urgency).

        Always uses DeepSeek. Falls back to a hardcoded rephrase on error.
        """
        info_provided = self._describe_info_provided(updated_fields)
        contextual_line = self._get_contextual_line(updated_fields, next_question)
        question_instruction = self._get_question_instruction(next_question, retry_count)

        msg_lower = incoming_message.lower()
        shona_markers = [
            'hongu', 'kwete', 'ndinoda', 'ndoda', 'chimbuzi', 'shawa',
            'bhavhu', 'kicheni', 'mauya', 'mangwana', 'mauro', 'zvakanaka',
        ]
        shona_count = sum(1 for m in shona_markers if m in msg_lower)
        language_note = (
            "The customer is writing in Shona — respond in Shona."
            if shona_count >= 2
            else "The customer is writing in mixed Shona/English — match their mix."
            if shona_count == 1 and len(msg_lower.split()) > 2
            else "The customer is writing in English — respond in English."
        )

        if info_provided and contextual_line:
            opening_instruction = (
                f"Open with a brief thank-you for the information they provided "
                f"({info_provided}), then add this specific contextual line: "
                f"\"{contextual_line}\". Then ask the question below. "
                f"Keep the whole message under 4 sentences."
            )
        elif info_provided:
            opening_instruction = (
                f"Open with a brief, natural thank-you for the information they "
                f"provided ({info_provided}). Then ask the question below. "
                f"Keep it under 3 sentences."
            )
        else:
            opening_instruction = (
                "Go straight to the question — no preamble. "
                "The customer hasn't provided new information this turn."
            )

        if retry_count == 1:
            escalation = "Simplify the question slightly. Same intent, fresher phrasing."
        elif retry_count == 2:
            escalation = (
                "Offer two explicit choices instead of an open question. "
                "Make it very easy to answer."
            )
        elif retry_count >= 3:
            escalation = (
                "Keep it to 1-2 sentences max. Add light urgency: "
                "\"We're booking up this week.\" or similar real constraint."
            )
        else:
            escalation = "Natural rephrasing."

        prompt = f"""You are writing a WhatsApp message for Homebase Plumbers in Zimbabwe/South Africa.

CUSTOMER'S LAST MESSAGE: "{incoming_message}"

WHAT TO DO:
{opening_instruction}

QUESTION TO ASK:
{question_instruction}

TONE RULES:
- Mirror the customer's vocabulary and sentence length exactly
- If they wrote 3 words, your question should be short too
- If they wrote in full sentences, match that
- South African / Zimbabwean English ("sorted", "keen", "sharp")
- {language_note}
- No markdown, no bold, no bullet points in the question itself
- One question only — never stack two questions
- At most one emoji for retry 1-2, zero emoji for retry 3+
- Never say "just checking in", "following up", "hope you're well"
- Never use the customer's name (we may not know it)
- Sound like a real person texting, not a bot

RETRY COUNT: {retry_count} (higher = simpler and more direct)
{escalation}

Write ONLY the message text. No labels, no quotes around it."""

        try:
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You write short WhatsApp messages for a plumbing company. "
                            "Match the customer's tone exactly. "
                            "Sound human. Never ask for the customer's name."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=200,
            )
            reply = response.choices[0].message.content.strip()
            reply = reply.replace('**', '').replace('__', '')
            print(
                f"🤖 Retry response | q={next_question} retry={retry_count} "
                f"updated={updated_fields}"
            )
            return reply

        except Exception as e:
            print(f"❌ DeepSeek retry response error: {e}")
            return self._hardcoded_retry_fallback(next_question, retry_count)

    def _describe_info_provided(self, updated_fields: list) -> str:
        """
        Return a human-readable summary of what the customer just provided,
        for use in the thank-you opening.
        """
        if not updated_fields:
            return ""

        field_labels = {
            'service_type': 'the type of service they need',
            'project_description': 'details about their project',
            'area': 'their area',
            'availability': 'their preferred time',
            'customer_name': 'their name',
            'property_type': 'their property type',
            'timeline': 'their timeline',
        }
        labels = [field_labels.get(f, f.replace('_', ' ')) for f in updated_fields]
        if len(labels) == 1:
            return labels[0]
        return ', '.join(labels[:-1]) + ' and ' + labels[-1]

    def _get_contextual_line(self, updated_fields: list, next_question: str) -> str:
        """
        Return a specific, relevant contextual line to add after the thank-you,
        before the next question. These lines make the bot feel human and informed
        rather than robotic.
        """
        if not updated_fields:
            return ""

        area = self.appointment.customer_area or ""
        service = (self.appointment.project_type or "").replace("_", " ").lower()
        desc = (self.appointment.project_description or "").lower()

        if 'area' in updated_fields and area:
            return (
                f"We've actually done a number of renovations in {area} "
                f"over the past month alone."
            )

        if 'service_type' in updated_fields:
            if 'bathroom' in service:
                return "Bathroom renovations are actually our most popular service right now."
            if 'kitchen' in service:
                return "Kitchen plumbing is one of our specialities — great choice."
            if 'installation' in service:
                return "New installations are something we handle from scratch — no problem at all."
            return "That's actually one of the services we do most frequently."

        if 'project_description' in updated_fields:
            if any(w in desc for w in ('tiled', 'already tiled', 'existing')):
                return (
                    "Since it's already tiled, the work focuses on fixtures and fittings "
                    "which keeps costs down."
                )
            if any(w in desc for w in ('new', 'from scratch', 'building')):
                return "Starting fresh gives us more flexibility with the layout — good to know."
            return "That gives us a much clearer picture of the job."

        if 'availability' in updated_fields and next_question == 'availability_time':
            return "That day works well on our side."

        if 'availability' in updated_fields and next_question == 'area':
            return "That time is noted — almost there."

        return ""

    def _get_question_instruction(self, next_question: str, retry_count: int) -> str:
        """
        Return the instruction for DeepSeek describing what question to ask next.
        Provides context-specific phrasing guidance per question.
        """
        if next_question == "service_type":
            return (
                "Ask which of our three services they need: "
                "Bathroom Renovation, New Plumbing Installation, or Kitchen Renovation. "
                "Don't list them as bullet points — weave them into a natural question."
            )

        if next_question == "project_description":
            return (
                "Ask what specifically they want done. Encourage detail by mentioning "
                "that more detail = more accurate quote. Keep it conversational."
            )

        if next_question == "availability_date":
            days = self._get_next_two_available_days()
            day_a = self._format_day(days[0]) if len(days) > 0 else "tomorrow"
            day_b = self._format_day(days[1]) if len(days) > 1 else "the day after"
            visit_desc = self._describe_project_context()
            return (
                f"Ask whether {day_a} or {day_b} works better for a free on-site visit "
                f"to {visit_desc}. Frame it as offering two specific options."
            )

        if next_question == "availability_time":
            selected_date = self._get_selected_local_date()
            day_label = self._format_day(selected_date) if selected_date else "that day"
            times = self._get_two_available_times_for_date(selected_date) if selected_date else []
            time_a = times[0].strftime('%I%p').lstrip('0') if len(times) > 0 else "9AM"
            time_b = times[1].strftime('%I%p').lstrip('0') if len(times) > 1 else "2PM"
            return (
                f"Ask whether {time_a} or {time_b} works better on {day_label}. "
                "Two options only — make it easy to reply."
            )

        if next_question == "area":
            return (
                "Ask which suburb or area they are in. Keep it short — "
                "just need the location to plan the visit."
            )

        if next_question == "name":
            return (
                "Ask what name to put on the booking. "
                "Mention they can decline if they prefer not to share."
            )

        return "Ask the most natural next question to move the booking forward."

    def _hardcoded_retry_fallback(self, next_question: str, retry_count: int) -> str:
        """
        Fallback retry questions used when DeepSeek is unavailable.
        Progressively simpler with each retry.
        """
        fallbacks = {
            'service_type': [
                "Which service were you after — bathroom, kitchen, or a new installation?",
                "Bathroom, kitchen, or new installation — which one?",
                "Just to confirm — which service do you need?",
            ],
            'project_description': [
                "Could you tell me a bit more about what you'd like done?",
                "What exactly needs doing — the more detail the better for the quote.",
                "What's the main thing you want sorted?",
            ],
            'availability_date': [
                "Which day works better for the site visit?",
                "Would tomorrow or the day after suit you better?",
                "What day works for you?",
            ],
            'availability_time': [
                "Morning or afternoon — which works better for you?",
                "What time suits you best?",
                "Morning or afternoon?",
            ],
            'area': [
                "Which area are you based in?",
                "What suburb are you in?",
                "Which area?",
            ],
        }
        options = fallbacks.get(next_question, ["What's the best next step for you?"])
        idx = min(retry_count - 1, len(options) - 1)
        return options[idx]

    def validate_plan_status_with_ai(self, extracted_status: str, original_message: str) -> tuple:
        """
        Use AI to validate and normalize plan status responses
        Handles spelling mistakes, context, and ambiguous answers
        
        Args:
            extracted_status: The raw AI extraction ('yes', 'no', 'has_plan', etc.)
            original_message: The customer's original message
            
        Returns:
            tuple: (is_valid: bool, normalized_value: bool or None, confidence: str)
        """
        try:
            validation_prompt = f"""You are a plan status validation assistant for an appointment booking system.

    CONTEXT:
    We asked the customer: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"

    CUSTOMER'S RESPONSE: "{original_message}"
system_prompt
    AI EXTRACTED VALUE: "{extracted_status}"

    TASK:
    Analyze the customer's response and determine:
    1. Did they answer the plan question?
    2. Do they HAVE a plan or do they NEED a site visit?
    3. How confident are you in this interpretation?

    ANALYSIS RULES:
    - Look at the MEANING, not just keywords
    - Handle spelling mistakes (e.g., "vist" = "visit", "pln" = "plan")
    - Handle context clues (e.g., "I'll send it" implies they have a plan)
    - Handle ambiguity (e.g., "maybe" or "not sure")
    - Ignore unrelated content (e.g., greetings, other questions)

    EXAMPLES:

    Customer: "A site visit would be ideal"
    Analysis: NEEDS_VISIT (customer wants site visit, doesn't have plan)
    Confidence: HIGH

    Customer: "yes i have one"
    Analysis: HAS_PLAN (customer confirms they have a plan)
    Confidence: HIGH

    Customer: "I'll send the blueprints later"
    Analysis: HAS_PLAN (implies they have plans to send)
    Confidence: HIGH

    Customer: "No, come see it first"
    Analysis: NEEDS_VISIT (customer wants visit first, no plan)
    Confidence: HIGH

    Customer: "I think so, let me check"
    Analysis: UNCLEAR (customer is uncertain)
    Confidence: LOW

    Customer: "How much will it cost?"
    Analysis: OFF_TOPIC (not answering the plan question)
    Confidence: N/A

    Customer: "yea I got da plan"
    Analysis: HAS_PLAN (spelling mistakes but clear intent)
    Confidence: HIGH

    Customer: "site vist would be better"
    Analysis: NEEDS_VISIT (spelling mistake but clear: site visit)
    Confidence: HIGH

    Customer: "No plan, need someone to come lok at it"
    Analysis: NEEDS_VISIT (no plan + wants someone to look = site visit)
    Confidence: HIGH

    RESPONSE FORMAT (CRITICAL - FOLLOW EXACTLY):
    Return ONLY a JSON object with this exact structure:
    {{
        "answer_provided": true/false,
        "interpretation": "HAS_PLAN" or "NEEDS_VISIT" or "UNCLEAR" or "OFF_TOPIC",
        "confidence": "HIGH" or "MEDIUM" or "LOW",
        "reasoning": "Brief explanation of your analysis"
    }}

    Do NOT include any other text, markdown, or explanations outside the JSON.

    CUSTOMER MESSAGE: "{original_message}"

    YOUR ANALYSIS:"""

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system", 
                        "content": "You are a precise validation assistant. Return ONLY valid JSON with no additional text or formatting."
                    },
                    {
                        "role": "user", 
                        "content": validation_prompt
                    }
                ],
                temperature=0.2,  # Low temperature for consistency
                max_tokens=150
            )
            
            ai_response = response.choices[0].message.content.strip()
            
            # Clean up response (remove markdown if present)
            ai_response = ai_response.replace('```json', '').replace('```', '').strip()
            
            # Parse JSON response
            try:
                validation_result = json.loads(ai_response)
            except json.JSONDecodeError as e:
                print(f"❌ AI returned invalid JSON: {ai_response}")
                print(f"JSON Error: {str(e)}")
                return (False, None, "ERROR")
            
            # Extract results
            answer_provided = validation_result.get('answer_provided', False)
            interpretation = validation_result.get('interpretation', 'UNCLEAR')
            confidence = validation_result.get('confidence', 'LOW')
            reasoning = validation_result.get('reasoning', '')
            
            print(f"🤖 AI Validation Result:")
            print(f"   Answer provided: {answer_provided}")
            print(f"   Interpretation: {interpretation}")
            print(f"   Confidence: {confidence}")
            print(f"   Reasoning: {reasoning}")
            
            # Only accept HIGH or MEDIUM confidence answers
            if not answer_provided or confidence == 'LOW':
                print(f"⚠️ Low confidence or no answer - will ask again")
                return (False, None, confidence)
            
            # Convert interpretation to boolean
            if interpretation == 'HAS_PLAN':
                normalized_value = True
                is_valid = True
            elif interpretation == 'NEEDS_VISIT':
                normalized_value = False
                is_valid = True
            elif interpretation == 'UNCLEAR':
                normalized_value = None
                is_valid = False
            elif interpretation == 'OFF_TOPIC':
                normalized_value = None
                is_valid = False
            else:
                print(f"❌ Unexpected interpretation: {interpretation}")
                return (False, None, "ERROR")
            
            print(f"✅ Validated: has_plan = {normalized_value} (confidence: {confidence})")
            return (is_valid, normalized_value, confidence)
            
        except Exception as e:
            print(f"❌ AI validation error: {str(e)}")
            import traceback
            traceback.print_exc()
            return (False, None, "ERROR")


    def generate_clarifying_question_for_plan_status(self, retry_count: int) -> str:
        """
        Generate varied clarifying questions when plan status is unclear
        Uses different phrasing on retries to help customer understand
        """
        try:
            clarification_prompt = f"""You are a professional appointment assistant.

    SITUATION:
    You asked: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"
    The customer's response was unclear or off-topic.
    This is retry attempt #{retry_count + 1}

    TASK:
    Generate a NEW way to ask about whether they have an existing plan.

    PHRASING OPTIONS (use different ones for different retries):

    Retry 1 (Direct):
    "Just to clarify - do you already have plans/blueprints for your bathroom, or would you like our plumber to visit first and create a plan?"

    Retry 2 (Explanation):
    "I need to know if you have existing plans (blueprints/drawings) that our plumber should review, OR if you need us to come assess your space first. Which one?"

    Retry 3 (Simple Yes/No):
    "Quick question: Do you have plans/blueprints ready? 
    • Reply YES if you have plans to send us
    • Reply NO if you need us to visit and assess first"

    Retry 4 (Examples):
    "Let me explain the options:

    Option A: You already have architectural plans/blueprints → We review them first
    Option B: You don't have plans yet → We do a site visit to assess and create a plan

    Which option fits your situation - A or B?"

    REQUIREMENTS:
    - Keep it professional but friendly
    - Be clear and concise (2-3 sentences max)
    - Use language appropriate for retry #{retry_count + 1}
    - No markdown formatting
    - If retry > 3, use very simple YES/NO format

    Current retry: {retry_count}

    Generate the clarifying question:"""

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful appointment assistant. Generate clear, varied questions."
                    },
                    {
                        "role": "user",
                        "content": clarification_prompt
                    }
                ],
                temperature=0.8,  # Higher temp for variety
                max_tokens=150
            )
            
            clarifying_question = response.choices[0].message.content.strip()
            print(f"🤖 Generated clarifying question (retry {retry_count}): {clarifying_question[:100]}...")
            
            return clarifying_question
            
        except Exception as e:
            print(f"❌ Error generating clarifying question: {str(e)}")
            # Fallback questions by retry count
            fallbacks = [
                "Just to confirm - do you have plans already, or would you like us to do a site visit?",
                "I need to know: do you have existing blueprints/plans, or should we visit your property first?",
                "Simple question: Do you have plans? Reply YES or NO.",
                "Option A: I have plans to send. Option B: I need a site visit. Which one - A or B?"
            ]
            return fallbacks[min(retry_count, len(fallbacks) - 1)]


    def _plan_question_already_pending(self) -> bool:
        """
        Return True if the bot's most recent message already asked about
        plan vs site visit. Prevents asking the same question twice in a row.
        """
        try:
            history = self.appointment.conversation_history or []
            for msg in reversed(history):
                if msg.get('role') == 'assistant':
                    content = msg.get('content', '').lower()
                    plan_phrases = [
                        'do you have a plan',
                        'have a plan',
                        'site visit',
                        'picture or pdf',
                        'plan already',
                        'plan or visit',
                        'photo/plan',
                        'photo or plan',
                    ]
                    return any(phrase in content for phrase in plan_phrases)
            return False
        except Exception:
            return False

    def handle_plan_later_response(self, message):
        """
        Use DeepSeek to detect if customer is saying they'll send their plan later.
        Returns True ONLY if customer clearly has a plan but will send it later.
        Never triggers on site visit requests.
        """
        try:
            # Only check if plan status is still undecided
            if self.appointment.has_plan is not None:
                return False

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an intent classifier for a plumbing appointment system in Zimbabwe. Customers may write in English, Shona, or mixed. Reply with ONLY 'YES' or 'NO'."
                    },
                    {
                        "role": "user",
                        "content": f"""We asked the customer: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"

    Is the customer saying they HAVE a plan and will send/share it later (not now)?

    This should be YES ONLY if:
    - They confirm they have a plan/blueprint/drawing
    - AND they say they will send it later, tonight, tomorrow, soon etc.

    This should be NO if:
    - They are asking for a site visit (even if they mention "tomorrow" as when they want the visit)
    - They say they don't have a plan
    - They mention "tomorrow" in the context of scheduling a visit, not sending a plan
    - They are asking about anything else

    Examples of YES:
    - "I'll send the plan later"
    - "I have blueprints, will send tonight"  
    - "Ndinayo plan, nditumire mangwana" (I have a plan, let me send it tomorrow)
    - "Let me send the drawings when I get home"

    Examples of NO:
    - "Site visit tomorrow" ← NO, they want a visit tomorrow, not sending a plan
    - "Come tomorrow for the visit"
    - "I don't have a plan"
    - "Please do a site visit"
    - "Uye uone mangwana" (Come and see tomorrow)
    - "Kwete, uye utarise" (No, come and look)

    Customer message: "{message}"

    Reply YES or NO only."""
                    }
                ],
                temperature=0.1,
                max_tokens=5
            )

            result = response.choices[0].message.content.strip().upper()
            is_plan_later = result == "YES"

            print(f"🤖 DeepSeek plan-later detection: '{message}' → {result}")

            if is_plan_later:
                self.appointment.has_plan = True
                if self._appointment_has_field('retry_count'):
                    self.appointment.save(update_fields=['retry_count'])
                print(f"✅ Updated: has_plan = True (customer will send plan later)")

            return is_plan_later

        except Exception as e:
            print(f"❌ DeepSeek plan-later detection error: {str(e)}")
            return False  # Safe default — don't assume

    def has_basic_info_for_plan_upload(self):
        """Check if we have enough basic info to start plan upload process"""
        return (self.appointment.project_type and 
                self.appointment.customer_area and 
                self.appointment.property_type)

    def initiate_plan_upload_flow(self):
        """Start the plan upload process"""
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
            print(f"❌ Error initiating plan upload: {str(e)}")
            return "I'd like to help you with your plan, but I'm having a technical issue. Could you try again in a moment?"

    def handle_plan_upload_flow(self, message):
        """Handle messages during plan upload process"""
        try:
            # Check if this is a plan completion message
            completion_indicators = ['done', 'finished', 'complete', 'that\'s all', 'no more', 'all sent']
            message_lower = message.lower()
            
            if any(indicator in message_lower for indicator in completion_indicators):
                return self.complete_plan_upload()
            
            # Check for more images/documents
            if any(word in message_lower for word in ['more', 'another', 'next', 'additional']):
                return "Great! Please send the next image or document."
            
            # Check for questions or concerns
            if '?' in message or any(word in message_lower for word in ['help', 'how', 'what', 'problem', 'issue']):
                return self.handle_plan_upload_question(message)
            
            # Default response during upload
            return """Thanks! I can see you're sending the plan materials. 

If you have more images or documents to send, please continue. 

When you're finished sending everything, just type "done" or "finished" and I'll send it all to the plumber."""

        except Exception as e:
            print(f"❌ Error in plan upload flow: {str(e)}")
            return "I'm processing your plan. If you have more to send, please continue. Type 'done' when finished."

    def handle_plan_upload_question(self, message):
        """Handle questions during plan upload process"""
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
                return "I'm here to help with your plan upload. Send your images/documents and type 'done' when finished. Any specific questions about the upload process?"

        except Exception as e:
            print(f"❌ Error handling upload question: {str(e)}")
            return "Please continue sending your plan materials. Type 'done' when you've sent everything."



    def complete_plan_upload(self):
        """Complete the plan upload process and notify plumber"""
        try:
            # Update appointment status
            self.appointment.plan_status = 'plan_uploaded'
            self.appointment.save()

            plumber_number = getattr(
                self.appointment,
                'plumber_contact_number',
                '+263774819901'
            )

            # Notify plumber
            self.notify_plumber_about_plan()

            service_name = self.appointment.project_type.replace('_', ' ').title()
            customer_name = self.appointment.customer_name

            # ✅ Customer-friendly wording
            if customer_name:
                intro_message = (
                    f"Hi {customer_name}, I've forwarded your {service_name} "
                    "plan to our plumber for review."
                )
            else:
                intro_message = (
                    f"Thanks! I've forwarded your {service_name} "
                    "plan to our plumber for review."
                )

            completion_message = f"""✅ PLAN SENT SUCCESSFULLY!

    {intro_message}

    📞 NEXT STEPS:
    • Our plumber will review your plan within 24 hours
    • They'll contact you directly on this number: {self.phone_number.replace('whatsapp:', '')}
    • They'll discuss the project details and provide a quote
    • Once approved, they'll book your appointment or message you to complete booking

    🔧 PLUMBER DIRECT CONTACT:
    If you need to reach them directly: {plumber_number.replace('+263', '0').replace('+', '')}

    You don't need to do anything now — just wait for their call. They're very responsive!

    Questions? Feel free to ask here anytime 😊
    """

            return completion_message

        except Exception as e:
            print(f"❌ Error completing plan upload: {str(e)}")
            return (
                "Your plan has been uploaded successfully. "
                "Our plumber will review it and contact you within 24 hours."
            )


    def detect_service_inquiry(self, message):
        """Use DeepSeek to detect if customer is asking about products/services/pricing."""
        try:
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an intent classifier for a Zimbabwean plumbing company. Customers may write in English, Shona, or mixed. Return ONLY valid JSON, no markdown."
                    },
                    {
                        "role": "user",
                        "content": f"""Classify the customer's message into ONE of these intents.

    Customer message: "{message}"

    "If the customer mentions multiple products (e.g. tub AND chamber, toilet AND
    shower), classify as 'bathtub_installation' if a tub is mentioned, otherwise
    pick the most prominent product.  Never return 'none' just because multiple
    products are mentioned — pick the most specific/expensive one."

    EXTRA CLASSIFICATION RULES:
    - Only choose an intent that is explicitly mentioned in the message.
    - Never return bathtub_installation unless the message explicitly mentions
      a tub, bathtub, bath, or freestanding tub.
    - If multiple non-tub products are mentioned, pick the clearest mentioned
      product instead of defaulting to bathtub_installation.

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
    - combined_pricing: asking for total/combined cost, a full quotation, or general pricing,
      e.g. "how much for all", "how much zvese zvakadai", "zvese izvi zvinodhura marii",
      "total for everything", "all together how much", "what's the total",
      "I want a quotation", "send me a quote", "I need a quote", "ndida quotation",
      "how much overall", "how much is everything", "marii zvese"
    - none: none of the above

    CRITICAL RULES:
    1. location_ask vs location_visit:
    - location_ask = ONLY asking for address/whereabouts. Examples:
        * "Where are you located"
        * "Whre ar u located"
        * "Where are you based"
        * "What's your address"
        * "Muri kupi" (Shona: where are you)
        * "Muri kupi imimi"
    - location_visit = customer explicitly wants to come in person. Examples:
        * "Can I come to your office"
        * "Ko when can I come ku office"
        * "I want to visit your showroom"
        * "Can I come and see the tubs"
        * "When can I come in"

    ⚠️ IMPORTANT EXCEPTIONS — these are NOT location_visit:
    * 'Site visit' alone = customer is answering a plan question (needs site visit to their property)
    * 'Site visit would be perfect' = same
    * 'I need a site visit' = same
    These should return intent: 'none'"
    
    - If message is ONLY an area name like "Hatfield", "Avondale", "Glen View" → intent must be "none"

    2. Confidence rules:
    - HIGH = message clearly matches the intent. Short messages naming a specific
      product are HIGH — product names are unambiguous regardless of length.
      bathtub_installation is only valid when the message explicitly mentions
      a tub, bathtub, bath, or freestanding tub.
      Examples that are HIGH confidence:
        * "how much tub", "tub price", "tub cost"
        * "geyser install", "geyser price", "how much geyser"
        * "toilet price", "how much toilet", "toilet cost"
        * "shower cubicle price", "how much shower"
        * "chamber price", "side chamber cost"
        * "vanity price", "how much vanity"
        * "bathtub install", "bath installation"
        * "facebook package", "the package"
        * "where are you", "your address", "where are you located"
        * "can I come", "can I visit your office"
        * "send pictures", "show me photos", "got pics"
        * "how much zvese", "zvese zvakadai", "how much for all", "total for everything"
        * "I want quotation", "send me a quote", "I need a quote", "ndida quotation"
        * "how much" (standalone, no product mentioned)
    - LOW = message is genuinely ambiguous and could match multiple intents
      or no specific product/service
      
    Return ONLY this JSON:
    {{
        "intent": "one of the intents above",
        "confidence": "HIGH or LOW"
    }}"""
                    }
                ],
                temperature=0.1,
                max_tokens=50
            )

            ai_response = response.choices[0].message.content.strip()
            ai_response = ai_response.replace('```json', '').replace('```', '').strip()
            result = json.loads(ai_response)

            message_lower = (message or '').lower()
            tub_terms = ('tub', 'bathtub', 'bath', 'freestanding tub', 'free-standing tub')
            if result.get('intent') == 'bathtub_installation' and not any(term in message_lower for term in tub_terms):
                if 'shower' in message_lower:
                    result = {"intent": "shower_cubicle", "confidence": result.get('confidence', 'HIGH')}
                elif 'chamber' in message_lower:
                    result = {"intent": "chamber", "confidence": result.get('confidence', 'HIGH')}
                elif 'toilet' in message_lower:
                    result = {"intent": "toilet", "confidence": result.get('confidence', 'HIGH')}
                else:
                    result = {"intent": "none", "confidence": "LOW"}

            print(f"🤖 Service inquiry detection: '{message}' → {result}")
            return result

        except Exception as e:
            print(f"❌ Service inquiry detection error: {str(e)}")
            return {"intent": "none", "confidence": "LOW"}


    def handle_service_inquiry(self, intent, message):
            """Generate response for product/service/pricing inquiries in English or Shona."""
            try:
                # Detect language
                lang_response = deepseek_client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {
                            "role": "system",
                            "content": "Detect the language of this message. Reply with ONLY 'shona', 'english', or 'mixed'."
                        },
                        {
                            "role": "user",
                            "content": message
                        }
                    ],
                    temperature=0.1,
                    max_tokens=5
                )
                language = lang_response.choices[0].message.content.strip().lower()
                print(f"🌍 Detected language: {language}")

                plumber_number = self.appointment.plumber_contact_number or '+263774819901'

                # Has the customer already committed to a site visit or given their location?
                already_visiting = self.appointment.has_plan is False
                has_area = bool(self.appointment.customer_area)
                visit_committed = already_visiting or has_area

                structured_pricing = {
                    "tub_sales": {
                        "breakdown_lines": [
                            "Tub supply: from US$400",
                            "Mixer if needed: from US$150",
                            "Installation and finishing: from US$120",
                        ],
                        "total_line": "Roughly looking at about US$670 for a basic supply-and-fit setup.",
                        "cheapest_line": "The cheapest tub option is an ordinary tub, which starts from US$80.",
                        "sn_breakdown_lines": [
                            "Tub supply: kubva US$400",
                            "Mixer kana ichidiwa: kubva US$150",
                            "Installation ne finishing: kubva US$120",
                        ],
                        "sn_total_line": "Zvingangoita US$670 pa basic supply-and-fit setup.",
                        "sn_cheapest_line": "Cheapest tub option i ordinary tub, inotangira paUS$80.",
                    },
                    "standalone_tub": {
                        "breakdown_lines": [
                            "Free-standing tub supply: from US$450",
                            "Free-standing mixer: from US$150",
                            "Mixer and tub installation: from US$120",
                        ],
                        "total_line": "Roughly looking at about US$720 for everything on a basic freestanding setup.",
                        "cheapest_line": "The cheapest tub option is an ordinary tub, which starts from US$80.",
                        "sn_breakdown_lines": [
                            "Free-standing tub supply: kubva US$450",
                            "Free-standing mixer: kubva US$150",
                            "Kuisa mixer netub: kubva US$120",
                        ],
                        "sn_total_line": "Zvingangoita US$720 yezvinhu zvese pa basic freestanding setup.",
                        "sn_cheapest_line": "Cheapest tub option i ordinary tub, inotangira paUS$80.",
                    },
                    "geyser": {
                        "breakdown_lines": [
                            "Geyser installation labour: from US$80",
                            "Extra fittings and connectors if needed: from US$20",
                            "Access and size-related extras can add more",
                        ],
                        "total_line": "Roughly looking at about US$100 to US$180 for a straightforward geyser installation.",
                        "cheapest_line": "The cheapest option is when you already have the geyser and it's a simple swap, where labour starts from US$80.",
                        "sn_breakdown_lines": [
                            "Labour yekuisa geyser: kubva US$80",
                            "Extra fittings nema connectors kana zvichidiwa: kubva US$20",
                            "Kana access yakaoma kana size yakakura zvinogona kuwedzera",
                        ],
                        "sn_total_line": "Zvingangoita US$100 kusvika US$180 pakuisa geyser kuri straightforward.",
                        "sn_cheapest_line": "Cheapest option ndeyekuti muchitova negeyser uye iri simple swap, apo labour inotangira paUS$80.",
                    },
                    "shower_cubicle": {
                        "breakdown_lines": [
                            "Standard 900x900 cubicle supply: from US$130",
                            "Installation: from US$40",
                        ],
                        "total_line": "Roughly looking at about US$170 for everything on a standard cubicle fit.",
                        "cheapest_line": "The cheapest option is a standard-size cubicle setup starting from US$170 all-in.",
                        "sn_breakdown_lines": [
                            "Standard 900x900 cubicle supply: kubva US$130",
                            "Installation: kubva US$40",
                        ],
                        "sn_total_line": "Zvingangoita US$170 yezvinhu zvese pa standard cubicle fit.",
                        "sn_cheapest_line": "Cheapest option i standard-size cubicle setup inotangira paUS$170 all-in.",
                    },
                    "vanity": {
                        "breakdown_lines": [
                            "Vanity unit supply: from US$150",
                            "Installation labour: from US$30",
                        ],
                        "total_line": "Roughly looking at about US$180 for a basic vanity setup, and more for larger or custom finishes.",
                        "cheapest_line": "The cheapest option is a small standard vanity, or just installation if you already have one, with labour starting from US$30.",
                        "sn_breakdown_lines": [
                            "Vanity unit supply: kubva US$150",
                            "Installation labour: kubva US$30",
                        ],
                        "sn_total_line": "Zvingangoita US$180 pa basic vanity setup, uye zvinokwira kana yakakura kana custom.",
                        "sn_cheapest_line": "Cheapest option i small standard vanity, kana kungoiisa chete kana muchitova nayo, labour ichitangira paUS$30.",
                    },
                    "bathtub_installation": {
                        "breakdown_lines": [
                            "Standard bathtub installation labour: from US$80",
                            "Free-standing tub supply if needed: from US$450",
                            "Free-standing mixer if needed: from US$150",
                            "Mixer installation: from US$120",
                        ],
                        "total_line": "Roughly looking at about US$80 for a basic install if you already have the tub, or from around US$720 for a full freestanding setup.",
                        "cheapest_line": "The cheapest option is when you already have a standard built-in tub, with installation starting from US$80.",
                        "sn_breakdown_lines": [
                            "Labour yekuisa standard bathtub: kubva US$80",
                            "Free-standing tub supply kana ichidiwa: kubva US$450",
                            "Free-standing mixer kana ichidiwa: kubva US$150",
                            "Kuisa mixer: kubva US$120",
                        ],
                        "sn_total_line": "Zvingangoita US$80 pa basic install kana muchitova netub, kana kubva paUS$720 pa full freestanding setup.",
                        "sn_cheapest_line": "Cheapest option ndeyekuti muchitova ne standard built-in tub, installation ichitangira paUS$80.",
                    },
                    "toilet": {
                        "breakdown_lines": [
                            "Close-coupled toilet supply: from US$50",
                            "Installation: from US$20",
                        ],
                        "total_line": "Roughly looking at about US$70 for everything on a standard toilet replacement.",
                        "cheapest_line": "The cheapest option is when you already have the toilet and only need fitting, with labour starting from US$20.",
                        "sn_breakdown_lines": [
                            "Close-coupled toilet supply: kubva US$50",
                            "Installation: kubva US$20",
                        ],
                        "sn_total_line": "Zvingangoita US$70 yezvinhu zvese pa standard toilet replacement.",
                        "sn_cheapest_line": "Cheapest option ndeyekuti muchitova netoilet uye muchingoda fitting chete, labour ichitangira paUS$20.",
                    },
                    "chamber": {
                        "breakdown_lines": [
                            "Side chamber supply: US$130",
                            "Installation: US$30",
                        ],
                        "total_line": "Roughly looking at about US$160 for everything on a standard chamber setup.",
                        "cheapest_line": "The cheapest option is if it's only a chamber fit or adjustment, with labour starting from US$30.",
                        "sn_breakdown_lines": [
                            "Side chamber supply: US$130",
                            "Installation: US$30",
                        ],
                        "sn_total_line": "Zvingangoita US$160 yezvinhu zvese pa standard chamber setup.",
                        "sn_cheapest_line": "Cheapest option ndeyekuti iri chamber fit kana adjustment chete, labour ichitangira paUS$30.",
                    },
                    "facebook_package": {
                        "breakdown_lines": [
                            "Core bathroom package: from US$600",
                            "Fixtures like tubs, showers, toilets, and mixers are added based on what you choose",
                            "Installation and finishing depend on the setup",
                        ],
                        "total_line": "Roughly looking at about US$600 upward, depending on the fixtures and layout.",
                        "cheapest_line": "The cheapest option is the basic package starting from US$600 before extra fixtures are added.",
                        "sn_breakdown_lines": [
                            "Core bathroom package: kubva US$600",
                            "Fixtures dzakaita sematub, mashower, toilet nemamixer zvinowedzerwa zvichienderana nezvamunosarudza",
                            "Installation ne finishing zvinoenderana nesetup",
                        ],
                        "sn_total_line": "Zvingangoita kubva paUS$600 zvichikwira, zvichienderana nema fixtures nelayout.",
                        "sn_cheapest_line": "Cheapest option i basic package inotangira paUS$600 zvinhu zvekuwedzera zvisati zvaiswa.",
                    },
                }

                # combined_pricing always delegates to generate_pricing_overview
                # for the full contextual Facebook-anchored response
                if intent == 'combined_pricing':
                    return self.generate_pricing_overview(message)

                if intent in structured_pricing:
                    pricing_payload = structured_pricing[intent]
                    if language == 'shona':
                        return self._build_pricing_response(
                            breakdown_lines=pricing_payload.get("sn_breakdown_lines", pricing_payload["breakdown_lines"]),
                            total_line=pricing_payload.get("sn_total_line", pricing_payload["total_line"]),
                            cheapest_line=pricing_payload.get("sn_cheapest_line", pricing_payload["cheapest_line"]),
                            visit_committed=visit_committed,
                            language="shona",
                        )
                    return self._build_pricing_response(
                        breakdown_lines=pricing_payload["breakdown_lines"],
                        total_line=pricing_payload["total_line"],
                        cheapest_line=pricing_payload["cheapest_line"],
                        visit_committed=visit_committed,
                    )

                # ── Pricing responses ──
                # Two variants per intent where relevant:
                #   "en" / "sn"         → standard (visit not yet committed)
                #   "en_v" / "sn_v"     → visit committed (drop the site-visit pitch)

                pricing_info = {

                    "tub_sales": {
                        "en": (
                            "Tubs start from US$400 supply-only, or US$500–$800 supply + install — "
                            "depends on the style and size. 🛁\n\n"
                            "Do you know what size space you're working with, or would it be easier "
                            "to have us come measure and give you a fixed price on the spot? "
                            "(Site assessment is free)"
                        ),
                        "en_v": (
                            "Tubs start from US$400 supply-only, or US$500–$800 supply + install — "
                            "depends on the style and size. 🛁\n\n"
                            "Our plumber will go through the options with you when they come out."
                        ),
                        "sn": (
                            "Tubs dzinotangira kuUS$400 supply chete, kana US$500–$800 supply neinstallation — "
                            "zvichienda nemhando neukuru. 🛁\n\n"
                            "Unoziva ukuru hwenzvimbo yako here, kana tiuye tiite free assessment "
                            "tikupe mutengo wakakwana pasite?"
                        ),
                        "sn_v": (
                            "Tubs dzinotangira kuUS$400 supply chete, kana US$500–$800 supply neinstallation. 🛁\n\n"
                            "Plumber wedu achakuratidza zvinosarudzwa paauya."
                        ),
                    },

                    "standalone_tub": {
                        "en": (
                            "Standalone / freestanding tubs run US$450–$800 supply, plus US$120–$200 "
                            "to fit and finish. 🛁\n\n"
                            "Full breakdown:\n"
                            "• Free-standing tub supply: from US$450\n"
                            "• Free-standing mixer: from US$150\n"
                            "• tub installation: US$120\n"
                            "• Side chamber: US$130 (installation US$30)\n\n"
                            "Most customers are all-in at US$750–$1,200 depending on the tub they pick.\n\n"
                            "Do you already know which tub style you want, or would you like us to come "
                            "out and show you options on-site? (Free visit, no obligation)"
                        ),
                        "en_v": (
                            "Standalone / freestanding tubs run US$450–$800 supply, plus US$120–$200 "
                            "to fit and finish. 🛁\n\n"
                            "Full breakdown:\n"
                            "• Free-standing tub supply: from US$450\n"
                            "• Free-standing mixer: from US$150\n"
                            "• Mixer + tub installation: US$120\n"
                            "• Side chamber: US$130 (installation US$30)\n\n"
                            "Most customers are all-in at US$750–$1,200 depending on the tub they pick.\n\n"
                            "Our plumber will go through the options with you on-site."
                        ),
                        "sn": (
                            "Free-standing tubs dzinotangira kuUS$450 supply, neUS$120–$200 "
                            "yeinstallation. 🛁\n\n"
                            "• Free-standing tub: kubva US$450\n"
                            "• Free-standing mixer: kubva US$150\n"
                            "• Kuisa mixer netub: US$120\n"
                            "• Side chamber: US$130 (installation US$30)\n\n"
                            "Vazhinji vanobhadhara US$750–$1,200 zvichienda netub yavasarudza.\n\n"
                            "Unoziva mhando yetub yaungada here, kana tiuye tikuratidze zvinosarudzwa pasite?"
                        ),
                        "sn_v": (
                            "Free-standing tubs dzinotangira kuUS$450 supply, neUS$120–$200 yeinstallation. 🛁\n\n"
                            "• Free-standing tub: kubva US$450\n"
                            "• Free-standing mixer: kubva US$150\n"
                            "• Kuisa mixer netub: US$120\n"
                            "• Side chamber: US$130 (installation US$30)\n\n"
                            "Vazhinji vanobhadhara US$750–$1,200. Plumber wedu achakuratidza paauya."
                        ),
                    },

                    "geyser": {
                        "en": (
                            "Geyser installation starts from US$80 — most jobs land between US$80–$180 "
                            "depending on the geyser size and access. 🔥\n\n"
                            "What size geyser are you putting in? (100L, 150L, 200L?) — "
                            "that'll let me give you a tighter number right now."
                        ),
                        "sn": (
                            "Kuisa geyser kunotangira kuUS$80 — mazhinji mapoka anosvika US$80–$180 "
                            "zvichienda nekukura kwegeyser. 🔥\n\n"
                            "Geyser yaunoda yakura zvakadini? (100L, 150L, 200L?) — "
                            "ndingakupe mutengo wakajika zviri nani."
                        ),
                    },

                    "shower_cubicle": {
                        "en": (
                            "Shower cubicles (900×900mm) start from US$130 supply + US$40 install — "
                            "so roughly US$170 all-in for a standard fit. 🚿\n\n"
                            "Bigger cubicles or custom sizes run a bit more. "
                            "Do you know the rough dimensions, or should we come out and measure? "
                            "(Free site visit)"
                        ),
                        "en_v": (
                            "Shower cubicles (900×900mm) start from US$130 supply + US$40 install — "
                            "roughly US$170 all-in for a standard fit. 🚿\n\n"
                            "Bigger or custom sizes run a bit more. Our plumber will measure up "
                            "and confirm the exact price when they come out."
                        ),
                        "sn": (
                            "Shower cubicles (900×900mm) dzinotangira kuUS$130 supply neUS$40 installation — "
                            "pamwe US$170 yese. 🚿\n\n"
                            "Huru dzakakura dzinoti nzira dzinopfuura. "
                            "Unoziva saizi here, kana tiuye tiite free visit tiite measurement?"
                        ),
                        "sn_v": (
                            "Shower cubicles dzinotangira kuUS$170 yese ye900×900mm. 🚿\n\n"
                            "Plumber wedu achaveza uye akupe mutengo wakajika paauya."
                        ),
                    },

                    "vanity": {
                        "en": (
                            "Custom vanity units start from US$150 + US$30 labour — "
                            "most jobs come out at US$180–$350 depending on size and finish. 🪞\n\n"
                            "What size are you thinking? (Width in cm helps, even roughly)"
                        ),
                        "sn": (
                            "Ma vanity unit anotangira kuUS$150 neUS$30 yevashandi — "
                            "mazhinji mapoka anosvika US$180–$350 zvichienda nekukura nekugadzirwa. 🪞\n\n"
                            "Unofunga ukuru hwakaita sei? (Upamhi mucm unobatsira, kunyangwe wakangofanana)"
                        ),
                    },

                    "bathtub_installation": {
                        "en": (
                            "Bathtub installation runs US$80–$200 depending on the type: 🛁\n\n"
                            "• Ordinary tub (with wall finishing): from US$80\n"
                            "• Free-standing tub supply: from US$450\n"
                            "• Free-standing mixer: from US$150\n"
                            "• Mixer installation: US$120\n"
                            "• Side chamber: US$130 (install US$30)\n\n"
                            "What type of tub are you going with — standard built-in or freestanding?"
                        ),
                        "sn": (
                            "Kuisa bathtub kunosvika US$80–$200 zvichienda nemhando: 🛁\n\n"
                            "• Tub yakajairwa (ine wall finishing): kubva US$80\n"
                            "• Free-standing tub: kubva US$450\n"
                            "• Free-standing mixer: kubva US$150\n"
                            "• Kuisa mixer: US$120\n"
                            "• Side chamber: US$130 (install US$30)\n\n"
                            "Unoda mhando ipi — yakavakirwa mumadziro kana inomira yega?"
                        ),
                    },

                    "toilet": {
                        "en": (
                            "Toilet supply + install runs US$70–$120 for a standard close-coupled unit: 🚽\n\n"
                            "• Close-coupled toilet supply: from US$50\n"
                            "• Installation: from US$20\n"
                            "• Side chamber: US$130 (install US$30)\n\n"
                            "Are you replacing an existing toilet or fitting a new one in a fresh space?"
                        ),
                        "sn": (
                            "Toilet supply neinstallation inosvika US$70–$120 yetoilet yakajairwa: 🚽\n\n"
                            "• Close-coupled toilet: kubva US$50\n"
                            "• Kuisa: kubva US$20\n"
                            "• Side chamber: US$130 (install US$30)\n\n"
                            "Uri kutsiva toilet yaimbopo kana kuisa itsva munzvimbo itsva?"
                        ),
                    },

                    "chamber": {
                        "en": (
                            "Side chamber supply + install is US$160 all-in (US$130 supply, US$30 fit). 🚽\n\n"
                            "If you also need a toilet: close-coupled units start from US$50 supply + US$20 install.\n\n"
                            "Are you just doing the chamber, or the full toilet setup?"
                        ),
                        "sn": (
                            "Side chamber supply neinstallation ndiUS$160 yese (US$130 supply, US$30 kuisa). 🚽\n\n"
                            "Kana uchidawo toilet: close-coupled toilet inotangira kuUS$50 supply neUS$20 installation.\n\n"
                            "Uri kuita chamber chete kana setup yese yetoilet?"
                        ),
                    },

                    "facebook_package": {
                        "en": (
                            "The bathroom package from our Facebook ad starts from US$600. 📢\n\n"
                            "That covers the core fit-out — exact price depends on the size of your bathroom "
                            "and fixtures you choose.\n\n"
                            "Want us to come do a free on-site assessment so we can lock in your exact number?"
                        ),
                        "en_v": (
                            "The bathroom package from our Facebook ad starts from US$600. 📢\n\n"
                            "Exact price depends on your bathroom size and fixtures. "
                            "Our plumber will lock in your exact price when they come out."
                        ),
                        "sn": (
                            "Package yebathroom yatakaiswa pa Facebook inotangira kuUS$600. 📢\n\n"
                            "Iyo inofukidza basa guru — mutengo wakakwana unoenderana nekukura kwebathroom "
                            "nemhando yezvinhu zvaunosarudza.\n\n"
                            "Unoda here kuti tiuye tiite free assessment tikupe mutengo wakajika?"
                        ),
                        "sn_v": (
                            "Package yebathroom yatakaiswa pa Facebook inotangira kuUS$600. 📢\n\n"
                            "Plumber wedu achakupa mutengo wakajika paauya."
                        ),
                    },

                    "location_ask": {
                        "en": "We are based in Hatfield, Harare, and yourself 📍\n\n",
                        "sn": "Tiri muHatfield, Harare. 📍\n\n",
                    },

                    "location_visit": {
                        "en": (
                            "We work by appointment rather than walk-ins. 📍 We're in Hatfield, Harare.\n\n"
                            "Would you like us to come to you instead? We can do a free on-site assessment "
                            "at your place — saves you the trip and gets you a fixed price on the spot."
                        ),
                        "sn": (
                            "Tinoshandisa ne appointment, hatisi kushanda ne walk-ins. 📍 Tiri muHatfield, Harare.\n\n"
                            "Unoda here kuti tiuye kwauri? Tinogona kuita free assessment paimba yako — "
                            "kukuponesa rwendo uye tikupe mutengo wakakwana pasite."
                        ),
                    },

                    "previous_quotation": {
                        "en": (
                            f"For your previous quotation, please reach out to our plumber directly "
                            f"and they'll pull it up for you right away. 📄\n\n"
                            f"Contact: {plumber_number}"
                        ),
                        "sn": (
                            f"Kuti uwane quotation yako yekare, taura neplumber yedu directly "
                            f"uye vachakubatsira nekukurumidza. 📄\n\n"
                            f"Bata: {plumber_number}"
                        ),
                    },

                    "combined_pricing": {
                        "en": (
                            "Here's a rough combined estimate based on everything we've discussed:\n\n"
                            "• Shower cubicle (supply + install): from US$170\n"
                            "• Vanity unit (supply + install): from US$180\n"
                            "• Toilet (supply + install): from US$70\n"
                            "• Chamber (supply + install): US$160\n"
                            "• Freestanding tub (supply + install): from US$720\n\n"
                            "Final price depends on your specific setup — once our plumber sees the space they'll give you a fixed number.\n\n"
                            f"{self._get_pricing_followup_prompt('english')}"
                        ),
                        "sn": (
                            "Apa mutengo wakafanana wezvinhu zvese zvatakaongorora:\n\n"
                            "• Shower cubicle (supply + install): kubva US$170\n"
                            "• Vanity unit (supply + install): kubva US$180\n"
                            "• Toilet (supply + install): kubva US$70\n"
                            "• Chamber (supply + install): US$160\n"
                            "• Freestanding tub (supply + install): kubva US$720\n\n"
                            "Mutengo wakakwana unoenderana nesetup yako — plumber wedu aona nzvimbo yako ozokuudza mutengo wakajika.\n\n"
                            f"{self._get_pricing_followup_prompt('shona')}"
                        ),
                    },

                    "pictures": {
                        "en": (
                            f"Our plumber can send you photos directly — they have the full portfolio. 📸\n\n"
                            f"Contact them on: {plumber_number}"
                        ),
                        "sn": (
                            f"Plumber wedu anogona kukutumira mifananidzo directly — vane portfolio yese. 📸\n\n"
                            f"Bata: {plumber_number}"
                        ),
                    },
                }

                responses = pricing_info.get(intent, {})

                # Select language key — prefer visit-committed variant when applicable
                if language == 'shona':
                    if visit_committed:
                        reply = responses.get('sn_v') or responses.get('sn', '')
                    else:
                        reply = responses.get('sn', '')
                else:
                    if visit_committed:
                        reply = responses.get('en_v') or responses.get('en', '')
                    else:
                        reply = responses.get('en', '')

                # Fallback to DeepSeek if no response found
                if not reply:
                    reply = self.generate_contextual_response(message, self.get_next_question_to_ask(), [])

                return reply

            except Exception as e:
                print(f"❌ Error handling service inquiry: {str(e)}")
                return self.generate_contextual_response(message, self.get_next_question_to_ask(), [])

    def _generate_pricing_overview_legacy(self, message):
        """Send approximate prices when customer asks about cost"""
        # Try to detect specific service first
        inquiry = self.detect_service_inquiry(message)
        
        if inquiry.get('intent') != 'none' and inquiry.get('confidence') == 'HIGH':
            return self.handle_service_inquiry(inquiry['intent'], message)

        try:
            lang_response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "Detect the language of this message. Reply with ONLY 'shona', 'english', or 'mixed'."
                    },
                    {
                        "role": "user",
                        "content": message
                    }
                ],
                temperature=0.1,
                max_tokens=5
            )
            language = lang_response.choices[0].message.content.strip().lower()
        except Exception:
            language = "english"

        if language == "shona":
            return (
                "Tub netoilet zviri paFacebook picture zvinenge zviri around US$500, "
                "uye labour ingangoita US$150 👍\n\n"
                "Final price inoenderana nesetup, saka tinozoconfirm kana tauya tangoona space.\n\n"
                f"{self._get_pricing_followup_prompt('shona')}"
            )

        return (
            "The tub & toilet on the Facebook picture are around US$500, "
            "and labour is about US$150 👍\n\n"
            "Final price depends on the setup, so we confirm after a quick site check.\n\n"
            f"{self._get_pricing_followup_prompt('english')}"
        )

    def generate_pricing_overview(self, message):
        """
        Send pricing overview for vague questions like 'how much', 'I want a quotation',
        'how much zvese zvakadai', or any reference to the Facebook offer.
        Anchors on the Facebook bathroom package, then shows individual item prices,
        then pushes to booking.
        """
        # Try to detect a specific service first — if HIGH confidence, hand off
        inquiry = self.detect_service_inquiry(message)
        if inquiry.get('intent') not in ('none', 'combined_pricing') and inquiry.get('confidence') == 'HIGH':
            return self.handle_service_inquiry(inquiry['intent'], message)

        # Detect language
        try:
            lang_response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "Detect the language of this message. Reply with ONLY 'shona', 'english', or 'mixed'."
                    },
                    {"role": "user", "content": message}
                ],
                temperature=0.1,
                max_tokens=5
            )
            language = lang_response.choices[0].message.content.strip().lower()
        except Exception:
            language = "english"

        # Has the customer committed to a site visit or given their area?
        visit_committed = (
            self.appointment.has_plan is False or
            bool(self.appointment.customer_area)
        )

        # Build context line based on what we know about the customer's project
        project_context = ""
        if self.appointment.project_description:
            desc_lower = self.appointment.project_description.lower()
            if any(w in desc_lower for w in ('tiled', 'already tiled', 'tile', 'existing')):
                project_context = (
                    "Since your bathroom is already tiled, the renovation cost "
                    "focuses on fixtures and fittings rather than tiling. "
                )
            elif any(w in desc_lower for w in ('new', 'from scratch', 'building')):
                project_context = (
                    "For a new bathroom build, pricing covers both rough plumbing and fixtures. "
                )

        followup = self._get_pricing_followup_prompt(language)

        if language == 'shona':
            reply = (
                f"{project_context}"
                "Mutengo unoenderana nezvaunoda, asi apa ndimwe mitengo yakajairwa:\n\n"
                "Facebook bathroom package: kubva US$600\n"
                "  (Iyi inofukidza basa guru — fixtures dzinowedzerwa)\n\n"
                "Kana uchida zvimwe-zwimwe:\n"
                "• Shower cubicle (supply + install): kubva US$170\n"
                "• Vanity unit (supply + install): kubva US$180\n"
                "• Toilet (supply + install): kubva US$70\n"
                "• Side chamber (supply + install): US$160\n"
                "• Free-standing tub (supply + install): kubva US$720\n"
                "• Geyser installation: kubva US$80\n\n"
                "Mutengo wakakwana unoenderana nesetup yako — "
                "plumber wedu aona nzvimbo yako ozokuudza mutengo wakajika.\n\n"
                f"{followup}"
            )
        else:
            reply = (
                f"{project_context}"
                "Pricing depends on what you need, but here's a rough guide:\n\n"
                "Facebook bathroom package: from US$600\n"
                "  (Covers the core fit-out — fixtures added based on your choice)\n\n"
                "Individual items:\n"
                "• Shower cubicle (supply + install): from US$170\n"
                "• Vanity unit (supply + install): from US$180\n"
                "• Toilet (supply + install): from US$70\n"
                "• Side chamber (supply + install): US$160\n"
                "• Free-standing tub (supply + install): from US$720\n"
                "• Geyser installation: from US$80\n\n"
                "Final price depends on your setup — once our plumber sees the space "
                "they'll give you a fixed number on the spot.\n\n"
                f"{followup}"
            )

        return reply

    def notify_plumber_about_plan(self):
        """Send plan details to plumber via WhatsApp"""
        try:
            base_url = os.getenv("SITE_URL", "http://127.0.0.1:8000")

            service_name = self.appointment.project_type.replace('_', ' ').title()
            customer_name = self.appointment.customer_name or "Customer"
            customer_phone = self.phone_number.replace('whatsapp:', '')

            details_url = (
                f"{base_url}/appointments/"
                f"{self.appointment.id}/documents/"
            )

            plumber_message = f"""📋 NEW PLAN RECEIVED!

    Customer: {customer_name}
    Phone: {customer_phone}
    Service: {service_name}
    Area: {self.appointment.customer_area}
    Property: {self.appointment.property_type}
    Timeline: {self.appointment.timeline}

    🔍 PLAN DETAILS:
    The customer has uploaded their plan via WhatsApp.

    Please:
    1. Review the uploaded plan materials
    2. Contact the customer within 24 hours
    3. Discuss project scope and provide a quote
    4. Book appointment once confirmed

    🔗 View full details:
    {details_url}

    Status: Plan uploaded — awaiting your review
    """

            plumber_numbers = [
                '263774819901',  # ✅ international format
            ]

            for number in plumber_numbers:
                whatsapp_api.send_text_message(number, plumber_message)
                print(f"✅ Plan notification sent to plumber {number}")

        except Exception as e:
            print(f"❌ Error notifying plumber: {str(e)}")
            
    def handle_post_upload_messages(self, message):
        """Handle messages after plan has been uploaded"""
        try:
            message_lower = message.lower()
            
            # Check for status inquiries
            if any(word in message_lower for word in ['status', 'update', 'heard', 'contact', 'call']):
                return self.provide_plan_status_update()
            
            # Check for plan changes
            if any(word in message_lower for word in ['change', 'update', 'modify', 'different', 'new plan']):
                return self.handle_plan_change_request()
            
            # Check for urgent requests
            if any(word in message_lower for word in ['urgent', 'asap', 'emergency', 'rush']):
                return self.handle_urgent_plan_request()
            
            # Default response for post-upload phase
            return """Your plan has been sent to our plumber and they'll contact you within 24 hours.

If you need immediate assistance:
📞 Call directly: 0774819901

Otherwise, please wait for their review and call. They're very reliable!

Need to change something about your plan? Let me know."""

        except Exception as e:
            print(f"❌ Error handling post-upload message: {str(e)}")
            return "Your plan is with our plumber for review. They'll contact you within 24 hours."

    def provide_plan_status_update(self):
        """Provide status update on plan review"""
        # Calculate time since upload
        upload_time = self.appointment.updated_at
        hours_since = (timezone.now() - upload_time).total_seconds() / 3600
        
        if hours_since < 24:
            remaining_hours = int(24 - hours_since)
            return f"""📋 PLAN STATUS UPDATE:

Your plan was sent {int(hours_since)} hours ago. Our plumber typically responds within 24 hours.

Expected contact: Within the next {remaining_hours} hours

If it's urgent, you can call directly: 0774819901

Otherwise, they'll definitely contact you today!"""
        else:
            return """I see it's been over 24 hours since your plan was sent. Let me check on this for you.

Please call our plumber directly at 0774819901 - they may have tried to reach you already.

I'll also send them a follow-up message now."""

    def handle_plan_change_request(self):
        """Handle requests to change or update the plan"""
        self.appointment.plan_status = 'pending_upload'
        self.appointment.save()
        
        return """No problem! I can help you send an updated plan.

Please send your revised plan materials now (images or PDF). 

I'll make sure the plumber gets the updated version and knows it replaces the previous one."""

    def handle_urgent_plan_request(self):
        """Handle urgent plan review requests"""
        try:
            # Send urgent notification to plumber
            urgent_message = f"""🚨 URGENT PLAN REVIEW REQUEST

Customer: {self.appointment.customer_name or 'Customer'}
Phone: {self.phone_number.replace('whatsapp:', '')}
Project: {self.appointment.project_type}

Customer is requesting urgent review of their uploaded plan.

Please contact ASAP: {self.phone_number.replace('whatsapp:', '')}

View details: http://127.0.0.1:8000/appointments/{self.appointment.id}/"""

            # Send to plumber
            twilio_client.messages.create(
                body=urgent_message,
                from_=TWILIO_WHATSAPP_NUMBER,
                to='whatsapp:+0774819901'
            )
            
            return """🚨 I've marked your plan review as URGENT and notified our plumber immediately.

They should contact you within the next few hours.

For immediate assistance, you can also call: 0774819901

I understand this is time-sensitive!"""

        except Exception as e:
            print(f"❌ Error handling urgent request: {str(e)}")
            return "I've noted this is urgent. Please call our plumber directly at 0774819901 for immediate assistance."



    def get_alternative_time_suggestions(self, requested_datetime):
        """UPDATED: More targeted alternative suggestions"""
        try:
            suggestions = []
            
            # Get the requested date and time
            requested_date = requested_datetime.date()
            
            # Business time slots (8am, 10am, 12pm, 2pm, 4pm)
            business_time_slots = [8, 10, 12, 14, 16]
            
            print(f"Looking for alternatives near {requested_datetime}")
            
            # Try same day first, then next few business days
            for day_offset in range(0, 7):  # Check today + next 6 days
                check_date = requested_date + timedelta(days=day_offset)
                
                # Skip Saturday only — Sunday is a working day
                if check_date.weekday() == 5:
                    continue

                for hour in business_time_slots:
                    candidate_time = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    candidate_datetime = sa_timezone.localize(candidate_time)
                    
                    # Skip times in the past
                    if candidate_datetime <= timezone.now():
                        continue
                    
                    # Skip the exact requested time
                    if candidate_datetime == requested_datetime:
                        continue
                    
                    is_available, conflict = self.check_appointment_availability(candidate_datetime)
                    if is_available:
                        day_type = 'same_day' if day_offset == 0 else 'next_days'
                        suggestions.append({
                            'datetime': candidate_datetime,
                            'display': candidate_datetime.strftime('%A, %B %d at %I:%M %p'),
                            'day_type': day_type
                        })
                        
                        # Limit to 4 suggestions
                        if len(suggestions) >= 4:
                            break
                
                if len(suggestions) >= 4:
                    break
            
            print(f"Found {len(suggestions)} alternative time suggestions")
            return suggestions
            
        except Exception as e:
            print(f"Error getting alternative suggestions: {str(e)}")
            return []


    def get_appointment_context(self):
        """Get current appointment data to provide context to AI"""
        try:
            context_parts = []
            
            if self.appointment.customer_name:
                context_parts.append(f"Customer Name: {self.appointment.customer_name}")
            else:
                context_parts.append("Customer Name: Not provided yet")
                
            if self.appointment.customer_area:
                context_parts.append(f"Area: {self.appointment.customer_area}")
            else:
                context_parts.append("Area: Not provided yet")
                
            if self.appointment.project_type:
                context_parts.append(f"Service Type: {self.appointment.project_type}")
            else:
                context_parts.append("Service Type: Not specified yet")
                
            if self.appointment.has_plan is True:
                context_parts.append("Plan Status: Customer has existing plan")
            elif self.appointment.has_plan is False:
                context_parts.append("Plan Status: Customer wants site visit")
            else:
                context_parts.append("Plan Status: Not specified yet")
                
            if self.appointment.property_type:
                context_parts.append(f"Property Type: {self.appointment.property_type}")
            else:
                context_parts.append("Property Type: Not specified yet")
                
            if self.appointment.timeline:
                context_parts.append(f"Timeline: {self.appointment.timeline}")
            else:
                context_parts.append("Timeline: Not specified yet")
                
            context_parts.append(f"Current Status: {self.appointment.get_status_display()}")
            
            # ✅ FIX: Check if scheduled_datetime exists before calling astimezone
            if self.appointment.scheduled_datetime:
                try:
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    sa_time = self.appointment.scheduled_datetime.astimezone(sa_timezone)
                    formatted_datetime = sa_time.strftime('%A, %B %d, %Y at %I:%M %p')
                    context_parts.append(f"Scheduled: {formatted_datetime}")
                    context_parts.append(f"⚠️ CRITICAL: When mentioning appointment time, ALWAYS use: {formatted_datetime}")
                except Exception as dt_error:
                    print(f"⚠️ Error formatting scheduled datetime: {dt_error}")
                    context_parts.append("Scheduled: Error reading datetime")
            else:
                context_parts.append("Scheduled: No appointment time set yet")
                
            next_question = self.get_next_question_to_ask()
            context_parts.append(f"Next Question Needed: {next_question}")
            
            retry_count = self._get_question_retry_count(next_question)
            context_parts.append(f"Question Retry Count: {retry_count}")
            
            completeness = self.appointment.get_customer_info_completeness()
            context_parts.append(f"Info Completeness: {completeness:.0f}%")

            return "\n".join(context_parts)
            
        except Exception as e:
            print(f"Error getting appointment context: {str(e)}")
            return "Unable to load appointment context"


    def verify_plan_question_not_asked_recently(self):
        """Check if we asked about plan in last 5 messages"""
        try:
            if not self.appointment.conversation_history:
                return False
            
            recent_messages = self.appointment.conversation_history[-5:]
            plan_keywords = ['have a plan', 'site visit', 'existing plan', 'Do you have']
            
            for msg in recent_messages:
                if msg.get('role') == 'assistant':
                    content = msg.get('content', '').lower()
                    if any(keyword.lower() in content for keyword in plan_keywords):
                        return True  # We asked recently
            
            return False  # Safe to ask
        except Exception as e:
            print(f"Error checking conversation history: {str(e)}")
            return False
    #
    def extract_all_available_info_with_ai(self, message):
        """
        Extract ALL possible appointment info from any message.
        Updated for new flow: service → description → datetime → area.
        """
        try:
            current_context  = self.get_appointment_context()
            next_question    = self.get_next_question_to_ask()
            current_time     = timezone.now().strftime('%Y-%m-%d %H:%M')
    
            extraction_prompt = f"""
    You are a data extraction assistant for a plumbing appointment system in Zimbabwe/South Africa.
    Customers may write in English, Shona, or a mix.
    
    CRITICAL: Return ONLY a valid JSON object — no markdown, no code blocks, no extra text.
    
    CURRENT APPOINTMENT STATE:
    {current_context}
    
    NEXT QUESTION WE NEED: {next_question}
    CUSTOMER MESSAGE: "{message}"
    
    EXTRACTION TARGETS:
    
    SERVICE TYPE — Look for any mention of the type of plumbing work needed.
    Return: "bathroom_renovation", "kitchen_renovation", or "new_plumbing_installation"
    
    PROJECT DESCRIPTION — Look for specific details of what the customer wants done,
    including renovation state clues like "already tiled", "new build", "existing bathroom",
    "from scratch", "walls done", "rough plumbing done".
    Capture verbatim where possible — these details affect pricing and the plumber's approach.
    Return: the description as a string (max 300 chars), or null.
    
    AREA/LOCATION — Any suburb, neighbourhood, or city mentioned as the work location.
    Return: the area name as stated, or null.
    
    AVAILABILITY / DATETIME — Look for any date + time combination.
    Return: YYYY-MM-DDTHH:MM or null.
    - "available all day" / "whole day" / "anytime" → return null (caller handles separately)
    - If only date given (no time) → return YYYY-MM-DDT00:00
    - If only time given (no date) → null
    TODAY = {current_time[:10]}
    
    CUSTOMER NAME — Only if the customer explicitly gives their name.
    Return: full name title-case, or null.
    
    RESPONSE FORMAT (CRITICAL — return EXACTLY this, nothing else):
    {{
        "service_type": "extracted_value_or_null",
        "project_description": "extracted_value_or_null",
        "area": "extracted_value_or_null",
        "availability": "extracted_value_or_null",
        "customer_name": "extracted_value_or_null"
    }}
    
    CURRENT DATE: {current_time}
    
    Extract from: "{message}"
    """
    
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a data extraction assistant. "
                            "Return ONLY valid JSON with no formatting or explanations."
                        )
                    },
                    {"role": "user", "content": extraction_prompt}
                ],
                temperature=0.1,
                max_tokens=200
            )
    
            ai_response = response.choices[0].message.content.strip()
            ai_response = ai_response.replace('```json', '').replace('```', '').strip()
    
            try:
                extracted_data = json.loads(ai_response)
                print(f"🤖 AI extracted data: {extracted_data}")
                return extracted_data
            except json.JSONDecodeError as e:
                print(f"❌ AI returned invalid JSON: {ai_response}")
                print(f"JSON Error: {str(e)}")
                return {}
    
        except Exception as e:
            print(f"❌ AI extraction error: {str(e)}")
            return {}




    def get_information_summary(self):
        """Get a summary of collected information for debugging"""
        try:
            summary = {
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
            return summary
        except Exception as e:
            print(f"Error getting info summary: {str(e)}")
            return {}
        


    def process_alternative_time_selection(self, message):
        """Use DeepSeek to detect and parse when customer selects an alternative time slot"""
        try:
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            now = timezone.now().astimezone(sa_timezone)

            # Build next-day lookup
            day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            next_days = {}
            for i, name in enumerate(day_names):
                days_ahead = (i - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                next_days[name] = (now + timedelta(days=days_ahead)).strftime('%B %d, %Y')

            prompt = f"""You are a datetime extraction assistant.

    The customer was shown a list of available appointment slots and is replying to choose one, 
    or suggesting a new time. Extract the date and time they want.

    CURRENT DATETIME: {now.strftime('%Y-%m-%d %H:%M')} (Africa/Johannesburg)
    WORKING DAYS: Sunday–Friday (Saturday CLOSED)

    NEXT OCCURRENCE OF EACH DAY:
    - Monday: {next_days['monday']}
    - Tuesday: {next_days['tuesday']}
    - Wednesday: {next_days['wednesday']}
    - Thursday: {next_days['thursday']}
    - Friday: {next_days['friday']}
    - Saturday: {next_days['saturday']} ← CLOSED, do not use
    - Sunday: {next_days['sunday']}
    - Tomorrow: {(now + timedelta(days=1)).strftime('%B %d, %Y')}

    CUSTOMER MESSAGE: "{message}"

    Return ONLY one of:
    - YYYY-MM-DDTHH:MM  (if both date and time are clear)
    - SATURDAY_CLOSED   (if they picked Saturday)
    - NOT_FOUND         (if no clear selection)

    No other text."""

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "Return only a datetime string YYYY-MM-DDTHH:MM, SATURDAY_CLOSED, or NOT_FOUND."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=25
            )

            ai_response = response.choices[0].message.content.strip()
            print(f"🤖 DeepSeek alternative selection: '{message}' → {ai_response}")

            if ai_response in ("SATURDAY_CLOSED", "NOT_FOUND"):
                msg = (message or '').strip().lower()

                if 'tomorrow' in msg:
                    candidate = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    if candidate.weekday() == 5:
                        return None
                    print(f"✅ Manual day selection captured from 'tomorrow': {candidate}")
                    return candidate

                for i, name in enumerate(day_names):
                    if name in msg:
                        if name == 'saturday':
                            return None
                        days_ahead = (i - now.weekday()) % 7
                        if days_ahead == 0:
                            days_ahead = 7
                        candidate = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
                        print(f"✅ Manual day selection captured from '{name}': {candidate}")
                        return candidate

                return None

            parsed_dt = datetime.strptime(ai_response, '%Y-%m-%dT%H:%M')
            localized_dt = sa_timezone.localize(parsed_dt)
            print(f"✅ Parsed alternative selection: {localized_dt}")
            return localized_dt

        except Exception as e:
            print(f"❌ DeepSeek alternative selection error: {e}")
            return None


    def book_appointment_with_selected_time(self, selected_datetime):
        """Book appointment with a customer-selected alternative time.

        Delegates entirely to book_appointment() which handles status update,
        confirmation message, team alert, and calendar — no duplicate sends.
        """
        try:
            print(f"🔄 Booking appointment with selected time: {selected_datetime}")

            is_available, conflict_info = self.check_appointment_availability(selected_datetime)

            if is_available:
                self.appointment.scheduled_datetime = selected_datetime
                self.appointment.save(update_fields=['scheduled_datetime'])

                # book_appointment() handles everything from here
                result = self.book_appointment(message=None)

                if result['success']:
                    print(f"✅ Appointment booked via selected time: {selected_datetime}")
                    return result

                alternatives = self.get_alternative_time_suggestions(selected_datetime)
                return {'success': False, 'error': 'Time became unavailable', 'alternatives': alternatives}

            else:
                print(f"❌ Selected time not available: {conflict_info}")
                alternatives = self.get_alternative_time_suggestions(selected_datetime)
                return {'success': False, 'error': 'Selected time not available', 'alternatives': alternatives}

        except Exception as e:
            print(f"❌ Error booking with selected time: {str(e)}")
            return {'success': False, 'error': str(e)}


    def extract_appointment_details(self):
        """Extract customer details from appointment data"""
        try:
            details = {}
            
            # Use existing appointment data
            if self.appointment.customer_name:
                details['name'] = self.appointment.customer_name
            if self.appointment.customer_area:
                details['area'] = self.appointment.customer_area
            if self.appointment.project_type:
                details['project_type'] = self.appointment.project_type
            if self.appointment.property_type:
                details['property_type'] = self.appointment.property_type
            if self.appointment.timeline:
                details['timeline'] = self.appointment.timeline
            if self.appointment.has_plan is not None:
                details['has_plan'] = self.appointment.has_plan

            return details
            
        except Exception as e:
            print(f"Error extracting appointment details: {str(e)}")
            return {}





    def extract_all_available_info_with_ai(self, message):
        """Extract ALL possible appointment information from any message - FIXED TO PREVENT RE-ASKING"""
        try:
            # Get current appointment state for context
            current_context = self.get_appointment_context()
            next_question = self.get_next_question_to_ask()
            
            # Format current time properly
            current_time = timezone.now().strftime('%Y-%m-%d %H:%M')
            
            extraction_prompt = f"""
            You are a comprehensive data extraction assistant for a plumbing appointment system.
            Customers may write in English, Shona, or mixed language.
            
            CRITICAL: You MUST return ONLY a valid JSON object with no markdown formatting, code blocks, or extra text.
            
            TASK: Extract information from the customer's message and return ONLY what you can clearly identify.
            
            CURRENT APPOINTMENT STATE:
            {current_context}
            
            NEXT QUESTION WE NEED: {next_question}
            
            CUSTOMER MESSAGE: "{message}"
            
            EXTRACTION RULES:
            1. ONLY extract information that is CLEARLY and EXPLICITLY present in the message
            2. DO NOT GUESS or ASSUME - if not explicitly stated, return null
            3. PRESERVE existing information - do NOT set fields to null if they already have values
            4. Return ONLY a JSON object - no markdown, no explanations, no code blocks
            5. For plan_status: ONLY extract if we are ACTIVELY ASKING about the plan RIGHT NOW
            
            EXTRACTION TARGETS:
            
            SERVICE TYPE - Look for:
            - English keywords: bathroom, kitchen, plumbing, installation, renovation, repair, toilet, shower, sink
            - Shona/mixed keywords: chimbuzi (toilet), shawa (shower), bhavhu/bhavu (bathtub),
              bheseni (basin/sink), kicheni (kitchen), mapombi (pipes), imba itsva (new house)
            - Return: "bathroom_renovation", "kitchen_renovation", or "new_plumbing_installation"
            
            PROJECT DESCRIPTION - Look for specific details of what the customer wants done,
            including renovation state clues like "already tiled", "new build", "existing bathroom",
            "from scratch", "walls done", "rough plumbing done".
            Capture verbatim where possible because these details affect pricing and the plumber's approach.
            - Return: the description as a string (max 300 chars), or null
            
            
            PLAN STATUS - ULTRA CRITICAL - STRICT EXTRACTION RULES:
            
            WHEN TO EXTRACT:
            - ONLY if next_question = "plan_or_visit" (we are actively asking about plan)
            - ONLY if customer is DIRECTLY answering the plan question in THIS message
            - NEVER extract from general conversation, greetings, or other topics
            
            CURRENT QUESTION CHECK: {next_question}
            
            IF next_question IS NOT "plan_or_visit":
            - ALWAYS return null for plan_status
            - Do NOT try to infer plan status from any message
            - This prevents re-asking questions already answered
            
            IF next_question IS "plan_or_visit":
            YES indicators (customer HAS plan):
            - Direct: "yes", "yeah", "yep", "i do", "i have", "got plan", "have plan"
            - Future: "will send", "i'll send", "send later", "let me send"
            - Shona/mixed: "hongu", "ehe", "ndine plan", "ndinayo plan", "tine plan",
              "ndine blueprint", "ndinayo blueprint", "ndine mapepa"
            
            NO indicators (customer needs site visit):
            - Direct: "no", "nope", "don't have", "no plan", "need visit", "site visit"
            - Shona/mixed: "kwete", "handina plan", "hapana plan", "sina plan",
              "mauye muone", "uyai muone", "tiuye muone", "shanyira"
            
            IF IN DOUBT: Return null (better to ask again than assume wrong answer)
            
            AREA/LOCATION - Look for:
            - Any location names, suburbs, areas mentioned
            - Return: the area name as stated
            
            TIMELINE - Look for:
            - When they want work done: ASAP, next week, next month, tomorrow, etc.
            - Return: timeline as stated
            
            PROPERTY TYPE - Look for:
            - English keywords: house, home, apartment, flat, business, office, commercial, shop, store
            - Shona/mixed keywords: imba (house/home), bhizimisi (business), shopu (shop)
            - Return: "house", "apartment", or "business"
            
            AVAILABILITY/DATETIME - Look for:
            - Complete date and time information
            - Handle: "Monday at 2pm", "tomorrow at 10am", "15th July at 14:00"
            - Return: YYYY-MM-DDTHH:MM format
            
            CUSTOMER NAME - Look for:
            - Patterns: "I'm John", "my name is Sarah", "call me Mike", just a name by itself
            - Return: full name in title case
            
            RESPONSE FORMAT (CRITICAL):
            Return EXACTLY this JSON structure with no additional text:
            {{
                "service_type": "extracted_value_or_null",
                "project_description": "extracted_value_or_null",
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
                    {"role": "system", "content": "You are a data extraction assistant. Return ONLY valid JSON with no formatting or explanations. NEVER extract plan_status unless actively asking about it RIGHT NOW."},
                    {"role": "user", "content": extraction_prompt}
                ],
                temperature=0.1,
                max_tokens=200
            )
            
            ai_response = response.choices[0].message.content.strip()
            
            # Clean up the response to handle markdown formatting
            ai_response = ai_response.replace('```json', '').replace('```', '').strip()
            
            # Parse AI response as JSON
            try:
                extracted_data = json.loads(ai_response)
                print(f"🤖 AI extracted data: {extracted_data}")
                # Shona fallback: plan_or_visit responses (only when actively asking)
                if next_question == "plan_or_visit" and not extracted_data.get("plan_status"):
                    msg = (message or "").lower().strip()
                    has_plan_terms = [
                        "hongu", "ehe", "ndine plan", "ndinayo plan", "tine plan",
                        "ndine blueprint", "ndinayo blueprint", "ndine mapepa",
                        "ndine drawing", "ndinayo drawing", "ndine maplani",
                    ]
                    needs_visit_terms = [
                        "kwete", "handina plan", "hapana plan", "sina plan",
                        "mauye muone", "uyai muone", "tiuye muone", "shanyira",
                        "site visit", "come see", "come and see",
                    ]
                    if any(term in msg for term in has_plan_terms):
                        extracted_data["plan_status"] = "has_plan"
                        print("✅ Shona fallback: detected HAS_PLAN")
                    elif any(term in msg for term in needs_visit_terms):
                        extracted_data["plan_status"] = "needs_visit"
                        print("✅ Shona fallback: detected NEEDS_VISIT")
                
                # ADDITIONAL SAFETY CHECK: Never extract plan_status if we already have it
                if self.appointment.has_plan is not None and extracted_data.get('plan_status'):
                    print(f"⚠️ BLOCKED: Attempted to re-extract plan_status when already set to {self.appointment.has_plan}")
                    extracted_data['plan_status'] = None  # Force to null
                
                # Debug log for plan status specifically
                if extracted_data.get('plan_status'):
                    print(f"✅ PLAN STATUS DETECTED: {extracted_data['plan_status']}")
                
                return extracted_data
            except json.JSONDecodeError as e:
                print(f"❌ AI returned invalid JSON: {ai_response}")
                print(f"❌ JSON Parse Error: {str(e)}")
                return {}
                
        except Exception as e:
            print(f"❌ AI extraction error: {str(e)}")
            return {}


    def get_next_question_to_ask(self):
        """
        5-question booking flow:
        1. service_type          → which service?
        2. project_description   → what exactly needs doing?
        3. availability_date     → which day?
        4. availability_time     → which time slot?
        5. area                  → which suburb?

        After all 5 are collected the appointment is booked immediately.
        The only follow-up question is the customer's name, asked once
        after the booking confirmation is sent.
        """
        if not self.appointment.project_type:
            return "service_type"

        if not self.appointment.project_description:
            return "project_description"

        if not self.appointment.scheduled_datetime:
            return "availability_date"

        if not self._time_confirmed():
            return "availability_time"

        if not self.appointment.customer_area:
            return "area"

        if (
            not self.appointment.customer_name
            and self.appointment.status == "confirmed"
            and not self._customer_name_declined()
        ):
            return "name"

        return "complete"



    def smart_booking_check(self):
        """
        Ready to book when all 5 required fields are present.
        has_plan is NOT required — it is no longer part of the booking flow.
        """
        has_service  = bool(self.appointment.project_type)
        has_desc     = bool(self.appointment.project_description)
        has_datetime = (
            bool(self.appointment.scheduled_datetime) and self._time_confirmed()
        )
        has_area     = bool(self.appointment.customer_area)

        has_all = has_service and has_desc and has_datetime and has_area

        missing = []
        if not has_service:
            missing.append("service type")
        if not has_desc:
            missing.append("project description")
        if not has_datetime:
            missing.append("availability")
        if not has_area:
            missing.append("area")

        return {
            'ready_to_book':         has_all,
            'missing_fields':        missing,
            'completion_percentage': ((4 - len(missing)) / 4) * 100,
        }



    def update_appointment_with_extracted_data(self, extracted_data, incoming_message=None):
        """
        Update appointment with AI-extracted data.
        New flow: service → project_description → datetime → area.
        """
        try:
            updated_fields = []
            next_question  = self.get_next_question_to_ask()
    
            print(f"🔄 Updating appointment — current question: {next_question}")
            print(f"📦 Extracted data: {extracted_data}")
    
            # ── Service type ──────────────────────────────────────────────────────
            if (extracted_data.get('service_type') and
                    extracted_data.get('service_type') != 'null' and
                    not self.appointment.project_type):
                self.appointment.project_type = extracted_data['service_type']
                updated_fields.append('service_type')
                print(f"✅ Updated service_type: {self.appointment.project_type}")
    
            # ── Project description ───────────────────────────────────────────────
            _extracted_desc = (extracted_data.get('project_description') or '').strip()
            if (
                _extracted_desc and
                _extracted_desc != 'null' and
                not self.appointment.project_description and
                not self._is_product_availability_question(incoming_message) and
                _extracted_desc.lower() not in _SERVICE_TYPE_LABELS
            ):
                self.appointment.project_description = _extracted_desc
                updated_fields.append('project_description')
                print(f"✅ Updated project_description: {self.appointment.project_description[:60]}")
            #        
            _SERVICE_TYPE_LABELS = {
                'bathroom renovation', 'bathroom',
                'kitchen renovation', 'kitchen',
                'new plumbing installation', 'plumbing installation',
                'bathroom_renovation', 'kitchen_renovation', 'new_plumbing_installation',
            }

            elif (
                next_question == 'project_description' and
                not self.appointment.project_description and
                self._looks_like_project_description_reply(incoming_message) and
                not self._is_product_availability_question(incoming_message) and
                incoming_message.strip().lower() not in _SERVICE_TYPE_LABELS
            ):
                self.appointment.project_description = incoming_message.strip()
                updated_fields.append('project_description')                
                print(f"✅ Fallback project_description from raw message: "
                    f"{self.appointment.project_description[:60]}")

            # ── Area — capture passively whenever volunteered ─────────────────────
            if (extracted_data.get('area') and
                    extracted_data.get('area') != 'null' and
                    not self.appointment.customer_area):
                self.appointment.customer_area = extracted_data['area']
                updated_fields.append('area')
                print(f"✅ Updated area: {self.appointment.customer_area}")
    
            # ── Availability / DateTime ───────────────────────────────────────────
            if (extracted_data.get('availability') and
                    extracted_data.get('availability') != 'null'):
                try:
                    parsed_dt = datetime.strptime(extracted_data['availability'], '%Y-%m-%dT%H:%M')
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    localized_dt = sa_timezone.localize(parsed_dt)
    
                    old_dt = self.appointment.scheduled_datetime
                    self.appointment.scheduled_datetime = localized_dt
                    updated_fields.append('availability')
                    print(f"📅 Updated datetime: {old_dt} -> {localized_dt}")
    
                    # If time is non-midnight, mark it confirmed
                    if localized_dt.hour != 0 or localized_dt.minute != 0:
                        self._mark_time_confirmed()
    
                except ValueError as e:
                    print(f"❌ Failed to parse AI datetime: {extracted_data['availability']} — {e}")
    
            elif (
                next_question == 'availability_date' and
                not self.appointment.scheduled_datetime and
                not extracted_data.get('availability')
            ):
                # Try to parse a day name selection using the existing helper
                parsed = self.process_alternative_time_selection(incoming_message)
                if parsed:
                    # Store date only (midnight) — time confirmed separately
                    self.appointment.scheduled_datetime = parsed.replace(hour=0, minute=0, second=0)
                    updated_fields.append('availability')
                    self.appointment.save(update_fields=['scheduled_datetime'])
                    print(f"✅ Day selection captured: {self._get_selected_local_date()}")

            # ── Customer name ─────────────────────────────────────────────────────
            elif (
                next_question == 'availability_time' and
                self.appointment.scheduled_datetime and
                not extracted_data.get('availability')
            ):
                parsed_time_only = self._parse_time_only_for_selected_date(incoming_message)
                if parsed_time_only:
                    old_dt = self.appointment.scheduled_datetime
                    self.appointment.scheduled_datetime = parsed_time_only
                    self._mark_time_confirmed()
                    updated_fields.append('availability')
                    print(f"âœ… Time selection captured: {old_dt} -> {self.appointment.scheduled_datetime}")

            if (extracted_data.get('customer_name') and
                    extracted_data.get('customer_name') != 'null' and
                    not self.appointment.customer_name):
                if self.is_valid_name(extracted_data['customer_name']):
                    self.appointment.customer_name = extracted_data['customer_name']
                    self._clear_customer_name_declined()
                    updated_fields.append('customer_name')
                    print(f"✅ Updated customer_name: {self.appointment.customer_name}")
    
            if updated_fields:
                update_field_map = {
                    'service_type': 'project_type',
                    'project_description': 'project_description',
                    'area': 'customer_area',
                    'availability': 'scheduled_datetime',
                    'customer_name': 'customer_name',
                }
                db_update_fields = [
                    update_field_map[field]
                    for field in updated_fields
                    if field in update_field_map and self._appointment_has_field(update_field_map[field])
                ]
                if db_update_fields:
                    self.appointment.save(update_fields=db_update_fields)
                refresh_lead_score(self.appointment)

                # Reset retry count for every question that was just answered
                # so the NEXT unanswered question starts fresh at 0
                question_to_field = {
                    'service_type': 'service_type',
                    'project_description': 'project_description',
                    'area': 'area',
                    'availability_date': 'availability',
                    'availability_time': 'availability',
                    'customer_name': 'customer_name',
                }
                for question_key, field_key in question_to_field.items():
                    if field_key in updated_fields:
                        self._set_question_retry_count(question_key, 0)
                        print(f"🔄 Reset retry count for question: {question_key}")
                print(f"💾 Saved appointment with updated fields: {updated_fields}")
            else:
                print("ℹ️ No fields were updated")
    
            return updated_fields
    
        except Exception as e:
            print(f"❌ Error updating appointment: {str(e)}")
            import traceback
            traceback.print_exc()
            return []



    def check_appointment_availability(self, requested_datetime):
        """Check if requested time slot is available"""
        try:
            # Ensure timezone awareness
            if requested_datetime.tzinfo is None:
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                requested_datetime = sa_timezone.localize(requested_datetime)
            
            # Define appointment duration (default 2 hours)
            appointment_duration = timedelta(hours=2)
            requested_end = requested_datetime + appointment_duration
            
            print(f"Checking availability for: {requested_datetime} to {requested_end}")
            
            # 1. Check if it's not in the past (with 1-hour buffer)
            now = timezone.now()
            min_booking_time = now + timedelta(hours=1)
            
            if requested_datetime <= min_booking_time:
                print(f"Requested time is too soon: {requested_datetime} vs minimum {min_booking_time}")
                return False, "too_soon"
            
            # 2. Check business days (Monday-Friday)
            # Check business days (Sunday-Friday, Saturday closed)
            weekday = requested_datetime.weekday()  # 0=Monday, 6=Sunday
            if weekday == 5:  # Only Saturday (5) is closed
                print(f"Requested time is on Saturday (closed): weekday {weekday}")
                # ✅ Clear the invalid datetime so it doesn't loop on every message
                self.appointment.scheduled_datetime = None
                if self._appointment_has_field('retry_count'):
                    self.appointment.save(update_fields=['retry_count'])
                return False, "saturday_closed"

            # 3. Check business hours (8 AM - 6 PM)
            hour = requested_datetime.hour
            if hour < 8 or hour >= 18:
                print(f"Outside business hours: {hour}:00 (business hours: 8 AM - 6 PM)")
                return False, "outside_business_hours"
            
            # 4. Check if appointment would end after business hours
            if requested_end.hour > 18 or (requested_end.hour == 18 and requested_end.minute > 0):
                print(f"Appointment would end after business hours: {requested_end}")
                return False, "ends_after_hours"
            
            # 5. Check for conflicts with other confirmed appointments
            conflicting_appointments = Appointment.objects.filter(
                status='confirmed',
                scheduled_datetime__isnull=False
            ).exclude(
                id=self.appointment.id  # Exclude current appointment for reschedules
            )
            
            for existing_appt in conflicting_appointments:
                # Ensure existing appointment is timezone-aware
                if existing_appt.scheduled_datetime.tzinfo is None:
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    existing_start = sa_timezone.localize(existing_appt.scheduled_datetime)
                else:
                    existing_start = existing_appt.scheduled_datetime
                    
                existing_end = existing_start + appointment_duration
                
                # Check for time overlap
                if (requested_datetime < existing_end and requested_end > existing_start):
                    print(f"Conflict found with appointment {existing_appt.id}")
                    print(f"Existing: {existing_start} to {existing_end}")
                    print(f"Requested: {requested_datetime} to {requested_end}")
                    return False, existing_appt
            
            # 6. Check maximum advance booking (3 months)
            max_advance_time = now + timedelta(days=90)
            if requested_datetime > max_advance_time:
                print(f"Too far in advance: {requested_datetime} vs maximum {max_advance_time}")
                return False, "too_far_ahead"
            
            print(f"✅ Time slot is available: {requested_datetime}")
            return True, None
            
        except Exception as e:
            print(f"❌ Error checking availability: {str(e)}")
            return False, "error"


    def get_alternative_time_suggestions(self, requested_datetime):
        """Get alternative available time slots near the requested time"""
        try:
            suggestions = []
            
            # Get the requested date and time
            requested_date = requested_datetime.date()
            
            # Business time slots (8am, 10am, 12pm, 2pm, 4pm)
            business_time_slots = [8, 10, 12, 14, 16]
            
            print(f"Looking for alternatives near {requested_datetime}")
            
            # Try same day first, then next few business days
            for day_offset in range(0, 5):  # Check today + next 4 days
                check_date = requested_date + timedelta(days=day_offset)
                
                # This one is actually correct already — but double-check the one
                # inside find_next_available_slots which has:
                if check_date.weekday() == 5:   # ← Skip Saturday only
                    continue
                    
                for hour in business_time_slots:
                    candidate_time = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    candidate_datetime = sa_timezone.localize(candidate_time)
                    
                    # Skip times in the past
                    if candidate_datetime <= timezone.now():
                        continue
                    
                    # Skip the exact requested time
                    if candidate_datetime == requested_datetime:
                        continue
                    
                    is_available, conflict = self.check_appointment_availability(candidate_datetime)
                    if is_available:
                        day_type = 'same_day' if day_offset == 0 else 'next_days'
                        suggestions.append({
                            'datetime': candidate_datetime,
                            'display': candidate_datetime.strftime('%A, %B %d at %I:%M %p'),
                            'day_type': day_type
                        })
                        
                        # Limit to 4 suggestions
                        if len(suggestions) >= 4:
                            break
                
                if len(suggestions) >= 4:
                    break
            
            print(f"Found {len(suggestions)} alternative time suggestions")
            return suggestions
            
        except Exception as e:
            print(f"Error getting alternative suggestions: {str(e)}")
            return []

    def format_datetime_for_display(self, dt):
        """Format datetime ensuring it shows in South Africa timezone"""
        try:
            import pytz
            
            # Ensure datetime is timezone-aware
            if dt.tzinfo is None:
                # If naive, assume it's already in SA time
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                dt = sa_timezone.localize(dt)
            else:
                # If aware, convert to SA timezone
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                dt = dt.astimezone(sa_timezone)
            
            return dt
            
        except Exception as e:
            print(f"Error formatting datetime: {str(e)}")
            return dt

    def _build_named_booking_confirmation(self):
        """Build the final customer-facing confirmation after capturing a name."""
        display_datetime = self.format_datetime_for_display(self.appointment.scheduled_datetime)
        customer_name = self.appointment.customer_name or "there"
        customer_area = self.appointment.customer_area or "your area"
        formatted_datetime = display_datetime.strftime('%A, %B %d, %Y at %I:%M %p')

        return (
            f"Perfect — thanks, {customer_name}. You're all set for your "
            f"*free on-site assessment* on **{formatted_datetime}** in {customer_area}. "
            "Our senior plumber will call you 30 minutes before arrival to confirm. "
            "See you then!"
        )


    def send_confirmation_message(self, appointment_info, appointment_datetime):
        """Send booking confirmation to customer."""
        try:
            display_datetime = self.format_datetime_for_display(appointment_datetime)

            service_map = {
                'bathroom_renovation':        'Bathroom Renovation',
                'new_plumbing_installation':  'New Plumbing Installation',
                'kitchen_renovation':         'Kitchen Renovation',
            }
            service_name = service_map.get(
                appointment_info.get('project_type', ''),
                (appointment_info.get('project_type') or 'Plumbing service')
                .replace('_', ' ').title()
            )

            confirmation_message = (
                f"✅ APPOINTMENT CONFIRMED\n\n"
                f"📅 Date: {display_datetime.strftime('%A, %B %d, %Y')}\n"
                f"🕐 Time: {display_datetime.strftime('%I:%M %p')}\n"
                f"📍 Area: {appointment_info.get('area', 'Your area')}\n"
                f"🔧 Service: {service_name}\n\n"
                f"Our plumber will contact you before arrival.\n\n"
                f"Questions? Just reply here.\n"
                f"— Homebase Plumbers"
            )

            clean_phone = clean_phone_number(self.phone_number)
            whatsapp_api.send_text_message(clean_phone, confirmation_message)
            print(f"✅ Confirmation sent to {clean_phone}")

        except Exception as e:
            print(f"❌ Confirmation message error: {str(e)}")


    # ALSO UPDATE YOUR notify_team METHOD:

    def notify_team(self, appointment_info, appointment_datetime):
            """Notify team about new appointment booking via WhatsApp."""
            try:
                import os

                # Format datetime for display
                display_datetime = self.format_datetime_for_display(appointment_datetime)

                service_name = appointment_info.get('project_type', 'Plumbing service')
                if service_name:
                    service_map = {
                        'bathroom_renovation': 'Bathroom Renovation',
                        'new_plumbing_installation': 'New Plumbing Installation',
                        'kitchen_renovation': 'Kitchen Renovation'
                    }
                    service_name = service_map.get(service_name, service_name.replace('_', ' ').title())

                plan_status = "Not specified"
                if appointment_info.get('has_plan') is not None:
                    plan_status = "Has existing plan" if appointment_info['has_plan'] else "Needs site visit"

                # AI conversation summary
                from bot.whatsapp_webhook import generate_conversation_summary
                ai_summary = generate_conversation_summary(self.appointment)

                customer_phone = (
                    self.phone_number
                    .replace('whatsapp:+', '')
                    .replace('whatsapp:', '')
                    .replace('+', '')
                )

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

                # Build recipient list from env var → appointment field → hardcoded fallback
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
                    team_numbers = ['263774819901']
                    print("⚠️ TEAM_NUMBERS env var not set — using hardcoded fallback")

                print(f"📤 Sending booking notifications to {len(team_numbers)} team member(s)...")

                sent_count = 0
                for number in team_numbers:
                    try:
                        whatsapp_api.send_text_message(number, team_message)
                        print(f"✅ Booking notification sent to {number}")
                        sent_count += 1
                    except Exception as msg_error:
                        print(f"❌ Failed to send to {number}: {msg_error}")

                if sent_count == 0:
                    print("❌ No booking notifications sent — check TEAM_NUMBERS env var and WhatsApp API config")

            except Exception as e:
                print(f"❌ Team notification error: {str(e)}")
                import traceback
                traceback.print_exc()

                
    def add_to_google_calendar(self, appointment_info, appointment_datetime):
        """Add appointment to Google Calendar"""
        try:
            # Skip if no credentials configured
            if not GOOGLE_CALENDAR_CREDENTIALS:
                print("⚠️ Google Calendar credentials not configured")
                return None
                
            # Initialize Google Calendar service
            credentials = service_account.Credentials.from_service_account_info(
                GOOGLE_CALENDAR_CREDENTIALS,
                scopes=['https://www.googleapis.com/auth/calendar']
            )
            service = build('calendar', 'v3', credentials=credentials)
            
            # Create event description
            description_parts = []
            if appointment_info.get('project_type'):
                description_parts.append(f"Service: {appointment_info['project_type']}")
            if appointment_info.get('area'):
                description_parts.append(f"Area: {appointment_info['area']}")
            if appointment_info.get('property_type'):
                description_parts.append(f"Property: {appointment_info['property_type']}")
            if appointment_info.get('timeline'):
                description_parts.append(f"Timeline: {appointment_info['timeline']}")
            if appointment_info.get('has_plan') is not None:
                plan_status = "Has existing plan" if appointment_info['has_plan'] else "Needs site visit"
                description_parts.append(f"Plan Status: {plan_status}")
                
            description_parts.append(f"Phone: {self.phone_number}")
            
            # Create event
            event = {
                'summary': f"Plumbing Appointment - {appointment_info.get('name', 'Customer')}",
                'description': "\n".join(description_parts),
                'start': {
                    'dateTime': appointment_datetime.isoformat(),
                    'timeZone': 'Africa/Johannesburg',
                },
                'end': {
                    'dateTime': (appointment_datetime + timedelta(hours=2)).isoformat(),
                    'timeZone': 'Africa/Johannesburg',
                },
                'attendees': [
                    {'email': 'team@plumbingcompany.com'},
                ],
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'email', 'minutes': 24 * 60},
                        {'method': 'popup', 'minutes': 30},
                    ],
                },
            }
            
            # Insert event
            event_result = service.events().insert(
                calendarId='primary',
                body=event
            ).execute()
            
            print(f"✅ Added to Google Calendar")
            return event_result
            
        except Exception as e:
            print(f"❌ Google Calendar Error: {str(e)}")
            return None


    def book_appointment(self, message):
        """Book an appointment using the stored datetime - FIXED TIMEZONE"""
        try:
            print(f"🔄 Starting appointment booking process...")
            
            # Use the stored datetime from AI extraction
            appointment_datetime = self.appointment.scheduled_datetime
            
            if not appointment_datetime:
                print("❌ No datetime available - booking cancelled")
                return {'success': False, 'error': 'No appointment time set'}

            print(f"📅 Using appointment time: {appointment_datetime}")

            # Ensure proper timezone handling
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            if appointment_datetime.tzinfo is None:
                appointment_datetime = sa_timezone.localize(appointment_datetime)
            else:
                appointment_datetime = appointment_datetime.astimezone(sa_timezone)

            print(f"📅 Timezone-corrected appointment time: {appointment_datetime}")

            # Check availability
            is_available, conflict_info = self.check_appointment_availability(appointment_datetime)
            
            if not is_available:
                print(f"❌ Time slot not available: {conflict_info}")
                alternatives = self.get_alternative_time_suggestions(appointment_datetime)
                
                return {
                    'success': False, 
                    'error': 'Time not available', 
                    'alternatives': alternatives
                }
            
            # SUCCESS PATH: Update appointment
            self.appointment.status = 'confirmed'
            self.appointment.scheduled_datetime = appointment_datetime
            self.appointment.save()
            
            print(f"💾 Appointment confirmed and saved: {appointment_datetime}")
            
            # Extract appointment details
            appointment_details = self.extract_appointment_details()
            
            # Send notifications
            try:
                print("📤 Sending notifications...")
                self.send_confirmation_message(appointment_details, appointment_datetime)
                self.notify_team(appointment_details, appointment_datetime)
                print("✅ Notifications sent")
            except Exception as notify_error:
                print(f"⚠️ Notification error: {notify_error}")
            
            # Add to calendar (optional)
            try:
                if GOOGLE_CALENDAR_CREDENTIALS:
                    self.add_to_google_calendar(appointment_details, appointment_datetime)
            except Exception as cal_error:
                print(f"⚠️ Calendar error: {cal_error}")
            
            # FIX: Format datetime for display
            display_datetime = self.format_datetime_for_display(appointment_datetime)
            
            return {
                'success': True,
                'datetime': display_datetime.strftime('%B %d, %Y at %I:%M %p')
            }

        except Exception as e:
            print(f"❌ Booking Error: {str(e)}")
            import traceback
            traceback.print_exc()
            return {'success': False, 'error': str(e)}


    def detect_reschedule_request_with_ai(self, message):
        """Use AI to intelligently detect rescheduling requests"""
        try:
            # Only check for reschedule if appointment is already confirmed
            if self.appointment.status != 'confirmed' or not self.appointment.scheduled_datetime:
                return False
                
            current_appt = self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')
            
            detection_prompt = f"""
            You are a rescheduling detection assistant for an appointment system.
            
            TASK: Determine if the customer's message is requesting to reschedule their existing appointment.
            
            CONTEXT:
            - Customer has a CONFIRMED appointment: {current_appt}
            - Customer message: "{message}"
            - Phone: {self.phone_number}
            
            DETECTION CRITERIA:
            Look for ANY indication the customer wants to:
            - Change their appointment time/date
            - Move their appointment to a different slot
            - Cancel and rebook for a different time
            - Express they can't make their current appointment
            - Request a different day or time
            
            EXAMPLES OF RESCHEDULE REQUESTS:
            - "Can we reschedule to Monday?"
            - "I need to change my appointment"
            - "Something came up, can we move it?"
            - "Can't make it tomorrow, how about Friday?"
            - "I'm busy that day, any other time?"
            - "Emergency came up"
            - "Can we do it earlier/later?"
            - "Different day would be better"
            - "Monday at 2pm instead?"
            
            RESPONSE FORMAT:
            Reply with ONLY:
            - "YES" if this is clearly a reschedule request
            - "NO" if this is not a reschedule request
            - "MAYBE" if it's ambiguous but could be a reschedule request
            
            CUSTOMER MESSAGE: "{message}"
            """
            
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a precise detection assistant. Follow instructions exactly and respond with only YES, NO, or MAYBE."},
                    {"role": "user", "content": detection_prompt}
                ],
                temperature=0.1,
                max_tokens=10
            )
            
            ai_response = response.choices[0].message.content.strip().upper()
            
            if ai_response in ["YES", "MAYBE"]:
                print(f"🤖 AI detected reschedule request: {ai_response}")
                return True
            elif ai_response == "NO":
                print(f"🤖 AI determined not a reschedule request: {ai_response}")
                return False
            else:
                print(f"🤖 AI gave unexpected response: {ai_response}, defaulting to False")
                return False
                
        except Exception as e:
            print(f"❌ AI reschedule detection error: {str(e)}")
            return False


    def _generate_contextual_response_legacy(self, incoming_message, next_question, updated_fields):
        """
        Generate the next bot message.
        retry_count == 0  → exact hardcoded wording.
        retry_count > 0   → AI rephrases with escalation psychology.
        """
        try:
            import pytz as _pytz
    
            retry_count = self._get_question_retry_count(next_question)
            sa_tz       = _pytz.timezone('Africa/Johannesburg')
    
            # ── Saturday guard ────────────────────────────────────────────────────
            saturday_indicators = ['saturday', 'sat']
            if any(s in incoming_message.lower() for s in saturday_indicators):
                alternatives = self.get_alternative_time_suggestions(
                    timezone.now() + timedelta(days=1)
                )
                alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives]) if alternatives else ""
                reply = "We unfortunately don't operate on Saturdays. 😊\n\nOur working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                if alt_text:
                    reply += f"Here are some available slots:\n{alt_text}\n\nOr feel free to suggest a different date and time!"
                else:
                    reply += "Could you please choose a different day that works for you?"
                return reply
    
            # ── "Available all day" guard ─────────────────────────────────────────
            all_day_phrases = [
                'available all day', 'whole day', 'all day', 'anytime',
                'any time', 'free all day', 'i am free', 'im free',
            ]
            if (next_question in ('availability_time', 'area', 'complete') and
                    self.appointment.scheduled_datetime and
                    any(p in incoming_message.lower() for p in all_day_phrases)):
                return self._handle_all_day_response()
            #
            if next_question == "name":
                # Name was just saved in this turn — send the final confirmation
                if self.appointment.customer_name and 'customer_name' in (updated_fields or []):
                    return self._build_named_booking_confirmation()
                # Name not yet provided — ask for it
                return (
                    "One last thing — what name should we put on the booking? "
                    "If you'd rather not share it, just say no."
                )
            
            # ── First-pass: exact hardcoded questions (retry_count == 0) ─────────
    
            if retry_count == 0:

                if next_question == "service_type":
                    return (
                        "Hello! Happy to help. Which service are you interested in?\n\n"
                        "We offer:\n"
                        "• Bathroom Renovation\n"
                        "• New Plumbing Installation\n"
                        "• Kitchen Renovation"
                    )

                if next_question == "project_description":
                    return (
                        "Got it! What exactly do you want done? "
                        "The more detail you give, the more accurate we can be with "
                        "the quote."
                    )

                if next_question == "availability_date":
                    days       = self._get_next_two_available_days()
                    day_a      = self._format_day(days[0]) if len(days) > 0 else "tomorrow"
                    day_b      = self._format_day(days[1]) if len(days) > 1 else "the day after"
                    visit_desc = self._describe_project_context()
                    return (
                        f"Great, what works better for you — {day_a} or {day_b} — "
                        f"for us to come through and {visit_desc}?"
                    )

                if next_question == "availability_time":
                    dt = self.appointment.scheduled_datetime
                    if dt:
                        selected_date = self._get_selected_local_date()
                        day_label = self._format_day(selected_date) if selected_date else "that day"
                        times     = self._get_two_available_times_for_date(selected_date) if selected_date else []
                        time_a    = times[0].strftime('%I%p').lstrip('0') if len(times) > 0 else "9AM"
                        time_b    = times[1].strftime('%I%p').lstrip('0') if len(times) > 1 else "2PM"
                        return (
                            f"Perfect, for {day_label} — "
                            f"what works better: {time_a} or {time_b}?"
                        )
                    return "What time works best for you — morning or afternoon?"

                if next_question == "area":
                    return "All good, what area are you in?"

                #
                if next_question == "name":
                    # If name was just captured this turn, send final confirmation
                    if self.appointment.customer_name and 'customer_name' in (updated_fields or []):
                        return self._build_named_booking_confirmation()
                    # Name declined this turn
                    if self._declines_sharing_name(incoming_message):
                        self._mark_customer_name_declined()
                        return (
                            "No problem at all. Your appointment is still confirmed — "
                            "we'll use this WhatsApp number for updates."
                        )
                    # Still waiting for name
                    return (
                        "One last thing — what name should we put on the booking? "
                        "If you'd rather not share it, just say no."
                    )
            # ── AI-driven retries ─────────────────────────────────────────────────
            appointment_context = self.get_appointment_context()
            retry_context_line = self._build_retry_context_line(updated_fields, next_question)
    
            system_prompt = f"""
    You are a sharp, confident sales assistant for Homebase Plumbers in Zimbabwe.
    
    CURRENT FLOW:
    1. service_type          ✅ or pending
    2. project_description   ✅ or pending
    3. availability_date     ✅ or pending
    4. availability_time     ✅ or pending
    5. area                  ✅ or pending
    
    CURRENT SITUATION:
    {appointment_context}
    
    Next question needed: {next_question}
    New info just received: {updated_fields if updated_fields else 'None'}
    Relevant line to weave in if helpful: {retry_context_line or 'None'}
    Retry count: {retry_count}
    
    RETRY ESCALATION (retry_count > 0):
    Retry 1 → Simplify the question to bare minimum.
    Retry 2 → Offer two explicit choices instead of open question.
    Retry 3 → Add light urgency: "We're booking up this week."
    
    QUESTION MAPPINGS (rephrase these — do NOT use word-for-word):
    - service_type       → ask which of our three services they need
    - project_description → ask what they specifically want done, more detail = better quote
    - availability_date  → ask which of two upcoming weekday dates works for the site assessment
    - availability_time  → ask which of two time slots (morning or afternoon) works for that date
    - area               → ask which suburb/area they are in
    
    RULES:
    - ONE question at a time. No stacking.
    - If new info was provided, start by thanking them for it.
    - If new info was provided, add one short relevant line tied to that info before the question.
    - Rephrase the question to match the customer's tone and wording style.
    - South African / Zimbabwean English tone.
    - No markdown headers. Short sentences.
    - NEVER ask for info already collected.
    
    Generate the response now:"""
    
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": f"Customer message: '{incoming_message}'"}
                ],
                temperature=0.7,
                max_tokens=250
            )
    
            reply = response.choices[0].message.content.strip()
    
            # Reset / increment retry counter
            if updated_fields:
                self._set_question_retry_count(next_question, 0)
            else:
                self._set_question_retry_count(next_question, retry_count + 1)
            self._sync_retry_count_field(next_question)

            return reply
    
        except Exception as e:
            print(f"❌ Error generating contextual response: {str(e)}")
            return "I understand. Let me ask you about the next detail we need for your appointment."


    def _handle_all_day_response(self) -> str:
        """
        Customer said they're available all day.
        Auto-assign next available time slot at or after 12:00 (noon).
        """
        import pytz as _pytz
        from datetime import datetime as dt_cls
    
        sa_tz = _pytz.timezone('Africa/Johannesburg')
        date_obj = self._get_selected_local_date()
        if not date_obj:
            return "What time works best for you — morning or afternoon?"
    
        # Try 12:00 first, then 13, 14, 15, 16
        for h in [12, 13, 14, 15, 16]:
            candidate = sa_tz.localize(
                dt_cls.combine(date_obj, dt_cls.min.time().replace(hour=h))
            )
            is_avail, _ = self.check_appointment_availability(candidate)
            if is_avail:
                self.appointment.scheduled_datetime = candidate
                self._mark_time_confirmed()
                self.appointment.save(update_fields=['scheduled_datetime', 'internal_notes'])
                hour_str = candidate.strftime('%I%p').lstrip('0')
                day_label = self._format_day(date_obj)
                return (
                    f"Perfect, please expect us anytime after {hour_str} on {day_label}. "
                    f"What area are you in?"
                )
    
        # No slot found — ask them to pick a time
        return (
            "We're quite booked that day from noon onwards. "
            "What time works best for you — morning or afternoon?"
        )



  
    def handle_early_datetime_provision(self, message):
        """Handle cases where customer provides date/time before we ask for availability"""
        try:
            # Extract datetime using existing method
            parsed_datetime = self.parse_datetime_with_ai(message)
            
            if parsed_datetime:
                # Store the datetime for later use
                self.appointment.scheduled_datetime = parsed_datetime
                if self._appointment_has_field('retry_count'):
                    self.appointment.save(update_fields=['retry_count'])
                
                print(f"📅 Early datetime provision captured: {parsed_datetime}")
                
                # Check if we can book immediately
                booking_status = self.smart_booking_check()
                
                if booking_status['ready_to_book']:
                    print("🎯 All information available, proceeding with booking...")
                    return self.attempt_immediate_booking()
                else:
                    missing = ", ".join(booking_status['missing_fields'])
                    print(f"📋 Still need: {missing}")
                    return None  # Continue with normal flow
            
            return None
            
        except Exception as e:
            print(f"❌ Error handling early datetime: {str(e)}")
            return None

    def attempt_immediate_booking(self):
        """Attempt to book appointment when all information is available"""
        try:
            if not self.appointment.scheduled_datetime:
                return None
                
            # Check availability
            is_available, conflict_info = self.check_appointment_availability(self.appointment.scheduled_datetime)
            
            if is_available:
                # Book the appointment
                self.appointment.status = 'confirmed'
                self.appointment.save(update_fields=['status'])
                
                # Get appointment details for response
                appointment_details = self.extract_appointment_details()
                
                # Add to calendar and notify team
                try:
                    self.send_confirmation_message(appointment_details, self.appointment.scheduled_datetime)
                    self.add_to_google_calendar(appointment_details, self.appointment.scheduled_datetime)
                    self.notify_team(appointment_details, self.appointment.scheduled_datetime)
                except Exception as notify_error:
                    print(f"⚠️ Notification error: {notify_error}")
                
                # Generate confirmation message
                if self.appointment.customer_name:
                    return self._build_named_booking_confirmation()
                else:
                    return (
                        f"Perfect! I've booked your appointment for "
                        f"{self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')}. "
                        f"I've also sent your confirmation details here on WhatsApp."
                    )
            
            else:
                # Handle conflict
                alternatives = self.get_alternative_time_suggestions(self.appointment.scheduled_datetime)
                if alternatives:
                    alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                    return f"That time isn't available. Here are some alternatives:\n{alt_text}\n\nWhich works better for you?"
                else:
                    return "That time isn't available. Could you suggest another time? Our hours are 8 AM - 6 PM, Monday to Friday."
            
        except Exception as e:
            print(f"❌ Error attempting immediate booking: {str(e)}")
            return None





    def validate_information_completeness(self):
        """Validate that all required information is present and correct"""
        try:
            validation_results = {
                'valid': True,
                'errors': [],
                'warnings': []
            }
            
            # Check required fields
            if not self.appointment.project_type:
                validation_results['errors'].append("Service type not specified")
                validation_results['valid'] = False
            
            if self.appointment.has_plan is None:
                validation_results['errors'].append("Plan preference not specified")
                validation_results['valid'] = False
            
            if not self.appointment.customer_area:
                validation_results['errors'].append("Customer area not provided")
                validation_results['valid'] = False
            
            if not self.appointment.property_type:
                validation_results['errors'].append("Property type not specified")
                validation_results['valid'] = False
            
            if not self.appointment.scheduled_datetime:
                validation_results['errors'].append("Appointment time not scheduled")
                validation_results['valid'] = False
            
            # Check data quality
            if self.appointment.scheduled_datetime:
                if self.appointment.scheduled_datetime <= timezone.now():
                    validation_results['errors'].append("Appointment time is in the past")
                    validation_results['valid'] = False
            
            if self.appointment.customer_name:
                if not self.is_valid_name(self.appointment.customer_name):
                    validation_results['warnings'].append("Customer name may not be valid")
            
            return validation_results
            
        except Exception as e:
            print(f"Error validating information: {str(e)}")
            return {'valid': False, 'errors': [str(e)], 'warnings': []}






    def is_valid_name(self, name):
        """Validate if a string looks like a real person's name"""
        if not name or len(name.strip()) < 2:
            return False
        
        # Remove common non-name words
        name_clean = name.strip().lower()
        invalid_words = ['yes', 'no', 'ok', 'sure', 'thanks', 'hello', 'hi', 'good', 'fine', 
                        'sharp', 'cool', 'noted', 'great', 'alright', 'okay', 'perfect', 'nice']        
        if name_clean in invalid_words:
            return False
            
        # Check if it contains mostly letters and spaces
        if not re.match(r'^[a-zA-Z\s]+$', name):
            return False
            
        return True




    def extract_appointment_data_with_ai(self, message):
        """Enhanced AI extraction with proper property_type handling"""
        try:
            next_question = self.get_next_question_to_ask()
            retry_count = getattr(self.appointment, 'retry_count', 0)
            
            extraction_prompt = f"""
            You are a data extraction assistant for a plumbing appointment system.
            
            TASK: Extract specific appointment information from the customer's message.
            
            CONTEXT:
            - Current date: {timezone.now().strftime('%Y-%m-%d')}
            - Current question being asked: {next_question}
            - Customer message: "{message}"
            - Phone number: {self.phone_number}
            - Retry attempt: {retry_count}
            
            EXTRACTION RULES:
            1. Only extract data relevant to the current question being asked
            2. Return ONLY the extracted value, no explanations
            3. If no clear answer is found, return "NOT_FOUND"
            4. Be flexible with language variations and typos
            
            QUESTION-SPECIFIC EXTRACTION:
            
            If current question is "service_type":
            - Look for: bathroom, kitchen, plumbing, installation, renovation, repair
            - Return one of: "bathroom renovation", "kitchen renovation", "new plumbing installation"
            
            If current question is "plan_or_visit":
            - Look for: existing plan, site visit, yes/no responses
            - Return one of: "has_plan", "needs_visit"
            
            If current question is "area":
            - Extract location/area information
            - Return the area name (e.g., "Hatfield", "Avondale")
            
            If current question is "timeline":
            - Extract when they want work done
            - Return the timeline as stated
            
            If current question is "property_type":
            - Look for: house, apartment, business, home, flat, office, shop
            - Be flexible with synonyms
            - Return one of: "house", "apartment", "business"
            
            If current question is "availability":
            - Parse complete date and time to format YYYY-MM-DDTHH:MM
            - Handle relative dates like "today", "tomorrow", weekdays
            - Return complete datetime or "PARTIAL_INFO" or "NOT_FOUND"
            
            If current question is "name":
            - Extract person's name from patterns like "I'm", "my name is", "call me"
            - Return full name in title case
            
            CUSTOMER MESSAGE: "{message}"
            CURRENT QUESTION: {next_question}
            
            EXTRACTED VALUE:"""
            
            # Call AI to extract the data
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a precise data extraction assistant."},
                    {"role": "user", "content": extraction_prompt}
                ],
                temperature=0.1,
                max_tokens=100
            )
            
            extracted_value = response.choices[0].message.content.strip()
            
            if extracted_value and extracted_value not in ["NOT_FOUND", "PARTIAL_INFO"]:
                result = self.process_extracted_data(next_question, extracted_value, message)
                if result == "BOOK_APPOINTMENT":
                    return "BOOK_APPOINTMENT"
                print(f"✅ AI extracted {next_question}: '{extracted_value}'")
            else:
                print(f"🤖 AI could not extract {next_question} from: '{message}'")
                
            return extracted_value
            
        except Exception as e:
            print(f"❌ AI extraction error: {str(e)}")
            return self.fallback_manual_extraction(message)

    
    def process_extracted_data(self, question_type, extracted_value, original_message):
        """FIXED: Process the AI-extracted data and update the appointment"""
        try:
            print(f"🔧 Processing extracted data: {question_type} = '{extracted_value}'")
            
            # Only update if we don't already have this information
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
                
            # FIXED: Add property_type handling that was missing
            elif question_type == "property_type" and not self.appointment.property_type:
                if extracted_value in ['house', 'apartment', 'business']:
                    self.appointment.property_type = extracted_value
                    
            elif question_type == "name" and not self.appointment.customer_name:
                if self.is_valid_name(extracted_value):
                    self.appointment.customer_name = extracted_value

            elif question_type == "availability" and not self.appointment.scheduled_datetime:
                if extracted_value not in ["PARTIAL_INFO", "NOT_FOUND"]:
                    try:
                        # Parse AI datetime format: YYYY-MM-DDTHH:MM
                        parsed_dt = datetime.strptime(extracted_value, '%Y-%m-%dT%H:%M')
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        localized_dt = sa_timezone.localize(parsed_dt)
                        
                        print(f"🤖 AI extracted datetime: {localized_dt}")
                        
                        # Store the parsed datetime for booking
                        self.appointment.scheduled_datetime = localized_dt
                        self.appointment.save()
                        
                        print(f"💾 Stored datetime for booking: {localized_dt}")
                        return "BOOK_APPOINTMENT"
                        
                    except ValueError as e:
                        print(f"❌ Failed to parse AI datetime '{extracted_value}': {str(e)}")
            
            # Save the updated appointment
            self.appointment.save()
            print(f"💾 Appointment updated successfully")
            
        except Exception as e:
            print(f"❌ Error processing extracted data: {str(e)}")



    def fallback_manual_extraction(self, message):
        """ENHANCED: Fallback extraction - ONLY extract what's being asked"""
        try:
            message_lower = message.lower()
            original_message = message.strip()
            next_question = self.get_next_question_to_ask()
            retry_count = getattr(self.appointment, 'retry_count', 0)
            
            print(f"🔍 Fallback extraction - Current question: {next_question}")
            
            # Be more generous on retries
            be_generous = retry_count > 0
            
            # CRITICAL: ONLY extract plan status when it's the actual question being asked
            if next_question == "plan_or_visit" and self.appointment.has_plan is None:
                print(f"❓ Looking for plan status in message: '{message}'")
                
                # Explicit YES patterns
                yes_patterns = [
                    'yes', 'yeah', 'yep', 'yup', 'sure', 'have plan', 'got plan', 
                    'have a plan', 'got a plan', 'already have', 'existing plan',
                    'i do', 'i have', 'yes i do', 'yes i have', 'i got'
                ]
                
                # Explicit NO patterns
                no_patterns = [
                    'no', 'nope', 'nah', "don't have", "dont have", 
                    'no plan', 'need visit', 'site visit', 'visit first',
                    "don't", "i don't", 'visit please', 'no i', 'i need'
                ]
                
                # Check for YES
                for pattern in yes_patterns:
                    if pattern in message_lower:
                        self.appointment.has_plan = True
                        self.appointment.save()
                        print(f"✅ Manual extraction: has_plan = True (matched: '{pattern}')")
                        return "has_plan"
                
                # Check for NO
                for pattern in no_patterns:
                    if pattern in message_lower:
                        self.appointment.has_plan = False
                        self.appointment.save()
                        print(f"✅ Manual extraction: has_plan = False (matched: '{pattern}')")
                        return "needs_visit"
                
                print(f"⚠️ No clear plan status found in message")
            
            # Property type detection
            if next_question == "property_type" and not self.appointment.property_type:
                property_keywords = {
                    'house': ['house', 'home', 'residential'],
                    'apartment': ['apartment', 'flat', 'unit', 'complex'],
                    'business': ['business', 'commercial', 'office', 'shop', 'store', 'company']
                }
                
                if be_generous:
                    property_keywords['house'].extend(['place', 'property', 'residence'])
                    property_keywords['apartment'].extend(['condo', 'townhouse'])
                    property_keywords['business'].extend(['work', 'workplace', 'commercial'])
                
                for prop_type, keywords in property_keywords.items():
                    if any(keyword in message_lower for keyword in keywords):
                        self.appointment.property_type = prop_type
                        self.appointment.save()
                        print(f"✅ Manual extraction: property_type = {prop_type}")
                        return prop_type
            
            return "NOT_FOUND"
            
        except Exception as e:
            print(f"❌ Fallback extraction error: {str(e)}")
            return "NOT_FOUND"



    def update_appointment_from_conversation(self, message):
        """Enhanced version using AI-powered extraction with retry logic"""
        try:
            print(f"🔍 Processing message: '{message}'")
            
            # Get current question and retry count
            next_question = self.get_next_question_to_ask()
            retry_count = getattr(self.appointment, 'retry_count', 0)
            
            # Use AI to extract appointment data
            extracted_result = self.extract_appointment_data_with_ai(message)
            
            # Check if extraction was successful
            if extracted_result and extracted_result not in ["NOT_FOUND", "ERROR"]:
                # Reset retry count on successful extraction
                self.appointment.retry_count = 0
                if self._appointment_has_field('retry_count'):
                    self.appointment.save(update_fields=['retry_count'])
                print(f"✅ Successfully extracted {next_question}: {extracted_result}")
                return extracted_result
            else:
                # Increment retry count for failed extraction
                self.appointment.retry_count = retry_count + 1
                self.appointment.save()
                print(f"⚠️ Failed to extract {next_question}. Retry count: {self.appointment.retry_count}")
                
                # Don't give up - let AI ask again with different phrasing
                return "RETRY_NEEDED"
            
            # Check if we should book appointment
            if extracted_result == "BOOK_APPOINTMENT":
                return "BOOK_APPOINTMENT"
                
        except Exception as e:
            print(f"❌ Error updating appointment from conversation: {str(e)}")
            return "ERROR"




    def parse_datetime(self, message):
        """Parse date and time from message - ENHANCED VERSION"""
        try:
            import datetime
            import pytz
            import re

            # Use South Africa timezone consistently
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            now = timezone.now().astimezone(sa_timezone)

            # Day mapping
            day_mapping = {
                'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                'friday': 4, 'saturday': 5, 'sunday': 6
            }

            message_lower = message.lower()
            appointment_date = None

            # Enhanced relative day detection
            for day_name, day_num in day_mapping.items():
                # Look for patterns like "next Monday", "this Friday", "coming Tuesday"
                patterns = [
                    rf'(next week|next)\s+{day_name}',
                    rf'(this|coming)\s+{day_name}',
                    rf'{day_name}(?!\s+(?:last|past))',  # Just the day name, not "last Monday"
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, message_lower)
                    if match:
                        modifier = match.group(1) if match.groups() else None
                        base_day = now.weekday()
                        days_ahead = (day_num - base_day) % 7

                        if modifier in ['next', 'next week']:
                            days_ahead = days_ahead + 7 if days_ahead == 0 else days_ahead + 7
                        elif modifier in ['this', 'coming']:
                            if days_ahead == 0 and now.hour >= 12:  # If it's the same day but afternoon
                                days_ahead = 7  # Next week
                            elif days_ahead == 0:
                                days_ahead = 0  # Today
                        elif not modifier:  # Just "Monday"
                            if days_ahead == 0:  # If today is Monday
                                if now.hour < 18:  # Before 6pm, could mean today
                                    days_ahead = 0
                                else:  # After 6pm, probably next Monday
                                    days_ahead = 7
                            # If days_ahead > 0, it's this week

                        appointment_date = now + datetime.timedelta(days=days_ahead)
                        print(f"Parsed relative day: {day_name} with modifier '{modifier}' = {appointment_date.date()}")
                        break
                
                if appointment_date:
                    break

            # Handle "tomorrow" and "today"
            if not appointment_date:
                if 'tomorrow' in message_lower:
                    appointment_date = now + datetime.timedelta(days=1)
                    print(f"Parsed 'tomorrow' = {appointment_date.date()}")
                elif 'today' in message_lower:
                    appointment_date = now
                    print(f"Parsed 'today' = {appointment_date.date()}")

            # Handle exact date formats with better patterns
            if not appointment_date:
                date_patterns = [
                    r'(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?',  # 15/07, 15-07, 15/07/2025
                    r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:,?\s*(\d{4}))?',
                    r'(\d{1,2})(?:st|nd|rd|th)?\s+(january|february|march|april|may|june|july|august|september|october|november|december)(?:,?\s*(\d{4}))?',
                ]

                month_names = ['january', 'february', 'march', 'april', 'may', 'june',
                            'july', 'august', 'september', 'october', 'november', 'december']

                for pattern in date_patterns:
                    date_match = re.search(pattern, message_lower)
                    if date_match:
                        try:
                            groups = date_match.groups()
                            
                            if '/' in pattern or '-' in pattern:  # DD/MM or DD-MM format
                                day, month = int(groups[0]), int(groups[1])
                                year = int(groups[2]) if groups[2] else now.year
                                if year < 100:  # Handle 2-digit years
                                    year += 2000
                            else:  # Month name formats
                                if groups[0].lower() in month_names:  # "January 15"
                                    month = month_names.index(groups[0].lower()) + 1
                                    day = int(groups[1])
                                    year = int(groups[2]) if groups[2] else now.year
                                else:  # "15 January"
                                    day = int(groups[0])
                                    month = month_names.index(groups[1].lower()) + 1
                                    year = int(groups[2]) if groups[2] else now.year
                            
                            # Create appointment date
                            appointment_date = now.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
                            
                            # If the date is in the past, assume next year
                            if appointment_date < now:
                                appointment_date = appointment_date.replace(year=now.year + 1)
                            
                            print(f"Parsed exact date: {appointment_date.date()}")
                            break
                            
                        except (ValueError, IndexError) as e:
                            print(f"Date parsing error for pattern {pattern}: {str(e)}")
                            continue

            if not appointment_date:
                print("No date found in message")
                return None

            # Enhanced time parsing
            time_patterns = [
                (r'(\d{1,2}):(\d{2})\s*(am|pm)', 'hh:mm am/pm'),
                (r'(\d{1,2})\s*(am|pm)', 'hh am/pm'),
                (r'(\d{1,2}):(\d{2})', 'hh:mm 24-hour'),
            ]

            time_found = False
            for pattern, description in time_patterns:
                time_match = re.search(pattern, message_lower)
                if time_match:
                    groups = time_match.groups()
                    
                    if len(groups) >= 3 and groups[2]:  # Has AM/PM
                        hour = int(groups[0])
                        minute = int(groups[1]) if len(groups) > 1 and groups[1] else 0
                        am_pm = groups[2]
                        
                        # Convert to 24-hour time
                        if am_pm == 'pm' and hour != 12:
                            hour += 12
                        elif am_pm == 'am' and hour == 12:
                            hour = 0
                            
                    elif len(groups) >= 2 and groups[1] and groups[1] in ['am', 'pm']:  # Just hour with AM/PM
                        hour = int(groups[0])
                        minute = 0
                        am_pm = groups[1]
                        
                        if am_pm == 'pm' and hour != 12:
                            hour += 12
                        elif am_pm == 'am' and hour == 12:
                            hour = 0
                            
                    else:  # 24-hour format
                        hour = int(groups[0])
                        minute = int(groups[1]) if len(groups) > 1 and groups[1] else 0

                    # Validate time
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        appointment_date = appointment_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        time_found = True
                        print(f"Parsed time using {description}: {hour:02d}:{minute:02d}")
                        break
                    else:
                        print(f"Invalid time values: hour={hour}, minute={minute}")

            if not time_found:
                print("No valid time found in message")
                return None

            print(f"Final parsed datetime: {appointment_date}")
            return appointment_date

        except Exception as e:
            print(f"DateTime parsing error: {str(e)}")
            return None




    def detect_reschedule_request_with_ai(self, message):
        """Use AI to intelligently detect rescheduling requests"""
        try:
            # Only check for reschedule if appointment is already confirmed
            if self.appointment.status != 'confirmed' or not self.appointment.scheduled_datetime:
                return False
                
            current_appt = self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')
            
            detection_prompt = f"""
            You are a rescheduling detection assistant for an appointment system.
            
            TASK: Determine if the customer's message is requesting to reschedule their existing appointment.
            
            CONTEXT:
            - Customer has a CONFIRMED appointment: {current_appt}
            - Customer message: "{message}"
            - Phone: {self.phone_number}
            
            DETECTION CRITERIA:
            Look for ANY indication the customer wants to:
            - Change their appointment time/date
            - Move their appointment to a different slot
            - Cancel and rebook for a different time
            - Express they can't make their current appointment
            - Request a different day or time
            
            EXAMPLES OF RESCHEDULE REQUESTS:
            - "Can we reschedule to Monday?"
            - "I need to change my appointment"
            - "Something came up, can we move it?"
            - "Can't make it tomorrow, how about Friday?"
            - "I'm busy that day, any other time?"
            - "Emergency came up"
            - "Can we do it earlier/later?"
            - "Different day would be better"
            - "Monday at 2pm instead?"
            
            EXAMPLES OF NON-RESCHEDULE MESSAGES:
            - "Thanks for confirming"
            - "Looking forward to it"
            - "What should I prepare?"
            - "Do you need directions?"
            - "How much will it cost?"
            
            RESPONSE FORMAT:
            Reply with ONLY:
            - "YES" if this is clearly a reschedule request
            - "NO" if this is not a reschedule request
            - "MAYBE" if it's ambiguous but could be a reschedule request
            
            Do not provide explanations, just the single word response.
            
            CUSTOMER MESSAGE: "{message}"
            """
            
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a precise detection assistant. Follow instructions exactly and respond with only YES, NO, or MAYBE."},
                    {"role": "user", "content": detection_prompt}
                ],
                temperature=0.1,  # Low temperature for consistency
                max_tokens=10
            )
            
            ai_response = response.choices[0].message.content.strip().upper()
            
            if ai_response in ["YES", "MAYBE"]:
                print(f"🤖 AI detected reschedule request: {ai_response}")
                return True
            elif ai_response == "NO":
                print(f"🤖 AI determined not a reschedule request: {ai_response}")
                return False
            else:
                print(f"🤖 AI gave unexpected response: {ai_response}, defaulting to False")
                return False
                
        except Exception as e:
            print(f"❌ AI reschedule detection error: {str(e)}")
            # Fallback to keyword detection
            return self.detect_reschedule_request(message)

    def handle_reschedule_request_with_ai(self, message):
        """Use AI to handle the complete rescheduling process"""
        try:
            print(f"🤖 AI processing reschedule request: '{message}'")
            
            # Get current appointment info
            current_appt = self.appointment.scheduled_datetime
            current_appt_str = current_appt.strftime('%A, %B %d at %I:%M %p')
            
            # Try to extract new datetime
            new_datetime = self.parse_datetime_with_ai(message)
            
            if new_datetime:
                # Check availability
                is_available, conflict = self.check_appointment_availability(new_datetime)
                
                if is_available:
                    return self.process_successful_reschedule(current_appt, new_datetime)
                else:
                    return self.handle_unavailable_reschedule_with_ai(new_datetime, message)
            else:
                return self.request_reschedule_clarification_with_ai(current_appt_str, message)
                
        except Exception as e:
            print(f"❌ AI reschedule handling error: {str(e)}")
            return "I'd like to help you reschedule, but I'm having some technical difficulties. Could you call us at (555) PLUMBING to reschedule?"

    def parse_datetime_with_ai(self, message):
        """Use DeepSeek AI to extract datetime from natural language"""
        try:
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            current_time = timezone.now().astimezone(sa_timezone)

            tomorrow_date_str = (current_time + timedelta(days=1)).strftime('%B %d, %Y')
            today_date_str = current_time.strftime('%B %d, %Y')

            # Build next-day lookup for each weekday name
            day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            next_days = {}
            for i, name in enumerate(day_names):
                days_ahead = (i - current_time.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                next_days[name] = (current_time + timedelta(days=days_ahead)).strftime('%B %d, %Y')

            datetime_extraction_prompt = f"""You are a datetime extraction assistant for appointment scheduling.

    TASK: Extract a complete date and time from the customer's message and convert it to YYYY-MM-DDTHH:MM format.

    CURRENT CONTEXT:
    - Current datetime: {current_time.strftime('%Y-%m-%d %H:%M')} (Africa/Johannesburg, UTC+2)
    - Business hours: 08:00–18:00
    - Working days: Sunday through Friday (Saturday is CLOSED)
    - Today is: {today_date_str} ({current_time.strftime('%A')})

    NEXT OCCURRENCE OF EACH DAY:
    - Monday: {next_days['monday']}
    - Tuesday: {next_days['tuesday']}
    - Wednesday: {next_days['wednesday']}
    - Thursday: {next_days['thursday']}
    - Friday: {next_days['friday']}
    - Saturday: {next_days['saturday']} (CLOSED — do NOT use)
    - Sunday: {next_days['sunday']}
    - Tomorrow: {tomorrow_date_str}

    EXTRACTION RULES:
    1. Return a complete datetime ONLY if BOTH date AND time are clearly specified.
    2. "Saturday" → return UNAVAILABLE (we are closed Saturdays)
    3. "Sunday" → use Sunday date above, valid working day
    4. "tomorrow" → {tomorrow_date_str}
    5. "today" → {today_date_str}
    6. Time formats: "2pm"=14:00, "10am"=10:00, "2:30pm"=14:30, "14:00"=14:00
    7. Default minutes to 00 if not specified.
    8. Do NOT adjust timezone — return local South Africa time.

    RESPONSE FORMAT (return ONLY one of these, no other text):
    - Complete datetime: YYYY-MM-DDTHH:MM
    - Saturday requested: SATURDAY_CLOSED
    - Only partial info (missing date OR time): PARTIAL_INFO
    - No datetime found: NOT_FOUND

    CUSTOMER MESSAGE: "{message}"
    EXTRACTED DATETIME:"""

            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise datetime extraction assistant. Return ONLY the format specified — a datetime string like 2025-11-03T14:00, or one of: SATURDAY_CLOSED, PARTIAL_INFO, NOT_FOUND."
                    },
                    {"role": "user", "content": datetime_extraction_prompt}
                ],
                temperature=0.1,
                max_tokens=30
            )

            ai_response = response.choices[0].message.content.strip()
            print(f"🤖 DeepSeek datetime extraction: '{message}' → {ai_response}")

            if ai_response == "SATURDAY_CLOSED":
                print("⚠️ Customer requested Saturday — closed")
                return None  # Caller will handle with alternatives

            if ai_response in ("PARTIAL_INFO", "NOT_FOUND"):
                return None

            # Parse the returned datetime
            parsed_dt = datetime.strptime(ai_response, '%Y-%m-%dT%H:%M')
            localized_dt = sa_timezone.localize(parsed_dt)
            print(f"✅ Parsed datetime: {localized_dt}")
            return localized_dt

        except ValueError as e:
            print(f"❌ DeepSeek returned invalid datetime format: {ai_response} — {e}")
            return self.parse_datetime(message)  # fallback
        except Exception as e:
            print(f"❌ DeepSeek datetime extraction error: {e}")
            return self.parse_datetime(message)  # fallback


    def handle_unavailable_reschedule_with_ai(self, requested_datetime, original_message):
        """Use AI to generate response when requested time is unavailable"""
        try:
            # Get alternative suggestions
            alternatives = self.get_alternative_time_suggestions(requested_datetime)
            
            unavailable_response_prompt = f"""
            You a professional appointment assistant for a plumbing company.
            
            SITUATION: Customer requested to reschedule to a time that's not available.
            
            CONTEXT:
            - Customer requested: {requested_datetime.strftime('%A, %B %d at %I:%M %p')}
            - This time is unavailable (conflict with another appointment)
            - Alternative times available: {[alt['display'] for alt in alternatives] if alternatives else 'None immediately available'}
            
            TASK: Write a professional, helpful response that:
            1. Politely explains the requested time isn't available
            2. Offers the alternative times if available
            3. Asks customer to choose an alternative or suggest another time
            4. Maintains friendly, professional tone
            5. Keep it concise (2-3 sentences max)
            
            RESPONSE STYLE:
            - Professional but warm
            - No humor or jokes
            - Direct and clear
            - Use "That time isn't available" rather than technical explanations
            
            Generate the response:"""
            
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a professional appointment assistant. Be helpful and concise."},
                    {"role": "user", "content": unavailable_response_prompt}
                ],
                temperature=0.7,
                max_tokens=150
            )
            
            ai_response = response.choices[0].message.content.strip()
            print(f"🤖 AI generated unavailable response")
            return ai_response
            
        except Exception as e:
            print(f"❌ AI unavailable response error: {str(e)}")
            # Fallback response
            if alternatives:
                alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                return f"That time isn't available. Here are some alternatives:\n{alt_text}\n\nWhich works better for you?"
            else:
                return "That time isn't available. Could you suggest another time? Our hours are 8 AM - 6 PM, Monday to Friday."

    def request_reschedule_clarification_with_ai(self, current_appt_str, message):
        """Use AI to generate clarification request when datetime parsing fails"""
        try:
            clarification_prompt = f"""
            You are a professional appointment assistant for a plumbing company.
            
            SITUATION: Customer wants to reschedule but didn't provide clear date/time information.
            
            CONTEXT:
            - Customer's current appointment: {current_appt_str}
            - Customer message: "{message}"
            - Need both date AND time to reschedule
            
            TASK: Write a professional response that:
            1. Acknowledges their reschedule request
            2. Mentions their current appointment time
            3. Asks for specific new date AND time
            4. Provides example format ("Monday at 2pm", "tomorrow at 10am")
            5. Keep it concise and helpful
            
            RESPONSE STYLE:
            - Professional and clear
            - No humor or excessive friendliness
            - Direct request for information
            - Include current appointment for reference
            
            Generate the response:"""
            
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a professional appointment assistant. Be clear and helpful."},
                    {"role": "user", "content": clarification_prompt}
                ],
                temperature=0.7,
                max_tokens=100
            )
            
            ai_response = response.choices[0].message.content.strip()
            print(f"🤖 AI generated clarification request")
            return ai_response
            
        except Exception as e:
            print(f"❌ AI clarification error: {str(e)}")
            # Fallback response
            return f"I understand you'd like to reschedule your appointment currently scheduled for {current_appt_str}. When would you prefer to reschedule to? Please provide both the day and time (e.g., 'Monday at 2pm', 'tomorrow at 10am')."

    def process_successful_reschedule(self, old_datetime, new_datetime):
        """Process a successful reschedule and generate confirmation"""
        try:
            # Update appointment
            self.appointment.scheduled_datetime = new_datetime
            if hasattr(self.appointment, 'reschedule_count'):
                self.appointment.reschedule_count = (self.appointment.reschedule_count or 0) + 1
            if hasattr(self.appointment, 'original_datetime') and not self.appointment.original_datetime:
                self.appointment.original_datetime = old_datetime
            self.appointment.save()
            
            # Update Google Calendar
            try:
                self.update_google_calendar_appointment(old_datetime, new_datetime)
            except Exception as cal_error:
                print(f"Calendar update error: {str(cal_error)}")
            
            # Notify team
            try:
                self.notify_team_about_reschedule(old_datetime, new_datetime)
            except Exception as team_error:
                print(f"Team notification error: {str(team_error)}")
            
            # Generate confirmation with AI
            confirmation_prompt = f"""
            You are a professional appointment assistant for a plumbing company.
            
            SITUATION: Successfully rescheduled customer's appointment.
            
            DETAILS:
            - Customer: {self.appointment.customer_name or 'Customer'}
            - Old appointment: {old_datetime.strftime('%A, %B %d at %I:%M %p')}
            - New appointment: {new_datetime.strftime('%A, %B %d at %I:%M %p')}
            - Service: {self.appointment.project_type or 'Plumbing service'}
            - Area: {self.appointment.customer_area or 'Your area'}
            
            TASK: Write a professional confirmation message that:
            1. Confirms the reschedule
            2. Shows the new appointment time clearly
            3. Mentions the team will contact them before arrival
            4. Offers help if they need to change again
            5. Professional, reassuring tone
            
            Keep it concise and clear.
            
            Generate the confirmation:"""
            
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a professional appointment assistant. Be reassuring and clear."},
                    {"role": "user", "content": confirmation_prompt}
                ],
                temperature=0.7,
                max_tokens=150
            )
            
            ai_confirmation = response.choices[0].message.content.strip()
            print(f"✅ Successful reschedule processed with AI confirmation")
            return ai_confirmation
            
        except Exception as e:
            print(f"❌ Error processing successful reschedule: {str(e)}")
            # Fallback confirmation
            return f"✅ Appointment rescheduled to {new_datetime.strftime('%A, %B %d at %I:%M %p')}. Our team will contact you before arrival."

    def log_ai_reschedule_decision(self, message, ai_decision, confidence=None):
        """Log AI reschedule decisions for monitoring and improvement"""
        try:
            log_entry = {
                'timestamp': timezone.now().isoformat(),
                'phone': self.phone_number,
                'message': message,
                'ai_decision': ai_decision,
                'confidence': confidence,
                'appointment_status': self.appointment.status,
                'has_scheduled_time': bool(self.appointment.scheduled_datetime)
            }
            
            # You can save this to a log file or database for analysis
            print(f"🤖 AI Reschedule Decision: {log_entry}")
            
            # Optional: Save to database for analysis
            # RescheduleDecisionLog.objects.create(**log_entry)
            
        except Exception as e:
            print(f"Error logging AI decision: {str(e)}")



    def get_availability_error_message(self, error_type, conflict_appointment=None):
        """Generate user-friendly error messages for availability issues"""
        try:
            if error_type == "past_time":
                return "That time has already passed. Please choose a future time."
            #
            elif error_type == "saturday_closed":
                return "We're closed on Saturdays. Please choose Sunday through Friday."

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
                # Skip Saturday only (Sunday is open)
                if check_date.weekday() != 5:
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
    #
    def is_business_day(self, check_date):
        """Check if a given date is a business day (Sunday-Friday)"""
        weekday = check_date.weekday()
        return weekday != 5  # All days except Saturday (5)

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

    def get_ai_performance_stats(self):
        """Get statistics on AI reschedule detection performance"""
        try:
            # This would query your log database if implemented
            # For now, just return placeholder stats
            return {
                'total_reschedule_requests': 0,
                'ai_detected_correctly': 0,
                'ai_missed': 0,
                'false_positives': 0,
                'accuracy_rate': 0.0
            }
        except Exception as e:
            print(f"Error getting AI stats: {str(e)}")
            return None





    def send_message(self, message_text):
        """Send WhatsApp message using Cloud API"""
        try:
            clean_phone = clean_phone_number(self.phone_number)
            result = whatsapp_api.send_text_message(clean_phone, message_text)
            print(f"✅ Message sent via Cloud API to {clean_phone}")
            return result
        except Exception as e:
            print(f"❌ Failed to send message: {str(e)}")
            raise



            
def send_reminder_message(appointment, reminder_type):
    """Send reminder message based on reminder type - UPDATED"""
    try:
        customer_name = appointment.customer_name or "there"
        appt_date = appointment.scheduled_datetime.strftime('%A, %B %d, %Y')
        appt_time = appointment.scheduled_datetime.strftime('%I:%M %p')
        
        if reminder_type == '1_day':
            message = f"""🔧 APPOINTMENT REMINDER

Hi {customer_name},

Just a friendly reminder about your plumbing appointment:

📅 Tomorrow: {appt_date}
🕐 Time: {appt_time}
📍 Area: {appointment.customer_area or 'Your location'}

Our team will contact you before arrival to confirm timing.

Need to reschedule? Reply to this message.

See you tomorrow!
- Homebase Plumbers"""

        elif reminder_type == 'morning':
            message = f"""🌅 GOOD MORNING REMINDER

Hi {customer_name},

Today's your plumbing appointment:

📅 Today: {appt_date}
🕐 Time: {appt_time}
📍 Area: {appointment.customer_area or 'Your location'}

Our team will call you 30 minutes before arrival.

Questions? Reply here.

Looking forward to serving you today!
- Homebase Plumbers"""

        elif reminder_type == '2_hours':
            message = f"""⏰ APPOINTMENT IN 2 HOURS

Hi {customer_name},

Your plumbing appointment is coming up:

🕐 In 2 hours: {appt_time}
📍 Area: {appointment.customer_area or 'Your location'}

Our team will call you in about 30 minutes to confirm arrival time.

Please ensure someone is available at the location.

Questions? Reply here.

- Homebase Plumbers"""
        else:
            return False

        # Send using WhatsApp Cloud API
        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, message)
        
        print(f"✅ {reminder_type} reminder sent to {clean_phone}")
        return True
    
    except Exception as e:
        print(f"❌ Failed to send {reminder_type} reminder: {str(e)}")
        return False


    def check_and_send_reminders():
        """Check for appointments that need reminders and send them"""
        try:
            # Get current time in South Africa timezone
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            now = timezone.now().astimezone(sa_timezone)
            
            # Get confirmed appointments
            confirmed_appointments = Appointment.objects.filter(
                status='confirmed',
                scheduled_datetime__isnull=False,
                scheduled_datetime__gte=now  # Only future appointments
            )
            
            reminders_sent = {
                '1_day': 0,
                'morning': 0, 
                '2_hours': 0
            }
            
            for appointment in confirmed_appointments:
                appt_time = appointment.scheduled_datetime.astimezone(sa_timezone)
                time_until_appointment = appt_time - now
                
                # 1 day before reminder (send between 22-24 hours before)
                if 22 <= time_until_appointment.total_seconds() / 3600 <= 24:
                    if not hasattr(appointment, 'reminder_1_day_sent') or not appointment.reminder_1_day_sent:
                        if send_reminder_message(appointment, '1_day'):
                            # Mark as sent (you'll need to add this field to your model)
                            appointment.reminder_1_day_sent = True
                            appointment.save()
                            reminders_sent['1_day'] += 1
                
                # Morning of appointment reminder (send at 7 AM on appointment day)
                appointment_date = appt_time.date()
                current_date = now.date()
                current_hour = now.hour
                
                if (appointment_date == current_date and 
                    current_hour == 7 and 
                    (not hasattr(appointment, 'reminder_morning_sent') or not appointment.reminder_morning_sent)):
                    if send_reminder_message(appointment, 'morning'):
                        appointment.reminder_morning_sent = True
                        appointment.save()
                        reminders_sent['morning'] += 1
                
                # 2 hours before reminder
                if 1.5 <= time_until_appointment.total_seconds() / 3600 <= 2.5:
                    if not hasattr(appointment, 'reminder_2_hours_sent') or not appointment.reminder_2_hours_sent:
                        if send_reminder_message(appointment, '2_hours'):
                            appointment.reminder_2_hours_sent = True
                            appointment.save()
                            reminders_sent['2_hours'] += 1
            
            print(f"📊 Reminders sent: 1-day: {reminders_sent['1_day']}, Morning: {reminders_sent['morning']}, 2-hours: {reminders_sent['2_hours']}")
            return reminders_sent
            
        except Exception as e:
            print(f"❌ Error checking reminders: {str(e)}")
            return None


    def manual_reminder_check(request):
        """Manual trigger for checking and sending reminders (for testing)"""
        if request.method == 'POST':
            try:
                results = check_and_send_reminders()
                if results:
                    messages.success(request, f"Reminder check completed. Sent: {sum(results.values())} reminders")
                else:
                    messages.error(request, "Error occurred during reminder check")
            except Exception as e:
                messages.error(request, f"Error: {str(e)}")
        
        return redirect('dashboard')


    def send_test_reminder(request, pk):
        """Send a test reminder for a specific appointment"""
        appointment = get_object_or_404(Appointment, pk=pk)
        
        if request.method == 'POST':
            reminder_type = request.POST.get('reminder_type', '2_hours')
            
            if send_reminder_message(appointment, reminder_type):
                messages.success(request, f'Test {reminder_type} reminder sent successfully')
            else:
                messages.error(request, 'Failed to send test reminder')
        
        return redirect('appointment_detail', pk=appointment.pk)


    # Management command function (you can also create this as a Django management command)
    def run_reminder_scheduler():
        """Function to be called by your scheduler (cron job, celery, etc.)"""
        print(f"🔄 Running reminder check at {timezone.now()}")
        results = check_and_send_reminders()
        if results:
            total_sent = sum(results.values())
            print(f"✅ Reminder check completed. Total reminders sent: {total_sent}")
            return True
        else:
            print("❌ Reminder check failed")
            return False



# Enhanced logging for the main bot function




def test_information_extraction(phone_number, test_message):
    """Test function to verify information extraction works correctly"""
    try:
        print(f"🧪 Testing information extraction...")
        print(f"📱 Phone: {phone_number}")
        print(f"💬 Message: '{test_message}'")
        
        plumbot = Plumbot(phone_number)
        
        # Show state before
        before = plumbot.get_information_summary()
        print(f"📊 Before: {before}")
        
        # Extract information
        extracted = plumbot.extract_all_available_info_with_ai(test_message)
        print(f"🔍 Extracted: {extracted}")
        
        # Update appointment
        updated_fields = plumbot.update_appointment_with_extracted_data(
            extracted,
            incoming_message=test_message,
        )
        print(f"✏️ Updated fields: {updated_fields}")
        
        # Show state after
        after = plumbot.get_information_summary()
        print(f"📊 After: {after}")
        
        # Check booking readiness
        booking_status = plumbot.smart_booking_check()
        print(f"🎯 Booking readiness: {booking_status}")
        
        return {
            'success': True,
            'before': before,
            'extracted': extracted,
            'updated_fields': updated_fields,
            'after': after,
            'booking_status': booking_status
        }
        
    except Exception as e:
        print(f"❌ Test error: {str(e)}")
        return {'success': False, 'error': str(e)}



# Add this test function to your views.py to verify WhatsApp setup
def test_whatsapp_notification(request):
    """Test function to verify WhatsApp notifications work"""
    try:
        # Test message
        test_message = """🧪 TEST NOTIFICATION

This is a test message to verify WhatsApp notifications are working.
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

If you receive this, notifications are working! ✅"""

        # Team numbers to test
        TEAM_NUMBERS = [
            'whatsapp:+263774819901',  # Your plumber's number
        ]
        
        results = []
        
        for number in TEAM_NUMBERS:
            try:
                message = twilio_client.messages.create(
                    body=test_message,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=number
                )
                results.append({
                    'number': number,
                    'status': 'success',
                    'sid': message.sid,
                    'error': None
                })
                print(f"✅ Test message sent to {number}. SID: {message.sid}")
            except Exception as e:
                results.append({
                    'number': number,
                    'status': 'failed',
                    'sid': None,
                    'error': str(e)
                })
                print(f"❌ Failed to send test message to {number}: {str(e)}")
        
        return JsonResponse({
            'success': True,
            'results': results,
            'message': 'Test completed. Check console logs for details.'
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })

# Add this to your URL patterns to test
# path('test-whatsapp/', test_whatsapp_notification, name='test_whatsapp'),



# Verification checklist function
def verify_whatsapp_setup():
    """Run this to verify your WhatsApp setup"""
    print("🔍 WHATSAPP SETUP VERIFICATION")
    print("=" * 40)
    
    # Check Twilio credentials
    print(f"📱 Twilio Account SID: {ACCOUNT_SID}")
    print(f"🔑 Auth Token: {'*' * (len(AUTH_TOKEN)-4) + AUTH_TOKEN[-4:]}")
    print(f"📞 WhatsApp Number: {TWILIO_WHATSAPP_NUMBER}")
    
    # Test Twilio client
    try:
        account = twilio_client.api.accounts(ACCOUNT_SID).fetch()
        print(f"✅ Twilio connection successful. Account status: {account.status}")
    except Exception as e:
        print(f"❌ Twilio connection failed: {str(e)}")
        return False
    
    # Check team numbers format
    TEAM_NUMBERS = ['whatsapp:+263774819901']  # Your actual numbers
    print(f"👥 Team numbers configured: {len(TEAM_NUMBERS)}")
    
    for number in TEAM_NUMBERS:
        if not number.startswith('whatsapp:+'):
            print(f"⚠️  Invalid format for {number}. Should be 'whatsapp:+263XXXXXXXXX'")
        else:
            print(f"✅ {number} format is correct")
    
    print("\n🔧 TROUBLESHOOTING TIPS:")
    print("1. Make sure the plumber's number is registered with WhatsApp")
    print("2. The plumber must have previously messaged your Twilio WhatsApp number")
    print("3. Check Twilio console for delivery status")
    print("4. Verify the phone number format: whatsapp:+263XXXXXXXXX")
    
    return True

# Call this function to verify setup
# verify_whatsapp_setup()


@csrf_exempt
def handle_whatsapp_media(request):
    """FIXED: Handle incoming media files from WhatsApp"""
    if request.method == 'POST':
        try:
            # Get message details
            sender = request.POST.get('From', '')
            num_media = int(request.POST.get('NumMedia', 0))
            
            if not sender or num_media == 0:
                return HttpResponse(status=200)
            
            print(f"📎 Processing {num_media} media files from {sender}")
            
            # Get the appointment
            try:
                appointment = Appointment.objects.get(phone_number=sender)
            except Appointment.DoesNotExist:
                print(f"❌ No appointment found for {sender}")
                # Send helpful message
                twilio_client.messages.create(
                    body="I don't have an active appointment for this number. Please start by telling me about your plumbing needs.",
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender
                )
                return HttpResponse(status=200)
            
            # Check if we should accept media based on appointment state
            plumbot = Plumbot(sender)
            
            # If they have a plan and we have basic info, initiate upload flow
            if (appointment.has_plan is True and 
                appointment.customer_area and 
                appointment.property_type and
                appointment.plan_status is None):
                
                # Start the plan upload process
                appointment.plan_status = 'pending_upload'
                appointment.save()
                print(f"🔄 Initiated plan upload flow for {sender}")
            
            # Only process media if we're in upload flow
            if appointment.plan_status != 'pending_upload':
                print(f"ℹ️ Ignoring media - not in upload flow. Status: {appointment.plan_status}")
                
                # Send helpful message
                if appointment.has_plan is True:
                    response_msg = "I'll need you to send your plan once we collect some basic information first. Let me continue with a few questions."
                else:
                    response_msg = "I see you sent a file, but I'm not currently expecting any documents. Let me continue with your appointment details."
                
                twilio_client.messages.create(
                    body=response_msg,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender
                )
                return HttpResponse(status=200)
            
            # Process each media file
            uploaded_files = []
            for i in range(num_media):
                media_url = request.POST.get(f'MediaUrl{i}', '')
                media_content_type = request.POST.get(f'MediaContentType{i}', '')
                
                if media_url:
                    file_info = download_and_save_media(
                        media_url, 
                        media_content_type, 
                        appointment, 
                        i
                    )
                    if file_info:
                        uploaded_files.append(file_info)
            
            if uploaded_files:
                # Update plan upload timestamp
                appointment.plan_uploaded_at = timezone.now()
                appointment.save()
                
                # Send acknowledgment using the plumbot's handle_plan_upload_flow
                ack_message = plumbot.handle_plan_upload_flow("file received")
                
                twilio_client.messages.create(
                    body=ack_message,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender
                )
            
            return HttpResponse(status=200)
            
        except Exception as e:
            print(f"❌ Media handling error: {str(e)}")
            return HttpResponse(status=500)
    
    return HttpResponse(status=405)


def download_and_save_media(media_url, content_type, appointment, file_index):
    """Download media from Twilio and save to Django storage - FIXED"""
    try:
        # FIXED: Use correct variable names from top of file
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)  # ✅ Changed from ACCOUNT_SID
        response = requests.get(media_url, auth=auth)
        
        if response.status_code != 200:
            print(f"❌ Failed to download media: {response.status_code}")
            return None
        
        # Determine file extension
        extension_map = {
            'image/jpeg': '.jpg',
            'image/png': '.png', 
            'image/webp': '.webp',
            'application/pdf': '.pdf',
            'image/gif': '.gif'
        }
        
        extension = extension_map.get(content_type, '.bin')
        
        # Generate filename
        timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
        customer_name = appointment.customer_name or 'customer'
        safe_name = ''.join(c for c in customer_name if c.isalnum())
        filename = f"plan_{safe_name}_{appointment.id}_{timestamp}_{file_index}{extension}"
        
        # Save file
        file_path = f"customer_plans/{filename}"
        file_content = ContentFile(response.content, name=filename)
        
        saved_path = default_storage.save(file_path, file_content)
        
        # Update appointment record if this is the first file
        if not getattr(appointment, 'plan_file', None):
            appointment.plan_file = saved_path
            appointment.save()
        
        print(f"✅ Saved media file: {saved_path}")
        
        return {
            'name': filename,
            'path': saved_path,
            'size': len(response.content),
            'content_type': content_type
        }
        
    except Exception as e:
        print(f"❌ Error downloading/saving media: {str(e)}")
        return None


