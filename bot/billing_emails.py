"""
bot/billing_emails.py
=====================
Emails the platform's invoices and receipts to a tenant, with the PDF attached
(docs/current-state/billing.md).

WHY these are sent with tenant=None: this is PLATFORM mail to the tenant, not
the tenant's mail to its customers. Passing the tenant would (1) put it behind
that tenant's outbound-email switch, which defaults OFF for everyone but
Homebase, so invoices would silently not go, and (2) send it from the tenant's
own customer address, i.e. the tenant would be invoicing itself.

WHY an explicit sender and Reply-To: with tenant=None the choke point falls
back to DEFAULT_FROM_EMAIL / EMAIL_REPLY_TO, and both default to Homebase's
identity. So billing sends from PLATFORM_BILLING_FROM_EMAIL under the issuer's
name, and replies go to the issuer's own billing inbox. The issuer inbox is
also Bcc'd, so the operator keeps a copy of everything that went out.

HOW: every send goes through `send_email_to_recipients` (the one choke point,
which records a SentEmail row, category BILLING) and returns (ok, error). A
manual send is exempt from the automated-email send windows, like every
operator-triggered send. Copy follows the house rules even though the reader
is a business: short lines, no emojis, no dash punctuation.
"""

from html import escape

from django.conf import settings
from django.utils import timezone

from .billing_pdf import build_invoice_pdf, build_receipt_pdf


def _money(currency, value):
    return f'{currency}{value:,.2f}'


def _sender(issuer):
    """'<issuer name> <PLATFORM_BILLING_FROM_EMAIL>' (billing@homexmedia.com)."""
    address = getattr(settings, 'PLATFORM_BILLING_FROM_EMAIL', '') or 'billing@homexmedia.com'
    name = (issuer.get('business_name') or '').strip()
    return f'{name} <{address}>' if name else address


def _html(paragraphs):
    """Plain paragraphs to minimal HTML, every value escaped."""
    body = ''.join(
        f'<p style="margin:0 0 14px;">{escape(p).replace(chr(10), "<br>")}</p>'
        for p in paragraphs if p)
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            f'line-height:1.5;color:#0b1c30;max-width:560px;">{body}</div>')


# ── The document email (owner, 2026-09-24) ──────────────────────────────────
#
# The invoice and receipt emails copy the Stripe receipt email Anthropic
# sends, which the owner sent as the model: the issuer's mark and name on a
# soft background, a white card with "Receipt from <name>", the amount large,
# "Paid <date>", download links and a few label/value facts, then a second
# card with the numbered document, its lines and totals, and a "Questions?"
# line. Built with tables and inline styles, because Gmail and Outlook strip
# <style> blocks and most layout CSS. The mark is the hosted static file
# (Gmail does not show data: images). Every value is escaped.

_BG, _CARD, _LINE = '#F4F6F7', '#FFFFFF', '#E5EAED'
_INK, _MUTED, _LINK = '#0F1A1F', '#5E6E76', '#0A8FD8'
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _long_day(value):
    """1 October 2026 (day first, as in Zimbabwe and South Africa)."""
    return f'{value.day} {value:%B %Y}' if value else ''


def _short_period(invoice):
    """'1 Sep 2026 to 30 Sep 2026', or '' with no period."""
    if not (invoice.period_start and invoice.period_end):
        return ''
    short = lambda d: f'{d.day} {d:%b %Y}'
    return f'{short(invoice.period_start)} to {short(invoice.period_end)}'


def _logo_url():
    """Absolute URL of the HX emblem, or '' if static cannot resolve it."""
    try:
        from django.templatetags.static import static
        base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
        return f"{base}{static('billing/homex_emblem.png')}"
    except Exception:  # noqa: BLE001 - a missing logo never stops an email
        return ''


