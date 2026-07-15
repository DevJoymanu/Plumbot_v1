"""
Platform (developer) console — docs/MULTI_TENANT_PLAN.md §3.4.

Superuser-only operator screens: tenant list, create/deactivate,
impersonation (the session tenant switcher), and the per-tenant config
editor (profile + price sheet). The onboarding wizard and intake
draft→approve flow build on these (Phase 3.3/5).
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Count
from django.forms import modelformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from ..middleware import TENANT_SESSION_KEY
from ..models import Tenant, TenantPriceItem, TenantProfile


def _superuser(user):
    return user.is_active and user.is_superuser


superuser_required = user_passes_test(_superuser)


@require_POST
@superuser_required
def switch_tenant(request):
    """Superuser-only: pin a tenant on the session (impersonation — the
    dashboard then shows that tenant's world)."""
    slug = request.POST.get('tenant', '')
    tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
    if tenant is None:
        messages.error(request, 'Unknown or inactive tenant.')
    else:
        request.session[TENANT_SESSION_KEY] = tenant.slug
        messages.success(request, f'Now viewing tenant: {tenant.name}')
    return redirect(request.POST.get('next') or 'dashboard')


@superuser_required
def platform_console(request):
    """Tenant list — the console home."""
    tenants = (
        Tenant.objects
        .annotate(lead_count=Count('appointments'))
        .prefetch_related('whatsapp_channels')
        .order_by('name')
    )
    rows = []
    for tenant in tenants:
        channel = next(iter(tenant.whatsapp_channels.all()), None)
        rows.append({
            'tenant': tenant,
            'lead_count': tenant.lead_count,
            'channel': channel,
            'has_profile': TenantProfile.objects.filter(tenant=tenant).exists(),
        })
    return render(request, 'bot/pages/platform_console.html', {
        'rows': rows,
        'active_nav': 'platform',
        'current_tenant': getattr(request, 'tenant', None),
    })


@require_POST
@superuser_required
def platform_create_tenant(request):
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, 'A tenant needs a name.')
        return redirect('platform_console')
    slug = slugify(request.POST.get('slug') or name)[:60]
    if Tenant.objects.filter(slug=slug).exists():
        messages.error(request, f'Slug "{slug}" is already taken.')
        return redirect('platform_console')
    tenant = Tenant.objects.create(name=name, slug=slug)
    TenantProfile.objects.create(tenant=tenant)  # blank — nullability rule
    messages.success(request, f'Tenant "{name}" created. Fill in their config before go-live.')
    return redirect('platform_tenant_config', slug=tenant.slug)


@require_POST
@superuser_required
def platform_toggle_tenant(request, slug):
    tenant = get_object_or_404(Tenant, slug=slug)
    if tenant.slug == 'homebase' and tenant.is_active:
        messages.error(request, 'Refusing to deactivate the homebase tenant from the console.')
        return redirect('platform_console')
    tenant.is_active = not tenant.is_active
    tenant.save(update_fields=['is_active'])
    state = 'activated' if tenant.is_active else 'deactivated'
    messages.success(request, f'Tenant "{tenant.name}" {state}.')
    return redirect('platform_console')


class TenantProfileForm(forms.ModelForm):
    class Meta:
        model = TenantProfile
        fields = [
            'plumber_name', 'plumber_contact', 'business_whatsapp',
            'location_line', 'location_area', 'location_city',
            'business_hours', 'timezone_name', 'excluded_areas',
            'currency', 'packages', 'faq_facts', 'scripts',
            'licensed_claim_enabled', 'email_from_name', 'email_sender',
        ]
        widgets = {
            field: forms.Textarea(attrs={'rows': 3})
            for field in ('business_hours', 'excluded_areas', 'packages', 'faq_facts', 'scripts')
        }


PriceItemFormSet = modelformset_factory(
    TenantPriceItem,
    fields=['family', 'variant', 'label', 'short_label',
            'supply', 'labour', 'flat', 'allin', 'sort_order', 'is_active'],
    extra=1, can_delete=True,
)


@superuser_required
def platform_tenant_config(request, slug):
    """Per-tenant config editor: profile + price sheet. The ONLY place tenant
    config is edited until the owner intake flow ships (decision #2)."""
    tenant = get_object_or_404(Tenant, slug=slug)
    profile, _ = TenantProfile.objects.get_or_create(tenant=tenant)
    prices_qs = TenantPriceItem.objects.filter(tenant=tenant)

    if request.method == 'POST':
        form = TenantProfileForm(request.POST, instance=profile)
        formset = PriceItemFormSet(request.POST, queryset=prices_qs)
        if form.is_valid() and formset.is_valid():
            form.save()
            items = formset.save(commit=False)
            for item in items:
                item.tenant = tenant
                item.save()
            for item in formset.deleted_objects:
                item.delete()
            from ..whatsapp_cloud_api import invalidate_client_cache
            invalidate_client_cache()
            messages.success(request, f'Config saved for {tenant.name}.')
            return redirect('platform_tenant_config', slug=tenant.slug)
        messages.error(request, 'Fix the errors below — nothing was saved.')
    else:
        form = TenantProfileForm(instance=profile)
        formset = PriceItemFormSet(queryset=prices_qs)

    channel = tenant.whatsapp_channels.first()
    return render(request, 'bot/pages/platform_tenant_config.html', {
        'tenant': tenant,
        'form': form,
        'formset': formset,
        'channel': channel,
        'active_nav': 'platform',
    })
