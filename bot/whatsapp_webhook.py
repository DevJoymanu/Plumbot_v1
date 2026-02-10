"""
WhatsApp Cloud API Webhook Handler
Replaces Twilio webhook handling
"""

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import json
import os
from .whatsapp_cloud_api import whatsapp_api
from .models import Appointment
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    """
    Handle WhatsApp Cloud API webhook events
    GET: Webhook verification
    POST: Incoming messages and status updates
    """
    
    if request.method == 'GET':
        return verify_webhook(request)
    elif request.method == 'POST':
        return handle_webhook_event(request)


def verify_webhook(request):
    """
    Verify webhook during initial setup
    WhatsApp sends: hub.mode, hub.verify_token, hub.challenge
    """
    try:
        mode = request.GET.get('hub.mode')
        token = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge')
        
        verify_token = os.environ.get('WHATSAPP_VERIFY_TOKEN', 'your_verify_token_here')
        
        if mode == 'subscribe' and token == verify_token:
            print(f"✅ Webhook verified successfully")
            return HttpResponse(challenge, content_type='text/plain')
        else:
            print(f"❌ Webhook verification failed")
            return HttpResponse(status=403)
            
    except Exception as e:
        print(f"❌ Webhook verification error: {str(e)}")
        return HttpResponse(status=500)


