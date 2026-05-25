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
from .plumber_notifications import send_plumber_notification_email
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.conf import settings
from django.db import IntegrityError
import threading
import time
import random
from pathlib import Path
from .services.lead_scoring import refresh_lead_score
from typing import Optional
from openai import OpenAI
from .repeated_question_detector import (
    detect_repeated_question,
    generate_repeat_clarification,
    detect_language_simple,
)

PREVIOUS_WORK_IMAGE_URLS = [
    url.strip()
    for url in os.environ.get('PREVIOUS_WORK_IMAGE_URLS', '').replace('\n', ',').split(',')
    if url.strip()
]

# --- Media debounce trackers --------------------------------------------------
_media_ack_timers: dict = {}
_media_ack_lock = threading.Lock()
MEDIA_DEBOUNCE_SECONDS = 8

# Plumber alert debounce — accumulates file URLs across a burst of images,
# then sends ONE consolidated alert after the burst window closes.
_plumber_alert_timers: dict = {}          # sender ? threading.Timer
_plumber_alert_pending: dict = {}         # sender ? list of file_url strings
_plumber_alert_lock = threading.Lock()

# Text dedupe window to suppress near-identical duplicate webhook deliveries.
_text_dedupe_lock = threading.Lock()
_recent_text_events: dict = {}  # key=(sender, normalized_text) -> monotonic timestamp
TEXT_DEDUPE_WINDOW_SECONDS = 20

# Message batch accumulator — collects messages sent by the same customer within a short
# window so that a single combined reply addresses all of them at once.
_pending_batches: dict = {}         # sender -> list of (message_body, message_id)
_pending_batch_timers: dict = {}    # sender -> threading.Timer
_pending_batch_lock = threading.Lock()
MESSAGE_BATCH_WINDOW_SECONDS = 1   # wait this long after the LAST message before generating a reply

# Per-sender cancel events for delayed sends still in their sleep window.
# When a new message arrives, the event is set so the sleeping thread aborts
# instead of sending a now-stale reply. The next batch covers everything.
_pending_send_events: dict = {}     # sender -> threading.Event
_pending_send_lock = threading.Lock()

# DeepSeek client for translation (optional)
_DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY')
_deepseek = (
    OpenAI(api_key=_DEEPSEEK_KEY, base_url='https://api.deepseek.com/v1')
    if _DEEPSEEK_KEY else None
)


def _clear_delay_signal_if_present(appointment: Appointment) -> None:
    if appointment.is_delayed or '[DELAY_SIGNAL]' in (appointment.internal_notes or ''):
        appointment.clear_delayed(save=True)
        print(f"▶️ Delay signal cleared — customer re-engaged on appointment {appointment.id}")
    
def _translate_reply_for_customer(customer_message: str, reply: str) -> str:
    """
    Translate the bot reply based on the customer's language.
    - If customer writes in Shona: respond in Shona.
    - If mixed: respond in both Shona and English (Shona first).
    - If English: keep English.
    """
    if not _deepseek or not reply:
        return reply

    try:
        prompt = f"""You are a language detector and Shona translator for Homebase Plumbers, a plumbing company in Harare, Zimbabwe.

Customer message (use this as the language signal):
\"\"\"{customer_message}\"\"\"

Bot reply to translate (English):
\"\"\"{reply}\"\"\"

STEP 1 — DETECT LANGUAGE
Classify the customer's language as one of:
- "english"  → mostly English, little or no Shona
- "shona"    → mostly Shona (may include borrowed English plumbing terms)
- "mixed"    → natural Zimbabwean code-switching (Shona + English blended)

STEP 2 — TRANSLATE (only if "shona" or "mixed")
Produce a natural Zimbabwean Shona translation of the bot reply.

TRANSLATION RULES:
1. Use Zimbabwean Shona (Karanga/Zezuru dialect blend common in Harare) — NOT Zambian or Malawian variants.
2. Keep these words in English — customers know them and use them daily:
   geyser, tub, shower, vanity, toilet, drain, pipe, plumber, quote, site visit,
   bathroom, kitchen, installation, supply, assessment, booking, WhatsApp, USD, US$
3. Keep all numbers, prices (US$...), dates, times, emojis, bullet points, and line breaks exactly as-is.
4. Keep brand/company names exactly: "Homebase Plumbers", "HomeBase".
5. For "mixed" — write the reply as natural Zimbabwean code-switching: blend Shona and English the way a Harare local would WhatsApp a friend. Do NOT produce two separate paragraphs.
6. For "shona" — write fully in Shona except for the technical terms listed above.
7. Match the tone: casual, warm, WhatsApp-friendly. Not formal. Not stiff.
8. Do NOT add information not in the original reply. Do NOT remove any detail.
9. If a sentence is already very short and idiomatic (e.g. "Sharp! 👍"), keep it or use a natural Shona equivalent.

RESPONSE JSON FORMAT (return ONLY this, no markdown):
{{
  "language": "english|shona|mixed",
  "shona_reply": "translated text or empty string if english"
}}
"""

        response = _deepseek.chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Return ONLY valid JSON. No markdown or extra text.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=900,
        )

        raw = response.choices[0].message.content.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()
        result = json.loads(raw)

        language = (result.get('language') or '').strip().lower()
        shona_reply = (result.get('shona_reply') or '').strip()

        if language in ('shona', 'mixed'):
            return shona_reply or reply
        return reply

    except Exception as exc:
        print(f"Translation error (DeepSeek): {exc}")
        return reply


# -----------------------------------------------------------------------------
# FIX 1 — SERVICE-LEVEL PRICING DEDUP
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# FIX 2 — PRICING OVERVIEW DEDUP (also blocks if any specific intent was sent)
# -----------------------------------------------------------------------------

def _is_genuine_pricing_question(message: str, appointment: Appointment) -> bool:
    """
    Return True ONLY when the message is a fresh, standalone pricing inquiry.
    """
    if getattr(appointment, 'pricing_overview_sent', False):
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
    # Allow "how much" and "marii" even when short — they are unambiguous pricing requests
    explicit_short_pricing = ('how much', 'marii', 'mari', 'mutengo', 'zvakadai')
    if len(msg.split()) <= 2:
        if any(phrase in msg for phrase in explicit_short_pricing):
            return True  # short but unambiguous — allow
        return False
    combined_pricing_phrases = [
        'how much for all', 'how much zvese', 'zvese zvakadai', 'zvese izvi',
        'all of these', 'all of it', 'total cost', 'total price', 'overall cost',
        'everything', 'all together', 'combined', 'grand total',
        'how much all', 'mutengo wese', 'mutengo wazvose',
    ]
    if any(phrase in msg for phrase in combined_pricing_phrases):
        return True
    # Block overview if any specific intent was already sent (customer should ask follow-ups)
    if appointment.sent_pricing_intents:
        return False
    return True

