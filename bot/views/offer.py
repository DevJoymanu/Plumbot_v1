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
from ..tenant_config import get_config


def _tenant_or_404(request):
    tenant = getattr(request, 'tenant', None)
    if tenant is None:
        raise Http404
    return tenant


def _offer_row(tenant):
    return TenantPriceItem.objects.filter(
        tenant=tenant, family='package', variant='facebook').first()


# Common package contents, offered as tap-to-select chips ("Other" covers the
# rest) — selection over typing: consistent names, no spelling risk.
COMMON_INCLUDES = [
    'freestanding tub', 'built-in tub', 'side chamber', 'shower cubicle',
    'vanity unit', 'toilet', 'basin', 'geyser', 'taps & mixers', 'tiling',
]


def _compose_offer_preview(tenant):
    """The bot's ACTUAL vague-'how much' reply for this tenant — same code
    path the router uses (_compose_pricing_overview), so the preview never
    drifts from what customers receive. Shims the mixin's per-conversation
    bits (fresh-conversation state) around the tenant's real config."""
    from .plumbot.response_mixin import ResponseMixin as _RM
    cfg = get_config(tenant)

    class _Shim:
        tenant_cfg = cfg
        _compose_pricing_overview = _RM._compose_pricing_overview
        _freestanding_tub_price = _RM._freestanding_tub_price
        _price_components_map = _RM._price_components_map
        _product_price_close = _RM._product_price_close
        _price_tiedown = _RM._price_tiedown
        _ensure_price_disclaimer = _RM._ensure_price_disclaimer
        _PRICED_INTENTS = _RM._PRICED_INTENTS

        def _last_assistant_was_tiedown(self):
            return False  # preview = a fresh conversation
    try:
        return _Shim()._compose_pricing_overview('english')
    except Exception:
        return None


@staff_required
def offer_page(request):
    tenant = _tenant_or_404(request)
    row = _offer_row(tenant)
    return render(request, 'bot/pages/offer.html', {
        'active_nav': 'offer',
        'row': row,
        'preview': _compose_offer_preview(tenant),
        'common_includes': COMMON_INCLUDES,
        'includes_list': [p.get('name', '') for p in
                          ((row.parts if row else None) or []) if p.get('name')],
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

    from ..media_library import resync_portfolio_prices

    if not price_raw:
        deleted, _ = TenantPriceItem.objects.filter(
            tenant=tenant, family='package', variant='facebook').delete()
        resync_portfolio_prices(tenant)   # linked photos follow the offer
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
    resync_portfolio_prices(tenant)   # linked photos follow the offer price
    messages.success(
        request,
        'Offer saved — this is what the assistant leads with when someone '
        'asks "how much" without saying for what.')
    return redirect('offer')
