# Update the imports section in your views.py file

from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect
from twilio.rest import Client
from .models import Appointment, Quotation, QuotationItem, QuotationTemplate, QuotationTemplateItem
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
import logging



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


# Add this separate view for API-based quotation creation
@csrf_exempt
@require_http_methods(["POST"])

logger = logging.getLogger(__name__)

def create_quotation_api(request):
    """API endpoint for creating quotations from the quotation generator page"""
    logger.info("🔹 Received request to create a new quotation")

    try:
        data = json.loads(request.body)
        logger.debug(f"📦 Parsed request data: {data}")

        # Get appointment if provided
        appointment = None
        appointment_id = data.get('appointment_id')
        if appointment_id:
            logger.debug(f"🔍 Looking up Appointment with ID: {appointment_id}")
            try:
                appointment = Appointment.objects.get(id=appointment_id)
                logger.info(f"✅ Found Appointment: {appointment}")
            except Appointment.DoesNotExist:
                logger.warning(f"⚠️ Appointment with ID {appointment_id} not found.")
                appointment = None
        
        # Create the quotation
        logger.debug("🧾 Creating Quotation record...")
        quotation = Quotation.objects.create(
            appointment=appointment,
            labor_cost=data.get('labour_cost', 0),
            transport_cost=data.get('transport_cost', data.get('2', 0)),  # possible typo fix
            materials_cost=data.get('materials_cost', 0),
            notes=data.get('notes', ''),
            status='draft'
        )
        logger.info(f"✅ Quotation created with ID : {quotation.id}")

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
                    quantity=item_data.get('qty', 1),
                    unit_price=item_data.get('unit', 0)
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
            'appointment_id': appointment.id if appointment else None,
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
        # Format the quotation message
        message = format_quotation_message(quotation)
        
        # Send via WhatsApp
        client = Client(ACCOUNT_SID, AUTH_TOKEN)
        whatsapp_message = client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=quotation.appointment.phone_number
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
            content=f"Quotation #{quotation.quotation_number} sent to customer via WhatsApp",
            timestamp=timezone.now()
        )
        
        messages.success(request, 'Quotation sent successfully via WhatsApp!')
        
    except Exception as e:
        messages.error(request, f'Failed to send quotation: {str(e)}')
    
    return redirect('appointment_detail', pk=quotation.appointment.pk)