# -----------------------------------------------------------------------------
# Unchanged helpers
# -----------------------------------------------------------------------------

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
    send_plumber_notification_email(
        subject=f"Priority lead update for {customer_name}",
        message=message,
    )


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
                "Got that, thanks for sharing! 🎥\n\n"
                "Could you describe what you're looking to get done? "
                "The more detail the better — even a rough idea helps us plan the visit."
            )
        else:
            customer_reply = (
                "Got it, thanks for sharing that! 📋\n\n"
                "Could you describe what you'd like done, or is there something specific "
                "you'd like to change? Just a few words is fine."
            )

        fresh.add_conversation_message("assistant", customer_reply)

        delay = get_random_delay()
        print(f"?? Sending single media ack to {sender} after {delay // 60}m delay")
        time.sleep(delay)
        try:
            whatsapp_api.send_text_message(sender, customer_reply)
            print(f"? Media ack sent to {sender}")
        except Exception as e:
            print(f"? Failed to send media ack to {sender}: {e}")

    with _media_ack_lock:
        existing = _media_ack_timers.get(sender)
        if existing is not None:
            existing.cancel()
            print(f"?? Reset media ack timer for {sender}")

        timer = threading.Timer(MEDIA_DEBOUNCE_SECONDS, _send_ack)
        timer.daemon = True
        _media_ack_timers[sender] = timer
        timer.start()
        print(f"? Media ack timer set for {sender} ({MEDIA_DEBOUNCE_SECONDS}s)")


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

        customer_name = fresh.customer_name or "A customer"

        if urls:
            file_lines = "\n".join(f"  ?? {u}" for u in urls)
            file_section = f"Files ({len(urls)}):\n{file_lines}"
        else:
            file_section = "?? Files could not be saved automatically."

        alert_message = (
            f"?? MEDIA RECEIVED FROM CUSTOMER\n\n"
            f"Customer: {customer_name}\n"
            f"Phone: +{sender}\n"
            f"WhatsApp: wa.me/{sender}\n"
            f"Media type: {media_type.upper()} ({len(urls)} file(s))\n"
            f"{file_section}\n\n"
            f"?? APPOINTMENT DETAILS:\n"
            f"  Service: {fresh.project_type or 'Not specified'}\n"
            f"  Area: {fresh.customer_area or 'Not specified'}\n\n"
            f"?? View appointment:\n"
            f"https://plumbotv1-production.up.railway.app/appointments/{fresh.id}/"
        )

        try:
            whatsapp_api.send_text_message(plumber_number, alert_message)
            print(f"? Consolidated plumber alert sent ({len(urls)} file(s)) for {sender}")
        except Exception as e:
            print(f"? Failed to send plumber alert: {e}")

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
        print(f"? Plumber alert timer reset for {sender} (accumulated {len(_plumber_alert_pending[sender])} file(s))")


def get_random_delay() -> int:
    minutes = random.randint(1, 5)
    seconds = minutes * 1
    print(f"?? Random delay: {minutes} minute(s)")
    return seconds


def delayed_response(sender, reply, delay_seconds, message_id=None, cancel_event=None):
    try:
        # Sleep in short chunks so a cancel_event can interrupt the wait quickly.
        _POLL = 5  # seconds between cancellation checks
        slept = 0
        while slept < delay_seconds:
            if cancel_event and cancel_event.is_set():
                print(f"🚫 Delayed send cancelled for {sender} — superseded by a new message")
                return
            chunk = min(_POLL, delay_seconds - slept)
            time.sleep(chunk)
            slept += chunk

        if cancel_event and cancel_event.is_set():
            print(f"🚫 Delayed send cancelled for {sender} — superseded by a new message")
            return

        # Clear the registry entry now that we're about to send (prevents stale cancellation).
        with _pending_send_lock:
            if _pending_send_events.get(sender) is cancel_event:
                _pending_send_events.pop(sender, None)

        # Abort if the appointment was confirmed during the delay window
        try:
            fresh = Appointment.objects.filter(
                phone_number=f"whatsapp:+{sender}"
            ).only('status').first()
            if fresh and fresh.status == 'confirmed':
                print(f"⚠️ Aborting delayed reply to {sender} — appointment already confirmed")
                return
        except Exception:
            pass  # DB unavailable — proceed with send rather than silently drop

        if message_id:
            try:
                whatsapp_api.mark_message_as_read(message_id)
            except Exception as e:
                print(f"⚠️ Could not mark as read before reply: {e}")
        if not reply or not reply.strip():
            print(f"⚠️ Skipping empty reply to {sender} — no message to send")
            return
        whatsapp_api.send_text_message(sender, reply)
        preview = reply.replace('\n', ' ')[:120]
        print(f"🤖 Bot → +{sender}: {preview}{'…' if len(reply) > 120 else ''}")
    except Exception as e:
        print(f"❌ Error in delayed response: {str(e)}")

def detect_objection_type(message: str) -> str:
    message_lower = message.lower().strip()

    # Vague pricing / quotation triggers — catches Shona, English, mixed
    pricing_terms = [
        # English
        'how much', 'cost', 'price', 'expensive', 'quotation', 'quote',
        'estimate', 'invoice', 'i want a quote', 'send me a quote',
        'i want quotation', 'need a quote', 'need quotation',
        'how much is it', 'what is the cost', 'what does it cost',
        # Shona / mixed
        'marii', 'mari', 'mutengo', 'zvinodhura', 'inodhura', 'bhadhara',
        'zvese zvakadai', 'zvese izvi', 'zvakadai', 'how much zvese',
        'quotation', 'invoice',
    ]
    if any(k in message_lower for k in pricing_terms):
        return 'pricing'

    if any(k in message_lower for k in [
        'how long', 'duration', 'when finish',
        'nguva', 'rinopera riini', 'rinopedza riini', 'mangani mazuva'
    ]):
        return 'timeline'

    if any(k in message_lower for k in [
        'when can you', 'available', 'come',
        'munouya rini', 'mungauya rini', 'mauya rini'
    ]):
        return 'availability'

    return 'other'


