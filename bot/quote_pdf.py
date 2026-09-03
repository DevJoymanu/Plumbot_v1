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
#
# Colours and proportions come straight from bot/includes/quote_sectioned_css.html
# so the PDF is the same document as the screen, not a plain-text version of it.
# The old renderer centred everything and led the table with Item; the sheet is
# a two-column letterhead with a green-ruled table led by QTY.

SEC = {
    'line': '#2fae66',
    'green': '#1f9d55',
    'green_dark': '#178a48',
    'teal': '#16918f',
    'blue': '#1b75bc',
    'red': '#d1352b',
    'ink': '#222222',
    'section_bg': '#f3faf5',
}

# Column proportions mirror col.bq-c-* in the stylesheet: qty 14%, description
# the remainder, unit 17%, total 17%.
SEC_COLS = (0.14, 0.52, 0.17, 0.17)


def _hex(name):
    from reportlab.lib import colors
    return colors.HexColor(SEC[name])


def _sectioned_letterhead(page):
    """Two columns: the mark and the business name left, contacts right.

    Mirrors .bq-head / .bq-logo / .bq-contact. Absent means omit - a tenant
    with no website simply has no website line, never another tenant's.
    """
    from . import branding

    top = page.y
    text_x = page.left

    raw, _ = branding.logo_bytes(page.tenant)
    if raw:
        try:
            import io as _io
            from reportlab.lib.utils import ImageReader
            image = ImageReader(_io.BytesIO(raw))
            iw, ih = image.getSize()
            draw_h = 40.0
            draw_w = min(120.0, iw * (draw_h / ih)) if ih else 90.0
            page.c.drawImage(image, page.left, top - draw_h + 6,
                             width=draw_w, height=draw_h,
                             preserveAspectRatio=True, mask='auto')
            text_x = page.left + draw_w + 10
        except Exception:
            logger.info('Quote PDF: logo not drawable for tenant %s', page.tenant)

    name = page.lh.get('business_name') or branding.brand_name(page.tenant)
    if name:
        page.c.setFont('Helvetica-Bold', 17)
        page.c.setFillColor(_hex('blue'))
        page.c.drawString(text_x, top - 14, name.upper())
        page.c.setFont('Helvetica-Bold', 7)
        page.c.setFillColor(_hex('green_dark'))
        page.c.drawString(text_x, top - 25, 'DOMESTIC | INDUSTRIAL | COMMERCIAL')

    # The contact column, right-aligned, wrapped inside its own width.
    contact = []
    if page.lh.get('address'):
        contact.append(page.lh['address'])
    phones = page.lh.get('phones') or []
    for i in range(0, len(phones), 2):
        contact.append(' / '.join(str(p) for p in phones[i:i + 2]))
    if page.lh.get('public_email'):
        contact.append(page.lh['public_email'])
    if page.lh.get('website'):
        contact.append(page.lh['website'])

    page.c.setFont('Helvetica', 8.5)
    page.c.setFillColor(_hex('teal'))
    line_y = top - 6
    # .bq-contact is max-width:270px on screen; the same share of the sheet here.
    contact_width = (page.right - page.left) * 0.42
    for entry in contact:
        for chunk in _wrap_px(page.c, entry, contact_width, 'Helvetica', 8.5):
            page.c.drawRightString(page.right, line_y, chunk)
            line_y -= 11

    page.y = min(top - 40, line_y) - 8

    # .bq-ta - the trading name, over a green rule.
    if page.lh.get('trading_name'):
        page.c.setFont('Helvetica-Bold', 14)
        page.c.setFillColor(_hex('ink'))
        page.c.drawString(page.left, page.y, f"t / a {page.lh['trading_name']}")
        page.y -= 8
    page.c.setStrokeColor(_hex('green'))
    page.c.setLineWidth(1.6)
    page.c.line(page.left, page.y, page.right, page.y)
    page.c.setLineWidth(1)
    page.y -= 14

    # .bq-blurb - italic, centred, and WRAPPED. It ran off the page before.
    blurb = ' '.join(x for x in (page.lh.get('services_blurb'),
                                 page.lh.get('maintenance_blurb')) if x)
    if blurb:
        page.c.setFont('Helvetica-Oblique', 8.5)
        page.c.setFillColor(_hex('green_dark'))
        for chunk in _wrap_px(page.c, blurb, page.right - page.left,
                              'Helvetica-Oblique', 8.5):
            page.c.drawCentredString(page.width / 2, page.y, chunk)
            page.y -= 11
        page.y -= 4


