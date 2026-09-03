"""
bot/quote_pdf.py
================
The quote as a PDF — the copy the customer actually receives.

**It follows the tenant's own layout, exactly as the screens do.** A tenant on
the sectioned sheet gets a sectioned PDF; everyone else gets the flat one. The
selector is the same `is_sectioned(tenant)` the four screens use, so the paper
in the customer's inbox and the paper on the plumber's screen cannot be
different documents.

Before this there was one renderer for everybody. A sectioned tenant (Barmak)
designed a sheet with numbered sections, per-section subtotals, discount, VAT,
terms, banking details and a signature line — and their customers received a
flat list with none of it. The PDF was also hardcoded to "US$" and printed the
`materials_cost` column rather than the item lines it is built from.

The blocks and their order mirror the templates deliberately:

    flat       -> bot/includes/quote_flat_document.html
    sectioned  -> bot/pages/quote_sectioned_view.html + quote_footer.html

If you change one, change the other. There is no way to share the markup — one
is HTML and the other is a reportlab canvas — so the tests assert the FIGURES
agree, which is the part a customer would notice.
"""

import logging
import tempfile
from decimal import Decimal

logger = logging.getLogger(__name__)

# Page geometry, shared by both sheets so they feel like the same stationery.
MARGIN = 40
LINE = 14