def format_quotation_message(quotation):
    """Format quotation for WhatsApp message"""
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

        # Add stats to context
        appointments = Appointment.objects.all()
        context.update({
            'total_appointments': appointments.count(),
            'pending_appointments': appointments.filter(status='pending').count(),
            'confirmed_appointments': appointments.filter(status='confirmed').count(),
            'recent_appointments': appointments.order_by('-created_at')[:5],
            'todays_confirmed_appointments': Appointment.objects.filter(
                status='confirmed',
                scheduled_datetime__date=today
            ).order_by('scheduled_datetime'),
            'tomorrows_confirmed_appointments': Appointment.objects.filter(
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        today = timezone.now().date()
        todays_confirmed_appointments = Appointment.objects.filter(
            status='confirmed',
            scheduled_datetime__date=today
        ).order_by('scheduled_datetime')


        context['status_counts'] = {
            'total': Appointment.objects.count(),
            'pending': Appointment.objects.filter(status='pending').count(),
            'confirmed': Appointment.objects.filter(status='confirmed').count(),
            'cancelled': Appointment.objects.filter(status='cancelled').count(),
            'todays_confirmed_appointments': todays_confirmed_appointments,

        }
        return context

@method_decorator(staff_required, name='dispatch')
class AppointmentDetailView(DetailView):
    template_name = 'appointment_detail.html'
    model = Appointment
    context_object_name = 'appointment'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_object()

        conversation_history = appointment.conversation_history

        context.update({
            'conversation_history': conversation_history,
            'completeness': appointment.get_customer_info_completeness(),
            'documents': appointment.get_uploaded_documents(),
            'has_documents': appointment.has_uploaded_documents(),
            'document_count': appointment.get_document_count(),
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
            
            # Handle datetime fields based on appointment type
            if appointment.appointment_type == 'job_appointment':
                job_datetime = request.POST.get('job_scheduled_datetime')
                if job_datetime:
                    appointment.job_scheduled_datetime = job_datetime
            else:
                scheduled_datetime = request.POST.get('scheduled_datetime')
                if scheduled_datetime:
                    appointment.scheduled_datetime = scheduled_datetime
            
            appointment.save()
            
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
        
        documents = appointment.get_uploaded_documents()
        
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
    messages.success(request, 'Appointment confirmed successfully')
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
            if job_datetime.weekday() >= 5:  # Weekend
                messages.error(request, 'Jobs can only be scheduled Monday-Friday')
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
        
        # Check business hours (8 AM - 6 PM, Monday-Friday)
        if job_datetime.weekday() >= 5:  # Weekend
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
    """Send notifications about new job appointment"""
    try:
        # Format job details
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
        twilio_client.messages.create(
            body=customer_message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=job_appointment.phone_number
        )
        
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
        TEAM_NUMBERS = ['whatsapp:+263774819901']
        for number in TEAM_NUMBERS:
            try:
                twilio_client.messages.create(
                    body=team_message,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=number
                )
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
                "area": appt.customer_area or "N/A",
                "status": appt.status,
                "propertyType": appt.property_type or "N/A",
                "timeline": appt.timeline or "N/A",
                "hasPlan": appt.has_plan
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




class Plumbot:
    def __init__(self, phone_number):
        self.phone_number = phone_number
        self.appointment, _ = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )


    def generate_response(self, incoming_message):
        """UPDATED: Enhanced response generation with proper alternative handling"""
        try:
            # Check if user is in plan upload flow
            if (self.appointment.has_plan is True and 
                self.appointment.plan_status == 'pending_upload'):
                return self.handle_plan_upload_flow(incoming_message)
            
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
                not self.appointment.customer_name):  # All info except name, likely selecting alternative
                
                # Try to parse as alternative time selection
                selected_time = self.process_alternative_time_selection(incoming_message)
                
                if selected_time:
                    print(f"🎯 Customer selecting alternative time: {selected_time}")
                    
                    # Book with the selected time
                    booking_result = self.book_appointment_with_selected_time(selected_time)
                    
                    if booking_result['success']:
                        reply = f"Perfect! I've booked your appointment for {booking_result['datetime']}. To complete your booking, may I have your full name?"
                    else:
                        # Still conflicts, offer new alternatives
                        alternatives = booking_result.get('alternatives', [])
                        if alternatives:
                            alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                            reply = f"That time isn't available either. Here are some other options:\n{alt_text}\n\nWhich works better for you?"
                        else:
                            reply = "I'm having trouble finding available times. Could you suggest a completely different day? Our hours are 8 AM - 6 PM, Monday to Friday."
                    
                    # Update conversation history and return
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply
            
            # STEP 2: Extract ALL available information from the message
            extracted_data = self.extract_all_available_info_with_ai(incoming_message)
            
            # STEP 3: Update appointment with extracted data
            updated_fields = self.update_appointment_with_extracted_data(extracted_data)
            
            # STEP 4: Check for reschedule requests (for confirmed appointments)
            if (self.appointment.status == 'confirmed' and 
                self.appointment.scheduled_datetime and 
                self.detect_reschedule_request_with_ai(incoming_message)):
                
                print("🤖 AI detected reschedule request, handling...")
                reschedule_response = self.handle_reschedule_request_with_ai(incoming_message)
                
                # Update conversation history
                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reschedule_response)
                
                return reschedule_response
            
            # STEP 5: Determine what to do next
            next_question = self.get_next_question_to_ask()
            
            # STEP 6: Check if we can book the appointment
            booking_status = self.smart_booking_check()
            
            # For users without plans, continue normal booking flow
            if (booking_status['ready_to_book'] and 
                self.appointment.status != 'confirmed' and
                self.appointment.has_plan is False):  # Only book directly if no plan needed
                
                booking_result = self.book_appointment(incoming_message)
                
                if booking_result['success']:
                    if not self.appointment.customer_name:
                        reply = (f"Perfect! I've booked your appointment for {booking_result['datetime']}. "
                                "To complete your booking, may I have your full name?")
                    else:
                        reply = f"✅ Appointment confirmed for {booking_result['datetime']}! Our team will contact you before arrival."
                else:
                    alternatives = booking_result.get('alternatives', [])
                    if alternatives:
                        alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                        reply = f"That time isn't available. Here are some alternatives:\n{alt_text}\n\nWhich works better for you?"
                    else:
                        reply = "That time isn't available. Could you suggest another time? Our hours are 8 AM - 6 PM, Monday to Friday."
            else:
                # Generate contextual response
                reply = self.generate_contextual_response(incoming_message, next_question, updated_fields)
            
            # Update conversation history
            self.appointment.add_conversation_message("user", incoming_message)
            self.appointment.add_conversation_message("assistant", reply)
            
            return reply

        except Exception as e:
            print(f"❌ API Error: {str(e)}")
            return "I'm having some trouble connecting to our system. Could you try again in a moment?"


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
            
            # Get plumber contact info
            plumber_number = getattr(self.appointment, 'plumber_contact_number', '+263774819901')
            
            # Send plan to plumber
            self.notify_plumber_about_plan()
            
            # Generate completion message
            service_name = self.appointment.project_type.replace('_', ' ').title()
            customer_name = self.appointment.customer_name or "Customer"
            
            completion_message = f"""✅ PLAN SENT SUCCESSFULLY!

{customer_name}, I've forwarded your {service_name} plan to our plumber for review.

📞 NEXT STEPS:
• Our plumber will review your plan within 24 hours
• They'll contact you directly on this number: {self.phone_number.replace('whatsapp:', '')}
• They'll discuss the project details and provide a quote
• Once approved, they'll book your appointment or message you to complete booking

🔧 PLUMBER DIRECT CONTACT:
If you need to reach them directly: {plumber_number.replace('+27', '0').replace('+', '')}

You don't need to do anything now - just wait for their call. They're very responsive!

Questions? Feel free to ask here anytime."""

            return completion_message

        except Exception as e:
            print(f"❌ Error completing plan upload: {str(e)}")
            return "Your plan has been uploaded. Our plumber will review it and contact you within 24 hours."

    def notify_plumber_about_plan(self):
        """Send plan details to plumber via WhatsApp"""
        try:
            # Prepare plumber notification
            service_name = self.appointment.project_type.replace('_', ' ').title()
            customer_name = self.appointment.customer_name or "Customer"
            customer_phone = self.phone_number.replace('whatsapp:', '')
            
            plumber_message = f"""📋 NEW PLAN RECEIVED!

Customer: {customer_name}
Phone: {customer_phone}
Service: {service_name}
Area: {self.appointment.customer_area}
Property: {self.appointment.property_type}
Timeline: {self.appointment.timeline}

🔍 PLAN DETAILS:
The customer has uploaded their plan through WhatsApp. Please:

1. Review the uploaded plan materials
2. Contact customer at {customer_phone} within 24 hours  
3. Discuss project scope and provide quote
4. Book appointment when ready

📱 Customer is expecting your call!

View full details: http://127.0.0.1:8000/appointments/{self.appointment.id}/

Status: Plan uploaded - awaiting your review"""

            # Send to plumber
            plumber_numbers = [
                'whatsapp:+263774819901',  # Main plumber
                # Add additional plumbers if needed
            ]
            
            for number in plumber_numbers:
                try:
                    message = twilio_client.messages.create(
                        body=plumber_message,
                        from_=TWILIO_WHATSAPP_NUMBER,
                        to=number
                    )
                    print(f"✅ Plan notification sent to plumber {number}. SID: {message.sid}")
                except Exception as msg_error:
                    print(f"❌ Failed to notify plumber {number}: {str(msg_error)}")

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
📞 Call directly: 0610318200

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

If it's urgent, you can call directly: 0610318200

Otherwise, they'll definitely contact you today!"""
        else:
            return """I see it's been over 24 hours since your plan was sent. Let me check on this for you.

Please call our plumber directly at 0610318200 - they may have tried to reach you already.

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
                to='whatsapp:+263774819901'
            )
            
            return """🚨 I've marked your plan review as URGENT and notified our plumber immediately.

They should contact you within the next few hours.

For immediate assistance, you can also call: 0610318200

I understand this is time-sensitive!"""

        except Exception as e:
            print(f"❌ Error handling urgent request: {str(e)}")
            return "I've noted this is urgent. Please call our plumber directly at 0610318200 for immediate assistance."



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
            for day_offset in range(0, 5):  # Check today + next 4 days
                check_date = requested_date + timedelta(days=day_offset)
                
                # Skip weekends
                if check_date.weekday() >= 5:
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
            
            # Customer info
            if self.appointment.customer_name:
                context_parts.append(f"Customer Name: {self.appointment.customer_name}")
            else:
                context_parts.append("Customer Name: Not provided yet")
                
            if self.appointment.customer_area:
                context_parts.append(f"Area: {self.appointment.customer_area}")
            else:
                context_parts.append("Area: Not provided yet")
                
            # Project details
            if self.appointment.project_type:
                context_parts.append(f"Service Type: {self.appointment.project_type}")
            else:
                context_parts.append("Service Type: Not specified yet")
                
            # Plan preference - Fixed boolean logic
            if self.appointment.has_plan is True:
                context_parts.append("Plan Status: Customer has existing plan")
            elif self.appointment.has_plan is False:
                context_parts.append("Plan Status: Customer wants site visit")
            else:  # has_plan is None
                context_parts.append("Plan Status: Not specified yet")
                
            if self.appointment.property_type:
                context_parts.append(f"Property Type: {self.appointment.property_type}")
            else:
                context_parts.append("Property Type: Not specified yet")
                
            if self.appointment.timeline:
                context_parts.append(f"Timeline: {self.appointment.timeline}")
            else:
                context_parts.append("Timeline: Not specified yet")
                
            # Appointment status
            context_parts.append(f"Current Status: {self.appointment.get_status_display()}")
            
            if self.appointment.scheduled_datetime:
                context_parts.append(f"Scheduled: {self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')}")
            else:
                context_parts.append("Scheduled: No appointment time set yet")
                
            # Next question to ask
            next_question = self.get_next_question_to_ask()
            context_parts.append(f"Next Question Needed: {next_question}")
            
            # Add retry attempt tracking
            retry_count = getattr(self.appointment, 'retry_count', 0)
            context_parts.append(f"Question Retry Count: {retry_count}")
                
            # Completion percentage
            completeness = self.appointment.get_customer_info_completeness()
            context_parts.append(f"Info Completeness: {completeness:.0f}%")
            
            return "\n".join(context_parts)
            
        except Exception as e:
            print(f"Error getting appointment context: {str(e)}")
            return "Unable to load appointment context"


    def update_appointment_with_extracted_data(self, extracted_data):
        """Update appointment with extracted data - COMPLETE FIX"""
        try:
            updated_fields = []
            
            # Service type - only update if we don't have one and AI found one
            if (extracted_data.get('service_type') and 
                extracted_data.get('service_type') != 'null' and
                not self.appointment.project_type):
                self.appointment.project_type = extracted_data['service_type']
                updated_fields.append('service_type')
            
            # FIXED: Plan status - ALWAYS update when AI finds one (was being blocked)
            if (extracted_data.get('plan_status') and 
                extracted_data.get('plan_status') != 'null'):
                old_value = self.appointment.has_plan
                # Convert string to boolean
                if extracted_data['plan_status'] == 'has_plan':
                    self.appointment.has_plan = True
                    updated_fields.append('plan_status')
                    print(f"✅ Updated plan status: {old_value} -> True")
                elif extracted_data['plan_status'] == 'needs_visit':
                    self.appointment.has_plan = False
                    updated_fields.append('plan_status')
                    print(f"✅ Updated plan status: {old_value} -> False")
            
            # Area - only update if we don't have one and AI found one
            if (extracted_data.get('area') and 
                extracted_data.get('area') != 'null' and
                not self.appointment.customer_area):
                self.appointment.customer_area = extracted_data['area']
                updated_fields.append('area')
            
            # Timeline - only update if we don't have one and AI found one
            if (extracted_data.get('timeline') and 
                extracted_data.get('timeline') != 'null' and
                not self.appointment.timeline):
                self.appointment.timeline = extracted_data['timeline']
                updated_fields.append('timeline')
            
            # Property type - only update if we don't have one and AI found one
            if (extracted_data.get('property_type') and 
                extracted_data.get('property_type') != 'null' and
                not self.appointment.property_type):
                self.appointment.property_type = extracted_data['property_type']
                updated_fields.append('property_type')
            
            # FIXED: Availability/DateTime - ALLOW UPDATES (was being blocked)
            if (extracted_data.get('availability') and 
                extracted_data.get('availability') != 'null'):
                try:
                    # Parse AI datetime format
                    parsed_dt = datetime.strptime(extracted_data['availability'], '%Y-%m-%dT%H:%M')
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    localized_dt = sa_timezone.localize(parsed_dt)
                    
                    # Update even if we already have a datetime (to handle time changes)
                    old_dt = self.appointment.scheduled_datetime
                    self.appointment.scheduled_datetime = localized_dt
                    updated_fields.append('availability')
                    print(f"📅 Updated datetime: {old_dt} -> {localized_dt}")
                    
                except ValueError as e:
                    print(f"❌ Failed to parse AI datetime: {extracted_data['availability']}")
            
            # Customer name - only update if we don't have one and AI found one
            if (extracted_data.get('customer_name') and 
                extracted_data.get('customer_name') != 'null' and
                not self.appointment.customer_name):
                if self.is_valid_name(extracted_data['customer_name']):
                    self.appointment.customer_name = extracted_data['customer_name']
                    updated_fields.append('customer_name')
            
            # Save if anything was updated
            if updated_fields:
                self.appointment.save()
                print(f"💾 Updated appointment fields: {updated_fields}")
            
            return updated_fields
            
        except Exception as e:
            print(f"❌ Error updating appointment: {str(e)}")
            return []



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
        """Process when customer selects an alternative time - NEW FUNCTION"""
        try:
            message_lower = message.lower()
            
            # Extract time from common patterns
            time_patterns = [
                (r'(\d{1,2}):(\d{2})\s*(am|pm)', 'time_with_minutes'),
                (r'(\d{1,2})\s*(am|pm)', 'time_only'),
                (r'monday.*?(\d{1,2}):(\d{2})\s*(am|pm)', 'day_time_minutes'),
                (r'monday.*?(\d{1,2})\s*(am|pm)', 'day_time_only'),
            ]
            
            selected_datetime = None
            
            for pattern, pattern_type in time_patterns:
                match = re.search(pattern, message_lower)
                if match:
                    groups = match.groups()
                    
                    # Parse hour and minute
                    if 'minutes' in pattern_type:
                        hour = int(groups[0])
                        minute = int(groups[1])
                        am_pm = groups[2] if len(groups) > 2 else groups[-1]
                    else:
                        hour = int(groups[0])
                        minute = 0
                        am_pm = groups[1] if len(groups) > 1 else groups[-1]
                    
                    # Convert to 24-hour format
                    if am_pm == 'pm' and hour != 12:
                        hour += 12
                    elif am_pm == 'am' and hour == 12:
                        hour = 0
                    
                    # Use tomorrow's date (or next Monday if specified)
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    tomorrow = timezone.now().astimezone(sa_timezone) + timedelta(days=1)
                    
                    if 'monday' in message_lower:
                        # Find next Monday
                        days_ahead = (0 - tomorrow.weekday()) % 7
                        if days_ahead == 0:  # Today is Monday
                            days_ahead = 7  # Next Monday
                        target_date = tomorrow + timedelta(days=days_ahead)
                    else:
                        target_date = tomorrow
                    
                    # Create the datetime
                    selected_datetime = target_date.replace(
                        hour=hour, 
                        minute=minute, 
                        second=0, 
                        microsecond=0
                    )
                    
                    print(f"✅ Parsed alternative selection: {selected_datetime}")
                    break
            
            return selected_datetime
            
        except Exception as e:
            print(f"❌ Error processing alternative time selection: {str(e)}")
            return None


    def book_appointment_with_selected_time(self, selected_datetime):
        """Book appointment with specifically selected alternative time - NEW FUNCTION"""
        try:
            print(f"🔄 Booking appointment with selected time: {selected_datetime}")
            
            # Check availability for the SELECTED time, not original time
            is_available, conflict_info = self.check_appointment_availability(selected_datetime)
            
            if is_available:
                # Book the appointment
                self.appointment.scheduled_datetime = selected_datetime
                self.appointment.status = 'confirmed'
                self.appointment.save()
                
                print(f"✅ Appointment booked successfully: {selected_datetime}")
                
                return {
                    'success': True,
                    'datetime': selected_datetime.strftime('%B %d, %Y at %I:%M %p')
                }
            else:
                print(f"❌ Selected time not available: {conflict_info}")
                # Get new alternatives
                alternatives = self.get_alternative_time_suggestions(selected_datetime)
                
                return {
                    'success': False,
                    'error': 'Selected time not available',
                    'alternatives': alternatives
                }
                
        except Exception as e:
            print(f"❌ Error booking with selected time: {str(e)}")
            return {'success': False, 'error': str(e)}




    def extract_all_available_info_with_ai(self, message):
        """Extract ALL possible appointment information from any message - FIXED VERSION"""
        try:
            # Get current appointment state for context
            current_context = self.get_appointment_context()
            
            # Format current time properly
            current_time = timezone.now().strftime('%Y-%m-%d %H:%M')
            
            extraction_prompt = f"""
            You are a comprehensive data extraction assistant for a plumbing appointment system.
            
            CRITICAL: You MUST return ONLY a valid JSON object with no markdown formatting, code blocks, or extra text.
            
            TASK: Extract information from the customer's message and return ONLY what you can clearly identify.
            
            CURRENT APPOINTMENT STATE:
            {current_context}
            
            CUSTOMER MESSAGE: "{message}"
            
            EXTRACTION RULES:
            1. ONLY extract information that is CLEARLY present in the message
            2. PRESERVE existing information - do NOT set fields to null if they already have values
            3. Return ONLY a JSON object - no markdown, no explanations, no code blocks
            4. Use null only for fields where no information was found in this message
            
            EXTRACTION TARGETS:
            
            SERVICE TYPE - Look for:
            - Keywords: bathroom, kitchen, plumbing, installation, renovation, repair, toilet, shower, sink
            - Return: "bathroom_renovation", "kitchen_renovation", or "new_plumbing_installation"
            
            PLAN STATUS - Look for:
            - Keywords: have plan, got plan, existing plan, site visit, visit, assess, quote
            - Return: "has_plan" or "needs_visit"
            
            AREA/LOCATION - Look for:
            - Any location names, suburbs, areas mentioned
            - Return: the area name as stated
            
            TIMELINE - Look for:
            - When they want work done: ASAP, next week, next month, tomorrow, etc.
            - Return: timeline as stated
            
            PROPERTY TYPE - Look for:
            - Keywords: house, home, apartment, flat, business, office, commercial, shop, store
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
                    {"role": "system", "content": "You are a data extraction assistant. Return ONLY valid JSON with no formatting or explanations."},
                    {"role": "user", "content": extraction_prompt}
                ],
                temperature=0.1,
                max_tokens=200
            )
            
            ai_response = response.choices[0].message.content.strip()
            
            # FIXED: Clean up the response to handle markdown formatting
            ai_response = ai_response.replace('```json', '').replace('```', '').strip()
            
            # Parse AI response as JSON
            try:
                extracted_data = json.loads(ai_response)
                print(f"🤖 AI extracted data: {extracted_data}")
                return extracted_data
            except json.JSONDecodeError as e:
                print(f"❌ AI returned invalid JSON: {ai_response}")
                print(f"❌ JSON Parse Error: {str(e)}")
                return {}
                
        except Exception as e:
            print(f"❌ AI extraction error: {str(e)}")
            return {}





    def get_next_question_to_ask(self):
        """Determine which question to ask next - FIXED"""
        
        if not self.appointment.project_type:
            return "service_type"
        
        # FIXED: Check if has_plan has been answered (not just if it's False)
        if self.appointment.has_plan is None:  # ✅ Check for None, not False
            return "plan_or_visit"

        # If they have a plan, handle plan upload flow
        if self.appointment.has_plan is True:
            if not self.appointment.customer_area:
                return "area"
            if not self.appointment.property_type:
                return "property_type"
            if (self.appointment.customer_area and 
                self.appointment.property_type and 
                self.appointment.plan_status is None):
                return "initiate_plan_upload"
            if self.appointment.plan_status == 'pending_upload':
                return "awaiting_plan_upload"
            if self.appointment.plan_status == 'plan_uploaded':
                return "plan_with_plumber"

        # If they don't have a plan (False), continue normal flow
        if self.appointment.has_plan is False:  # ✅ Explicitly check for False
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
        """Check if we have enough information to attempt booking - FIXED"""
        required_for_booking = [
            self.appointment.project_type,
            self.appointment.has_plan is not None,  # ✅ Must be answered (True or False)
            self.appointment.customer_area,
            self.appointment.timeline,
            self.appointment.property_type,
            self.appointment.scheduled_datetime
        ]
        
        has_all_required = all(required_for_booking)
        missing_fields = []
        
        if not self.appointment.project_type:
            missing_fields.append("service type")
        if self.appointment.has_plan is None:  # ✅ Check for None
            missing_fields.append("plan preference")
        if not self.appointment.customer_area:
            missing_fields.append("area")
        if not self.appointment.timeline:
            missing_fields.append("timeline")
        if not self.appointment.property_type:
            missing_fields.append("property type")
        if not self.appointment.scheduled_datetime:
            missing_fields.append("availability")
        
        return {
            'ready_to_book': has_all_required,
            'missing_fields': missing_fields,
            'completion_percentage': ((6 - len(missing_fields)) / 6) * 100
        }


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


    def generate_contextual_response(self, incoming_message, next_question, updated_fields):
        """FIXED: Handle plan upload initiation properly"""
        try:
            # Check if we need to initiate plan upload
            if next_question == "initiate_plan_upload":
                return self.initiate_plan_upload_flow()
            
            # Check if we're awaiting plan upload
            if next_question == "awaiting_plan_upload":
                return "I'm waiting for your plan. Please send your images or PDF documents now."
            
            # Check if plan is with plumber
            if next_question == "plan_with_plumber":
                return "Your plan has been sent to our plumber. They'll contact you within 24 hours to discuss the project and provide a quote."
            
            # Get current state
            appointment_context = self.get_appointment_context()
            retry_count = getattr(self.appointment, 'retry_count', 0)
            is_retry = retry_count > 0
            
            # Build acknowledgment of received information
            acknowledgments = []
            if 'service_type' in updated_fields:
                service_display = self.appointment.project_type.replace('_', ' ').title()
                acknowledgments.append(f"service: {service_display}")
            
            if 'plan_status' in updated_fields:
                plan_text = "you have a plan" if self.appointment.has_plan else "you'd like a site visit"
                acknowledgments.append(f"plan status: {plan_text}")
            
            if 'area' in updated_fields:
                acknowledgments.append(f"area: {self.appointment.customer_area}")
            
            if 'property_type' in updated_fields:
                acknowledgments.append(f"property type: {self.appointment.property_type}")
            
            system_prompt = f"""
            You are Sarah, a professional appointment assistant for a luxury plumbing company.
            
            SITUATION ANALYSIS:
            - Customer provided new information: {updated_fields if updated_fields else 'None'}
            - Next question needed: {next_question}
            - Retry attempt: {retry_count}
            
            CURRENT APPOINTMENT STATE:
            {appointment_context}
            
            RESPONSE STRATEGY:
            1. Acknowledge any new information received
            2. Ask the next needed question naturally
            3. Keep it conversational and professional
            4. If this is a retry ({is_retry}), rephrase the question differently
            
            QUESTION TEMPLATES:
            - service_type: "Which service are you interested in? We offer: Bathroom Renovation, New Plumbing Installation, or Kitchen Renovation"
            - plan_or_visit: "Do you have a plan already, or would you like us to do a site visit?"
            - area: "Which area are you located in? (e.g. Harare Hatfield, Harare Avondale)"
            - timeline: "When were you hoping to get this done?"
            - property_type: "Is this for a house, apartment, or business?"
            - availability: "When would you be available for an appointment? Please provide both the day and time (e.g., Monday at 2pm, tomorrow at 10am)"
            - name: "To complete your booking, may I have your full name?"
            
            RESPONSE RULES:
            - Ask only the next needed question
            - Professional tone
            - Concise (1-2 sentences max)
            - No markdown formatting
            
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
            
            # Reset retry count if we successfully extracted new information
            if updated_fields:
                self.appointment.retry_count = 0
                self.appointment.save()
            else:
                # Increment retry count if no new info was extracted
                self.appointment.retry_count = getattr(self.appointment, 'retry_count', 0) + 1
                self.appointment.save()
            
            return reply
            
        except Exception as e:
            print(f"❌ Error generating contextual response: {str(e)}")
            return "I understand. Let me ask you about the next detail we need for your appointment."



    def smart_booking_check(self):
        """Check if we have enough information to attempt booking"""
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
        
        if not self.appointment.project_type:
            missing_fields.append("service type")
        if self.appointment.has_plan is None:
            missing_fields.append("plan preference")
        if not self.appointment.customer_area:
            missing_fields.append("area")
        if not self.appointment.timeline:
            missing_fields.append("timeline")
        if not self.appointment.property_type:
            missing_fields.append("property type")
        if not self.appointment.scheduled_datetime:
            missing_fields.append("availability")
        
        return {
            'ready_to_book': has_all_required,
            'missing_fields': missing_fields,
            'completion_percentage': ((6 - len(missing_fields)) / 6) * 100
        }

    def handle_early_datetime_provision(self, message):
        """Handle cases where customer provides date/time before we ask for availability"""
        try:
            # Extract datetime using existing method
            parsed_datetime = self.parse_datetime_with_ai(message)
            
            if parsed_datetime:
                # Store the datetime for later use
                self.appointment.scheduled_datetime = parsed_datetime
                self.appointment.save()
                
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
                self.appointment.save()
                
                # Get appointment details for response
                appointment_details = self.extract_appointment_details()
                
                # Add to calendar and notify team
                try:
                    self.add_to_google_calendar(appointment_details, self.appointment.scheduled_datetime)
                    self.notify_team(appointment_details, self.appointment.scheduled_datetime)
                except Exception as notify_error:
                    print(f"⚠️ Notification error: {notify_error}")
                
                # Generate confirmation message
                if self.appointment.customer_name:
                    return f"✅ Perfect! Your appointment is confirmed for {self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')}. Our team will contact you before arrival."
                else:
                    return f"Perfect! I've booked your appointment for {self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')}. To complete your booking, may I have your full name?"
            
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
        invalid_words = ['yes', 'no', 'ok', 'sure', 'thanks', 'hello', 'hi', 'good', 'fine']
        
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
        """ENHANCED: Fallback extraction with better property_type handling"""
        try:
            message_lower = message.lower()
            original_message = message.strip()
            next_question = self.get_next_question_to_ask()
            retry_count = getattr(self.appointment, 'retry_count', 0)
            
            # Be more generous on retries
            be_generous = retry_count > 0
            
            if next_question == "property_type" and not self.appointment.property_type:
                # Enhanced property type detection
                property_keywords = {
                    'house': ['house', 'home', 'residential'],
                    'apartment': ['apartment', 'flat', 'unit', 'complex'],
                    'business': ['business', 'commercial', 'office', 'shop', 'store', 'company']
                }
                
                # On retries, be more generous with keywords
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
            
            # Add other manual extraction logic here for other fields...
            # (keeping existing logic for service_type, area, etc.)
            
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
                self.appointment.save()
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


    def extract_appointment_details(self):
        """Extract customer details from conversation history"""
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


    def book_appointment(self, message):
        """Book an appointment using the correct datetime from AI extraction"""
        try:
            print(f"🔄 Starting appointment booking process...")
            
            # Use the stored datetime from AI extraction
            appointment_datetime = self.appointment.scheduled_datetime
            
            if not appointment_datetime:
                appointment_datetime = self.parse_datetime(message)
            
            if not appointment_datetime:
                print("❌ Could not get complete date/time - booking cancelled")
                return {'success': False, 'error': 'Incomplete date/time information'}

            print(f"📅 Using appointment time: {appointment_datetime}")

            # Ensure proper timezone handling
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            if appointment_datetime.tzinfo is None:
                appointment_datetime = sa_timezone.localize(appointment_datetime)
            else:
                appointment_datetime = appointment_datetime.astimezone(sa_timezone)

            print(f"📅 Timezone-corrected appointment time: {appointment_datetime}")

            # Check availability with the CORRECT time
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
            
            # ✅ ADD THIS: Extract appointment details
            appointment_details = self.extract_appointment_details()
            
            # ✅ ADD THIS: Send notifications
            try:
                print("📤 Sending team notifications...")
                self.notify_team(appointment_details, appointment_datetime)
                print("✅ Team notifications sent")
            except Exception as notify_error:
                print(f"⚠️ Notification error: {notify_error}")
            
            # ✅ ADD THIS: Add to calendar (optional)
            try:
                if GOOGLE_CALENDAR_CREDENTIALS:
                    self.add_to_google_calendar(appointment_details, appointment_datetime)
            except Exception as cal_error:
                print(f"⚠️ Calendar error: {cal_error}")
            
            return {
                'success': True,
                'datetime': appointment_datetime.strftime('%B %d, %Y at %I:%M %p')
            }

        except Exception as e:
            print(f"❌ Booking Error: {str(e)}")
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
        """Use AI to extract datetime from natural language - FIXED VERSION"""
        try:
            # Get current time in South Africa timezone
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            current_time = timezone.now().astimezone(sa_timezone)
            
            # Pre-format datetime strings to avoid f-string issues
            current_time_str = current_time.strftime('%A, %B %d, %Y at %I:%M %p')
            tomorrow_date_str = (current_time + timedelta(days=1)).strftime('%B %d, %Y')
            today_date_str = current_time.strftime('%B %d, %Y')
            
            datetime_extraction_prompt = f"""
            You are a datetime extraction assistant for appointment scheduling.
            
            TASK: Extract a complete date and time from the customer's message and convert it to YYYY-MM-DDTHH:MM format.
            
            CURRENT CONTEXT:
            - Current date/time: {current_time_str}
            - Timezone: Africa/Johannesburg (UTC+2)
            - Business hours: 8 AM - 6 PM, Monday to Friday
            
            CUSTOMER MESSAGE: "{message}"
            
            EXTRACTION RULES:
            1. Only return a complete datetime if BOTH date and time are clearly specified
            2. Handle relative terms correctly:
            - "tomorrow" = {tomorrow_date_str}
            - "today" = {today_date_str}
            - "next Monday" = next occurrence of Monday after today
            - "this Friday" = this week's Friday if not past, otherwise next Friday
            3. Handle time formats: "2pm" = 14:00, "10am" = 10:00, "2:30pm" = 14:30
            4. DO NOT adjust timezone - return local South Africa time
            5. Default minutes to 00 if not specified
            
            EXAMPLES:
            Input: "Can we do Monday at 2pm instead?" 
            Output: 2025-09-08T14:00 (if next Monday is Sep 8)
            
            Input: "How about tomorrow morning at 10?"
            Output: 2025-09-02T10:00 (tomorrow + 10am)
            
            Input: "Friday would be better"
            Output: PARTIAL_INFO (no time specified)
            
            Input: "2pm works"  
            Output: PARTIAL_INFO (no date specified)
            
            RESPONSE FORMAT:
            - If complete datetime found: Return YYYY-MM-DDTHH:MM
            - If partial information: Return "PARTIAL_INFO"
            - If no datetime info: Return "NOT_FOUND"
            
            MESSAGE: "{message}"
            EXTRACTED DATETIME:"""
            
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a precise datetime extraction assistant. Follow the format exactly."},
                    {"role": "user", "content": datetime_extraction_prompt}
                ],
                temperature=0.1,
                max_tokens=50
            )
            
            ai_response = response.choices[0].message.content.strip()
            
            if ai_response == "PARTIAL_INFO" or ai_response == "NOT_FOUND":
                print(f"AI datetime extraction: {ai_response}")
                return None
                
            # Try to parse the AI response as datetime
            try:
                # AI should return format: YYYY-MM-DDTHH:MM
                parsed_dt = datetime.strptime(ai_response, '%Y-%m-%dT%H:%M')
                localized_dt = sa_timezone.localize(parsed_dt)
                
                print(f"AI extracted datetime: {localized_dt}")
                return localized_dt
                
            except ValueError:
                print(f"AI returned invalid datetime format: {ai_response}")
                # Fallback to original parsing method
                return self.parse_datetime(message)
                
        except Exception as e:
            print(f"AI datetime extraction error: {str(e)}")
            return self.parse_datetime(message)



    def handle_unavailable_reschedule_with_ai(self, requested_datetime, original_message):
        """Use AI to generate response when requested time is unavailable"""
        try:
            # Get alternative suggestions
            alternatives = self.get_alternative_time_suggestions(requested_datetime)
            
            unavailable_response_prompt = f"""
            You are Sarah, a professional appointment assistant for a plumbing company.
            
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
                    {"role": "system", "content": "You are Sarah, a professional appointment assistant. Be helpful and concise."},
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
            You are Sarah, a professional appointment assistant for a plumbing company.
            
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
                    {"role": "system", "content": "You are Sarah, a professional appointment assistant. Be clear and helpful."},
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
            You are Sarah, a professional appointment assistant for a plumbing company.
            
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
                    {"role": "system", "content": "You are Sarah, a professional appointment assistant. Be reassuring and clear."},
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
            weekday = requested_datetime.weekday()  # 0=Monday, 6=Sunday
            if weekday >= 5:  # Saturday=5, Sunday=6
                print(f"Requested time is on weekend: weekday {weekday}")
                return False, "weekend"
            
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
                
                # Check for time overlap: appointments overlap if start1 < end2 AND start2 < end1
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
        """Get alternative available time slots near the requested time - UPDATED VERSION"""
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
#            if appointment_info.get('house_stage'):
#                description_parts.append(f"House Stage: {appointment_info['house_stage']}")
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
                    'dateTime': (appointment_datetime + datetime.timedelta(hours=2)).isoformat(),
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
            
            return event_result
            
        except Exception as e:
            print(f"Google Calendar Error: {str(e)}")
            return None

    def send_confirmation_message(self, appointment_info, appointment_datetime):
        """Send confirmation message to customer"""
        try:
            # Build service description
            service_name = "Plumbing service"
