from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods, require_GET
from django.utils.decorators import method_decorator
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.generic import ListView, DetailView, TemplateView, CreateView, UpdateView, DeleteView
from django.db.models import Count, Q
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.forms import modelformset_factory
from django.templatetags.static import static
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import requests
import pytz
import os
import json
import re
import tempfile
import base64
import logging

from ..models import (
    Appointment, Quotation, QuotationItem,
    QuotationTemplate, QuotationTemplateItem, ConversationMessage,
)
from ..forms import (
    AppointmentForm, SettingsForm, CalendarSettingsForm, AISettingsForm,
    QuotationForm, QuotationItemFormSet,
    QuotationTemplateForm, QuotationTemplateItemFormSet,
)
from ..decorators import staff_required, anonymous_required, StaffRequiredMixin
from ..whatsapp_cloud_api import whatsapp_api
from ..services.clients import (
    twilio_client, deepseek_client,
    TWILIO_WHATSAPP_NUMBER, GOOGLE_CALENDAR_CREDENTIALS,
    DEEPSEEK_API_KEY,
)
from ..utils import (
    _to_decimal, _to_float, _safe_logo_url, _safe_logo_data_uri,
    _reset_pk_sequence, _append_admin_note,
    clean_phone_number, format_phone_number_for_storage,
)

logger = logging.getLogger(__name__)


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
class StandaloneQuotationView(View):
    """Render the standalone quotation creation form."""
    template_name = 'bot/pages/standalone_quotation.html'
 
    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {
            'logo_url':      _safe_logo_url(),
            'logo_data_uri': _safe_logo_data_uri(),
        })


@csrf_exempt
@require_http_methods(["POST"])
@staff_required
def create_standalone_quotation_api(request):
    """
    Create a quotation that may or may not be linked to an appointment.
 
    If `appointment_id` is provided and resolves to a real Appointment, the
    quotation is linked to that appointment (same as the existing API).
 
    If `appointment_id` is absent/null, we still create a valid Quotation
    by temporarily linking it to a placeholder/bare appointment that carries
    the client info, OR by creating a quotation with appointment=None if the
    model allows it.  We store extra client metadata in the quotation notes.
    """
    from .models import Appointment, Quotation, QuotationItem
    from .utils import _reset_pk_sequence
 
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
 
    client_name    = (data.get('client_name') or '').strip()
    client_phone   = (data.get('client_phone') or '').strip()
    client_email   = (data.get('client_email') or '').strip()
    client_address = (data.get('client_address') or '').strip()
    project_type   = (data.get('project_type') or '').strip()
    project_loc    = (data.get('project_location') or '').strip()
    notes_raw      = (data.get('notes') or '').strip()
    items_raw      = data.get('items') or []
    labour_cost    = _to_decimal(data.get('labour_cost',    0))
    transport_cost = _to_decimal(data.get('transport_cost', 0))
    materials_cost = _to_decimal(data.get('materials_cost', 0))
 
    if not client_name:
        return JsonResponse({'success': False, 'error': 'client_name is required'}, status=400)
 
    # ── Resolve (or create) appointment ───────────────────────────────────────
    appointment    = None
    appointment_id = data.get('appointment_id')
 
    if appointment_id:
        try:
            appointment = Appointment.objects.get(id=int(appointment_id))
        except (Appointment.DoesNotExist, ValueError, TypeError):
            return JsonResponse(
                {'success': False, 'error': f'Appointment {appointment_id} not found'},
                status=404,
            )
    else:
        # No appointment supplied — create a lightweight stub so the FK is
        # satisfied.  Use a generated "quotation-only" phone key.
        import uuid
        stub_phone = f"quotation_only_{uuid.uuid4().hex[:10]}"
        appointment = Appointment.objects.create(
            phone_number=stub_phone,
            customer_name=client_name or None,
            customer_email=client_email or None,
            customer_area=client_address or None,
            project_type=project_type or None,
            project_description=project_loc or None,
            status='pending',
        )
 
    # ── Build notes string ────────────────────────────────────────────────────
    meta_lines = []
    if client_phone:   meta_lines.append(f"Phone: {client_phone}")
    if client_email:   meta_lines.append(f"Email: {client_email}")
    if client_address: meta_lines.append(f"Address: {client_address}")
    if project_loc:    meta_lines.append(f"Site: {project_loc}")
    if notes_raw:      meta_lines.append(notes_raw)
    combined_notes = '\n'.join(meta_lines)
 
    # ── Create Quotation ──────────────────────────────────────────────────────
    quotation = None
    for attempt in range(2):
        try:
            with transaction.atomic():
                quotation = Quotation.objects.create(
                    appointment=appointment,
                    plumber=request.user if request.user.is_authenticated else None,
                    labor_cost=labour_cost,
                    transport_cost=transport_cost,
                    materials_cost=materials_cost,
                    notes=combined_notes,
                    status='draft',
                )
            break
        except IntegrityError as exc:
            err = str(exc).lower()
            if attempt == 0 and 'bot_quotation_pkey' in err and 'key (id)=' in err:
                _reset_pk_sequence(Quotation)
                continue
            raise
 
    if quotation is None:
        return JsonResponse({'success': False, 'error': 'Failed to create quotation'}, status=500)
 
    # ── Create line items ─────────────────────────────────────────────────────
    for item in items_raw:
        desc = (item.get('name') or item.get('description') or '').strip()
        if not desc:
            continue
        qty   = _to_decimal(item.get('qty')  or item.get('quantity') or 1, '1.00')
        unit  = _to_decimal(item.get('unit') or item.get('unit_price') or 0)
        QuotationItem.objects.create(
            quotation=quotation,
            description=desc,
            quantity=qty,
            unit_price=unit,
        )
 
    # Recalculate totals after items are added
    quotation.save()
 
    logger.info(
        f"Standalone quotation created: #{quotation.quotation_number} "
        f"for {client_name} by {getattr(request.user, 'username', 'anon')}"
    )
 
    return JsonResponse({
        'success':          True,
        'quotation_id':     quotation.id,
        'quotation_number': quotation.quotation_number,
        'quotation_name':   quotation.get_display_name(),
        'appointment_id':   appointment.id,
        'total_amount':     float(quotation.total_amount),
        'message':          f'Quotation {quotation.quotation_number} created successfully',
    })


