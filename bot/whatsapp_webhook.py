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
from pathlib import Path

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
    """Use DeepSeek AI to detect if customer is asking to see previous work photos - including Shona."""
    try:
        from openai import OpenAI
        import os
        
        deepseek_client = OpenAI(
            api_key=os.environ.get('DEEPSEEK_API_KEY'),
            base_url="https://api.deepseek.com/v1"
        )
        
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": "You are a message intent classifier for a Zimbabwean plumbing company. Customers may write in English, Shona, or a mix of both. Reply with ONLY 'YES' or 'NO', nothing else."
                },
                {
                    "role": "user",
                    "content": f"""Is the customer asking to see photos, pictures, images, or examples of previous/past plumbing work?

Consider English expressions like:
- "send me photos", "show me your work", "do you have pictures", "portfolio", "examples"

Consider Shona expressions like:
- "ndiratidze mifananidzo" (show me pictures)
- "une mifananidzo here" (do you have pictures)
- "ndiona basa renyu" (let me see your work)
- "tumira mifananidzo" (send pictures)
- "ratidza basa renyu" (show your work)
- "mifananidzo yebasa renyu" (pictures of your work)
- "ndione zvamakamboita" (let me see what you've done before)
- "mufananidzo" (picture/image)
- "basa renyu" (your work)

Also consider:
- Mixed Shona/English: "send mifananidzo", "show me basa renyu"
- Informal spelling and typos in either language

Customer message: "{message}"

Reply YES or NO only."""
                }
            ],
            temperature=0.1,
            max_tokens=5
        )
        
        result = response.choices[0].message.content.strip().upper()
        is_request = result == "YES"
        
        print(f"🤖 DeepSeek photo request detection: '{message}' → {result}")
        return is_request
        
    except Exception as e:
        print(f"❌ DeepSeek photo detection error: {str(e)}, falling back to keyword check")
        message_lower = message.lower()
        fallback_keywords = [
            # English
            'picture', 'photo', 'image', 'previous work', 'portfolio', 'show me', 'your work',
            # Shona
            'mifananidzo', 'mufananidzo', 'ratidza', 'ndiratidze', 'basa renyu', 'ndiona', 'ndione', 'tumira'
        ]
        return any(kw in message_lower for kw in fallback_keywords)
        
# Put your images in a folder like: bot/static/previous_work/
# Or anywhere on the server - just update PREVIOUS_WORK_IMAGES_DIR

PREVIOUS_WORK_IMAGES_DIR = os.environ.get(
    'PREVIOUS_WORK_IMAGES_DIR',
    os.path.join(os.path.dirname(__file__), 'previous_work_photos')
)

SUPPORTED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}


def get_previous_work_images() -> list:
    """Get list of image file paths from the previous work folder"""
    images = []
    
    if not os.path.exists(PREVIOUS_WORK_IMAGES_DIR):
        print(f"⚠️ Previous work images folder not found: {PREVIOUS_WORK_IMAGES_DIR}")
        return images
    
    for filename in sorted(os.listdir(PREVIOUS_WORK_IMAGES_DIR)):
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_IMAGE_EXTENSIONS:
            full_path = os.path.join(PREVIOUS_WORK_IMAGES_DIR, filename)
            images.append(full_path)
    
    print(f"📸 Found {len(images)} previous work images")
    return images