def _row(label, value, *, strong=False, muted_value=False, top_rule=False):
    """One label/value row across the card."""
    weight = '600' if strong else '400'
    colour = _MUTED if muted_value else _INK
    rule = f'border-top:1px solid {_LINE};' if top_rule else ''
    # The label never wraps ("Invoice number" broke onto two lines at phone
    # width); a long value wraps on its own side instead.
    return (f'<tr><td style="padding:9px 12px 9px 0;{rule}font-size:14px;color:{_MUTED if not strong else _INK};'
            f'font-weight:{weight};white-space:nowrap;vertical-align:top;">{escape(label)}</td>'
            f'<td align="right" style="padding:9px 0;{rule}font-size:14px;color:{colour};'
            f'font-weight:{weight};">{escape(value)}</td></tr>')


def _document_email(*, issuer, eyebrow, amount, subline, note='', links=(), facts=(),
                    title, period='', items=(), totals=(), pay_rows=(), subline_colour=None):
    """The HTML for an invoice / receipt / reminder email.

    eyebrow   "Invoice from HomeX Media"      amount  "US$160.00"
    subline   "Due 1 October 2026"            note    an optional short paragraph
    links     [(label, url)]                  facts   [(label, value)]
    title     "Invoice HMX-2026-0001"         period  "1 Sep 2026 to 30 Sep 2026"
    items     [(description, qty, amount)]    totals  [(label, value, strong)]
    pay_rows  payment_lines(): "Label: value" strings, '' for a gap
    """
    name = escape(issuer.get('business_name') or '')
    logo = _logo_url()
    mark = (f'<img src="{escape(logo)}" width="32" height="32" alt="" '
            f'style="display:block;border-radius:8px;border:0;">' if logo else '')
    contact = (issuer.get('contact_email') or issuer.get('email') or '').strip()

    link_html = ''.join(
        f'<a href="{escape(url)}" style="color:{_INK};text-decoration:none;font-size:14px;'
        f'margin-right:22px;white-space:nowrap;">&#8595;&nbsp;{escape(label)}</a>'
        for label, url in links if url)
    facts_html = ''.join(_row(k, v) for k, v in facts if v)
    items_html = ''.join(
        f'<tr><td style="padding:10px 0 2px;font-size:14px;color:{_INK};">{escape(desc)}'
        f'<div style="font-size:12px;color:{_MUTED};padding-top:2px;">Qty {escape(qty)}</div></td>'
        f'<td align="right" valign="top" style="padding:10px 0 2px;font-size:14px;color:{_INK};">'
        f'{escape(amt)}</td></tr>'
        for desc, qty, amt in items)
    totals_html = ''.join(_row(label, value, strong=strong, top_rule=True)
                          for label, value, strong in totals)
    pay_html = ''
    if pay_rows:
        rows = []
        for line in pay_rows:
            if not line.strip():
                continue
            key, sep, value = line.partition(':')
            rows.append(_row(key.strip(), value.strip()) if sep else
                        f'<tr><td colspan="2" style="padding:6px 0;font-size:14px;color:{_INK};">'
                        f'{escape(line)}</td></tr>')
        pay_html = (f'<tr><td colspan="2" style="padding:22px 0 4px;font-size:15px;font-weight:600;'
                    f'color:{_INK};">How to pay</td></tr>' + ''.join(rows))

    card = (f'background:{_CARD};border:1px solid {_LINE};border-radius:12px;')
    return f'''<!doctype html><html><body style="margin:0;padding:0;background:{_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};font-family:{_FONT};">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;">
<tr><td style="padding:0 4px 20px;">
  <table role="presentation" cellpadding="0" cellspacing="0"><tr>
    <td valign="middle" style="padding-right:12px;">{mark}</td>
    <td valign="middle" style="font-size:17px;font-weight:600;color:{_INK};">{name}</td>
  </tr></table>
</td></tr>
<tr><td style="{card}padding:28px 28px 22px;">
  <div style="font-size:15px;color:{_MUTED};">{escape(eyebrow)}</div>
  <div style="font-size:34px;font-weight:700;color:{_INK};padding:6px 0 4px;">{escape(amount)}</div>
  <div style="font-size:15px;color:{subline_colour or _MUTED};">{escape(subline)}</div>
  {f'<div style="font-size:14px;color:{_INK};padding-top:14px;line-height:1.5;">{escape(note)}</div>' if note else ''}
  {f'<div style="border-top:1px solid {_LINE};margin-top:18px;padding-top:16px;">{link_html}</div>' if link_html else ''}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;">{facts_html}</table>
</td></tr>
<tr><td style="height:16px;line-height:16px;">&nbsp;</td></tr>
<tr><td style="{card}padding:26px 28px 22px;">
  <div style="font-size:18px;font-weight:600;color:{_INK};">{escape(title)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;">
    {f'<tr><td colspan="2" style="padding:4px 0 0;font-size:13px;color:{_MUTED};">{escape(period)}</td></tr>' if period else ''}
    {items_html}
    <tr><td colspan="2" style="height:10px;line-height:10px;">&nbsp;</td></tr>
    {totals_html}
    {pay_html}
  </table>
  {f'<div style="font-size:14px;color:{_MUTED};padding-top:20px;">Questions? Contact <a href="mailto:{escape(contact)}" style="color:{_LINK};text-decoration:none;">{escape(contact)}</a>.</div>' if contact else ''}
</td></tr>
<tr><td align="center" style="padding:20px 0 0;font-size:12px;color:{_MUTED};">{name}{' &middot; ' + escape(issuer.get('tagline') or '') if issuer.get('tagline') else ''}</td></tr>
</table>
</td></tr></table></body></html>'''


