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

PREVIOUS_WORK_IMAGE_URLS = [
    url.strip()
    for url in os.environ.get('PREVIOUS_WORK_IMAGE_URLS', '').replace('\n', ',').split(',')
    if url.strip()
]


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


def is_previous_work_photo_request(message: str) -> bool:
    """Detect if customer is asking to see previous work photos."""
    message_lower = message.lower()
    keywords = [
        'picture', 'pictures', 'photo', 'photos', 'image', 'images',
        'previous work', 'past work', 'your work', 'portfolio', 'gallery',
        'show me', 'examples'
    ]
    has_visual_keyword = any(keyword in message_lower for keyword in keywords)
    has_request_intent = any(term in message_lower for term in ['send', 'show', 'see', 'share'])
    return has_visual_keyword and has_request_intent


def send_previous_work_photos(sender, appointment=None) -> bool:
    """
    Send previous-work photos via WhatsApp Cloud API.
    Uses PREVIOUS_WORK_IMAGE_URLS (comma/newline-separated URLs).
    """
    if not PREVIOUS_WORK_IMAGE_URLS:
        return False

    try:
        intro = "Sure, here are some photos of our previous plumbing work."
        whatsapp_api.send_text_message(sender, intro)

        for index, image_url in enumerate(PREVIOUS_WORK_IMAGE_URLS):
            caption = "Previous work example" if index == 0 else None
            whatsapp_api.send_media_message(
                sender,
                image_url,
                media_type='image',
                caption=caption
            )

        if appointment:
            appointment.add_conversation_message("assistant", intro)
            appointment.add_conversation_message(
                "assistant",
                f"[MEDIA] Sent {len(PREVIOUS_WORK_IMAGE_URLS)} previous-work image(s)"
            )

        print(f"✅ Sent {len(PREVIOUS_WORK_IMAGE_URLS)} previous-work image(s) to {sender}")
        return True
    except Exception as e:
        print(f"❌ Failed to send previous-work photos: {str(e)}")
        return False


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
    """Process message change with support for ALL message types"""
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
            
            elif message_type == 'audio':
                handle_audio_message(sender, message.get('audio', {}))
            
            elif message_type == 'video':
                handle_unsupported_media(sender, 'video')
            
            elif message_type == 'sticker':
                handle_unsupported_media(sender, 'sticker')
            
            elif message_type == 'location':
                handle_location_message(sender, message.get('location', {}))
            
            elif message_type == 'contacts':
                handle_unsupported_media(sender, 'contacts')
            
            elif message_type == 'voice':
                handle_audio_message(sender, message.get('voice', {}))
            
            else:
                print(f"⚠️ Unknown message type: {message_type}")
                handle_unsupported_media(sender, message_type)
        
    except Exception as e:
        print(f"❌ Error processing message: {str(e)}")

def handle_location_message(sender, location_data):
    """
    Handle location messages
    Could be useful for getting customer area
    """
    try:
        latitude = location_data.get('latitude')
        longitude = location_data.get('longitude')
        address = location_data.get('address')
        name = location_data.get('name')
        
        print(f"📍 Location from {sender}: {latitude}, {longitude}")
        
        phone_number = f"whatsapp:+{sender}"
        
        try:
            appointment = Appointment.objects.get(phone_number=phone_number)
        except Appointment.DoesNotExist:
            response_msg = "Thanks for the location! To get started, please tell me about your plumbing needs."
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, response_msg, delay),
                daemon=True
            ).start()
            return
        
        # Check if we're asking for area
        from .views import Plumbot
        plumbot = Plumbot(phone_number)
        next_question = plumbot.get_next_question_to_ask()
        
        if next_question == 'area' and not appointment.customer_area:
            # Use location to set area
            if address:
                appointment.customer_area = address
                appointment.save()
                
                response_msg = f"""Perfect! I've got your location: {address}

Let me continue with the next question..."""
                
                # Generate next question
                reply = plumbot.generate_response(f"My location is {address}")
                
                delay = get_random_delay()
                threading.Thread(
                    target=delayed_response,
                    args=(sender, reply, delay),
                    daemon=True
                ).start()
            else:
                # No address, ask for area name
                response_msg = """Thanks for the location pin! 📍

Could you also type the area name? (e.g., Harare Hatfield, Harare Avondale)

This helps us serve you better."""
                
                delay = get_random_delay()
                threading.Thread(
                    target=delayed_response,
                    args=(sender, response_msg, delay),
                    daemon=True
                ).start()
        else:
            # Not asking for area, just acknowledge
            response_msg = """Thanks for sharing your location! 📍

I've noted it. Let me continue with your appointment details..."""
            
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, response_msg, delay),
                daemon=True
            ).start()
        
        print(f"✅ Location handling response scheduled")
        
    except Exception as e:
        print(f"❌ Error handling location: {str(e)}")

