"""Shared upload rules for tenant portfolio media (photos + short videos).

Every tenant's uploads live under one bucket prefix so the R2 dashboard
shows a folder per tenant: tenant_portfolios/<slug>/<uuid>.<ext>.
The cap and type rules live here so the wizard's upload endpoint and the
portal Gallery page can't drift apart.
"""
import uuid

from django.core.files.storage import default_storage

PORTFOLIO_PREFIX = 'tenant_portfolios'
MAX_PORTFOLIO_MEDIA = 20

IMAGE_EXTS = ('jpg', 'jpeg', 'png', 'webp')
VIDEO_EXTS = ('mp4', 'mov', '3gp')  # the set WhatsApp Cloud API can send
IMAGE_MAX_BYTES = 8 * 1024 * 1024
VIDEO_MAX_BYTES = 16 * 1024 * 1024  # WhatsApp's own video send cap


def is_video_filename(filename: str) -> bool:
    return (filename or '').rsplit('.', 1)[-1].lower() in VIDEO_EXTS


# The categorized job library shown when annotating gallery photos (mirrors
# the intake wizard's LIBRARY). (label, family, variant) — family/variant
# join to TenantPriceItem so the tenant's own price rides along.
PORTFOLIO_LIBRARY = [
    ('Geysers', [
        ('Geyser supply & install', 'geyser', ''),
        ('Full geyser replacement', 'geyser_service', 'replacement'),
        ('Element replacement', 'geyser_service', 'element'),
        ('Thermostat replacement', 'geyser_service', 'thermostat'),
        ('Pressure valve', 'geyser_service', 'pressure_valve'),
    ]),
    ('Drains', [
        ('Drain unblocking (simple)', 'repair', 'drain_simple'),
        ('Severe blockage / sewer line', 'repair', 'drain_severe'),
        ('High-pressure jetting', 'repair', 'jetting'),
    ]),
    ('Taps & fixtures', [
        ('Leaking tap', 'repair', 'leaking_tap'),
        ('Toilet seat replacement', 'repair', 'toilet_seat_replacement'),
        ('Cistern repair', 'repair', 'cistern'),
        ('Full toilet replacement', 'repair', 'full_toilet_replacement'),
    ]),
    ('Pipes', [
        ('Minor pipe leak', 'repair', 'minor_pipe_leak'),
        ('Burst pipe', 'repair', 'burst_pipe'),
        ('Pipe section replacement', 'repair', 'pipe_section'),
    ]),
    ('Specials & packages', [
        ('Facebook / social media special', 'package', 'facebook'),
    ]),
    ('Installs', [
        ('Shower cubicle', 'shower', ''),
        ('Vanity unit', 'vanity', ''),
        ('Toilet install', 'toilet', ''),
        ('Basin', 'basin', ''),
        ('Built-in tub', 'tub', ''),
        ('Freestanding tub', 'tub', 'freestanding'),
        ('Side chamber', 'chamber', ''),
    ]),
]


def _price_display(value) -> str:
    text = str(value)
    return text.rstrip('0').rstrip('.') if '.' in text else text


# (family, variant) → (library label, category) — the labels/categories used
# when composing a photo's price line and bucketing it in the gallery.
_LIBRARY_INDEX = {(family, variant or ''): (label, cat)
                  for cat, items in PORTFOLIO_LIBRARY
                  for label, family, variant in items}


def _fam_tag(family: str, variant: str) -> str:
    """Gallery category key for a job — server-side twin of gallery.html's
    famTag(); keep the two in lockstep so bucketing is identical either side."""
    if family.startswith('geyser'):
        return 'geyser'
    if variant in ('drain_simple', 'drain_severe', 'jetting'):
        return 'drain'
    if variant in ('leaking_tap', 'toilet_seat_replacement', 'cistern', 'full_toilet_replacement'):
        return 'taps'
    if variant in ('minor_pipe_leak', 'burst_pipe', 'pipe_section'):
        return 'pipes'
    if family in ('shower', 'vanity', 'toilet', 'basin', 'tub', 'chamber'):
        return 'bathroom install'
    return 'general'


def _price_value(row):
    """A price row's headline figure: all-in, else flat, else supply+labour."""
    value = row.allin or row.flat
    if value is None and row.supply is not None and row.labour is not None:
        value = row.supply + row.labour
    return value


def clean_price_refs(raw) -> list:
    """Normalise a photo's price refs to [{family, variant}] — the link to the
    price list. De-duplicated, first-seen order kept."""
    out, seen = [], set()
    for ref in raw or []:
        if not isinstance(ref, dict):
            continue
        family = str(ref.get('family') or '').strip().lower()[:40]
        variant = str(ref.get('variant') or '').strip().lower()[:40]
        if not family:
            continue
        key = (family, variant)
        if key in seen:
            continue
        seen.add(key)
        out.append({'family': family, 'variant': variant})
    return out


def _tenant_currency(tenant) -> str:
    from .models import TenantProfile
    profile = TenantProfile.objects.filter(tenant=tenant).first()
    return (profile.currency if profile and profile.currency else 'US$')