def _invoice_card(invoice, *, eyebrow, amount, subline, note='', subline_colour=None,
                  totals, pay=True):
    """The invoice card set shared by the invoice, reminder and switch-off
    emails: one link (the invoice PDF), Invoice number / Due date / Billing
    period, the lines, `totals`, and How to pay (EcoCash first for a
    Zimbabwean client, bank only otherwise; PlatformInvoice.payment_lines)."""
    from .billing_links import invoice_pdf_url
    cur = invoice.currency
    return _document_email(
        issuer=invoice.issuer or {}, eyebrow=eyebrow, amount=amount, subline=subline,
        subline_colour=subline_colour, note=note,
        links=[('Download invoice', invoice_pdf_url(invoice))],
        facts=[('Invoice number', invoice.number), ('Due date', _long_day(invoice.due_date))],
        # The period heads the lines in the second card, as on the Stripe
        # receipt; short months, or it wrapped in the facts at phone width.
        title=f'Invoice {invoice.number}', period=_short_period(invoice),
        items=[(i.description, f'{i.quantity.normalize():f}', _money(cur, i.line_total))
               for i in invoice.items.all()],
        totals=totals, pay_rows=invoice.payment_lines() if pay else ())


def _send(*, to, subject, paragraphs, issuer, pdf, filename, html=None):
    from .models import SentEmail
    from .plumber_notifications import send_email_to_recipients

    if not to:
        return False, 'There is no billing email on this invoice. Add one, then send again.'
    reply_to = (issuer.get('email') or '').strip() or None
    ok = send_email_to_recipients(
        [to], subject, '\n\n'.join(p for p in paragraphs if p),
        # `html` is the designed document email; plain paragraphs otherwise.
        html_message=html or _html(paragraphs),
        attachment=pdf, attachment_name=filename,
        from_email=_sender(issuer), reply_to=reply_to,
        bcc=[reply_to] if reply_to and reply_to.lower() != to.lower() else None,
        tenant=None, category=SentEmail.Category.BILLING,
        to_role=SentEmail.ToRole.OTHER,
    )
    return (True, '') if ok else (False, 'The email did not go out. Check Settings > Email and try again.')


