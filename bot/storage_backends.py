"""
Cloudflare R2 Storage Backend for Django
Supports images, documents, and videos
"""

from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings


class R2MediaStorage(S3Boto3Storage):
    """Storage backend for all user-uploaded media files (plans, documents, videos)"""
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    file_overwrite = False
    custom_domain = getattr(settings, 'R2_CUSTOM_DOMAIN', None)

    # No fixed location — subclasses set their own folder
    def url(self, name):
        if self.custom_domain:
            return f"https://{self.custom_domain}/{name}"
        return super().url(name)


class R2ImageStorage(R2MediaStorage):
    """Images: jpg, png, webp, gif"""
    location = 'media/images'


class R2DocumentStorage(R2MediaStorage):
    """Documents: PDF, etc."""
    location = 'media/documents'


class R2VideoStorage(R2MediaStorage):
    """Videos: mp4, 3gp, etc."""
    location = 'media/videos'

    # Videos can be large — increase timeouts
    object_parameters = {
        'ContentType': 'video/mp4',
    }


class R2StaticStorage(R2MediaStorage):
    """Static files: CSS, JS, images"""
    location = 'static'
    file_overwrite = True


# Default catch-all for anything going through DEFAULT_FILE_STORAGE
class R2DefaultStorage(R2MediaStorage):
    location = 'media'