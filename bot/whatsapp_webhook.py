"""
WhatsApp Cloud API Webhook Handler - ASYNC VERSION
Handles delays without blocking the webhook response
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
import threading
import time
import random


def get_random_delay() -> int:
    """Returns random delay between 1-5 minutes in seconds"""
    minutes = random.randint(1, 5)
    seconds = minutes * 60
    print(f"⏱️ Random delay: {minutes} minute(s)")
    return seconds


def delayed_response(sender, reply, delay_seconds):
    """
    Send response after delay in a background thread
    This prevents webhook timeout
    """
    try:
        print(f"💤 Scheduling response in {delay_seconds // 60} minute(s)...")
        time.sleep(delay_seconds)
        print(f"✅ Delay complete, sending response now")
        whatsapp_api.send_text_message(sender, reply)
        print(f"✅ Response sent to {sender}")
    except Exception as e:
        print(f"❌ Error in delayed response: {str(e)}")


def detect_objection_type(message: str) -> str:
    """Detect customer objection type"""
    message_lower = message.lower()
    
    pricing_keywords = ['how much', 'cost', 'price', 'expensive', 'kuisa', 'mari']
    if any(k in message_lower for k in pricing_keywords):
        return 'pricing'
    
    timeline_keywords = ['how long', 'duration', 'when finish']
    if any(k in message_lower for k in timeline_keywords):
        return 'timeline'
    
    availability_keywords = ['when can you', 'available', 'come']
    if any(k in message_lower for k in availability_keywords):
        return 'availability'
    
    return 'other'


def handle_pricing_objection(appointment) -> str:
    """Handle pricing request with explanation"""
    missing = []
    
    if not appointment.project_type:
        missing.append("which service you need")
    if not appointment.property_type:
        missing.append("your property type")
    if not appointment.customer_area:
        missing.append("your location")
    if appointment.has_plan is None:
        missing.append("whether you have a plan")
    
    if not missing:
        # We have enough info - provide range
        service_ranges = {
            'bathroom_renovation': 'US$1,500 - US$6,000',
            'kitchen_renovation': 'US$3,000 - US$12,000',
            'new_plumbing_installation': 'US$700 - US$8,000'
        }
        
        range_str = service_ranges.get(appointment.project_type, 'US$1,000 - US$15,000')
        
        return f"""Based on your {appointment.project_type.replace('_', ' ')}, typical pricing ranges from {range_str}.

However, the exact cost depends on:
• Specific fixtures and materials you choose
• Size and complexity of the work
• Your exact location ({appointment.customer_area})

For an accurate quote, our plumber will need to {"review your plan" if appointment.has_plan else "do a site visit"}.

Would you like to proceed with booking?"""
    
    # Missing info - explain why we can't price yet
    missing_str = ' and '.join(missing) if len(missing) <= 2 else f"{', '.join(missing[:-1])}, and {missing[-1]}"
    
    return f"""I'd love to give you a price! To provide an accurate quote, I need to know {missing_str}.

Our pricing varies based on your specific project details - every bathroom, kitchen, and plumbing job is unique.