def _days_phrase(days):
    """3 -> 'in 3 days', 1 -> 'tomorrow', 0 -> 'today'."""
    if days <= 0:
        return 'today'
    if days == 1:
        return 'tomorrow'
    return f'in {days} days'


def _invoice_link(invoice):
    """Absolute link to the invoice's billing page, for the operator's emails
    (a login is needed; the page carries the paid / more days / not paid /
    pause buttons, all POST)."""
    from django.urls import reverse
    base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    return f"{base}{reverse('billing_invoice_detail', args=[invoice.pk])}"


def _operator_inbox(issuer):
    from .plumber_notifications import PLATFORM_NOTIFICATION_EMAIL
    return (issuer.get('email') or '').strip() or PLATFORM_NOTIFICATION_EMAIL


def send_billing_notice(invoice, kind, days):
    """One scheduled billing email (bot/billing_reminders.py). Returns
    (ok, error).

    kind:
      'remind'     tenant: invoice due in `days` days (0 = today), PDF attached
      'check'      operator: it was due today, did they pay? link to the page
      'switch_off' tenant: the service is switched off in `days` days unless
                   paid, PDF attached
      'pause'      operator: switch-off day, link to the page to pause them

    Every tenant subject carries "invoice HMX-..." so a reply is left for the
    operator by process_inbound_emails (the "invoice hmx-" subject hint), and
    operator mail is from billing@, which the same reader skips.
    """
    issuer = invoice.issuer or {}
    business = issuer.get('business_name') or ''
    owed = _money(invoice.currency, invoice.balance)
    due = f'{invoice.due_date:%d %b %Y}'
    sign_off = f'Thank you,\n{business}' if business else 'Thank you'
    # EcoCash first for a Zimbabwean client, bank only otherwise: the same
    # PlatformInvoice.payment_lines the PDF prints (owner, 2026-09-24).
    pay_lines = invoice.payment_lines()
    how_to_pay = ('How to pay:\n' + '\n'.join(pay_lines)) if pay_lines else ''

    if kind == 'remind':
        when = 'is due today' if days <= 0 else f'is due on {due}, {_days_phrase(days)}'
        subject = (f'Reminder: invoice {invoice.number} is due today' if days <= 0
                   else f'Reminder: invoice {invoice.number} is due {_days_phrase(days)}')
        paragraphs = [
            f'Hi {invoice.bill_to_name},',
            f'A quick reminder that invoice {invoice.number} for {owed} {when}. '
            'A copy is attached.',
            how_to_pay,
            'If you have already paid, thank you. Reply to this email so we can match it.',
            sign_off,
        ]
        # Same invoice card as the invoice email, headed as a reminder.
        html = _invoice_card(
            invoice, eyebrow=f'Payment reminder from {business}' if business else 'Payment reminder',
            amount=owed,
            subline=('Due today' if days <= 0 else f'Due {_long_day(invoice.due_date)}, {_days_phrase(days)}'),
            note='If you have already paid, thank you. Reply to this email so we can match it.',
            totals=[('Total', _money(invoice.currency, invoice.total), False),
                    ('Amount due', owed, True)])
        return _send(to=invoice.bill_to_email, subject=subject, paragraphs=paragraphs,
                     issuer=issuer, pdf=build_invoice_pdf(invoice),
                     filename=f'{invoice.number}.pdf', html=html)

    if kind == 'switch_off':
        off = f'{invoice.switch_off_on:%d %b %Y}'
        when = 'today' if days <= 0 else f'on {off}, {_days_phrase(days)}'
        subject = (f'Your Plumbot service is switched off today (invoice {invoice.number})'
                   if days <= 0 else
                   f'Your Plumbot service will be switched off {_days_phrase(days)} '
                   f'(invoice {invoice.number})')
        paragraphs = [
            f'Hi {invoice.bill_to_name},',
            f'Invoice {invoice.number} for {owed} was due on {due} and we have not '
            'received payment yet.',
            f'Your Plumbot service will be switched off {when} unless it is paid. '
            'While it is off, the bot stops answering your customers on WhatsApp.',
            how_to_pay,
            'If you have already paid, reply to this email so we can match it.',
            sign_off,
        ]
        # The invoice card again, headed as overdue, the switch-off date in red.
        html = _invoice_card(
            invoice, eyebrow=f'Overdue invoice from {business}' if business else 'Overdue invoice',
            amount=owed,
            subline=(f'Service switched off today unless paid' if days <= 0
                     else f'Service switched off {when} unless paid'),
            subline_colour='#BA1A1A',
            note=(f'This invoice was due on {_long_day(invoice.due_date)}. While the service is off, '
                  'the bot stops answering your customers on WhatsApp. If you have already paid, '
                  'reply to this email so we can match it.'),
            totals=[('Total', _money(invoice.currency, invoice.total), False),
                    ('Amount due', owed, True)])
        return _send(to=invoice.bill_to_email, subject=subject, paragraphs=paragraphs,
                     issuer=issuer, pdf=build_invoice_pdf(invoice),
                     filename=f'{invoice.number}.pdf', html=html)

    # Operator mail: to the billing inbox, from billing@, no attachment.
    link = _invoice_link(invoice)
    if kind == 'check':
        subject = f'Has {invoice.bill_to_name} paid {invoice.number}?'
        paragraphs = [
            f'{invoice.number} for {owed} to {invoice.bill_to_name} was due on {due}.',
            'Open it to mark it paid, give them more days, or mark it not paid. '
            'Not paid starts the switch-off notices to them.',
            link,
        ]
    elif kind == 'pause':
        subject = f'Switch-off day for {invoice.bill_to_name} ({invoice.number})'
        paragraphs = [
            f'{invoice.bill_to_name} still owes {owed} on {invoice.number}, and today '
            'is the switch-off date they were told about.',
            'Open the invoice to pause their account, or record the payment if it came in.',
            link,
        ]
    else:
        return False, f'Unknown billing notice kind: {kind}'
    return _send_operator(to=_operator_inbox(issuer), subject=subject,
                          paragraphs=paragraphs, issuer=issuer)


