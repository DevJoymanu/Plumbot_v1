"""
Platform billing: the operator invoicing the plumbing companies
(docs/MULTI_TENANT_PLAN.md §3.4 "Usage & billing", decision #4 flat monthly
fee; rules in docs/current-state/billing.md).

WHAT: invoices to tenants, payments against them, and a receipt per payment,
each downloadable as a PDF and emailable to the tenant, plus the issuer's own
details (the Billing settings page).

WHY here and not on the tenant dashboard: these are the platform's books.
Every view is superuser-only on the VIEW (`superuser_required`, never only a
template check), like the rest of the console, and nothing reads
`request.tenant`: the operator's "View as" lens must not narrow or widen what
billing shows. A tenant-facing "your invoices" page would be a separate,
tenant-scoped view.

HOW: every state change is a POST (`@require_POST`), so a prefetching browser
can never void or email anything. Line items are editable only on a draft;
once an invoice has been sent, the correction is void and reissue, and a
receipt is voided rather than deleted, so a number that went out on paper
always resolves to a row. Pinned by `PlatformBillingTests` in
bot/test_views_actions.py.
"""

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.db import transaction
from django.forms import inlineformset_factory
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..billing_emails import send_invoice_email, send_receipt_email
from ..billing_pdf import build_invoice_pdf, build_receipt_pdf
from ..models import (
    PlatformBillingProfile, PlatformInvoice, PlatformInvoiceItem,
    PlatformInvoiceTemplate, PlatformPayment, Tenant, TenantProfile, allocate_breakdown,
    is_zimbabwean_tenant,
)
from .platform import superuser_required

