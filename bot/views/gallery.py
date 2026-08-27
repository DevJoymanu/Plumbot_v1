"""Tenant portal Gallery — owners manage the previous-work media the bot
sends. Writes the same TenantPortfolioItem rows the intake wizard's approve
step creates, so wizard photos and portal uploads are one library."""
import json
import mimetypes
import os
from collections import OrderedDict

from django.contrib import messages
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .. import portfolio_catalog
from ..decorators import staff_required
from ..media_library import (MAX_PORTFOLIO_MEDIA, PENDING_TITLE,
                             clean_price_refs,
                             describe_portfolio_items_async, is_video_filename,
                             portfolio_library_with_prices,
                             price_line_and_tags_for_refs,
                             save_portfolio_upload, tenant_media_count,
                             tenant_prefix)
from ..models import TenantPortfolioItem


def _unique_item_id(tenant, tag: str, path: str) -> str:
    """A per-tenant-unique item_id for a new portfolio row.

    `uniq_portfolio_item_per_tenant` is a DB constraint, so a collision is a
    500, not a warning. Two things used to collide: the id took only the first
    EIGHT characters of the filename (two uploads sharing a prefix landed on
    one id), and nothing checked the id was free before inserting. Widened to
    16 characters and suffixed until free.

    Note this is only for CREATES — an existing row keeps the id it was given,
    which is what logs and dedup already refer to.
    """
    base = slugify(f'{tag}-{os.path.splitext(os.path.basename(path))[0][:16]}')[:72]
    if not base:
        base = 'item'
    candidate, n = base, 2
    while TenantPortfolioItem.objects.filter(tenant=tenant, item_id=candidate).exists():
        suffix = f'-{n}'
        candidate = f'{base[:80 - len(suffix)]}{suffix}'
        n += 1
    return candidate

# Gallery groups, in display order. The annotator's famTag() (gallery.html)
# maps each library job to one of these keys; a multi-item photo carries
# several tags and is grouped under its first (primary) one.
GALLERY_CATEGORIES = [
    ('bathroom install', 'Bathroom installs'),
    ('kitchen', 'Kitchens'),
    ('geyser', 'Geysers'),
    ('drain', 'Drains'),
    ('taps', 'Taps & fixtures'),
    ('pipes', 'Pipes'),
    ('general', 'General'),
]
_CATEGORY_LABELS = dict(GALLERY_CATEGORIES)


def _clean_tags(raw) -> list:
    """Normalise a list of category tags: trimmed, lowercased, capped,
    de-duplicated, first-seen order preserved."""
    seen, out = set(), []
    for tag in raw or []:
        tag = str(tag or '').strip().lower()[:40]
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


# Deleting a row must never unlink homebase's repo-bundled photos — only
# files the tenant actually uploaded (their bucket folder, old intake path).
def _is_tenant_owned_file(tenant, filename: str) -> bool:
    return bool(filename) and (
        filename.startswith(f'{tenant_prefix(tenant)}/')
        or filename.startswith(f'intake_photos/{tenant.slug}/'))


def _tenant_or_404(request):
    tenant = getattr(request, 'tenant', None)
    if tenant is None:
        raise Http404
    return tenant


@staff_required
def gallery_page(request):
    tenant = _tenant_or_404(request)
    items = list(TenantPortfolioItem.objects.filter(tenant=tenant))
    # Bucket each item under its primary (first) category; a multi-item photo
    # still carries all its tags for the filter bar (item.cats_attr).
    buckets = OrderedDict((key, []) for key, _ in GALLERY_CATEGORIES)
    for item in items:
        item.is_video = is_video_filename(item.filename)
        tags = _clean_tags(item.keywords) or ['general']
        item.tag = tags[0]
        item.cats_attr = '|'.join(tags)  # '|' — some keys contain spaces
        item.refs_json = json.dumps(item.price_refs or [])  # for the edit re-link
        buckets.setdefault(item.tag, []).append(item)
    groups = [{'key': key, 'label': _CATEGORY_LABELS.get(key, key.title()),
               'items': group}
              for key, group in buckets.items() if group]
    return render(request, 'bot/pages/gallery.html', {
        'active_nav': 'gallery',
        'items': items,
        'groups': groups,
        'media_used': tenant_media_count(tenant),
        'media_max': MAX_PORTFOLIO_MEDIA,
        'library': portfolio_library_with_prices(tenant),
    })


