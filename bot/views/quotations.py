from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods, require_GET
from django.utils.decorators import method_decorator
from django.http import FileResponse, HttpResponse, JsonResponse, HttpResponseRedirect
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
import io
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
from ..whatsapp_cloud_api import get_client_for_tenant, whatsapp_api
from ..services.clients import (
    deepseek_client, GOOGLE_CALENDAR_CREDENTIALS, DEEPSEEK_API_KEY,
)
from ..utils import (
    _to_decimal, _to_float, _safe_logo_url, _safe_logo_data_uri,
    _reset_pk_sequence, _append_admin_note,
    clean_phone_number, format_phone_number_for_storage,
)

logger = logging.getLogger(__name__)

from .. import branding
from google.oauth2 import service_account
from googleapiclient.discovery import build
try:
    from reportlab.lib.pagesizes import A4, letter
    from reportlab.lib.units import mm, inch
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle,
        Paragraph, Spacer, Image, HRFlowable,
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
except ImportError:
    pass


from .quote_layout import (
    document_context, is_sectioned, letterhead_for, quote_terms,
    sections_payload, tenant_of,
)


def _sectioned_form_context(request, appointment=None, quotation=None):
    """Shared context for the sectioned quote editor.

    Every value on the sheet resolves through the lead's OWN tenant. A tenant
    with no letterhead gets empty values and the template omits those blocks
    outright, rather than borrowing another tenant's address or bank account.
    """
    tenant = tenant_of(request, appointment=appointment, quotation=quotation)
    letterhead = letterhead_for(tenant)
    return {
        'lh': letterhead,
        'appointment': appointment,
        'quotation': quotation,
        'existing_sections': sections_payload(quotation) if quotation else [],
        'quote_date': quotation.created_at if quotation else timezone.localdate(),
        'vat_percent_initial': (
            quotation.vat_percent if quotation
            else letterhead.get('default_vat_percent') or 0
        ),
        'terms_initial': (
            quote_terms(quotation, letterhead) if quotation
            else list(letterhead.get('terms') or [])
        ),
    }


@method_decorator(staff_required, name='dispatch')
class CreateQuotationView(CreateView):
    model = Quotation
    form_class = QuotationForm
    template_name = 'bot/pages/create_quotation.html'

    def _appointment(self):
        if 'pk' not in self.kwargs:
            return None
        return get_object_or_404(
            Appointment.objects.for_tenant_or_seed(getattr(self.request, 'tenant', None)),
            pk=self.kwargs['pk'])

    def get_template_names(self):
        if is_sectioned(tenant_of(self.request, appointment=self._appointment())):
            return ['bot/pages/quote_sectioned_form.html']
        return [self.template_name]
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get appointment if pk is provided
        appointment = None
        if 'pk' in self.kwargs:
            appointment = get_object_or_404(Appointment.objects.for_tenant_or_seed(getattr(self.request, 'tenant', None)), pk=self.kwargs['pk'])
            context['appointment'] = appointment

        # Resolved AFTER the appointment lands in context, so the LEAD's tenant
        # wins: an operator raising a quote for a client must get the client's
        # letterhead, not their own workspace's.
        context.update(branding.branding_context(
            _branding_tenant(self.request, context)))

        # Job notes from the post-visit debrief carry into the quote screen —
        # the plumber typed them minutes ago and should not retype them. Only
        # while unsent: an old report must not overwrite a later description.
        report = getattr(appointment, 'site_visit_report', None) if appointment else None
        if report is not None and (report.job_notes or '').strip():
            context['quote_prefill_notes'] = report.job_notes.strip()

        if is_sectioned(tenant_of(self.request, appointment=appointment)):
            context.update(_sectioned_form_context(self.request, appointment=appointment))
            return context

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
            appointment = get_object_or_404(Appointment.objects.for_tenant_or_seed(getattr(self.request, 'tenant', None)), pk=self.kwargs['pk'])
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


def _quote_notes(data, current=''):
    """What goes in `notes`.

    The flat layout puts the project notes there. The sectioned sheet has no
    notes box — its `notes` field carries the payment terms instead, one per
    line, which is what the terms rows on the document are.
    """
    if isinstance(data.get('terms'), list):
        return "\n".join(str(t).strip() for t in data['terms'] if str(t).strip())
    return data.get('notes', data.get('project_notes', current))


