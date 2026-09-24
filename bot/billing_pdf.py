"""
bot/billing_pdf.py
==================
The platform's invoice and receipt as PDFs: what a tenant downloads or
receives by email when HomeX Media bills them (docs/current-state/billing.md).

THE DESIGN is "HomeX signature" (design C, chosen by the owner 2026-09-24 from
three drafts modelled on the Stripe layout Anthropic and OpenAI send and on
NVIDIA-style enterprise billing): a thin charcoal and electric-blue band, the
HX emblem on its own charcoal tile beside "HomeX Media / Unmatched Velocity",
the amount in a large tinted panel, From and Bill to columns, the lines with
the subscription's feature breakdown under them, a "How to pay" panel, and a
charcoal footer carrying the tagline and "Page x of y". Receipts get a PAID
stamp; a void document gets VOID. The palette is sampled from the logo:
charcoal #1A2225 (its background), electric blue #0AA0F0 (its wordmark), neon
cyan #5FE3FF (its glow).

WHY a separate renderer from `quote_pdf.py`: a quote is the TENANT's paper to
their customer, under the tenant's letterhead. These are the PLATFORM's paper
to the tenant, so the words come from the issuer snapshot frozen on the
invoice (`PlatformInvoice.issuer`), never a tenant profile and never
Homebase's identity. The emblem is the platform's own file
(`bot/static/billing/homex_emblem.png`), read from disk so a PDF never
depends on the static host; if it is missing the header simply has no tile.

HOW: reportlab (already a dependency; it brings Pillow, which reads the PNG)
draws onto an in-memory buffer and the builders return bytes, which is what
both the download view and the email attachment want. `_Doc` keeps the y
cursor and breaks pages (a long invoice continues under a slim header), and
`_NumberedCanvas` draws every page's footer at the end, once the page count
is known. Absent issuer fields are omitted, never replaced by placeholders.
Pinned by `PlatformBillingTests` and `BillingPdfTests`.
"""

import io
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas

W, H = A4
M = 48
FOOTER_H = 26
BOTTOM = FOOTER_H + 40   # lowest y content may reach before a page break

CHARCOAL = colors.HexColor('#1A2225')
BLUE = colors.HexColor('#0AA0F0')
CYAN = colors.HexColor('#5FE3FF')
TINT = colors.HexColor('#EAF6FE')
SOFT = colors.HexColor('#F5F9FB')
INK = colors.HexColor('#0F1A1F')
MUTED = colors.HexColor('#5E6E76')
RULE = colors.HexColor('#DCE6EB')
RED = colors.HexColor('#BA1A1A')
WHITE = colors.white

EMBLEM_PATH = Path(__file__).resolve().parent / 'static' / 'billing' / 'homex_emblem.png'


def _emblem():
    try:
        return ImageReader(str(EMBLEM_PATH)) if EMBLEM_PATH.exists() else None
    except Exception:  # noqa: BLE001 - a bad image never stops an invoice
        return None


def _money(currency, value):
    return f'{currency}{Decimal(str(value or 0)):,.2f}'


def _day(value, long=False):
    """'1 Oct 2026' without a platform-specific %-d (Windows has no %-d)."""
    if not value:
        return ''
    month = value.strftime('%B' if long else '%b')
    return f'{value.day} {month} {value.year}'