#            if appointment_info.get('project_type'):
#                service_map = {
#                    'bathroom': 'Bathroom Renovation',
#                    'installation': 'New Plumbing Installation', 
#                    'kitchen': 'Kitchen Renovation'
#                }
#                service_name = service_map.get(appointment_info['project_type'], service_name)
            
            confirmation_message = f"""🔧 APPOINTMENT CONFIRMED! 🔧

Hi {appointment_info.get('name', 'there')},

Your plumbing appointment is confirmed:
📅 Date: {appointment_datetime.strftime('%A, %B %d, %Y')}
🕐 Time: {appointment_datetime.strftime('%I:%M %p')}
📍 Area: {appointment_info.get('area', 'Your area')}
🔨 Service: {appointment_info.get('project_type','Plumbing Service')}

Our team will contact you before arrival. 

Questions? Reply to this message or call (555) PLUMBING.

Thank you for choosing us.
- Sarah & team"""

            twilio_client.messages.create(
                body=confirmation_message,
                from_=TWILIO_WHATSAPP_NUMBER,
                to=self.phone_number
            )
            
        except Exception as e:
            print(f"Confirmation message error: {str(e)}")

    def notify_team(self, appointment_info, appointment_datetime):
        """Notify team about new appointment - FIXED VERSION"""
        try:
            # Get service name with fallback
            service_name = appointment_info.get('project_type', 'Plumbing service')
            if service_name:
                service_map = {
                    'bathroom_renovation': 'Bathroom Renovation',
                    'new_plumbing_installation': 'New Plumbing Installation',
                    'kitchen_renovation': 'Kitchen Renovation'
                }
                service_name = service_map.get(service_name, service_name.replace('_', ' ').title())
            
            # Plan status for team
            plan_status = "Not specified"
            if appointment_info.get('has_plan') is not None:
                plan_status = "Has existing plan" if appointment_info['has_plan'] else "Needs site visit"
            
            # Format the team notification message
            team_message = f"""🚨 NEW APPOINTMENT BOOKED!

    Customer: {appointment_info.get('name', 'Unknown')}
    Phone: {self.phone_number.replace('whatsapp:', '')}
    Date/Time: {appointment_datetime.strftime('%A, %B %d at %I:%M %p')}
    Area: {appointment_info.get('area', 'Not provided')}
    Service: {service_name}
    Property: {appointment_info.get('property_type', 'Not specified')}
    Timeline: {appointment_info.get('timeline', 'Not specified')}
    Plan Status: {plan_status}

    View appointment: https://plumbotv1-production.up.railway.app/appointments/{self.appointment.id}/

    Check calendar for details."""

            # Team numbers to notify
            TEAM_NUMBERS = [
                'whatsapp:+263774819901',  # Your plumber's number
            ]
            
            print(f"📤 Attempting to send notifications to {len(TEAM_NUMBERS)} team members...")
            
            sent_count = 0
            for number in TEAM_NUMBERS:
                try:
                    message = twilio_client.messages.create(
                        body=team_message,
                        from_=TWILIO_WHATSAPP_NUMBER,
                        to=number
                    )
                    print(f"✅ Team notification sent successfully to {number}. Message SID: {message.sid}")
                    sent_count += 1
                except Exception as msg_error:
                    print(f"❌ Failed to send team notification to {number}: {str(msg_error)}")
            
            if sent_count > 0:
                print(f"✅ Successfully sent {sent_count} team notifications")
            else:
                print(f"❌ Failed to send any team notifications")
                    
        except Exception as e:
            print(f"❌ Team notification error: {str(e)}")
            import traceback
            print(traceback.format_exc())
            
    def send_reminder_message(appointment, reminder_type):
        """Send reminder message based on reminder type"""
        try:
            # Get customer name or default to "there"
            customer_name = appointment.customer_name or "there"
            
            # Format appointment datetime
            appt_date = appointment.scheduled_datetime.strftime('%A, %B %d, %Y')
            appt_time = appointment.scheduled_datetime.strftime('%I:%M %p')
            
            # Get service name
