"""Per-tenant lead-magnet PDF (portfolio + pricing guide).

One design per tenant, rotated across a small pool of visual themes so the
fleet doesn't all look identical, but the CONTENT is always the tenant's own:
their name, contact, hours, service area and price list. Built with reportlab
and cached in object storage under lead_magnets_pdfs/<slug>/portfolio.pdf, so
the WhatsApp/email send paths just fetch the tenant's file.

Regenerated (in the background) whenever a tenant's config or prices change.
"""
import logging
import os
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

STORAGE_PREFIX = 'lead_magnets_pdfs'

# The design pool — homebase's portfolio layout in one theme colour per tenant
# (deterministic). Add/remove freely; the cycling adapts.
LEAD_MAGNET_DESIGNS = [
    dict(key='ocean',  primary='#006591'),
    dict(key='forest', primary='#15803d'),   # homebase-style green
    dict(key='amber',  primary='#b45309'),
    dict(key='indigo', primary='#4648d4'),
    dict(key='slate',  primary='#334155'),
]


def design_index_for(tenant) -> int:
    """Deterministic design slot for a tenant (rotates across the pool)."""
    return (getattr(tenant, 'pk', None) or 0) % len(LEAD_MAGNET_DESIGNS)


def design_for(tenant) -> dict:
    return LEAD_MAGNET_DESIGNS[design_index_for(tenant)]


def storage_path(tenant) -> str:
    slug = getattr(tenant, 'slug', None) or 'homebase'
    return f'{STORAGE_PREFIX}/{slug}/portfolio.pdf'


# ── Pricing content (from the tenant's own price list) ──────────────────────
def _money(value, cur):
    if value is None:
        return None
    value = int(value) if value == int(value) else value
    return f'{cur}{value}'


def _price_str(item, cur):
    parts = [p for p in (item.parts or []) if p.get('amount') not in (None, '')]
    if item.allin is not None:
        return f'from {_money(item.allin, cur)}'
    if item.flat is not None:
        return f'from {_money(item.flat, cur)}'
    if item.supply is not None or item.labour is not None:
        return f'from {_money((item.supply or 0) + (item.labour or 0), cur)}'
    if parts:
        return f'from {_money(sum(p["amount"] for p in parts), cur)}'
    return None


def _portfolio_images(tenant, limit=6):
    """Up to `limit` (local_path, is_temp, caption) for the tenant's gallery
    (best-effort; remote files are materialised to temp)."""
    out = []
    try:
        from . import portfolio_catalog
        from .whatsapp_webhook import _materialize_image, get_previous_work_images
        for path in (get_previous_work_images(tenant) or [])[:limit]:
            local, is_temp = _materialize_image(path)
            if not local or not os.path.exists(local):
                continue
            try:
                cap = portfolio_catalog.build_gallery_caption(path, tenant=tenant)
            except Exception:
                cap = None
            out.append((local, is_temp, cap or 'Completed project'))
    except Exception:
        logger.exception('lead-magnet portfolio images failed for %s',
                         getattr(tenant, 'slug', None))
    return out


# Pricing sections mirror homebase's portfolio PDF layout.
_FITTING_FAMILIES = ['shower', 'tub', 'vanity', 'toilet', 'chamber', 'basin', 'geyser']
_PRICING_SECTIONS = [
    ('Full Renovations & Packages', ['renovation', 'package'], 'cost'),
    ('Bathroom Fittings', _FITTING_FAMILIES, 'fitting'),
    ('Geyser Services', ['geyser_service'], 'cost'),
    ('Repairs & Maintenance', ['repair'], 'cost'),
]


def _fitting_cells(item, cur):
    """(supply, install, all-in) strings for a fitting row — parts breakdown
    goes in the install column when there's no plain labour figure."""
    parts = [p for p in (item.parts or []) if p.get('amount') not in (None, '')]
    supply = _money(item.supply, cur) or '—'
    if item.labour is not None:
        install = _money(item.labour, cur)
    elif parts:
        install = ' + '.join(f"{p.get('name', '').title()} {_money(p['amount'], cur)}"
                             for p in parts) or '—'
    else:
        install = '—'
    allin_val = item.allin
    if allin_val is None:
        allin_val = item.flat
    if allin_val is None and (item.supply is not None or item.labour is not None):
        allin_val = (item.supply or 0) + (item.labour or 0)
    if allin_val is None and parts:
        allin_val = sum(p['amount'] for p in parts)
    return supply, install, (_money(allin_val, cur) or '—')