class _NumberedCanvas(canvas.Canvas):
    """Holds each page until save() so every footer can say "Page x of y".
    The footer text is set on the canvas by the builder (`footer_left`,
    `footer_right`)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages = []
        self.footer_left = ''
        self.footer_right = ''

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total):
        self.setFillColor(CHARCOAL)
        self.rect(0, 0, W, FOOTER_H, fill=1, stroke=0)
        self.setFont('Helvetica', 7.5)
        self.setFillColor(CYAN)
        self.drawString(M, 10, self.footer_left)
        self.setFillColor(WHITE)
        right = f'{self.footer_right}   Page {self._pageNumber} of {total}'.strip()
        self.drawRightString(W - M, 10, right)


class _Doc:
    """A y cursor over the canvas with the design's drawing primitives."""

    def __init__(self, buffer, issuer, doc_title, doc_number):
        self.c = _NumberedCanvas(buffer, pagesize=A4)
        self.issuer = issuer
        self.title = doc_title
        self.number = doc_number
        self.emblem = _emblem()
        name = issuer.get('business_name') or ''
        tagline = issuer.get('tagline') or ''
        self.c.footer_left = ' · '.join(p for p in (name, tagline) if p)
        email = issuer.get('email') or ''
        self.c.footer_right = f'Questions? {email}' if email else ''
        self.c.setTitle(f'{doc_title.title()} {doc_number}')
        self.c.setAuthor(name)
        self.y = H

    # -- primitives --------------------------------------------------------

    def text(self, x, y, value, size=9.5, bold=False, color=INK, right=False):
        self.c.setFont('Helvetica-Bold' if bold else 'Helvetica', size)
        self.c.setFillColor(color)
        (self.c.drawRightString if right else self.c.drawString)(x, y, str(value))

    def label(self, x, y, value, color=MUTED, right=False):
        self.text(x, y, str(value).upper(), 7, True, color, right)

    def panel(self, x, y, w, h, fill, radius=10):
        self.c.setFillColor(fill)
        self.c.roundRect(x, y, w, h, radius, fill=1, stroke=0)

    def tile(self, x, y, size, radius=10):
        """The HX emblem clipped to a rounded charcoal tile."""
        self.panel(x, y, size, size, CHARCOAL, radius)
        if self.emblem is None:
            return
        path = self.c.beginPath()
        path.roundRect(x, y, size, size, radius)
        self.c.saveState()
        self.c.clipPath(path, stroke=0, fill=0)
        self.c.drawImage(self.emblem, x, y, size, size)
        self.c.restoreState()

    def stamp(self, x, y, word, color):
        """A rotated outline stamp (PAID, VOID)."""
        self.c.saveState()
        self.c.translate(x, y)
        self.c.rotate(12)
        self.c.setStrokeColor(color)
        self.c.setLineWidth(2)
        width = 30 + len(word) * 13
        self.c.roundRect(-width / 2, -16, width, 32, 6, stroke=1, fill=0)
        self.c.setFont('Helvetica-Bold', 18)
        self.c.setFillColor(color)
        self.c.drawCentredString(0, -7, word)
        self.c.restoreState()

    # -- page furniture ----------------------------------------------------

    def _band(self):
        self.c.setFillColor(CHARCOAL)
        self.c.rect(0, H - 10, W, 10, fill=1, stroke=0)
        self.c.setFillColor(BLUE)
        self.c.rect(0, H - 12, W, 2, fill=1, stroke=0)

    def masthead(self):
        """First page: emblem tile, name and tagline, document title and number."""
        self._band()
        top = H - 44
        name = self.issuer.get('business_name') or ''
        tagline = self.issuer.get('tagline') or ''
        self.tile(M, top - 50, 50)
        self.text(M + 62, top - (20 if tagline else 30), name, 15, True)
        if tagline:
            self.text(M + 62, top - 36, tagline, 9, False, BLUE)
        self.text(W - M, top - 20, self.title, 18, True, right=True)
        self.text(W - M, top - 36, self.number, 9.5, False, MUTED, right=True)
        self.y = top - 78

    def continuation(self):
        """Later pages: the band, a small tile and the document number."""
        self._band()
        self.tile(M, H - 58, 28, 6)
        self.text(M + 38, H - 46, f'{self.title.title()} {self.number}', 10, True)
        self.text(W - M, H - 46, 'continued', 8.5, False, MUTED, right=True)
        self.y = H - 84

    def need(self, height):
        """Break the page when `height` more points will not fit."""
        if self.y - height < BOTTOM:
            self.c.showPage()
            self.continuation()
            return True
        return False

    # -- blocks ------------------------------------------------------------

    def amount_panel(self, label, amount, sub, facts, sub_color=MUTED, stamp=None):
        """The tinted panel: label, the big figure, one line under it, and up
        to three label/value facts on the right."""
        h = 78 if len(facts) <= 2 else 96
        top = self.y
        self.panel(M, top - h, W - 2 * M, h, TINT, 12)
        self.c.setFillColor(BLUE)
        self.c.rect(M, top - h + 12, 4, h - 24, fill=1, stroke=0)
        self.label(M + 22, top - 22, label, BLUE)
        self.text(M + 22, top - 52, amount, 28, True)
        self.text(M + 22, top - 68, sub, 9, False, sub_color)
        fy = top - 24
        for key, value in facts:
            self.label(W - M - 20, fy, key, MUTED, right=True)
            self.text(W - M - 20, fy - 12, value, 9.5, True, right=True)
            fy -= 26 if len(facts) > 2 else 30
        if stamp:
            self.stamp(W - M - 215, top - h / 2, *stamp)
        self.y = top - h - 30

    def parties(self, left_title, right_title, bill_to):
        """From (the issuer) and Bill to / Received from, side by side."""
        col2 = M + (W - 2 * M) / 2
        issuer = self.issuer
        left = [issuer.get('business_name') or '']
        left += [l for l in (issuer.get('address') or '').splitlines() if l.strip()]
        left += [v for v in (issuer.get('phone'), issuer.get('email')) if v]
        self.label(M, self.y, left_title)
        self.label(col2, self.y, right_title)
        y1 = y2 = self.y - 15
        for i, line in enumerate(left):
            self.text(M, y1, line, 9.5 if i == 0 else 9, i == 0, INK if i == 0 else MUTED)
            y1 -= 12.5
        for i, line in enumerate(bill_to):
            for part in simpleSplit(line, 'Helvetica', 9, col2 - M - 10) or ['']:
                self.text(col2, y2, part, 9.5 if i == 0 else 9, i == 0, INK if i == 0 else MUTED)
                y2 -= 12.5
        self.y = min(y1, y2) - 22

    def items(self, invoice, currency):
        """The lines, each with its feature breakdown (if any) underneath."""
        qty_x, unit_x, amt_x = W - M - 190, W - M - 95, W - M
        desc_w = qty_x - M - 40

        def head():
            self.label(M, self.y, 'Description')
            self.label(qty_x, self.y, 'Qty', right=True)
            self.label(unit_x, self.y, 'Unit price', right=True)
            self.label(amt_x, self.y, 'Amount', right=True)
            self.c.setStrokeColor(RULE)
            self.c.line(M, self.y - 8, W - M, self.y - 8)
            self.y -= 24

        self.need(60)
        head()
        for item in invoice.items.all():
            lines = simpleSplit(item.description, 'Helvetica', 10, desc_w) or ['']
            breakdown = item.breakdown or []
            if self.need(14 * len(lines) + 18 + 24 * len(breakdown)):
                head()
            self.text(qty_x, self.y, f'{item.quantity.normalize():f}', 10, right=True)
            self.text(unit_x, self.y, _money(currency, item.unit_price), 10, right=True)
            self.text(amt_x, self.y, _money(currency, item.line_total), 10, right=True)
            for line in lines:
                self.text(M, self.y, line, 10)
                self.y -= 14
            if breakdown:
                # The owner's cost allocation: each feature, its share and
                # its part of the line, indented under it in muted type. The
                # parts are allocated to the cent (models.allocate_breakdown),
                # so they add up exactly to the line above.
                self.y -= 2
                for part in breakdown:
                    self.text(M + 12, self.y, f"{part.get('feature', '')}", 8.5, False, INK)
                    self.text(qty_x, self.y, f"{part.get('percent', '')}%", 8.5, False, MUTED, right=True)
                    self.text(amt_x, self.y, _money(currency, part.get('amount')), 8.5, False, MUTED, right=True)
                    self.y -= 11
                    if part.get('detail'):
                        self.text(M + 12, self.y, part['detail'], 7.5, False, MUTED)
                        self.y -= 11
                    else:
                        self.y -= 2
            self.y -= 10
        self.c.setStrokeColor(RULE)
        self.c.line(M, self.y + 6, W - M, self.y + 6)
        self.y -= 12

    def totals(self, rows, strong=BLUE):
        """Right-aligned figures; the last row is the bold one."""
        self.need(18 * len(rows) + 10)
        x = W - M - 200
        for i, (key, value) in enumerate(rows):
            last = i == len(rows) - 1
            self.text(x, self.y, key, 10.5 if last else 9.5, last, INK if last else MUTED)
            self.text(W - M, self.y, value, 10.5 if last else 9.5, last,
                      strong if last else INK, right=True)
            self.y -= 17
        self.y -= 10

    def info_panel(self, title, lines):
        """A soft panel with a blue label: How to pay, Notes."""
        wrapped = []
        for line in lines:
            wrapped.extend(simpleSplit(line, 'Helvetica', 9, W - 2 * M - 32) or [''])
        h = 30 + 13 * len(wrapped)
        self.need(h + 12)
        top = self.y
        self.panel(M, top - h, W - 2 * M, h, SOFT, 10)
        self.label(M + 16, top - 16, title, BLUE)
        y = top - 31
        for line in wrapped:
            self.text(M + 16, y, line, 9)
            y -= 13
        self.y = top - h - 16

    def small_print(self, value):
        for line in simpleSplit(value, 'Helvetica', 8, W - 2 * M):
            self.need(12)
            self.text(M, self.y, line, 8, False, MUTED)
            self.y -= 11

    def finish(self):
        self.c.showPage()
        self.c.save()