def handle_webhook_event(request):
    """
    Handle incoming webhook events from WhatsApp Cloud API
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
        
        # Log the incoming webhook
        print(f"📨 Webhook received: {json.dumps(body, indent=2)}")
        
        # WhatsApp Cloud API sends events in this structure
        if body.get('object') != 'whatsapp_business_account':
            return HttpResponse(status=200)
        
        # Process each entry
        for entry in body.get('entry', []):
            for change in entry.get('changes', []):
                if change.get('field') == 'messages':
                    process_message_change(change.get('value', {}))
        
        return HttpResponse(status=200)
        
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in webhook: {str(e)}")
        return HttpResponse(status=400)
    except Exception as e:
        print(f"❌ Webhook processing error: {str(e)}")
        return HttpResponse(status=500)


def process_message_change(value):
    """
    Process a message change event
    
    Args:
        value: The 'value' object from the webhook containing messages and metadata
    """
    try:
        # Get messages array
        messages = value.get('messages', [])
        
        for message in messages:
            message_type = message.get('type')
            message_id = message.get('id')
            sender = message.get('from')  # Phone number without + or whatsapp:
            timestamp = message.get('timestamp')
            
            print(f"📬 Processing message from {sender}, type: {message_type}")
            
            # Mark message as read
            try:
                whatsapp_api.mark_message_as_read(message_id)
            except Exception as e:
                print(f"⚠️ Could not mark message as read: {str(e)}")
            
            # Process based on message type
            if message_type == 'text':
                handle_text_message(sender, message.get('text', {}))
            
            elif message_type == 'image':
                handle_media_message(sender, message.get('image', {}), 'image')
            
            elif message_type == 'document':
                handle_media_message(sender, message.get('document', {}), 'document')
            
            elif message_type == 'audio':
                handle_media_message(sender, message.get('audio', {}), 'audio')
            
            elif message_type == 'video':
                handle_media_message(sender, message.get('video', {}), 'video')
            
            elif message_type == 'button':
                handle_button_response(sender, message.get('button', {}))
            
            elif message_type == 'interactive':
                handle_interactive_response(sender, message.get('interactive', {}))
            
            else:
                print(f"⚠️ Unsupported message type: {message_type}")
        
    except Exception as e:
        print(f"❌ Error processing message change: {str(e)}")
        import traceback
        traceback.print_exc()


def handle_text_message(sender, text_data):
    """
    Handle incoming text message
    
    Args:
        sender: Phone number of the sender (without + or whatsapp: prefix)
        text_data: Text message data containing 'body'
    """
    try:
        message_body = text_data.get('body', '').strip()
        
        if not message_body:
            return
        
        print(f"💬 Text message from {sender}: {message_body}")
        
        # Format phone number for compatibility (add whatsapp: prefix for existing code)
        phone_number = f"whatsapp:+{sender}"
        
        # Import here to avoid circular imports
        from .views import Plumbot
        
        # Initialize Plumbot and generate response
        plumbot = Plumbot(phone_number)
        reply = plumbot.generate_response(message_body)
        
        print(f"🤖 Generated reply: {reply}")
        
        # Send reply using WhatsApp Cloud API (remove whatsapp: prefix)
        whatsapp_api.send_text_message(sender, reply)
        
    except Exception as e:
        print(f"❌ Error handling text message: {str(e)}")
        import traceback
        traceback.print_exc()


def handle_media_message(sender, media_data, media_type):
    """
    Handle incoming media message (image, document, video, audio)
    
    Args:
        sender: Phone number of the sender
        media_data: Media message data containing 'id', 'mime_type', etc.
        media_type: Type of media (image, document, video, audio)
    """
    try:
        media_id = media_data.get('id')
        mime_type = media_data.get('mime_type')
        caption = media_data.get('caption', '')
        
        print(f"📎 {media_type.capitalize()} from {sender}, ID: {media_id}")
        
        # Format phone number for compatibility
        phone_number = f"whatsapp:+{sender}"
        
        # Get or create appointment
        try:
            appointment = Appointment.objects.get(phone_number=phone_number)
        except Appointment.DoesNotExist:
            print(f"❌ No appointment found for {phone_number}")
            whatsapp_api.send_text_message(
                sender,
                "I don't have an active appointment for this number. Please start by telling me about your plumbing needs."
            )
            return
        
        # Check if we should accept media
        if appointment.plan_status != 'pending_upload':
            print(f"ℹ️ Ignoring media - not in upload flow. Status: {appointment.plan_status}")
            
            response_msg = "I see you sent a file, but I'm not currently expecting any documents. Let me continue with your appointment details."
            whatsapp_api.send_text_message(sender, response_msg)
            return
        
        # Download and save the media
        try:
            media_content = whatsapp_api.download_media(media_id)
            
            # Generate filename
            extension_map = {
                'image/jpeg': '.jpg',
                'image/png': '.png',
                'image/webp': '.webp',
                'application/pdf': '.pdf',
                'image/gif': '.gif',
                'video/mp4': '.mp4',
                'audio/ogg': '.ogg'
            }
            
            extension = extension_map.get(mime_type, '.bin')
            timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
            customer_name = appointment.customer_name or 'customer'
            safe_name = ''.join(c for c in customer_name if c.isalnum())
            filename = f"plan_{safe_name}_{appointment.id}_{timestamp}{extension}"
            
            # Save file
            file_path = f"customer_plans/{filename}"
            file_content = ContentFile(media_content, name=filename)
            saved_path = default_storage.save(file_path, file_content)
            
            # Update appointment
            if not appointment.plan_file:
                appointment.plan_file = saved_path
            appointment.plan_uploaded_at = timezone.now()
            appointment.save()
            
            print(f"✅ Saved media file: {saved_path}")
            
            # Send acknowledgment
            from .views import Plumbot
            plumbot = Plumbot(phone_number)
            ack_message = plumbot.handle_plan_upload_flow("file received")
            
            whatsapp_api.send_text_message(sender, ack_message)
            
        except Exception as download_error:
            print(f"❌ Error downloading/saving media: {str(download_error)}")
            whatsapp_api.send_text_message(
                sender,
                "I had trouble processing that file. Could you try sending it again?"
            )
        
    except Exception as e:
        print(f"❌ Error handling media message: {str(e)}")
        import traceback
        traceback.print_exc()


def handle_button_response(sender, button_data):
    """
    Handle button reply
    
    Args:
        sender: Phone number of the sender
        button_data: Button response data
    """
    try:
        button_text = button_data.get('text', '')
        button_payload = button_data.get('payload', '')
        
        print(f"🔘 Button response from {sender}: {button_text}")
        
        # Treat as regular text message
        handle_text_message(sender, {'body': button_text})
        
    except Exception as e:
        print(f"❌ Error handling button response: {str(e)}")


def handle_interactive_response(sender, interactive_data):
    """
    Handle interactive message response (list, button)
    
    Args:
        sender: Phone number of the sender
        interactive_data: Interactive response data
    """
    try:
        interactive_type = interactive_data.get('type')
        
        if interactive_type == 'button_reply':
            button_reply = interactive_data.get('button_reply', {})
            response_text = button_reply.get('title', '')
            
        elif interactive_type == 'list_reply':
            list_reply = interactive_data.get('list_reply', {})
            response_text = list_reply.get('title', '')
        
        else:
            print(f"⚠️ Unknown interactive type: {interactive_type}")
            return
        
        print(f"🎯 Interactive response from {sender}: {response_text}")
        
        # Treat as regular text message
        handle_text_message(sender, {'body': response_text})
        
    except Exception as e:
        print(f"❌ Error handling interactive response: {str(e)}")


def handle_status_update(value):
    """
    Handle message status updates (sent, delivered, read, failed)
    
    Args:
        value: The 'value' object containing status information
    """
    try:
        statuses = value.get('statuses', [])
        
        for status in statuses:
            message_id = status.get('id')
            recipient = status.get('recipient_id')
            status_type = status.get('status')
            timestamp = status.get('timestamp')
            
            print(f"📊 Message {message_id} to {recipient}: {status_type}")
            
            # You can log these status updates to your database if needed
            # For example, track delivery success rates
            
    except Exception as e:
        print(f"❌ Error handling status update: {str(e)}")