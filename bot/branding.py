"""
bot/branding.py
===============
One place that answers "what does this business look like?".

Before this, the logo was a hardcoded static file (`bot/static/images/logo.jpg`
— Homebase's), pulled in by `utils._safe_logo_url` / `_safe_logo_data_uri`. Every
tenant's quote therefore went out under Homebase's mark, which is the exact
thing the no-value-crosses-tenants rule exists to prevent.

The logo now lives on `TenantProfile.logo` and is read ONLY through here, so the
four places it appears — the quote PDF, the quote email header, the intake form
header and the dashboard — cannot drift apart or fall back differently:

    logo_url(tenant)        -> a URL for a browser (email, dashboard, forms)
    logo_data_uri(tenant)   -> bytes inlined, for PDFs and mail that must not
                               depend on the media host being reachable
    logo_bytes(tenant)      -> raw bytes + content type, for reportlab
    brand_name(tenant)      -> the fallback, and the alt text

**Absent means fall back, never borrow.** A tenant with no logo gets their own
business name as text — never the platform's mark and never another tenant's.
That is the one rule every caller has to honour, so `brand_name` is always
returned alongside the logo rather than left for each caller to invent.
"""

import base64
import logging
import mimetypes
import os

from django.conf import settings

logger = logging.getLogger(__name__)


# Upload constraints. Stated here rather than left to the form so the API, the
# profile page and the platform console all enforce the same thing.
ALLOWED_LOGO_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.svg')
ALLOWED_LOGO_CONTENT_TYPES = (
    'image/png', 'image/jpeg', 'image/jpg', 'image/svg+xml',
)
MAX_LOGO_BYTES = 2 * 1024 * 1024          # 2 MB
RECOMMENDED_LOGO_WIDTH = 400              # px — the width the quote letterhead draws at
LOGO_HELP_TEXT = (
    'PNG, JPG or SVG. Up to 2 MB. Around 400px wide works best, '
    'and a transparent background looks cleanest on the quote.'
)


class LogoRejected(ValueError):
    """An uploaded file that is not a usable logo. The message is shown to the
    user as-is, so it says what to do rather than what went wrong."""


def validate_logo(upload):
    """Check an uploaded logo, raising LogoRejected with a usable message.

    Returns the upload so callers can write ``profile.logo = validate_logo(f)``.
    Format is checked on BOTH the extension and the browser-reported content
    type: the extension alone is trivially wrong, and the content type alone is
    missing or generic often enough to fail honest uploads.
    """
    if upload is None:
        raise LogoRejected('Choose a file to upload.')

    name = (getattr(upload, 'name', '') or '').lower()
    ext = os.path.splitext(name)[1]
    content_type = (getattr(upload, 'content_type', '') or '').lower()

    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise LogoRejected(
            'That file type is not supported. Please upload a PNG, JPG or SVG.')
    if content_type and content_type not in ALLOWED_LOGO_CONTENT_TYPES:
        raise LogoRejected(
            'That file does not look like an image. Please upload a PNG, JPG or SVG.')

    size = getattr(upload, 'size', None)
    if size is not None and size > MAX_LOGO_BYTES:
        raise LogoRejected(
            'That file is {:.1f} MB. Please upload a logo of 2 MB or less.'.format(
                size / (1024 * 1024)))
    return upload


# ── Reading it back ──────────────────────────────────────────────────────────

def _profile(tenant):
    if tenant is None:
        return None
    try:
        from bot.models import TenantProfile
        return TenantProfile.objects.filter(tenant=tenant).first()
    except Exception:
        logger.exception('branding: could not load profile for %s', tenant)
        return None


def has_logo(tenant) -> bool:
    return bool(logo_url(tenant))


def logo_url(tenant) -> str:
    """A URL for the logo, or '' when this tenant has not set one.

    '' is a real answer, not a failure: the caller renders brand_name instead.
    """
    profile = _profile(tenant)
    logo = getattr(profile, 'logo', None) if profile else None
    if not logo:
        return ''
    try:
        return logo.url
    except Exception:
        # A row pointing at a file the storage backend no longer has. Fall back
        # to the business name rather than a broken image.
        logger.warning('branding: logo file missing for tenant %s', tenant)
        return ''


def logo_bytes(tenant):
    """(bytes, content_type) for the logo, or (None, None).

    For anything that must embed the file rather than link to it — the PDF, and
    email clients that will not fetch remote images.
    """
    profile = _profile(tenant)
    logo = getattr(profile, 'logo', None) if profile else None
    if not logo:
        return None, None
    try:
        with logo.open('rb') as fh:
            raw = fh.read()
    except Exception:
        logger.warning('branding: could not read logo for tenant %s', tenant)
        return None, None
    content_type = mimetypes.guess_type(logo.name)[0] or 'image/png'
    return raw, content_type


def logo_data_uri(tenant) -> str:
    """The logo inlined as a data: URI, or ''.

    Used where a linked image would not survive: the quote PDF, and mail read
    offline or with remote images blocked.
    """
    raw, content_type = logo_bytes(tenant)
    if not raw:
        return ''
    return 'data:{};base64,{}'.format(
        content_type, base64.b64encode(raw).decode('ascii'))


def brand_name(tenant) -> str:
    """What to print when there is no logo — and the logo's alt text.

    The tenant's letterhead trading name first (that is the name on their
    paperwork), then the tenant's own name. Never a platform default: an
    unnamed tenant gets '', and the caller omits the block entirely.
    """
    profile = _profile(tenant)
    if profile:
        letterhead = getattr(profile, 'letterhead', None) or {}
        trading = (letterhead.get('business_name') or '').strip()
        if trading:
            return trading
    return (getattr(tenant, 'name', '') or '').strip()


def branding_context(tenant) -> dict:
    """Everything a template needs to render the mark, in one call.

    Templates should use this rather than calling the three readers separately —
    it is what keeps "logo, else business name, else nothing" identical on the
    quote, the email, the intake form and the dashboard.
    """
    url = logo_url(tenant)
    return {
        'logo_url': url,
        'logo_data_uri': logo_data_uri(tenant) if url else '',
        'brand_name': brand_name(tenant),
        'has_logo': bool(url),
    }


def save_logo(tenant, upload):
    """Validate and store a logo for this tenant. Returns the saved profile.

    Raises LogoRejected for anything unusable — the caller shows the message.
    """
    from bot.models import TenantProfile

    validate_logo(upload)
    profile, _ = TenantProfile.objects.get_or_create(tenant=tenant)
    profile.logo = upload
    profile.save(update_fields=['logo'])
    return profile


def clear_logo(tenant):
    """Remove this tenant's logo, falling the four surfaces back to the name."""
    from bot.models import TenantProfile

    profile = TenantProfile.objects.filter(tenant=tenant).first()
    if profile and profile.logo:
        profile.logo.delete(save=False)
        profile.logo = None
        profile.save(update_fields=['logo'])
    return profile
