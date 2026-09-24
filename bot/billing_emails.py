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


def _send(*, to, subject, paragraphs, issuer, pdf, filename):
    from .models import SentEmail
    from .plumber_notifications import send_email_to_recipients

    if not to:
        return False, 'There is no billing email on this invoice. Add one, then send again.'
    reply_to = (issuer.get('email') or '').strip() or None
    ok = send_email_to_recipients(
        [to], subject, '\n\n'.join(p for p in paragraphs if p),
        html_message=_html(paragraphs),
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
        return _send(to=invoice.bill_to_email, subject=subject, paragraphs=paragraphs,
                     issuer=issuer, pdf=build_invoice_pdf(invoice),
                     filename=f'{invoice.number}.pdf')

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
        return _send(to=invoice.bill_to_email, subject=subject, paragraphs=paragraphs,
                     issuer=issuer, pdf=build_invoice_pdf(invoice),
                     filename=f'{invoice.number}.pdf')

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
    ok, error = _send(
        to=invoice.bill_to_email,
        subject=f'Invoice {invoice.number}' + (f' from {business}' if business else ''),
        paragraphs=paragraphs, issuer=issuer,
        pdf=build_invoice_pdf(invoice), filename=f'{invoice.number}.pdf',
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
    ok, error = _send(
        to=invoice.bill_to_email,
        subject=f'Receipt {payment.receipt_number}' + (f' from {business}' if business else ''),
        paragraphs=paragraphs, issuer=issuer,
        pdf=build_receipt_pdf(payment), filename=f'{payment.receipt_number}.pdf',
    )
    if ok:
        payment.emailed_at = timezone.now()
        payment.save(update_fields=['emailed_at'])
    return ok, error
