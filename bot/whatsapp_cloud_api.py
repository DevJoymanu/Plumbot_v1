"""
WhatsApp Cloud API Integration Module
This replaces Twilio for WhatsApp messaging
"""

import requests
import json
import os
from typing import Dict, Optional, List
import mimetypes
from django.core.files.storage import default_storage
from django.utils import timezone


class WhatsAppCloudAPI:
    """
    WhatsApp Cloud API client for sending messages and handling media
    """
    
    def __init__(self):
        self.access_token = os.environ.get('WHATSAPP_ACCESS_TOKEN')
        self.phone_number_id = os.environ.get('WHATSAPP_PHONE_NUMBER_ID')
        self.business_account_id = os.environ.get('WHATSAPP_BUSINESS_ACCOUNT_ID')
        self.verify_token = os.environ.get('WHATSAPP_VERIFY_TOKEN', 'your_verify_token_here')
        self.api_version = 'v21.0'
        self.base_url = f'https://graph.facebook.com/{self.api_version}'
        
    def send_text_message(self, to: str, message: str) -> Dict:
        """
        Send a text message via WhatsApp Cloud API
        
        Args:
            to: Recipient phone number (format: 27610318200, no + or whatsapp: prefix)
            message: Text message to send
            
        Returns:
            API response dict
        """
        try:
            # Clean phone number - remove whatsapp: prefix and + if present
            to_clean = to.replace('whatsapp:', '').replace('+', '').strip()
            
            url = f'{self.base_url}/{self.phone_number_id}/messages'
            
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': to_clean,
                'type': 'text',
                'text': {
                    'preview_url': False,
                    'body': message
                }
            }
            
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            result = response.json()
            print(f"✅ Message sent to {to_clean}. Message ID: {result.get('messages', [{}])[0].get('id')}")
            
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send message to {to}: {str(e)}")
            if hasattr(e.response, 'text'):
                print(f"Error details: {e.response.text}")
            raise
    
    def send_media_message(self, to: str, media_url: str, media_type: str = 'image', 
                          caption: str = None) -> Dict:
        """
        Send a media message (image, document, video, audio)
        
        Args:
            to: Recipient phone number
            media_url: URL of the media file (must be publicly accessible)
            media_type: Type of media (image, document, video, audio)
            caption: Optional caption for the media
            
        Returns:
            API response dict
        """
        try:
            to_clean = to.replace('whatsapp:', '').replace('+', '').strip()
            
            url = f'{self.base_url}/{self.phone_number_id}/messages'
            
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            }
            
            media_object = {
                'link': media_url
            }
            
            if caption and media_type in ['image', 'video', 'document']:
                media_object['caption'] = caption
            
            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': to_clean,
                'type': media_type,
                media_type: media_object
            }
            
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send media to {to}: {str(e)}")
            raise
    
    def send_template_message(self, to: str, template_name: str, 
                             language_code: str = 'en', 
                             components: List[Dict] = None) -> Dict:
        """
        Send a template message (pre-approved messages)
        
        Args:
            to: Recipient phone number
            template_name: Name of the approved template
            language_code: Language code (e.g., 'en', 'en_US')
            components: Template components (parameters, buttons, etc.)
            
        Returns:
            API response dict
        """
        try:
            to_clean = to.replace('whatsapp:', '').replace('+', '').strip()
            
            url = f'{self.base_url}/{self.phone_number_id}/messages'
            
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': to_clean,
                'type': 'template',
                'template': {
                    'name': template_name,
                    'language': {
                        'code': language_code
                    }
                }
            }
            
            if components:
                payload['template']['components'] = components
            
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send template to {to}: {str(e)}")
            raise
    
    def upload_media(self, file_path: str, mime_type: str = None) -> str:
        """
        Upload media to WhatsApp Cloud API and get media ID
        
        Args:
            file_path: Path to the file to upload
            mime_type: MIME type of the file (auto-detected if not provided)
            
        Returns:
            Media ID that can be used to send the media
        """
        try:
            url = f'{self.base_url}/{self.phone_number_id}/media'
            
            headers = {
                'Authorization': f'Bearer {self.access_token}'
            }
            
            # Auto-detect MIME type if not provided
            if not mime_type:
                mime_type, _ = mimetypes.guess_type(file_path)
                if not mime_type:
                    mime_type = 'application/octet-stream'
            
            # Read file
            with open(file_path, 'rb') as f:
                files = {
                    'file': (os.path.basename(file_path), f, mime_type),
                    'messaging_product': (None, 'whatsapp'),
                    'type': (None, mime_type)
                }
                
                response = requests.post(url, headers=headers, files=files)
                response.raise_for_status()
            
            result = response.json()
            media_id = result.get('id')
            
            print(f"✅ Media uploaded. ID: {media_id}")
            return media_id
            
        except Exception as e:
            print(f"❌ Failed to upload media: {str(e)}")
            raise
    
    def send_media_by_id(self, to: str, media_id: str, media_type: str = 'image',
                        caption: str = None) -> Dict:
        """
        Send media using a previously uploaded media ID
        
        Args:
            to: Recipient phone number
            media_id: Media ID from upload_media()
            media_type: Type of media (image, document, video, audio)
            caption: Optional caption
            
        Returns:
            API response dict
        """
        try:
            to_clean = to.replace('whatsapp:', '').replace('+', '').strip()
            
            url = f'{self.base_url}/{self.phone_number_id}/messages'
            
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            }
            
            media_object = {
                'id': media_id
            }
            
            if caption and media_type in ['image', 'video', 'document']:
                media_object['caption'] = caption
            
            payload = {
                'messaging_product': 'whatsapp',
                'recipient_type': 'individual',
                'to': to_clean,
                'type': media_type,
                media_type: media_object
            }
            
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send media by ID to {to}: {str(e)}")
            raise
    
    def download_media(self, media_id: str) -> bytes:
        """
        Download media from WhatsApp Cloud API
        
        Args:
            media_id: Media ID from incoming message
            
        Returns:
            Media file content as bytes
        """
        try:
            # First, get the media URL
            url = f'{self.base_url}/{media_id}'
            
            headers = {
                'Authorization': f'Bearer {self.access_token}'
            }
            
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            
            media_info = response.json()
            media_url = media_info.get('url')
            
            if not media_url:
                raise ValueError("No media URL in response")
            
            # Download the actual media file
            media_response = requests.get(media_url, headers=headers)
            media_response.raise_for_status()
            
            print(f"✅ Media downloaded. Size: {len(media_response.content)} bytes")
            return media_response.content
            
        except Exception as e:
            print(f"❌ Failed to download media {media_id}: {str(e)}")
            raise
    
    def mark_message_as_read(self, message_id: str) -> Dict:
        """
        Mark a message as read
        
        Args:
            message_id: ID of the message to mark as read
            
        Returns:
            API response dict
        """
        try:
            url = f'{self.base_url}/{self.phone_number_id}/messages'
            
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'messaging_product': 'whatsapp',
                'status': 'read',
                'message_id': message_id
            }
            
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            
            return response.json()
            
        except Exception as e:
            print(f"❌ Failed to mark message as read: {str(e)}")
            raise


# Singleton instance
whatsapp_api = WhatsAppCloudAPI()