@staff_required
@require_POST
def gallery_upload(request):
    """AJAX upload for the annotator/pair flows — stores the file, the
    item row is created by gallery_finalize once it's been named."""
    tenant = _tenant_or_404(request)
    upload = request.FILES.get('media')
    if upload is None:
        return JsonResponse({'ok': False, 'error': 'no file'}, status=400)
    path, error = save_portfolio_upload(tenant, upload)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)
    try:
        url = default_storage.url(path)
    except Exception:
        url = ''
    return JsonResponse({'ok': True, 'path': path, 'url': url})


@staff_required
@require_POST
def gallery_finalize(request):
    """Create item rows for annotated uploads. Entries: {path, caption, tag,
    price_line, pair_path?} — pair_path is the BEFORE shot of a pair."""
    tenant = _tenant_or_404(request)
    try:
        entries = json.loads(request.body or '[]')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'bad payload'}, status=400)
    if not isinstance(entries, list):
        return JsonResponse({'ok': False, 'error': 'bad payload'}, status=400)
    prefix = f'{tenant_prefix(tenant)}/'

    # Validate the WHOLE batch before writing anything. This loop used to
    # validate and insert in one pass, so a batch that failed on entry 3 had
    # already committed entries 1 and 2 — and the owner's retry then collided
    # with those on `uniq_portfolio_item_per_tenant`, making every retry worse.
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get('path') or '')
        if not path.startswith(prefix):
            continue  # only files in the tenant's own folder
        # A caption is OPTIONAL: a tenant may just upload their pictures, and
        # vision names the row (describe_portfolio_item). PENDING_TITLE holds
        # the place until it does — the row cannot be titled '' because of the
        # portfolio_title_required constraint.
        caption = str(entry.get('caption') or '').strip()
        pair_path = str(entry.get('pair_path') or '')
        if pair_path and not pair_path.startswith(prefix):
            pair_path = ''
        # The picked jobs (family/variant) link this photo to the price list —
        # price line and categories are pulled from it, so they always match.
        refs = clean_price_refs(entry.get('refs'))
        price_line, derived_tags = price_line_and_tags_for_refs(tenant, refs)
        if refs:
            tags = derived_tags
        else:
            # Hand-typed photo (no library job): keep the owner's own text.
            price_line = str(entry.get('price_line') or '').strip()[:200]
            tags = _clean_tags(entry.get('tags')) or [
                (str(entry.get('tag') or 'general').strip().lower() or 'general')[:40]]
        cleaned.append(dict(
            path=path, pair_path=pair_path, caption=caption,
            price_line=(price_line or '')[:200], refs=refs, tags=tags,
        ))

    created = 0
    fresh_ids = []
    with transaction.atomic():
        for c in cleaned:
            fields = dict(
                pair_filename=c['pair_path'],
                title=(c['caption'] or PENDING_TITLE)[:120],
                description=c['caption'],
                price_line=c['price_line'],
                price_refs=c['refs'],
                keywords=c['tags'],
            )
            # The stored FILE is a photo's real identity, so re-finalizing the
            # same upload (a double-click, a retry after a slow request, a
            # refresh that re-posts) updates that row instead of inserting a
            # duplicate and 500ing on the unique constraint.
            existing = TenantPortfolioItem.objects.filter(
                tenant=tenant, filename=c['path']).first()
            if existing is not None:
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.save()
                continue
            item = TenantPortfolioItem.objects.create(
                tenant=tenant,
                item_id=_unique_item_id(tenant, c['tags'][0], c['path']),
                filename=c['path'],
                sort_order=TenantPortfolioItem.objects.filter(tenant=tenant).count() + 1,
                **fields,
            )
            fresh_ids.append(item.pk)
            created += 1
    # Let the bot look at what it will be sending — in the background, so the
    # owner isn't held up by one call per photo.
    describe_portfolio_items_async(fresh_ids)
    return JsonResponse({'ok': True, 'created': created})


