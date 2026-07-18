"""Tenant portal: the owner's own Facebook/social offer.

This is the price the assistant LEADS with when a lead asks "how much"
with no context at all (usually straight off the ad) — see
ResponseMixin._compose_pricing_overview. Stored as the tenant's
TenantPriceItem(family='package', variant='facebook') row.
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from ..decorators import staff_required
from ..models import TenantPriceItem
from ..pricing_copy import facebook_package_facts
from ..tenant_config import get_config


def _tenant_or_404(request):
    tenant = getattr(request, 'tenant', None)
    if tenant is None:
        raise Http404
    return tenant


def _offer_row(tenant):
    return TenantPriceItem.objects.filter(
        tenant=tenant, family='package', variant='facebook').first()


@staff_required
def offer_page(request):
    tenant = _tenant_or_404(request)
    row = _offer_row(tenant)
    return render(request, 'bot/pages/offer.html', {
        'active_nav': 'offer',
        'row': row,
        'facts': facebook_package_facts(get_config(tenant)),
        'includes': '\n'.join(
            p.get('name', '') for p in ((row.parts if row else None) or [])),
    })


@staff_required
@require_POST
def offer_save(request):
    tenant = _tenant_or_404(request)
    label = (request.POST.get('label') or '').strip()
    price_raw = ((request.POST.get('price') or '')
                 .replace('US$', '').replace('$', '').replace(',', '').strip())
    includes = [line.strip() for line in
                (request.POST.get('includes') or '').splitlines() if line.strip()]

    if not price_raw:
        deleted, _ = TenantPriceItem.objects.filter(
            tenant=tenant, family='package', variant='facebook').delete()
        messages.success(
            request,
            'Offer removed — the assistant now deflects vague price questions '
            'to the free quote instead.' if deleted else 'No offer set.')
        return redirect('offer')

    try:
        price = Decimal(price_raw)
    except InvalidOperation:
        messages.error(request, 'Enter the price as a number, e.g. 800.')
        return redirect('offer')

    TenantPriceItem.objects.update_or_create(
        tenant=tenant, family='package', variant='facebook',
        defaults=dict(
            label=(label or 'Facebook special')[:120],
            flat=price,
            parts=[{'name': name[:80]} for name in includes[:12]],
        ))
    messages.success(
        request,
        'Offer saved — this is what the assistant leads with when someone '
        'asks "how much" without saying for what.')
    return redirect('offer')