def _bill_to_lines(invoice):
    lines = [invoice.bill_to_name]
    lines += [l for l in (invoice.bill_to_address or '').splitlines() if l.strip()]
    if invoice.bill_to_email:
        lines.append(invoice.bill_to_email)
    return lines


def build_invoice_pdf(invoice) -> bytes:
    """Render one PlatformInvoice in the HomeX signature design. Returns bytes.

    The amount panel says what the tenant needs to know NOW: the amount due;
    the balance, once part is paid; "Paid in full" with a PAID stamp once it
    is; "Overdue" in red after the due date; VOID on a void invoice so a stray
    copy cannot be paid. How to pay is left off once nothing is owed.
    """
    issuer = invoice.issuer or {}
    currency = invoice.currency
    buffer = io.BytesIO()
    doc = _Doc(buffer, issuer, 'INVOICE', invoice.number)
    doc.masthead()

    total, paid, balance = invoice.total, invoice.amount_paid, invoice.balance
    status = invoice.display_status
    facts = [('Issued', _day(invoice.issue_date)), ('Due', _day(invoice.due_date))]
    if invoice.period_start and invoice.period_end:
        facts.append(('Period', f'{_day(invoice.period_start)} to {_day(invoice.period_end)}'))
    stamp, sub_color = None, MUTED
    if status == 'void':
        label, figure, sub = 'Void', _money(currency, total), 'This invoice was cancelled and is not payable.'
        stamp, sub_color = ('VOID', RED), RED
    elif status == 'paid':
        label, figure, sub = 'Paid in full', _money(currency, total), 'Thank you, nothing is owed on this invoice.'
        stamp = ('PAID', BLUE)
    elif paid:
        label, figure = 'Balance due', _money(currency, balance)
        sub = f'{_money(currency, paid)} of {_money(currency, total)} paid. Due {_day(invoice.due_date, True)}'
    else:
        label, figure, sub = 'Amount due', _money(currency, total), f'Due {_day(invoice.due_date, True)}'
    if status == 'overdue':
        sub, sub_color = f'Overdue since {_day(invoice.due_date, True)}', RED
    doc.amount_panel(label, figure, sub, facts, sub_color, stamp)

    doc.parties('From', 'Bill to', _bill_to_lines(invoice))
    doc.items(invoice, currency)
    rows = [('Subtotal', _money(currency, total))]
    if paid:
        rows.append(('Paid', _money(currency, paid)))
        rows.append(('Balance due', _money(currency, balance)))
    else:
        rows.append(('Amount due', _money(currency, total)))
    doc.totals(rows)

    if status not in ('paid', 'void') and issuer.get('payment_details'):
        doc.info_panel('How to pay', issuer['payment_details'].splitlines()
                       + [f'Reference: {invoice.number}'])
    if invoice.notes:
        doc.info_panel('Notes', invoice.notes.splitlines())
    if issuer.get('footer_note'):
        doc.small_print(issuer['footer_note'])
    doc.finish()
    return buffer.getvalue()