@staff_required
@require_POST
def gallery_add(request):
    tenant = _tenant_or_404(request)
    upload = request.FILES.get('media')
    if upload is None:
        messages.error(request, 'Choose a photo or video first.')
        return redirect('gallery')
    tag = (request.POST.get('tag') or 'general').strip().lower() or 'general'
    # Optional — vision names an unnamed photo (see gallery_finalize).
    caption = (request.POST.get('caption') or '').strip()
    path, error = save_portfolio_upload(tenant, upload)
    if error:
        messages.error(request, error)
        return redirect('gallery')
    item = TenantPortfolioItem.objects.create(
        tenant=tenant,
        item_id=_unique_item_id(tenant, tag, path),
        filename=path,
        title=(caption or PENDING_TITLE)[:120],
        description=caption,
        price_line=(request.POST.get('price_line') or '').strip()[:200],
        keywords=[tag],
        sort_order=TenantPortfolioItem.objects.filter(tenant=tenant).count() + 1,
    )
    describe_portfolio_items_async([item.pk])
    messages.success(request, 'Added to your gallery.')
    return redirect('gallery')


@staff_required
@require_POST
def gallery_update(request, pk):
    tenant = _tenant_or_404(request)
    item = get_object_or_404(TenantPortfolioItem, pk=pk, tenant=tenant)
    title = (request.POST.get('title') or '').strip()
    if not title:
        messages.error(request, 'Please provide names of items for the image.')
        return redirect('gallery')
    item.title = title[:120]
    item.description = (request.POST.get('description') or '').strip()
    # Re-linked jobs → price line + categories come from the price list.
    refs = None
    try:
        refs = clean_price_refs(json.loads(request.POST.get('refs') or 'null'))
    except ValueError:
        refs = None
    if refs:
        price_line, tags = price_line_and_tags_for_refs(tenant, refs)
        item.price_refs = refs
        item.price_line = (price_line or '')[:200]
        item.keywords = tags
    else:
        item.price_refs = []
        item.price_line = (request.POST.get('price_line') or '').strip()[:200]
        # Prefer a multi-item `tags` JSON list; fall back to a single `tag`.
        tags = None
        try:
            tags = _clean_tags(json.loads(request.POST.get('tags') or 'null'))
        except ValueError:
            tags = None
        if not tags:
            single = (request.POST.get('tag') or '').strip().lower()
            tags = [single[:40]] if single else None
        if tags:
            item.keywords = tags
    item.save(update_fields=['title', 'price_line', 'price_refs', 'description', 'keywords'])
    if request.headers.get('x-requested-with') == 'fetch':
        return JsonResponse({'ok': True})
    messages.success(request, f'Updated "{item.title}".')
    return redirect('gallery')


@staff_required
@require_POST
def gallery_delete(request, pk):
    tenant = _tenant_or_404(request)
    item = get_object_or_404(TenantPortfolioItem, pk=pk, tenant=tenant)
    for filename in (item.filename, item.pair_filename):
        if _is_tenant_owned_file(tenant, filename):
            try:
                default_storage.delete(filename)
            except OSError:
                pass
    title = item.title
    item.delete()
    messages.success(request, f'Removed "{title}" from your gallery.')
    return redirect('gallery')


@staff_required
def gallery_media(request, pk):
    """Stream an item's file (works for bucket uploads AND repo-bundled
    photos, which have no public URL). ?pair=1 serves the 'before' shot."""
    tenant = _tenant_or_404(request)
    item = get_object_or_404(TenantPortfolioItem, pk=pk, tenant=tenant)
    filename = item.pair_filename if request.GET.get('pair') else item.filename
    if not filename:
        raise Http404
    if portfolio_catalog._is_storage_path(filename):
        if not default_storage.exists(filename):
            raise Http404
        handle = default_storage.open(filename, 'rb')
    else:
        full = portfolio_catalog.image_path_for({'filename': filename})
        if not os.path.exists(full):
            raise Http404
        handle = open(full, 'rb')
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    return FileResponse(handle, content_type=content_type)