@staff_required
@require_GET
def appointment_search_api(request):
    """
    GET /api/appointments/search/?q=<query>
    Returns matching appointments for the typeahead in the standalone form.
    """
    from .models import Appointment
    from django.db.models import Q
 
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'appointments': []})
 
    qs = (
        Appointment.objects
        .filter(
            Q(customer_name__icontains=query)  |
            Q(phone_number__icontains=query)   |
            Q(customer_area__icontains=query)  |
            Q(project_type__icontains=query)
        )
        .exclude(phone_number__startswith='quotation_only_')
        .order_by('-updated_at')[:15]
    )
 
    results = []
    for a in qs:
        results.append({
            'id':                a.id,
            'customer_name':     a.customer_name or '',
            'phone_number':      a.phone_number or '',
            'customer_email':    a.customer_email or '',
            'customer_area':     a.customer_area or '',
            'project_type':      a.project_type or '',
            'project_description': a.project_description or '',
            'status':            a.status,
        })
 
    return JsonResponse({'appointments': results})


@method_decorator(staff_required, name='dispatch')
class QuotationTemplatesListView(ListView):
    """List all quotation templates"""
    model = QuotationTemplate
    template_name = 'bot/pages/quotation_templates_list.html'
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
    template_name = 'bot/pages/create_quotation_template.html'
    
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
    template_name = 'bot/pages/edit_quotation_template.html'
    
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
    template_name = 'bot/pages/quotation_template_detail.html'
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
    
    return render(request, 'bot/pages/duplicate_template.html', {
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
    
    return render(request, 'bot/pages/delete_template.html', {
        'template': template
    })


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
