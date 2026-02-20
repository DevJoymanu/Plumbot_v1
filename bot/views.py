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
from .whatsapp_cloud_api import whatsapp_api

import logging
logger = logging.getLogger(__name__)




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
        quotation = Quotation.objects.create(
            appointment=appointment,  # This is now guaranteed to exist
            labor_cost=data.get('labour_cost', 0),
            transport_cost=data.get('transport_cost', 0),
            materials_cost=data.get('materials_cost', 0),
            notes=data.get('notes', ''),
            status='draft'
        )
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
                    # Parse string into datetime object
                    dt = datetime.strptime(job_datetime, "%Y-%m-%d %H:%M")
                    # Make timezone aware
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    appointment.job_scheduled_datetime = sa_timezone.localize(dt)
            else:
                #s
                scheduled_datetime = request.POST.get('scheduled_datetime')
                if scheduled_datetime:
                    dt = datetime.fromisoformat(scheduled_datetime)
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    if dt.tzinfo is None:
                        dt = sa_timezone.localize(dt)
                    appointment.scheduled_datetime = dt
                                
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
    
    # Get statistics
    total_active_leads = Appointment.objects.filter(
        is_lead_active=True,
        status='pending'
    ).count()
    
    # Leads by follow-up stage
    stage_counts = {}
    for stage_code, stage_name in Appointment._meta.get_field('followup_stage').choices:
        count = Appointment.objects.filter(
            is_lead_active=True,
            followup_stage=stage_code
        ).count()
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
    
    # Filter to those actually ready for follow-up
    ready_for_followup = [
        lead for lead in leads_needing_followup 
        if lead.should_send_followup_now()
    ]
    
    # Recent responses (last 7 days)
    recent_responses = Appointment.objects.filter(
        last_customer_response__gte=now - timedelta(days=7),
        is_lead_active=True
    ).order_by('-last_customer_response')[:10]
    
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

    #
    def generate_response(self, incoming_message):
        """Check service inquiries ONLY when not mid-conversation."""
        try:
            current_question = self.get_next_question_to_ask()

            mid_conversation = (
                self.appointment.project_type is not None
            )

            if not mid_conversation:
                inquiry = self.detect_service_inquiry(incoming_message)
                if inquiry.get('intent') != 'none' and inquiry.get('confidence') == 'HIGH':
                    print(f"💡 Handling service inquiry: {inquiry['intent']}")
                    reply = self.handle_service_inquiry(inquiry['intent'], incoming_message)
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
            updated_fields = self.update_appointment_with_extracted_data(extracted_data)
            
            # STEP 4: Check for reschedule requests (for confirmed appointments)
            if (self.appointment.status == 'confirmed' and 
                self.appointment.scheduled_datetime and 
                self.detect_reschedule_request_with_ai(incoming_message)):
                
                print("🤖 AI detected reschedule request, handling...")
                reschedule_response = self.handle_reschedule_request_with_ai(incoming_message)
                
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
                self.appointment.has_plan is False):
                
                booking_result = self.book_appointment(incoming_message)
                
                #
                if booking_result['success']:
                    reply = f"Perfect! I've booked your appointment for {booking_result['datetime']}. To complete your booking, may I have your full name?"
                else:
                    error = booking_result.get('error', '')
                    alternatives = booking_result.get('alternatives', [])
                    
                    # ✅ Saturday-specific message
                    if 'saturday' in error.lower() or not alternatives:
                        alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives]) if alternatives else ""
                        reply = (
                            "We unfortunately don't operate on Saturdays. 😊\n\n"
                            "Our working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                        )
                        if alt_text:
                            reply += f"Here are some available slots:\n{alt_text}\n\nOr feel free to suggest a different date and time!"
                        else:
                            reply += "Could you please suggest a different date and time that works for you?"
                    else:
                        alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                        reply = f"That time isn't available either. Here are some other options:\n{alt_text}\n\nWhich works better for you?"
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
                self.appointment.save()
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
                '+27610318200'
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
    If you need to reach them directly: {plumber_number.replace('+27', '0').replace('+', '')}

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

    INTENTS:
    - tub_sales: asking if we sell tubs or about small bathroom tubs
    - standalone_tub: asking about standalone/freestanding tub price or availability
    - geyser: asking about geyser installation or pricing
    - shower_cubicle: asking about shower cubicles, pricing, installation
    - vanity: asking about vanity units, custom vanity
    - bathtub_installation: asking about installing a bathtub, wall finishing around tub
    - toilet: asking about toilet supply or installation
    - chamber: asking about side chamber, chamber supply or installation   # <-- ADD THIS
    - facebook_package: referencing a Facebook ad or package deal
    - location_ask: customer is ONLY asking where we are located or for our address
    - location_visit: customer wants to physically come IN PERSON to our office or showroom
    - previous_quotation: saying we sent them a quotation before
    - pictures: asking to see product pictures (not previous work photos)
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
    - HIGH = message clearly matches the intent
    - LOW = message is ambiguous or too short to be certain

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

            pricing_info = {
                "tub_sales": {
                    "en": f"We don't operate as a retail store, but we can supply and install tubs as part of a renovation project. 🛁 Standalone tubs start from US$400 depending on the design and quality.\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWould you like us to assist with supply and installation? 😊",
                    "sn": f"Hatitengesi setshopu, asi tinogona kuita supply neinstallation yetub sebhizimisi rekuvandudza imba yako. 🛁 Tubs dzinotangira kuUS$400 zvichienda nedhizaini nequality.\n\n⚠️ Mitengo iyi yakangofanana - inogona kushanduka zvichienda nebasa riri pasite. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nUnoda here kuti tikubatsire? 😊"
                },
                "standalone_tub": {
                    "en": f"Standalone tubs start from US$400, depending on the design and quality. 🛁\n\nFor bathtub installation:\n• Ordinary tub (with wall finishing) starts from US$80\n• Free-standing tub supply starts from US$450\n• Free-standing mixer starts from US$150\n• Mixer installation: US$120\n• Side chamber: US$130 (installation US$30)\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWould you like installation included as well? 🔧✨",
                    "sn": f"Tubs dzinomira dzega dzinotangira kuUS$400, zvichienda nedhizaini nequality. 🛁\n\nNeinstallation:\n• Tub yakajairwa (ine wall finishing) inotangira kuUS$80\n• Free-standing tub inotangira kuUS$450\n• Free-standing mixer: US$150\n• Kuisa mixer: US$120\n• Side chamber: US$130 (installation US$30)\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nUnoda here installation zvakare? 🔧"
                },
                "geyser": {
                    "en": f"Yes, we do geyser installations! 🔥\n\nGeyser installation starts from US$80, depending on the type and size of the geyser.\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWhat size geyser are you installing?",
                    "sn": f"Hongu, tinoisa ma geyser! 🔥\n\nKuisa geyser kunotangira kuUS$80, zvichienda nemhando neukuru hwegeyser.\n\n⚠️ Mutengo uyu wakangofanana. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nGeyser yekuisa yakura zvakadini?"
                },
                "shower_cubicle": {
                    "en": f"We supply and install shower cubicles! 🚿\n\n• Ordinary shower cubicle (900mm x 900mm): starts from US$130\n• Installation: starts from US$40\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWould you like supply and installation together?",
                    "sn": f"Tinopa uye tinoisa ma shower cubicle! 🚿\n\n• Shower cubicle yakajairwa (900mm x 900mm): inotangira kuUS$130\n• Kuisa: inotangira kuUS$40\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nUnoda here supply neinstallation pamwechete?"
                },
                "vanity": {
                    "en": f"Yes, we do custom-made vanity units! 🪞\n\n• Vanity units start from US$150 (depending on size, type, and material)\n• Labour starts from US$30\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWhat size are you looking for?",
                    "sn": f"Hongu, tinoita ma vanity unit akagadzirwa zvaunoda! 🪞\n\n• Ma vanity unit anotangira kuUS$150 (zvichienda nekukura, mhando, nesimba resimbi)\n• Kubhadhara vashandi kunotangira kuUS$30\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nUnoda ukuru wakaita sei?"
                },
                "bathtub_installation": {
                    "en": f"Here are our bathtub installation prices: 🛁\n\n• Ordinary tub installation (with wall finishing): from US$80\n• Free-standing tub supply: from US$450\n• Free-standing mixer: from US$150\n• Mixer installation: US$120\n• Side chamber: US$130\n• Side chamber installation: US$30\n\n⚠️ These are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWhat type of bathtub are you interested in?",
                    "sn": f"Mitengo yedu yekuisa ma bathtub: 🛁\n\n• Tub yakajairwa (ine wall finishing): kubva kuUS$80\n• Free-standing tub: kubva kuUS$450\n• Free-standing mixer: kubva kuUS$150\n• Kuisa mixer: US$120\n• Side chamber: US$130\n• Kuisa side chamber: US$30\n\n⚠️ Mitengo iyi yakangofanana. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nUnoda mhando ipi yebathtub?"
                },
                "toilet": {
                    "en": f"We supply and install toilets and side chambers! \n\n• Close-coupled toilet supply: starts from US$50\n• New toilet seat installation: starts from US$20 (depending on type)\n• Side chamber: US$130\n• Side chamber installation: US$30\n\nThese are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo or schedule a site visit.\n\nWould you like supply and installation?",
                    "sn": f"Tinopa uye tinoisa ma toilet nema side chamber! \n\n• Close-coupled toilet: inotangira kuUS$50\n• Kuisa chigaro chitsva chetoilet: inotangira kuUS$20 (zvichienda nemhando)\n• Side chamber: US$130\n• Kuisa side chamber: US$30\n\nMitengo iyi yakangofanana. Tumira photo kana plan kana tibvumirene visit.\n\nUnoda here supply neinstallation?",
                },
                "chamber": {
                    "en": f"We supply and install side chambers and toilets! \n\n• Side chamber: US$130\n• Side chamber installation: US$30\n• Close-coupled toilet supply: starts from US$50\n• New toilet seat installation: starts from US$20 (depending on type)\n\nThese are approximate prices and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo or schedule a site visit.\n\nWould you like supply and installation?",
                    "sn": f"Tinopa uye tinoisa ma side chamber nema toilet! \n\n• Side chamber: US$130\n• Kuisa side chamber: US$30\n• Close-coupled toilet: inotangira kuUS$50\n• Kuisa chigaro chitsva chetoilet: inotangira kuUS$20\n\nMitengo iyi yakangofanana. Tumira photo kana plan kana tibvumirene visit.\n\nUnoda here supply neinstallation?",
                },
                "facebook_package": {
                    "en": f"The bathroom package shown on our Facebook ad starts from US$600. 📢\n\n⚠️ This is an approximate price and may vary depending on the scope of work on site. For a more accurate quotation, please send a plan/photo 📸 or schedule a site visit.\n\nWould you like us to assess your space first?",
                    "sn": f"Package yebathroom yatakaiswa pa Facebook inotangira kuUS$600. 📢\n\n⚠️ Mutengo uyu wakangofanana - unogona kushanduka zvichienda nebasa. Tumira photo kana plan 📸 kana tibvumirene visit.\n\nUnoda here kuti tiuye titarise nzvimbo yako?"
                },
                # ✅ SPLIT: location_ask = just asking address, location_visit = wants to come in person
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
                    "en": f"I'll connect you with our plumber so they can send you available pictures and options. 📸\n\nPlease contact them directly on: {plumber_number}",
                    "sn": f"Ndichakubatanidza neplumber wedu kuti vakutumire mifananidzo uye zvinosarudzwa. 📸\n\nBata: {plumber_number}"
                },
            }
            responses = pricing_info.get(intent, pricing_info.get('toilet', {}))
            
            # Select response based on language
            responses = pricing_info.get(intent, {})
            if language == 'shona':
                reply = responses.get('sn', responses.get('en', ''))
            else:
                reply = responses.get('en', '')

            # If no specific response, generate one with DeepSeek
            if not reply:
                reply = self.generate_contextual_response(message, self.get_next_question_to_ask(), [])

            return reply

        except Exception as e:
            print(f"❌ Error handling service inquiry: {str(e)}")
            return self.generate_contextual_response(message, self.get_next_question_to_ask(), [])

    def generate_pricing_overview(self, message):
        """Send approximate prices when customer asks about cost"""
        # Try to detect specific service first
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

    Which service are you interested in?"""

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
                '27610318200',  # ✅ international format
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
            
            retry_count = getattr(self.appointment, 'retry_count', 0)
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
    def update_appointment_with_extracted_data(self, extracted_data):
        """Update appointment with extracted data - FIXED VERSION"""
        try:
            updated_fields = []
            next_question = self.get_next_question_to_ask()
            
            print(f"🔄 Updating appointment - Current question: {next_question}")
            print(f"📦 Extracted data: {extracted_data}")
            
            # Service type - only update if we don't have one and AI found one
            if (extracted_data.get('service_type') and 
                extracted_data.get('service_type') != 'null' and
                not self.appointment.project_type):
                self.appointment.project_type = extracted_data['service_type']
                updated_fields.append('service_type')
                print(f"✅ Updated service_type: {self.appointment.project_type}")
            
            # EMERGENCY FIX: Plan status with response normalization
            if extracted_data.get('plan_status') and extracted_data.get('plan_status') != 'null':
                
                # SAFETY CHECK 1: Never update if already set
                if self.appointment.has_plan is not None:
                    print(f"🛡️ SAFETY: Blocked plan_status update - already set to {self.appointment.has_plan}")
                
                # SAFETY CHECK 2: Only update if we're actually asking about it
                elif next_question != "plan_or_visit":
                    print(f"🛡️ SAFETY: Blocked plan_status update - not currently asking about plans (question: {next_question})")
                
                # SAFETY CHECK 3: Normalize and validate response
                else:
                    old_value = self.appointment.has_plan
                    plan_status = str(extracted_data['plan_status']).lower().strip()
                    
                    # Map ALL possible AI responses to boolean
                    # Handles: 'has_plan', 'yes', 'true', 'no', 'false', 'needs_visit'
                    has_plan_indicators = [
                        'has_plan', 'has plan', 'have plan', 'got plan',
                        'yes', 'yep', 'yeah', 'yup', 'true',
                        'have it', 'got it', 'i do', 'i have'
                    ]
                    
                    needs_visit_indicators = [
                        'needs_visit', 'needs visit', 'need visit', 
                        'site visit', 'site_visit',
                        'no', 'nope', 'nah', 'false',
                        'no plan', 'dont have', "don't have",
                        'visit', 'prefer visit'
                    ]
                    
                    updated = False
                    
                    # Check if response indicates HAS plan
                    if any(indicator in plan_status for indicator in has_plan_indicators):
                        self.appointment.has_plan = True
                        updated_fields.append('plan_status')
                        updated = True
                        print(f"✅ Updated plan status: {old_value} -> True (matched: {plan_status})")
                    
                    # Check if response indicates NEEDS visit
                    elif any(indicator in plan_status for indicator in needs_visit_indicators):
                        self.appointment.has_plan = False
                        updated_fields.append('plan_status')
                        updated = True
                        print(f"✅ Updated plan status: {old_value} -> False (matched: {plan_status})")
                    
                    # Unrecognized response
                    else:
                        print(f"⚠️ WARNING: Unrecognized plan_status value: '{plan_status}'")
                        print(f"Expected variants of: has_plan/needs_visit or yes/no")
                        print(f"Bot will ask the question again with different phrasing")
            
            # Area - only update if we don't have one and AI found one
            if (extracted_data.get('area') and 
                extracted_data.get('area') != 'null' and
                not self.appointment.customer_area):
                self.appointment.customer_area = extracted_data['area']
                updated_fields.append('area')
                print(f"✅ Updated area: {self.appointment.customer_area}")
            
            # Timeline - only update if we don't have one and AI found one
            # Timeline - only update if we don't have one and AI found one
            if (extracted_data.get('timeline') and 
                extracted_data.get('timeline') != 'null' and
                not self.appointment.timeline):
                
                timeline_value = extracted_data['timeline']
                
                # ✅ Block Saturday from being stored as a timeline
                saturday_indicators = ['saturday', 'sat']
                if any(s in timeline_value.lower() for s in saturday_indicators):
                    print(f"⚠️ Blocked Saturday as timeline value: '{timeline_value}'")
                    extracted_data['timeline'] = None  # Don't save it
                else:
                    self.appointment.timeline = timeline_value
                    updated_fields.append('timeline')
                    print(f"✅ Updated timeline: {self.appointment.timeline}")
            
            # Property type - only update if we don't have one and AI found one
            if (extracted_data.get('property_type') and 
                extracted_data.get('property_type') != 'null' and
                not self.appointment.property_type):
                self.appointment.property_type = extracted_data['property_type']
                updated_fields.append('property_type')
                print(f"✅ Updated property_type: {self.appointment.property_type}")
            
            # Availability/DateTime
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
                    
                    # Auto-fill timeline if still missing
                    if not self.appointment.timeline:
                        self.appointment.timeline = localized_dt.strftime('%A, %B %d')
                        updated_fields.append('timeline')
                        print(f"✅ Auto-filled timeline from datetime: {self.appointment.timeline}")
                        
                except ValueError as e:
                    print(f"❌ Failed to parse AI datetime: {extracted_data['availability']} — {e}")
            
            # Customer name - only update if we don't have one and AI found one
            if (extracted_data.get('customer_name') and 
                extracted_data.get('customer_name') != 'null' and
                not self.appointment.customer_name):
                if self.is_valid_name(extracted_data['customer_name']):
                    self.appointment.customer_name = extracted_data['customer_name']
                    updated_fields.append('customer_name')
                    print(f"✅ Updated customer_name: {self.appointment.customer_name}")
            
            # Save if anything was updated
            if updated_fields:
                self.appointment.save()
                print(f"💾 Saved appointment with updated fields: {updated_fields}")
            else:
                print(f"ℹ️ No fields were updated")
            
            return updated_fields
            
        except Exception as e:
            print(f"❌ Error updating appointment: {str(e)}")
            import traceback
            traceback.print_exc()
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
                return None

            parsed_dt = datetime.strptime(ai_response, '%Y-%m-%dT%H:%M')
            localized_dt = sa_timezone.localize(parsed_dt)
            print(f"✅ Parsed alternative selection: {localized_dt}")
            return localized_dt

        except Exception as e:
            print(f"❌ DeepSeek alternative selection error: {e}")
            return None

    def book_appointment_with_selected_time(self, selected_datetime):
        """Book appointment with specifically selected alternative time"""
        try:
            print(f"🔄 Booking appointment with selected time: {selected_datetime}")
            
            is_available, conflict_info = self.check_appointment_availability(selected_datetime)
            
            if is_available:
                self.appointment.scheduled_datetime = selected_datetime
                self.appointment.status = 'confirmed'
                self.appointment.save()
                
                #
                print(f"✅ Appointment booked successfully: {selected_datetime}")
                
                # ✅ Send notifications immediately at booking time
                appointment_details = self.extract_appointment_details()
                try:
                    self.send_confirmation_message(appointment_details, selected_datetime)
                except Exception as e:
                    print(f"⚠️ Confirmation message error: {e}")
                try:
                    self.notify_team(appointment_details, selected_datetime)
                except Exception as e:
                    print(f"⚠️ Team notification error: {e}")
                
                display_datetime = self.format_datetime_for_display(selected_datetime)
                return {
                    'success': True,
                    'datetime': display_datetime.strftime('%B %d, %Y at %I:%M %p')
                }
            else:
                print(f"❌ Selected time not available: {conflict_info}")
                alternatives = self.get_alternative_time_suggestions(selected_datetime)
                
                return {
                    'success': False,
                    'error': 'Selected time not available',
                    'alternatives': alternatives
                }
                
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
            - Keywords: bathroom, kitchen, plumbing, installation, renovation, repair, toilet, shower, sink
            - Return: "bathroom_renovation", "kitchen_renovation", or "new_plumbing_installation"
            
            
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
            
            NO indicators (customer needs site visit):
            - Direct: "no", "nope", "don't have", "no plan", "need visit", "site visit"
            
            IF IN DOUBT: Return null (better to ask again than assume wrong answer)
            
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
        """Determine which question to ask next - FIXED for early uploads"""
        
        if not self.appointment.project_type:
            return "service_type"
        
        # FIXED: Skip plan question if customer already uploaded plan early
        if self.appointment.has_plan is None:
            # Only ask if they haven't uploaded anything yet
            if not self.appointment.plan_file:
                return "plan_or_visit"
            else:
                # They uploaded early - skip to next question
                print(f"⏭️ Skipping plan question - customer already uploaded plan")
                self.appointment.has_plan = True  # Mark as having plan
                self.appointment.save()
        
        # If they have a plan, IMMEDIATELY ask them to send it before anything else
        if self.appointment.has_plan is True:
            # Plan not yet uploaded - ask for it RIGHT NOW, before area/property questions
            if not self.appointment.plan_file and self.appointment.plan_status not in ('plan_uploaded', 'plan_reviewed', 'ready_to_book'):
                return "initiate_plan_upload"
            
            # Plan uploaded but awaiting completion confirmation
            if self.appointment.plan_status == 'pending_upload' and self.appointment.plan_file:
                return "awaiting_plan_upload"
            
            # Plan already with plumber
            if self.appointment.plan_status == 'plan_uploaded':
                return "plan_with_plumber"
            
            # Plan done - now collect any remaining info
            if not self.appointment.customer_area:
                return "area"
            if not self.appointment.property_type:
                return "property_type"

        # If they don't have a plan (False), continue normal flow
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
                self.appointment.save()
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


    def send_confirmation_message(self, appointment_info, appointment_datetime):
        """Send confirmation message to customer - FIXED TIMEZONE"""
        try:
            # FIX: Ensure datetime is in correct timezone for display
            display_datetime = self.format_datetime_for_display(appointment_datetime)
            
            service_name = appointment_info.get('project_type', 'Plumbing service')
            if service_name:
                service_map = {
                    'bathroom_renovation': 'Bathroom Renovation',
                    'new_plumbing_installation': 'New Plumbing Installation',
                    'kitchen_renovation': 'Kitchen Renovation'
                }
                service_name = service_map.get(service_name, service_name.replace('_', ' ').title())
            
            confirmation_message = f"""🔧 APPOINTMENT CONFIRMED! 🔧

    Hi {appointment_info.get('name', 'there')},

    Your plumbing appointment is confirmed:
    📅 Date: {display_datetime.strftime('%A, %B %d, %Y')}
    🕐 Time: {display_datetime.strftime('%I:%M %p')}
    📍 Area: {appointment_info.get('area', 'Your area')}
    🔨 Service: {service_name}

    Our team will contact you before arrival. 

    Questions? Reply to this message.

    Thank you for choosing us.
    - Homebase Plumbers"""

            clean_phone = clean_phone_number(self.phone_number)
            whatsapp_api.send_text_message(clean_phone, confirmation_message)
            print(f"✅ Confirmation sent to {clean_phone}")
            
        except Exception as e:
            print(f"❌ Confirmation message error: {str(e)}")


    # ALSO UPDATE YOUR notify_team METHOD:

    def notify_team(self, appointment_info, appointment_datetime):
        """Notify team about new appointment - immediately via WhatsApp Cloud API"""
        try:
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

            # Generate AI conversation summary
            from bot.whatsapp_webhook import generate_conversation_summary
            ai_summary = generate_conversation_summary(self.appointment)

            customer_phone = self.phone_number.replace('whatsapp:+', '').replace('whatsapp:', '').replace('+', '')

            team_message = (
                f"🚨 NEW APPOINTMENT BOOKED!\n\n"
                f"Customer: {appointment_info.get('name', 'Unknown')}\n"
                f"Phone: +{customer_phone}\n"
                f"WhatsApp: wa.me/{customer_phone}\n\n"
                f"📋 APPOINTMENT DETAILS:\n"
                f"  Date/Time: {display_datetime.strftime('%A, %B %d at %I:%M %p')}\n"
                f"  Service: {service_name}\n"
                f"  Area: {appointment_info.get('area', 'Not provided')}\n"
                f"  Property: {appointment_info.get('property_type', 'Not specified')}\n"
                f"  Timeline: {appointment_info.get('timeline', 'Not specified')}\n"
                f"  Plan Status: {plan_status}\n\n"
                f"🤖 AI CONVERSATION SUMMARY:\n{ai_summary}\n\n"
                f"🔗 View full appointment:\n"
                f"https://plumbotv1-production.up.railway.app/appointments/{self.appointment.id}/"
            )

            TEAM_NUMBERS = ['27610318200']

            print(f"📤 Sending booking notifications to {len(TEAM_NUMBERS)} team members...")

            sent_count = 0
            for number in TEAM_NUMBERS:
                try:
                    whatsapp_api.send_text_message(number, team_message)
                    print(f"✅ Booking notification sent to {number}")
                    sent_count += 1
                except Exception as msg_error:
                    print(f"❌ Failed to send booking notification to {number}: {str(msg_error)}")

            if sent_count > 0:
                print(f"✅ Successfully sent {sent_count} booking notifications")

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
            
            #
            if next_question == "plan_or_visit" and retry_count > 0:
                # Customer's previous response was unclear - use AI to rephrase
                clarifying_question = self.generate_clarifying_question_for_plan_status(retry_count)
                return clarifying_question


            if 'plan_status' in updated_fields:
                plan_text = "you have a plan" if self.appointment.has_plan else "you'd like a site visit"
                acknowledgments.append(f"plan status: {plan_text}")
            
            if 'area' in updated_fields:
                acknowledgments.append(f"area: {self.appointment.customer_area}")
            
            if 'property_type' in updated_fields:
                acknowledgments.append(f"property type: {self.appointment.property_type}")
            
            # ✅ Check if customer is suggesting Saturday for their timeline/availability
            saturday_indicators = ['saturday', 'sat']
            if any(s in incoming_message.lower() for s in saturday_indicators):
                alternatives = self.get_alternative_time_suggestions(
                    timezone.now() + timedelta(days=1)
                )
                alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives]) if alternatives else ""
                
                reply = (
                    "We unfortunately don't operate on Saturdays. 😊\n\n"
                    "Our working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                )
                if alt_text:
                    reply += f"Here are some available slots:\n{alt_text}\n\nOr feel free to suggest a different date and time!"
                else:
                    reply += "Could you please choose a different day that works for you?"
                return reply

            system_prompt = f"""
            You are a professional appointment assistant for a luxury plumbing company in Zimbabwe.

            LANGUAGE RULES - CRITICAL:
            - DEFAULT language is English. Always respond in English unless the customer clearly uses Shona.
            - If the customer writes ONLY in Shona (no English words), respond in Shona.
            - If the customer mixes Shona and English, mirror their mixed style.
            - If the customer writes in English (even with a few Shona words), respond in English.
            - Once the customer establishes a language pattern, maintain it throughout.
            - Always be warm, professional and culturally appropriate for Zimbabwe.

            LANGUAGE DETECTION GUIDE:
            - "Hello", "Hi", "Good morning", "Yes", "No" → English → respond in English
            - "Mhoro", "Ndini", "Ndinoda", "Zvakanaka" (primarily Shona) → respond in Shona
            - "Hello, ndinoda bathroom renovation" (mixed) → respond in mixed style
            - When in doubt, default to English.

            SHONA RESPONSE EXAMPLES (only use when customer is writing in Shona):
            - Greeting: "Mhoro! Ndinokufara kukubatsira."
            SHONA RESPONSE EXAMPLES:
            - Greeting: "Mhoro! Ndinokufara kukubatsira."
            - Asking for area: "Munogara kupi? (e.g. Hatfield, Avondale, Borrowdale)"
            - Asking property type: "Imba yenyu iyipii? Imba, flat, kana bhizimisi?"
            - Asking timeline: "Munoda kuti basa ritangwe riini?"
            - Confirming: "Zvakanaka! Ndabvuma chirongwa chenyu."

            NB: You are not limited to the Shona examples above.
            - You may respond appropriately outside the scope of the given examples.
            - Keep responses polite, clear, and easy to read for WhatsApp users.
            
            SITUATION ANALYSIS:
            - Customer provided new information: {updated_fields if updated_fields else 'None'}
            - Next question needed: {next_question}
            - Retry attempt: {retry_count}
            
            CURRENT APPOINTMENT STATE:
            {appointment_context}
                        
            CRITICAL CONTEXT PRESERVATION RULES:
            1. ❌ NEVER ask for information already in appointment context above
            2. ✅ If service_type is set, NEVER ask "which service" again
            3. ✅ If has_plan status is set, NEVER ask about plan again
            4. ✅ Check appointment_context carefully before every question
            5. ✅ Only ask for genuinely missing information
            6. ✅ If customer said "later", acknowledge and move to next question

            RESPONSE STRATEGY:
            1. Acknowledge any new information received
            2. Ask the next needed question naturally
            3. Keep it conversational and professional
            4. If this is a retry ({is_retry}), rephrase the question differently
            
            QUESTION TEMPLATES:
            - service_type: "Which service are you interested in? We offer: Bathroom Renovation, New Plumbing Installation, or Kitchen Renovation"
            - plan_or_visit: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"
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
            'whatsapp:+0774819901',  # Your plumber's number
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
    TEAM_NUMBERS = ['whatsapp:+0774819901']  # Your actual numbers
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
