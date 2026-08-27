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
    # The whole-room jobs. These are priced (renovation/*, package/*) but were
    # missing from the picker, so the biggest-ticket photos a tenant owns could
    # be neither categorised nor price-linked — kitchens had nowhere to go at all.
    ('Renovations', [
        ('Kitchen renovation', 'renovation', 'kitchen'),
        ('Bathroom renovation', 'renovation', 'bathroom'),
        ('Full bathroom package', 'package', 'full_bathroom'),
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
    if family == 'renovation':
        # Kitchens are their own bucket; a bathroom renovation shows with the
        # bathroom work rather than opening a near-duplicate group.
        return 'kitchen' if variant == 'kitchen' else 'bathroom install'
    if family == 'package' and variant == 'full_bathroom':
        return 'bathroom install'
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


def price_line_for_item(tenant, item) -> str:
    """The best price line we can produce for one portfolio photo, or ''.

    Three sources, most authoritative first:

      1. the stored `price_line` (written at annotation, kept fresh by
         resync_portfolio_prices)
      2. its `price_refs`, resolved LIVE against the price list — so a price
         added after the photo was annotated still shows
      3. the photo's own TITLE matched against the tenant's price families

    Step 3 exists because a photo is only linked to the price list if whoever
    annotated it picked a job from the library, and the library does not offer
    every family a tenant sells. Prod 2026-08-27: barmak's price sheet had
    `borehole` at US$500 all-in and their gallery had a photo titled
    "Borehole", but with `price_refs=[]` and a blank `price_line` — so a
    customer quoting that photo could not be told the price the tenant had
    already entered.

    Matching is deliberately strict (whole title, or title starting with the
    name) — a loose match would price the wrong job, which is the failure this
    whole path exists to prevent.
    """
    stored = (getattr(item, 'price_line', '') or '').strip()
    if stored:
        return stored

    live, _ = price_line_and_tags_for_refs(tenant, getattr(item, 'price_refs', None))
    if live:
        return live

    title = (getattr(item, 'title', '') or '').strip().lower()
    if not title:
        return ''
    from .models import TenantPriceItem
    cur = _tenant_currency(tenant)
    for row in TenantPriceItem.objects.filter(tenant=tenant, is_active=True):
        value = _price_value(row)
        if value is None:
            continue
        names = {
            (row.family or '').replace('_', ' ').replace('-', ' ').strip().lower(),
            (row.variant or '').replace('_', ' ').strip().lower(),
            (row.label or '').strip().lower(),
            (row.short_label or '').strip().lower(),
        }
        names.update(str(k).strip().lower() for k in (row.keywords or []))
        names.discard('')
        if any(title == n or title.startswith(f'{n} ') for n in names):
            label = row.label or row.short_label or (row.family or '').replace('_', ' ')
            return f"{label[:1].upper()}{label[1:]} from {cur}{_price_display(value)}"
    return ''


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


# ── Vision on our OWN gallery photos ─────────────────────────────────────────
# The bot describes a CUSTOMER's photo on arrival, but its own previous-work
# photos carried only a title. A customer who quotes one and asks "how much"
# therefore gave the classifiers a single word to work with ("Borehole"), which
# is how a borehole question came back priced as a bathroom package (prod,
# 2026-08-27). Describing them here — once, when the photo is added, not on
# every send — gives that quote real text to resolve against.

def describe_portfolio_item(item) -> str:
    """Fill and save `vision_description` for one portfolio row. Returns it.

    Best-effort: returns '' and leaves the row untouched on any failure, and
    never re-describes a row that already has one.
    """
    if item is None or getattr(item, 'vision_description', ''):
        return getattr(item, 'vision_description', '') or ''
    name = getattr(item, 'filename', '') or ''
    if not name or is_video_filename(name):
        return ''
    try:
        import mimetypes

        from .services.vision import describe_portfolio_image
        with default_storage.open(name, 'rb') as handle:
            payload = handle.read()
        mime = mimetypes.guess_type(name)[0] or 'image/jpeg'
        description = describe_portfolio_image(
            payload, mime, tenant=getattr(item, 'tenant', None))
    except Exception:
        return ''
    if not description:
        return ''
    item.vision_description = description
    try:
        item.save(update_fields=['vision_description'])
    except Exception:
        return ''
    return description


def describe_portfolio_items_async(item_ids):
    """Describe new gallery photos in a daemon thread.

    Off the request path deliberately: a gallery batch is up to 20 photos and
    each call takes a second or two — the owner should not sit through that.
    Same pattern as regenerate_lead_magnet_async.
    """
    item_ids = [i for i in (item_ids or []) if i]
    if not item_ids:
        return
    import threading

    def _work():
        from .models import TenantPortfolioItem
        for item in TenantPortfolioItem.objects.filter(pk__in=item_ids):
            try:
                describe_portfolio_item(item)
            except Exception:
                pass
    threading.Thread(target=_work, daemon=True).start()
