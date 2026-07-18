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


def _portfolio_cards(tenant, limit=6):
    """Up to `limit` dicts {local, is_temp, title, desc} for the tenant's
    gallery (best-effort; remote files materialised to temp)."""
    cards = []
    try:
        from .whatsapp_webhook import _is_foreign_tenant, _materialize_image
        if _is_foreign_tenant(tenant):
            from .models import TenantPortfolioItem
            for it in TenantPortfolioItem.objects.filter(tenant=tenant, is_active=True)[:limit]:
                local, is_temp = _materialize_image(it.filename)
                if local and os.path.exists(local):
                    cards.append(dict(local=local, is_temp=is_temp,
                                      title=(it.title or 'Completed project'),
                                      desc=(it.description or '')))
        else:
            from . import portfolio_catalog
            from .whatsapp_webhook import get_previous_work_images
            for path in (get_previous_work_images(tenant) or [])[:limit]:
                local, is_temp = _materialize_image(path)
                if not local or not os.path.exists(local):
                    continue
                try:
                    cap = portfolio_catalog.build_gallery_caption(path, tenant=tenant)
                except Exception:
                    cap = None
                cards.append(dict(local=local, is_temp=is_temp,
                                  title=(cap or 'Completed project'), desc=''))
    except Exception:
        logger.exception('lead-magnet portfolio cards failed for %s',
                         getattr(tenant, 'slug', None))
    return cards


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
    """The tenant's portfolio+pricing PDF as bytes — the HomeBase portfolio
    design (dark hero cover, green feature band, photo cards with caption
    overlays, dark-header pricing tables with all-in pills, dark contact footer),
    in the tenant's theme colour. Returns None on failure (callers fall back)."""
    cards = []
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import (Flowable, HRFlowable, KeepTogether, PageBreak,
                                        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

        from .models import TenantPriceItem
        from .tenant_config import get_config

        cfg = get_config(tenant)
        theme = colors.HexColor(design_for(tenant)['primary'])
        dark_bg = colors.HexColor('#141414')
        dark = colors.HexColor('#1a1a1a')
        mid = colors.HexColor('#555555')
        light = colors.HexColor('#eeeeee')
        white = colors.white

        business = (getattr(tenant, 'name', '') or 'Our Plumbing').strip()
        initials = (''.join(w[0] for w in business.split()[:2]).upper() or 'HB')
        cur = cfg.currency or 'US$'
        wa, call = cfg.business_whatsapp, cfg.plumber_contact
        loc = cfg.location_short()

        PW, PH = A4
        margin = 1.6 * cm
        AW = PW - 2 * margin

        styles = getSampleStyleSheet()
        serif = 'Times-Bold'   # serif display face, mirrors the source's headline font
        S = lambda **k: ParagraphStyle(k.pop('name', 'x'), parent=styles['Normal'], **k)
        badge = S(fontName='Helvetica-Bold', fontSize=9, textColor=white, alignment=TA_CENTER, leading=11)
        hero = S(fontName=serif, fontSize=34, textColor=white, leading=38)
        hero_sub = S(fontSize=12, textColor=colors.HexColor('#c8c8c8'), leading=17)
        feat = S(fontName='Helvetica-Bold', fontSize=9, textColor=white, leading=12)
        eyebrow = S(fontName='Helvetica-Bold', fontSize=9, textColor=theme, leading=12)
        display = S(fontName=serif, fontSize=26, textColor=dark, leading=29)
        secttitle = S(fontName=serif, fontSize=15, textColor=dark, leading=18)
        body = S(fontSize=9.5, textColor=mid, leading=14)
        th = S(fontName='Helvetica-Bold', fontSize=7.5, textColor=white, leading=10)
        thr = S(fontName='Helvetica-Bold', fontSize=7.5, textColor=white, leading=10, alignment=TA_RIGHT)
        td = S(fontName='Helvetica-Bold', fontSize=8, textColor=dark, leading=11)
        td_mid = S(fontSize=8, textColor=mid, leading=11, alignment=TA_RIGHT)
        price_g = S(fontName='Helvetica-Bold', fontSize=8.5, textColor=theme, leading=11, alignment=TA_RIGHT)
        note = S(fontSize=7, textColor=mid, leading=10)
        foot_h = S(fontName=serif, fontSize=17, textColor=white, leading=20)
        foot_b = S(fontSize=9, textColor=colors.HexColor('#c8c8c8'), leading=14)
        foot_k = S(fontName='Helvetica-Bold', fontSize=7.5, textColor=colors.HexColor('#999999'), leading=11)
        foot_v = S(fontName='Helvetica-Bold', fontSize=10, textColor=white, leading=13)

        def pill(text, bg, fg=white, size=6.5):
            p = Paragraph(text, S(fontName='Helvetica-Bold', fontSize=size, textColor=fg, alignment=TA_CENTER))
            t = Table([[p]], colWidths=[1.15 * cm])
            t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), bg),
                                   ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                                   ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                                   ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
            return t

        def price_cell(text, allin=False):
            p = Paragraph(text, price_g)
            if not allin:
                return p
            inner = Table([[p], [pill('ALL-IN', theme)]], colWidths=[2.4 * cm])
            inner.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'RIGHT'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                       ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 0),
                                       ('BOTTOMPADDING', (0, 0), (-1, 0), 2), ('BOTTOMPADDING', (0, 1), (-1, 1), 0)]))
            return inner

        class PhotoCard(Flowable):
            def __init__(self, path, title, desc, width, height):
                super().__init__()
                self.path, self.title, self.desc = path, title, desc
                self.width, self.height = width, height

            def _fit(self, text, font, size):
                while text and self.canv.stringWidth(text, font, size) > self.width - 16:
                    text = text[:-1]
                return text

            def draw(self):
                c = self.canv
                try:
                    c.drawImage(self.path, 0, 0, self.width, self.height,
                                preserveAspectRatio=False, mask='auto')
                except Exception:
                    c.setFillColor(colors.HexColor('#e9edef'))
                    c.rect(0, 0, self.width, self.height, fill=1, stroke=0)
                oh = min(self.height * 0.42, 1.7 * cm)
                c.saveState()
                path = c.beginPath(); path.rect(0, 0, self.width, oh); c.clipPath(path, stroke=0, fill=0)
                c.linearGradient(0, 0, 0, oh, [colors.Color(0, 0, 0, 0.78), colors.Color(0, 0, 0, 0)], extend=True)
                c.restoreState()
                c.setFillColor(white); c.setFont('Helvetica-Bold', 8.5)
                c.drawString(9, oh - 15, self._fit(self.title, 'Helvetica-Bold', 8.5))
                if self.desc:
                    c.setFillColor(colors.Color(1, 1, 1, 0.82)); c.setFont('Helvetica', 7)
                    c.drawString(9, oh - 27, self._fit(self.desc, 'Helvetica', 7))

        def cover_bg(c, d):
            c.saveState()
            c.setFillColor(dark_bg); c.rect(0, 0, PW, PH, fill=1, stroke=0)
            c.saveState()
            bar = c.beginPath(); bar.rect(0, PH - 7, PW, 7); c.clipPath(bar, stroke=0, fill=0)
            c.linearGradient(0, PH - 4, PW, PH - 4, [theme, colors.HexColor('#f59e0b')], extend=True)
            c.restoreState()
            c.setFillColor(colors.Color(1, 1, 1, 0.05)); c.setFont('Times-Bold', 230)
            c.drawRightString(PW - 6, PH / 2 - 20, initials)
            c.restoreState()

        def band(cells, bg, style, cols):
            data, row = [], []
            for cell in cells:
                row.append(Paragraph(f'✓  {cell}', style))
                if len(row) == cols:
                    data.append(row); row = []
            if row:
                while len(row) < cols:
                    row.append('')
                data.append(row)
            t = Table(data, colWidths=[AW / cols] * cols)
            t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), bg),
                                   ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                                   ('LEFTPADDING', (0, 0), (-1, -1), 10), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
            return t

        def dark_table(data, col_widths):
            t = Table(data, colWidths=col_widths, repeatRows=1)
            t.setStyle(TableStyle([
                ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('BACKGROUND', (0, 0), (-1, 0), dark),
                ('LINEBELOW', (0, 1), (-1, -2), 0.4, colors.HexColor('#e6e6e6')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, colors.HexColor('#fafafa')]),
            ]))
            return t

        def sect_head(title):
            dot = Table([['']], colWidths=[0.42 * cm], rowHeights=[0.42 * cm])
            dot.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), theme)]))
            row = Table([[dot, Paragraph(title, secttitle)]], colWidths=[0.8 * cm, AW - 0.8 * cm])
            row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                     ('LEFTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 10),
                                     ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]))
            return row

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=margin, rightMargin=margin,
                                topMargin=margin, bottomMargin=margin)
        elems = []

        # ── Cover (dark; background drawn by cover_bg) ──
        elems.append(Spacer(1, 2.4 * cm))
        tagline_badge = f"{loc.upper()} · TRUSTED PLUMBING SPECIALISTS" if loc else 'TRUSTED PLUMBING SPECIALISTS'
        elems.append(band([tagline_badge], theme, badge, 1))
        elems.append(Spacer(1, 0.5 * cm))
        elems.append(Paragraph(business, hero))
        elems.append(Spacer(1, 0.3 * cm))
        elems.append(Paragraph(
            'Quality workmanship, honest pricing, and genuine care for your home — every job, every time.',
            hero_sub))
        cta = []
        if wa:
            cta.append(Paragraph(f'WhatsApp {wa}', S(fontName='Helvetica-Bold', fontSize=10, textColor=white, alignment=TA_CENTER)))
        if call:
            cta.append(Paragraph(f'Call {call}', S(fontName='Helvetica-Bold', fontSize=10, textColor=white, alignment=TA_CENTER)))
        if cta:
            elems.append(Spacer(1, 0.6 * cm))
            widths = [4.6 * cm] * len(cta)
            ct = Table([cta], colWidths=widths, hAlign='LEFT')
            stylec = [('TOPPADDING', (0, 0), (-1, -1), 9), ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
                      ('BACKGROUND', (0, 0), (0, 0), theme)]
            if len(cta) > 1:
                stylec += [('BOX', (1, 0), (1, 0), 1, colors.HexColor('#3a3a3a'))]
            ct.setStyle(TableStyle(stylec))
            elems.append(ct)
        elems.append(Spacer(1, 6.5 * cm))
        elems.append(band(['Licensed & experienced plumbers', 'Free on-site written quote',
                           'Satisfaction guaranteed', f'All prices in {cur}'], theme, feat, 2))
        elems.append(PageBreak())

        # ── Our Work ──
        elems.append(Paragraph('OUR WORK', eyebrow))
        elems.append(Spacer(1, 0.15 * cm))
        elems.append(Paragraph('Real jobs. Real results.', display))
        elems.append(Spacer(1, 0.2 * cm))
        elems.append(Paragraph(
            'Every project below was personally overseen by our senior plumber — from the first '
            'fitting to the final handover.', body))
        elems.append(Spacer(1, 0.4 * cm))
        cards = _portfolio_cards(tenant, limit=6)
        if cards:
            cell_w = (AW - 0.5 * cm) / 2
            img_h = 5.4 * cm
            for i in range(0, len(cards), 2):
                pair = cards[i:i + 2]
                row = []
                for c in pair:
                    row.append(PhotoCard(c['local'], c['title'], c['desc'], cell_w, img_h))
                while len(row) < 2:
                    row.append('')
                grid = Table([row], colWidths=[cell_w, cell_w], hAlign='LEFT')
                grid.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                          ('LEFTPADDING', (0, 0), (0, 0), 0), ('RIGHTPADDING', (0, 0), (0, 0), 6),
                                          ('LEFTPADDING', (1, 0), (1, 0), 6), ('RIGHTPADDING', (1, 0), (1, 0), 0),
                                          ('BOTTOMPADDING', (0, 0), (-1, -1), 12)]))
                elems.append(grid)
        else:
            msg = 'Portfolio photos available on request.'
            if wa:
                msg += f' Message us on WhatsApp ({wa}) and we will send examples of our completed work.'
            elems.append(Paragraph(msg, body))

        # ── Services & Pricing ──
        by_family = {}
        for item in TenantPriceItem.objects.filter(tenant=tenant, is_active=True):
            by_family.setdefault(item.family, []).append(item)
        pricing_elems, any_priced = [], False
        for title, families, kind in _PRICING_SECTIONS:
            rows = []
            for fam in families:
                for item in by_family.get(fam, []):
                    name = (item.label or fam).title()
                    if kind == 'fitting':
                        supply, install, allin = _fitting_cells(item, cur)
                        if supply == '—' and install == '—' and allin == '—':
                            continue
                        rows.append([Paragraph(name, td), Paragraph(supply, td_mid),
                                     Paragraph(install, td_mid),
                                     price_cell(allin, allin=allin != '—')])
                    else:
                        price = _price_str(item, cur)
                        if not price:
                            continue
                        is_allin = item.allin is not None or item.flat is not None
                        rows.append([Paragraph(name, td), price_cell(price, allin=is_allin)])
            if not rows:
                continue
            any_priced = True
            pricing_elems.append(sect_head(title))
            if kind == 'fitting':
                header = [Paragraph('ITEM', th), Paragraph('SUPPLY', thr),
                          Paragraph('INSTALL', thr), Paragraph('ALL-IN', thr)]
                widths = [AW * 0.30, AW * 0.20, AW * 0.28, AW * 0.22]
            else:
                header = [Paragraph('SERVICE', th), Paragraph('COST', thr)]
                widths = [AW * 0.66, AW * 0.34]
            pricing_elems.append(dark_table([header] + rows, widths))
            pricing_elems.append(Spacer(1, 0.3 * cm))

        if any_priced:
            elems.append(Spacer(1, 0.5 * cm))
            elems.append(Paragraph('SERVICES & PRICING', eyebrow))
            elems.append(Spacer(1, 0.15 * cm))
            elems.append(Paragraph('Clear pricing. No surprises.', display))
            elems.append(Spacer(1, 0.3 * cm))
            callout = Table([[Paragraph(
                f'<b>All prices are in {cur}.</b> Renovation prices are all-in — supply and labour '
                'included. The figures below are starting rates; your final cost depends on fixture '
                'choice, site conditions, and scope. We always provide a <b>free on-site written '
                'quote</b> before any work begins.', body)]], colWidths=[AW])
            callout.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f4f6f8')),
                                         ('LINEBEFORE', (0, 0), (0, -1), 4, theme),
                                         ('LEFTPADDING', (0, 0), (-1, -1), 14), ('RIGHTPADDING', (0, 0), (-1, -1), 14),
                                         ('TOPPADDING', (0, 0), (-1, -1), 12), ('BOTTOMPADDING', (0, 0), (-1, -1), 12)]))
            elems.append(callout)
            elems += pricing_elems
            elems.append(Paragraph(
                'Labour prices are for work only unless shown as all-in. Parts and fixtures are '
                'charged separately unless stated. All prices are starting rates — we always confirm '
                'the final price with you before starting any work.', note))

        # ── Dark contact footer ──
        elems.append(Spacer(1, 0.5 * cm))
        left = [Paragraph(business, foot_h), Spacer(1, 4),
                Paragraph('Trusted plumbing specialists' + (f' in {loc}' if loc else '')
                          + '. Ready to assess your job — free, no obligation.', foot_b)]
        right_rows = []
        if wa:
            right_rows.append([Paragraph('WHATSAPP', foot_k), Paragraph(wa, foot_v)])
        if call:
            right_rows.append([Paragraph('CALL', foot_k), Paragraph(call, foot_v)])
        right_rows.append([Paragraph('FREE ASSESSMENT', foot_k),
                           Paragraph('On-site written quote, no obligation', foot_v)])
        right_cells = []
        for k, v in right_rows:
            right_cells.append([k])
            right_cells.append([v])
            right_cells.append([Spacer(1, 5)])
        right_tbl = Table(right_cells, colWidths=[AW * 0.5 - 18])
        right_tbl.setStyle(TableStyle([('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
                                       ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
        footer = Table([[left, right_tbl]], colWidths=[AW * 0.5, AW * 0.5])
        footer.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), dark_bg),
                                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                    ('LEFTPADDING', (0, 0), (-1, -1), 18), ('RIGHTPADDING', (0, 0), (-1, -1), 18),
                                    ('TOPPADDING', (0, 0), (-1, -1), 20), ('BOTTOMPADDING', (0, 0), (-1, -1), 20)]))
        elems.append(footer)

        doc.build(elems, onFirstPage=cover_bg)
        return buffer.getvalue()
    except Exception:
        logger.exception('build_lead_magnet_pdf failed for %s',
                         getattr(tenant, 'slug', None))
        return None
    finally:
        for c in cards:
            if c.get('is_temp'):
                try:
                    os.unlink(c['local'])
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
