"""
bot/short_links.py
==================
A short link on the BUSINESS's own domain that forwards to the plumber's
per-lead wa.me link: https://wa.homebaseplumbing.co.zw/q/Jd7Kx2

WHY (owner, 2026-09-21): the per-lead pre-filled wa.me link is ~330 characters,
and the plumber's WhatsApp Business short link can only carry one fixed
message. These leads come from a Facebook ad with their guard up about scams,
so the short link must not be our platform's domain or a public shortener; on
the business's own domain (a "wa." subdomain, so it reads as WhatsApp and does
not touch their website) it matches the business they are already talking to.
When tapped, WhatsApp's preview and the chat it opens are WhatsApp's own.

HOW: the code is the lead's id in base62 plus a 4-character signature (HMAC of
the id with SECRET_KEY), so it needs no table and cannot be walked to other
leads. ``bot.views.short_links.plumber_link_redirect`` decodes it, checks the
request came in on THAT lead's tenant's domain, rebuilds the wa.me link from
the lead's current details (``plumber_link.long_quote_link``) and 302s to it.
A tenant opts in by setting ``TenantProfile.scripts['short_link_domain']``
(``TenantConfig.short_link_domain``); the host must also be in the Railway
``ALLOWED_HOSTS`` variable and point at the web service. No domain means no
short link, and the caller uses the long wa.me link.

Pinned by the "short link" cases in TEST 0 and ``ShortLinkTests``.
"""

import hashlib
import hmac
import re

from django.conf import settings

_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
_SIG_LEN = 4
_CODE_RE = re.compile(r'^[0-9a-zA-Z]{%d,16}$' % (_SIG_LEN + 1))


def _b62(n: int) -> str:
    if n <= 0:
        return '0'
    out = ''
    while n:
        n, r = divmod(n, 62)
        out = _ALPHABET[r] + out
    return out


def _from_b62(s: str) -> int:
    n = 0
    for ch in s:
        n = n * 62 + _ALPHABET.index(ch)
    return n


def _sig(lead_id: int) -> str:
    digest = hmac.new(settings.SECRET_KEY.encode(), f'plumber-link:{lead_id}'.encode(),
                      hashlib.sha256).digest()
    return _b62(int.from_bytes(digest[:8], 'big'))[-_SIG_LEN:].rjust(_SIG_LEN, '0')


def encode(lead_id: int) -> str:
    """The short code for a lead: base62 id + signature."""
    return _b62(int(lead_id)) + _sig(int(lead_id))


def decode(code) -> int:
    """The lead id a code stands for, or None when it is malformed or forged."""
    code = str(code or '')
    if not _CODE_RE.match(code):
        return None
    body, sig = code[:-_SIG_LEN], code[-_SIG_LEN:]
    try:
        lead_id = _from_b62(body)
    except ValueError:
        return None
    return lead_id if hmac.compare_digest(sig, _sig(lead_id)) else None


def short_url(appointment) -> str:
    """https://<tenant short-link domain>/q/<code>, or '' when the lead's own
    tenant has no domain set (never another tenant's domain)."""
    if getattr(appointment, 'pk', None) is None:
        return ''
    try:
        from .tenant_config import get_config
        domain = get_config(getattr(appointment, 'tenant', None)).short_link_domain
    except Exception:
        return ''
    return f'https://{domain}/q/{encode(appointment.pk)}' if domain else ''