# ── PDF build ───────────────────────────────────────────────────────────────
def build_lead_magnet_pdf(tenant):
    """The tenant's portfolio+pricing PDF as bytes, in their assigned design.
    Returns None on failure (callers fall back)."""
    temps = []
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (HRFlowable, Image as RLImage, KeepTogether,
                                        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

        from .models import TenantPriceItem
        from .tenant_config import get_config

        cfg = get_config(tenant)
        theme = colors.HexColor(design_for(tenant)['primary'])   # replaces homebase's green
        dark = colors.HexColor('#1a1a1a')
        mid = colors.HexColor('#555555')
        light = colors.HexColor('#eeeeee')
        white = colors.white

        business = (getattr(tenant, 'name', '') or 'Our').strip()
        cur = cfg.currency or 'US$'
        wa, call = cfg.business_whatsapp, cfg.plumber_contact
        loc = cfg.location_short()

        styles = getSampleStyleSheet()
        h1 = ParagraphStyle('h1', parent=styles['Heading1'], fontSize=24, textColor=dark, spaceAfter=2, spaceBefore=0)
        sub = ParagraphStyle('sub', parent=styles['Normal'], fontSize=11, textColor=mid, spaceAfter=0)
        h2 = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=13, textColor=theme, spaceAfter=4, spaceBefore=14, fontName='Helvetica-Bold')
        body = ParagraphStyle('body', parent=styles['Normal'], fontSize=9, textColor=mid, leading=13)
        caption = ParagraphStyle('cap', parent=styles['Normal'], fontSize=8, textColor=mid, leading=12, fontName='Helvetica-Oblique', spaceAfter=8)
        th = ParagraphStyle('th', parent=styles['Normal'], fontSize=8, textColor=white, fontName='Helvetica-Bold')
        td = ParagraphStyle('td', parent=styles['Normal'], fontSize=8, textColor=dark, leading=11)
        td_mid = ParagraphStyle('tdm', parent=styles['Normal'], fontSize=8, textColor=mid, leading=11)
        note = ParagraphStyle('note', parent=styles['Normal'], fontSize=7, textColor=mid, leading=10, fontName='Helvetica-Oblique')

        buffer = BytesIO()
        margin = 1.8 * cm
        doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=margin, rightMargin=margin,
                                topMargin=margin, bottomMargin=margin)
        AW = A4[0] - 2 * margin

        def styled_table(data, col_widths):
            t = Table(data, colWidths=col_widths, repeatRows=1)
            t.setStyle(TableStyle([
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#dddddd')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, colors.HexColor('#f7f7f7')]),
                ('BACKGROUND', (0, 0), (-1, 0), theme),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ]))
            return t

        elems = []

        # ── Header ──
        elems.append(Paragraph(business, h1))
        elems.append(Paragraph(
            f'Trusted plumbing specialists in {loc}' if loc else 'Trusted plumbing specialists', sub))
        elems.append(HRFlowable(width='100%', thickness=2, color=theme, spaceAfter=14, spaceBefore=6))

        # ── Our Previous Work ──
        elems.append(Paragraph('Our Previous Work', h2))
        elems.append(Paragraph(
            "Every project below was completed with a focus on quality, cleanliness, "
            "and care for the client's home.", body))
        elems.append(Spacer(1, 0.3 * cm))
        temps = _portfolio_images(tenant, limit=6)
        if temps:
            cell_w = (AW - 0.4 * cm) / 2
            img_h = 5.5 * cm
            for i in range(0, len(temps), 2):
                pair = temps[i:i + 2]
                imgs, caps = [], []
                for local, _t, cap in pair:
                    try:
                        imgs.append(RLImage(local, width=cell_w, height=img_h))
                    except Exception:
                        imgs.append(Paragraph('(photo)', td_mid))
                    caps.append(Paragraph(cap, caption))
                while len(imgs) < 2:
                    imgs.append('')
                    caps.append('')
                img_row = Table([imgs], colWidths=[cell_w, cell_w])
                img_row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                                             ('LEFTPADDING', (0, 0), (-1, -1), 2), ('RIGHTPADDING', (0, 0), (-1, -1), 2),
                                             ('BOTTOMPADDING', (0, 0), (-1, -1), 2)]))
                cap_row = Table([caps], colWidths=[cell_w, cell_w])
                cap_row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 2),
                                             ('RIGHTPADDING', (0, 0), (-1, -1), 2), ('TOPPADDING', (0, 0), (-1, -1), 3),
                                             ('BOTTOMPADDING', (0, 0), (-1, -1), 8)]))
                elems.append(KeepTogether([img_row, cap_row]))
        else:
            msg = 'Portfolio photos available on request.'
            if wa:
                msg += f" Message us on WhatsApp ({wa}) and we'll send examples of our completed work."
            elems.append(Paragraph(msg, body))

        elems.append(Spacer(1, 0.2 * cm))
        elems.append(HRFlowable(width='100%', thickness=1, color=light, spaceAfter=4, spaceBefore=4))

        # ── Complete Services & Pricing Guide (styled tables per section) ──
        by_family = {}
        for item in TenantPriceItem.objects.filter(tenant=tenant, is_active=True):
            by_family.setdefault(item.family, []).append(item)
        pricing_elems, any_priced = [], False
        for title, families, kind in _PRICING_SECTIONS:
            rows = []
            for fam in families:
                for item in by_family.get(fam, []):
                    if kind == 'fitting':
                        supply, install, allin = _fitting_cells(item, cur)
                        if supply == '—' and install == '—' and allin == '—':
                            continue
                        rows.append([Paragraph(item.label or fam, td), Paragraph(supply, td_mid),
                                     Paragraph(install, td_mid), Paragraph(allin, td)])
                    else:
                        price = _price_str(item, cur)
                        if not price:
                            continue
                        rows.append([Paragraph(item.label or fam, td),
                                     Paragraph(price.replace('from ', 'From '), td)])
            if not rows:
                continue
            any_priced = True
            pricing_elems.append(Paragraph(title, h2))
            if kind == 'fitting':
                header = [Paragraph(x, th) for x in ['Item', 'Supply (from)', 'Install (from)', 'All-in (from)']]
                widths = [AW * 0.34, AW * 0.20, AW * 0.26, AW * 0.20]
            else:
                header = [Paragraph(x, th) for x in ['Service', 'Cost (from)']]
                widths = [AW * 0.66, AW * 0.34]
            pricing_elems.append(styled_table([header] + rows, widths))
            pricing_elems.append(Spacer(1, 0.2 * cm))

        if any_priced:
            elems.append(Paragraph('Complete Services & Pricing Guide', h2))
            elems.append(Paragraph(
                f'All prices are in {cur}. Supply and labour vary by fixture choice, site '
                'conditions, and scope of work. A free on-site assessment gives you an exact '
                'written quote with no obligation.', body))
            elems.append(Spacer(1, 0.25 * cm))
            elems += pricing_elems
            elems.append(Paragraph(
                '* Labour prices are for work only; parts and fixtures are charged separately '
                'unless stated as all-in. All prices are starting rates — we confirm the final '
                'price before starting work.', note))

        elems.append(Spacer(1, 0.3 * cm))
        elems.append(HRFlowable(width='100%', thickness=1, color=light, spaceAfter=8, spaceBefore=4))

        # ── Footer ──
        bits = []
        if wa:
            bits.append(f'WhatsApp: {wa}')
        if call:
            bits.append(f'Call: {call}')
        elems.append(Paragraph(
            'Ready to book a free on-site assessment?  ' + '   |   '.join(bits), body))
        elems.append(Paragraph(
            'All work carried out by experienced plumbers. Satisfaction guaranteed on every job.',
            note))

        doc.build(elems)
        return buffer.getvalue()
    except Exception:
        logger.exception('build_lead_magnet_pdf failed for %s',
                         getattr(tenant, 'slug', None))
        return None
    finally:
        for entry in temps:
            if entry[1]:  # is_temp
                try:
                    os.unlink(entry[0])
                except OSError:
                    pass