def send_previous_work_photos(sender, appointment=None):
    """
    Send previous work photos with a small delay between each image,
    after an initial random delay, to simulate human-like sending.
    """
    images = get_previous_work_images()
    
    if not images:
        print("⚠️ No previous work images found")
        return False

    try:
        # Compose initial message
        intro = "Here are some examples of our previous plumbing work! 🔧✨"
        
        def send_images_with_delay():
            try:
                # Random delay before starting
                delay_seconds = get_random_delay()
                print(f"💤 Waiting {delay_seconds // 60} minute(s) before sending images to {sender}")
                time.sleep(delay_seconds)

                # Send intro text
                whatsapp_api.send_text_message(sender, intro)
                
                sent_count = 0
                # Send images one by one with 0.5s gap
                for index, image_path in enumerate(images):
                    caption = "Our previous work - high quality plumbing & renovations" if index == 0 else None
                    whatsapp_api.send_local_image(sender, image_path, caption=caption)
                    sent_count += 1
                    time.sleep(0.5)  # small delay between images

                # Follow-up message after all images
                follow_up = "Would you like to book an appointment? Just tell me what service you need! 😊"
                time.sleep(1)  # slight pause before follow-up
                whatsapp_api.send_text_message(sender, follow_up)

                # Save to conversation history
                if appointment:
                    appointment.add_conversation_message("assistant", intro)
                    appointment.add_conversation_message(
                        "assistant", f"[MEDIA] Sent {sent_count} previous work image(s)"
                    )
                    appointment.add_conversation_message("assistant", follow_up)

                print(f"✅ Sent {sent_count}/{len(images)} previous work images to {sender}")

            except Exception as e:
                print(f"❌ Failed to send images: {str(e)}")

        # Run in background thread so webhook is not blocked
        threading.Thread(target=send_images_with_delay, daemon=True).start()

        return True

    except Exception as e:
        print(f"❌ Error preparing previous work images: {str(e)}")
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
    try:
        message_body = text_data.get('body', '').strip()
        if not message_body:
            return

        print(f"💬 Text from {sender}: {message_body}")

        phone_number = f"whatsapp:+{sender}"

        appointment, created = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )

        # Save user message first
        appointment.add_conversation_message("user", message_body)
        print(f"✅ User message saved to conversation history")

        # Mark customer response
        appointment.mark_customer_response()

        # ✅ STEP 1: Check for previous work photo request
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

        # ✅ STEP 2: Check for service inquiry BEFORE pricing objection
        # This ensures "How much is standalone tub" hits the right handler
        from .views import Plumbot
        plumbot = Plumbot(phone_number)

        mid_conversation = (
            appointment.project_type is not None and
            (
                appointment.has_plan is not None or
                appointment.customer_area is not None or
                appointment.property_type is not None
            )
        )

        reply = None

        if not mid_conversation:
            inquiry = plumbot.detect_service_inquiry(message_body)
            print(f"🔍 Service inquiry check: {inquiry}")

            if inquiry.get('intent') != 'none' and inquiry.get('confidence') == 'HIGH':
                print(f"💡 Handling service inquiry: {inquiry['intent']}")
                reply = plumbot.handle_service_inquiry(inquiry['intent'], message_body)

        # ✅ STEP 3: Only check pricing objection if no service inquiry matched
        if reply is None:
            objection_type = detect_objection_type(message_body)

            if objection_type == 'pricing':
                print(f"🛡️ Handling generic pricing objection")
                reply = handle_pricing_objection(appointment)

        # ✅ STEP 4: Fall through to normal Plumbot processing
        if reply is None:
            reply = plumbot.generate_response(message_body)

        print(f"🤖 Generated reply: {reply[:100]}...")

        # Save assistant reply
        appointment.add_conversation_message("assistant", reply)
        print(f"✅ Assistant reply saved to conversation history")

        # Schedule delayed response
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
    """Handle ANY media sent at ANY point - alert plumber immediately."""
    try:
        media_id = media_data.get('id')
        phone_number = f"whatsapp:+{sender}"

        appointment, created = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )

        customer_name = appointment.customer_name or "A customer"
        plumber_number = getattr(appointment, 'plumber_contact_number', None) or '27610318200'
        plumber_number = plumber_number.replace('+', '').replace('whatsapp:', '')

        service = appointment.project_type or 'Not specified'
        area = appointment.customer_area or 'Not specified'
        has_plan = appointment.has_plan
        property_type = appointment.property_type or 'Not specified'
        timeline = appointment.timeline or 'Not specified'
        status = appointment.get_status_display() if hasattr(appointment, 'get_status_display') else appointment.status

        # Generate AI conversation summary
        ai_summary = generate_conversation_summary(appointment)
        
        alert_message = (
                    f"📎 MEDIA RECEIVED FROM CUSTOMER\n\n"
                    f"Customer: {customer_name}\n"
                    f"Phone: +{sender}\n"
                    f"WhatsApp: wa.me/{sender}\n"
                    f"Media type: {media_type.upper()}\n\n"
                    f"📋 APPOINTMENT DETAILS:\n"
                    f"  Service: {service}\n"
                    f"  Area: {area}\n"
                    f"  Property: {property_type}\n"
                    f"  Timeline: {timeline}\n"
                    f"  Status: {status}\n"
                    f"  Has plan: {'Yes' if has_plan is True else 'No' if has_plan is False else 'Not answered'}\n\n"
                    f"🤖 AI CONVERSATION SUMMARY:\n{ai_summary}\n\n"
                    f"🔗 View full appointment:\n"
                    f"https://plumbotv1-production.up.railway.app/appointments/{appointment.id}/\n\n"
                    f"Please contact the customer directly to assist them."
                )
        # Alert plumber
        try:
            whatsapp_api.send_text_message(plumber_number, alert_message)
            print(f"✅ Plumber alerted about {media_type} from {sender}")
        except Exception as e:
            print(f"❌ Failed to alert plumber: {str(e)}")

        # Update appointment plan status based on context
        if media_type in ['image', 'document']:
            if has_plan is None:
                appointment.has_plan = True
                print(f"✅ Auto-set has_plan=True (customer sent media)")
            appointment.plan_status = 'received'
            appointment.save()

        # Tell customer
        customer_reply = (
            "Thank you for sending that! 📎 Our plumber has been notified and will be "
            "in touch with you directly. If it's urgent, you can also call them on "
            f"{appointment.plumber_contact_number or '+27610318200'}."
        )

        appointment.add_conversation_message("user", f"[Sent {media_type}]")
        appointment.add_conversation_message("assistant", customer_reply)

        delay = get_random_delay()
        threading.Thread(
            target=delayed_response,
            args=(sender, customer_reply, delay),
            daemon=True
        ).start()

    except Exception as e:
        print(f"❌ Error handling media: {str(e)}")