Let me ask you a few quick questions so I can give you the most accurate estimate."""


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    """Handle WhatsApp Cloud API webhook events - ASYNC VERSION"""
    
    if request.method == 'GET':
        return verify_webhook(request)
    elif request.method == 'POST':
        return handle_webhook_event(request)


def verify_webhook(request):
    """Verify webhook during initial setup"""
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
    Handle incoming webhook events
    IMMEDIATELY return 200 OK, process messages in background
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
        
        print(f"📨 Webhook received")
        
        if body.get('object') != 'whatsapp_business_account':
            return HttpResponse(status=200)
        
        # Process messages in background thread - don't block webhook
        threading.Thread(
            target=process_webhook_in_background,
            args=(body,),
            daemon=True
        ).start()
        
        # IMMEDIATELY return 200 OK to WhatsApp
        return HttpResponse(status=200)
        
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in webhook: {str(e)}")
        return HttpResponse(status=400)
    except Exception as e:
        print(f"❌ Webhook processing error: {str(e)}")
        return HttpResponse(status=500)


def process_webhook_in_background(body):
    """
    Process webhook in background thread
    This allows webhook to return immediately
    """
    try:
        for entry in body.get('entry', []):
            for change in entry.get('changes', []):
                if change.get('field') == 'messages':
                    process_message_change(change.get('value', {}))
    except Exception as e:
        print(f"❌ Background processing error: {str(e)}")


def process_message_change(value):
    """Process message change"""
    try:
        messages = value.get('messages', [])
        
        for message in messages:
            message_type = message.get('type')
            message_id = message.get('id')
            sender = message.get('from')
            
            print(f"📬 Processing message from {sender}, type: {message_type}")
            
            # Mark as read immediately
            try:
                whatsapp_api.mark_message_as_read(message_id)
            except Exception as e:
                print(f"⚠️ Could not mark as read: {str(e)}")
            
            # Process based on type
            if message_type == 'text':
                handle_text_message(sender, message.get('text', {}))
            elif message_type == 'image':
                handle_media_message(sender, message.get('image', {}), 'image')
            elif message_type == 'document':
                handle_media_message(sender, message.get('document', {}), 'document')
            else:
                print(f"⚠️ Unsupported message type: {message_type}")
        
    except Exception as e:
        print(f"❌ Error processing message: {str(e)}")


def handle_text_message(sender, text_data):
    """
    Handle text message - SCHEDULES delayed response
    Returns immediately, response sent after delay in background
    """
    try:
        message_body = text_data.get('body', '').strip()
        
        if not message_body:
            return
        
        print(f"💬 Text from {sender}: {message_body}")
        
        # Format phone number
        phone_number = f"whatsapp:+{sender}"
        
        # Get or create appointment
        appointment, created = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )
        
        # ✅ FIX 1: SAVE USER MESSAGE FIRST (before generating response)
        appointment.add_conversation_message("user", message_body)
        print(f"✅ User message saved to conversation history")
        
        # Mark customer response
        appointment.mark_customer_response()
        
        # Check for objections FIRST
        objection_type = detect_objection_type(message_body)
        objection_response = None
        
        if objection_type == 'pricing':
            objection_response = handle_pricing_objection(appointment)
        
        # Generate reply
        if objection_response:
            print(f"🛡️ Handling {objection_type} objection")
            reply = objection_response
        else:
            # Normal Plumbot processing
            from .views import Plumbot
            plumbot = Plumbot(phone_number)
            reply = plumbot.generate_response(message_body)
        
        print(f"🤖 Generated reply: {reply[:100]}...")
        
        # ✅ Save assistant reply to conversation history
        appointment.add_conversation_message("assistant", reply)
        print(f"✅ Assistant reply saved to conversation history")
        
        # ✅ SCHEDULE delayed response in background thread
        delay = get_random_delay()
        threading.Thread(
            target=delayed_response,
            args=(sender, reply, delay),
            daemon=True
        ).start()
        
        print(f"✅ Response scheduled for {delay // 60} minute(s) from now")
        
    except Exception as e:
        print(f"❌ Error handling text: {str(e)}")
        import traceback
        traceback.print_exc()

def handle_media_message(sender, media_data, media_type):
    """Handle media with scheduled delay"""
    try:
        media_id = media_data.get('id')
        mime_type = media_data.get('mime_type')
        
        print(f"📎 {media_type} from {sender}, ID: {media_id}")
        
        phone_number = f"whatsapp:+{sender}"
        
        try:
            appointment = Appointment.objects.get(phone_number=phone_number)
        except Appointment.DoesNotExist:
            print(f"❌ No appointment for {phone_number}")
            
            # Schedule delayed error message
            error_msg = "I don't have an active appointment for this number. Please start by telling me about your plumbing needs."
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, error_msg, delay),
                daemon=True
            ).start()
            return
        
        # Check if expecting media
        if appointment.plan_status != 'pending_upload':
            print(f"ℹ️ Not in upload flow. Status: {appointment.plan_status}")
            
            # Schedule delayed response
            response_msg = "I see you sent a file, but I'm not currently expecting any documents. Let me continue with your appointment details."
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, response_msg, delay),
                daemon=True
            ).start()
            return
        
        # Download and save media
        try:
            media_content = whatsapp_api.download_media(media_id)
            
            extension_map = {
                'image/jpeg': '.jpg',
                'image/png': '.png',
                'image/webp': '.webp',
                'application/pdf': '.pdf'
            }
            
            extension = extension_map.get(mime_type, '.bin')
            timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')
            customer_name = appointment.customer_name or 'customer'
            safe_name = ''.join(c for c in customer_name if c.isalnum())
            filename = f"plan_{safe_name}_{appointment.id}_{timestamp}{extension}"
            
            file_path = f"customer_plans/{filename}"
            file_content = ContentFile(media_content, name=filename)
            saved_path = default_storage.save(file_path, file_content)
            
            if not appointment.plan_file:
                appointment.plan_file = saved_path
            appointment.plan_uploaded_at = timezone.now()
            appointment.save()
            
            print(f"✅ Saved: {saved_path}")
            
            # Generate acknowledgment
            from .views import Plumbot
            plumbot = Plumbot(phone_number)
            ack_message = plumbot.handle_plan_upload_flow("file received")
            
            # Schedule delayed acknowledgment
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, ack_message, delay),
                daemon=True
            ).start()
            
        except Exception as download_error:
            print(f"❌ Error with media: {str(download_error)}")
            
            # Schedule delayed error message
            error_msg = "I had trouble processing that file. Could you try sending it again?"
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, error_msg, delay),
                daemon=True
            ).start()
        
    except Exception as e:
        print(f"❌ Error handling media: {str(e)}")