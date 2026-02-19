"""
WhatsApp Cloud API Integration Module
Supports text, images, documents, audio, and video
"""

import requests
import json
import os
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

    # ─── Text ────────────────────────────────────────────────────────────────

    def send_text_message(self, to: str, message: str) -> Dict:
        """Send a plain text message."""
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
            response = requests.post(
                self._messages_url(),
                headers=self._headers(),
                json=payload,
                timeout=30,
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
            response = requests.post(
                self._messages_url(),
                headers=self._headers(),
                json=payload,
                timeout=60,
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
            response = requests.post(
                self._messages_url(),
                headers=self._headers(),
                json=payload,
                timeout=60,
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
        media_id = self.upload_media(image_path)
        return self.send_media_by_id(to, media_id, media_type='image', caption=caption)

    def send_local_video(self, to: str, video_path: str, caption: str = None) -> Dict:
        """Upload a local video file then send it."""
        media_id = self.upload_media(video_path)
        return self.send_media_by_id(to, media_id, media_type='video', caption=caption)

    def send_local_document(
        self, to: str, doc_path: str, caption: str = None, filename: str = None
    ) -> Dict:
        """Upload a local document then send it."""
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

            response = requests.post(
                self._messages_url(),
                headers=self._headers(),
                json=payload,
                timeout=30,
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