def _send_operator(*, to, subject, paragraphs, issuer):
    """Operator-facing billing mail: same sender and SentEmail category as the
    tenant mail, no attachment and no Bcc (it already goes to the inbox)."""
    from .models import SentEmail
    from .plumber_notifications import send_email_to_recipients
    ok = send_email_to_recipients(
        [to], subject, '\n\n'.join(p for p in paragraphs if p),
        html_message=_html(paragraphs), from_email=_sender(issuer),
        # Explicit, or tenant=None falls back to EMAIL_REPLY_TO (Homebase's).
        reply_to=to,
        tenant=None, category=SentEmail.Category.BILLING,
        to_role=SentEmail.ToRole.OPERATOR,
    )
    return (True, '') if ok else (False, 'The email did not go out.')


def send_invoice_email(invoice):
    """Email `invoice` to its bill-to address. Returns (ok, error).

    On success it stamps `emailed_at`, and a draft becomes sent: emailing an
    invoice is issuing it, so it also stops being editable.
    """
    issuer = invoice.issuer or {}
    business = issuer.get('business_name') or ''
    total = invoice.balance if invoice.amount_paid else invoice.total
    paragraphs = [
        f'Hi {invoice.bill_to_name},',
        f'Please find attached invoice {invoice.number} for '
        f'{_money(invoice.currency, total)}, due on {invoice.due_date:%d %b %Y}.',
        # Reference first, then EcoCash (Zimbabwean clients) and the bank, as
        # short "Label: value" lines: the owner found sentence instructions
        # too wordy (2026-09-24), so no "Please use ... as your reference".
        ('How to pay:\n' + '\n'.join(invoice.payment_lines())) if invoice.payment_lines() else '',
        'Reply to this email if you have any questions.',
        f'Thank you,\n{business}' if business else 'Thank you',
    ]
    cur = invoice.currency
    totals = [('Subtotal', _money(cur, invoice.total), False), ('Total', _money(cur, invoice.total), False)]
    if invoice.amount_paid:
        totals.append(('Paid', _money(cur, invoice.amount_paid), False))
    totals.append(('Amount due', _money(cur, total), True))
    html = _invoice_card(
        invoice, eyebrow=f'Invoice from {business}' if business else 'Invoice',
        amount=_money(cur, total), subline=f'Due {_long_day(invoice.due_date)}', totals=totals)
    ok, error = _send(
        to=invoice.bill_to_email,
        subject=f'Invoice {invoice.number}' + (f' from {business}' if business else ''),
        paragraphs=paragraphs, issuer=issuer,
        pdf=build_invoice_pdf(invoice), filename=f'{invoice.number}.pdf', html=html,
    )
    if ok:
        now = timezone.now()
        invoice.emailed_at = now
        fields = ['emailed_at', 'updated_at']
        if invoice.status == invoice.Status.DRAFT:
            invoice.status = invoice.Status.SENT
            invoice.sent_at = now
            fields += ['status', 'sent_at']
        invoice.save(update_fields=fields)
    return ok, error