def handle_unsupported_media(sender, media_type):
    """
    Handle unsupported media types with friendly message
    """
    try:
        print(f"⚠️ Unsupported media type from {sender}: {media_type}")
        
        # Map media types to friendly names
        media_names = {
            'video': 'video',
            'sticker': 'sticker',
            'contacts': 'contact card',
            'voice': 'voice message',
            'gif': 'GIF'
        }
        
        friendly_name = media_names.get(media_type, media_type)
        
        response_msg = f"""Thanks for the {friendly_name}! 😊

I can't process {friendly_name}s right now, but I work great with:
✅ Text messages
✅ Images (for plans)
✅ PDF documents (for plans)

Could you send that as a text message instead?

Thanks!"""
        
        # Schedule delayed response
        delay = get_random_delay()
        threading.Thread(
            target=delayed_response,
            args=(sender, response_msg, delay),
            daemon=True
        ).start()
        
        print(f"✅ Unsupported media response scheduled")
        
    except Exception as e:
        print(f"❌ Error handling unsupported media: {str(e)}")

def handle_location_message(sender, location_data):
    latitude = location_data.get('latitude')
    longitude = location_data.get('longitude')
    address = location_data.get('address')
    
    # If asking for area, use location
    if next_question == 'area':
        if address:
            appointment.customer_area = address
            appointment.save()
            
            return "Perfect! I've got your location. Let me continue..."
        else:
            return "Thanks for the pin! Could you type the area name too?"

def handle_audio_message(sender, audio_data):
    """
    Handle audio/voice messages
    Currently unsupported but acknowledge politely
    """
    try:
        print(f"🎤 Audio message from {sender}")
        
        phone_number = f"whatsapp:+{sender}"
        
        # Get appointment to check context
        try:
            appointment = Appointment.objects.get(phone_number=phone_number)
        except Appointment.DoesNotExist:
            # New customer sending audio - polite redirect
            response_msg = """Hi there! 👋

I received your voice message, but I work better with text messages.

Could you please type your message instead? That way I can help you book your plumbing appointment more efficiently.

Thanks! 😊"""
            
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, response_msg, delay),
                daemon=True
            ).start()
            return
        
        # Check if we're expecting a plan upload
        if appointment.plan_status == 'pending_upload':
            response_msg = """I see you sent an audio message, but I need images or PDF documents for your plan.

Please send:
📸 Photos of your plan/blueprint
📄 PDF document

Or type "done" if you've finished uploading."""
        
        # Check what question we're on
        else:
            from .views import Plumbot
            plumbot = Plumbot(phone_number)
            next_question = plumbot.get_next_question_to_ask()
            
            if next_question == "complete":
                # Appointment done, just acknowledge
                response_msg = """I got your voice message! 

Your appointment is all set. If you need to make any changes, please type them out so I can help you.

Thanks! 😊"""
            
            elif next_question in ['service_type', 'plan_or_visit', 'area', 'property_type', 'timeline', 'availability', 'name']:
                # In middle of booking - need text
                response_msg = """I received your voice message! 🎤

However, I work better with text messages. Could you please type your response instead?

I'll continue where we left off... 😊"""
            
            else:
                # General acknowledgment
                response_msg = """Thanks for your voice message!

I work better with text though. Could you type that out for me?

I'm here to help! 😊"""
        
        # Schedule delayed response
        delay = get_random_delay()
        threading.Thread(
            target=delayed_response,
            args=(sender, response_msg, delay),
            daemon=True
        ).start()
        
        print(f"✅ Audio handling response scheduled")
        
    except Exception as e:
        print(f"❌ Error handling audio: {str(e)}")


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

        # Handle requests for previous-work pictures immediately
        if is_previous_work_photo_request(message_body):
            photos_sent = send_previous_work_photos(sender, appointment)
            if photos_sent:
                return
            fallback_reply = (
                "I can share previous-work photos, but they are not configured yet. "
                "Please ask our team and we will send them shortly."
            )
            appointment.add_conversation_message("assistant", fallback_reply)
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, fallback_reply, delay),
                daemon=True
            ).start()
            return
        
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
    """Handle media - accept early uploads"""
    try:
        media_id = media_data.get('id')
        phone_number = f"whatsapp:+{sender}"
        
        appointment = Appointment.objects.get(phone_number=phone_number)
        
        # NEW: Accept uploads even if not explicitly in upload flow
        # As long as customer hasn't explicitly said "no plan"
        if appointment.has_plan != False:  # None or True
            
            # Save the media
            save_plan_media(appointment, media_id, media_data)
            
            # Update appointment status
            if appointment.has_plan is None:
                appointment.has_plan = True
                print(f"✅ Auto-detected: Customer has plan (sent early)")
            
            appointment.plan_status = 'pending_upload'  # In case they send more
            appointment.save()
            
            # Acknowledge
            response_msg = """Thanks for sending that! 

I've got your plan. You can send more images if needed, or I'll continue with a few questions."""
            
        else:
            # They said NO to having a plan, this is unexpected
            response_msg = "I see you sent a file, but you mentioned you need a site visit..."
        
        # Send response
        delay = get_random_delay()
        threading.Thread(
            target=delayed_response,
            args=(sender, response_msg, delay),
            daemon=True
        ).start()
        
    except Exception as e:
        print(f"❌ Error handling media: {str(e)}")        