def _explicitly_requests_price(message: str) -> bool:
    """
    Return True only when the customer clearly asks about pricing.
    """
    msg = (message or '').strip().lower()
    if not msg:
        return False

    price_markers = (
        'price', 'pricing', 'cost', 'quote', 'quotation', 'how much',
        'how much is', 'how much are', 'charges', 'charge', 'rate', 'rates',
        'mutengo', 'marii', 'mari', 'zvinodhura', 'inodhura', 'bhadhara',
    )
    return any(marker in msg for marker in price_markers)


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
    """
    Return True ONLY when the customer's PRIMARY intent is to see previous work photos.
    Returns False when the message also contains a stronger pricing or product signal —
    in that case the pricing path should handle the message instead.

    Uses DeepSeek for accurate intent detection with a fast keyword pre-filter.
    """
    try:
        message_clean = (message or "").strip().lower()

        # Fast-path: ignore tiny acks
        if len(message_clean) <= 4 or message_clean in {
            "ok", "okay", "k", "thanks", "thank you", "cool", "fine"
        }:
            return False

        # Fast-path: if message contains a clear pricing signal, pricing wins
        # regardless of whether a photo word also appears
        pricing_signals = (
            'how much', 'price', 'cost', 'quote', 'quotation',
            'marii', 'mari', 'mutengo', 'zvinodhura', 'inodhura',
            'zvese', 'how much shud', 'how much should',
        )
        has_pricing_signal = any(p in message_clean for p in pricing_signals)

        # Photo-only keywords — words that on their own strongly suggest a photo request
        photo_primary_keywords = (
            'send photo', 'send photos', 'send pic', 'send pics',
            'show me', 'show your work', 'show me your', 'got photos',
            'got pics', 'got pictures', 'previous work', 'portfolio',
            'your work', 'examples of', 'ndiratidze', 'ratidza basa',
            'basa renyu', 'ndiona basa', 'mifananidzo yebasa',
        )
        has_strong_photo_signal = any(p in message_clean for p in photo_primary_keywords)

        # Weak photo keywords — only count if no pricing signal present
        photo_weak_keywords = (
            'photo', 'photos', 'picture', 'pictures', 'pic', 'pics',
            'pix', 'image', 'images', 'papic', 'mufananidzo', 'mifananidzo',
            'tumira', 'ndione', 'catalogue',
        )
        has_weak_photo_signal = any(p in message_clean for p in photo_weak_keywords)

        # Decision without DeepSeek (fast path)
        if has_pricing_signal and not has_strong_photo_signal:
            # Pricing wins — don't classify as photo request
            print(f"📊 Photo check fast-path: pricing signal dominates '{message_clean[:60]}'")
            return False

        if not has_strong_photo_signal and not has_weak_photo_signal:
            # No photo keywords at all — skip DeepSeek
            return False

        # If only weak photo signal exists alongside pricing, pricing wins without DeepSeek
        if has_pricing_signal and has_weak_photo_signal and not has_strong_photo_signal:
            print("📊 Photo check: weak photo word + pricing signal → pricing wins")
            return False

        # DeepSeek for ambiguous cases (strong photo signal, or no pricing signal)
        from openai import OpenAI
        deepseek_client = OpenAI(
            api_key=os.environ.get('DEEPSEEK_API_KEY'),
            base_url="https://api.deepseek.com/v1"
        )

        response = deepseek_client.chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a message intent classifier for a Zimbabwean plumbing company. "
                        "Customers write in English, Shona, or a mix. "
                        "Reply with ONLY 'YES' or 'NO', nothing else."
                    )
                },
                {
                    "role": "user",
                    "content": f"""Is the customer's PRIMARY intent to see photos or pictures of previous plumbing work?

IMPORTANT: If the message ALSO asks about price/cost/how much, the answer is NO — pricing is the primary intent.

Say YES only when the customer is mainly asking to see photos/pictures/examples of past work, with no pricing question in the same message.

Examples where answer is NO (pricing dominates):
- "papic how much" → NO (asking price, photo word is incidental)
- "show me pics and how much" → NO (pricing present)
- "send photos and quote" → NO (pricing present)
- "how much shud i have, papic" → NO (pricing is primary)
- "pics and price" → NO

Examples where answer is YES (photo is primary):
# Direct English Requests
- "send me photos of your work"
- "do you have pictures"
- "show me your previous jobs"
- "got any pics of bathrooms you've done"
- "may you kindly share your catalogue"
- "where can I view your portfolio"
- "let me see your previous work around Harare"
- "can I see some of the jobs you've done"
- "do you have a portfolio of your plumbing work"
- "kindly share pics of your previous installations"
- "I want to see the quality of your pipe work first"
- "any before and after photos of bathrooms you've renovated"
- "show me examples of your plumbing jobs"
- "have you got a gallery I can look at"
- "Can I see"
- "send through some images of your past projects"
- "I'd like to see what your work looks like"
- "can you share some recent jobs you've completed"
- "do you have photos of similar work you've done"
- "let me see samples of your craftsmanship"
- "what does your work look like"
- "can you show me what I'm getting"
- "I need to see proof of your work"

# Shona Only
- "ndiratidze mifananidzo"
- "ndoda kuona mapic ebasa renyu"
- "tumirai mapics ekitchen ne bathroom"
- "mune catalogue here"
- "ndiratidzei solar geyser installations"
- "ndiratidzei basa renyu"
- "mune mifananidzo here yebasa"
- "ndingawana mifananidzo here"
- "tumirai mifananidzo yebasa renyu"
- "ndirikuda kuona mabasa amakaita"
- "munayo mifananidzo yemabathroom here"
- "ratidzai basa renyu rekupayipa"
- "ndiratidzei ma geyser amakamboisa"
- "mifananidzo yebasa renyu irikupi"
- "ndoda kuona kugona kwenyu"
- "pane patingaona basa renyu here"
- "munotumira mapikicha here e previous work"
- "ndiratidzei mabasa amakaita kuHarare"
- "mune mapikicha ekicheni here"
- "ndiratidzei zvamunoita"

# Mixed Shona/English (Sheng/Slang)
- "hesi ndione basa renyu papic"
- "tumirai mapic ework yenyu"
- "pane patingaona mapics emabathroom"
- "munotumira here ma pics e previous jobs"
- "send mapic e plumbing yenyu"
- "ndoda kuona quality yenyu yekupayipa papic"
- "mune ma sample pics here"
- "ndiratidzei ma photos e work yamakaita"
- "tumirai mapictures e geyser installation"
- "ndoda kuona proof yebasa"
- "pane mapic ekitchen renovations here"
- "ndingawane mapics ebasa renyu kuWhatsApp here"
- "sendai catalogue yenyu ndione"
- "mune status here yebasa renyu"
- "ndoda kuona mapikicha ekuti munoita sei"

# Catalogue/Portfolio Terms (Zim Context)
- "do you have a catalogue on WhatsApp I can look at"
- "please send me your brochure or catalogue"
- "where can I view your work online or on Facebook"
- "do you have a Facebook page with your work"
- "send me your business profile with pictures"
- "I want to see your company profile"
- "do you have an Instagram for your plumbing work"
- "where do you post your completed jobs"
- "send me the link to your work photos"
- "can you forward me your portfolio on WhatsApp"
- "do you have a catalog for your services"
- "share your catalogue ndione"

# Specific Work Types (Harare Context)
- "have you done any solar geyser installations show me"
- "I want to see how you do kitchen sink plumbing"
- "show me a bathroom you tiled and plumbed in Borrowdale"
- "can I see pictures of outside drains or manholes you've fixed"
- "got any pics of borehole to tank connections you've done"
- "show me your work on burst pipe repairs"
- "have you installed any JoJo tanks with pumps show me"
- "I want to see toilet installations you've done"
- "show me how you do kitchen sink traps"
- "pictures of bathroom renovations you've completed"
- "any photos of geyser drip tray installations"
- "show me mixer installations in showers"
- "got pics of pressure pump setups"
- "bathroom plumbing and tiling pics please"
- "show me how you run pipes in the ceiling"
- "any examples of outside tap installations"
- "pics of water heater installations"
- "show me your work on sewer line repairs"
- "got examples of manhole covers you've done"
- "I want to see kitchen plumbing with dishwasher connections"

# Area/Location Based (Harare Suburbs)
- "let me see work you've done in Borrowdale"
- "any jobs in Mount Pleasant I can look at"
- "show me what you did in Avondale"
- "got pics of work in Greendale"
- "I'm in Chisipite show me local jobs"
- "have you worked in Glen Lorne show me"
- "any pics from jobs in the Avenues"
- "show me what you've done around the CBD"
- "work in Hatfield I can see"
- "got examples from Highlands"
- "I want to see jobs you've done in my area"
- "any work in my neighborhood I can view"

# Follow-up/Context Based
- "I saw your number on a gate in Greendale got pics of that job"
- "my neighbor used you show me what you did for them"
- "I've seen your work before but let me see more"
- "you fixed a leak in my area last week got photos"
- "I want to see what you're capable of"
- "before I book I need to see your work quality"
- "show me what to expect"
- "let me see your standard of work"
- "I'm particular about neatness can I see examples"
- "show me how tidy your installations are"
- "I want to see if you do clean work"
- "demonstrate your quality with some photos"
- "show me why I should choose you"
- "what makes your work different show me"

# WhatsApp/App Specific
- "can you send pics on WhatsApp"
- "do you have a WhatsApp catalogue"
- "tumirai mapic paWhatsApp"
- "send your portfolio to this number"
- "forward me your work photos please"
- "share your gallery on WhatsApp"
- "can I see your WhatsApp status updates"
- "do you post work on your status"
- "send me voice note with pics"
- "whatsapp me some examples"
- "drop pics in my inbox"

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
        message_lower = (message or "").lower()
        # Conservative fallback: only return True for unambiguous photo-only messages
        pricing_fallback = (
            'how much', 'price', 'cost', 'quote', 'marii', 'mutengo', 'zvese'
        )
        if any(p in message_lower for p in pricing_fallback):
            return False
        photo_fallback = (
            'send photo', 'send pic', 'show me', 'previous work',
            'portfolio', 'your work', 'ndiratidze', 'basa renyu',
        )
        return any(kw in message_lower for kw in photo_fallback)


PREVIOUS_WORK_IMAGES_DIR = os.environ.get(
    'PREVIOUS_WORK_IMAGES_DIR',
    os.path.join(os.path.dirname(__file__), 'previous_work_photos')
)
CATALOGUE_IMAGES_DIR = os.environ.get(
    'CATALOGUE_IMAGES_DIR',
    os.path.join(os.path.dirname(__file__), 'catalogue_photos')
)
SUPPORTED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}


def get_catalogue_images() -> list:
    images = []
    if not os.path.exists(CATALOGUE_IMAGES_DIR):
        print(f"Catalogue images folder not found: {CATALOGUE_IMAGES_DIR}")
        return images
    for filename in sorted(os.listdir(CATALOGUE_IMAGES_DIR)):
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_IMAGE_EXTENSIONS:
            images.append(os.path.join(CATALOGUE_IMAGES_DIR, filename))
    print(f"Found {len(images)} catalogue images")
    return images


def send_catalogue_images(sender, appointment=None) -> bool:
    """
    Send product catalogue images to the customer.
    Returns True if images were queued, False if no images configured
    (caller should show the text-only price list as a fallback).
    """
    images = get_catalogue_images()
    if not images:
        print("No catalogue images found — text-only fallback will be used")
        return False

    def _send():
        try:
            time.sleep(1)  # let the text message arrive first
            sent_count = 0
            for index, image_path in enumerate(images):
                caption = "HomeBase Plumbers — product catalogue" if index == 0 else None
                whatsapp_api.send_local_image(sender, image_path, caption=caption)
                sent_count += 1
                time.sleep(0.5)
            if appointment:
                appointment.add_conversation_message(
                    "assistant", f"[MEDIA] Sent {sent_count} catalogue image(s)"
                )
            print(f"Sent {sent_count}/{len(images)} catalogue images to {sender}")
        except Exception as exc:
            print(f"Failed to send catalogue images: {exc}")

    threading.Thread(target=_send, daemon=True).start()
    return True


def get_previous_work_images() -> list:
    images = []
    if not os.path.exists(PREVIOUS_WORK_IMAGES_DIR):
        print(f"?? Previous work images folder not found: {PREVIOUS_WORK_IMAGES_DIR}")
        return images
    for filename in sorted(os.listdir(PREVIOUS_WORK_IMAGES_DIR)):
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_IMAGE_EXTENSIONS:
            images.append(os.path.join(PREVIOUS_WORK_IMAGES_DIR, filename))
    print(f"?? Found {len(images)} previous work images")
    return images


# -----------------------------------------------------------------------------
# FIX 3 — PREVIOUS WORK PHOTO DEDUP
# send_previous_work_photos now returns True ONLY after photos are confirmed
# queued; the caller must NOT send any fallback text when True is returned.
# -----------------------------------------------------------------------------

def send_previous_work_photos(sender, appointment=None):
    """
    Send previous work photos with a small delay between each image.
    Returns True if photos were queued (caller must NOT send additional text).
    Returns False if no images are configured (caller may send a text fallback).
    Photos are only sent once per 24-hour window per appointment to prevent duplicates.
    """
    if appointment is not None:
        from django.utils import timezone
        from datetime import timedelta
        last_sent = getattr(appointment, 'previous_work_photos_sent_at', None)
        if last_sent and (timezone.now() - last_sent) < timedelta(hours=24):
            print(f"Skipping previous work photos for {sender} - already sent within 24h")
            return True
    images = get_previous_work_images()
    if not images:
        print("No previous work images found - caller should handle fallback")
        return False
    if appointment is not None:
        from django.utils import timezone
        appointment.previous_work_photos_sent_at = timezone.now()
        appointment.save(update_fields=['previous_work_photos_sent_at'])
    intro = "Here are some examples of our previous plumbing work!"
    def send_images_with_delay():
        try:
            delay_seconds = get_random_delay()
            print(f"Waiting {delay_seconds // 60} minute(s) before sending images to {sender}")
            time.sleep(delay_seconds)
            whatsapp_api.send_text_message(sender, intro)
            sent_count = 0
            for index, image_path in enumerate(images):
                caption = "Our previous work - high quality plumbing & renovations" if index == 0 else None
                whatsapp_api.send_local_image(sender, image_path, caption=caption)
                sent_count += 1
                time.sleep(0.5)
            follow_up = "Those are some of our recent jobs. Anything there you'd like for your bathroom? We can do a free site visit to show you exactly what's possible in your space."
            time.sleep(1)
            whatsapp_api.send_text_message(sender, follow_up)
            if appointment:
                appointment.add_conversation_message("assistant", intro)
                appointment.add_conversation_message(
                    "assistant", f"[MEDIA] Sent {sent_count} previous work image(s)"
                )
                appointment.add_conversation_message("assistant", follow_up)
            print(f"Sent {sent_count}/{len(images)} previous work images to {sender}")
        except Exception as e:
            print(f"Failed to send images: {str(e)}")
    threading.Thread(target=send_images_with_delay, daemon=True).start()
    return True

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
            'bathroom_renovation':    'US$1,500 - US$6,000',
            'bathroom_installation':  'US$1,800 - US$7,000',
            'kitchen_renovation':     'US$3,000 - US$12,000',
            'kitchen_installation':   'US$3,500 - US$14,000',
            'new_plumbing_installation': 'US$700 - US$8,000',
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


# -----------------------------------------------------------------------------
# Webhook entry points
# -----------------------------------------------------------------------------

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
            print("? Webhook verified successfully")
            return HttpResponse(challenge, content_type='text/plain')
        print("? Webhook verification failed")
        return HttpResponse(status=403)
    except Exception as e:
        print(f"? Webhook verification error: {str(e)}")
        return HttpResponse(status=500)


def handle_webhook_event(request):
    try:
        body = json.loads(request.body.decode('utf-8'))


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
        return HttpResponse(status=200)  # 🔥 prevent retry loop

def process_webhook_in_background(body):
    try:
        for entry in body.get('entry', []):
            for change in entry.get('changes', []):
                if change.get('field') == 'messages':
                    
                    value = change.get('value', {})  # ✅ DEFINE VALUE HERE
                    
                    process_message_change(value)

    except Exception as e:
        print(f"❌ Background processing error: {str(e)}")

def process_message_change(value):
    try:
        # ✅ 1. HANDLE STATUSES FIRST AND EXIT
        statuses = value.get('statuses', [])
        if statuses:
            process_status_updates(statuses)
            return  # 🔥 CRITICAL FIX — stops the loop

        # ✅ 2. HANDLE MESSAGES ONLY
        messages = value.get('messages', [])
        if not messages:
            return


        for message in messages:
            message_type = message.get('type')
            message_id   = message.get('id')
            sender       = message.get('from')

            # ✅ Guard against invalid/system messages
            if not sender:
                print("⚠️ Skipping message with no sender")
                continue

            if message_id:
                try:
                    WhatsAppInboundEvent.objects.create(
                        message_id=message_id,
                        sender=sender
                    )
                except IntegrityError:
                    print(f"Duplicate inbound message ignored: {message_id}")
                    continue

            print(f"📩 Processing message from {sender}, type: {message_type}")


            if message_type == 'text':
                handle_text_message(sender, message.get('text', {}), message_id=message_id)

            elif message_type == 'image':
                handle_media_message(sender, message.get('image', {}), 'image')

            elif message_type == 'document':
                handle_media_message(sender, message.get('document', {}), 'document')

            elif message_type in ('audio', 'voice'):
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
                print(f"⚠️ Unknown message type from {sender}: '{message_type}'")

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


            if error_text:
                print(f"❌ WhatsApp delivery [{status_name}] +{_clean_phone(recipient_id)}: {error_text}")


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
            print(f"? Failed to process status update: {status_err}")


def handle_location_message(sender, location_data):
    try:
        latitude = location_data.get('latitude')
        longitude = location_data.get('longitude')
        address = location_data.get('address')
        print(f"?? Location from {sender}: {latitude}, {longitude}")

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
                    "Thanks for the location pin! ??\n\n"
                    "Could you also type the area name? (e.g., Harare Hatfield, Harare Avondale)\n\n"
                    "This helps us serve you better."
                )
                delay = get_random_delay()
                threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()
        else:
            response_msg = "Thanks for sharing your location! ??\n\nI've noted it. Let me continue with your appointment details..."
            delay = get_random_delay()
            threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()

    except Exception as e:
        print(f"? Error handling location: {str(e)}")


def handle_unsupported_media(sender, media_type):
    try:
        if is_chatbot_paused_for_sender(sender):
            print(f"Chatbot paused for whatsapp:+{sender}; skipping unsupported media auto response.")
            return
        print(f"?? Unsupported media type from {sender}: '{media_type}'")

        # Guard: these types have dedicated handlers — should NEVER reach here.
        # If they do it means process_message_change has a routing bug.
        if media_type in ('image', 'document', 'video', 'audio', 'voice'):
            print(
                f"?? WARNING: '{media_type}' was incorrectly routed to "
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
            f"We can't open that one — could you send a text or a photo instead?"
        )
        threading.Thread(
            target=delayed_response,
            args=(sender, response_msg, delay),
            daemon=True
        ).start()
    except Exception as e:
        print(f"? Error handling unsupported media: {str(e)}")


def handle_audio_message(sender, audio_data):
    try:
        if is_chatbot_paused_for_sender(sender):
            print(f"Chatbot paused for whatsapp:+{sender}; skipping audio auto response.")
            return
        print(f"?? Audio message from {sender}")

        phone_number = f"whatsapp:+{sender}"
        try:
            appointment = Appointment.objects.get(phone_number=phone_number)
        except Appointment.DoesNotExist:
            response_msg = (
                "Voice notes we can't read unfortunately — just type it out and we'll get you sorted 👍"
            )
            delay = get_random_delay()
            threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()
            return

        if appointment.plan_status == 'pending_upload':
            response_msg = (
                "That came through as a voice note — for the plans we need photos or a PDF. "
                "Send those when you're ready, or type \"done\" if you're finished."
            )
        else:
            response_msg = (
                "Voice notes we can't read — just type it out and we'll carry on from where we were 👍"
            )

        delay = get_random_delay()
        threading.Thread(target=delayed_response, args=(sender, response_msg, delay), daemon=True).start()

    except Exception as e:
        print(f"? Error handling audio: {str(e)}")


# -----------------------------------------------------------------------------
# MAIN TEXT HANDLER — all dedup logic lives here
# -----------------------------------------------------------------------------

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

        from .out_of_scope_handler import detect_delay_signal_message, mark_delay_signal

        delay_check = detect_delay_signal_message(message_body, appointment)
        if delay_check.get('is_delay'):
            mark_delay_signal(appointment, message_body)
        else:
            # Don't clear the delay signal for bare acks ("ok", "thanks", "👍").
            # A customer typing "ok" after the bot's farewell is acknowledging the
            # delay confirmation — treating it as re-engagement asks the next
            # qualification question (e.g. "What suburb are you in?").
            _DELAY_ACKS = {
                'ok', 'okay', 'k', 'kk', 'oky', 'oh ok', 'oh okay',
                'sharp', 'shap', 'sho', 'cool', 'nice', 'noted',
                'got it', 'alright', 'great', 'good', 'fine', 'sure', 'yes',
                'yep', 'yeah', 'yup', 'ok thanks', 'ok thank you',
                'thanks', 'thank you', 'thank u', 'thx', 'thnx',
                'understood', 'i see', 'ah ok', 'ah okay', 'ok cool',
                'ok bye', 'okay bye', 'bye', 'no worries',
                '👍', '🙏', '✅', '😊', 'bo', 'bho',
                'hongu', 'zvakanaka', 'maita basa', 'ndatenda',
            }
            _msg_norm = (message_body or '').strip().lower()
            if appointment.is_delayed and _msg_norm in _DELAY_ACKS:
                # Ack while delayed — keep the pause, save silently, no reply.
                print(f"🔇 Delay active — ack ignored at arrival: '{message_body[:60]}'")
            else:
                # Customer re-engaged with a substantive message — clear the pause.
                _clear_delay_signal_if_present(appointment)

        # Auto-classify service type from the customer's message
        if not appointment.project_type:
            from .service_type_classifier import classify_and_save
            classify_and_save(appointment, message_body)

        previous_status = appointment.lead_status
        _, new_status = refresh_lead_score(appointment)
        if new_status != previous_status and new_status in {LeadStatus.HOT, LeadStatus.VERY_HOT}:
            notify_admin_of_priority_lead(appointment, sender)

        # Queue the message — if another arrives within MESSAGE_BATCH_WINDOW_SECONDS the
        # timer resets, and one combined reply handles both concerns together.
        _enqueue_for_response(sender, message_body, message_id)

    except Exception as e:
        print(f"Error handling text: {str(e)}")
        import traceback
        traceback.print_exc()


def _enqueue_for_response(sender: str, message_body: str, message_id):
    """Add message to the per-sender batch queue and reset the debounce timer.

    Also cancels any delayed send already in flight — the next batch will generate
    a single reply that covers all unanswered messages via conversation history.
    """
    # Cancel a pending send if one is sleeping (msg arrived during the send delay window).
    with _pending_send_lock:
        old_event = _pending_send_events.pop(sender, None)
        if old_event is not None:
            old_event.set()
            print(f"🚫 Pending send cancelled for {sender} — will be handled in next batch")

    with _pending_batch_lock:
        if sender not in _pending_batches:
            _pending_batches[sender] = []
        _pending_batches[sender].append((message_body, message_id))
        count = len(_pending_batches[sender])

        existing = _pending_batch_timers.pop(sender, None)
        if existing is not None:
            existing.cancel()
            print(f"🔄 Batch timer reset for {sender} — {count} message(s) pending")
        else:
            print(f"⏳ Batch timer started for {sender} ({MESSAGE_BATCH_WINDOW_SECONDS}s)")

        timer = threading.Timer(MESSAGE_BATCH_WINDOW_SECONDS, _flush_text_batch, args=(sender,))
        timer.daemon = True
        _pending_batch_timers[sender] = timer
        timer.start()


def _flush_text_batch(sender: str):
    """Timer callback — drain the queue and generate one reply covering all messages."""
    with _pending_batch_lock:
        batch = _pending_batches.pop(sender, [])
        _pending_batch_timers.pop(sender, None)

    if not batch:
        return

    messages = [body for body, _ in batch]
    last_message_id = batch[-1][1]

    if len(messages) == 1:
        combined = messages[0]
        print(f"📤 Batch flush: 1 message for {sender}")
    else:
        combined = "\n".join(messages)
        print(f"📦 Batch flush: {len(messages)} messages combined for {sender} → '{combined[:120]}'")

    _generate_and_schedule_reply(sender, combined, last_message_id)


def _generate_and_schedule_reply(sender: str, message_body: str, message_id=None):
    """Generate a bot reply for message_body and schedule it with a 1-5 min send delay."""
    try:
        phone_number = f"whatsapp:+{sender}"
        appointment = Appointment.objects.filter(phone_number=phone_number).first()
        if not appointment:
            return

        if appointment.chatbot_paused:
            print(f"Chatbot paused for {phone_number}; skipping auto response.")
            return

        if appointment.status == 'confirmed' and is_post_booking_ack_message(message_body):
            print(f"Post-booking ack detected; no reply sent. sender={sender}, message='{message_body}'")
            return

        from .views import Plumbot
        plumbot = Plumbot(phone_number)

        reply = None

        # ── FAQ LAYER ─────────────────────────────────────────────────────────
        from bot.faq import lookup_faq
        _faq_reply = lookup_faq(message_body)
        if _faq_reply is not None:
            appointment.add_conversation_message("assistant", _faq_reply)
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response,
                args=(sender, _faq_reply, delay, message_id),
                daemon=True,
            ).start()
            return

        # ── UNIFIED PRE-CLASSIFIER ────────────────────────────────────────────
        from bot.unified_classifier import (
            unified_classify,
            uc_intent, uc_confidence, uc_product_intent,
            uc_is_photo_request, uc_is_plan_later, uc_is_repeat,
            uc_as_service_inquiry, uc_as_oos_classification,
        )
        from django.utils import timezone as _tz
        _uclass = unified_classify(
            message_body,
            appointment=appointment,
            conversation_history=appointment.conversation_history,
            today_date=_tz.now().strftime('%Y-%m-%d'),
        )
        _quick_service_check = uc_as_service_inquiry(_uclass)

        _is_clear_product_inquiry = (
            _quick_service_check.get('intent') not in ('none', 'pictures') and
            _quick_service_check.get('confidence') == 'HIGH'
        )
        _pricing_signals = (
            'how much', 'price', 'cost', 'quote', 'quotation',
            'marii', 'mari', 'mutengo', 'zvinodhura', 'zvese',
        )
        _has_pricing_signal = any(p in message_body.lower() for p in _pricing_signals)

        # -- STEP 1: Previous work photo request --------------------------------
        print(f"Checking photo request: '{message_body}'")
        if uc_is_photo_request(_uclass) and not _is_clear_product_inquiry and not _has_pricing_signal:
            print("Photo request detected")
            photos_queued = send_previous_work_photos(sender, appointment)
            if photos_queued:
                return
            fallback_reply = (
                "I can share previous-work photos, but they are not configured yet. "
                "Please ask our team and we will send them shortly."
            )
            appointment.add_conversation_message("assistant", fallback_reply)
            delay = get_random_delay()
            threading.Thread(target=delayed_response, args=(sender, fallback_reply, delay), daemon=True).start()
            return

        # -- STEP 1b: Out-of-scope / delay / complaint --------------------------
        from .out_of_scope_handler import handle_out_of_scope
        oos_reply = handle_out_of_scope(
            message_body, appointment,
            precomputed=uc_as_oos_classification(_uclass),
        )
        if oos_reply is not None:
            appointment.add_conversation_message("assistant", oos_reply)
            appointment.last_outbound_at = timezone.now()
            appointment.last_contacted_at = appointment.last_outbound_at
            appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
            delay = get_random_delay()
            threading.Thread(
                target=delayed_response, args=(sender, oos_reply, delay, message_id), daemon=True
            ).start()
            return

        # -- STEP 2: Service-specific pricing inquiry ---------------------------
        any_pricing_sent = (
            getattr(appointment, 'pricing_overview_sent', False) or
            bool(appointment.sent_pricing_intents) or
            getattr(appointment, 'previous_work_photos_sent_at', None) is not None
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
            ) or
            (appointment.followup_count > 0) or
            (appointment.conversation_history and len(appointment.conversation_history) > 4)
        )

        print(f"Checking service inquiry: '{message_body}'")
        inquiry = _quick_service_check
        print(f"Service inquiry result: {inquiry}")

        PRODUCT_INTENTS = {
            'tub_sales', 'standalone_tub', 'geyser', 'shower_cubicle',
            'vanity', 'bathtub_installation', 'toilet', 'chamber',
            'facebook_package', 'location_ask', 'location_visit',
            'previous_quotation', 'pictures', 'combined_pricing',
        }
        NON_PRICING_AUTO_REPLY_INTENTS = {
            'location_ask', 'location_visit', 'previous_quotation', 'pictures',
            'combined_pricing',
        }
        PRICING_AUTO_REPLY_INTENTS = {
            'geyser', 'shower_cubicle', 'vanity', 'toilet', 'chamber',
            'drain_unblocking', 'pipe_repair', 'geyser_repair', 'toilet_repair',
            'facebook_package',
        }
        intent = inquiry.get('intent')
        price_requested = _explicitly_requests_price(message_body)

        _is_specific_product_inquiry = (
            intent in PRICING_AUTO_REPLY_INTENTS and
            inquiry.get('confidence') == 'HIGH'
        )
        should_bypass_mid_conversation_gate = (
            intent in NON_PRICING_AUTO_REPLY_INTENTS or
            price_requested or
            _is_specific_product_inquiry
        )

        if mid_conversation and not should_bypass_mid_conversation_gate:
            print("Skipping service inquiry reply - mid-conversation and no explicit info/price request")
        elif intent != 'none' and (
            inquiry.get('confidence') == 'HIGH' or intent in PRODUCT_INTENTS
        ):
            if (intent not in NON_PRICING_AUTO_REPLY_INTENTS and
                    intent not in PRICING_AUTO_REPLY_INTENTS and
                    not price_requested):
                print(f"Skipping priced service inquiry for intent: {intent} - no explicit price request")
            else:
                already_sent = _has_sent_pricing_for_intent(appointment, intent)
                if already_sent and intent != 'combined_pricing':
                    print(f"Skipping already-sent service inquiry: {intent}")
                else:
                    if not already_sent:
                        print(f"Service inquiry matched (first time): {intent}")
                        _mark_pricing_intent_sent(appointment, intent)
                    else:
                        print(f"Re-sending combined pricing reply for: {intent}")
                    reply = plumbot.handle_service_inquiry(intent, message_body)

        # -- STEP 3: Full pricing overview --------------------------------------
        if reply is None:
            objection_type = detect_objection_type(message_body)
            print(f"Objection type: {objection_type}")

            if objection_type == 'pricing':
                _ITEM_CONTEXT = {
                    'vanity':   'vanity',
                    'geyser':   'geyser',
                    'shower':   'shower_cubicle',
                    'cubicle':  'shower_cubicle',
                    'tub':      'tub_sales',
                    'bathtub':  'tub_sales',
                    'toilet':   'toilet',
                    'chamber':  'chamber',
                    'drain':    'drain_unblocking',
                    'pipe':     'pipe_repair',
                }
                _recent = appointment.conversation_history or []
                _recent_text = ' '.join(
                    m.get('content', '') for m in _recent[-6:]
                    if m.get('role') == 'user'
                ).lower()
                for _keyword, _intent in _ITEM_CONTEXT.items():
                    if _keyword in _recent_text:
                        print(f"Pricing context match: {_keyword} → {_intent}")
                        reply = plumbot.handle_service_inquiry(_intent, message_body)
                        break

            if reply is None and objection_type == 'pricing' and _is_genuine_pricing_question(message_body, appointment):
                reply = plumbot.generate_pricing_overview(message_body)
                appointment.pricing_overview_sent = True
                appointment.save(update_fields=['pricing_overview_sent'])
            elif reply is None and objection_type == 'pricing' and getattr(appointment, 'pricing_overview_sent', False):
                reply = (
                    "Our Facebook package is US$600 — freestanding tub and side chamber. "
                    "We'll give you a fixed price once we've seen the space. "
                    f"{plumbot._get_pricing_followup_prompt('english')}"
                )

        # -- STEP 3b: Repeated-question detection ------------------------------
        if reply is None and uc_is_repeat(_uclass):
            repeat_info = detect_repeated_question(
                message_body,
                appointment.conversation_history or [],
            )
            if repeat_info:
                print(f"Repeated question detected — matched: '{repeat_info['matched_question'][:60]}'")
                lang = detect_language_simple(message_body)
                plumber_contact = (
                    getattr(appointment, 'plumber_contact_number', None)
                    or '+263774819901'
                )
                reply = generate_repeat_clarification(
                    new_message=message_body,
                    matched_question=repeat_info['matched_question'],
                    matched_answer=repeat_info['matched_answer'],
                    plumber_number=plumber_contact,
                    language_hint=lang,
                )

        # -- STEP 4: Normal Plumbot processing ---------------------------------
        if reply is None:
            print("Running normal Plumbot processing")
            reply = plumbot.generate_response(
                message_body,
                precomputed_service_inquiry=inquiry,
                precomputed_classification=_uclass,
            )

        if reply is None:
            print("🔇 Conversation complete — no reply sent")
            return

        appointment.add_conversation_message("assistant", reply)
        appointment.last_outbound_at = timezone.now()
        appointment.last_contacted_at = appointment.last_outbound_at
        appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
        print("Assistant reply saved to conversation history")

        delay = get_random_delay()
        cancel_event = threading.Event()
        with _pending_send_lock:
            _pending_send_events[sender] = cancel_event
        print(f"Random delay: {delay // 60} minute(s)")
        threading.Thread(
            target=delayed_response,
            args=(sender, reply, delay, message_id, cancel_event),
            daemon=True,
        ).start()
        print(f"Response scheduled for {delay // 60} minute(s) from now")

    except Exception as e:
        print(f"Error generating reply: {str(e)}")
        import traceback
        traceback.print_exc()


# -----------------------------------------------------------------------------
# Media handler (unchanged logic, kept intact)
# -----------------------------------------------------------------------------

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
                print(f"? Downloaded {len(file_bytes)} bytes from WhatsApp (id={media_id})")
            except Exception as dl_err:
                print(f"? Failed to download media from WhatsApp: {dl_err}")

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

                print(f"? Media saved: {saved_path}")
                print(f"? File URL: {file_url}")

                if media_type in ('image', 'document'):
                    file_note = f"\n[FILE UPLOADED] {saved_path} | URL: {file_url} | {timezone.now().isoformat()}"

                    # Atomic append to internal_notes — safe under concurrent writes
                    Appointment.objects.filter(pk=appointment.pk).update(
                        internal_notes=Concat('internal_notes', Value(file_note)),
                    )

                    # Only advance plan_status when the customer was explicitly asked to
                    # upload a plan (pending_upload). Any other image (e.g. a product photo
                    # sent mid-conversation) must NOT flip the state to plan_uploaded,
                    # because that routes all future text messages to handle_post_upload_messages
                    # and produces the wrong canned "Your plan has been sent" reply.
                    Appointment.objects.filter(
                        pk=appointment.pk, plan_status='pending_upload'
                    ).update(
                        plan_status='plan_uploaded',
                        plan_uploaded_at=timezone.now(),
                    )

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
                print(f"? Failed to save media to storage: {save_err}")
                import traceback
                traceback.print_exc()

        appointment.add_conversation_message("user", f"[Sent {media_type}]")

        if not appointment.chatbot_paused:
            _schedule_media_ack(sender, appointment, media_type)
        else:
            print(f"Chatbot paused for whatsapp:+{sender}; skipped media acknowledgment.")

    except Exception as e:
        print(f"? Error handling media: {str(e)}")
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
            model=settings.DEEPSEEK_MODEL,
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
            max_tokens=150
        )

        summary = response.choices[0].message.content.strip()
        print("? AI conversation summary generated")
        return summary

    except Exception as e:
        print(f"? AI summary generation failed: {str(e)}")
        try:
            fallback_lines = []
            for msg in appointment.conversation_history[-3:]:
                role = "Customer" if msg.get('role') == 'user' else "Bot"
                content = msg.get('content', '')[:150]
                fallback_lines.append(f"{role}: {content}")
            return "Summary unavailable. Last messages:\n" + "\n".join(fallback_lines)
        except Exception:
            return "Summary unavailable."


