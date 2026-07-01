"""
WhatsApp Cloud API Integration Module
Supports text, images, documents, audio, and video
"""

import requests
import json
import os
import time
from typing import Dict, Optional, List
import mimetypes
from django.core.files.storage import default_storage
from django.utils import timezone
import base64
import os
import requests
from pathlib import Path


# ─── MIME type helpers ───────────────────────────────────────────────────────

# All file extensions supported, mapped to MIME types
EXTENSION_TO_MIME = {
    # Images
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png':  'image/png',
    '.webp': 'image/webp',
    '.gif':  'image/gif',
    # Documents
    '.pdf':  'application/pdf',
    '.doc':  'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xls':  'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    # Videos
    '.mp4':  'video/mp4',
    '.3gp':  'video/3gpp',
    '.3gpp': 'video/3gpp',
    '.mov':  'video/quicktime',
    '.avi':  'video/x-msvideo',
    '.mkv':  'video/x-matroska',
    # Audio
    '.mp3':  'audio/mpeg',
    '.ogg':  'audio/ogg',
    '.wav':  'audio/wav',
    '.m4a':  'audio/mp4',
    '.aac':  'audio/aac',
    '.amr':  'audio/amr',
}

# MIME type → file extension (for saving downloaded files)
MIME_TO_EXTENSION = {
    # Images
    'image/jpeg':       '.jpg',
    'image/png':        '.png',
    'image/webp':       '.webp',
    'image/gif':        '.gif',
    # Documents
    'application/pdf':  '.pdf',
    'application/msword': '.doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    # Videos
    'video/mp4':        '.mp4',
    'video/3gpp':       '.3gp',
    'video/quicktime':  '.mov',
    'video/x-msvideo':  '.avi',
    'video/x-matroska': '.mkv',
    'video/mpeg':       '.mpeg',
    # Audio
    'audio/mpeg':       '.mp3',
    'audio/ogg':        '.ogg',
    'audio/wav':        '.wav',
    'audio/mp4':        '.m4a',
    'audio/aac':        '.aac',
    'audio/amr':        '.amr',
}