def _apply_client_fields(appointment, data):
    """The sectioned sheet edits the client's own details in place, the way
    writing on the paper quote would. Only overwrite what was actually sent."""
    changed = []
    for key, field in (('client_name', 'customer_name'),
                       ('client_email', 'customer_email'),
                       ('client_address', 'customer_area')):
        value = (data.get(key) or '').strip()
        if value and value != getattr(appointment, field, ''):
            setattr(appointment, field, value)
            changed.append(field)
    if changed:
        appointment.save(update_fields=changed)


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
            appointment = Appointment.objects.for_tenant_or_seed(getattr(request, 'tenant', None)).get(id=appointment_id)
            logger.info(f"✅ Found Appointment: {appointment}")
        except Appointment.DoesNotExist:
            logger.error(f"❌ Appointment with ID {appointment_id} not found")
            return JsonResponse({
                'success': False,
                'error': f'Appointment with ID {appointment_id} not found'
            }, status=404)
        
        _apply_client_fields(appointment, data)

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
                        discount=_to_decimal(data.get('discount', 0)),
                        vat_percent=_to_decimal(data.get('vat_percent', 0)),
                        notes=_quote_notes(data),
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
                    section=(item_data.get('section') or '')[:120],
                    quantity=_to_decimal(item_data.get('qty', 1), default='1.00'),
                    quantity_text=(item_data.get('qty_text') or '')[:40],
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
    template_name = 'bot/pages/view_quotation.html'
    context_object_name = 'quotation'

    def get_template_names(self):
        if is_sectioned(tenant_of(self.request, quotation=self.get_object())):
            return ['bot/pages/quote_sectioned_view.html']
        return [self.template_name]


    def get_queryset(self):
        # Same resolver as every other quote action: no workspace means no
        # quotes, never all of them.
        return _visible_quotations(self.request)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(branding.branding_context(_branding_tenant(self.request, context)))

        quotation = context['quotation']
        tenant = tenant_of(self.request, quotation=quotation)
        if is_sectioned(tenant):
            letterhead = letterhead_for(tenant)
            context['lh'] = letterhead
            context.update(document_context(quotation, letterhead))
        return context


def _branding_tenant(request, context=None):
    """Whose brand goes on this quote screen.

    The QUOTE's own tenant wins over the request's: a platform operator
    reviewing a client's quote must see the client's letterhead, not homebase's.
    Falls back to the request's workspace for the blank/standalone screens,
    where there is no quote yet to ask.
    """
    context = context or {}
    for key in ('quotation', 'object', 'appointment'):
        obj = context.get(key)
        tenant = getattr(obj, 'tenant', None)
        if tenant is not None:
            return tenant
        apt = getattr(obj, 'appointment', None)
        if apt is not None and getattr(apt, 'tenant', None) is not None:
            return apt.tenant
    return getattr(request, 'tenant', None)


def _viewing_all_tenants(request) -> bool:
    """Is the operator explicitly asking to look across every client?

    NOT the default, even for the platform owner. The tenant switcher is
    impersonation - "the dashboard then shows that tenant's world" - and every
    other page in the app honours it. Bypassing it here put Homebase's quote in
    Barmak's section, which is exactly what the switcher exists to prevent.
    Seeing across clients is a deliberate act, so it takes a deliberate flag.
    """
    from ..decorators import is_platform_owner

    return bool(request.GET.get('all')) and is_platform_owner(request.user)


def _visible_quotations(request, across_tenants=None):
    """Quotes this request may touch.

    Scoped to the CURRENT WORKSPACE - the tenant the switcher is pointing at -
    for everybody, operator included. The single scoping resolver for the quote
    actions: the per-view `.filter(appointment__tenant=...) if ... else
    Quotation.objects` inline was subtly different (it fell back to EVERY tenant
    when no workspace resolved) and had to be repeated correctly at each call
    site.

    `across_tenants` is the opt-out, and only the operator can set it. Leave it
    None and it is read from the request, so a per-quote action never widens
    past the workspace by accident.
    """
    if across_tenants is None:
        across_tenants = _viewing_all_tenants(request)
    if across_tenants:
        return Quotation.objects.all()
    tenant = getattr(request, 'tenant', None)
    if tenant is None:
        return Quotation.objects.none()
    return Quotation.objects.filter(tenant=tenant)