def _sectioned_sheet(page, document):
    """Mirrors bot/pages/quote_sectioned_view.html and quote_footer.html."""
    quotation = page.quotation
    appointment = getattr(quotation, 'appointment', None)

    _sectioned_letterhead(page)

    # .bq-qtitle - centred, red, underlined.
    page.c.setFont('Helvetica-Bold', 12)
    page.c.setFillColor(_hex('red'))
    title = 'Quotation'
    page.c.drawCentredString(page.width / 2, page.y, title)
    half = page.c.stringWidth(title, 'Helvetica-Bold', 12) / 2
    page.c.setStrokeColor(_hex('red'))
    page.c.line(page.width / 2 - half, page.y - 2, page.width / 2 + half, page.y - 2)
    page.y -= 22

    # .bq-meta - a two-column grid of label/value.
    meta = [(label, value) for label, value in (
        ('DATE:', quotation.created_at.strftime('%d %b %Y') if quotation.created_at else ''),
        ('CLIENT:', _client_name(appointment)),
        ('CONTACT:', _phone(appointment)),
        ('ADDRESS:', getattr(appointment, 'customer_area', '') or ''),
        ('EMAIL:', getattr(appointment, 'customer_email', '') or ''),
    ) if value]
    column = (page.right - page.left) / 2
    for index, (label, value) in enumerate(meta):
        col_x = page.left + (column if index % 2 else 0)
        row_y = page.y - (index // 2) * 15
        page.c.setFont('Helvetica-Bold', 8.5)
        page.c.setFillColor(_hex('teal'))
        page.c.drawString(col_x, row_y, label)
        page.c.setFont('Helvetica', 10)
        page.c.setFillColor(_hex('ink'))
        page.c.drawString(col_x + 62, row_y, str(value))
    page.y -= ((len(meta) + 1) // 2) * 15 + 12

    # ONE table, with every section inside it - the sheet has a single <table>
    # whose section headings are rows, so the column header belongs at the top
    # and nowhere else. It was being redrawn before each section.
    widths = [(page.right - page.left) * f for f in SEC_COLS]
    sections = document.get('sections') or []
    page.space(90)
    _sec_table_head(page, widths)

    if not sections:
        _sec_empty_row(page, widths)
    for index, group in enumerate(sections, start=1):
        # A section heading alone at the foot of a page is an orphan; take the
        # heading and its first row over together.
        if page.space(56):
            _sec_table_head(page, widths)
        _sec_section_row(page, widths, index, group['title'] or 'ITEMS')
        for item in group['items']:
            if page.space(34):
                _sec_table_head(page, widths)
            _sec_item_row(page, widths, item)
        _sec_subtotal_row(page, widths, page.money(group['subtotal']))
    page.y -= 12

    rows = [('MATERIALS SUB-TOTAL', document.get('materials_total') or 0, False),
            ('ADD: Labour', quotation.labor_cost, False),
            ('Transport', quotation.transport_cost, False)]
    if _dec(quotation.discount):
        rows.append(('Discount', -_dec(quotation.discount), False))
    rows.append(('SUB-TOTAL', document.get('net_subtotal') or 0, True))
    if _dec(quotation.vat_percent):
        rows.append((f'VAT ({_dec(quotation.vat_percent):g}%)',
                     document.get('vat_amount') or 0, False))
    _sec_totals(page, widths, rows, quotation.total_amount)

    terms = document.get('quote_terms') or []
    if terms:
        page.space(30 + len(terms) * 18)
        page.y -= 6
        for term in terms:
            _sec_term_row(page, term)
        page.y -= 8

    _sectioned_foot(page)


def _sec_table_head(page, widths):
    """QTY | DESCRIPTION | UNIT PRICE | TOTAL PRICE - the sheet's own order."""
    labels = ('QTY', 'DESCRIPTION', 'UNIT PRICE', 'TOTAL PRICE')
    page.c.setFont('Helvetica-Bold', 9)
    _sec_row(page, widths, labels, height=18, fill=None,
             colour=_hex('teal'), align=('left', 'left', 'left', 'left'))


def _sec_section_row(page, widths, number, title):
    page.c.setFont('Helvetica-Bold', 9.5)
    _sec_row(page, widths, (f'{number}.', title.upper(), '', ''),
             height=18, fill=_hex('section_bg'), colour=_hex('green_dark'),
             align=('left', 'left', 'left', 'left'))


def _sec_item_row(page, widths, item):
    page.c.setFont('Helvetica', 9)
    _sec_row(page, widths,
             (str(item['qty_text']), str(item['description'])[:52],
              page.money(item['unit']), page.money(item['total_price'])),
             height=18, fill=None, colour=_hex('ink'),
             align=('left', 'left', 'left', 'left'))


def _sec_empty_row(page, widths):
    page.c.setFont('Helvetica', 9)
    _sec_row(page, widths, ('', 'No items on this quotation yet.', '', ''),
             height=18, fill=None, colour=_hex('ink'),
             align=('left', 'left', 'left', 'left'))


def _sec_subtotal_row(page, widths, amount):
    """The per-section SUB-TOTAL: teal, label right-aligned over the first
    three columns, exactly as tr.bq-subtotal renders it."""
    page.c.setFont('Helvetica-Bold', 9.5)
    merged = (sum(widths[:3]), widths[3])
    _sec_row(page, merged, ('SUB-TOTAL', amount), height=18, fill=None,
             colour=_hex('teal'), align=('right', 'left'))


def _sec_row(page, widths, values, *, height, fill, colour, align):
    """One bordered row, green-ruled like .bq-table th/td."""
    x = page.left
    top = page.y + 4
    bottom = top - height
    for width, value, how in zip(widths, values, align):
        if fill is not None:
            page.c.setFillColor(fill)
            page.c.rect(x, bottom, width, height, stroke=0, fill=1)
        page.c.setStrokeColor(_hex('line'))
        page.c.rect(x, bottom, width, height, stroke=1, fill=0)
        page.c.setFillColor(colour)
        if how == 'right':
            page.c.drawRightString(x + width - 6, bottom + 5, str(value))
        else:
            page.c.drawString(x + 6, bottom + 5, str(value))
        x += width
    page.y = bottom - 4


def _sec_totals(page, widths, rows, grand):
    """Labels teal and right-aligned, amounts right - as .bq-totals draws."""
    page.space(40 + len(rows) * 16)
    label_right = page.left + sum(widths[:3]) - 6
    for label, value, underline in rows:
        page.c.setFont('Helvetica-Bold', 9.5)
        page.c.setFillColor(_hex('teal'))
        page.c.drawRightString(label_right, page.y, label)
        if underline:
            width = page.c.stringWidth(label, 'Helvetica-Bold', 9.5)
            page.c.setStrokeColor(_hex('teal'))
            page.c.line(label_right - width, page.y - 2, label_right, page.y - 2)
        page.c.setFont('Helvetica', 9.5)
        page.c.setFillColor(_hex('ink'))
        page.c.drawRightString(page.right - 6, page.y, page.money(value))
        page.y -= 16

    page.y -= 2
    page.c.setStrokeColor(_hex('line'))
    page.c.line(page.left + sum(widths[:2]), page.y + 6, page.right, page.y + 6)
    page.y -= 8
    page.c.setFont('Helvetica-Bold', 12)
    page.c.setFillColor(_hex('ink'))
    page.c.drawRightString(label_right, page.y, 'GRAND TOTAL')
    page.c.drawRightString(page.right - 6, page.y, page.money(grand))
    page.y -= 22


def _sec_term_row(page, term):
    """A payment term: red and bold, in its own ruled box (.bq-terms)."""
    width = page.right - page.left
    top = page.y + 4
    page.c.setStrokeColor(_hex('line'))
    page.c.rect(page.left, top - 18, width, 18, stroke=1, fill=0)
    page.c.setFont('Helvetica-Bold', 9)
    page.c.setFillColor(_hex('red'))
    page.c.drawString(page.left + 6, top - 13, str(term))
    page.y = top - 22


def _sectioned_foot(page):
    """Banking, signature and tagline - each block only if the tenant has it."""
    bank = page.lh.get('bank') or {}
    bank_lines = [(label, bank.get(key)) for label, key in (
        ('Account Name', 'account_name'), ('Bank Name', 'bank_name'),
        ('Branch', 'branch'), ('Account Number', 'account_number'))
        if bank.get(key)]

    if bank_lines:
        page.space(50 + len(bank_lines) * 14)
        page.y -= 12
        page.c.setFont('Helvetica-Bold', 11)
        page.c.setFillColor(_hex('green_dark'))
        page.c.drawString(page.left, page.y, 'Banking Details')
        width = page.c.stringWidth('Banking Details', 'Helvetica-Bold', 11)
        page.c.setStrokeColor(_hex('green_dark'))
        page.c.line(page.left, page.y - 2, page.left + width, page.y - 2)
        page.y -= 16
        for label, value in bank_lines:
            page.c.setFont('Helvetica', 9.5)
            page.c.setFillColor(_hex('teal'))
            page.c.drawString(page.left, page.y, f'{label}: - ')
            offset = page.c.stringWidth(f'{label}: - ', 'Helvetica', 9.5)
            page.c.setFont('Helvetica-Bold', 9.5)
            page.c.setFillColor(_hex('ink'))
            page.c.drawString(page.left + offset, page.y, str(value))
            page.y -= 14

    page.space(50)
    page.y -= 14
    page.c.setFont('Helvetica-Bold', 9.5)
    page.c.setFillColor(_hex('teal'))
    page.c.drawString(page.left, page.y, 'Client signature: ...........................')
    if page.lh.get('signatory'):
        page.c.drawRightString(page.right, page.y, f"Authorized by: {page.lh['signatory']}")
    page.y -= 24

    if page.lh.get('tagline'):
        page.c.setFont('Helvetica-Oblique', 9)
        page.c.setFillColor(_hex('teal'))
        page.c.drawCentredString(page.width / 2, page.y, page.lh['tagline'])
        page.y -= 13
    if page.lh.get('website'):
        page.c.setFont('Helvetica', 9)
        page.c.setFillColor(_hex('blue'))
        page.c.drawCentredString(page.width / 2, page.y, page.lh['website'])
        page.y -= 13


def _wrap_px(canvas_obj, value, max_width, font, size):
    """Wrap to a MEASURED width, not a character count.

    Counting characters is guesswork - it put Barmak's services blurb past the
    right margin, where the customer's copy simply cut it off mid-word. The
    canvas knows exactly how wide a string is; ask it.
    """
    words = str(value or '').split()
    if not words:
        return ['']
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = f'{current} {word}'
        if canvas_obj.stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


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