#            service_name = "Plumbing service"
#            if appointment.project_type:
#                service_map = {
#                    'bathroom_renovation': 'Bathroom Renovation',
#                    'new_plumbing_installation': 'New Plumbing Installation',
#                    'kitchen_renovation': 'Kitchen Renovation'
#                }
#                service_name = service_map.get(appointment.project_type, service_name)
            
            # Create reminder messages based on type
            if reminder_type == '1_day':
                message = f"""🔧 APPOINTMENT REMINDER

    Hi {customer_name},

    Just a friendly reminder about your plumbing appointment:

    📅 Tomorrow: {appt_date}
    🕐 Time: {appt_time}
    📍 Area: {appointment.customer_area or 'Your location'}
    🔨 Service: {service_name}

    Our team will contact you before arrival to confirm timing.

    Need to reschedule? Reply to this message or call (555) PLUMBING.

    See you tomorrow!
    - Sarah & team"""

            elif reminder_type == 'morning':
                message = f"""🌅 GOOD MORNING REMINDER

    Hi {customer_name},

    Today's your plumbing appointment:

    📅 Today: {appt_date}
    🕐 Time: {appt_time}
    📍 Area: {appointment.customer_area or 'Your location'}
    🔨 Service: {service_name}

    Our team will call you 30 minutes before arrival.

    Questions? Reply here or call (555) PLUMBING.

    Looking forward to serving you today!
    - Sarah & team"""

            elif reminder_type == '2_hours':
                message = f"""⏰ APPOINTMENT IN 2 HOURS

    Hi {customer_name},

    Your plumbing appointment is coming up:

    🕐 In 2 hours: {appt_time}
    📍 Area: {appointment.customer_area or 'Your location'}
    🔨 Service: {service_name}

    Our team will call you in about 30 minutes to confirm arrival time.

    Please ensure someone is available at the location.

    Questions? Reply here or call (555) PLUMBING.

    - Sarah & team"""

            else:
                return False

            # Send the reminder message
            twilio_client.messages.create(
                body=message,
                from_=TWILIO_WHATSAPP_NUMBER,
                to=appointment.phone_number
            )
            
            print(f"✅ {reminder_type} reminder sent to {appointment.phone_number}")
            return True
        
        except Exception as e:
            print(f"❌ Failed to send {reminder_type} reminder to {appointment.phone_number}: {str(e)}")
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