@method_decorator(staff_required, name='dispatch')
class QuotationsListView(ListView):
    """"My quotes" - every quote this business has raised, and only theirs.

    SECURITY: this view had no get_queryset at all, so ListView fell back to
    Quotation.objects.all() and every client saw every other client's quotes,
    lead names and figures. Scoping is done at the QUERY, never in the template,
    so nothing downstream can reintroduce the leak. The platform operator is the
    one role that sees across clients, and the page says so when they do.
    """
    model = Quotation
    template_name = 'bot/pages/quotations_list.html'
    context_object_name = 'quotations'
    paginate_by = 25

    def _tenant(self):
        return getattr(self.request, 'tenant', None)

    def _sees_all_tenants(self):
        return _viewing_all_tenants(self.request)

    def _can_see_all_tenants(self):
        """May this user switch the cross-client view ON? (Offering the toggle
        is a different question from having it on.)"""
        from ..decorators import is_platform_owner
        return is_platform_owner(self.request.user)

    def get_queryset(self):
        qs = (_visible_quotations(self.request)
              .select_related('appointment', 'appointment__tenant', 'tenant')
              # Newest first, with id as the tie-break: created_at is
              # auto_now_add, so two quotes raised in the same instant would
              # otherwise come back in whatever order the database felt like -
              # and the order would change between page loads.
              .order_by('-created_at', '-id'))

        status = (self.request.GET.get('status') or '').strip()
        if status in dict(Quotation.STATUS_CHOICES):
            qs = qs.filter(status=status)

        # Search by lead name or quote number - the two things anyone actually
        # has to hand when looking for a quote.
        query = (self.request.GET.get('q') or '').strip()
        if query:
            qs = qs.filter(
                Q(quotation_number__icontains=query)
                | Q(appointment__customer_name__icontains=query)
                | Q(appointment__phone_number__icontains=query)
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'active_nav': 'quotations',
            'search_query': (self.request.GET.get('q') or '').strip(),
            'status_filter': (self.request.GET.get('status') or '').strip(),
            'status_choices': Quotation.STATUS_CHOICES,
            'sees_all_tenants': self._sees_all_tenants(),
            'can_see_all_tenants': self._can_see_all_tenants(),
            # Distinguishes "no quotes yet" from "nothing matched your search":
            # the same empty table means very different things.
            'is_filtered': bool(self.request.GET.get('q') or self.request.GET.get('status')),
        })
        return context


