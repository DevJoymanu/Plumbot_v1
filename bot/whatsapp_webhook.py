"""
WhatsApp Cloud API Webhook Handler - ASYNC VERSION
Handles delays without blocking the webhook response

FIXES IN THIS VERSION:
1. Service-level pricing dedup  — each intent (toilet, geyser, etc.) sent once per lead
2. Pricing overview dedup       — full price list blocked if a specific intent was already sent
3. Previous-work photo dedup    — fallback text never fires when photos were actually queued
4. Confirmation message dedup   — book_appointment_with_selected_time no longer double-sends
5. Plan question dedup          — helper guards re-ask of plan_or_visit
"""
from django.db.models import Value
from django.db.models.functions import Concat
from django.db.models.functions import Replace
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import json
import os
from .whatsapp_cloud_api import whatsapp_api, get_extension_for_mime, MEDIA_SIZE_LIMITS
from .models import Appointment, WhatsAppInboundEvent, LeadStatus
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError
import threading
import time
import random
from pathlib import Path
from .services.lead_scoring import refresh_lead_score
from typing import Optional


PREVIOUS_WORK_IMAGE_URLS = [
    url.strip()
    for url in os.environ.get('PREVIOUS_WORK_IMAGE_URLS', '').replace('\n', ',').split(',')
    if url.strip()
]

# ─── Media debounce trackers ──────────────────────────────────────────────────
_media_ack_timers: dict = {}
_media_ack_lock = threading.Lock()
MEDIA_DEBOUNCE_SECONDS = 8

# Plumber alert debounce — accumulates file URLs across a burst of images,
# then sends ONE consolidated alert after the burst window closes.
_plumber_alert_timers: dict = {}          # sender → threading.Timer
_plumber_alert_pending: dict = {}         # sender → list of file_url strings
_plumber_alert_lock = threading.Lock()

# Text dedupe window to suppress near-identical duplicate webhook deliveries.
_text_dedupe_lock = threading.Lock()
_recent_text_events: dict = {}  # key=(sender, normalized_text) -> monotonic timestamp
TEXT_DEDUPE_WINDOW_SECONDS = 20


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1 — SERVICE-LEVEL PRICING DEDUP
# ─────────────────────────────────────────────────────────────────────────────

def _has_sent_pricing_for_intent(appointment: Appointment, intent: str) -> bool:
    """Return True if we already sent pricing for this specific intent."""
    sent = appointment.sent_pricing_intents or []
    return intent in sent


def _mark_pricing_intent_sent(appointment: Appointment, intent: str) -> None:
    """Record that we sent pricing for this intent so we never repeat it."""
    sent = list(appointment.sent_pricing_intents or [])
    if intent not in sent:
        sent.append(intent)
        appointment.sent_pricing_intents = sent
        appointment.save(update_fields=['sent_pricing_intents'])


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2 — PRICING OVERVIEW DEDUP (also blocks if any specific intent was sent)
# ─────────────────────────────────────────────────────────────────────────────

def _is_genuine_pricing_question(message: str, appointment: Appointment) -> bool:
    """
    Return True ONLY when the message is a fresh, standalone pricing inquiry.

    Blocks the pricing response when:
    - We already sent the full pricing overview to this lead
    - We already sent ANY specific service pricing (sent_pricing_intents is non-empty)
    - The message is an acknowledgment / thanks
    - The message expresses intent / next step rather than asking about price
    - The message is very short follow-on noise
    """
    # Never send the overview if we already did
    if getattr(appointment, 'pricing_overview_sent', False):
        return False

    # Never send the overview if we already sent a specific service price
    # (customer should ask a specific follow-up, not get the whole list again)
    if appointment.sent_pricing_intents:
        return False

    msg = message.lower().strip()

    ack_phrases = [
        'ok thank', 'thank u', 'thank you', 'thanks', 'ok cool', 'noted',
        'alright', 'got it', 'ok ok', 'okay', 'understood'
    ]
    if any(phrase in msg for phrase in ack_phrases):
        return False

    intent_phrases = [
        'start from scratch', 'need to start', 'want to start',
        'i need', 'i want', 'let me', 'can you', 'please help',
        'i would like', 'we would like', 'looking to', 'looking for',
    ]
    if any(phrase in msg for phrase in intent_phrases):
        return False

    if len(msg.split()) <= 2 and 'price' not in msg and 'cost' not in msg:
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Unchanged helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_chatbot_paused_for_sender(sender: str) -> bool:
    phone_number = f"whatsapp:+{sender}"
    appointment = Appointment.objects.filter(phone_number=phone_number).only('chatbot_paused').first()
    return bool(appointment and appointment.chatbot_paused)


