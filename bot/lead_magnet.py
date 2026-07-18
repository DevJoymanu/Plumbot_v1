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
from collections import OrderedDict
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

STORAGE_PREFIX = 'lead_magnets_pdfs'

# The design pool — same content, different colour/cover treatments. A tenant
# is pinned to one by design_index_for(); add/remove freely (cycling adapts).
LEAD_MAGNET_DESIGNS = [
    dict(key='ocean',  primary='#006591', accent='#0ea5e9', cover='band'),
    dict(key='forest', primary='#15803d', accent='#22c55e', cover='panel'),
    dict(key='amber',  primary='#b45309', accent='#f59e0b', cover='minimal'),
    dict(key='indigo', primary='#4648d4', accent='#6366f1', cover='band'),
    dict(key='slate',  primary='#334155', accent='#0ea5e9', cover='panel'),
]

# family → section title for the pricing guide.
_FAMILY_TITLES = {
    'shower': 'Showers', 'tub': 'Bathtubs', 'geyser': 'Geysers',
    'geyser_service': 'Geyser services', 'vanity': 'Vanities',
    'toilet': 'Toilets', 'chamber': 'Chambers', 'basin': 'Basins',
    'renovation': 'Renovations', 'package': 'Packages',
    'repair': 'Repairs & maintenance',
}


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


def _pricing_sections(tenant, cur):
    """{section title: [(label, price str), …]} from the tenant's priced items."""
    from .models import TenantPriceItem
    sections = OrderedDict()
    for item in TenantPriceItem.objects.filter(tenant=tenant, is_active=True):
        price = _price_str(item, cur)
        if not price:
            continue
        title = _FAMILY_TITLES.get(item.family, item.family.replace('_', ' ').title())
        sections.setdefault(title, []).append((item.label or item.family, price))
    return sections