_DATE = forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')
CURRENCY_CHOICES = [('US$', 'US dollars (US$)'), ('R', 'South African rand (R)')]


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = PlatformInvoice
        fields = ['tenant', 'issue_date', 'due_date', 'period_start', 'period_end',
                  'currency', 'zimbabwe_client', 'bill_to_name', 'bill_to_email',
                  'bill_to_address', 'notes']
        widgets = {
            'issue_date': _DATE, 'due_date': _DATE,
            'period_start': _DATE, 'period_end': _DATE,
            'bill_to_address': forms.Textarea(attrs={'rows': 3}),
            'notes': forms.Textarea(attrs={'rows': 3}),
            # Per tenant (owner, 2026-09-24): US dollars by default, rand for
            # a tenant who pays in rand. A pick list, not free text, so the
            # symbol printed on the PDF is always one of these two.
            'currency': forms.Select(choices=CURRENCY_CHOICES),
        }
        labels = {'bill_to_name': 'Bill to', 'bill_to_email': 'Billing email',
                  'bill_to_address': 'Address', 'period_start': 'Period from',
                  'period_end': 'Period to',
                  'zimbabwe_client': 'Zimbabwean client (EcoCash first, then bank)'}
        help_texts = {'zimbabwe_client': 'Unticked: bank details only, no EcoCash.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Every invoice is to a tenant; the FK is only nullable so the record
        # survives the tenant being deleted.
        self.fields['tenant'].required = True
        self.fields['tenant'].queryset = Tenant.objects.order_by('name')

    def clean(self):
        data = super().clean()
        issue, due = data.get('issue_date'), data.get('due_date')
        if issue and due and due < issue:
            self.add_error('due_date', 'The due date cannot be before the issue date.')
        start, end = data.get('period_start'), data.get('period_end')
        if bool(start) != bool(end):
            self.add_error('period_end', 'Give both ends of the period, or neither.')
        elif start and end and end < start:
            self.add_error('period_end', 'The period cannot end before it starts.')
        return data


class InvoiceItemForm(forms.ModelForm):
    """One invoice line, plus a tick box for printing the subscription's
    feature breakdown under it (owner, 2026-09-24). The box is not a column:
    `_save_items` turns it into the frozen `breakdown` allocation, and an
    existing line opens with the box ticked when it already has one."""
    with_breakdown = forms.BooleanField(required=False, label='Feature breakdown')

    class Meta:
        model = PlatformInvoiceItem
        fields = ['description', 'quantity', 'unit_price']
        # Placeholders and aria-labels carry the column names on a phone,
        # where the line heading row is hidden and each line is stacked.
        widgets = {
            'description': forms.TextInput(attrs={'placeholder': 'What this line is for',
                                                  'aria-label': 'Description'}),
            'quantity': forms.NumberInput(attrs={'placeholder': 'Qty', 'aria-label': 'Quantity',
                                                 'inputmode': 'decimal'}),
            'unit_price': forms.NumberInput(attrs={'placeholder': 'Unit price', 'aria-label': 'Unit price',
                                                   'inputmode': 'decimal'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.breakdown:
            self.fields['with_breakdown'].initial = True


def _item_formset(extra):
    return inlineformset_factory(
        PlatformInvoice, PlatformInvoiceItem, form=InvoiceItemForm,
        extra=extra, can_delete=True, min_num=1, validate_min=True,
    )


class BillingProfileForm(forms.ModelForm):
    class Meta:
        model = PlatformBillingProfile
        fields = ['business_name', 'tagline', 'address', 'email', 'contact_email', 'phone',
                  'zimbabwe_phone', 'currency',
                  'default_monthly_fee', 'payment_terms_days', 'payment_details', 'ecocash_details',
                  'subscription_breakdown', 'footer_note', 'reminder_days', 'reminder_time']
        widgets = {'address': forms.Textarea(attrs={'rows': 3}),
                   'payment_details': forms.Textarea(attrs={'rows': 5}),
                   'ecocash_details': forms.Textarea(attrs={'rows': 3}),
                   'subscription_breakdown': forms.Textarea(attrs={'rows': 7}),
                   'currency': forms.Select(choices=CURRENCY_CHOICES),
                   'reminder_time': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M')}
        labels = {'business_name': 'Business name', 'email': 'Reply-to inbox',
                  'contact_email': 'Email printed on invoices', 'phone': 'Phone (all clients)',
                  'zimbabwe_phone': 'Phone for Zimbabwean clients',
                  'default_monthly_fee': 'Default monthly fee',
                  'payment_terms_days': 'Days to pay',
                  'payment_details': 'Bank details', 'ecocash_details': 'EcoCash (Zimbabwean clients)',
                  'footer_note': 'Footer note',
                  'reminder_days': 'Reminder days before due',
                  'reminder_time': 'Send reminders from',
                  'subscription_breakdown': 'Subscription feature breakdown',
                  'currency': 'Default currency'}

    def clean_subscription_breakdown(self):
        """Refuse a split that does not parse or does not total 100%, so the
        page says what is wrong instead of invoices silently printing none."""
        from ..models import parse_breakdown
        raw = self.cleaned_data.get('subscription_breakdown', '')
        if raw.strip() and not parse_breakdown(raw):
            raise forms.ValidationError(
                'Write one feature per line as "Feature | what it covers | percent", '
                'with the percents adding up to 100.')
        return raw


# ── Resolvers ────────────────────────────────────────────────────────────────

def _month_bounds(day):
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last)


def _bill_to_defaults(tenant):
    """Who a new invoice for `tenant` is addressed to.

    The tenant's LAST invoice wins, because the billing contact the operator
    typed there is the best evidence of who pays; failing that, the tenant's
    name and the internal-alerts inbox they chose on their Profile page. That
    inbox is theirs, so it can never borrow another tenant's address.
    """
    last = (PlatformInvoice.objects.filter(tenant=tenant)
            .order_by('-issue_date', '-id').first())
    if last:
        # The currency and the Zimbabwe payment block come with them: a
        # tenant who pays in rand, or by EcoCash, is not re-picked every month.
        return {'bill_to_name': last.bill_to_name,
                'bill_to_email': last.bill_to_email,
                'bill_to_address': last.bill_to_address,
                'currency': last.currency,
                'zimbabwe_client': last.zimbabwe_client}
    profile = TenantProfile.objects.filter(tenant=tenant).first()
    letterhead = (getattr(profile, 'letterhead', None) or {}) if profile else {}
    return {
        'bill_to_name': (letterhead.get('trading_name') or tenant.name),
        'bill_to_email': getattr(profile, 'email_sender', '') or '',
        'bill_to_address': getattr(profile, 'location_line', '') or '',
        # First invoice: a +263 number or a Zimbabwean place on their records.
        'zimbabwe_client': is_zimbabwean_tenant(tenant),
    }


def _new_invoice_initial(tenant, profile, template=None):
    """Starting values for a new invoice: today, due after the issuer's
    terms, this calendar month as the period, and the lines of `template`
    (the chosen one, else the default, "Plumbot Standard"). With no template
    at all it falls back to one subscription line at the default fee. Every
    value stays editable on the form."""
    today = timezone.localdate()
    start, end = _month_bounds(today)
    initial = {
        'issue_date': today,
        'due_date': today + timedelta(days=profile.payment_terms_days),
        'period_start': start, 'period_end': end,
        'currency': profile.currency,
    }
    if tenant is not None:
        initial['tenant'] = tenant.pk
        initial.update(_bill_to_defaults(tenant))
    template = template or PlatformInvoiceTemplate.default()
    if template is not None and template.lines:
        items = template.form_lines(f'{today:%B %Y}')
        # A template's breakdown tick only holds while a valid split exists.
        if not profile.breakdown_rows():
            for item in items:
                item['with_breakdown'] = False
        if template.notes:
            initial['notes'] = template.notes
        return initial, items
    items = [{
        'description': f'Plumbot monthly subscription, {today:%B %Y}',
        'quantity': 1, 'unit_price': profile.default_monthly_fee,
        'with_breakdown': bool(profile.breakdown_rows()),
    }]
    return initial, items


def _save_items(invoice, formset):
    """Write the item formset in on-screen order.

    A row ticked for removal, or an existing row whose description was
    cleared, is deleted; an untouched blank spare row is skipped. Every kept
    row is renumbered 1..n so the PDF prints lines in the order typed.

    The new-invoice prefill (the subscription line) is passed as formset
    `initial` on GET only. On POST the formset is built WITHOUT it, so that
    line counts as changed and saves; with initial on POST too, Django would
    treat an untouched prefilled line as an empty extra form and drop it.
    """
    # A ticked "Feature breakdown" allocates the line's total across the
    # Billing details split NOW and freezes it on the line, so changing the
    # split later never rewrites an issued invoice. Unticked clears it.
    rows = PlatformBillingProfile.current().breakdown_rows()
    order = 0
    for form in formset.forms:
        data = getattr(form, 'cleaned_data', None) or {}
        if data.get('DELETE') or not data.get('description'):
            if form.instance.pk:
                form.instance.delete()
            continue
        order += 1
        item = form.save(commit=False)
        item.invoice = invoice
        item.sort_order = order
        item.breakdown = allocate_breakdown(item.line_total, rows) if data.get('with_breakdown') else []
        item.save()


def _has_real_item(formset):
    return any(
        form.cleaned_data.get('description') and not form.cleaned_data.get('DELETE')
        for form in formset.forms if hasattr(form, 'cleaned_data'))


def _parse_amount(raw):
    try:
        value = Decimal(str(raw or '').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None
    return value.quantize(Decimal('0.01')) if value > 0 else None


def _parse_date(raw, default):
    try:
        return date.fromisoformat(str(raw or '').strip())
    except ValueError:
        return default


def _pdf_response(data, filename, inline=True):
    response = HttpResponse(data, content_type='application/pdf')
    disposition = 'inline' if inline else 'attachment'
    response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    return response


# ── Pages ────────────────────────────────────────────────────────────────────

@superuser_required
def billing_home(request):
    """All invoices with their live status, a filter by status and tenant,
    the four figures that answer "who owes us what", and recent receipts.

    Status is derived (`display_status`), so the filter runs in Python over
    the prefetched rows; platform billing is a handful of rows a month.
    """
    status = request.GET.get('status', '')
    tenant_slug = request.GET.get('tenant', '')
    invoices = list(
        PlatformInvoice.objects.select_related('tenant')
        .prefetch_related('items', 'payments'))

    today = timezone.localdate()
    month_start, _ = _month_bounds(today)
    live = [inv for inv in invoices if inv.status != PlatformInvoice.Status.VOID]
    issued = [inv for inv in live if inv.status == PlatformInvoice.Status.SENT]
    summary = {
        'outstanding': sum((inv.balance for inv in issued), Decimal('0')),
        'overdue_count': sum(1 for inv in issued if inv.display_status == 'overdue'),
        'draft_count': sum(1 for inv in live if inv.status == PlatformInvoice.Status.DRAFT),
        'received_month': sum(
            (p.amount for p in PlatformPayment.objects.filter(
                is_void=False, paid_on__gte=month_start, paid_on__lte=today)),
            Decimal('0')),
    }

    rows = invoices
    if tenant_slug:
        rows = [inv for inv in rows if inv.tenant and inv.tenant.slug == tenant_slug]
    if status:
        rows = [inv for inv in rows if inv.display_status == status]

    profile = PlatformBillingProfile.current()
    return render(request, 'bot/pages/billing_home.html', {
        'active_nav': 'billing',
        'invoices': rows,
        'any_invoices': bool(invoices),
        'summary': summary,
        'currency': profile.currency,
        'profile': profile,
        'status': status,
        'tenant_slug': tenant_slug,
        'status_choices': list(PlatformInvoice.DISPLAY_LABELS.items()),
        'tenants': Tenant.objects.order_by('name'),
        'invoice_templates': PlatformInvoiceTemplate.objects.all(),
        'receipts': (PlatformPayment.objects.select_related('invoice')
                     .order_by('-paid_on', '-id')[:10]),
    })


@superuser_required
def billing_invoice_new(request):
    """Raise an invoice. GET prefills from `?tenant=<slug>`; POST creates it
    as a draft and lands on its page, where it is sent or emailed."""
    profile = PlatformBillingProfile.current()
    if request.method == 'POST':
        form = InvoiceForm(request.POST)
        formset = _item_formset(0)(request.POST, instance=PlatformInvoice())
        if form.is_valid() and formset.is_valid() and _has_real_item(formset):
            with transaction.atomic():
                invoice = form.save()
                _save_items(invoice, formset)
            messages.success(request, f'Invoice {invoice.number} created as a draft.')
            return redirect('billing_invoice_detail', pk=invoice.pk)
        if formset.is_valid() and not _has_real_item(formset):
            messages.error(request, 'An invoice needs at least one line.')
    else:
        # ?tenant=<slug> from the billing home's chips, ?tenant_id=<pk> from
        # the form's own tenant picker reloading to refill the bill-to fields.
        slug = request.GET.get('tenant', '')
        tenant_id = request.GET.get('tenant_id', '')
        tenant = None
        if slug:
            tenant = Tenant.objects.filter(slug=slug).first()
        elif tenant_id.isdigit():
            tenant = Tenant.objects.filter(pk=int(tenant_id)).first()
        # ?template=<pk> from the template chips; absent means the default.
        template_id = request.GET.get('template', '')
        template = (PlatformInvoiceTemplate.objects.filter(pk=int(template_id)).first()
                    if template_id.isdigit() else None) or PlatformInvoiceTemplate.default()
        initial, items = _new_invoice_initial(tenant, profile, template)
        form = InvoiceForm(initial=initial)
        formset = _item_formset(len(items) + 2)(instance=PlatformInvoice(), initial=items)
        return render(request, 'bot/pages/billing_invoice_form.html', {
            'active_nav': 'billing', 'form': form, 'formset': formset,
            'mode': 'new', 'profile': profile,
            'templates': PlatformInvoiceTemplate.objects.all(),
            'template': template, 'tenant_slug': tenant.slug if tenant else '',
        })
    return render(request, 'bot/pages/billing_invoice_form.html', {
        'active_nav': 'billing', 'form': form, 'formset': formset,
        'mode': 'new', 'profile': profile,
    })


@superuser_required
def billing_invoice_edit(request, pk):
    """Edit a DRAFT invoice. A sent or void one is refused: what the tenant
    already holds must not change underneath them."""
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    if not invoice.is_editable:
        messages.error(request, f'{invoice.number} has been sent, so it can no longer be edited. '
                                'Void it and raise a new one instead.')
        return redirect('billing_invoice_detail', pk=invoice.pk)
    FormSet = _item_formset(2)
    if request.method == 'POST':
        form = InvoiceForm(request.POST, instance=invoice)
        formset = FormSet(request.POST, instance=invoice)
        if form.is_valid() and formset.is_valid() and _has_real_item(formset):
            with transaction.atomic():
                invoice = form.save()
                _save_items(invoice, formset)
            messages.success(request, f'{invoice.number} saved.')
            return redirect('billing_invoice_detail', pk=invoice.pk)
        if formset.is_valid() and not _has_real_item(formset):
            messages.error(request, 'An invoice needs at least one line.')
    else:
        form = InvoiceForm(instance=invoice)
        formset = FormSet(instance=invoice)
    return render(request, 'bot/pages/billing_invoice_form.html', {
        'active_nav': 'billing', 'form': form, 'formset': formset,
        'mode': 'edit', 'invoice': invoice,
        'profile': PlatformBillingProfile.current(),
    })


@superuser_required
def billing_invoice_detail(request, pk):
    """One invoice: its lines, payments and receipts, and, once it is due,
    the payment check (paid / more days / not paid) that the operator's
    due-day email links to. `stage` tells the template which panel to show:
    'check' from the due day until answered, 'countdown' once marked not paid.
    """
    invoice = get_object_or_404(
        PlatformInvoice.objects.select_related('tenant')
        .prefetch_related('items', 'payments'), pk=pk)
    today = timezone.localdate()
    stage = ''
    if invoice.status == PlatformInvoice.Status.SENT and invoice.balance > 0:
        if invoice.unpaid_confirmed_at:
            stage = 'countdown'
        elif invoice.due_date <= today:
            stage = 'check'
    return render(request, 'bot/pages/billing_invoice_detail.html', {
        'active_nav': 'billing',
        'invoice': invoice,
        'payments': invoice.payments.all(),
        'methods': PlatformPayment.Method.choices,
        'today': today,
        'stage': stage,
        'days_left': (invoice.due_date - today).days,
        'days_overdue': max(0, (today - invoice.due_date).days),
        'days_to_off': ((invoice.switch_off_on - today).days
                        if invoice.switch_off_on else None),
        'offsets': [d for d in invoice.reminder_offsets() if d],
        'profile': PlatformBillingProfile.current(),
        'reminder_history': _reminder_history(invoice),
        'tenant_paused': bool(invoice.tenant and not invoice.tenant.is_active),
    })


_REMINDER_LABELS = {
    'remind': 'Reminder to tenant', 'check': 'Payment check to you',
    'off': 'Switch-off notice to tenant', 'pause': 'Pause prompt to you',
}


def _reminder_history(invoice):
    """The invoice's reminder log as rows for the page, newest first: what
    it was, which step (days before), when, and whether it went."""
    rows = []
    for key, entry in (invoice.reminder_log or {}).items():
        parts = key.split(':')
        label = _REMINDER_LABELS.get(parts[0], parts[0])
        if parts[0] in ('remind', 'off') and len(parts) == 3 and parts[2].isdigit():
            days = int(parts[2])
            label += ', on the day' if days == 0 else f', {days} days before'
        try:
            at = timezone.localtime(datetime.fromisoformat(entry.get('at', '')))
        except (TypeError, ValueError):
            at = None
        rows.append({'label': label, 'at': at,
                     'ok': entry.get('ok'), 'tries': entry.get('tries', 0)})
    rows.sort(key=lambda r: r['at'] or timezone.now(), reverse=True)
    return rows


@superuser_required
def billing_settings(request):
    """The issuer's own details. Changes apply to invoices raised from now
    on; existing invoices keep the snapshot they were issued with."""
    profile = PlatformBillingProfile.current()
    if request.method == 'POST':
        form = BillingProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Billing details saved. New invoices will use them.')
            return redirect('billing_home')
    else:
        form = BillingProfileForm(instance=profile)
    return render(request, 'bot/pages/billing_settings.html', {
        'active_nav': 'billing', 'form': form,
    })


# ── Documents ────────────────────────────────────────────────────────────────

@superuser_required
def billing_invoice_pdf(request, pk):
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    return _pdf_response(build_invoice_pdf(invoice), f'{invoice.number}.pdf',
                         inline=not request.GET.get('download'))


@superuser_required
def billing_receipt_pdf(request, pk):
    payment = get_object_or_404(PlatformPayment.objects.select_related('invoice'), pk=pk)
    return _pdf_response(build_receipt_pdf(payment), f'{payment.receipt_number}.pdf',
                         inline=not request.GET.get('download'))


# ── Actions (POST only) ──────────────────────────────────────────────────────

@require_POST
@superuser_required
def billing_invoice_email(request, pk):
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    if invoice.status == PlatformInvoice.Status.VOID:
        messages.error(request, 'A void invoice cannot be sent.')
        return redirect('billing_invoice_detail', pk=invoice.pk)
    ok, error = send_invoice_email(invoice)
    if ok:
        messages.success(request, f'{invoice.number} emailed to {invoice.bill_to_email}.')
    else:
        messages.error(request, error)
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_invoice_mark_sent(request, pk):
    """Issue a draft without emailing it, for an invoice handed over another
    way (WhatsApp, printed). From here it is locked like an emailed one."""
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    if invoice.status == PlatformInvoice.Status.DRAFT:
        invoice.status = PlatformInvoice.Status.SENT
        invoice.sent_at = timezone.now()
        invoice.save(update_fields=['status', 'sent_at', 'updated_at'])
        messages.success(request, f'{invoice.number} marked as sent.')
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_invoice_extend(request, pk):
    """Give the tenant more days: the due date moves to the later of the old
    due date and today, plus N days (the page's "7 more days" button sends 7).
    A not-paid countdown is called off, and the reminders start a fresh cycle
    against the new date because their log keys carry it."""
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    try:
        days = int(request.POST.get('days', ''))
    except ValueError:
        days = 0
    if not 1 <= days <= 90:
        messages.error(request, 'Give between 1 and 90 more days.')
        return redirect('billing_invoice_detail', pk=invoice.pk)
    if invoice.status == PlatformInvoice.Status.VOID:
        messages.error(request, 'A void invoice cannot be extended.')
        return redirect('billing_invoice_detail', pk=invoice.pk)
    invoice.due_date = max(invoice.due_date, timezone.localdate()) + timedelta(days=days)
    invoice.unpaid_confirmed_at = None
    invoice.switch_off_on = None
    invoice.save(update_fields=['due_date', 'unpaid_confirmed_at', 'switch_off_on', 'updated_at'])
    messages.success(request, f'{invoice.number} is now due on {invoice.due_date:%d %b %Y}. '
                              'Reminders will follow the new date.')
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_invoice_not_paid(request, pk):
    """The operator confirms the tenant has not paid. Starts the switch-off
    countdown (bot/billing_reminders.py): notices to the tenant the day after,
    3 days after, the day before and on the switch-off day, and an email to
    the operator on that day to pause them. Refused before the due date."""
    from ..billing_reminders import switch_off_date_for
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    today = timezone.localdate()
    if invoice.status != PlatformInvoice.Status.SENT or invoice.balance <= 0:
        messages.error(request, 'Only a sent invoice with money still owed can be marked not paid.')
    elif invoice.due_date > today:
        messages.error(request, 'This invoice is not due yet.')
    elif not invoice.unpaid_confirmed_at:
        invoice.unpaid_confirmed_at = timezone.now()
        invoice.switch_off_on = switch_off_date_for(invoice, today)
        invoice.save(update_fields=['unpaid_confirmed_at', 'switch_off_on', 'updated_at'])
        messages.success(request, f'Marked not paid. {invoice.bill_to_name} will be told their '
                                  f'service is switched off on {invoice.switch_off_on:%d %b %Y}.')
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_invoice_pause_account(request, pk):
    """Pause the tenant's account over this invoice. It sets the tenant
    inactive, the same switch as the console's Deactivate: the webhook stops
    answering their customers and their staff lose the dashboard. Homebase is
    refused, as it is on the console. Scheduled crons do not yet check it."""
    invoice = get_object_or_404(PlatformInvoice.objects.select_related('tenant'), pk=pk)
    tenant = invoice.tenant
    if tenant is None:
        messages.error(request, 'This tenant no longer exists.')
    elif tenant.slug == 'homebase':
        messages.error(request, 'Refusing to pause the homebase tenant.')
    elif tenant.is_active:
        tenant.is_active = False
        tenant.save(update_fields=['is_active'])
        invoice.account_paused_at = timezone.now()
        invoice.save(update_fields=['account_paused_at', 'updated_at'])
        messages.success(request, f'{tenant.name} is paused. Their bot no longer answers customers.')
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_invoice_resume_account(request, pk):
    """Switch a paused tenant back on, for when the money arrives."""
    invoice = get_object_or_404(PlatformInvoice.objects.select_related('tenant'), pk=pk)
    tenant = invoice.tenant
    if tenant is not None and not tenant.is_active:
        tenant.is_active = True
        tenant.save(update_fields=['is_active'])
        invoice.account_paused_at = None
        invoice.save(update_fields=['account_paused_at', 'updated_at'])
        messages.success(request, f'{tenant.name} is active again.')
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_invoice_reminders(request, pk):
    """This invoice's reminder settings: on or off, and its own days-before
    (blank = the Billing details default). The checkbox posts `enabled` only
    when ticked, so its absence is off."""
    from ..models import parse_reminder_days
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    invoice.reminders_enabled = bool(request.POST.get('enabled'))
    raw = (request.POST.get('reminder_days') or '').strip()
    invoice.reminder_days = ','.join(str(d) for d in parse_reminder_days(raw) if d) if raw else ''
    invoice.save(update_fields=['reminders_enabled', 'reminder_days', 'updated_at'])
    messages.success(request, 'Reminders on.' if invoice.reminders_enabled
                     else 'Reminders off for this invoice.')
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_invoice_void(request, pk):
    """Cancel an invoice, keeping its number and record. Refused while any
    receipt still stands against it: void those first, so the books never
    show money received on a cancelled invoice."""
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    if invoice.payments.filter(is_void=False).exists():
        messages.error(request, 'This invoice has payments recorded. Void those receipts first.')
    elif invoice.status != PlatformInvoice.Status.VOID:
        invoice.status = PlatformInvoice.Status.VOID
        invoice.voided_at = timezone.now()
        invoice.save(update_fields=['status', 'voided_at', 'updated_at'])
        messages.success(request, f'{invoice.number} voided.')
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_invoice_delete(request, pk):
    """Delete a DRAFT that never went out. Anything issued is voided instead,
    so its number keeps resolving."""
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    if invoice.status != PlatformInvoice.Status.DRAFT or invoice.payments.exists():
        messages.error(request, 'Only an unsent draft with no payments can be deleted. Void it instead.')
        return redirect('billing_invoice_detail', pk=invoice.pk)
    number = invoice.number
    invoice.delete()
    messages.success(request, f'Draft {number} deleted.')
    return redirect('billing_home')


@require_POST
@superuser_required
def billing_payment_add(request, pk):
    """Record money received against an invoice, which issues its receipt.

    The amount must be positive and no more than the balance: an overpayment
    would leave a negative balance with nowhere to go. Ticking "email the
    receipt" sends it straight away.
    """
    invoice = get_object_or_404(PlatformInvoice, pk=pk)
    if not invoice.can_take_payment:
        messages.error(request, 'This invoice cannot take a payment (it is void or already paid).')
        return redirect('billing_invoice_detail', pk=invoice.pk)
    amount = _parse_amount(request.POST.get('amount'))
    method = request.POST.get('method', '')
    if amount is None:
        messages.error(request, 'Enter the amount received, above zero.')
        return redirect('billing_invoice_detail', pk=invoice.pk)
    if amount > invoice.balance:
        messages.error(request, f'That is more than the balance of {invoice.currency}{invoice.balance:,.2f}.')
        return redirect('billing_invoice_detail', pk=invoice.pk)
    if method not in PlatformPayment.Method.values:
        method = PlatformPayment.Method.OTHER
    payment = PlatformPayment.objects.create(
        invoice=invoice, amount=amount, method=method,
        paid_on=_parse_date(request.POST.get('paid_on'), timezone.localdate()),
        reference=(request.POST.get('reference') or '').strip()[:120],
        note=(request.POST.get('note') or '').strip()[:255],
    )
    messages.success(request, f'Payment recorded. Receipt {payment.receipt_number} issued.')
    if request.POST.get('email_receipt'):
        ok, error = send_receipt_email(payment)
        if ok:
            messages.success(request, f'Receipt emailed to {invoice.bill_to_email}.')
        else:
            messages.error(request, error)
    return redirect('billing_invoice_detail', pk=invoice.pk)


@require_POST
@superuser_required
def billing_receipt_email(request, pk):
    payment = get_object_or_404(PlatformPayment.objects.select_related('invoice'), pk=pk)
    if payment.is_void:
        messages.error(request, 'A void receipt cannot be sent.')
    else:
        ok, error = send_receipt_email(payment)
        if ok:
            messages.success(request, f'{payment.receipt_number} emailed to {payment.invoice.bill_to_email}.')
        else:
            messages.error(request, error)
    return redirect('billing_invoice_detail', pk=payment.invoice_id)


@require_POST
@superuser_required
def billing_payment_void(request, pk):
    """Take back a payment recorded by mistake. The receipt keeps its number
    and reads VOID; the invoice's balance goes back up."""
    payment = get_object_or_404(PlatformPayment, pk=pk)
    if not payment.is_void:
        payment.is_void = True
        payment.voided_at = timezone.now()
        payment.save(update_fields=['is_void', 'voided_at'])
        messages.success(request, f'Receipt {payment.receipt_number} voided.')
    return redirect('billing_invoice_detail', pk=payment.invoice_id)


# ── Invoice templates ────────────────────────────────────────────────────────
#
# Named, saved line sets a new invoice starts from (owner, 2026-09-24: the
# generic "Plumbot Standard" invoice, US$150 with the breakdown plus the US$10
# website, "on the app"). See PlatformInvoiceTemplate.
# The editor uses the invoice form's own line markup and classes, so a
# template is built the way an invoice is.

class TemplateForm(forms.ModelForm):
    class Meta:
        model = PlatformInvoiceTemplate
        fields = ['name', 'is_default', 'notes']
        labels = {'is_default': 'Start new invoices from this template',
                  'notes': 'Notes printed on the invoice'}
        widgets = {'notes': forms.Textarea(attrs={'rows': 2})}


class TemplateLineForm(forms.Form):
    """One template line; blank ones are skipped on save."""
    description = forms.CharField(
        required=False, max_length=255,
        widget=forms.TextInput(attrs={'placeholder': 'What this line is for (use {month} for the month)',
                                      'aria-label': 'Description'}))
    quantity = forms.DecimalField(
        required=False, initial=1, max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={'placeholder': 'Qty', 'aria-label': 'Quantity', 'inputmode': 'decimal'}))
    unit_price = forms.DecimalField(
        required=False, initial=0, max_digits=10, decimal_places=2,
        widget=forms.NumberInput(attrs={'placeholder': 'Unit price', 'aria-label': 'Unit price',
                                        'inputmode': 'decimal'}))
    with_breakdown = forms.BooleanField(required=False)


TemplateLineFormSet = forms.formset_factory(TemplateLineForm, extra=2, can_delete=True)


def _template_lines(formset):
    """The formset as the template's JSON lines, in order, blank and removed
    rows dropped. Money is kept as strings so JSON never rounds it."""
    lines = []
    for form in formset.forms:
        data = getattr(form, 'cleaned_data', None) or {}
        if data.get('DELETE') or not (data.get('description') or '').strip():
            continue
        lines.append({
            'description': data['description'].strip(),
            'quantity': str(data.get('quantity') or 1),
            # To the cent: a typed "250" is stored "250.00", like a real line.
            'unit_price': str(Decimal(data.get('unit_price') or 0).quantize(Decimal('0.01'))),
            'with_breakdown': bool(data.get('with_breakdown')),
        })
    return lines


def _template_page(request, template=None):
    """Create (template=None) or edit a template: GET shows the editor, a
    valid POST saves and returns to Billing. A template needs one line."""
    if request.method == 'POST':
        form = TemplateForm(request.POST, instance=template)
        formset = TemplateLineFormSet(request.POST, prefix='lines')
        if form.is_valid() and formset.is_valid():
            lines = _template_lines(formset)
            if lines:
                saved = form.save(commit=False)
                saved.lines = lines
                saved.save()
                messages.success(request, f'Template "{saved.name}" saved.')
                return redirect('billing_home')
            messages.error(request, 'A template needs at least one line.')
    else:
        form = TemplateForm(instance=template)
        formset = TemplateLineFormSet(prefix='lines', initial=list(template.lines) if template else None)
    return render(request, 'bot/pages/billing_template_form.html', {
        'active_nav': 'billing', 'form': form, 'formset': formset, 'template': template,
    })


@superuser_required
def billing_template_new(request):
    return _template_page(request)


@superuser_required
def billing_template_edit(request, pk):
    return _template_page(request, get_object_or_404(PlatformInvoiceTemplate, pk=pk))


@require_POST
@superuser_required
def billing_template_delete(request, pk):
    """Delete a template. Invoices raised from it are untouched: they copied
    its lines when they were made."""
    template = get_object_or_404(PlatformInvoiceTemplate, pk=pk)
    name = template.name
    template.delete()
    messages.success(request, f'Template "{name}" deleted.')
    return redirect('billing_home')


@superuser_required
def billing_template_preview(request, pk):
    """The invoice this template would produce today, as a PDF, through the
    same renderer as a real invoice (bill to is a placeholder)."""
    from ..billing_pdf import build_template_pdf
    template = get_object_or_404(PlatformInvoiceTemplate, pk=pk)
    # ?zimbabwe=1 previews a Zimbabwean client's invoice (EcoCash first).
    data = build_template_pdf(template, PlatformBillingProfile.current(), timezone.localdate(),
                              zimbabwe=bool(request.GET.get('zimbabwe')))
    return _pdf_response(data, f'{template.name}.pdf', inline=not request.GET.get('download'))


# ── Public document links (the emails' "Download invoice / receipt") ────────

def billing_public_document(request, token):
    """Serve the invoice or receipt PDF a signed email link names, with no
    login (bot/billing_links.py): the tenant reading their email has no
    billing access, as with Stripe's links. The token names exactly one
    document; a forged, edited or unknown one is a 404. GET only, reads only."""
    from django.http import Http404
    from ..billing_links import read_token
    if request.method != 'GET':
        raise Http404
    found = read_token(token)
    if found is None:
        raise Http404
    kind, pk = found
    if kind == 'inv':
        invoice = get_object_or_404(PlatformInvoice, pk=pk)
        return _pdf_response(build_invoice_pdf(invoice), f'{invoice.number}.pdf')
    payment = get_object_or_404(PlatformPayment.objects.select_related('invoice'), pk=pk)
    return _pdf_response(build_receipt_pdf(payment), f'{payment.receipt_number}.pdf')