def notify_admin_of_priority_lead(appointment: Appointment, sender: str):
    if appointment.lead_status not in {LeadStatus.HOT, LeadStatus.VERY_HOT}:
        return

    plumber_number = (appointment.plumber_contact_number or '263774819901')
    plumber_number = plumber_number.replace('+', '').replace('whatsapp:', '')
    customer_name = appointment.customer_name or 'Unknown customer'
    message = (
        f"Priority lead update\n"
        f"Lead status: {appointment.get_lead_status_display()}\n"
        f"Score: {appointment.lead_score}\n"
        f"Customer: {customer_name}\n"
        f"Phone: +{sender}\n"
        f"Service: {appointment.project_type or 'Not specified'}\n"
        f"Area: {appointment.customer_area or 'Not specified'}\n"
        f"Timeline: {appointment.timeline or 'Not specified'}\n"
        f"Site visit: {appointment.scheduled_datetime or 'Not set'}\n"
        f"Lead: https://plumbotv1-production.up.railway.app/appointments/{appointment.id}/"
    )
    try:
        whatsapp_api.send_text_message(plumber_number, message)
    except Exception as exc:
        print(f"Failed to notify admin for appointment {appointment.id}: {exc}")


def _schedule_media_ack(sender: str, appointment: "Appointment", media_type: str):
    def _send_ack():
        with _media_ack_lock:
            _media_ack_timers.pop(sender, None)

        try:
            fresh = Appointment.objects.get(phone_number=f"whatsapp:+{sender}")
        except Appointment.DoesNotExist:
            fresh = appointment

        if media_type == 'video':
            customer_reply = (
                "Thank you for sending that video! 🎥 Our plumber has been notified and will "
                "review it and contact you directly. If it's urgent, you can also call them on "
                f"{fresh.plumber_contact_number or '+263774819901'}."
            )
        else:
            customer_reply = (
                "Thank you for sending your plan! 📎 Our plumber has been notified and will "
                "be in touch with you directly to discuss your project.\n\n"
                "If it's urgent, you can also call them on "
                f"{fresh.plumber_contact_number or '+263774819901'}."
            )

        fresh.add_conversation_message("assistant", customer_reply)

        delay = get_random_delay()
        print(f"📨 Sending single media ack to {sender} after {delay // 60}m delay")
        time.sleep(delay)
        try:
            whatsapp_api.send_text_message(sender, customer_reply)
            print(f"✅ Media ack sent to {sender}")
        except Exception as e:
            print(f"❌ Failed to send media ack to {sender}: {e}")

    with _media_ack_lock:
        existing = _media_ack_timers.get(sender)
        if existing is not None:
            existing.cancel()
            print(f"🔄 Reset media ack timer for {sender}")

        timer = threading.Timer(MEDIA_DEBOUNCE_SECONDS, _send_ack)
        timer.daemon = True
        _media_ack_timers[sender] = timer
        timer.start()
        print(f"⏳ Media ack timer set for {sender} ({MEDIA_DEBOUNCE_SECONDS}s)")


def _schedule_plumber_alert(sender: str, appointment: "Appointment", file_url: "Optional[str]", media_type: str):
    """
    Debounced plumber alert — resets timer on each file received.
    After MEDIA_DEBOUNCE_SECONDS of silence, sends ONE alert listing all URLs.
    """
    def _send_alert():
        with _plumber_alert_lock:
            urls = _plumber_alert_pending.pop(sender, [])
            _plumber_alert_timers.pop(sender, None)

        try:
            fresh = Appointment.objects.get(phone_number=f"whatsapp:+{sender}")
        except Appointment.DoesNotExist:
            fresh = appointment

        plumber_number = (getattr(fresh, 'plumber_contact_number', None) or '263774819901')
        plumber_number = plumber_number.replace('+', '').replace('whatsapp:', '')

        ai_summary = generate_conversation_summary(fresh)
        customer_name = fresh.customer_name or "A customer"

        if urls:
            file_lines = "\n".join(f"  🔗 {u}" for u in urls)
            file_section = f"Files ({len(urls)}):\n{file_lines}"
        else:
            file_section = "⚠️ Files could not be saved automatically."

        alert_message = (
            f"📎 MEDIA RECEIVED FROM CUSTOMER\n\n"
            f"Customer: {customer_name}\n"
            f"Phone: +{sender}\n"
            f"WhatsApp: wa.me/{sender}\n"
            f"Media type: {media_type.upper()} ({len(urls)} file(s))\n"
            f"{file_section}\n\n"
            f"📋 APPOINTMENT DETAILS:\n"
            f"  Service: {fresh.project_type or 'Not specified'}\n"
            f"  Area: {fresh.customer_area or 'Not specified'}\n"
            f"  Property: {fresh.property_type or 'Not specified'}\n"
            f"  Timeline: {fresh.timeline or 'Not specified'}\n"
            f"  Has plan: {'Yes' if fresh.has_plan is True else 'No' if fresh.has_plan is False else 'Not answered'}\n\n"
            f"🤖 AI SUMMARY:\n{ai_summary}\n\n"
            f"🔗 View appointment:\n"
            f"https://plumbotv1-production.up.railway.app/appointments/{fresh.id}/"
        )

        try:
            whatsapp_api.send_text_message(plumber_number, alert_message)
            print(f"✅ Consolidated plumber alert sent ({len(urls)} file(s)) for {sender}")
        except Exception as e:
            print(f"❌ Failed to send plumber alert: {e}")

    with _plumber_alert_lock:
        # Accumulate the URL
        if sender not in _plumber_alert_pending:
            _plumber_alert_pending[sender] = []
        if file_url:
            _plumber_alert_pending[sender].append(file_url)

        # Reset the timer
        existing = _plumber_alert_timers.get(sender)
        if existing is not None:
            existing.cancel()

        timer = threading.Timer(MEDIA_DEBOUNCE_SECONDS, _send_alert)
        timer.daemon = True
        _plumber_alert_timers[sender] = timer
        timer.start()
        print(f"⏳ Plumber alert timer reset for {sender} (accumulated {len(_plumber_alert_pending[sender])} file(s))")


