"""
bot/billing_links.py
====================
Private download links for a platform invoice or receipt PDF, the "Download
invoice" / "Download receipt" links in the billing emails (owner, 2026-09-24,
modelled on the Stripe receipt email Anthropic sends).

WHY a signed token and not a login: the reader is a tenant's owner opening
their email, who has no billing login (billing is superuser-only). Stripe's
links work the same way: an unguessable address that opens that one
document.

HOW: django.core.signing (no new dependency, no new column) signs
{"k": "inv"|"rct", "id": <pk>} with a billing-only salt. The token cannot be
forged or edited without SECRET_KEY, and names exactly one document, so it
cannot be walked to another tenant's. No expiry: an invoice from last year
must still open from the email it was sent in. `read_token` returns
(kind, pk) or None, and the public view 404s on None. Pinned by
`BillingEmailDesignTests`.
"""

from django.conf import settings
from django.core import signing
from django.urls import reverse

_SALT = 'platform-billing-document'


def make_token(kind, pk):
    return signing.dumps({'k': kind, 'id': pk}, salt=_SALT, compress=True)


def read_token(token):
    """(kind, pk) for a genuine token, else None."""
    try:
        data = signing.loads(token, salt=_SALT)
    except signing.BadSignature:
        return None
    kind, pk = data.get('k'), data.get('id')
    if kind not in ('inv', 'rct') or not isinstance(pk, int):
        return None
    return kind, pk


def _absolute(path):
    base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    return f'{base}{path}'


def invoice_pdf_url(invoice):
    """Public link to this invoice's PDF, or '' for an unsaved invoice."""
    if not invoice.pk:
        return ''
    return _absolute(reverse('billing_public_document', args=[make_token('inv', invoice.pk)]))


def receipt_pdf_url(payment):
    """Public link to this receipt's PDF, or '' for an unsaved payment."""
    if not payment.pk:
        return ''
    return _absolute(reverse('billing_public_document', args=[make_token('rct', payment.pk)]))