def generate_conversation_summary(appointment) -> str:
    """
    Use DeepSeek AI to generate a concise summary of the conversation
    for the plumber alert message.
    """
    try:
        if not appointment.conversation_history:
            return "No conversation history available."

        # Build conversation transcript (last 20 messages to stay within token limits)
        recent_messages = appointment.conversation_history[-20:]
        transcript_lines = []
        for msg in recent_messages:
            role = msg.get('role', '')
            content = msg.get('content', '').strip()

            # Skip empty messages or system tags
            if not content or content.startswith('[Sent '):
                continue

            # Clean up tags
            content = (
                content
                .replace('[AUTOMATIC FOLLOW-UP] ', '')
                .replace('[MANUAL FOLLOW-UP] ', '')
                .replace('[BULK MANUAL FOLLOW-UP] ', '')
            )

            label = "Customer" if role == 'user' else "Bot"
            transcript_lines.append(f"{label}: {content[:300]}")

        if not transcript_lines:
            return "No meaningful conversation history found."

        transcript = "\n".join(transcript_lines)

        # Call DeepSeek AI
        from openai import OpenAI
        import os

        deepseek_client = OpenAI(
            api_key=os.environ.get('DEEPSEEK_API_KEY'),
            base_url="https://api.deepseek.com/v1"
        )

        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful assistant summarising WhatsApp conversations "
                        "between a plumbing company's chatbot and a customer. "
                        "Your summary will be sent to a plumber so they know exactly "
                        "what the customer needs before calling them. "
                        "Be concise, factual, and highlight anything actionable."
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"Please summarise this conversation in 3-5 bullet points. "
                        f"Focus on: what the customer wants, key details they shared, "
                        f"any concerns or questions they raised, and what the next step should be.\n\n"
                        f"CONVERSATION:\n{transcript}"
                    )
                }
            ],
            temperature=0.3,
            max_tokens=300
        )

        summary = response.choices[0].message.content.strip()
        print(f"✅ AI conversation summary generated")
        return summary

    except Exception as e:
        print(f"❌ AI summary generation failed: {str(e)}")

        # Fallback: return last 3 messages as plain text
        try:
            fallback_lines = []
            for msg in appointment.conversation_history[-3:]:
                role = "Customer" if msg.get('role') == 'user' else "Bot"
                content = msg.get('content', '')[:150]
                fallback_lines.append(f"{role}: {content}")
            return "Summary unavailable. Last messages:\n" + "\n".join(fallback_lines)
        except Exception:
            return "Summary unavailable."