def get_random_delay() -> int:
    minutes = random.randint(1, 5)
    seconds = minutes * 1
    print(f"⏱️ Random delay: {minutes} minute(s)")
    return seconds


def delayed_response(sender, reply, delay_seconds):
    try:
        print(f"💤 Scheduling response in {delay_seconds // 60} minute(s)...")
        time.sleep(delay_seconds)
        print(f"✅ Delay complete, sending response now")
        whatsapp_api.send_text_message(sender, reply)
        print(f"✅ Response sent to {sender}")
    except Exception as e:
        print(f"❌ Error in delayed response: {str(e)}")


def detect_objection_type(message: str) -> str:
    message_lower = message.lower()
    if any(k in message_lower for k in ['how much', 'cost', 'price', 'expensive', 'kuisa', 'mari']):
        return 'pricing'
    if any(k in message_lower for k in ['how long', 'duration', 'when finish']):
        return 'timeline'
    if any(k in message_lower for k in ['when can you', 'available', 'come']):
        return 'availability'
    return 'other'


def is_post_booking_ack_message(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    normalized = " ".join(msg.split())
    ack_phrases = {
        "ok", "okay", "k", "kk", "oky", "ok cool", "alright",
        "sharp", "sharp sharp", "sho", "cool", "nice", "thanks",
        "thank you", "noted", "got it", "sawa",
    }
    return normalized in ack_phrases


def is_previous_work_photo_request(message: str) -> bool:
    """Use DeepSeek AI to detect if customer is asking to see previous work photos."""
    try:
        message_clean = (message or "").strip().lower()
        # Fast-path: avoid an expensive AI call for tiny acknowledgements.
        if len(message_clean) <= 4 or message_clean in {"ok", "okay", "k", "thanks", "thank you", "cool", "fine"}:
            return False

        keyword_hints = (
            "photo", "photos", "picture", "pictures", "pic", "pics",
            "image", "images", "portfolio", "examples", "show me",
            "mifananidzo", "mufananidzo", "ratidza", "ndiratidze", "basa renyu",
        )
        if not any(k in message_clean for k in keyword_hints):
            return False

        from openai import OpenAI

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
- "can I hv a pic", "send pics", "got pics"

Consider Shona expressions like:
- "ndiratidze mifananidzo", "une mifananidzo here", "ndiona basa renyu"
- "tumira mifananidzo", "ratidza basa renyu", "mifananidzo yebasa renyu"
- "ndione zvamakamboita", "mufananidzo", "basa renyu"

Also consider mixed Shona/English and informal abbreviations like "pic", "pix", "img".

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
            # English — including abbreviations
            'picture', 'photo', 'photos', 'pic', 'pics', 'pix', 'image', 'images',
            'previous work', 'portfolio', 'show me', 'your work', 'examples',
            # Shona
            'mifananidzo', 'mufananidzo', 'ratidza', 'ndiratidze', 'basa renyu',
            'ndiona', 'ndione', 'tumira',
        ]
        return any(kw in message_lower for kw in fallback_keywords)


PREVIOUS_WORK_IMAGES_DIR = os.environ.get(
    'PREVIOUS_WORK_IMAGES_DIR',
    os.path.join(os.path.dirname(__file__), 'previous_work_photos')
)
SUPPORTED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}


def get_previous_work_images() -> list:
    images = []
    if not os.path.exists(PREVIOUS_WORK_IMAGES_DIR):
        print(f"⚠️ Previous work images folder not found: {PREVIOUS_WORK_IMAGES_DIR}")
        return images
    for filename in sorted(os.listdir(PREVIOUS_WORK_IMAGES_DIR)):
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_IMAGE_EXTENSIONS:
            images.append(os.path.join(PREVIOUS_WORK_IMAGES_DIR, filename))
    print(f"📸 Found {len(images)} previous work images")
    return images


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3 — PREVIOUS WORK PHOTO DEDUP
# send_previous_work_photos now returns True ONLY after photos are confirmed
# queued; the caller must NOT send any fallback text when True is returned.
# ─────────────────────────────────────────────────────────────────────────────