def price_line_and_tags_for_refs(tenant, refs):
    """The AUTHORITATIVE price line + gallery category tags for a photo, pulled
    live from the tenant's price list. `<label> from <cur><price>` per priced
    ref (newline-joined, blank while unpriced); tags come from every ref so the
    photo is bucketed by the jobs it shows. Returns (None, None) when there are
    no refs — the caller then keeps whatever was typed by hand."""
    refs = clean_price_refs(refs)
    if not refs:
        return None, None
    from .models import TenantPriceItem
    cur = _tenant_currency(tenant)
    rows = {(r.family, r.variant or ''): r
            for r in TenantPriceItem.objects.filter(tenant=tenant)}
    lines, tags, seen = [], [], set()
    for ref in refs:
        key = (ref['family'], ref['variant'])
        label = _LIBRARY_INDEX.get(key, (ref['family'].replace('_', ' '), None))[0]
        tag = _fam_tag(ref['family'], ref['variant'])
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
        value = _price_value(rows[key]) if key in rows else None
        if value is not None:
            lines.append(f"{label} from {cur}{_price_display(value)}")
    return '\n'.join(lines), (tags or ['general'])


def infer_price_refs(item) -> list:
    """Best-effort price-list link for a legacy photo saved before refs existed:
    match the library job labels against the photo's own text — its auto-composed
    title / description / price line named the jobs it shows. Longest labels
    first so 'Freestanding tub' wins over a bare 'tub' style match."""
    haystack = ' '.join(
        (item.title or '', item.description or '', item.price_line or '')).lower()
    refs, seen = [], set()
    for (family, variant), (label, _cat) in sorted(
            _LIBRARY_INDEX.items(), key=lambda kv: -len(kv[1][0])):
        if label.lower() in haystack:
            key = (family, variant)
            if key not in seen:
                seen.add(key)
                refs.append({'family': family, 'variant': variant})
    return refs


def resync_portfolio_prices(tenant) -> int:
    """Re-pull every linked photo's price line (and category) from the current
    price list — called after prices change so images and prices never drift.
    Photos saved before the link existed are back-filled from their own text so
    they sync too; truly hand-typed photos (no match) are left alone."""
    from .models import TenantPortfolioItem
    updated = 0
    for item in TenantPortfolioItem.objects.filter(tenant=tenant):
        fields = []
        refs = item.price_refs or []
        if not refs:
            refs = infer_price_refs(item)
            if not refs:
                continue
            item.price_refs = refs
            fields.append('price_refs')          # persist the recovered link
        line, tags = price_line_and_tags_for_refs(tenant, refs)
        if line is None:
            continue
        if item.price_line != line[:200]:
            item.price_line = line[:200]
            fields.append('price_line')
        if tags and item.keywords != tags:
            item.keywords = tags
            fields.append('keywords')
        if fields:
            item.save(update_fields=fields)
            updated += 1
    return updated


def portfolio_library_with_prices(tenant):
    """PORTFOLIO_LIBRARY as JSON-ready dicts with the tenant's own price
    (all-in, else flat, else supply+labour) attached to each item; '' when
    the tenant hasn't priced that job."""
    from .models import TenantPriceItem
    prices = {}
    for row in TenantPriceItem.objects.filter(tenant=tenant):
        value = row.allin or row.flat
        if value is None and row.supply is not None and row.labour is not None:
            value = row.supply + row.labour
        if value is not None:
            prices[(row.family, row.variant or '')] = _price_display(value)
    return [{
        'cat': cat,
        'items': [{'label': label, 'family': family, 'variant': variant,
                   'price': prices.get((family, variant), '')}
                  for label, family, variant in items],
    } for cat, items in PORTFOLIO_LIBRARY]


# Inbound customer media (plans, site photos/videos, voice notes) gets a
# per-tenant subfolder too, so the bucket reads customer_plans/<slug>/...
CUSTOMER_MEDIA_FOLDERS = {
    'image':    'customer_plans',
    'document': 'customer_plans',
    'video':    'customer_videos',
    'audio':    'customer_audio',
}


def customer_media_path(tenant, media_type: str, filename: str) -> str:
    folder = CUSTOMER_MEDIA_FOLDERS.get(media_type, 'customer_media')
    slug = getattr(tenant, 'slug', None) or 'homebase'
    return f'{folder}/{slug}/{filename}'


def tenant_prefix(tenant) -> str:
    return f'{PORTFOLIO_PREFIX}/{tenant.slug}'


def tenant_media_count(tenant) -> int:
    """How many files this tenant has in the bucket (wizard uploads included,
    even before approval — abandoned uploads still occupy quota until cleaned)."""
    try:
        _dirs, files = default_storage.listdir(tenant_prefix(tenant))
        return len(files)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return 0


def save_portfolio_upload(tenant, upload):
    """Validate + store one uploaded file under the tenant's folder.

    Returns (path, None) on success or (None, error_message) on rejection.
    """
    ext = (upload.name.rsplit('.', 1)[-1] if '.' in upload.name else '').lower()
    if ext in VIDEO_EXTS:
        if upload.size > VIDEO_MAX_BYTES:
            return None, 'Video too large (16 MB max — WhatsApp cannot send bigger).'
    elif ext in IMAGE_EXTS:
        if upload.size > IMAGE_MAX_BYTES:
            return None, 'Photo too large (8 MB max).'
    else:
        return None, 'Use a JPG, PNG, or WebP photo, or an MP4/MOV video.'
    if tenant_media_count(tenant) >= MAX_PORTFOLIO_MEDIA:
        return None, (f'Media limit reached ({MAX_PORTFOLIO_MEDIA} files). '
                      'Delete something from your gallery first.')
    path = default_storage.save(
        f'{tenant_prefix(tenant)}/{uuid.uuid4().hex}.{ext}', upload)
    return path, None