def build_receipt_pdf(payment) -> bytes:
    """Render one PlatformPayment as its receipt. Returns bytes.

    The panel carries THIS payment (its amount never changes); the totals
    show where the invoice stands now, so a receipt downloaded after a later
    payment shows the later balance. A voided receipt says VOID.
    """
    invoice = payment.invoice
    issuer = invoice.issuer or {}
    currency = invoice.currency
    buffer = io.BytesIO()
    doc = _Doc(buffer, issuer, 'RECEIPT', payment.receipt_number)
    doc.masthead()

    method = payment.get_method_display()
    facts = [('Invoice', invoice.number)]
    if payment.reference:
        facts.append(('Reference', payment.reference))
    if payment.is_void:
        doc.amount_panel('Void', _money(currency, payment.amount),
                         'This receipt was cancelled.', facts, RED, ('VOID', RED))
    else:
        doc.amount_panel('Amount paid', _money(currency, payment.amount),
                         f'Paid {_day(payment.paid_on, True)} by {method}', facts,
                         stamp=('PAID', BLUE))

    doc.parties('From', 'Received from', _bill_to_lines(invoice))
    doc.items(invoice, currency)
    doc.totals([('Invoice total', _money(currency, invoice.total)),
                ('Paid to date', _money(currency, invoice.amount_paid)),
                ('Balance remaining', _money(currency, invoice.balance))])
    if payment.note:
        doc.info_panel('Note', payment.note.splitlines())
    if not payment.is_void:
        doc.need(24)
        doc.text(M, doc.y, 'Thank you for your payment.', 11, True, BLUE)
        doc.y -= 24
    if issuer.get('footer_note'):
        doc.small_print(issuer['footer_note'])
    doc.finish()
    return buffer.getvalue()