def send_receipt_email(payment):
    """Email the receipt for `payment` to the invoice's bill-to address.
    Returns (ok, error); stamps `emailed_at` on success."""
    invoice = payment.invoice
    issuer = invoice.issuer or {}
    business = issuer.get('business_name') or ''
    balance = invoice.balance
    paragraphs = [
        f'Hi {invoice.bill_to_name},',
        f'Thank you. We received {_money(invoice.currency, payment.amount)} on '
        f'{payment.paid_on:%d %b %Y} for invoice {invoice.number}. '
        f'Your receipt {payment.receipt_number} is attached.',
        (f'Balance remaining on this invoice: {_money(invoice.currency, balance)}.'
         if balance > 0 else 'This invoice is now paid in full.'),
        f'Thank you,\n{business}' if business else 'Thank you',
    ]
    # The Stripe receipt the owner sent as the model: "Receipt from <name>",
    # the amount, "Paid <date>", both download links, then Receipt number /
    # Invoice number / Payment method, and the invoice's lines and totals
    # with this payment as Amount paid.
    from .billing_links import invoice_pdf_url, receipt_pdf_url
    cur = invoice.currency
    method = payment.get_method_display()
    if payment.reference:
        method = f'{method}, {payment.reference}'
    totals = [('Subtotal', _money(cur, invoice.total), False),
              ('Total', _money(cur, invoice.total), False),
              ('Amount paid', _money(cur, payment.amount), True)]
    if balance > 0:
        totals.append(('Balance remaining', _money(cur, balance), False))
    period = _short_period(invoice)
    html = _document_email(
        issuer=issuer, eyebrow=f'Receipt from {business}' if business else 'Receipt',
        amount=_money(cur, payment.amount), subline=f'Paid {_long_day(payment.paid_on)}',
        links=[('Download invoice', invoice_pdf_url(invoice)),
               ('Download receipt', receipt_pdf_url(payment))],
        facts=[('Receipt number', payment.receipt_number), ('Invoice number', invoice.number),
               ('Payment method', method)],
        title=f'Receipt {payment.receipt_number}', period=period,
        items=[(i.description, f'{i.quantity.normalize():f}', _money(cur, i.line_total))
               for i in invoice.items.all()],
        totals=totals)
    ok, error = _send(
        to=invoice.bill_to_email,
        subject=f'Receipt {payment.receipt_number}' + (f' from {business}' if business else ''),
        paragraphs=paragraphs, issuer=issuer,
        pdf=build_receipt_pdf(payment), filename=f'{payment.receipt_number}.pdf', html=html,
    )
    if ok:
        payment.emailed_at = timezone.now()
        payment.save(update_fields=['emailed_at'])
    return ok, error