# WhatsApp media type categories
WHATSAPP_MEDIA_TYPES = {
    'image':    ['image/jpeg', 'image/png', 'image/webp', 'image/gif'],
    'video':    ['video/mp4', 'video/3gpp', 'video/quicktime'],
    'audio':    ['audio/mpeg', 'audio/ogg', 'audio/wav', 'audio/mp4', 'audio/aac', 'audio/amr'],
    'document': ['application/pdf', 'application/msword',
                 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
}

# WhatsApp size limits (bytes)
MEDIA_SIZE_LIMITS = {
    'image':    5 * 1024 * 1024,    # 5 MB
    'video':    16 * 1024 * 1024,   # 16 MB
    'audio':    16 * 1024 * 1024,   # 16 MB
    'document': 100 * 1024 * 1024,  # 100 MB
}


def get_media_category(mime_type: str) -> str:
    """Return WhatsApp media category for a MIME type."""
    for category, mime_list in WHATSAPP_MEDIA_TYPES.items():
        if mime_type in mime_list:
            return category
    return 'document'  # safe fallback


def get_extension_for_mime(mime_type: str) -> str:
    """Return file extension for a MIME type."""
    return MIME_TO_EXTENSION.get(mime_type, '.bin')


class WhatsAppCloudAPI:
    """
    WhatsApp Cloud API client.
    Supports text, images, documents, video, audio.
    """

    def __init__(self):
        self.access_token = os.environ.get('WHATSAPP_ACCESS_TOKEN')
        self.phone_number_id = os.environ.get('WHATSAPP_PHONE_NUMBER_ID')
        self.business_account_id = os.environ.get('WHATSAPP_BUSINESS_ACCOUNT_ID')
        self.verify_token = os.environ.get('WHATSAPP_VERIFY_TOKEN', 'your_verify_token_here')
        self.api_version = 'v21.0'
        self.base_url = f'https://graph.facebook.com/{self.api_version}'

    # ─── Internal helpers ────────────────────────────────────────────────────

    def _headers(self, content_type: str = 'application/json') -> Dict:
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': content_type,
        }

    def _clean_phone(self, phone: str) -> str:
        return phone.replace('whatsapp:', '').replace('+', '').strip()

    def _messages_url(self) -> str:
        return f'{self.base_url}/{self.phone_number_id}/messages'

    # Meta statuses worth a quick retry: rate-limited or a transient server-side
    # failure. The message was NOT accepted, so re-POSTing is safe. A 4xx (bad
    # token / bad payload) is permanent — retrying it just wastes time.
    _RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    _RETRY_ATTEMPTS = 3          # total tries (1 initial + 2 retries)
    _RETRY_BASE_DELAY = 1.0      # seconds; exponential backoff (1s, 2s, …)

    def _post_with_retry(self, url: str, payload: Dict, timeout: int = 30,
                         label: str = 'send') -> requests.Response:
        """POST JSON to the Graph API with a short exponential backoff on transient
        failures — a reset/timeout (ECONNRESET, the reported prod error) or a
        429/5xx from Meta. Without this a single transient reset silently drops a
        customer-facing reply, since the caller only logs and the assistant turn is
        already in history (so it never resends). Permanent 4xx errors are returned
        as-is (not retried) so the caller's raise_for_status still surfaces them.

        Note: WhatsApp's messages endpoint isn't idempotent, so a retry after a
        reset that Meta had already accepted can rarely double-send. That's the
        deliberate trade — a rare duplicate beats silently losing the lead."""
        last_exc = None
        for attempt in range(1, self._RETRY_ATTEMPTS + 1):
            try:
                response = requests.post(
                    url, headers=self._headers(), json=payload, timeout=timeout,
                )
                if (response.status_code in self._RETRY_STATUSES
                        and attempt < self._RETRY_ATTEMPTS):
                    wait = self._RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    print(f"⚠️ {label}: HTTP {response.status_code} — "
                          f"retry {attempt}/{self._RETRY_ATTEMPTS - 1} in {wait:g}s")
                    time.sleep(wait)
                    continue
                return response
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                last_exc = e
                if attempt < self._RETRY_ATTEMPTS:
                    wait = self._RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    print(f"⚠️ {label}: {type(e).__name__} — "
                          f"retry {attempt}/{self._RETRY_ATTEMPTS - 1} in {wait:g}s")
                    time.sleep(wait)
                    continue
                raise
        # Loop only falls through here if every try was a retryable status; re-raise
        # the last network error if there was one, else return the final response.
        if last_exc:
            raise last_exc
        return response

    # ─── Text ────────────────────────────────────────────────────────────────

    def send_text_message(self, to: str, message: str) -> Dict:
        """Send a plain text message."""
        from .test_console import is_test_sender, record_outbound
        if is_test_sender(to):
            return record_outbound(to, 'text', text=message)
        try:
            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': self._clean_phone(to),
                'type': 'text',
                'text': {
                    'preview_url': False,
                    'body': message,
                },
            }
            response = self._post_with_retry(
                self._messages_url(), payload, timeout=30, label=f'text→{to}',
            )
            response.raise_for_status()
            result = response.json()
            msg_id = result.get('messages', [{}])[0].get('id', '')
            print(f"✅ Text sent to {to}. ID: {msg_id}")
            return result
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send text to {to}: {e}")
            raise

    # ─── Generic media by URL ────────────────────────────────────────────────

    def send_media_message(
        self,
        to: str,
        media_url: str,
        media_type: str = 'image',
        caption: str = None,
        filename: str = None,
    ) -> Dict:
        """
        Send media using a publicly accessible URL.
        media_type: 'image' | 'video' | 'audio' | 'document'
        """
        from .test_console import is_test_sender, record_outbound
        if is_test_sender(to):
            return record_outbound(
                to, media_type, media_url=media_url, caption=caption, filename=filename
            )
        try:
            media_object = {'link': media_url}
            if caption and media_type in ('image', 'video', 'document'):
                media_object['caption'] = caption
            if filename and media_type == 'document':
                media_object['filename'] = filename

            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': self._clean_phone(to),
                'type': media_type,
                media_type: media_object,
            }
            response = self._post_with_retry(
                self._messages_url(), payload, timeout=60,
                label=f'{media_type}-url→{to}',
            )
            response.raise_for_status()
            print(f"✅ {media_type} sent to {to} via URL")
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send {media_type} to {to}: {e}")
            raise

    # ─── Upload & send by media ID ───────────────────────────────────────────

    def upload_media(self, file_path: str, mime_type: str = None) -> str:
        """
        Upload a local file to WhatsApp and return its media ID.
        Works for images, documents, videos, and audio.
        """
        try:
            if not mime_type:
                ext = Path(file_path).suffix.lower()
                mime_type = EXTENSION_TO_MIME.get(ext)
                if not mime_type:
                    mime_type, _ = mimetypes.guess_type(file_path)
                if not mime_type:
                    mime_type = 'application/octet-stream'

            url = f'{self.base_url}/{self.phone_number_id}/media'
            headers = {'Authorization': f'Bearer {self.access_token}'}

            with open(file_path, 'rb') as f:
                files = {
                    'file': (os.path.basename(file_path), f, mime_type),
                    'messaging_product': (None, 'whatsapp'),
                    'type': (None, mime_type),
                }
                response = requests.post(url, headers=headers, files=files, timeout=120)
                response.raise_for_status()

            media_id = response.json().get('id')
            print(f"✅ Uploaded {os.path.basename(file_path)} → media ID: {media_id}")
            return media_id
        except Exception as e:
            print(f"❌ Upload failed for {file_path}: {e}")
            raise

    def send_media_by_id(
        self,
        to: str,
        media_id: str,
        media_type: str = 'image',
        caption: str = None,
        filename: str = None,
    ) -> Dict:
        """Send media using a previously uploaded media ID."""
        from .test_console import is_test_sender, record_outbound
        if is_test_sender(to):
            return record_outbound(
                to, media_type, media_id=media_id, caption=caption, filename=filename
            )
        try:
            media_object = {'id': media_id}
            if caption and media_type in ('image', 'video', 'document'):
                media_object['caption'] = caption
            if filename and media_type == 'document':
                media_object['filename'] = filename

            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': self._clean_phone(to),
                'type': media_type,
                media_type: media_object,
            }
            response = self._post_with_retry(
                self._messages_url(), payload, timeout=60,
                label=f'{media_type}-id→{to}',
            )
            response.raise_for_status()
            print(f"✅ {media_type} sent to {to} via media ID")
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send {media_type} by ID to {to}: {e}")
            raise

    # ─── Download incoming media ─────────────────────────────────────────────

    def download_media(self, media_id: str) -> bytes:
        """
        Download media sent by a customer.
        Handles images, documents, videos, and audio.
        Always passes auth on both requests (required by WhatsApp).
        """
        try:
            headers = {'Authorization': f'Bearer {self.access_token}'}

            # Step 1: Resolve media_id → download URL
            meta_response = requests.get(
                f'{self.base_url}/{media_id}',
                headers=headers,
                timeout=30,
            )
            meta_response.raise_for_status()
            meta = meta_response.json()

            media_url = meta.get('url')
            if not media_url:
                raise ValueError(f"No download URL returned for media_id={media_id}")

            file_size = meta.get('file_size', 0)
            mime_type = meta.get('mime_type', 'unknown')
            print(f"📥 Downloading media_id={media_id} | type={mime_type} | size={file_size} bytes")

            # Step 2: Download — auth header is mandatory here too
            media_response = requests.get(
                media_url,
                headers=headers,
                timeout=120,   # videos can be large
                stream=True,   # stream to avoid loading huge files into RAM at once
            )
            media_response.raise_for_status()

            content = media_response.content
            print(f"✅ Downloaded {len(content)} bytes (mime={mime_type})")
            return content

        except Exception as e:
            print(f"❌ Failed to download media {media_id}: {e}")
            raise

    def get_media_info(self, media_id: str) -> Dict:
        """
        Get metadata for a media file (URL, MIME type, size) without downloading it.
        Useful for deciding whether to accept/reject before downloading.
        """
        try:
            headers = {'Authorization': f'Bearer {self.access_token}'}
            response = requests.get(
                f'{self.base_url}/{media_id}',
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ Failed to get media info for {media_id}: {e}")
            raise

    # ─── Local file helpers ──────────────────────────────────────────────────

    def send_local_image(self, to: str, image_path: str, caption: str = None) -> Dict:
        """Upload a local image file then send it."""
        from .test_console import is_test_sender, record_outbound
        if is_test_sender(to):
            return record_outbound(to, 'image', media_path=image_path, caption=caption)
        media_id = self.upload_media(image_path)
        return self.send_media_by_id(to, media_id, media_type='image', caption=caption)

    def send_local_video(self, to: str, video_path: str, caption: str = None) -> Dict:
        """Upload a local video file then send it."""
        from .test_console import is_test_sender, record_outbound
        if is_test_sender(to):
            return record_outbound(to, 'video', media_path=video_path, caption=caption)
        media_id = self.upload_media(video_path)
        return self.send_media_by_id(to, media_id, media_type='video', caption=caption)

    def send_local_document(
        self, to: str, doc_path: str, caption: str = None, filename: str = None
    ) -> Dict:
        """Upload a local document then send it."""
        from .test_console import is_test_sender, record_outbound
        if is_test_sender(to):
            return record_outbound(
                to, 'document', media_path=doc_path, caption=caption,
                filename=filename or os.path.basename(doc_path),
            )
        media_id = self.upload_media(doc_path)
        return self.send_media_by_id(
            to, media_id, media_type='document',
            caption=caption,
            filename=filename or os.path.basename(doc_path),
        )

    # ─── Templates & read receipts ───────────────────────────────────────────

    def send_template_message(
        self,
        to: str,
        template_name: str,
        language_code: str = 'en',
        components: List[Dict] = None,
    ) -> Dict:
        """Send a pre-approved template message."""
        try:
            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': self._clean_phone(to),
                'type': 'template',
                'template': {
                    'name': template_name,
                    'language': {'code': language_code},
                },
            }
            if components:
                payload['template']['components'] = components

            response = self._post_with_retry(
                self._messages_url(), payload, timeout=30, label=f'template→{to}',
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send template to {to}: {e}")
            raise

    def mark_message_as_read(self, message_id: str) -> Dict:
        """Mark an incoming message as read (shows blue ticks)."""
        try:
            payload = {
                'messaging_product': 'whatsapp',
                'status': 'read',
                'message_id': message_id,
            }
            response = requests.post(
                self._messages_url(),
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ Failed to mark message as read: {e}")
            raise


# ─── Singleton ───────────────────────────────────────────────────────────────
whatsapp_api = WhatsAppCloudAPI()