def send_previous_work_photos(sender, appointment=None):
    """
    Send previous work photos with a small delay between each image.
    Returns True if photos were queued (caller must NOT send additional text).
    Returns False if no images are configured (caller may send a text fallback).
    """
    images = get_previous_work_images()

    if not images:
        print("⚠️ No previous work images found — caller should handle fallback")
        return False  # Caller will send text fallback

    intro = "Here are some examples of our previous plumbing work! 🔧✨"

    def send_images_with_delay():
        try:
            delay_seconds = get_random_delay()
            print(f"💤 Waiting {delay_seconds // 60} minute(s) before sending images to {sender}")
            time.sleep(delay_seconds)

            whatsapp_api.send_text_message(sender, intro)

            sent_count = 0
            for index, image_path in enumerate(images):
                caption = "Our previous work - high quality plumbing & renovations" if index == 0 else None
                whatsapp_api.send_local_image(sender, image_path, caption=caption)
                sent_count += 1
                time.sleep(0.5)

            follow_up = "Would you like to book an appointment? Just tell me what service you need! 😊"
            time.sleep(1)
            whatsapp_api.send_text_message(sender, follow_up)

            if appointment:
                appointment.add_conversation_message("assistant", intro)
                appointment.add_conversation_message(
                    "assistant", f"[MEDIA] Sent {sent_count} previous work image(s)"
                )
                appointment.add_conversation_message("assistant", follow_up)

            print(f"✅ Sent {sent_count}/{len(images)} previous work images to {sender}")

        except Exception as e:
            print(f"❌ Failed to send images: {str(e)}")

    threading.Thread(target=send_images_with_delay, daemon=True).start()
    return True  # Photos queued — caller must not add any extra text


