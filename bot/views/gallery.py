"""Tenant portal Gallery — owners manage the previous-work media the bot
sends. Writes the same TenantPortfolioItem rows the intake wizard's approve
step creates, so wizard photos and portal uploads are one library."""
import json
import mimetypes
import os

from django.contrib import messages
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .. import portfolio_catalog
from ..decorators import staff_required
from ..media_library import (MAX_PORTFOLIO_MEDIA, is_video_filename,
                             portfolio_library_with_prices,
                             save_portfolio_upload, tenant_media_count,
                             tenant_prefix)
from ..models import TenantPortfolioItem

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
    for item in items:
        item.is_video = is_video_filename(item.filename)
        item.tag = (item.keywords or ['general'])[0]
    return render(request, 'bot/pages/gallery.html', {
        'active_nav': 'gallery',
        'items': items,
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
    created = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get('path') or '')
        if not path.startswith(prefix):
            continue  # only files in the tenant's own folder
        caption = str(entry.get('caption') or '').strip()
        if not caption:
            return JsonResponse(
                {'ok': False,
                 'error': 'Please provide names of items for the image.'},
                status=400)
        pair_path = str(entry.get('pair_path') or '')
        if pair_path and not pair_path.startswith(prefix):
            pair_path = ''
        tag = (str(entry.get('tag') or 'general').strip().lower() or 'general')[:40]
        TenantPortfolioItem.objects.create(
            tenant=tenant,
            item_id=slugify(
                f'{tag}-{os.path.splitext(os.path.basename(path))[0][:8]}')[:80],
            filename=path,
            pair_filename=pair_path,
            title=caption[:120],
            description=caption,
            price_line=str(entry.get('price_line') or '').strip()[:200],
            keywords=[tag],
            sort_order=TenantPortfolioItem.objects.filter(tenant=tenant).count() + 1,
        )
        created += 1
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
    caption = (request.POST.get('caption') or '').strip()
    if not caption:
        messages.error(request, 'Please provide names of items for the image.')
        return redirect('gallery')
    path, error = save_portfolio_upload(tenant, upload)
    if error:
        messages.error(request, error)
        return redirect('gallery')
    TenantPortfolioItem.objects.create(
        tenant=tenant,
        item_id=slugify(f'{tag}-{os.path.splitext(os.path.basename(path))[0][:8]}')[:80],
        filename=path,
        title=caption[:120],
        description=caption,
        price_line=(request.POST.get('price_line') or '').strip()[:200],
        keywords=[tag],
        sort_order=TenantPortfolioItem.objects.filter(tenant=tenant).count() + 1,
    )
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
    item.price_line = (request.POST.get('price_line') or '').strip()[:200]
    item.description = (request.POST.get('description') or '').strip()
    tag = (request.POST.get('tag') or '').strip().lower()
    if tag:
        item.keywords = [tag[:40]]
    item.save(update_fields=['title', 'price_line', 'description', 'keywords'])
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