def build_quotation_pdf(quotation):
    """Render `quotation` to a temp PDF and return its path.

    The caller owns the file and is responsible for removing it.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from .views.quote_layout import (document_context, is_sectioned,
                                     letterhead_for, tenant_of)

    tenant = tenant_of(None, quotation=quotation)
    letterhead = letterhead_for(tenant) or {}

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        path = tmp.name

    sheet = canvas.Canvas(path, pagesize=A4)
    page = _Sheet(sheet, A4, tenant, letterhead, quotation)

    if is_sectioned(tenant):
        _sectioned_sheet(page, document_context(quotation, letterhead))
    else:
        _flat_sheet(page)

    sheet.save()
    return path


class _Sheet:
    """A cursor over the canvas, so neither renderer tracks y by hand.

    Every draw call goes through here, which is what keeps the two sheets on the
    same margins, the same fonts and the same page-break behaviour.
    """

    def __init__(self, canvas_obj, pagesize, tenant, letterhead, quotation):
        self.c = canvas_obj
        self.width, self.height = pagesize
        self.left = MARGIN
        self.right = self.width - MARGIN
        self.y = self.height - 45
        self.tenant = tenant
        self.lh = letterhead
        self.quotation = quotation
        self.currency = (letterhead.get('currency')
                         or _tenant_currency(tenant) or 'US$')

    # -- page mechanics ----------------------------------------------------

    def space(self, needed):
        """Start a new page when `needed` points would run off this one."""
        if self.y - needed < 60:
            self.c.showPage()
            self.y = self.height - 45
            return True
        return False

    def gap(self, points=10):
        self.y -= points

    def text(self, value, *, size=10, bold=False, italic=False, colour=None,
             x=None, indent=0):
        from reportlab.lib import colors
        font = 'Helvetica'
        if bold:
            font = 'Helvetica-Bold'
        elif italic:
            font = 'Helvetica-Oblique'
        self.c.setFont(font, size)
        self.c.setFillColor(colour or colors.black)
        self.c.drawString((x if x is not None else self.left) + indent, self.y, str(value))
        self.y -= size + 4

    def right_text(self, value, *, size=10, bold=False, colour=None, x=None):
        from reportlab.lib import colors
        self.c.setFont('Helvetica-Bold' if bold else 'Helvetica', size)
        self.c.setFillColor(colour or colors.black)
        self.c.drawRightString(x if x is not None else self.right, self.y, str(value))

    def centered(self, value, *, size=10, bold=False, italic=False, colour=None):
        from reportlab.lib import colors
        font = 'Helvetica'
        if bold:
            font = 'Helvetica-Bold'
        elif italic:
            font = 'Helvetica-Oblique'
        self.c.setFont(font, size)
        self.c.setFillColor(colour or colors.black)
        self.c.drawCentredString(self.width / 2, self.y, str(value))
        self.y -= size + 4

    def rule(self, *, colour=None):
        from reportlab.lib import colors
        self.c.setStrokeColor(colour or colors.HexColor('#dddddd'))
        self.c.line(self.left, self.y, self.right, self.y)
        self.y -= 12

    def money(self, value):
        return f'{self.currency}{_dec(value):.2f}'

    def wrapped(self, value, *, width=95, size=10, colour=None, limit=None):
        """Long prose over several lines rather than one truncated one."""
        import textwrap
        lines = []
        for paragraph in str(value or '').splitlines():
            lines.extend(textwrap.wrap(paragraph, width) or [''])
        if limit:
            lines = lines[:limit]
        for line in lines:
            self.space(20)
            self.text(line, size=size, colour=colour)


# ── The letterhead, shared by both sheets ────────────────────────────────────

def _letterhead(page):
    """The tenant's own mark and details, centred, as both screens draw it.

    Absent means omit — never a placeholder, never another tenant's. The old
    builder printed "HOMEBASE CONSTRUCTION" and a Johannesburg address on every
    tenant's quote.
    """
    from reportlab.lib import colors
    from . import branding

    raw, _ = branding.logo_bytes(page.tenant)
    if raw:
        try:
            import io as _io
            from reportlab.lib.utils import ImageReader
            image = ImageReader(_io.BytesIO(raw))
            width, height = image.getSize()
            draw_h = 52.0
            draw_w = min(170.0, width * (draw_h / height)) if height else 120.0
            page.c.drawImage(image, (page.width - draw_w) / 2, page.y - draw_h,
                             width=draw_w, height=draw_h,
                             preserveAspectRatio=True, mask='auto')
            page.y -= draw_h + 10
        except Exception:
            # An SVG, or a file reportlab cannot draw. The name still prints.
            logger.info('Quote PDF: logo not drawable for tenant %s', page.tenant)

    name = page.lh.get('business_name') or branding.brand_name(page.tenant)
    if name:
        page.centered(name, size=15, bold=True)
    if page.lh.get('trading_name'):
        page.centered(f"t / a {page.lh['trading_name']}", size=9,
                      colour=colors.grey)
    # services_blurb, not 'strapline': that key does not exist in
    # TenantConfig.letterhead() and never rendered anywhere.
    if page.lh.get('services_blurb'):
        page.centered(page.lh['services_blurb'], size=9, italic=True, colour=colors.grey)
    if page.lh.get('address'):
        page.centered(page.lh['address'], size=9, colour=colors.grey)
    phones = page.lh.get('phones') or []
    if phones:
        page.centered(' · '.join(str(p) for p in phones), size=9, colour=colors.grey)
    if page.lh.get('public_email'):
        page.centered(page.lh['public_email'], size=9, colour=colors.grey)

    page.gap(6)
    page.rule()


# ── The flat sheet ───────────────────────────────────────────────────────────

def _flat_sheet(page):
    """Mirrors bot/includes/quote_flat_document.html, block for block."""
    from reportlab.lib import colors

    quotation = page.quotation
    appointment = getattr(quotation, 'appointment', None)
    _letterhead(page)

    page.text('CLIENT INFORMATION', size=9, bold=True, colour=colors.grey)
    page.text(_client_name(appointment), size=11, bold=True)
    for line in _client_lines(appointment):
        page.text(line, size=10, colour=colors.HexColor('#3e4850'))
    page.gap(6)

    page.text('PROJECT DETAILS', size=9, bold=True, colour=colors.grey)
    page.text(_service_label(appointment) or 'Service', size=11, bold=True)
    if getattr(appointment, 'customer_area', ''):
        page.text(appointment.customer_area, size=10, colour=colors.HexColor('#3e4850'))
    if getattr(appointment, 'project_description', ''):
        page.wrapped(appointment.project_description, size=10,
                     colour=colors.HexColor('#3e4850'), limit=4)
    page.gap(10)

    _items_table(page, [
        (item.description, item.quantity, item.unit_price, item.total_price)
        for item in quotation.items.all()
    ])

    # The item lines are the materials. The stored materials_cost column is
    # double-counted into total_amount by Quotation.save() (a known, deliberate
    # pre-existing quirk), so the sheet shows what the lines actually add to.
    items_total = sum((_dec(i.total_price) for i in quotation.items.all()), Decimal('0'))
    page.gap(12)
    _totals_block(page, [
        ('Material cost', items_total),
        ('Labour', quotation.labor_cost),
        ('Transport', quotation.transport_cost),
    ], grand=('Total Amount', quotation.total_amount))

    if quotation.notes:
        page.space(70)
        page.gap(10)
        page.text('NOTES', size=9, bold=True, colour=colors.grey)
        page.wrapped(quotation.notes, size=10, colour=colors.HexColor('#3e4850'))

    _closing_line(page)


def _closing_line(page):
    """The tenant's own contact, or nothing. Never another business's."""
    from reportlab.lib import colors

    appointment = getattr(page.quotation, 'appointment', None)
    page.space(60)
    page.gap(16)
    page.rule()
    page.centered(f'Prepared for: {_client_name(appointment)}', size=9,
                  colour=colors.grey)
    if page.lh.get('public_email'):
        page.centered(
            f"Any questions about this quotation, contact us at {page.lh['public_email']}",
            size=9, colour=colors.grey)
    elif page.lh.get('phones'):
        page.centered(
            f"Any questions about this quotation, call us on {page.lh['phones'][0]}",
            size=9, colour=colors.grey)


# ── The sectioned sheet ──────────────────────────────────────────────────────

def _sectioned_sheet(page, document):
    """Mirrors bot/pages/quote_sectioned_view.html and quote_footer.html.

    Numbered sections with their own SUB-TOTAL, then labour, transport,
    discount, VAT and GRAND TOTAL, then terms, banking and the signature line.
    Every block is guarded: a tenant with no bank details gets no banking block.
    """
    from reportlab.lib import colors

    quotation = page.quotation
    appointment = getattr(quotation, 'appointment', None)
    _letterhead(page)
    page.centered('QUOTATION', size=13, bold=True)
    page.gap(8)

    for label, value in (
        ('DATE:', quotation.created_at.strftime('%d %b %Y') if quotation.created_at else ''),
        ('CLIENT:', _client_name(appointment)),
        ('CONTACT:', _phone(appointment)),
        ('ADDRESS:', getattr(appointment, 'customer_area', '') or ''),
        ('EMAIL:', getattr(appointment, 'customer_email', '') or ''),
    ):
        if not value:
            continue
        page.c.setFont('Helvetica-Bold', 9)
        page.c.setFillColor(colors.grey)
        page.c.drawString(page.left, page.y, label)
        page.c.setFont('Helvetica', 10)
        page.c.setFillColor(colors.black)
        page.c.drawString(page.left + 70, page.y, str(value))
        page.y -= LINE
    page.gap(8)

    for index, group in enumerate(document.get('sections') or [], start=1):
        page.space(80)
        page.text(f"{index}. {group['title'] or 'ITEMS'}", size=10, bold=True)
        _items_table(page, [
            (item['description'], item['qty_text'], item['unit'], item['total_price'])
            for item in group['items']
        ], qty_is_text=True)
        page.gap(4)
        page.right_text(f"SUB-TOTAL   {page.money(group['subtotal'])}", size=10, bold=True)
        page.y -= 18
        page.gap(6)

    rows = [('MATERIALS SUB-TOTAL', document.get('materials_total') or 0),
            ('Labour', quotation.labor_cost),
            ('Transport', quotation.transport_cost)]
    if _dec(quotation.discount):
        rows.append(('Discount', -_dec(quotation.discount)))
    rows.append(('SUB-TOTAL', document.get('net_subtotal') or 0))
    if _dec(quotation.vat_percent):
        rows.append((f'VAT ({_dec(quotation.vat_percent):g}%)',
                     document.get('vat_amount') or 0))
    page.gap(8)
    _totals_block(page, rows, grand=('GRAND TOTAL', quotation.total_amount))

    terms = document.get('quote_terms') or []
    if terms:
        page.space(40 + len(terms) * LINE)
        page.gap(12)
        for term in terms:
            page.text(term, size=9, colour=colors.HexColor('#3e4850'))

    bank = page.lh.get('bank') or {}
    bank_lines = [(label, bank.get(key)) for label, key in (
        ('Account Name', 'account_name'), ('Bank Name', 'bank_name'),
        ('Branch', 'branch'), ('Account Number', 'account_number'))
        if bank.get(key)]
    if bank_lines:
        page.space(40 + len(bank_lines) * LINE)
        page.gap(14)
        page.text('Banking Details', size=10, bold=True)
        for label, value in bank_lines:
            page.text(f'{label}: {value}', size=9, colour=colors.HexColor('#3e4850'))

    page.space(50)
    page.gap(20)
    page.c.setFont('Helvetica', 9)
    page.c.setFillColor(colors.black)
    page.c.drawString(page.left, page.y, 'Client signature: ...........................')
    if page.lh.get('signatory'):
        page.c.drawRightString(page.right, page.y, f"Authorized by: {page.lh['signatory']}")
    page.y -= 22

    tagline = page.lh.get('tagline')
    if tagline:
        page.centered(tagline, size=9, italic=True, colour=colors.grey)
    if page.lh.get('website'):
        page.centered(page.lh['website'], size=9, colour=colors.grey)


# ── Shared pieces ────────────────────────────────────────────────────────────

def _items_table(page, rows, *, qty_is_text=False):
    """Item / Qty / Price / Total, with the header repeated on a page break."""
    from reportlab.lib import colors

    col_qty = page.left + 300
    col_price = page.left + 370
    col_total = page.right

    def header():
        page.c.setFillColor(colors.HexColor('#eff4ff'))
        page.c.rect(page.left, page.y - 5, page.right - page.left, 18, stroke=0, fill=1)
        page.c.setFillColor(colors.HexColor('#3e4850'))
        page.c.setFont('Helvetica-Bold', 9)
        page.c.drawString(page.left + 4, page.y, 'Item')
        page.c.drawRightString(col_qty, page.y, 'Qty')
        page.c.drawRightString(col_price, page.y, 'Price')
        page.c.drawRightString(col_total - 4, page.y, 'Total')
        page.y -= 20

    page.space(60)
    header()

    if not rows:
        page.text('No items on this quote', size=9, colour=colors.grey, indent=4)
        return

    for description, qty, unit, total in rows:
        if page.space(30):
            header()
        page.c.setStrokeColor(colors.HexColor('#e8eef7'))
        page.c.line(page.left, page.y + 12, page.right, page.y + 12)
        page.c.setFont('Helvetica', 9)
        page.c.setFillColor(colors.black)
        page.c.drawString(page.left + 4, page.y, str(description)[:56])
        page.c.drawRightString(col_qty, page.y, str(qty) if qty_is_text else f'{_dec(qty):g}')
        page.c.drawRightString(col_price, page.y, page.money(unit))
        page.c.drawRightString(col_total - 4, page.y, page.money(total))
        page.y -= 18


def _totals_block(page, rows, *, grand):
    """The totals, right-aligned, with the grand total ruled off."""
    from reportlab.lib import colors

    page.space(30 + len(rows) * 16)
    for label, value in rows:
        page.c.setFont('Helvetica', 10)
        page.c.setFillColor(colors.HexColor('#3e4850'))
        page.c.drawRightString(page.right - 110, page.y, label)
        page.c.setFillColor(colors.black)
        page.c.drawRightString(page.right, page.y, page.money(value))
        page.y -= 16

    page.y -= 4
    page.c.setStrokeColor(colors.HexColor('#cbd7e6'))
    page.c.line(page.right - 240, page.y + 6, page.right, page.y + 6)
    page.y -= 6
    page.c.setFont('Helvetica-Bold', 11)
    page.c.setFillColor(colors.black)
    page.c.drawRightString(page.right - 110, page.y, grand[0])
    page.c.drawRightString(page.right, page.y, page.money(grand[1]))
    page.y -= 18


# ── Small resolvers ──────────────────────────────────────────────────────────

def _dec(value):
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal('0')


def _tenant_currency(tenant):
    if tenant is None:
        return ''
    try:
        from .tenant_config import get_config
        return get_config(tenant).currency
    except Exception:
        return ''


def _client_name(appointment):
    if appointment is None:
        return 'Client'
    name = (getattr(appointment, 'customer_name', '') or '').strip()
    return name or _phone(appointment) or 'Client'


def _phone(appointment):
    raw = (getattr(appointment, 'phone_number', '') or '')
    return raw.replace('whatsapp:', '').strip()


def _service_label(appointment):
    if appointment is None or not getattr(appointment, 'project_type', ''):
        return ''
    try:
        return appointment.get_project_type_display()
    except Exception:
        return (appointment.project_type or '').replace('_', ' ').title()


def _client_lines(appointment):
    if appointment is None:
        return []
    lines = []
    for value in (getattr(appointment, 'customer_area', ''),
                  _phone(appointment),
                  getattr(appointment, 'customer_email', '')):
        if value:
            lines.append(value)
    return lines