@csrf_exempt  
def bot(request):
    """Enhanced webhook handler for both text and media"""
    if request.method == 'POST':
        try:
            incoming_msg = request.POST.get('Body', '').strip()
            sender = request.POST.get('From', '')
            num_media = int(request.POST.get('NumMedia', 0))
            
            print(f"📨 Received from {sender}: Message='{incoming_msg}', Media={num_media}")
            
            # Handle media files first
            if num_media > 0:
                media_result = handle_whatsapp_media(request)
                # If there's also a text message, continue processing
                if not incoming_msg:
                    return media_result
            
            # Continue with text processing
            if not incoming_msg or not sender:
                return HttpResponse(status=200)
            
            # Initialize Plumbot and generate response
            plumbot = Plumbot(sender)
            
            # Log current state
            before_state = plumbot.get_information_summary()
            print(f"📊 State before processing: {before_state}")
            
            # Generate response
            reply = plumbot.generate_response(incoming_msg)
            
            # Log state after
            after_state = plumbot.get_information_summary()
            print(f"📊 State after processing: {after_state}")
            
            print(f"🤖 Generated reply: {reply}")

            # Send reply
            try:
                customer_message = twilio_client.messages.create(
                    body=reply,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=sender
                )
                print(f"✅ Reply sent. SID: {customer_message.sid}")
            except Exception as send_error:
                print(f"❌ Failed to send reply: {str(send_error)}")
                
            return HttpResponse(status=200)
            
        except Exception as e:
            print(f"❌ Webhook Error: {str(e)}")
            return HttpResponse(status=500)
            
    return HttpResponse(status=405)



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
        updated_fields = plumbot.update_appointment_with_extracted_data(extracted)
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
            print(f"⚠️  Invalid format for {number}. Should be 'whatsapp:+27XXXXXXXXX'")
        else:
            print(f"✅ {number} format is correct")
    
    print("\n🔧 TROUBLESHOOTING TIPS:")
    print("1. Make sure the plumber's number is registered with WhatsApp")
    print("2. The plumber must have previously messaged your Twilio WhatsApp number")
    print("3. Check Twilio console for delivery status")
    print("4. Verify the phone number format: whatsapp:+27XXXXXXXXX")
    
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
    """Download media from Twilio and save to Django storage"""
    try:
        # Authenticate with Twilio to download media
        auth = (ACCOUNT_SID, AUTH_TOKEN)
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