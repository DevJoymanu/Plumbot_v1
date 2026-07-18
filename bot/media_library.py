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