# ── Storage (cache in R2) ────────────────────────────────────────────────────
def get_or_build_lead_magnet(tenant, force=False):
    """Storage path of the tenant's lead magnet, building + caching it if
    missing (or `force`). Returns None if it couldn't be built."""
    if tenant is None:
        return None
    path = storage_path(tenant)
    try:
        if not force and default_storage.exists(path):
            return path
    except Exception:
        logger.exception('lead-magnet exists() failed for %s', path)
    data = build_lead_magnet_pdf(tenant)
    if not data:
        return None
    try:
        if default_storage.exists(path):
            default_storage.delete(path)
        default_storage.save(path, ContentFile(data))
    except Exception:
        logger.exception('lead-magnet save failed for %s', path)
        return None
    return path


def lead_magnet_bytes(tenant):
    """The tenant's lead-magnet PDF as bytes (from cache, else freshly built)."""
    if tenant is None:
        return None
    path = get_or_build_lead_magnet(tenant)
    if not path:
        return None
    try:
        with default_storage.open(path, 'rb') as fh:
            return fh.read()
    except Exception:
        logger.exception('lead-magnet read failed for %s', path)
        return build_lead_magnet_pdf(tenant)


def invalidate_lead_magnet(tenant):
    """Drop the cached PDF so it rebuilds on next need (config/prices changed)."""
    if tenant is None:
        return
    path = storage_path(tenant)
    try:
        if default_storage.exists(path):
            default_storage.delete(path)
    except Exception:
        logger.exception('lead-magnet invalidate failed for %s', path)


def regenerate_lead_magnet_async(tenant):
    """Rebuild + cache in a daemon thread (keeps config saves snappy)."""
    if tenant is None:
        return
    import threading

    def _work():
        try:
            get_or_build_lead_magnet(tenant, force=True)
        except Exception:
            logger.exception('lead-magnet async regen failed for %s',
                             getattr(tenant, 'slug', None))
    threading.Thread(target=_work, daemon=True).start()