@staff_required
@require_http_methods(["GET"])
def quotation_detail_api(request, pk):
    """Return quotation payload used by edit_quotation.html."""
    quotation = get_object_or_404(_visible_quotations(request), pk=pk)
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
    template_name = 'bot/pages/edit_quotation.html'

    def get_template_names(self):
        if is_sectioned(tenant_of(self.request, quotation=self.get_object())):
            return ['bot/pages/quote_sectioned_form.html']
        return [self.template_name]
    


    def get_queryset(self):
        _t = getattr(self.request, 'tenant', None)
        return Quotation.objects.filter(appointment__tenant=_t) if _t else Quotation.objects.all()

    def get_queryset(self):
        _t = getattr(self.request, 'tenant', None)
        return Quotation.objects.filter(appointment__tenant=_t) if _t else Quotation.objects.all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['formset'] = QuotationItemFormSet(self.request.POST, instance=self.object)
        else:
            context['formset'] = QuotationItemFormSet(instance=self.object)

        quotation = self.object
        if is_sectioned(tenant_of(self.request, quotation=quotation)):
            context.update(branding.branding_context(_branding_tenant(self.request, context)))
            context.update(_sectioned_form_context(self.request, quotation=quotation))
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

            quotation.notes = _quote_notes(data, quotation.notes or '')
            quotation.labor_cost = _to_decimal(data.get('labour_cost', quotation.labor_cost))
            quotation.transport_cost = _to_decimal(data.get('transport_cost', quotation.transport_cost))
            quotation.materials_cost = _to_decimal(data.get('materials_cost', quotation.materials_cost))
            quotation.discount = _to_decimal(data.get('discount', quotation.discount))
            quotation.vat_percent = _to_decimal(data.get('vat_percent', quotation.vat_percent))
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
                        section=((item or {}).get('section') or '')[:120],
                        quantity=qty,
                        quantity_text=((item or {}).get('qty_text') or '')[:40],
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
    quotation = get_object_or_404(_visible_quotations(request), pk=pk)
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
    quotation = get_object_or_404(_visible_quotations(request), pk=pk)
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
    quotation = get_object_or_404(_visible_quotations(request), pk=pk)
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
        get_client_for_tenant(quotation.appointment.tenant).send_local_document(
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


@staff_required
def download_quotation_pdf(request, pk):
    """Stream the quote as a PDF - the same file the send paths attach.

    One builder (build_quotation_pdf_file) behind the download, the WhatsApp
    send and the email send, so what the plumber checks is byte-for-byte what
    the customer receives.
    """
    quotation = get_object_or_404(_visible_quotations(request), pk=pk)
    pdf_path = build_quotation_pdf_file(quotation)
    safe = re.sub(r'[^A-Za-z0-9 _-]+', '', quotation.get_display_name()).strip().replace(' ', '_')
    filename = f"{safe[:80] or 'Quotation'}-{quotation.quotation_number}.pdf"
    # FileResponse closes the handle; the temp file is unlinked after the
    # response is written, so a slow client cannot be served a deleted file.
    response = FileResponse(open(pdf_path, 'rb'), content_type='application/pdf',
                            as_attachment=True, filename=filename)
    response._resource_closers.append(lambda: _quiet_unlink(pdf_path))
    return response


def _quiet_unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


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

    # THIS tenant's logo, never a hardcoded file. The old block reached for
    # bot/static/logo.jpg - Homebase's mark - so every other tenant's quote went
    # out under someone else's brand. bot/branding is the single reader, and a
    # tenant with no logo falls back to their own name in text (below), never to
    # the platform's mark and never to another tenant's.
    _apt = getattr(quotation, 'appointment', None)
    _tenant = getattr(_apt, 'tenant', None) or getattr(quotation, 'tenant', None)
    _text_x = left
    _logo_bytes, _ = branding.logo_bytes(_tenant)
    if _logo_bytes:
        try:
            from reportlab.lib.utils import ImageReader
            c.drawImage(ImageReader(io.BytesIO(_logo_bytes)), left, y - 82,
                        width=70, height=70, preserveAspectRatio=True, mask='auto')
            _text_x = left + 85
        except Exception:
            # An SVG, or a file reportlab cannot draw. The name still prints,
            # so the header is never blank - it just starts at the margin.
            logger.info('Quote PDF: logo not drawable for tenant %s', _tenant)

    # Tenant identity on the quotation header (Phase 2.2b). The strapline and
    # street address come from the tenant's own letterhead; absent means the
    # line is omitted, never filled with another tenant's. (The hardcoded
    # "HOMEBASE CONSTRUCTION" / Johannesburg address that used to sit here were
    # template remnants printed on every tenant's quote.)
    _letterhead = {}
    if _tenant is not None:
        from ..tenant_config import get_config
        _letterhead = get_config(_tenant).letterhead() or {}
    _name = (branding.brand_name(_tenant) or '').upper()
    _cell = _apt.plumber_contact() if _apt is not None and hasattr(_apt, 'plumber_contact') else ''
    _strapline = (_letterhead.get('strapline') or '').strip()
    _address = (_letterhead.get('address') or _letterhead.get('location_line') or '').strip()

    _line_y = y - 20
    c.setFillColor(colors.black)
    if _name:
        c.setFont("Helvetica-Bold", 16)
        c.drawString(_text_x, _line_y, _name[:48])
        _line_y -= 18
    if _strapline:
        c.setFont("Helvetica-Oblique", 10)
        c.setFillColor(colors.grey)
        c.drawString(_text_x, _line_y, _strapline[:70])
        _line_y -= 17
    if _address:
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.grey)
        c.drawString(_text_x, _line_y, _address[:80])
        _line_y -= 14
    if _cell:
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.grey)
        c.drawString(_text_x, _line_y, f"Cell: {_cell}")
        _line_y -= 17
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(colors.black)
    c.drawString(_text_x, min(_line_y, y - 86), quotation.get_display_name()[:80])
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