def _portfolio_images(tenant, limit=4):
    """Up to `limit` local image paths for the tenant's gallery (best-effort;
    remote files are materialised to temp). Returns [(local_path, is_temp), …]."""
    out = []
    try:
        from .whatsapp_webhook import _materialize_image, get_previous_work_images
        for path in (get_previous_work_images(tenant) or [])[:limit]:
            local, is_temp = _materialize_image(path)
            if local and os.path.exists(local):
                out.append((local, is_temp))
    except Exception:
        logger.exception('lead-magnet portfolio images failed for %s',
                         getattr(tenant, 'slug', None))
    return out


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
        from reportlab.lib.utils import ImageReader
        from reportlab.platypus import (HRFlowable, Image as RLImage, Paragraph,
                                        SimpleDocTemplate, Spacer, Table, TableStyle)

        from .tenant_config import get_config

        cfg = get_config(tenant)
        design = design_for(tenant)
        primary = colors.HexColor(design['primary'])
        accent = colors.HexColor(design['accent'])
        dark = colors.HexColor('#1a1a1a')
        mid = colors.HexColor('#555555')
        white = colors.white

        business = (getattr(tenant, 'name', '') or 'Our').strip()
        cur = cfg.currency or 'US$'

        styles = getSampleStyleSheet()
        cover_title = ParagraphStyle('ct', parent=styles['Heading1'], fontSize=26,
                                     textColor=white, leading=30, spaceAfter=4)
        cover_sub = ParagraphStyle('cs', parent=styles['Normal'], fontSize=12,
                                   textColor=white, leading=16)
        h2 = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=13,
                            textColor=primary, spaceBefore=14, spaceAfter=4,
                            fontName='Helvetica-Bold')
        body = ParagraphStyle('body', parent=styles['Normal'], fontSize=10,
                              textColor=mid, leading=15)
        item_l = ParagraphStyle('il', parent=styles['Normal'], fontSize=9.5,
                               textColor=dark, leading=13)
        item_p = ParagraphStyle('ip', parent=styles['Normal'], fontSize=9.5,
                               textColor=primary, leading=13, fontName='Helvetica-Bold',
                               alignment=2)
        note = ParagraphStyle('note', parent=styles['Normal'], fontSize=8,
                             textColor=mid, leading=11)

        buffer = BytesIO()
        margin = 1.6 * cm
        doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=margin,
                                rightMargin=margin, topMargin=margin, bottomMargin=margin)
        W = A4[0] - 2 * margin
        story = []

        # ── Cover (style varies by design) ──
        contact_bits = [b for b in (
            f'WhatsApp {cfg.business_whatsapp}' if cfg.business_whatsapp else '',
            f'Call {cfg.plumber_contact}' if cfg.plumber_contact else '',
        ) if b]
        cover_inner = [Paragraph(business, cover_title),
                       Paragraph('Portfolio &amp; Pricing Guide', cover_sub)]
        if contact_bits:
            cover_inner.append(Spacer(1, 6))
            cover_inner.append(Paragraph(' &nbsp;·&nbsp; '.join(contact_bits), cover_sub))
        cover = Table([[cover_inner]], colWidths=[W])
        cover_style = [('LEFTPADDING', (0, 0), (-1, -1), 18),
                       ('RIGHTPADDING', (0, 0), (-1, -1), 18),
                       ('TOPPADDING', (0, 0), (-1, -1), 22),
                       ('BOTTOMPADDING', (0, 0), (-1, -1), 22)]
        if design['cover'] == 'minimal':
            # Light cover: dark text on white with an accent rule underneath.
            cover_title.textColor = dark
            cover_sub.textColor = mid
            cover_style += [('LINEBELOW', (0, 0), (-1, -1), 3, accent)]
        elif design['cover'] == 'panel':
            cover_style += [('BACKGROUND', (0, 0), (-1, -1), primary),
                            ('LINEBEFORE', (0, 0), (0, -1), 8, accent)]
        else:  # band
            cover_style += [('BACKGROUND', (0, 0), (-1, -1), primary)]
        cover.setStyle(TableStyle(cover_style))
        story += [cover, Spacer(1, 16)]

        # ── Intro ──
        intro_bits = []
        loc = cfg.location_short()
        if loc:
            intro_bits.append(f'Based in {loc}')
        hrs = cfg.hours_sentence()
        if hrs:
            intro_bits.append(f'open {hrs}')
        intro = '. '.join(intro_bits)
        story.append(Paragraph(
            (f'{intro}. ' if intro else '') +
            f'{business} handles the full range of plumbing — installs, renovations, '
            'geysers, drains and repairs. Below is our previous work and a guide to our '
            'pricing so you know what to expect before we start.', body))

        # ── Pricing ──
        sections = _pricing_sections(tenant, cur)
        if sections:
            story += [Spacer(1, 6), Paragraph('Pricing guide', h2),
                      HRFlowable(width='100%', thickness=1, color=accent, spaceAfter=6)]
            for title, rows in sections.items():
                story.append(Paragraph(title, ParagraphStyle(
                    't', parent=body, fontSize=10.5, textColor=dark,
                    fontName='Helvetica-Bold', spaceBefore=8, spaceAfter=2)))
                data = [[Paragraph(label, item_l), Paragraph(price, item_p)]
                        for label, price in rows]
                tbl = Table(data, colWidths=[W * 0.72, W * 0.28])
                tbl.setStyle(TableStyle([
                    ('LINEBELOW', (0, 0), (-1, -2), 0.5, colors.HexColor('#eeeeee')),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ]))
                story.append(tbl)
            story.append(Spacer(1, 4))
            story.append(Paragraph(
                'Prices are "from" starting rates — the free on-site visit confirms the '
                'exact figure before any work begins.', note))

        # ── Portfolio images (best-effort, 2-up) ──
        temps = _portfolio_images(tenant, limit=4)
        if temps:
            story += [Spacer(1, 10), Paragraph('Previous work', h2),
                      HRFlowable(width='100%', thickness=1, color=accent, spaceAfter=8)]
            cell_w = (W - 0.5 * cm) / 2
            flow, row = [], []
            for local, _t in temps:
                try:
                    iw, ih = ImageReader(local).getSize()
                    img = RLImage(local, width=cell_w, height=cell_w * ih / iw)
                    row.append(img)
                    if len(row) == 2:
                        flow.append(row)
                        row = []
                except Exception:
                    continue
            if row:
                row.append('')
                flow.append(row)
            if flow:
                grid = Table(flow, colWidths=[cell_w, cell_w])
                grid.setStyle(TableStyle([
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                    ('TOPPADDING', (0, 0), (-1, -1), 0),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ]))
                story.append(grid)

        doc.build(story)
        return buffer.getvalue()
    except Exception:
        logger.exception('build_lead_magnet_pdf failed for %s',
                         getattr(tenant, 'slug', None))
        return None
    finally:
        for local, is_temp in temps:
            if is_temp:
                try:
                    os.unlink(local)
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
