"""Which quote document a tenant gets, and the facts that fill it.

A tenant whose profile sets ``letterhead.layout = "sectioned"`` gets the
grouped quote sheet (numbered sections with their own subtotals, plus
discount and VAT); everyone else keeps the flat default. The switch is tenant
data, never a slug check, so one tenant's document can never render for
another — and every letterhead value comes from that tenant's own profile.
"""
from decimal import Decimal

from ..tenant_config import get_config

SECTIONED = 'sectioned'


def tenant_of(request, appointment=None, quotation=None):
    """The tenant whose document we are about to render.

    The lead owns the quote, so its tenant wins over the request's — a staff
    member browsing with one tenant selected must still see the lead's own
    letterhead, never a borrowed one.
    """
    if quotation is not None and getattr(quotation, 'appointment', None) is not None:
        appointment = quotation.appointment
    if appointment is not None and getattr(appointment, 'tenant_id', None):
        return appointment.tenant
    return getattr(request, 'tenant', None)


def layout_for(tenant) -> str:
    return get_config(tenant).quote_layout()


def is_sectioned(tenant) -> bool:
    return layout_for(tenant) == SECTIONED


def letterhead_for(tenant) -> dict:
    return get_config(tenant).letterhead()


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal('0')


def group_items(quotation):
    """The quotation's items, grouped into the sections they were entered in.

    Consecutive items sharing a section title stay together, so the sheet
    rebuilds in the order it was typed. Items with no section land in one
    untitled group, which is what a quote saved on the flat layout looks like.
    """
    groups = []
    for item in quotation.items.all():
        title = (item.section or '').strip()
        if not groups or groups[-1]['title'] != title:
            groups.append({'title': title, 'items': [], 'subtotal': Decimal('0.00')})
        groups[-1]['items'].append({
            'description': item.description,
            'qty_text': (item.quantity_text or '').strip() or _plain_qty(item.quantity),
            'qty': item.quantity,
            'unit': item.unit_price,
            'unit_price': item.unit_price,
            'total_price': item.total_price,
        })
        groups[-1]['subtotal'] += _dec(item.total_price)
    return groups


def _plain_qty(quantity):
    """'3' rather than '3.00' — a quantity reads as a count on the sheet."""
    value = _dec(quantity)
    if value == value.to_integral_value():
        return str(int(value))
    return str(value.normalize())


def sections_payload(quotation):
    """The saved sections in the shape the editor's JavaScript rebuilds from."""
    return [
        {
            'title': group['title'],
            'items': [
                {
                    'description': item['description'],
                    'qty': item['qty_text'],
                    'unit': str(item['unit']),
                }
                for item in group['items']
            ],
        }
        for group in group_items(quotation)
    ]


def quote_terms(quotation, letterhead) -> list:
    """A quote's payment terms.

    Stored as the quotation's notes — one term per line — because that is what
    `notes` holds on a sectioned quote. A new quote starts from the tenant's
    own default terms; absent means no terms block at all.
    """
    saved = [line.strip() for line in (quotation.notes or '').splitlines() if line.strip()]
    return saved or list(letterhead.get('terms') or [])


def document_context(quotation, letterhead) -> dict:
    """Everything the read-only sheet needs, computed the way the editor does:
    materials from the item lines, discount off the gross, VAT on the rest."""
    groups = group_items(quotation)
    materials = sum((group['subtotal'] for group in groups), Decimal('0.00'))
    gross = materials + _dec(quotation.labor_cost) + _dec(quotation.transport_cost)
    net = gross - _dec(quotation.discount)
    vat = net * _dec(quotation.vat_percent) / Decimal('100')
    return {
        'sections': groups,
        'materials_total': materials,
        'net_subtotal': net,
        'vat_amount': vat,
        'quote_terms': quote_terms(quotation, letterhead),
    }