def handle_pricing_objection(appointment) -> str:
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
        service_ranges = {
            'bathroom_renovation': 'US$1,500 - US$6,000',
            'kitchen_renovation': 'US$3,000 - US$12,000',
            'new_plumbing_installation': 'US$700 - US$8,000'
        }
        range_str = service_ranges.get(appointment.project_type, 'US$1,000 - US$15,000')
        return (
            f"Based on your {appointment.project_type.replace('_', ' ')}, typical pricing ranges "
            f"from {range_str}.\n\nHowever, the exact cost depends on:\n"
            f"• Specific fixtures and materials you choose\n"
            f"• Size and complexity of the work\n"
            f"• Your exact location ({appointment.customer_area})\n\n"
            f"For an accurate quote, our plumber will need to "
            f"{'review your plan' if appointment.has_plan else 'do a site visit'}.\n\n"
            f"Would you like to proceed with booking?"
        )

    missing_str = ' and '.join(missing) if len(missing) <= 2 else f"{', '.join(missing[:-1])}, and {missing[-1]}"
    return (
        f"I'd love to give you a price! To provide an accurate quote, I need to know {missing_str}.\n\n"
        f"Our pricing varies based on your specific project details - every job is unique.\n\n"
        f"Let me ask you a few quick questions so I can give you the most accurate estimate."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Webhook entry points
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    if request.method == 'GET':
        return verify_webhook(request)
    return handle_webhook_event(request)


def verify_webhook(request):
    try:
        mode = request.GET.get('hub.mode')
        token = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge')
        verify_token = os.environ.get('WHATSAPP_VERIFY_TOKEN', 'your_verify_token_here')
        if mode == 'subscribe' and token == verify_token:
            print("✅ Webhook verified successfully")
            return HttpResponse(challenge, content_type='text/plain')
        print("❌ Webhook verification failed")
        return HttpResponse(status=403)
    except Exception as e:
        print(f"❌ Webhook verification error: {str(e)}")
        return HttpResponse(status=500)


def handle_webhook_event(request):
    try:
        body = json.loads(request.body.decode('utf-8'))
        print("📨 Webhook received")
        if body.get('object') != 'whatsapp_business_account':
            return HttpResponse(status=200)
        threading.Thread(
            target=process_webhook_in_background,
            args=(body,),
            daemon=True
        ).start()
        return HttpResponse(status=200)
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in webhook: {str(e)}")
        return HttpResponse(status=400)
    except Exception as e:
        print(f"❌ Webhook processing error: {str(e)}")
        return HttpResponse(status=500)


def process_webhook_in_background(body):
    try:
        for entry in body.get('entry', []):
            for change in entry.get('changes', []):
                if change.get('field') == 'messages':
                    process_message_change(change.get('value', {}))
    except Exception as e:
        print(f"❌ Background processing error: {str(e)}")


def process_message_change(value):
    try:
        statuses = value.get('statuses', [])
        if statuses:
            process_status_updates(statuses)

        messages = value.get('messages', [])
        for message in messages:
            message_type = message.get('type')
            message_id   = message.get('id')
            sender       = message.get('from')

            if message_id:
                try:
                    WhatsAppInboundEvent.objects.create(message_id=message_id, sender=sender or "")
                except IntegrityError:
                    print(f"Duplicate inbound message ignored: {message_id}")
                    continue

            print(f"📬 Processing message from {sender}, type: {message_type}")

            try:
                whatsapp_api.mark_message_as_read(message_id)
            except Exception as e:
                print(f"⚠️ Could not mark as read: {str(e)}")

            if message_type == 'text':
                handle_text_message(sender, message.get('text', {}), message_id=message_id)
            elif message_type == 'image':
                handle_media_message(sender, message.get('image', {}), 'image')
            elif message_type == 'document':
                handle_media_message(sender, message.get('document', {}), 'document')
            elif message_type in ('audio', 'voice'):
                # WhatsApp sends recorded voice notes as type 'audio' OR 'voice'
                handle_audio_message(sender, message.get('audio') or message.get('voice') or {})
            elif message_type == 'video':
                handle_media_message(sender, message.get('video', {}), 'video')
            elif message_type == 'sticker':
                handle_unsupported_media(sender, 'sticker')
            elif message_type == 'location':
                handle_location_message(sender, message.get('location', {}))
            elif message_type == 'contacts':
                handle_unsupported_media(sender, 'contacts')
            else:
                # Truly unknown type — log it so we can add a handler later
                print(f"⚠️ Unrecognised message type from {sender}: '{message_type}' — ignoring")
                # Do NOT call handle_unsupported_media here to avoid confusing
                # the customer with an error message for types they didn't choose.

    except Exception as e:
        print(f"❌ Error processing message: {str(e)}")


def _clean_phone(raw_phone: str) -> str:
    return (raw_phone or "").replace("whatsapp:", "").replace("+", "").strip()


def _find_appointment_by_recipient(recipient_id: str) -> Optional[Appointment]:
    """
    Best-effort lookup from webhook status recipient_id (usually digits only)
    to our stored appointment phone formats.
    """
    cleaned = _clean_phone(recipient_id)
    if not cleaned:
        return None

    direct_candidates = {
        cleaned,
        f"+{cleaned}",
        f"whatsapp:{cleaned}",
        f"whatsapp:+{cleaned}",
    }
    appointment = (
        Appointment.objects.filter(phone_number__in=direct_candidates)
        .order_by('-updated_at')
        .first()
    )
    if appointment:
        return appointment

    return (
        Appointment.objects.annotate(
            clean_phone=Replace(
                Replace(
                    Replace('phone_number', Value('whatsapp:+'), Value('')),
                    Value('whatsapp:'),
                    Value(''),
                ),
                Value('+'),
                Value(''),
            )
        )
        .filter(clean_phone=cleaned)
        .order_by('-updated_at')
        .first()
    )


def _format_status_errors(errors: list) -> str:
    if not errors:
        return ""
    parts = []
    for err in errors:
        code = err.get('code')
        title = err.get('title') or err.get('message') or 'Unknown error'
        details = err.get('error_data', {}).get('details')
        piece = f"code={code}, title={title}"
        if details:
            piece += f", details={details}"
        parts.append(piece)
    return " | ".join(parts)


def process_status_updates(statuses):
    """
    Handle asynchronous WhatsApp outbound delivery state updates.
    This is the source of truth for delivered/read/failed, not send-time logs.
    """
    for status_obj in statuses:
        try:
            message_id = status_obj.get('id', '')
            status_name = (status_obj.get('status') or 'unknown').lower()
            recipient_id = status_obj.get('recipient_id', '')
            timestamp = status_obj.get('timestamp', '')
            conversation_id = (status_obj.get('conversation') or {}).get('id', '')
            pricing_model = (status_obj.get('pricing') or {}).get('pricing_model', '')
            billable = (status_obj.get('pricing') or {}).get('billable')
            errors = status_obj.get('errors') or []
            error_text = _format_status_errors(errors)

            appointment = _find_appointment_by_recipient(recipient_id) if recipient_id else None
            appointment_ref = f"appointment_id={appointment.id}" if appointment else "appointment_id=unknown"

            print(
                f"📶 WhatsApp status: status={status_name}, recipient={recipient_id}, "
                f"message_id={message_id}, ts={timestamp}, {appointment_ref}, "
                f"conversation_id={conversation_id or 'n/a'}, "
                f"pricing_model={pricing_model or 'n/a'}, billable={billable}"
            )

            if error_text:
                print(f"❌ WhatsApp delivery error: {error_text}")

            # Persist failure context where team can see it in appointment details.
            if status_name == 'failed' and appointment:
                note = (
                    f"[WA Delivery Failure] recipient=+{_clean_phone(recipient_id)} "
                    f"message_id={message_id} timestamp={timestamp} "
                    f"errors={error_text or 'unknown'}"
                )
                existing = (appointment.internal_notes or "").strip()
                appointment.internal_notes = f"{note}\n{existing}".strip()
                appointment.save(update_fields=['internal_notes'])

        except Exception as status_err:
            print(f"❌ Failed to process status update: {status_err}")


def handle_location_message(sender, location_data):
    try:
        latitude = location_data.get('latitude')
        longitude = location_data.get('longitude')
        address = location_data.get('address')
        print(f"📍 Location from {sender}: {latitude}, {longitude}")

        phone_number = f"whatsapp:+{sender}"
        try:
            appointment = Appointment.objects.get(phone_number=phone_number)
        except Appointment.DoesNotExist:
            response_msg = "Thanks for the location! To get started, please tell me about your plumbing needs."
            delay = get_random_delay()
            threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()
            return

        if appointment.chatbot_paused:
            print(f"Chatbot paused for {phone_number}; ignoring auto location response.")
            return

        from .views import Plumbot
        plumbot = Plumbot(phone_number)
        next_question = plumbot.get_next_question_to_ask()

        if next_question == 'area' and not appointment.customer_area:
            if address:
                appointment.customer_area = address
                appointment.save()
                refresh_lead_score(appointment)
                reply = plumbot.generate_response(f"My location is {address}")
                delay = get_random_delay()
                threading.Thread(target=delayed_response, args=(sender, reply, delay), daemon=True).start()
            else:
                response_msg = (
                    "Thanks for the location pin! 📍\n\n"
                    "Could you also type the area name? (e.g., Harare Hatfield, Harare Avondale)\n\n"
                    "This helps us serve you better."
                )
                delay = get_random_delay()
                threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()
        else:
            response_msg = "Thanks for sharing your location! 📍\n\nI've noted it. Let me continue with your appointment details..."
            delay = get_random_delay()
            threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()

    except Exception as e:
        print(f"❌ Error handling location: {str(e)}")


def handle_unsupported_media(sender, media_type):
    try:
        if is_chatbot_paused_for_sender(sender):
            print(f"Chatbot paused for whatsapp:+{sender}; skipping unsupported media auto response.")
            return
        print(f"⚠️ Unsupported media type from {sender}: '{media_type}'")

        # Guard: these types have dedicated handlers — should NEVER reach here.
        # If they do it means process_message_change has a routing bug.
        if media_type in ('image', 'document', 'video', 'audio', 'voice'):
            print(
                f"⚠️ WARNING: '{media_type}' was incorrectly routed to "
                f"handle_unsupported_media. Ignoring silently."
            )
            return

        media_names = {
            'sticker':  'sticker',
            'contacts': 'contact card',
            'gif':      'GIF',
        }
        # Use 'file' as the fallback instead of the raw type string,
        # so we never say "Thanks for the unsupported!"
        friendly_name = media_names.get(media_type, 'file')

        response_msg = (
            f"Thanks for the {friendly_name}! 😊\n\n"
            f"I can't process {friendly_name}s right now, but I work great with:\n"
            f"✅ Text messages\n"
            f"✅ Images (for plans)\n"
            f"✅ PDF documents (for plans)\n"
            f"✅ Videos\n\n"
            f"Could you send that as a text message instead?\n\nThanks!"
        )
        delay = get_random_delay()
        threading.Thread(
            target=delayed_response, args=(sender, response_msg, delay), daemon=True
        ).start()

    except Exception as e:
        print(f"❌ Error handling unsupported media: {str(e)}")


def handle_audio_message(sender, audio_data):
    try:
        if is_chatbot_paused_for_sender(sender):
            print(f"Chatbot paused for whatsapp:+{sender}; skipping audio auto response.")
            return
        print(f"🎤 Audio message from {sender}")

        phone_number = f"whatsapp:+{sender}"
        try:
            appointment = Appointment.objects.get(phone_number=phone_number)
        except Appointment.DoesNotExist:
            response_msg = (
                "Hi there! 👋\n\nI received your voice message, but I work better with text messages.\n\n"
                "Could you please type your message instead? That way I can help you book your "
                "plumbing appointment more efficiently.\n\nThanks! 😊"
            )
            delay = get_random_delay()
            threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()
            return

        if appointment.plan_status == 'pending_upload':
            response_msg = (
                "I see you sent an audio message, but I need images or PDF documents for your plan.\n\n"
                "Please send:\n📸 Photos of your plan/blueprint\n📄 PDF document\n\n"
                "Or type \"done\" if you've finished uploading."
            )
        else:
            response_msg = (
                "I received your voice message! 🎤\n\n"
                "However, I work better with text messages. Could you please type your response instead?\n\n"
                "I'll continue where we left off... 😊"
            )

        delay = get_random_delay()
        threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()

    except Exception as e:
        print(f"❌ Error handling audio: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN TEXT HANDLER — all dedup logic lives here
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_text_for_dedupe(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _is_duplicate_text_event(sender: str, message_body: str) -> bool:
    now = time.monotonic()
    normalized = _normalize_text_for_dedupe(message_body)
    key = (sender or "", normalized)
    with _text_dedupe_lock:
        # Evict stale keys to keep memory bounded.
        cutoff = now - TEXT_DEDUPE_WINDOW_SECONDS
        stale_keys = [k for k, ts in _recent_text_events.items() if ts < cutoff]
        for stale_key in stale_keys:
            _recent_text_events.pop(stale_key, None)

        last_seen = _recent_text_events.get(key)
        if last_seen is not None and (now - last_seen) < TEXT_DEDUPE_WINDOW_SECONDS:
            return True

        _recent_text_events[key] = now
        return False


def handle_text_message(sender, text_data, message_id=None):
    try:
        message_body = text_data.get('body', '').strip()
        if not message_body:
            return

        if _is_duplicate_text_event(sender, message_body):
            print(
                f"Duplicate text suppressed: sender={sender}, "
                f"message_id={message_id}, body='{message_body[:80]}'"
            )
            return

        print(f"Text from {sender}: {message_body}")

        phone_number = f"whatsapp:+{sender}"

        appointment, created = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )

        appointment.add_conversation_message("user", message_body)
        print("User message saved to conversation history")

        appointment.mark_customer_response()

        # Auto-classify service type from the customer's message
        if not appointment.project_type:
            from .service_type_classifier import classify_and_save
            classify_and_save(appointment, message_body)

        previous_status = appointment.lead_status
        _, new_status = refresh_lead_score(appointment)
        if new_status != previous_status and new_status in {LeadStatus.HOT, LeadStatus.VERY_HOT}:
            notify_admin_of_priority_lead(appointment, sender)

        if appointment.chatbot_paused:
            print(f"Chatbot paused for {phone_number}; skipping auto response.")
            return

        if appointment.status == 'confirmed' and is_post_booking_ack_message(message_body):
            print(
                f"Post-booking ack detected; no reply sent. "
                f"sender={sender}, message='{message_body}'"
            )
            return

        from .views import Plumbot
        plumbot = Plumbot(phone_number)

        reply = None

        # ── STEP 1: Previous work photo request ──────────────────────────────
        print(f"Checking photo request: '{message_body}'")
        if is_previous_work_photo_request(message_body):
            print("Photo request detected")
            photos_queued = send_previous_work_photos(sender, appointment)
            if photos_queued:
                # FIX 3: photos are queued — do NOT send any additional text
                return
            # No images configured — send a single fallback text (no bot reply on top)
            fallback_reply = (
                "I can share previous-work photos, but they are not configured yet. "
                "Please ask our team and we will send them shortly."
            )
            appointment.add_conversation_message("assistant", fallback_reply)
            delay = get_random_delay()
            threading.Thread(target=delayed_response, args=(sender, fallback_reply, delay), daemon=True).start()
            return  # Stop here — do NOT also run normal bot flow

        # ── STEP 2: Service-specific pricing inquiry ─────────────────────────
        # Block service inquiry responses once:
        #   (a) we are mid-conversation (collecting booking details), OR
        #   (b) ANY pricing has already been sent (overview or specific intent), OR
        #   (c) the specific intent was already sent to this lead.
        any_pricing_sent = (
            getattr(appointment, 'pricing_overview_sent', False) or
            bool(appointment.sent_pricing_intents)
        )
        mid_conversation = (
            any_pricing_sent or
            (
                appointment.project_type is not None and
                (
                    appointment.has_plan is not None or
                    appointment.customer_area is not None or
                    appointment.property_type is not None
                )
            )
        )

        if not mid_conversation:
            print(f"Checking service inquiry: '{message_body}'")
            inquiry = plumbot.detect_service_inquiry(message_body)
            print(f"Service inquiry result: {inquiry}")

            if inquiry.get('intent') != 'none' and inquiry.get('confidence') == 'HIGH':
                intent = inquiry['intent']

                # FIX 1: Only send pricing for this intent once per lead
                if _has_sent_pricing_for_intent(appointment, intent):
                    print(f"⏭️ Skipping already-sent pricing for intent: {intent} — falling through to bot")
                    # reply stays None → falls through to normal Plumbot below
                else:
                    print(f"Service inquiry matched (first time): {intent}")
                    reply = plumbot.handle_service_inquiry(intent, message_body)
                    _mark_pricing_intent_sent(appointment, intent)
        else:
            print(f"⏭️ Skipping service inquiry check — mid-conversation or pricing already sent")

        # ── STEP 3: Full pricing overview ────────────────────────────────────
        # FIX 2: _is_genuine_pricing_question now also blocks if any specific
        # intent was already sent.
        if reply is None:
            objection_type = detect_objection_type(message_body)
            print(f"Objection type: {objection_type}")

            if objection_type == 'pricing' and _is_genuine_pricing_question(message_body, appointment):
                reply = plumbot.generate_pricing_overview(message_body)
                appointment.pricing_overview_sent = True
                appointment.save(update_fields=['pricing_overview_sent'])

        # ── STEP 4: Normal Plumbot processing ────────────────────────────────
        if reply is None:
            print("Running normal Plumbot processing")
            reply = plumbot.generate_response(
                message_body,
                precomputed_service_inquiry=inquiry if not mid_conversation else None,
            )

        print(f"Final reply: {reply[:100]}...")

        appointment.add_conversation_message("assistant", reply)
        appointment.last_outbound_at = timezone.now()
        appointment.last_contacted_at = appointment.last_outbound_at
        appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
        print("Assistant reply saved to conversation history")

        delay = get_random_delay()
        print(f"Random delay: {delay // 60} minute(s)")
        threading.Thread(target=delayed_response, args=(sender, reply, delay), daemon=True).start()
        print(f"Response scheduled for {delay // 60} minute(s) from now")

    except Exception as e:
        print(f"Error handling text: {str(e)}")
        import traceback
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Media handler (unchanged logic, kept intact)
# ─────────────────────────────────────────────────────────────────────────────

MEDIA_STORAGE_FOLDERS = {
    'image':    'customer_plans',
    'document': 'customer_plans',
    'video':    'customer_videos',
    'audio':    'customer_audio',
}

IMAGE_DOC_EXT_MAP = {
    'image/jpeg': '.jpg',
    'image/jpg':  '.jpg',
    'image/png':  '.png',
    'image/webp': '.webp',
    'image/gif':  '.gif',
    'application/pdf': '.pdf',
}


def handle_media_message(sender, media_data, media_type):
    try:
        media_id = media_data.get('id')
        mime_type = media_data.get('mime_type', '')
        phone_number = f"whatsapp:+{sender}"

        appointment, created = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )

        file_bytes = None
        if media_id:
            try:
                file_bytes = whatsapp_api.download_media(media_id)
                print(f"✅ Downloaded {len(file_bytes)} bytes from WhatsApp (id={media_id})")
            except Exception as dl_err:
                print(f"❌ Failed to download media from WhatsApp: {dl_err}")

        saved_path = None
        file_url = None
        if file_bytes:
            try:
                if media_type in ('image', 'document'):
                    ext = IMAGE_DOC_EXT_MAP.get(mime_type, '.bin')
                else:
                    ext = get_extension_for_mime(mime_type)

                folder = MEDIA_STORAGE_FOLDERS.get(media_type, 'customer_media')
                timestamp = timezone.now().strftime('%Y%m%d_%H%M%S_%f')  # Added %f for microseconds to avoid filename collisions
                customer_slug = ''.join(
                    c for c in (appointment.customer_name or 'customer') if c.isalnum()
                )
                filename = f"{media_type}_{customer_slug}_{appointment.id}_{timestamp}{ext}"
                storage_path = f"{folder}/{filename}"

                file_obj = ContentFile(file_bytes, name=filename)
                saved_path = default_storage.save(storage_path, file_obj)
                file_url = default_storage.url(saved_path)

                print(f"✅ Media saved: {saved_path}")
                print(f"✅ File URL: {file_url}")

                if media_type in ('image', 'document'):
                    file_note = f"\n[FILE UPLOADED] {saved_path} | URL: {file_url} | {timezone.now().isoformat()}"

                    # Atomic append to internal_notes — safe under concurrent writes
                    update_kwargs = dict(
                        internal_notes=Concat('internal_notes', Value(file_note)),
                        plan_status='plan_uploaded',
                        plan_uploaded_at=timezone.now(),
                    )
                    Appointment.objects.filter(pk=appointment.pk).update(**update_kwargs)

                    # Only set plan_file if still empty (first image wins)
                    Appointment.objects.filter(pk=appointment.pk, plan_file='').update(plan_file=saved_path)
                    Appointment.objects.filter(pk=appointment.pk, plan_file__isnull=True).update(plan_file=saved_path)

                    # Only set has_plan=True if it hasn't been answered yet
                    Appointment.objects.filter(pk=appointment.pk, has_plan__isnull=True).update(has_plan=True)

                elif media_type == 'video':
                    video_note = f"\n[VIDEO UPLOADED] {saved_path} | URL: {file_url} | {timezone.now().isoformat()}"

                    # Atomic append to internal_notes
                    Appointment.objects.filter(pk=appointment.pk).update(
                        internal_notes=Concat('internal_notes', Value(video_note)),
                    )
                    # Only update these fields if not already set
                    Appointment.objects.filter(pk=appointment.pk, has_plan__isnull=True).update(has_plan=True)
                    Appointment.objects.filter(pk=appointment.pk, plan_status__isnull=True).update(
                        plan_status='plan_uploaded',
                        plan_uploaded_at=timezone.now(),
                    )
                    Appointment.objects.filter(pk=appointment.pk, plan_status='').update(
                        plan_status='plan_uploaded',
                        plan_uploaded_at=timezone.now(),
                    )

                # Refresh in-memory object so refresh_lead_score sees current state
                appointment.refresh_from_db()
                refresh_lead_score(appointment)

            except Exception as save_err:
                print(f"❌ Failed to save media to storage: {save_err}")
                import traceback
                traceback.print_exc()

        appointment.add_conversation_message("user", f"[Sent {media_type}]")

        # Debounced plumber alert — waits for burst to finish, then sends ONE message
        # with all file URLs listed.
        _schedule_plumber_alert(sender, appointment, file_url, media_type)

        if not appointment.chatbot_paused:
            _schedule_media_ack(sender, appointment, media_type)
        else:
            print(f"Chatbot paused for whatsapp:+{sender}; skipped media acknowledgment.")

    except Exception as e:
        print(f"❌ Error handling media: {str(e)}")
        import traceback
        traceback.print_exc()


def generate_conversation_summary(appointment) -> str:
    try:
        if not appointment.conversation_history:
            return "No conversation history available."

        recent_messages = appointment.conversation_history[-20:]
        transcript_lines = []
        for msg in recent_messages:
            role = msg.get('role', '')
            content = msg.get('content', '').strip()
            if not content or content.startswith('[Sent '):
                continue
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

        from openai import OpenAI
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
        print("✅ AI conversation summary generated")
        return summary

    except Exception as e:
        print(f"❌ AI summary generation failed: {str(e)}")
        try:
            fallback_lines = []
            for msg in appointment.conversation_history[-3:]:
                role = "Customer" if msg.get('role') == 'user' else "Bot"
                content = msg.get('content', '')[:150]
                fallback_lines.append(f"{role}: {content}")
            return "Summary unavailable. Last messages:\n" + "\n".join(fallback_lines)
        except Exception:
            return "Summary unavailable."
