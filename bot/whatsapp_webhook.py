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
from .models import Appointment, WhatsAppInboundEvent, LeadStatus, Tenant, TenantWhatsAppChannel
from .plumber_notifications import send_plumber_notification_email
from .utils import business_name_for
from django.utils import timezone
from django.utils.dateparse import parse_datetime
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
    detect_language,
)
from .views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER
from .pricing_copy import is_tenant_item_intent

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
MESSAGE_BATCH_WINDOW_SECONDS = 45 # wait this long after the LAST message before generating a reply

# Per-sender cancel events for delayed sends still in their sleep window.
# When a new message arrives, the event is set so the sleeping thread aborts
# instead of sending a now-stale reply. The next batch covers everything.
_pending_send_events: dict = {}     # sender -> threading.Event
_pending_send_lock = threading.Lock()

# Reply pacing — we answer at the lead's own tempo. How long they took to reply
# to our last message sets how long we take to reply to theirs, measured on top
# of the batch window (which has always elapsed by the time a delay is picked).
LEAD_INSTANT_REPLY_SECONDS = 60    # under this = they're typing at us in real time
LEAD_FAST_REPLY_SECONDS = 5 * 60   # under this = they're live in the chat
FAST_REPLY_DELAY_MINUTES = (1, 2)  # live lead: batch window + 1 min, then 2, alternating
SLOW_REPLY_DELAY_MINUTES = 5       # slow lead: batch window + 5 min
# An opener is not a reply — there is nothing to measure it against. Pace it in
# the fast band but never the instant one, so a first contact still reads as a
# person answering rather than an auto-responder firing back.
OPENER_LATENCY_SECONDS = float(LEAD_INSTANT_REPLY_SECONDS)
_lead_reply_latency: dict = {}     # sender -> seconds the lead took to reply
_fast_reply_turn: dict = {}        # sender -> count of fast replies paced so far
_lead_latency_lock = threading.Lock()

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
9. If a sentence is already very short and idiomatic (e.g. "Sharp!"), keep it or use a natural Shona equivalent.

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

def is_chatbot_paused_for_sender(sender: str, tenant=None) -> bool:
    """Is the bot paused for this lead, on this tenant?

    Must be tenant-scoped: pausing is a per-conversation decision. Unscoped, one
    tenant taking a lead over by hand silenced every other tenant's bot for the
    same handset (and vice versa — a stale row could un-pause a live handover).
    """
    phone_number = f"whatsapp:+{sender}"
    leads = Appointment.objects.filter(phone_number=phone_number)
    if tenant is not None:
        leads = leads.for_tenant(tenant)
    appointment = leads.only('chatbot_paused').first()
    return bool(appointment and appointment.chatbot_paused)


def _plumber_wa_number(appointment) -> str:
    """The digits to WhatsApp this lead's plumber on: the per-lead override,
    else the OWNING TENANT's number. '' when neither is set — the caller skips
    the WhatsApp alert (the email alert still goes out).

    Both alert paths used to fall back to a hardcoded 263774819901, so every
    tenant's hot leads and plan uploads pinged Homebase's plumber.
    """
    number = ''
    if appointment is not None:
        number = (appointment.plumber_contact() or '') if hasattr(appointment, 'plumber_contact')             else (getattr(appointment, 'plumber_contact_number', '') or '')
    return number.replace('+', '').replace('whatsapp:', '').strip()


def notify_admin_of_priority_lead(appointment: Appointment, sender: str):
    from .test_console import is_test_sender
    if is_test_sender(sender):
        print(f"🧪 Test lead +{sender} — priority-lead alert muted")
        return
    if appointment.lead_status not in {LeadStatus.HOT, LeadStatus.VERY_HOT}:
        return

    plumber_number = _plumber_wa_number(appointment)
    customer_name = appointment.customer_name or 'Unknown customer'

    # CTWA ad lead: tell the plumber they can reply free-form until the 72h window
    # closes (after that a customer reply or template is needed).
    ad_line = ""
    if appointment.ctwa_window_open:
        local_close = timezone.localtime(appointment.ctwa_window_expires_at)
        ad_line = f"Ad lead: free reply window until {local_close.strftime('%a %d %b %H:%M')}\n"

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
        f"{ad_line}"
        f"Lead: {settings.SITE_URL}/appointments/{appointment.id}/"
    )
    try:
        if plumber_number:
            from .whatsapp_cloud_api import get_client_for_tenant
            get_client_for_tenant(appointment.tenant).send_text_message(plumber_number, message)
        else:
            print(
                f"No plumber WhatsApp number for tenant "
                f"{getattr(appointment.tenant, 'slug', None)!r} — priority-lead alert by email only"
            )
    except Exception as exc:
        print(f"Failed to notify admin for appointment {appointment.id}: {exc}")
    send_plumber_notification_email(
        subject=f"Priority lead update for {customer_name}",
        message=message,
        tenant=getattr(appointment, 'tenant', None),
    )


# Question scripts for the media acknowledgement, keyed by
# get_next_question_to_ask(). Wording is reused from ResponseMixin._FORWARD_BANK
# so a lead never hears two different phrasings of the same question.
_MEDIA_ACK_QUESTIONS = {
    'service_type':      "Could you describe what you'd like done? Just a few words is fine.",
    'project_description': "Could you describe what you'd like done? Just a few words is fine.",
    'area':              "Whereabouts are you based?",
    # Static dict, so the two-concrete-days close is not available here — use
    # the this-or-that timeframe pair instead of an open "when".
    'availability_date': "Are you looking to get this done this week, or a bit further out?",
    'availability_time': "What works better for you, morning or afternoon?",
}


def _media_ack_reply(appointment: "Appointment", media_type: str,
                     is_plan_document: bool = False) -> str:
    """
    Acknowledgement for an uploaded file plus the next OUTSTANDING booking
    question — never one we already have the answer to.

    The old copy was a fixed string that asked "could you describe what you'd
    like done" even when project_description was already captured, because it
    never consulted booking state. It also bypassed the semantic duplicate
    detector (that runs on the text path only) and then polluted the next turn,
    since the ack is logged as an assistant turn that _last_assistant_was_tiedown
    reads.

    Copy rules: no plumber name (a literal one is a homebase value that would
    reach another tenant's customer), casual visit framing, no emojis.
    """
    try:
        from .views import Plumbot
        plumbot = Plumbot(appointment.phone_number, tenant=appointment.tenant)
        # One Appointment instance per turn — a second copy saving later
        # resurrects state this handler already changed.
        plumbot.appointment = appointment
        next_q = plumbot.get_next_question_to_ask()
    except Exception as exc:
        print(f"Media ack could not resolve the next question: {exc}")
        next_q = None

    # We just LOOKED at their photo. Asking "could you describe what you'd like
    # done" after seeing a freestanding tub is the same absurdity as asking it
    # after they send the plan: name the fixtures back and confirm scope instead.
    seen_question = None
    try:
        seen = appointment.latest_image_description()
        if seen and next_q in ('service_type', 'project_description'):
            # Only fixtures we can NAME count. A photo of a cubicle also matches
            # 'tap', which would read as two items and bounce us to the generic
            # question — the customer sees one thing in that picture, not two.
            fams = {f for f in plumbot._product_families_in(seen)
                    if f in plumbot._FAMILY_DISPLAY}
            seen_question = plumbot._confirm_intent_question(fams)
            if seen_question is None and len(fams) == 1:
                only = plumbot._FAMILY_DISPLAY.get(next(iter(fams)))
                if only:
                    seen_question = (
                        f"Is it the {only} you're looking to get sorted?"
                    )
    except Exception as exc:
        print(f"Media ack could not read the photo description: {exc}")

    return _compose_media_ack(
        next_q, appointment.status, media_type, is_plan_document,
        seen_question=seen_question,
    )


def _description_is_a_plan(description: str) -> bool:
    """True when what vision saw is an architectural drawing rather than a photo
    of a space or a fixture.

    Deterministic (CLAUDE.md) and deliberately narrow: 'drawing' or 'layout'
    alone can describe a sketch of a tub, so a plan word must be paired with a
    plan context, or be unambiguous on its own ('blueprint', 'floor plan').
    """
    text = (description or '').lower()
    if not text:
        return False
    if any(t in text for t in (
            'floor plan', 'house plan', 'site plan', 'building plan',
            'blueprint', 'blue print', 'architectural',
    )):
        return True
    return 'plan' in text and any(
        t in text for t in ('drawing', 'layout', 'elevation', 'scale', 'not a photo'))


def _compose_media_ack(next_question, status: str, media_type: str,
                       is_plan_document: bool = False,
                       seen_question: str = None) -> str:
    """
    Pure copy builder — no DB, no network — so every branch is pinned in the
    TEST 0 gate. See _media_ack_reply for why the state matters.
    """
    if is_plan_document:
        ack = "Got the plan, thanks."
    elif media_type == 'video':
        ack = "Got the video, thanks."
    else:
        ack = "Got the photo, thanks."

    # Already committed, or nothing left to ask: acknowledge and stop. Never
    # re-pitch someone who has already booked.
    # A question built from what the photo actually showed beats the generic
    # "describe what you'd like done" — but only for the scope questions, never
    # for area/date/time, which the picture cannot answer.
    question = _MEDIA_ACK_QUESTIONS.get(next_question)
    if seen_question and next_question in ('service_type', 'project_description'):
        question = seen_question
    if status == 'confirmed' or next_question in (None, 'complete', 'name')             or not question:
        # A plan is sent to BE QUOTED — say what happens to it, don't just file
        # it for the visit. Prod: a customer's floor plan got "I'll have it ready
        # for when we come round" and their next message was "Quote those".
        if is_plan_document:
            return (f"{ack} I'll go through it and put a written quotation "
                    f"together for you.{MESSAGE_SPLIT_MARKER}"
                    "Anything you want included that isn't on the plan?")
        return f"{ack} I'll have it ready for when we come round."

    # Two messages, not one block: acknowledgement, a beat, then the question.
    return f"{ack}{MESSAGE_SPLIT_MARKER}{question}"


def _schedule_media_ack(sender: str, appointment: "Appointment", media_type: str,
                        is_plan_document: bool = False):
    """
    Debounced acknowledgement for an upload that arrived with NO caption.

    Goes out through delayed_response so each part is logged, WAMID-stamped and
    cancellable exactly like a normal reply. The old direct send left the ack
    unstamped, so a customer quoting it resolved to None.

    A CAPTIONED upload never reaches here; it routes through the main dispatcher.
    """
    def _send_ack():
        with _media_ack_lock:
            _media_ack_timers.pop(sender, None)

        # Re-check at FIRE time, not only when the photo arrived. The guard in
        # handle_media_message runs the instant the image lands — a text arriving
        # during this 8s debounce opens a batch AFTER that check, and the ack then
        # goes out alongside the batch reply. Prod 2026-08-23 (barmak-plumbing):
        # a lead sent a photo, typed "How much" a second later, and received
        # THREE messages — the ack, its question, and the price reply.
        with _pending_batch_lock:
            batch_open = bool(_pending_batches.get(sender))
        with _pending_send_lock:
            send_in_flight = _pending_send_events.get(sender) is not None
        if batch_open or send_in_flight:
            print(f"Media ack dropped for {sender} — a reply is already on its way")
            return

        try:
            fresh = Appointment.objects.for_tenant(appointment.tenant).get(
                phone_number=f"whatsapp:+{sender}")
        except Appointment.DoesNotExist:
            fresh = appointment

        reply = _media_ack_reply(fresh, media_type, is_plan_document)
        parts = [p.strip() for p in reply.split(MESSAGE_SPLIT_MARKER)]             if MESSAGE_SPLIT_MARKER in reply else [reply]
        parts = [p for p in parts if p]
        if not parts:
            return

        for _part in parts:
            fresh.add_conversation_message("assistant", _part)
        fresh.last_outbound_at = timezone.now()
        fresh.last_contacted_at = fresh.last_outbound_at
        fresh.save(update_fields=['last_outbound_at', 'last_contacted_at'])

        delay = get_random_delay(appointment.tenant, sender=sender)
        cancel_event = threading.Event()
        with _pending_send_lock:
            _pending_send_events[sender] = cancel_event
        print(f"Sending media ack to {sender} after {delay // 60}m delay "
              f"({len(parts)} part(s))")
        delayed_response(sender, parts, delay, None, cancel_event,
                         tenant=appointment.tenant)

    with _media_ack_lock:
        existing = _media_ack_timers.get(sender)
        if existing is not None:
            existing.cancel()
            print(f"Reset media ack timer for {sender}")

        timer = threading.Timer(MEDIA_DEBOUNCE_SECONDS, _send_ack)
        timer.daemon = True
        _media_ack_timers[sender] = timer
        timer.start()
        print(f"Media ack timer set for {sender} ({MEDIA_DEBOUNCE_SECONDS}s)")


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
            fresh = Appointment.objects.for_tenant(appointment.tenant).get(
                phone_number=f"whatsapp:+{sender}")
        except Appointment.DoesNotExist:
            fresh = appointment

        plumber_number = _plumber_wa_number(fresh)

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
            f"{settings.SITE_URL}/appointments/{fresh.id}/"
        )

        try:
            if not plumber_number:
                print(
                    f"No plumber WhatsApp number for tenant "
                    f"{getattr(fresh.tenant, 'slug', None)!r} — media alert skipped"
                )
                return
            from .whatsapp_cloud_api import get_client_for_tenant
            get_client_for_tenant(appointment.tenant).send_text_message(plumber_number, alert_message)
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


def _record_lead_reply_latency(sender: str, appointment) -> None:
    """Note how long this lead took to answer our last outbound message.

    Only the message that OPENS a batch counts — the rest of a burst are part of
    the same reply, not separate response times. Read back by get_random_delay.
    """
    with _pending_batch_lock:
        if _pending_batches.get(sender):
            return

    gap = None
    try:
        _spoke_before = False
        _fallback_ts = None
        for entry in reversed(appointment.conversation_history or []):
            if not isinstance(entry, dict) or entry.get('role') != 'assistant':
                continue
            _spoke_before = True
            # sent_at is when the customer actually SAW the message. `timestamp`
            # is when we generated it — minutes earlier, because the reply then
            # waited out its own send delay. Measuring from `timestamp` charges
            # our delay to the lead's thinking time and drags every lead into
            # the slow branch, so it is only a last-resort fallback for history
            # written before sent_at existed.
            sent_at = parse_datetime(entry.get('sent_at') or '')
            if sent_at is None:
                # Logged but never sent (a reply cancelled mid-wait by this very
                # message) — keep looking back for one that actually went out.
                if _fallback_ts is None:
                    _fallback_ts = parse_datetime(entry.get('timestamp') or '')
                continue
            if timezone.is_naive(sent_at):
                sent_at = timezone.make_aware(sent_at)
            gap = max(0.0, (timezone.now() - sent_at).total_seconds())
            break
        if gap is None and _fallback_ts is not None:
            if timezone.is_naive(_fallback_ts):
                _fallback_ts = timezone.make_aware(_fallback_ts)
            gap = max(0.0, (timezone.now() - _fallback_ts).total_seconds())
            print(f"Lead {sender}: no sent_at in history yet - measuring from generation time")
        if gap is None and not _spoke_before:
            # We've never messaged them — this is the lead opening the conversation.
            # They're as live as a lead ever gets, so pace it like a fast reply
            # rather than leaving them on the old up-to-5-minute wait.
            gap = OPENER_LATENCY_SECONDS
    except Exception as exc:
        print(f"Lead reply latency check failed for {sender}: {exc}")

    with _lead_latency_lock:
        if gap is None:
            # We spoke, but the timestamp was unusable — fall back to the old pacing.
            _lead_reply_latency.pop(sender, None)
        else:
            _lead_reply_latency[sender] = gap
            print(f"Lead {sender} replied after {int(gap)}s")


def get_random_delay(tenant=None, sender=None) -> int:
    # Per-tenant admin switch: off = send as soon as the reply is ready. Call
    # sites that sleep on the delay themselves must pass their tenant; the ones
    # that hand it to delayed_response are covered there (it always has the
    # tenant, even when the delay was computed without one).
    from .platform_flags import reply_delay_enabled
    if not reply_delay_enabled(tenant):
        print("? Reply delay OFF (admin switch) - sending immediately")
        return 0

    # Match the lead's tempo. The batch window has already elapsed by the time a
    # delay is picked, so these minutes land on top of it.
    with _lead_latency_lock:
        latency = _lead_reply_latency.get(sender) if sender else None

    if latency is None:
        minutes = random.randint(1, 5)
        print(f"Random delay: {minutes} minute(s)")
    elif latency < LEAD_INSTANT_REPLY_SECONDS:
        # They answered inside a minute — they are sitting in the chat right now.
        # The batch window alone is the whole wait; adding to it would stall a
        # conversation that is moving at speed.
        print(f"Lead replied in {int(latency)}s - batch window only, no added delay")
        return 0
    elif latency < LEAD_FAST_REPLY_SECONDS:
        # Alternate 1, 2, 1, 2 rather than picking at random: a live back-and-forth
        # reads more naturally when our turnaround varies on a steady beat than
        # when it can land on the same number several exchanges running.
        with _lead_latency_lock:
            turn = _fast_reply_turn.get(sender, 0)
            _fast_reply_turn[sender] = turn + 1
        minutes = FAST_REPLY_DELAY_MINUTES[turn % len(FAST_REPLY_DELAY_MINUTES)]
        print(f"Lead replied in {int(latency)}s - answering {minutes} min after the batch window")
    else:
        minutes = SLOW_REPLY_DELAY_MINUTES
        print(f"Lead took {int(latency)}s - answering {minutes} min after the batch window")
    return minutes * 60


def delayed_response(sender, reply, delay_seconds, message_id=None, cancel_event=None, tenant=None):
    try:
        # Phase 1.3: send with the owning tenant's client (falls back to the
        # env-credential singleton when tenant is None — correct for homebase
        # and every pre-threading call site).
        from .whatsapp_cloud_api import get_client_for_tenant
        client = get_client_for_tenant(tenant)
        print(
            f"🔎 delayed_response client pick — tenant="
            f"{getattr(tenant, 'slug', None)!r}(pk={getattr(tenant, 'pk', None)!r}) "
            f"-> client.phone_number_id={client.phone_number_id!r}"
        )

        def _leads():
            qs = Appointment.objects.filter(phone_number=f"whatsapp:+{sender}")
            return qs.for_tenant(tenant) if tenant is not None else qs

        # Snapshot the appointment status at the START of the delay window. The
        # abort guard below must only drop a reply that was generated while the
        # lead was still PENDING and then got confirmed mid-wait (a stale
        # pre-booking question). Post-booking replies — the "what name?" / "what
        # email?" asks — are generated when the status is ALREADY confirmed, and
        # must NOT be aborted, or the customer never receives them.
        try:
            _initial = _leads().only('status').first()
            was_confirmed_at_start = bool(_initial and _initial.status == 'confirmed')
        except Exception:
            was_confirmed_at_start = False

        # Web test console: deliver instantly so the browser sees the reply in
        # seconds rather than minutes, and never call Meta's read-receipt endpoint
        # with a fake WAMID.
        from .test_console import is_test_sender
        if is_test_sender(sender):
            delay_seconds = 0

        # This tenant's reply-delay switch is the authority on the wait, whoever
        # computed delay_seconds — the call sites resolve the tenant AFTER the
        # delay, so re-checking here is what makes the switch reliable per tenant.
        from .platform_flags import reply_delay_enabled
        if delay_seconds and not reply_delay_enabled(tenant):
            delay_seconds = 0

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

        # Abort only if the appointment got confirmed DURING the delay window —
        # i.e. this reply is a stale pre-booking question superseded by a booking.
        # Replies generated after confirmation (the name/email asks) are kept.
        try:
            fresh = _leads().only('status').first()
            if fresh and fresh.status == 'confirmed' and not was_confirmed_at_start:
                print(f"⚠️ Aborting delayed reply to {sender} — confirmed mid-wait (stale pre-booking reply)")
                return
        except Exception:
            pass  # DB unavailable — proceed with send rather than silently drop

        if message_id and not is_test_sender(sender):
            try:
                client.mark_message_as_read(message_id)
            except Exception as e:
                print(f"⚠️ Could not mark as read before reply: {e}")

        # reply may be a single string or a list of parts (a reply split into an
        # acknowledgement + the question — see MESSAGE_SPLIT_MARKER). Normalise to a
        # clean list; strip any stray marker so it can never reach the customer.
        parts = list(reply) if isinstance(reply, (list, tuple)) else [reply]
        parts = [
            str(p).replace(MESSAGE_SPLIT_MARKER, ' ').strip()
            for p in parts if p and str(p).strip()
        ]
        if not parts:
            print(f"⚠️ Skipping empty reply to {sender} — no message to send")
            return

        for _i, part in enumerate(parts):
            if _i > 0:
                # A short human "typing" gap between the acknowledgement and the
                # follow-up question so the two messages don't land as one block.
                time.sleep(random.randint(3, 7))
            send_result = client.send_text_message(sender, part)
            preview = part.replace('\n', ' ')[:120]
            print(f"🤖 Bot → +{sender}: {preview}{'…' if len(part) > 120 else ''}")

            # Stamp the send. Two things ride on this: the outbound WAMID, so a
            # later customer reply that highlights this message resolves back to
            # it; and sent_at, so reply-pacing measures the lead's thinking time
            # from when they actually SAW this — the entry was logged before the
            # delay above, so its timestamp is minutes too early. Unconditional:
            # a missing WAMID must not cost us the sent_at stamp.
            try:
                sent_wamid = (send_result or {}).get('messages', [{}])[0].get('id')
                appt = _leads().first()
                if appt:
                    appt.mark_message_sent("assistant", part, sent_wamid)
            except Exception as e:
                print(f"⚠️ Could not record outbound send: {e}")
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
    Return True when the customer is asking about pricing.

    Primary path is a DeepSeek classifier (catches typos, abbreviations, and
    Shona/English mixing). If DeepSeek is unavailable or returns nothing, fall
    back to keyword matching so price detection never goes completely dark.
    """
    msg = (message or '').strip().lower()
    if not msg:
        return False

    # ── Primary: DeepSeek intent classification ──
    from bot.services.clients import deepseek_detects_price_request
    ai = deepseek_detects_price_request(message)
    if ai is not None:
        return ai

    # ── Fallback: keyword match (DeepSeek down / empty) ──
    price_markers = (
        'price', 'pricing', 'cost', 'quote', 'quotation', 'how much',
        'how much is', 'how much are', 'charges', 'charge', 'rate', 'rates',
        'hw much', 'hw mch', 'hwmuch', 'how mch', 'howmuch',
        'mutengo', 'marii', 'mari', 'zvinodhura', 'inodhura', 'bhadhara',
    )
    from .message_normalizer import contains_any, search_any
    if contains_any(message, price_markers):
        return True
    # Catch abbreviated / misspelt "how much": "hw much", "howmuch", "hw mch"…
    import re
    return search_any(message, re.compile(r'\bh(?:o)?w\s*m(?:u)?ch\b'))


def _explicitly_requests_photos(message: str) -> bool:
    """
    True when the customer explicitly asks to see pictures/photos/catalogue —
    even if they also name products. Used so an explicit photo request always
    sends photos instead of being swallowed by the product-inquiry path.
    """
    import re
    msg = (message or '').lower()
    if not msg:
        return False
    markers = (
        r'\bpic\b', r'\bpics\b', r'\bpicture', r'\bphoto', r'\bimage',
        r'\bcatalog', r'\bcatalogue', r'\bportfolio', r'\bgallery',
        r'see your work', r'previous work', r'examples of', r'show me',
    )
    return any(re.search(m, msg) for m in markers)


# Plain-English definitions of the fixtures customers most often ask us to
# explain. Generic trade vocabulary, not tenant data — no prices, no brands, no
# business names — so it is safe for every tenant (CLAUDE.md: no Homebase value
# reaches another tenant's customer).
_FIXTURE_GLOSSARY = (
    (('mixer', 'mixer tap', 'mixa'),
     "A mixer is the tap that blends the hot and cold into one spout — the kind "
     "you fit over a basin, a bath or a shower."),
    (('pedestal',),
     "A pedestal is the ceramic stand a basin sits on — it carries the basin and "
     "hides the pipework running down to the floor."),
    (('cistern',),
     "The cistern is the tank behind or above the toilet pan that holds the "
     "flush water."),
    (('chamber',),
     "A chamber toilet is the wall-mounted type, where the cistern is built into "
     "the wall behind the pan so only the pan and the flush plate show."),
    (('vanity', 'vanity unit'),
     "A vanity is the cabinet the basin sits in or on, so you get storage under "
     "the sink instead of open pipework."),
    (('cubicle', 'shower cubicle'),
     "A cubicle is the enclosed shower — the glass panels and door that keep the "
     "water in, with the tray or the tiled floor underneath."),
    (('geyser',),
     "A geyser is the hot water tank that heats and stores the water for the "
     "taps and the shower."),
    (('trap', 'p trap', 's trap'),
     "A trap is the bend in the waste pipe under a basin or a sink — it holds a "
     "little water so drain smells can't come back up."),
)

_DEFINITION_ASKS = (
    'what is', "what's", 'whats', 'what are', 'what does', 'meaning of',
    'define', 'explain', 'chii', 'chii chinonzi', 'zvinorevei',
)


def _definition_answer(message: str) -> str:
    """A one-line answer when the customer asks what a fixture IS, or None.

    Deterministic (CLAUDE.md keeps short/fuzzy strings off the LLM). It exists
    because an explicit photo request wins the router outright: prod answered
    "What's a mixer can I have a pic" with fifteen gallery photos and never said
    what a mixer is.
    """
    import re
    msg = (message or '').lower().strip()
    if not msg or not any(a in msg for a in _DEFINITION_ASKS):
        return None
    for terms, answer in _FIXTURE_GLOSSARY:
        if any(re.search(rf'\b{re.escape(t)}\b', msg) for t in terms):
            return answer
    return None


def _explicitly_requests_catalogue(message: str) -> bool:
    """
    True when the customer asks for the PRODUCT range / catalogue together with
    pricing — e.g. "send your products and prices", "what products do you have",
    "send catalogue and prices", "product list", "price list". These should get
    the product catalogue images AND the price list alongside, not a single
    product's price line. Distinct from a previous-work photo request (past jobs).
    """
    import re
    msg = (message or '').lower()
    if not msg:
        return False
    patterns = (
        r'\bproducts?\b.*\bprices?\b',           # "products and prices"
        r'\bprices?\b.*\bproducts?\b',           # "prices and products"
        r'\b(?:send|share|show)\b.*\bproducts?\b',  # "send your products"
        r'\b(?:your|what|which)\s+products?\b',  # "your products", "what products"
        r'\bproduct list\b',
        r'\bprice ?list\b',
        r'\bcatalog(?:ue)?\b.*\bprices?\b',      # "catalogue and prices"
    )
    return any(re.search(p, msg) for p in patterns)


def _mentions_wall_hung_toilet(message: str) -> bool:
    """True when the customer names a wall-mounted / wall-hung / concealed
    toilet system. That job IS the chamber install (owner: "toilet installation
    is the same as chamber" — from US$160 all-in), never toilet-seat pricing:
    a prod lead asking to install "a wall mounted toilet system" was quoted the
    US$70 seat block. Deterministic on purpose; shared by the keyword resolver
    here and _correct_service_intent in response_mixin.
    """
    import re
    msg = (message or '').lower()
    return bool('toilet' in msg and re.search(
        r'wall[\s-]*(?:mount|hung|hang)|concealed|in[\s-]?wall', msg))


def _tenant_item_keyword_intent(message: str, tenant_cfg=None):
    """The tenant's OWN priced services, matched on the customer's own word.

    Homebase's product keywords below know nothing about a tenant who also
    tiles or fits gutters, so those questions resolved to no intent at all and
    fell through to the generic pricing overview (prod, barmak, 2026-08-28: a
    tiling price question was answered with the tub package). Matching is on
    word STEMS, so "tiles" finds a row labelled "Tiling per square meter".
    Returns the synthetic 'tenant_item:<family>:<variant>' intent, or None.
    """
    if tenant_cfg is None:
        return None
    try:
        from .pricing_copy import tenant_custom_items
        from .out_of_scope_handler import _stems
        items = tenant_custom_items(tenant_cfg)
    except Exception:
        return None
    if not items:
        return None

    from .message_normalizer import rule_texts
    msg_stems = set()
    for text in rule_texts(message):
        msg_stems |= _stems(text)
    if not msg_stems:
        return None

    best, best_score = None, 0
    for key, item in items.items():
        vocab = " ".join(filter(None, [
            item.label or '', item.short_label or '',
            (item.variant or '').replace('_', ' '),
            " ".join(str(k) for k in (item.keywords or [])),
        ]))
        # Words that name no product on their own — every price row has them.
        tokens = _stems(vocab) - _GENERIC_PRICE_WORDS
        hits = tokens & msg_stems
        # Score by how much of the row's own vocabulary the customer used, so
        # "kitchen sink" beats a row that merely shares the word "kitchen".
        if len(hits) > best_score:
            best, best_score = key, len(hits)
    return best


# Stems of words that appear in price-row labels but name nothing on their own.
_GENERIC_PRICE_WORDS = frozenset({
    'per', 'squar', 'met', 'metr', 'unit', 'suppli', 'instal', 'install',
    'and', 'the', 'for', 'from', 'valu', 'full', 'new', 'standard', 'siz',
})


def _keyword_product_intent(message: str, tenant_cfg=None):
    """
    Keyword fallback for product/service intent when the AI classifier returns
    'none' (e.g. during a DeepSeek outage). Returns an intent string matching
    handle_service_inquiry()'s intents, or None. Conservative — only fires on
    clear product keywords, most-specific first.

    `tenant_cfg` is optional so existing callers keep working; passing it lets
    the tenant's own priced services (tiling, gutters, pumps) win a match that
    Homebase's hardcoded product words below can never make.

    Every keyword below is English, so the scan covers this turn's English
    rendering as well as the customer's own words — "Ndoda kugadzirisa
    chimbuzi" names a toilet just as plainly as "I need my toilet fixed", and
    only one of the two used to resolve.
    """
    from .message_normalizer import rule_texts
    # Joined with a non-word separator: every check below is a substring or a
    # \b-anchored regex, so nothing can match across the boundary.
    msg = " | ".join(rule_texts(message))
    if not msg.strip(' |'):
        return None

    has_tub = ('tub' in msg) or ('bathtub' in msg)
    if has_tub and any(k in msg for k in (
        'freestanding', 'free standing', 'free-standing', 'stand alone',
        'standalone', 'stand-alone',
    )):
        return 'standalone_tub'
    if 'geyser' in msg or 'water heater' in msg:
        if any(k in msg for k in ('not working', 'broken', 'leak', 'no hot water',
                                  'repair', 'fix', 'tripping')):
            return 'geyser_repair'
        return 'geyser'
    if 'cubicle' in msg or 'shower' in msg:
        return 'shower_cubicle'
    if 'vanity' in msg:
        return 'vanity'
    if 'chamber' in msg:
        return 'chamber'
    if 'toilet' in msg:
        if _mentions_wall_hung_toilet(msg):
            return 'wall_hung_toilet'
        if any(k in msg for k in ('not flush', "won't flush", 'wont flush', 'leak',
                                  'broken', 'running', 'fix', 'repair')):
            return 'toilet_repair'
        return 'toilet'
    if has_tub:
        return 'tub_sales'
    if any(k in msg for k in ('drain', 'blocked', 'clog', 'sewer')):
        return 'drain_unblocking'
    if 'pipe' in msg or 'burst' in msg:
        return 'pipe_repair'
    if 'facebook' in msg or 'package' in msg:
        return 'facebook_package'
    # Last: the tenant's own services. Checked AFTER the shared product words so
    # a tenant row that happens to share a word ("Kitchen Sink") never steals a
    # message that named a standard fitting.
    return _tenant_item_keyword_intent(message, tenant_cfg)


# Product "family" groups variants of the same item (tub_sales / standalone_tub
# both = tub). Used to decide when the customer's own keyword should override the
# LLM: a different FAMILY is a real misclassification (shower vs tub), while the
# same family is just a specificity difference where the LLM's choice is kept.
_PRODUCT_FAMILY = {
    'tub_sales': 'tub', 'standalone_tub': 'tub', 'bathtub_installation': 'tub',
    'shower_cubicle': 'shower',
    'geyser': 'geyser', 'geyser_repair': 'geyser',
    'toilet': 'toilet', 'toilet_repair': 'toilet',
    'vanity': 'vanity',
    # A wall-hung toilet is the chamber job (same install, same US$160 rate) —
    # family 'chamber' so the customer's own wall-mount wording overrides an
    # LLM 'toilet' guess (different family) but defers to an LLM 'chamber'.
    'chamber': 'chamber', 'wall_hung_toilet': 'chamber',
    'drain_unblocking': 'drain',
    'pipe_repair': 'pipe',
    'facebook_package': 'facebook',
}


def _product_family(intent):
    """Return the product family for an intent, or None for non-product intents
    ('none', 'pictures', location asks, etc.) so they never match a real product."""
    if not intent or intent in ('none', 'pictures', 'location_ask', 'location_visit'):
        return None
    return _PRODUCT_FAMILY.get(intent, intent)


def _is_unprompted_carryover_pricing(intent, message, price_requested,
                                     pricing_auto_reply_intents):
    """True when a price block would fire on a reply that NAMES no product and
    ASKS no price — i.e. the priceable intent was carried over from the running
    topic onto a bare booking-field answer (classic: the area reply "Avondale"
    coming back classified as shower_cubicle, then dumping the cubicle price).

    Deterministic guard: the LLM intent is unreliable on short booking-field
    replies, so the customer's own words must corroborate before we ever
    volunteer a price (CLAUDE.md: never volunteer price; prefer deterministic
    resolvers for short/fuzzy strings). Pure function so it is regression-testable
    without the API.
    """
    return (
        intent in pricing_auto_reply_intents
        and not price_requested
        and _keyword_product_intent(message) is None
    )


def _is_quoted_item_reference(message: str) -> bool:
    """True for a short demonstrative reply that points at a quoted item —
    "this one?", "and this one?", "how about this", "what about that one".

    When the customer is replying to (quoting) a portfolio photo, such a message
    is an elliptical price ask on the quoted item ("how much for this one?"), so
    it must be treated as a price request — otherwise it lacks an explicit price
    word, gets read as a project description, and the price is skipped (it then
    only survives if the standalone-question handler happens to rescue it).

    Kept deliberately tight (≤5 words) so genuine project descriptions that
    merely contain "this"/"that" are not swept in. Deterministic on purpose.
    """
    msg = (message or '').lower().strip().rstrip('?.! ')
    if not msg or len(msg.split()) > 5:
        return False
    patterns = (
        'this one', 'that one', 'this here', 'and this', 'and that',
        'how about this', 'how about that', 'what about this', 'what about that',
        'how about', 'what about',
    )
    return any(p in msg for p in patterns)


# Weekday tokens (full names + common abbreviations), most ambiguous handled by
# word-boundary matching so "wed" matches "Wed" but not "wedding", "sun" not
# "sunny", etc. Order matches Python's weekday() index (Monday=0).
_AVAILABILITY_DAY_PATTERNS = [
    (0, r'mon(?:day)?'),
    (1, r'tue(?:s|sday)?'),
    (2, r'wed(?:s|nesday)?'),
    (3, r'thu(?:r|rs|rsday)?'),
    (4, r'fri(?:day)?'),
    (5, r'sat(?:urday)?'),
    (6, r'sun(?:day)?'),
]


def _keyword_availability_date(message: str, closed_weekdays=None):
    """
    Deterministic day-name → next-future-date resolver. Mirrors
    _keyword_product_intent: conservative, no LLM round-trip. Returns a
    'YYYY-MM-DDT00:00' string (date only, midnight) for a bare weekday,
    'tomorrow', or 'today' token, else None. A day the tenant is closed on
    (passed in — never assumed) → None.

    Used to backfill the unified classifier's availability when the LLM misses
    the date math on partial inputs like "out of town but Wed I'm available".
    The LLM's value always wins when present (it may carry a specific time);
    this only fills an empty slot.
    """
    import re
    from datetime import timedelta
    import pytz

    msg = (message or '').lower()
    if not msg:
        return None

    # Default kept for the pre-tenant callers: the legacy Homebase week.
    closed = frozenset({5}) if closed_weekdays is None else frozenset(closed_weekdays)

    now = timezone.now().astimezone(pytz.timezone('Africa/Johannesburg'))

    def _fmt(d):
        return d.replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M')

    if re.search(r'\btoday\b', msg):
        return _fmt(now)
    if re.search(r'\b(?:tomorrow|tmrw|tmr|2moro)\b', msg):
        candidate = now + timedelta(days=1)
        return None if candidate.weekday() in closed else _fmt(candidate)

    for weekday_idx, pattern in _AVAILABILITY_DAY_PATTERNS:
        if re.search(r'\b' + pattern + r'\b', msg):
            if weekday_idx in closed:     # business is closed that day
                return None
            days_ahead = (weekday_idx - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7            # "Monday" on a Monday → next Monday
            return _fmt(now + timedelta(days=days_ahead))
    return None


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


def catalogue_price_lines(tenant=None) -> str:
    """This tenant's product price list, rendered from their own price rows.

    Was a hardcoded copy of Homebase's sheet, which meant another tenant's
    customer got THEIR photos with HOMEBASE's prices. Empty string when the
    tenant has no prices on file — the caller then sends the catalogue without
    a price list rather than quoting figures that aren't theirs.
    """
    from .tenant_config import get_config
    return get_config(tenant).catalogue_price_lines()


def build_catalogue_price_text(followup: str, tenant=None) -> str:
    """Text price list sent alongside the catalogue images."""
    lines = catalogue_price_lines(tenant)
    if not lines:
        # No price sheet on file — show the work, quote nothing.
        return (
            "Here's our product catalogue. "
            "The plumber gives a fixed quote on the spot after seeing the space, "
            "and the site visit is free.\n\n"
            f"{followup}"
        )
    return (
        "Here's our product catalogue — rough supply + install prices "
        "(final cost confirmed after a free site visit):\n\n"
        f"{lines}\n\n"
        "Bundling items can get you a discount. "
        "The plumber gives a fixed quote on the spot after seeing the space.\n\n"
        f"{followup}"
    )


def _materialize_image(path: str):
    """A local filesystem path for an image that may live in remote storage
    (wizard uploads). Returns (local_path, is_temp) — callers unlink temps
    after sending."""
    if os.path.exists(path):
        return path, False
    rel = (path or '').replace(chr(92), '/')
    try:
        from django.core.files.storage import default_storage
        if default_storage.exists(rel):
            import shutil
            import tempfile
            suffix = os.path.splitext(rel)[1] or '.jpg'
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            with default_storage.open(rel) as fh:
                shutil.copyfileobj(fh, tmp)
            tmp.close()
            return tmp.name, True
    except Exception as exc:
        print(f"_materialize_image failed for {path}: {exc}")
    return path, False


def _send_local_media(client, to, source_path, local_path, caption=None):
    """Gallery items may be photos or short videos — route by extension."""
    from .media_library import is_video_filename
    if is_video_filename(source_path):
        return client.send_local_video(to, local_path, caption=caption)
    return client.send_local_image(to, local_path, caption=caption)


def _describe_work_image(filename: str, tenant=None) -> str:
    """
    Human description of a sent image, derived from its filename, so a customer
    who replies to a specific photo ("this one how much") can be told — and the
    bot reminded — what that photo shows. Curated portfolio titles win when the
    filename matches a catalogued piece; otherwise the name is tidied up.
    """
    import re
    base = os.path.splitext(os.path.basename(filename or ''))[0]
    if not base:
        return "one of our previous work photos"

    # Curated title for catalogued pieces.
    try:
        from bot import portfolio_catalog
        for item in portfolio_catalog.items_for(tenant):
            item_base = os.path.splitext(os.path.basename(item['filename']))[0].lower()
            if item_base == base.lower():
                # Title first, so a quoted reply still resolves back to the row
                # by its leading title (see _quoted_portfolio_item); the bot's
                # own look at the photo follows, giving the classifiers real
                # text instead of a single word.
                vision = (item.get('vision') or '').strip()
                return f"{item['title']} - {vision}" if vision else item['title']
    except Exception:
        pass

    # Derive from the filename: split camelCase, tidy separators, drop camera /
    # WhatsApp codes and bare numbers (e.g. IMG-20250205-WA0009 → no useful text).
    cleaned = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', base)
    cleaned = re.sub(r'(?<=[A-Za-z])(?=\d)', ' ', cleaned)
    cleaned = re.sub(r'[_\-()]+', ' ', cleaned)
    cleaned = re.sub(r'\b(?:img|image|photo|wa|whatsapp)\d*\b', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b\d+\b', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().lower()
    if len(cleaned) < 3:
        return "one of our previous work photos"
    return cleaned


def _quoted_portfolio_item(tenant, quoted_text):
    """The tenant's OWN portfolio row for a photo the customer quoted, or None.

    The media index stores each sent image's description, and for a catalogued
    piece that description IS its title (see _describe_work_image), so the
    quoted text maps straight back to the row the tenant annotated.

    This exists because product intent is a fixed family list (tub / shower /
    geyser / …) that only covers what Homebase sells. A tenant can put ANY work
    in their gallery — prod 2026-08-27: barmak sent a customer their "Borehole"
    photo, the customer quoted it and asked "How much", no family matched, and
    the pricing overview answered with the bathroom package instead. The photo's
    own price_line is the tenant's own figure for exactly that work, so it
    answers the question the family list cannot — and cannot leak across
    tenants, because it is scoped to the row this tenant wrote.
    """
    quoted = (quoted_text or '').strip()
    if not quoted:
        return None
    try:
        from bot.models import TenantPortfolioItem
        rows = list(TenantPortfolioItem.objects.filter(tenant=tenant, is_active=True))
    except Exception:
        return None
    lowered = quoted.lower()
    # The stored description is the title alone on older photos, and
    # "<title> - <what the bot sees>" once vision has run, so match on the
    # LEADING title either way. Longest title first so "Borehole and tank"
    # cannot be swallowed by "Borehole".
    matches = [
        item for item in sorted(rows, key=lambda i: -len(i.title or ''))
        if (item.title or '').strip()
        and (lowered == item.title.strip().lower()
             or lowered.startswith(item.title.strip().lower()))
    ]
    # Exactly one match only: two photos sharing a title cannot tell us which
    # one the customer meant, and guessing a price is worse than falling through.
    if len(matches) == 1:
        return matches[0]
    if matches and len(matches[0].title) > len(matches[1].title):
        return matches[0]   # an unambiguous longest-title win
    return None


def _quoted_title(quoted_text):
    """The tenant's own label out of a quote, dropping any vision sentences.

    A sent photo is indexed as "<title> - <what the bot saw>". The full string
    is what the LLM and the classifiers should see, but the DETERMINISTIC
    keyword resolver must see only the title: vision writes prose, and prose
    names fixtures incidentally.
    """
    text = (quoted_text or '').strip()
    head = text.split(' - ', 1)[0].strip()
    return head or text


def _enrich_quoted_photo(appointment, quoted_text):
    """Look at a highlighted photo NOW, if we never have, and return the richer
    quote text. Returns quoted_text unchanged when there is nothing to add.

    The media index stores what we knew about a photo at SEND time, which for
    anything uploaded before vision existed is a bare title — "Borehole". That
    single word is all the classifiers, the pricing gates and generate_response
    ever saw for a customer's "how much for this one".

    Describing on demand rather than sweeping the whole gallery: only photos a
    customer actually asks about cost a call, the description is saved so it is
    a one-off per photo, and there is no backfill to remember to run. This runs
    on the batch-flush thread, never the webhook response, and the reply is
    already held 1-5 minutes — a second or two here is invisible.
    """
    quoted = (quoted_text or '').strip()
    if not quoted:
        return quoted_text
    tenant = getattr(appointment, 'tenant', None)
    item = _quoted_portfolio_item(tenant, quoted)
    if item is None or (item.vision_description or '').strip():
        return quoted_text
    try:
        from bot.media_library import describe_portfolio_item
        description = describe_portfolio_item(item)
    except Exception:
        return quoted_text
    if not description:
        return quoted_text
    print(f"👁️  Looked at highlighted photo '{item.item_id}': {description[:80]}")
    # Title first, so the quote still resolves back to this row by its leading
    # title on this turn and every later one (see _quoted_portfolio_item).
    return f"{item.title} - {description}"


def _quoted_portfolio_price_reply(plumbot, appointment, quoted_text, message_body):
    """Answer a price question about a quoted portfolio photo, or None.

    None means "this quote isn't one of our photos" — only then may the caller
    fall through to the family/multi-item pricing steps.

    A photo we DO recognise always answers about that photo: its own price line
    when the tenant priced it, otherwise the free-visit deflection. It must
    never fall through, because the later steps read the running conversation
    rather than the quote — prod 2026-08-27: a customer quoted barmak's
    unpriced "Borehole" photo, this returned None, and the multi-item branch
    answered with shower cubicle and tub prices carried over from an earlier
    photo. Wrong prices for the wrong job is worse than no price at all.
    """
    item = _quoted_portfolio_item(getattr(appointment, 'tenant', None), quoted_text)
    if item is None:
        return None
    try:
        from bot.repeated_question_detector import detect_language
        language = detect_language(message_body) or 'english'
    except Exception:
        language = 'english'
    language = 'shona' if language == 'shona' else 'english'

    # Not just the photo's stored line: a tenant may have entered the price in
    # their config without the photo ever being linked to it (see
    # price_line_for_item).
    try:
        from bot.media_library import price_line_for_item
        price_line = price_line_for_item(getattr(appointment, 'tenant', None), item)
    except Exception:
        price_line = (getattr(item, 'price_line', '') or '').strip()

    # The catalogued path first — it prices every item in the shot, not just the
    # headline one — then fall back to this photo's own line. Either way the
    # reply goes out in the SAME shape as every other priced answer: the money,
    # then the starting-prices disclaimer, then one closing question.
    try:
        catalogued = plumbot.compose_quoted_photo_price_reply(
            _quoted_title(quoted_text), language=language)
    except Exception:
        catalogued = None
    # Belt and braces on top of the builder's own check: a "pricing" reply
    # carrying no digit is not a pricing reply, whatever composed it.
    if catalogued and any(ch.isdigit() for ch in catalogued):
        return plumbot._ensure_price_disclaimer('pricing', catalogued)

    if not price_line:
        # Recognised the photo, hold no price for it: name the piece so the
        # customer knows we understood, then the tenant's own visit offer.
        print(f"💬 Quoted photo '{item.item_id}' has no price on file — visit deflection")
        lead_in = ("Iyoyo" if language == 'shona' else "That one") + f" — {item.title}."
        return f"{lead_in} {plumbot._no_price_on_file_reply(language)}"
    lead_in = ("Iyoyo" if language == 'shona' else "That one") + f" — {item.title}."
    try:
        close = plumbot._product_price_close(language)
    except Exception:
        close = ''
    # Same script as every other priced answer: the money in one block, then a
    # blank line, then the closing question — with _ensure_price_disclaimer
    # slotting "These are starting prices…" in between. Joining these with
    # spaces ran the price, the qualifier and the question into one paragraph,
    # which reads nothing like the rest of the bot's pricing copy.
    reply = f"{lead_in} {price_line}"
    if close:
        reply = f"{reply}\n\n{close}"
    reply = plumbot._ensure_price_disclaimer('pricing', reply)
    print(f"💬 Quoted-photo price reply from portfolio item '{item.item_id}'")
    return reply


def _tenant_gallery_paths(tenant) -> list:
    """A non-homebase tenant's own portfolio image paths (Phase 2.5). Their
    gallery is ONLY their uploaded/seeded items — never homebase's bundled
    photo folders. [] when they have none (senders return False → text
    fallback)."""
    from bot import portfolio_catalog
    return [
        portfolio_catalog.image_path_for(item)
        for item in portfolio_catalog.available_items(tenant)
    ]


def _is_foreign_tenant(tenant) -> bool:
    return tenant is not None and getattr(tenant, 'slug', '') != 'homebase'


def get_catalogue_images(tenant=None) -> list:
    if _is_foreign_tenant(tenant):
        return _tenant_gallery_paths(tenant)
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
    images = get_catalogue_images(appointment.tenant if appointment else None)
    if not images:
        print("No catalogue images found — text-only fallback will be used")
        return False

    def _send():
        try:
            from .whatsapp_cloud_api import get_client_for_tenant
            client = get_client_for_tenant(appointment.tenant if appointment else None)
            time.sleep(1)  # let the text message arrive first
            sent_count = 0
            media_index = {}
            _tenant = appointment.tenant if appointment else None
            for index, image_path in enumerate(images):
                caption = (f"{business_name_for(appointment)} — product catalogue"
                           if index == 0 else None)
                local_path, is_temp = _materialize_image(image_path)
                try:
                    result = _send_local_media(client, sender, image_path, local_path, caption=caption)
                finally:
                    if is_temp:
                        try: os.unlink(local_path)
                        except OSError: pass
                wamid = (result or {}).get('messages', [{}])[0].get('id')
                if wamid:
                    media_index[wamid] = _describe_work_image(image_path, tenant=_tenant)
                sent_count += 1
                time.sleep(0.5)
            if appointment:
                appointment.record_sent_media(
                    media_index, f"[MEDIA] Sent {sent_count} catalogue image(s)"
                )
            print(f"Sent {sent_count}/{len(images)} catalogue images to {sender}")
        except Exception as exc:
            print(f"Failed to send catalogue images: {exc}")

    threading.Thread(target=_send, daemon=True).start()
    return True


def send_portfolio_item(sender, item, appointment=None) -> bool:
    """
    Send ONE specific portfolio piece (image + its product/service-name caption)
    when a customer asks about that piece by name or feature.

    Returns True if the image was queued, False if the file is missing
    (caller should fall back to the generic gallery / a text reply).
    """
    from bot import portfolio_catalog

    image_path = portfolio_catalog.image_path_for(item)
    if not portfolio_catalog.item_is_available(item):
        print(f"Portfolio item image missing: {image_path}")
        return False

    caption = portfolio_catalog.build_item_caption(item)

    def _send():
        try:
            from .whatsapp_cloud_api import get_client_for_tenant
            client = get_client_for_tenant(appointment.tenant if appointment else None)
            time.sleep(1)  # let any preceding text land first
            local_path, is_temp = _materialize_image(image_path)
            try:
                result = _send_local_media(client, sender, image_path, local_path, caption=caption)
            finally:
                if is_temp:
                    try: os.unlink(local_path)
                    except OSError: pass
            wamid = (result or {}).get('messages', [{}])[0].get('id')
            if appointment:
                if wamid:
                    appointment.record_sent_media(
                        {wamid: item['title']},
                        f"[MEDIA] Sent portfolio item '{item['title']}'",
                    )
                else:
                    appointment.add_conversation_message(
                        "assistant", f"[MEDIA] Sent portfolio item '{item['title']}'"
                    )
                appointment.add_conversation_message("assistant", caption)
            print(f"Sent portfolio item '{item['id']}' to {sender}")
        except Exception as exc:
            print(f"Failed to send portfolio item '{item['id']}': {exc}")

    threading.Thread(target=_send, daemon=True).start()
    return True


def get_previous_work_images(tenant=None) -> list:
    if _is_foreign_tenant(tenant):
        return _tenant_gallery_paths(tenant)
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


def _strip_emojis(text: str) -> str:
    """Remove emojis to honour the no-emoji house rule on customer-facing copy.

    Thin alias kept for this module's existing callers — the implementation moved
    to bot.utils so the other generative paths (follow-ups, retry re-asks, repeat
    clarifications) can share the one stripper instead of trusting their prompt.
    """
    from .utils import strip_emojis
    return strip_emojis(text)


def _fallback_photo_followup(appointment=None) -> str:
    """Project-type-aware follow-up used when DeepSeek is unavailable."""
    project = (getattr(appointment, 'project_type', None) or '')
    if 'kitchen' in project:
        focus = "your kitchen"
    elif 'bathroom' in project:
        focus = "your bathroom"
    else:
        focus = "your project"
    return (
        f"Did anything there catch your eye for {focus}? "
        "We can do a free on-site visit and show you exactly what's possible "
        "in your space."
    )


def generate_photo_followup(appointment=None) -> str:
    """
    Build a CONTEXTUAL one-liner to send after the work-photo gallery — tailored
    to what this lead has actually been discussing — instead of a fixed
    'anything for your bathroom?' line. Falls back to a project-type template if
    DeepSeek is unavailable.
    """
    default = _fallback_photo_followup(appointment)
    if appointment is None:
        return default
    try:
        from bot.services.clients import deepseek_call

        history = appointment.conversation_history or []
        lines = []
        for m in history[-10:]:
            if not isinstance(m, dict):
                continue
            content = (m.get('content') or '').strip()
            if not content or content.startswith('[MEDIA]') or content.startswith('[IMAGE]'):
                continue
            who = 'Customer' if m.get('role') == 'user' else 'Plumbot'
            lines.append(f"{who}: {content}")
        transcript = "\n".join(lines[-6:])
        project = (appointment.project_type or '').replace('_', ' ') or 'not stated yet'

        reply = deepseek_call(
            messages=[
                {"role": "system", "content": (
                    f"You are Plumbot, a warm WhatsApp assistant for "
                    f"{business_name_for(appointment)} in Harare, Zimbabwe. You have JUST "
                    "sent the customer a gallery of our "
                    "previous-work photos. Write ONE short follow-up message (max 2 "
                    "sentences) that:\n"
                    "- refers to what THIS customer has actually been discussing\n"
                    "- invites them to point out anything in the photos they liked\n"
                    "- gently nudges toward the free on-site visit / next booking step\n"
                    "Reply in the SAME language the customer used (English or Shona). "
                    "No emojis. Never use a dash as punctuation: no em dashes, no en dashes, no ' - ' between clauses. Use a comma, a full stop or a new sentence. Hyphens inside words are fine (on-site, all-in, wall-hung). "
                    "Do not quote a price. Sound like a knowledgeable colleague "
                    "texting, not a script. Output only the message text."
                )},
                {"role": "user", "content": (
                    f"Customer's project: {project}\n\n"
                    f"Recent conversation:\n{transcript or '(no prior detail)'}\n\n"
                    "Write the follow-up message now."
                )},
            ],
            temperature=0.7,
            max_tokens=90,
            retries=1,
            timeout=10,
        )
        reply = _strip_emojis((reply or '').strip().strip('"').strip())
        return reply or default
    except Exception as exc:
        print(f"Photo follow-up generation failed ({exc}) — using template")
        return default


# -----------------------------------------------------------------------------
# FIX 3 — PREVIOUS WORK PHOTO DEDUP
# send_previous_work_photos now returns True ONLY after photos are confirmed
# queued; the caller must NOT send any fallback text when True is returned.
# -----------------------------------------------------------------------------

def send_previous_work_photos(sender, appointment=None, intro=None):
    """
    Send previous work photos with a small delay between each image.
    Returns True if photos were queued (caller must NOT send additional text).
    Returns False if no images are configured (caller may send a text fallback).
    Photos are only sent once per 24-hour window per appointment to prevent duplicates.

    `intro` replaces the standard lead-in line — the photo path returns outright,
    so anything the customer asked alongside the pictures has to travel with
    them or it is never answered. Optional, so existing callers are untouched.
    """
    if appointment is not None:
        from django.utils import timezone
        from datetime import timedelta
        last_sent = getattr(appointment, 'previous_work_photos_sent_at', None)
        if last_sent and (timezone.now() - last_sent) < timedelta(hours=24):
            print(f"Skipping previous work photos for {sender} - already sent within 24h")
            return True
    images = get_previous_work_images(appointment.tenant if appointment else None)
    if not images:
        print("No previous work images found - caller should handle fallback")
        return False
    if appointment is not None:
        from django.utils import timezone
        appointment.previous_work_photos_sent_at = timezone.now()
        appointment.save(update_fields=['previous_work_photos_sent_at'])
    intro = intro or "Here are some examples of our previous plumbing work!"
    def send_images_with_delay():
        try:
            from .test_console import is_test_sender
            from .whatsapp_cloud_api import get_client_for_tenant
            client = get_client_for_tenant(appointment.tenant if appointment else None)
            delay_seconds = 0 if is_test_sender(sender) else get_random_delay(sender=sender)
            print(f"Waiting {delay_seconds // 60} minute(s) before sending images to {sender}")
            time.sleep(delay_seconds)
            client.send_text_message(sender, intro)
            sent_count = 0
            media_index = {}
            from bot import portfolio_catalog
            for index, image_path in enumerate(images):
                # Per-image caption from the catalogue (product/service name only,
                # no pricing); generic fallback for uncatalogued shots.
                caption = portfolio_catalog.build_gallery_caption(
                    image_path, tenant=appointment.tenant if appointment else None)
                if caption is None:
                    caption = (
                        "Our previous work - high quality plumbing & renovations"
                        if index == 0 else None
                    )
                local_path, is_temp = _materialize_image(image_path)
                try:
                    result = _send_local_media(client, sender, image_path, local_path, caption=caption)
                finally:
                    if is_temp:
                        try: os.unlink(local_path)
                        except OSError: pass
                wamid = (result or {}).get('messages', [{}])[0].get('id')
                if wamid:
                    media_index[wamid] = _describe_work_image(
                        image_path, tenant=appointment.tenant if appointment else None)
                sent_count += 1
                time.sleep(0.5)
            follow_up = generate_photo_followup(appointment)
            time.sleep(1)
            client.send_text_message(sender, follow_up)
            if appointment:
                appointment.add_conversation_message("assistant", intro)
                appointment.record_sent_media(
                    media_index, f"[MEDIA] Sent {sent_count} previous work image(s)"
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
        # "from" rates taken verbatim from bot/sales_profiles/homebase.md (the
        # pricing source of truth). Quote "from" and defer the exact figure to
        # the free site visit — never invent prices beyond the profile table.
        service_from = {
            'bathroom_renovation':       'from US$900',
            'bathroom_installation':     'from US$900',
            'kitchen_renovation':        'from US$600',
            'kitchen_installation':      'from US$600',
            'new_plumbing_installation': None,  # not in the price table → defer
        }
        from_price = service_from.get(appointment.project_type)
        service_label = appointment.project_type.replace('_', ' ')
        price_line = (
            f"Our {service_label} work starts {from_price}, with the final "
            f"price confirmed on site."
            if from_price else
            f"Pricing for {service_label} depends on the scope, and we confirm "
            f"the exact figure on site."
        )
        # Isolate the objection with a tie-down yes before walking through the
        # price, then close — don't answer and drop into the next field question.
        return (
            f"Quick one before I break it down — if the number lands somewhere "
            f"fair for you, is this something you'd want to get moving on?\n\n"
            f"{price_line}\n\nThe exact cost depends on:\n"
            f"• The specific fixtures and materials you choose\n"
            f"• The size and complexity of the work\n"
            f"• Your exact location ({appointment.customer_area})\n\n"
            f"The site visit and quote are free — our plumber will "
            f"{'review your plan' if appointment.has_plan else 'come and look at the space'} "
            f"and give you a fixed price before any work starts.\n\n"
            f"Shall I lock that in for you?"
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

        # Live inspection: set WEBHOOK_LOG_RAW=1 (e.g. on Railway while testing a
        # CTWA ad) to dump the full inbound payload to the log stream. Off by default
        # to keep production logs clean.
        if os.environ.get('WEBHOOK_LOG_RAW') == '1':
            print(f"[webhook] RAW {json.dumps(body)}")

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
        return HttpResponse(status=200)  # prevent retry loop

def process_webhook_in_background(body):
    try:
        for entry in body.get('entry', []):
            for change in entry.get('changes', []):
                if change.get('field') == 'messages':
                    
                    value = change.get('value', {})  # DEFINE VALUE HERE
                    
                    process_message_change(value)

    except Exception as e:
        print(f"❌ Background processing error: {str(e)}")

def _resolve_tenant_for_value(value):
    """Phase 1 tenant routing (docs/MULTI_TENANT_PLAN.md §3.2): every Meta
    webhook event carries metadata.phone_number_id — look up the owning
    TenantWhatsAppChannel.

    A route miss returns None and the event is DROPPED. This used to fall back to
    the homebase seed, which was safe only while homebase was the sole live
    tenant; with tenant #2 live a miss is another business's traffic, and the
    fallback answered their lead in homebase's voice, from homebase's number,
    judged against homebase's conversation history. Dropping is the lesser harm:
    a missing/inactive channel row is a config bug to fix, not traffic to guess at."""
    phone_number_id = (value.get('metadata') or {}).get('phone_number_id', '')
    if phone_number_id:
        channel = (
            TenantWhatsAppChannel.objects
            .filter(phone_number_id=phone_number_id, is_active=True, tenant__is_active=True)
            .select_related('tenant')
            .first()
        )
        if channel is not None:
            return channel.tenant
        print(f"🚨 TENANT ROUTE MISS: unknown phone_number_id={phone_number_id} — dropping event (register this channel)")
    else:
        print("🚨 TENANT ROUTE MISS: event has no metadata.phone_number_id — dropping event")
    return None


def process_message_change(value):
    try:
        # Resolve the owning tenant once per event, thread it down. No tenant =
        # unroutable event; drop it rather than attribute it to the wrong business.
        tenant = _resolve_tenant_for_value(value)
        if tenant is None:
            return

        # ✅ 1. HANDLE STATUSES FIRST AND EXIT
        statuses = value.get('statuses', [])
        if statuses:
            process_status_updates(statuses, tenant=tenant)
            return  # CRITICAL FIX — stops the loop

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

            # CTWA "Click to WhatsApp" ad attribution. When a chat starts from a
            # linked ad, the Meta Cloud API attaches a `referral` object to the first
            # message (referral.source_type == 'ad', plus source_id/headline/body).
            # We persist it on the inbound event so it's visible in the admin and so
            # downstream code can tell ad-originated leads from organic ones.
            referral = message.get('referral')

            if message_id:
                try:
                    WhatsAppInboundEvent.objects.create(
                        tenant=tenant,
                        message_id=message_id,
                        sender=sender,
                        message_type=message_type or '',
                        referral=referral,
                        raw_payload=message,
                    )
                except IntegrityError:
                    print(f"Duplicate inbound message ignored: {message_id}")
                    continue

            if referral:
                print(
                    f"📣 CTWA ad referral on {message_id}: "
                    f"source_type={referral.get('source_type')} "
                    f"source_id={referral.get('source_id')}"
                )

            print(f"📩 Processing message from {sender}, type: {message_type}")

            # WhatsApp reply-to ("highlighted") context — the Cloud API attaches
            # only the WAMID of the quoted message, not its text. We resolve it to
            # the actual text downstream against stored conversation history.
            quoted_id = (message.get('context') or {}).get('id')

            if message_type == 'text':
                handle_text_message(
                    sender, message.get('text', {}),
                    message_id=message_id, quoted_id=quoted_id,
                    referral=referral, tenant=tenant,
                )

            elif message_type == 'image':
                handle_media_message(
                    sender, message.get('image', {}), 'image',
                    message_id=message_id, quoted_id=quoted_id, tenant=tenant,
                )

            elif message_type == 'document':
                handle_media_message(
                    sender, message.get('document', {}), 'document',
                    message_id=message_id, quoted_id=quoted_id, tenant=tenant,
                )

            elif message_type in ('audio', 'voice'):
                handle_audio_message(sender, message.get('audio') or message.get('voice') or {}, tenant=tenant)

            elif message_type == 'video':
                handle_media_message(
                    sender, message.get('video', {}), 'video',
                    message_id=message_id, quoted_id=quoted_id, tenant=tenant,
                )

            elif message_type == 'sticker':
                handle_unsupported_media(sender, 'sticker', tenant=tenant)

            elif message_type == 'location':
                handle_location_message(sender, message.get('location', {}), tenant=tenant)

            elif message_type == 'contacts':
                handle_unsupported_media(sender, 'contacts', tenant=tenant)

            else:
                print(f"⚠️ Unknown message type from {sender}: '{message_type}'")

    except Exception as e:
        print(f"❌ Error processing message: {str(e)}")
def _clean_phone(raw_phone: str) -> str:
    return (raw_phone or "").replace("whatsapp:", "").replace("+", "").strip()


def _find_appointment_by_recipient(recipient_id: str, tenant=None) -> Optional[Appointment]:
    """
    Best-effort lookup from webhook status recipient_id (usually digits only)
    to our stored appointment phone formats.

    MUST be tenant-scoped: one lead can talk to several tenants from the same
    handset, so a phone number alone identifies a person, not a conversation.
    Without `tenant` the `-updated_at` tiebreak returns whichever tenant's row
    was touched last, and a delivery verdict (notably 131047) gets written onto
    another business's lead — silently freezing that lead's follow-ups.
    """
    cleaned = _clean_phone(recipient_id)
    if not cleaned:
        return None

    base = Appointment.objects.all()
    if tenant is not None:
        base = base.filter(tenant=tenant)

    direct_candidates = {
        cleaned,
        f"+{cleaned}",
        f"whatsapp:{cleaned}",
        f"whatsapp:+{cleaned}",
    }
    appointment = (
        base.filter(phone_number__in=direct_candidates)
        .order_by('-updated_at')
        .first()
    )
    if appointment:
        return appointment

    return (
        base.annotate(
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


def _record_send_cost(status_obj, *, message_id, status_name, recipient_id,
                      errors, appointment, tenant):
    """Persist the ``pricing`` object Meta attaches to an outbound status.

    Meta sends several statuses per message (sent, delivered, read) and only
    some carry pricing, so this upserts on message_id and never overwrites a
    known pricing verdict with a blank one from a later status.

    The hours_since_* values are stamped from OUR view of the window at the
    moment of the verdict, which is the whole point: it lets a later read ask
    whether a send we believed was inside a 72h CTWA window was one Meta agreed
    was free, still inside its customer service window, or bounced outright.
    """
    from django.utils import timezone
    from .models import WhatsAppSendCost

    if not message_id:
        return

    pricing = status_obj.get('pricing') or {}
    has_pricing = bool(pricing)

    hours_since_ctwa = None
    hours_since_inbound = None
    is_ctwa = False
    if appointment is not None:
        now = timezone.now()
        entry = getattr(appointment, 'ctwa_entry_at', None)
        if entry:
            is_ctwa = True
            hours_since_ctwa = round((now - entry).total_seconds() / 3600.0, 2)
        last_in = (getattr(appointment, 'last_inbound_at', None)
                   or getattr(appointment, 'last_customer_response', None))
        if last_in:
            hours_since_inbound = round((now - last_in).total_seconds() / 3600.0, 2)

    defaults = {
        'recipient': (recipient_id or '')[:50],
        'status': (status_name or '')[:20],
        'error_codes': ','.join(
            str(e.get('code')) for e in (errors or []) if e.get('code') is not None
        )[:120],
        'was_ctwa_lead': is_ctwa,
        'hours_since_ctwa_entry': hours_since_ctwa,
        'hours_since_last_inbound': hours_since_inbound,
    }
    if appointment is not None:
        defaults['appointment'] = appointment
    if tenant is not None:
        defaults['tenant'] = tenant
    if has_pricing:
        defaults.update({
            'billable': pricing.get('billable'),
            'pricing_type': str(pricing.get('type') or '')[:40],
            'category': str(pricing.get('category') or '')[:40],
            'pricing_model': str(pricing.get('pricing_model') or '')[:20],
        })

    WhatsAppSendCost.objects.update_or_create(
        message_id=message_id[:128], defaults=defaults,
    )


def process_status_updates(statuses, tenant=None):
    """
    Handle asynchronous WhatsApp outbound delivery state updates.
    This is the source of truth for delivered/read/failed, not send-time logs.

    `tenant` is the owner of the phone_number_id the event arrived on; it scopes
    the recipient lookup so a status only ever resolves within the conversation
    it belongs to (see _find_appointment_by_recipient).
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

            appointment = _find_appointment_by_recipient(recipient_id, tenant=tenant) if recipient_id else None
            appointment_ref = f"appointment_id={appointment.id}" if appointment else "appointment_id=unknown"

            # Record Meta's own billing verdict for this message. Free to collect
            # (it rides the status webhook we already receive) and the only
            # evidence of what Meta really does with our windows — our
            # messaging_window_closes_at is a local guess. Best-effort: a failure
            # here must never break delivery-status handling.
            try:
                _record_send_cost(
                    status_obj, message_id=message_id, status_name=status_name,
                    recipient_id=recipient_id, errors=errors,
                    appointment=appointment, tenant=tenant,
                )
            except Exception as cost_err:
                print(f"? Failed to record send cost for {message_id}: {cost_err}")


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

                # 131047 ("Re-engagement message") = the free-form window is closed
                # on Meta's side, even though our 24h/72h calc may say otherwise.
                # Meta is authoritative: stop proactive free-form sends to this lead
                # until they message again (no paid template fallback). Reopens via
                # mark_customer_response on the next inbound.
                if any((e.get('code') == 131047)
                       or ('re-engagement' in (e.get('title') or '').lower())
                       for e in errors):
                    appointment.mark_freeform_window_closed()
                    print(
                        f"🚫 Free-form window closed for appointment {appointment.id} "
                        f"(131047) — pausing proactive sends until the customer replies"
                    )

        except Exception as status_err:
            print(f"? Failed to process status update: {status_err}")


def handle_location_message(sender, location_data, tenant=None):
    try:
        latitude = location_data.get('latitude')
        longitude = location_data.get('longitude')
        address = location_data.get('address')
        print(f"?? Location from {sender}: {latitude}, {longitude}")

        phone_number = f"whatsapp:+{sender}"
        try:
            leads = Appointment.objects.filter(phone_number=phone_number)
            if tenant is not None:
                leads = leads.for_tenant(tenant)
            appointment = leads.get()
        except Appointment.DoesNotExist:
            response_msg = "Thanks for the location! To get started, please tell me about your plumbing needs."
            delay = get_random_delay(sender=sender)
            threading.Thread(target=delayed_response, args=(sender, response_msg, delay), kwargs={'tenant': tenant}, daemon=True).start()
            return

        if appointment.chatbot_paused:
            print(f"Chatbot paused for {phone_number}; ignoring auto location response.")
            return

        from .views import Plumbot
        plumbot = Plumbot(phone_number, tenant=tenant)
        next_question = plumbot.get_next_question_to_ask()

        if next_question == 'area' and not appointment.customer_area:
            if address:
                appointment.customer_area = address
                appointment.save()
                refresh_lead_score(appointment)
                reply = plumbot.generate_response(f"My location is {address}")
                delay = get_random_delay(sender=sender)
                threading.Thread(target=delayed_response, args=(sender, reply, delay), kwargs={'tenant': tenant}, daemon=True).start()
            else:
                response_msg = (
                    "Thanks for the location pin! ??\n\n"
                    "Could you also type the area name? (e.g., Harare Hatfield, Harare Avondale)\n\n"
                    "This helps us serve you better."
                )
                delay = get_random_delay(sender=sender)
                threading.Thread(target=delayed_response, args=(sender, response_msg, delay), kwargs={'tenant': tenant}, daemon=True).start()
        else:
            response_msg = "Thanks for sharing your location! ??\n\nI've noted it. Let me continue with your appointment details..."
            delay = get_random_delay(sender=sender)
            threading.Thread(target=delayed_response, args=(sender, response_msg, delay), kwargs={'tenant': tenant}, daemon=True).start()

    except Exception as e:
        print(f"? Error handling location: {str(e)}")


def handle_unsupported_media(sender, media_type, tenant=None):
    try:
        if is_chatbot_paused_for_sender(sender, tenant=tenant):
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
        # (fixed: `delay` was previously never assigned here, so this reply
        # silently failed with a swallowed NameError for every sticker/contact)
        delay = get_random_delay(sender=sender)
        threading.Thread(
            target=delayed_response,
            args=(sender, response_msg, delay),
            kwargs={'tenant': tenant},
            daemon=True
        ).start()
    except Exception as e:
        print(f"? Error handling unsupported media: {str(e)}")


def handle_audio_message(sender, audio_data, tenant=None):
    try:
        if is_chatbot_paused_for_sender(sender, tenant=tenant):
            print(f"Chatbot paused for whatsapp:+{sender}; skipping audio auto response.")
            return
        print(f"?? Audio message from {sender}")

        phone_number = f"whatsapp:+{sender}"
        try:
            leads = Appointment.objects.filter(phone_number=phone_number)
            if tenant is not None:
                leads = leads.for_tenant(tenant)
            appointment = leads.get()
        except Appointment.DoesNotExist:
            response_msg = (
                "Voice notes we can't read unfortunately — just type it out and we'll get you sorted"
            )
            delay = get_random_delay(sender=sender)
            threading.Thread(target=delayed_response, args=(sender, response_msg, delay), kwargs={'tenant': tenant}, daemon=True).start()
            return

        if appointment.plan_status == 'pending_upload':
            response_msg = (
                "That came through as a voice note — for the plans we need photos or a PDF. "
                "Send those when you're ready, or type \"done\" if you're finished."
            )
        else:
            response_msg = (
                "Voice notes we can't read — just type it out and we'll carry on from where we were"
            )

        delay = get_random_delay(sender=sender)
        threading.Thread(target=delayed_response, args=(sender, response_msg, delay), kwargs={'tenant': tenant}, daemon=True).start()

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


def handle_text_message(sender, text_data, message_id=None, quoted_id=None, referral=None, tenant=None):
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

        appointment, created = Appointment.objects.get_or_create_lead(
            phone_number, tenant=tenant,
        )

        # Source attribution: tag where the lead came from (ad referral wins;
        # else inferred from their own words; first message w/o signal = direct).
        try:
            appointment.update_lead_source(message_body, is_first_message=created)
        except Exception as src_err:
            print(f"lead_source tagging failed: {src_err}")

        # CTWA ad lead — record the referral and (re)start the 72h free-form window.
        if referral and appointment.record_ctwa_referral(referral):
            print(
                f"📣 CTWA window started for {sender}: "
                f"closes {appointment.ctwa_window_expires_at:%Y-%m-%d %H:%M %Z}"
            )

        # Resolve a WhatsApp reply-to ("highlighted message") into its text so the
        # bot knows which earlier message the customer is responding to.
        quoted_text = appointment.resolve_quoted_message(quoted_id) if quoted_id else None
        if quoted_id:
            if quoted_text:
                print(f"🔗 Reply to earlier message: '{quoted_text[:80]}'")
            else:
                print(f"🔗 Reply to message {quoted_id} — not found in history")

        appointment.add_conversation_message(
            "user", message_body, message_id=message_id, quoted=quoted_text,
        )
        print("User message saved to conversation history")

        appointment.mark_customer_response()

        # Post-visit: a lead in the quote follow-up sequence who names a day
        # moves from the ask cadence onto the confirmation branch. A no-op for
        # every lead without a live site-visit report, and it never raises.
        from .post_visit import note_inbound_reply
        note_inbound_reply(appointment, message_body, source='whatsapp')

        from .out_of_scope_handler import detect_delay_signal_message, mark_delay_signal

        delay_check = detect_delay_signal_message(message_body, appointment)
        if delay_check.get('is_delay'):
            mark_delay_signal(appointment, message_body)
        else:
            # Don't clear the delay signal for bare acks ("ok", "thanks", "👍").
            # A customer typing "ok" after the bot's farewell is acknowledging the
            # delay confirmation — treating it as re-engagement asks the next
            # qualification question (e.g. "What suburb are you in?").
            # Same resolver the reply gate uses, so the hold is never cleared
            # here on a turn that gate would have answered with silence.
            from .out_of_scope_handler import should_hold_silently
            _is_ack = (appointment.is_delayed
                       and should_hold_silently(message_body, appointment))
            if _is_ack:
                # Ack while delayed — keep the pause. The turn still goes to
                # the batch; the reply gate is what suppresses the reply.
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

        # How long they took to come back to us sets how long we take to come
        # back to them — recorded before the queue so only the batch-opening
        # message counts.
        _record_lead_reply_latency(sender, appointment)

        # Queue the message — if another arrives within MESSAGE_BATCH_WINDOW_SECONDS the
        # timer resets, and one combined reply handles both concerns together.
        _enqueue_for_response(sender, message_body, message_id, quoted_text, tenant=tenant)

    except Exception as e:
        print(f"Error handling text: {str(e)}")
        import traceback
        traceback.print_exc()


def _enqueue_for_response(sender: str, message_body: str, message_id, quoted_text=None, tenant=None):
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

    # Web test console: bypass the debounce window and generate the reply inline,
    # so the HTTP "send" request returns only once the bot's reply is in history.
    from .test_console import is_test_sender
    if is_test_sender(sender):
        with _pending_batch_lock:
            existing = _pending_batch_timers.pop(sender, None)
            if existing is not None:
                existing.cancel()
            _pending_batches[sender] = [(message_body, message_id, quoted_text, tenant)]
        _flush_text_batch(sender)
        return

    # This tenant's batch switch: off = answer this message on its own, right
    # now. Anything already queued rides along so nothing is stranded. The flush
    # runs on its own thread — generation must not block the webhook response
    # back to Meta.
    from .platform_flags import batch_window_enabled
    if not batch_window_enabled(tenant):
        with _pending_batch_lock:
            existing = _pending_batch_timers.pop(sender, None)
            if existing is not None:
                existing.cancel()
            _pending_batches.setdefault(sender, []).append(
                (message_body, message_id, quoted_text, tenant))
        print(f"⚡ Batch window OFF (admin switch) — replying to {sender} immediately")
        threading.Thread(target=_flush_text_batch, args=(sender,), daemon=True).start()
        return

    with _pending_batch_lock:
        if sender not in _pending_batches:
            _pending_batches[sender] = []
        _pending_batches[sender].append((message_body, message_id, quoted_text, tenant))
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

    messages = [body for body, _, _, _ in batch]
    last_message_id = batch[-1][1]
    # The quoted context that matters is whatever the customer last replied to.
    quoted_text = next(
        (q for _, _, q, _ in reversed(batch) if q), None
    )
    # All entries in a sender's batch arrived on the same channel.
    tenant = next((t for _, _, _, t in reversed(batch) if t is not None), None)

    if len(messages) == 1:
        combined = messages[0]
        print(f"📤 Batch flush: 1 message for {sender}")
    else:
        combined = "\n".join(messages)
        print(f"📦 Batch flush: {len(messages)} messages combined for {sender} → '{combined[:120]}'")

    _generate_and_schedule_reply(sender, combined, last_message_id, quoted_text, tenant=tenant)


def _derive_service_item(message: str) -> str:
    """Pull the thing the lead named out of a service-availability question, as a
    project-description phrase. 'do you have shower rooms' -> 'shower rooms'. Used
    only as a fallback when the classifier didn't extract a project_description."""
    m = (message or '').strip()
    ml = m.lower()
    for p in ('do you have', 'do you do', 'do you sell', 'do you install',
              'do you offer', 'do you fix', 'can you do', 'can you install',
              'what about', 'do you also do', 'do you guys do'):
        if ml.startswith(p):
            return m[len(p):].strip().lstrip(':,').strip().rstrip('?').strip()
    return m.rstrip('?').strip()


def _derive_additional_items(message: str) -> str:
    """Pull the extra item(s) out of a 'No, also a toilet' reply to the service
    confirm question. Fallback only — the classifier's project_description wins."""
    m = (message or '').strip().rstrip('?.').strip()
    ml = m.lower()
    for p in ('no, also add', 'no also add', 'no, just add', 'no just add',
              'no, also', 'no also', 'no, add', 'no add', 'also add', 'just add',
              'and also', 'also', 'and', 'add', 'no,', 'no'):
        if ml.startswith(p):
            m = m[len(p):].strip().lstrip(',').strip()
            ml = m.lower()
            break
    for a in ('a ', 'an ', 'the '):
        if ml.startswith(a):
            m = m[len(a):].strip()
            break
    return m


def _mark_stop_requested(appointment) -> None:
    """Record that this lead asked us to stop, so every send path can see it.

    Written once and never cleared automatically: the customer has to re-open
    the conversation themselves. The follow-up crons exclude
    [STOP_REQUESTED] (see send_followups._exclude_suppressed_states), which is
    what makes the request stick beyond the current turn.
    """
    try:
        notes = appointment.internal_notes or ''
        if '[STOP_REQUESTED]' in notes:
            return
        stamp = timezone.now().strftime('%Y-%m-%d %H:%M')
        appointment.internal_notes = f"[STOP_REQUESTED] {stamp}\n{notes}".strip()
        appointment.is_lead_active = False
        appointment.save(update_fields=['internal_notes', 'is_lead_active'])
    except Exception as exc:
        print(f"WARNING could not mark stop request: {exc}")


def _generate_and_schedule_reply(sender: str, message_body: str, message_id=None, quoted_text=None, tenant=None):
    """Generate a bot reply for message_body and schedule it with a 1-5 min send delay."""
    try:
        phone_number = f"whatsapp:+{sender}"
        leads = Appointment.objects.filter(phone_number=phone_number)
        if tenant is not None:
            # Phone is unique per tenant — scope the lookup to the channel's
            # owner so we never pick up another tenant's lead for this number.
            leads = leads.for_tenant(tenant)
        appointment = leads.first()
        if not appointment:
            return

        if appointment.chatbot_paused:
            print(f"Chatbot paused for {phone_number}; skipping auto response.")
            return

        if appointment.status == 'confirmed' and is_post_booking_ack_message(message_body):
            print(f"Post-booking ack detected; no reply sent. sender={sender}, message='{message_body}'")
            return

        # ── HARD STOP — before every other step, including classification ────
        # "please dont say anything more" must end the chasing immediately and
        # permanently. Marking the lead is the point: the crons read
        # [STOP_REQUESTED] too, so the decision survives this turn (prod lead
        # 872 was told to stop and then sent three more automated pitches).
        from .out_of_scope_handler import is_hard_stop_request, build_hard_stop_reply
        if is_hard_stop_request(message_body):
            _mark_stop_requested(appointment)
            print(f"Hard stop requested by {phone_number}; suppressing all proactive sends.")
            # detect_language_simple is imported at MODULE level. Importing it
            # again here made the name local to this entire function, so every
            # later use — the FAQ language pick, _advance_after_scope, the
            # reschedule path — raised UnboundLocalError on any message that did
            # not take this branch. Which was most of them: "where are you
            # located" died on it in production (2026-09-01).
            _stop_reply = build_hard_stop_reply(
                is_shona=detect_language_simple(message_body) == 'shona')
            appointment.add_conversation_message("assistant", _stop_reply)
            appointment.last_outbound_at = timezone.now()
            appointment.last_contacted_at = appointment.last_outbound_at
            appointment.save(update_fields=[
                'conversation_history', 'last_outbound_at', 'last_contacted_at'])
            threading.Thread(
                target=delayed_response,
                args=(sender, _stop_reply, get_random_delay(sender=sender)),
                kwargs={'tenant': tenant}, daemon=True,
            ).start()
            return

        # A highlighted photo we have never looked at gets described NOW, so
        # every step below — classification, the pricing gates, generate_response
        # — sees what is in it rather than a one-word title.
        quoted_text = _enrich_quoted_photo(appointment, quoted_text)

        from .views import Plumbot
        plumbot = Plumbot(phone_number, tenant=tenant)
        # One Appointment instance per turn: Plumbot.__init__ get_or_creates its
        # OWN copy, and a later save from that stale copy resurrects state this
        # handler already changed (prod: [SERVICE_CONFIRM_PENDING] was removed on
        # the 'yes' turn, then written back by the retry-count save — so the next
        # message got consumed by the confirm flow instead of being answered).
        plumbot.appointment = appointment

        reply = None

        # ── UNIFIED DEEPSEEK CLASSIFIER (fires FIRST, on every message) ───────
        # DeepSeek classifies every inbound BEFORE any keyword short-circuit below,
        # so an out-of-context message (a delay/complaint/OOS, or a real booking
        # answer that happens to trip an FAQ keyword) is never swallowed by the
        # keyword layer. Returns None on failure — callers fall back to keywords.
        from bot.unified_classifier import (
            unified_classify,
            uc_intent, uc_confidence, uc_product_intent,
            uc_is_photo_request, uc_is_plan_later, uc_is_repeat,
            uc_as_service_inquiry, uc_as_oos_classification,
            uc_pivoted_to_timeline, uc_offered_date, uc_offered_timeframe,
            uc_extracted, uc_answered_current_question, uc_english,
        )
        from django.utils import timezone as _tz
        _today_str = _tz.now().strftime('%Y-%m-%d')
        _next_question = plumbot.get_next_question_to_ask()
        _uclass = unified_classify(
            message_body,
            appointment=appointment,
            conversation_history=appointment.conversation_history,
            today_date=_today_str,
            next_question=_next_question,
        )

        # Deterministic availability backfill: on partial inputs the LLM can miss
        # or mis-resolve a bare weekday ("out of town but Wed I'm available"). A
        # day name maps to an exact next-future date with no LLM round-trip, so
        # fill it in when the classifier left availability empty. The LLM's value
        # always wins when present (it may carry a specific time).
        if _uclass is not None:
            _ext = _uclass.get('extracted') or {}
            if not _ext.get('availability') or _ext.get('availability') == 'null':
                _kw_date = _keyword_availability_date(
                    message_body, plumbot.tenant_cfg.closed_weekdays()
                )
                if _kw_date:
                    _ext['availability'] = _kw_date
                    _uclass['extracted'] = _ext
                    print(f"📅 Availability keyword backfill: {_kw_date}")

        # ── Inbound language normalisation ───────────────────────────────────
        # Every deterministic resolver downstream matches ENGLISH phrases, and
        # the customer writes Shona — so each one silently failed until its
        # Shona phrases were hand-written in after a lead had already been
        # mishandled. Hand them the English rendering the classifier just
        # produced (same call, no extra round trip) and they all gain Shona at
        # once. RULE ENGINE ONLY: nothing customer-facing may read this, the
        # bot answers in the lead's own language.
        try:
            from .message_normalizer import remember as _remember_english
            _remember_english(message_body, uc_english(_uclass))
        except Exception as _norm_exc:
            print(f"⚠️ Message normalisation failed: {_norm_exc}")

        # ── Area backfill: capture a volunteered suburb BEFORE routing ────────
        # Booking fields are only extracted in STEP 4, but several steps below
        # answer and RETURN (out-of-scope, delay, photos, pricing). An area
        # given in the same breath as a delay signal was therefore thrown away
        # and then asked for again: "Ndiri kuChitungwiza ndichakubatayi ndapedza
        # kuronga mari" answered the area question we had just asked, went to
        # the delay handler, and left the lead with area=None after three asks
        # (prod, barmak, 2026-08-28). The classifier has already read it — store
        # it here, where no branch can lose it. Excluded cities are still
        # refused, exactly as extraction_mixin does it.
        _uc_area = (uc_extracted(_uclass).get('area') or '').strip()
        if (_uc_area and _uc_area.lower() != 'null'
                and not appointment.customer_area):
            try:
                _excluded_city = plumbot._is_excluded_city(
                    _uc_area, tenant=getattr(appointment, 'tenant', None))
            except Exception as _area_exc:
                print(f"⚠️ Early area check failed: {_area_exc}")
                _excluded_city = None
            if _excluded_city:
                print(f"🚫 Excluded area (early capture): {_uc_area} → {_excluded_city}")
            else:
                appointment.customer_area = _uc_area
                appointment.save(update_fields=['customer_area'])
                print(f"✅ Area captured before routing: {_uc_area}")

        # ── SERVICE-CONFIRM FOLLOW-UP ─────────────────────────────────────────
        # We asked "Is a <X> the only thing you're looking to get sorted?" last turn
        # (project set to <X>). Handle their answer without losing anything:
        #   • plain "yes" → fall through and advance,
        #   • they name MORE ("also a toilet") → append it to the project,
        #   • plain "no" with nothing named → ask what else, capture it next turn.
        # AI decides yes/no + what they named; note tags make it one-shot.
        _sc_notes = appointment.internal_notes or ''
        _sc_pending = '[SERVICE_CONFIRM_PENDING]' in _sc_notes
        _sc_awaiting_more = '[AWAITING_MORE_ITEMS]' in _sc_notes

        # A delay/exit signal in this reply OUTRANKS the scope question we asked.
        # "No my main bedroom is not yet sorted will get in touch ndasvika pa
        # stage iyoyo thanx" is not "no, there is more to add" — it is "not yet,
        # I will come back to you". Reading the leading "No" as a scope answer
        # pushed a departing lead for more work ("what else would you like
        # sorted while we're there?") and the delay handler in STEP 1b never
        # ran, because this branch answers and returns first (prod, barmak,
        # 2026-08-28). Same rule as every other pending state: what the customer
        # just said beats what we were waiting to hear. The tags are cleared so
        # the scope question cannot re-fire on their delay-flow answer.
        from bot.out_of_scope_handler import _is_explicit_deferral
        _sc_delay_override = (
            (_sc_pending or _sc_awaiting_more)
            and (uc_intent(_uclass) == 'delay_signal'
                 or _is_explicit_deferral(message_body))
        )
        if _sc_delay_override:
            print("⏸️ Delay signal outranks the service-confirm question — "
                  "handing to the delay flow")
            appointment._remove_notes_tag('[SERVICE_CONFIRM_PENDING]')
            appointment._remove_notes_tag('[AWAITING_MORE_ITEMS]')

        if (_sc_pending or _sc_awaiting_more) and not _sc_delay_override:
            from bot.out_of_scope_handler import _classify_affirmation
            _more_ai = uc_extracted(_uclass).get('project_description')
            _named = uc_product_intent(_uclass) not in ('none', None)

            def _append_project(item):
                _existing = (appointment.project_description or '').strip()
                if item and item.lower() not in _existing.lower():
                    appointment.project_description = (
                        f"{_existing}, {item}" if _existing else str(item)
                    )[:200]
                    appointment.save(update_fields=['project_description'])
                    print(f"📝 Appended to project_description: {item}")

            def _send_scope_advance():
                # Scope answer captured — advance to booking, never price the items.
                _adv = plumbot._advance_after_scope(detect_language_simple(message_body))
                if _adv:
                    appointment.add_conversation_message("assistant", _adv)
                    delay = get_random_delay(sender=sender)
                    threading.Thread(
                        target=delayed_response, args=(sender, _adv, delay, message_id), kwargs={'tenant': tenant},
                        daemon=True,
                    ).start()
                    return True
                return False

            if _sc_pending:
                appointment._remove_notes_tag('[SERVICE_CONFIRM_PENDING]')
                _aff = _classify_affirmation(message_body)
                if _aff != 'yes' and (_more_ai or _named):
                    _append_project(_more_ai or _derive_additional_items(message_body))
                    if _send_scope_advance():
                        return
                elif _aff == 'no':
                    # "No" with nothing named yet — elicit the rest, capture next turn.
                    appointment._add_notes_tag('[AWAITING_MORE_ITEMS]')
                    _reply = ("No problem — what else would you like sorted while "
                              "we're there?")
                    appointment.add_conversation_message("assistant", _reply)
                    delay = get_random_delay(sender=sender)
                    threading.Thread(
                        target=delayed_response, args=(sender, _reply, delay, message_id), kwargs={'tenant': tenant},
                        daemon=True,
                    ).start()
                    return
                # 'yes' / 'unclear' → fall through and advance
            else:  # _sc_awaiting_more — we asked "what else?"; capture what they named
                appointment._remove_notes_tag('[AWAITING_MORE_ITEMS]')
                if _more_ai or _named:
                    _append_project(_more_ai or _derive_additional_items(message_body))
                if _send_scope_advance():
                    return
                # else fall through and advance

        # ── VALUE-CHECK "NOTHING ELSE" → ADVANCE ──────────────────────────────
        # We closed a free-form answer with the property-scope value-check
        # ("Anything else on the property?"). A bare 'no'/ack now means "nothing
        # else, let's proceed" — advance to the next booking field. Without this the
        # 'no' falls to semantic-rescue, which misreads it as declining the whole job
        # ("So just to be sure, you're not interested…?") and disengages a warm lead.
        # Customer's own words win: only a bare negative/ack matches here — a named
        # item or a new question falls through to the normal flow.
        if (plumbot._last_assistant_was_value_check()
                and plumbot._is_nothing_else_reply(message_body)):
            _adv = plumbot._advance_after_scope(detect_language_simple(message_body))
            if _adv:
                appointment.add_conversation_message("assistant", _adv)
                delay = get_random_delay(sender=sender)
                threading.Thread(
                    target=delayed_response, args=(sender, _adv, delay, message_id), kwargs={'tenant': tenant},
                    daemon=True,
                ).start()
                return

        # ── "NO" TO THE REQUEST FOR PROJECT DETAIL ──────────────────────
        # They will not elaborate, and we do not need them to — the visit prices
        # whatever is there. Stop asking and move on, instead of re-sending the
        # question with a second one bolted on (prod probe 2026-09-01).
        _detail_no = plumbot._handle_no_to_detail_request(message_body)
        if _detail_no:
            appointment.add_conversation_message("assistant", _detail_no)
            delay = get_random_delay(sender=sender)
            threading.Thread(
                target=delayed_response, args=(sender, _detail_no, delay, message_id),
                kwargs={'tenant': tenant}, daemon=True,
            ).start()
            return

        # ── "NO" / "NEITHER" TO A DAY OR TIME OFFER ───────────────────────────
        # The slots we named don't work. Open the question up instead of putting
        # the same two back — "No" to "what works better: 9AM or 2PM?" used to be
        # answered "9AM or 2PM tomorrow?" (prod probe 2026-09-01). Only a bare
        # negative lands here; "no, Friday please" carries the answer and goes to
        # the normal date parser.
        _slot_no = plumbot._handle_no_to_slot_offer(message_body)
        if _slot_no:
            appointment.add_conversation_message("assistant", _slot_no)
            delay = get_random_delay(sender=sender)
            threading.Thread(
                target=delayed_response, args=(sender, _slot_no, delay, message_id),
                kwargs={'tenant': tenant}, daemon=True,
            ).start()
            return

        # ── "NO" TO THE NEW-BUILD CONFIRMATION ────────────────────────────────
        # We asked "So you need a new plumbing installation for a new house?"
        # and recorded that service type presumptively in order to ask it. A no
        # makes it a wrong guess on the record, so it is cleared here — BEFORE
        # extraction runs, so their correction can take its place — and a bare
        # no gets asked what the plumbing job actually is. Without this the no
        # was not detected at all and the flow carried on to "All good, what
        # area are you in?", booking a visit for a job we could not name, on a
        # service type the lead had just rejected (prod 2026-09-01).
        if plumbot._last_assistant_was_new_build_confirm():
            _nb_no = plumbot._handle_new_build_rejection(message_body)
            if _nb_no:
                appointment.add_conversation_message("assistant", _nb_no)
                delay = get_random_delay(sender=sender)
                threading.Thread(
                    target=delayed_response, args=(sender, _nb_no, delay, message_id),
                    kwargs={'tenant': tenant}, daemon=True,
                ).start()
                return

        # ── DATE-STAGE TIMELINE PIVOT (deterministic dispatch) ────────────────
        # At the date/time stage, when the lead pivots to timeline instead of
        # answering, dispatch on offered_date vs today — >7 days out parks the lead;
        # within a week keeps booking. DeepSeek already resolved the date; code only
        # does the math + state transition. No extra API call (reuses _uclass).
        if _next_question in ('availability_date', 'availability_time') and \
                uc_pivoted_to_timeline(_uclass):
            _pivot_reply = plumbot._dispatch_timeline_pivot(
                _next_question,
                uc_offered_date(_uclass),
                uc_offered_timeframe(_uclass),
                _today_str,
                detect_language_simple(message_body),
            )
            if _pivot_reply is not None:
                appointment.add_conversation_message("assistant", _pivot_reply)
                delay = get_random_delay(sender=sender)
                threading.Thread(
                    target=delayed_response, args=(sender, _pivot_reply, delay, message_id), kwargs={'tenant': tenant},
                    daemon=True,
                ).start()
                return

        # ── FAQ LAYER ─────────────────────────────────────────────────────────
        # Skip FAQ when DeepSeek flagged the message as delay/complaint/out-of-scope
        # (those go to their own handlers, never a generic FAQ answer), and for
        # explicit photo/catalogue requests — a broad trigger like "do you have…"
        # must not swallow "do you have pics of your work".
        from bot.faq import match_faq_topic, faq_fact
        _faq_skip = (
            uc_intent(_uclass) in ('delay_signal', 'complaint', 'out_of_scope')
            or _explicitly_requests_photos(message_body)
            or _explicitly_requests_catalogue(message_body)
        )
        # Tenant passed so the lead's OWN business and plumber names are
        # triggers — the static lists carried Homebase's.
        _faq_topic = None if _faq_skip else match_faq_topic(message_body, tenant=tenant)

        # Asking for the plumber is re-engagement, and the FAQ answers before
        # the delay handler ever runs — so clear any holding state here, or the
        # lead gets their answer and then walks back into the delay flow on
        # their next message as though they had never come back.
        if _faq_topic == 'contact':
            try:
                from bot.out_of_scope_handler import _read_pending, _clear_pending
                if _read_pending(appointment):
                    print("⏭️ Contact request re-engages the lead — clearing the delay hold")
                    _clear_pending(appointment)
            except Exception as _pend_clear_exc:
                print(f"⚠️ Could not clear delay hold on contact request: {_pend_clear_exc}")

        # AI catch: a typo'd / loose service-availability question the keyword
        # topic-match missed ("Do you for shower rooms") but the classifier read as a
        # specific product. Route it to the clean services continuation. Gated so it
        # can't hijack a price ask, a booking answer, or another FAQ topic.
        _PRODUCT_LABEL = {
            'shower_cubicle': 'shower cubicle', 'geyser': 'geyser', 'vanity': 'vanity',
            'toilet': 'toilet', 'chamber': 'side chamber', 'tub_sales': 'tub',
            'standalone_tub': 'freestanding tub', 'bathtub_installation': 'tub',
        }
        _ai_service_q = (
            not _faq_skip
            and _faq_topic in (None, 'services')
            and uc_product_intent(_uclass) in _PRODUCT_LABEL
            and not uc_answered_current_question(_uclass)
            and not plumbot._asks_price_figure(message_body, classification=_uclass)
            # A bare "Yes"/"ok" asks nothing — it ANSWERS us, almost always the
            # tie-down we just closed on. The classifier keeps product_intent alive
            # across turns, so without this the stale intent answered a question the
            # lead never asked: they agreed the tub price and got "Yes, we handle tub
            # and all related plumbing work" (prod 2026-07-29, lead 670). The
            # customer's own words win over a carried-over intent — see CLAUDE.md.
            and not plumbot._is_bare_affirmation(message_body)
            # A size/spec ask ("how big are your tubs") is NOT an availability
            # question — it must fall through to the measurements reply.
            and not plumbot._is_size_spec_question(message_body)
            and _next_question in ('service_type', 'project_description')
            # Only route an OPENING "do you have X" — once we've asked the
            # description question, a product mention ("a tub and chamber") is the
            # ANSWER to it, not a new service question. Don't hijack it.
            and plumbot._get_question_retry_count('project_description') == 0
        )
        if _ai_service_q and _faq_topic is None:
            _faq_topic = 'services'
            print(f"🤖 AI-routed service question via product_intent="
                  f"{uc_product_intent(_uclass)}: '{message_body[:60]}'")

        if _faq_topic is not None:
            _faq_fact = faq_fact(_faq_topic, tenant=tenant)
            # This tenant's profile has no fact for the topic. Never dead-end on
            # that: the FAQ block used to build a None reply from it, store the
            # None in conversation_history and send nothing, so the lead's
            # question got pure silence (prod 2026-07-29: tenant jd3 has no
            # location fact — "Where are you located" went unanswered). Fall
            # through to the normal pipeline, which still answers.
            if not (_faq_fact or '').strip():
                print(f"⚠️ No FAQ fact for topic={_faq_topic} on tenant="
                      f"{getattr(tenant, 'slug', None)} — falling through")
                _faq_topic = None

        if _faq_topic is not None:
            # AI-primary: answer contextually, grounded in the fact so it stays
            # accurate but never sounds copy-pasted; canned fact (+ qualifying close)
            # is the fallback. When the lead asked whether we do a SPECIFIC service
            # ("do you have shower rooms"), continue the sale — name it back and ask
            # if it's the only thing they want sorted.
            _faq_lang = detect_language_simple(message_body)
            _faq_service_q = _ai_service_q or (
                _faq_topic == 'services'
                and plumbot._is_product_availability_question(message_body)
                and not plumbot._is_size_spec_question(message_body)
            )
            # The service they asked about IS their project — capture it now so a
            # following "Yes" ("is a shower room the only thing?") advances the flow
            # instead of re-asking. AI extraction first, then the classifier's product
            # label (clean even on typos), then a light message derive.
            _item = None
            if _faq_service_q:
                # Prefer the classifier's clean product label ("shower cubicle",
                # "tub") over the raw message, so the scripted continuation reads
                # right ("Is a tub the only thing?") — not "Is a A tub and chamber…".
                _item = (_PRODUCT_LABEL.get(uc_product_intent(_uclass))
                         or uc_extracted(_uclass).get('project_description')
                         or _derive_service_item(message_body))
                # Carry quantity + accessories from the customer's own words
                # ("2x shower cubicles and asseries" → "2 shower cubicles and
                # accessories") so the confirm, the description, and later scope
                # pricing all reflect what they actually asked for.
                if _item:
                    _item = plumbot._scope_item_phrase(message_body, str(_item))
                if _item and not appointment.project_description:
                    appointment.project_description = str(_item)[:120]
                    appointment.save(update_fields=['project_description'])
                    # Await the "is that the only thing?" answer so the next turn can
                    # append if they name more (see SERVICE-CONFIRM FOLLOW-UP).
                    appointment._add_notes_tag('[SERVICE_CONFIRM_PENDING]')
                    print(f"📝 Captured service item as project_description: {_item}")

            # FIRST pass = exact scripted reply (consistency); only paraphrase via
            # ai_answer_faq on a REPEAT of the same topic (so a re-ask isn't
            # word-for-word identical). Follow the script first, vary on retry.
            _faq_done_tag = f'[FAQ_DONE:{_faq_topic}]'
            _faq_repeat = _faq_done_tag in (appointment.internal_notes or '')
            if _faq_repeat:
                _faq_reply = (
                    plumbot.ai_answer_faq(message_body, _faq_fact, _faq_lang,
                                          service_question=_faq_service_q)
                    or (plumbot._service_continuation_reply(_item, _faq_lang)
                        if _faq_service_q else
                        plumbot._append_tiedown(_faq_fact, _faq_lang))
                )
            elif _faq_service_q:
                _faq_reply = plumbot._service_continuation_reply(_item, _faq_lang)
            else:
                _faq_reply = plumbot._append_tiedown(_faq_fact, _faq_lang)
            appointment._add_notes_tag(_faq_done_tag)
            appointment.add_conversation_message("assistant", _faq_reply)
            delay = get_random_delay(sender=sender)
            threading.Thread(
                target=delayed_response,
                args=(sender, _faq_reply, delay, message_id),
                kwargs={'tenant': tenant},
                daemon=True,
            ).start()
            return


        _quick_service_check = uc_as_service_inquiry(_uclass)

        # Cross-check the LLM product intent against the customer's own product
        # word. The deterministic keyword resolver only fires when the customer
        # literally named a product, and on those inputs it is authoritative — the
        # LLM sometimes lands on the wrong product FAMILY even at HIGH confidence
        # (observed: "bathroom cubicles" → tub_sales, "rain shower" → tub_sales).
        # So whenever the keyword resolver names a DIFFERENT family than the LLM,
        # the customer's literal word wins. This also covers the old empty-intent
        # case (family(none)=None ≠ any product) and the LOW-confidence case. A
        # SAME-family disagreement (tub_sales vs standalone_tub) keeps the LLM's
        # more specific choice. Quoted-message handling stays separate below.
        # Don't let a single product keyword override the LLM's combined_pricing
        # on a genuine MULTI-ITEM message ("how much tab and shower") — that
        # collapsed a two-item price ask down to one item and priced only the
        # shower. When the LLM saw combined_pricing and the message names a job /
        # multiple items, trust it so the customer gets prices for everything.
        _multi_item_combined = (
            _quick_service_check.get('intent') == 'combined_pricing'
            and plumbot._is_job_quote_request(message_body, classification=_uclass)
        )
        if not quoted_text and not _multi_item_combined:
            _kw_intent = _keyword_product_intent(message_body, plumbot.tenant_cfg)
            if (_kw_intent
                    and _product_family(_kw_intent)
                        != _product_family(_quick_service_check.get('intent'))):
                print(
                    f"🎯 Keyword product override: {_kw_intent} "
                    f"(LLM said {_quick_service_check.get('intent')})"
                )
                _quick_service_check = {'intent': _kw_intent, 'confidence': 'HIGH'}

        # When the customer replies to a specific earlier message (e.g. a
        # portfolio photo), the quote tells us which item "this one" refers to.
        # Map it to a product intent DETERMINISTICALLY: the customer's own
        # product word wins, else the quoted photo's. We use the keyword resolver
        # rather than the LLM because short photo captions get mis-mapped by the
        # classifier (observed: a "rain shower" quote → tub_sales). The keyword
        # map ('shower' → shower_cubicle) is exact. Raw message_body stays
        # untouched for the rule engine; only this classification sees the quote.
        if quoted_text:
            _det_intent = (
                _keyword_product_intent(message_body, plumbot.tenant_cfg)
                # The TITLE only, never the vision sentences after it. Those are
                # prose, and prose carries incidental fixture words: a storage
                # tank photo described as "on a steel tower structure with pipe"
                # resolved to pipe_repair and priced pipe work (prod, barmak,
                # 2026-08-28). The tenant's own label is the deliberate signal.
                or _keyword_product_intent(_quoted_title(quoted_text),
                                           plumbot.tenant_cfg)
            )
            if _det_intent and _det_intent != _quick_service_check.get('intent'):
                print(
                    f"🔗 Quote-derived service intent: {_det_intent} "
                    f"(was {_quick_service_check})"
                )
                _quick_service_check = {'intent': _det_intent, 'confidence': 'HIGH'}

        _is_clear_product_inquiry = (
            _quick_service_check.get('intent') not in ('none', 'pictures') and
            _quick_service_check.get('confidence') == 'HIGH'
        )
        _pricing_signals = (
            'how much', 'price', 'cost', 'quote', 'quotation',
            'marii', 'mari', 'mutengo', 'zvinodhura', 'zvese',
        )
        _has_pricing_signal = any(p in message_body.lower() for p in _pricing_signals)

        # When a delay-signal lead was just offered the portfolio and is replying
        # to receive it HERE on WhatsApp (we're in the delay_email pending state),
        # that "send it on WhatsApp / to this number" reply must reach the delay
        # handler (STEP 1b) so it sends the lead-magnet PDF — the same document
        # we'd have emailed — not the image gallery. The photo handlers below
        # would otherwise intercept it and send loose photos instead. Only skip
        # them when the reply is actually a delivery-channel ask, so a genuine
        # fresh photo request still works.
        _delay_email_wants_wa = False
        try:
            from .out_of_scope_handler import _read_pending, wants_whatsapp_delivery
            _pending = _read_pending(appointment)
            _delay_email_wants_wa = bool(
                _pending and _pending.get('category') == 'delay_email'
                and wants_whatsapp_delivery(message_body)
            )
        except Exception as _pend_exc:
            print(f"⚠️ delay_email pending check failed: {_pend_exc}")

        # -- STEP 0: Multi-intent compose (2+ questions in one message) ---------
        # e.g. "where are you based and how much" → answer both in one reply.
        # Only fires for 2+ answerable INFO intents; booking-related messages
        # fall through to the normal flow untouched.
        try:
            _multi = plumbot.compose_multi_answer(message_body)
        except Exception as _multi_exc:
            print(f"⚠️ Multi-intent compose failed: {_multi_exc}")
            _multi = None
        if _multi and _multi.get('reply'):
            print(f"🧩 Multi-intent compose — intents={_multi.get('intents')}")
            if _multi.get('send_photos'):
                send_previous_work_photos(sender, appointment)
            for _pi in (_multi.get('intents') or []):
                if _pi not in ('location', 'hours', 'pictures', 'other'):
                    _mark_pricing_intent_sent(appointment, _pi)
            reply_text = _multi['reply']
            appointment.add_conversation_message("assistant", reply_text)
            appointment.last_outbound_at = timezone.now()
            appointment.last_contacted_at = appointment.last_outbound_at
            appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
            delay = get_random_delay(sender=sender)
            threading.Thread(
                target=delayed_response, args=(sender, reply_text, delay, message_id), kwargs={'tenant': tenant}, daemon=True
            ).start()
            return

        # -- STEP 0a: Any catalogue / pictures request → send the WHOLE gallery -
        # When a lead asks for the catalogue, pictures, photos or to "see our
        # work", send every image in previous_work_photos so they can spot
        # anything else they like — we deliberately do NOT narrow to a single
        # matched piece here. Exception: an explicit "products AND prices" ask
        # falls through to STEP 0d so the price list still goes out alongside.
        if (_explicitly_requests_photos(message_body)
                and not _explicitly_requests_catalogue(message_body)
                and not _delay_email_wants_wa):
            print("Catalogue/pictures request → sending full previous-work gallery")
            if send_previous_work_photos(sender, appointment):
                return
            # No images on disk → fall through to the existing handlers below.

        # -- STEP 0b: Specific portfolio piece request --------------------------
        # If the customer points at ONE distinctive piece ("the gold taps one",
        # "how much was the black bathtub", "that marble shower") WITHOUT asking
        # for the catalogue/pictures outright, send just that image with its
        # title/price/story caption. Generic photo/catalogue asks are handled by
        # STEP 0a above; ambiguous matches return None and fall through to STEP 1.
        try:
            from bot import portfolio_catalog
            _portfolio_item = portfolio_catalog.match_portfolio_item(message_body, tenant=tenant)
        except Exception as _pc_exc:
            print(f"Portfolio item match failed: {_pc_exc}")
            _portfolio_item = None
        if _portfolio_item is not None:
            print(f"Specific portfolio item request: {_portfolio_item['id']}")
            if send_portfolio_item(sender, _portfolio_item, appointment):
                return

        # -- STEP 0c: Portfolio menu request ------------------------------------
        # Customer wants to know WHAT we can show ("what can you show me?") but
        # hasn't named a piece — reply with the text menu of available pieces so
        # they can pick one. Distinct from "send me your portfolio" (gallery).
        try:
            _menu_request = portfolio_catalog.is_catalogue_menu_request(message_body)
            _menu_text = portfolio_catalog.catalogue_overview(tenant=tenant) if _menu_request else None
        except Exception as _menu_exc:
            print(f"Portfolio menu check failed: {_menu_exc}")
            _menu_text = None
        if _menu_text:
            print("Portfolio menu request — sending catalogue overview")
            appointment.add_conversation_message("assistant", _menu_text)
            appointment.last_outbound_at = timezone.now()
            appointment.last_contacted_at = appointment.last_outbound_at
            appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
            delay = get_random_delay(sender=sender)
            threading.Thread(
                target=delayed_response, args=(sender, _menu_text, delay, message_id), kwargs={'tenant': tenant}, daemon=True
            ).start()
            return

        # -- STEP 0d: Product catalogue + prices request ------------------------
        # "send your products and prices" → send the catalogue images AND the
        # price list alongside (not a single product's price line). Falls back to
        # previous-work photos if no catalogue images are configured.
        if _explicitly_requests_catalogue(message_body):
            print("Product catalogue + prices request detected")
            images_queued = send_catalogue_images(sender, appointment)
            if not images_queued:
                images_queued = send_previous_work_photos(sender, appointment)
            price_text = build_catalogue_price_text(
                plumbot._get_pricing_followup_prompt('english'),
                tenant=getattr(appointment, 'tenant', None),
            )
            appointment.add_conversation_message("assistant", price_text)
            appointment.pricing_overview_sent = True
            appointment.last_outbound_at = timezone.now()
            appointment.last_contacted_at = appointment.last_outbound_at
            appointment.save(update_fields=[
                'pricing_overview_sent', 'last_outbound_at', 'last_contacted_at'
            ])
            delay = get_random_delay(sender=sender)
            threading.Thread(
                target=delayed_response,
                args=(sender, price_text, delay, message_id),
                kwargs={'tenant': tenant},
                daemon=True,
            ).start()
            return

        # -- STEP 1: Previous work photo request --------------------------------
        print(f"Checking photo request: '{message_body}'")
        # An EXPLICIT photo request ("can I have a pic", "catalogue", "send photos")
        # must send photos even when products are named (which would otherwise
        # flag it as a product inquiry and suppress the photo path).
        _explicit_photo = _explicitly_requests_photos(message_body)
        if not _delay_email_wants_wa and (_explicit_photo or (
            uc_is_photo_request(_uclass) and not _is_clear_product_inquiry and not _has_pricing_signal
        )):
            print(f"Photo request detected (explicit={_explicit_photo})")
            # "What's a mixer can I have a pic" is two things. This step returns
            # outright, so a question asked alongside the request rides in on the
            # intro line or it is never answered at all (prod: fifteen photos and
            # no answer).
            _definition = _definition_answer(message_body)
            _photo_intro = (
                f"{_definition}\n\nHere are some examples of our previous work "
                "so you can see it fitted."
            ) if _definition else None
            photos_queued = send_previous_work_photos(
                sender, appointment, intro=_photo_intro)
            if photos_queued:
                return
            fallback_reply = (
                "I can share previous-work photos, but they are not configured yet. "
                "Please ask our team and we will send them shortly."
            )
            appointment.add_conversation_message("assistant", fallback_reply)
            delay = get_random_delay(sender=sender)
            threading.Thread(target=delayed_response, args=(sender, fallback_reply, delay), kwargs={'tenant': tenant}, daemon=True).start()
            return

        # -- STEP 1a: Budget objection after a price tie-down -------------------
        # Must run BEFORE the OOS/complaint step: a soft decline ("not really") to
        # "That sit alright with your budget?" otherwise gets mis-flagged as a
        # complaint and deflected to the plumber.
        _budget_reply = None
        if (plumbot._last_assistant_was_price_tiedown()
                and plumbot._is_budget_decline(message_body)):
            _budget_reply = plumbot._handle_budget_objection(
                detect_language_simple(message_body)
            )
            print(f"💸 Budget objection (webhook): '{message_body[:60]}'")
        if _budget_reply is not None:
            appointment.add_conversation_message("assistant", _budget_reply)
            appointment.last_outbound_at = timezone.now()
            appointment.last_contacted_at = appointment.last_outbound_at
            appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
            delay = get_random_delay(sender=sender)
            threading.Thread(
                target=delayed_response, args=(sender, _budget_reply, delay, message_id), kwargs={'tenant': tenant}, daemon=True
            ).start()
            return

        # -- STEP 1b: Out-of-scope / delay / complaint --------------------------
        from .out_of_scope_handler import handle_out_of_scope
        oos_reply = handle_out_of_scope(
            message_body, appointment,
            precomputed=uc_as_oos_classification(_uclass),
            classification=_uclass,
        )
        if oos_reply is not None:
            appointment.add_conversation_message("assistant", oos_reply)
            appointment.last_outbound_at = timezone.now()
            appointment.last_contacted_at = appointment.last_outbound_at
            appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
            delay = get_random_delay(sender=sender)
            threading.Thread(
                target=delayed_response, args=(sender, oos_reply, delay, message_id), kwargs={'tenant': tenant}, daemon=True
            ).start()
            return

        # -- STEP 1c: Price on a quoted portfolio photo -------------------------
        # The customer pointing at one of OUR photos and asking the price is the
        # most specific signal there is, so it is resolved before the family-based
        # pricing steps — which only know Homebase's product list and would other-
        # wise answer a borehole question with the bathroom package (prod, barmak,
        # 2026-08-27). Only fires when that photo carries the tenant's own price.
        # Sent-and-returned like STEP 1b, NOT left for the steps below to
        # respect: nothing between here and STEP 3 guards on `reply is None`,
        # so setting it merely built the right answer and then watched STEP 2
        # overwrite it (prod, barmak, 2026-08-28 — the quoted-photo price was
        # composed and then replaced with pipe-repair rates).
        if quoted_text and _explicitly_requests_price(message_body):
            _quoted_reply = _quoted_portfolio_price_reply(
                plumbot, appointment, quoted_text, message_body)
            if _quoted_reply is not None:
                appointment.add_conversation_message("assistant", _quoted_reply)
                appointment.last_outbound_at = timezone.now()
                appointment.last_contacted_at = appointment.last_outbound_at
                appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
                delay = get_random_delay(sender=sender)
                threading.Thread(
                    target=delayed_response,
                    args=(sender, _quoted_reply, delay, message_id),
                    kwargs={'tenant': tenant}, daemon=True,
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
            # "Have we ever chased this lead?" - the COUNTER resets to zero on
            # every reply now (reset_followup_sequence), so ask the timestamp,
            # which is the durable record of a follow-up having gone out.
            (appointment.followup_count > 0) or
            (getattr(appointment, 'last_followup_sent', None) is not None) or
            (appointment.conversation_history and len(appointment.conversation_history) > 4)
        )

        print(f"Checking service inquiry: '{message_body}'")
        inquiry = _quick_service_check
        print(f"Service inquiry result: {inquiry}")

        PRODUCT_INTENTS = {
            'tub_sales', 'standalone_tub', 'geyser', 'shower_cubicle',
            'vanity', 'bathtub_installation', 'toilet', 'chamber',
            'wall_hung_toilet',
            'facebook_package', 'location_ask', 'location_visit',
            'previous_quotation', 'pictures', 'combined_pricing',
        }
        NON_PRICING_AUTO_REPLY_INTENTS = {
            'location_ask', 'location_visit', 'previous_quotation', 'pictures',
            'combined_pricing',
        }
        PRICING_AUTO_REPLY_INTENTS = {
            'geyser', 'shower_cubicle', 'vanity', 'toilet', 'chamber',
            'wall_hung_toilet',
            'drain_unblocking', 'pipe_repair', 'geyser_repair', 'toilet_repair',
            'facebook_package',
        }
        intent = inquiry.get('intent')
        # A service only this tenant sells (tiling, gutters, a pump) behaves
        # like any other named product: priced when they ask a price, never
        # volunteered. Deliberately NOT added to PRICING_AUTO_REPLY_INTENTS —
        # "do you do tiling?" must get a yes, not a price list.
        if is_tenant_item_intent(intent):
            PRODUCT_INTENTS = PRODUCT_INTENTS | {intent}
        price_requested = _explicitly_requests_price(message_body)
        # A demonstrative reply to a quoted portfolio photo ("this one?", "and
        # this one?") is an elliptical price ask on the quoted item — treat it as
        # a price request so STEP 2 prices the quoted item directly instead of
        # skipping it as a project description and leaning on the standalone-Q rescue.
        quoted_photo_price_ref = bool(
            quoted_text and _is_quoted_item_reference(message_body)
        )
        if quoted_photo_price_ref:
            price_requested = True

        # Split the price signal: asking for a FIGURE (how much / price / cost)
        # gets approximate prices; asking for *a quote* leans to the free site
        # visit (the quote is delivered there), per business policy. A quoted-
        # photo "this one?" is treated as a figure ask.
        asks_figure = (plumbot._asks_price_figure(message_body, classification=_uclass)
                       or quoted_photo_price_ref)
        asks_quote = plumbot._asks_for_quote(message_body)

        _is_specific_product_inquiry = (
            intent in PRICING_AUTO_REPLY_INTENTS and
            inquiry.get('confidence') == 'HIGH'
        )
        should_bypass_mid_conversation_gate = (
            intent in NON_PRICING_AUTO_REPLY_INTENTS or
            price_requested or
            _is_specific_product_inquiry
        )

        # When the bot is actively collecting the service / project description
        # and the lead simply NAMES a service (e.g. "Shower Cubicles") without
        # explicitly asking a price, let the booking flow capture it and advance
        # instead of dropping a price pitch and stalling.
        booking_capture_phase = False
        is_project_description = False
        try:
            booking_capture_phase = (
                plumbot.get_next_question_to_ask() in ('service_type', 'project_description')
            )
            # Declarative project descriptions ("I want a bathroom with a toilet
            # for my new home") should be acknowledged + progressed, never priced
            # — even after the service/description fields are already captured.
            is_project_description = plumbot._looks_like_project_description_reply(message_body)
        except Exception:
            pass

        if (booking_capture_phase or is_project_description) and not price_requested:
            print("Skipping service inquiry reply - lead is describing their project (no price asked)")
        elif _is_unprompted_carryover_pricing(
            intent, message_body, price_requested, PRICING_AUTO_REPLY_INTENTS
        ):
            print(
                f"Skipping service inquiry reply - '{message_body}' names no product "
                f"and no price asked (carried-over intent: {intent})"
            )
        elif mid_conversation and not should_bypass_mid_conversation_gate:
            print("Skipping service inquiry reply - mid-conversation and no explicit info/price request")
        elif ((intent in PRICING_AUTO_REPLY_INTENTS or intent == 'combined_pricing')
                and not asks_figure
                and (asks_quote or plumbot._is_job_quote_request(
                    message_body, classification=_uclass))):
            # A QUOTE request ("need a quote to fit tub and shower"), or a job /
            # multi-item request, with NO explicit how-much/price ask routes to the
            # free on-site quote — not a chat price block. Applies to combined_pricing
            # too (a multi-item quote request classifies there). The quote is
            # delivered at the visit, so lean toward setting it up. An actual
            # how-much/price ("how much to fit tub and shower") falls through below.
            print("Quote / job request (no price figure asked) -> routing to free on-site quote")
            reply = plumbot._build_job_quote_reply(
                language=detect_language(message_body), message=message_body
            )
        elif asks_figure and (
            plumbot._names_multiple_products(message_body)
            or (plumbot._asks_about_labour(message_body)
                and len(plumbot._context_product_families(message_body)) >= 2)
            # A bare "how much" naming nothing, with a captured scope on file
            # ("2 shower cubicles and accessories") must price THAT scope — not
            # whichever intent the classifier guessed (prod: got the Facebook
            # tub package instead of the cubicles the lead named).
            or (not plumbot._product_families_in(message_body)
                and plumbot._context_product_families(message_body))
        ):
            # Explicit how-much/price naming MULTIPLE items ("how much tab and
            # shower") -> price every item named, not just the one a single-intent
            # classifier or keyword override happened to pick. A context-free
            # labour follow-up ("how much is labour") after a multi-item ask is
            # routed here too, so it covers every item the lead mentioned (from
            # project_description) with a supply + labour split — not just one.
            print("Multi-item price ask -> combined approximate prices for each item")
            reply = plumbot._build_combined_price_reply(
                message_body, language=detect_language(message_body)
            )
        elif intent != 'none' and (
            inquiry.get('confidence') == 'HIGH' or intent in PRODUCT_INTENTS
        ):
            if (intent not in NON_PRICING_AUTO_REPLY_INTENTS and
                    intent not in PRICING_AUTO_REPLY_INTENTS and
                    not price_requested):
                print(f"Skipping priced service inquiry for intent: {intent} - no explicit price request")
            else:
                already_sent = _has_sent_pricing_for_intent(appointment, intent)
                # The customer's own words override the already-sent gate: when
                # they point at a SPECIFIC photo and ask its price ("this one how
                # much", "what about this one"), that's an explicit price ask on
                # the quoted piece — answer it even if we've priced that intent
                # before, otherwise distinct photo asks fall through to the
                # generic Facebook-package line or the repeat-question handler.
                if already_sent and intent != 'combined_pricing' and not quoted_photo_price_ref:
                    print(f"Skipping already-sent service inquiry: {intent}")
                else:
                    if not already_sent:
                        print(f"Service inquiry matched (first time): {intent}")
                        _mark_pricing_intent_sent(appointment, intent)
                    elif quoted_photo_price_ref:
                        print(f"Re-pricing quoted photo despite already-sent: {intent}")
                    else:
                        print(f"Re-sending combined pricing reply for: {intent}")

                    # A price ask on a SPECIFIC quoted photo gets a purpose-built
                    # reply: lead with the full pricing for that piece (every item
                    # in the shot, verbatim from the catalogue), then a
                    # visit-capture close — instead of the generic service-inquiry
                    # composition, which can open with an affirm/custom-build
                    # preamble and bury the price. Uncatalogued shots return None
                    # and fall back to the normal reply.
                    reply = None
                    if quoted_photo_price_ref:
                        try:
                            reply = plumbot.compose_quoted_photo_price_reply(
                                quoted_text,
                                language=detect_language(message_body),
                            )
                            if reply:
                                print(f"🧾 Photo-led price reply for '{quoted_text}'")
                        except Exception as _ppl_exc:
                            print(f"⚠️ Could not build photo-led price reply: {_ppl_exc}")
                    if reply is None:
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
                    # Wall-mount keys BEFORE the bare 'toilet' key — first match
                    # wins, and a wall-hung system prices at the chamber rate.
                    'wall mounted toilet': 'wall_hung_toilet',
                    'wall-mounted toilet': 'wall_hung_toilet',
                    'wall hung toilet':    'wall_hung_toilet',
                    'wall-hung toilet':    'wall_hung_toilet',
                    'toilet':   'toilet',
                    'chamber':  'chamber',
                    'drain':    'drain_unblocking',
                    'pipe':     'pipe_repair',
                }
                _recent = appointment.conversation_history or []
                _recent_text = ' '.join(
                    (m.get('content') or '') for m in _recent[-6:]
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
                # Built from the lead's OWN tenant offer, and None when they
                # have none — never Homebase's package restated to somebody
                # else's customer.
                try:
                    _recap_lang = detect_language(message_body) or 'english'
                except Exception:
                    _recap_lang = 'english'
                reply = plumbot._pricing_overview_recap(_recap_lang)

        # -- STEP 3b: Repeated-question detection ------------------------------
        if reply is None and uc_is_repeat(_uclass):
            repeat_info = detect_repeated_question(
                message_body,
                appointment.conversation_history or [],
            )
            if repeat_info:
                print(f"Repeated question detected — matched: '{repeat_info['matched_question'][:60]}'")
                lang = detect_language(message_body)
                plumber_contact = appointment.plumber_contact()
                reply = generate_repeat_clarification(
                    new_message=message_body,
                    matched_question=repeat_info['matched_question'],
                    matched_answer=repeat_info['matched_answer'],
                    plumber_number=plumber_contact,
                    language_hint=lang,
                    business_name=business_name_for(appointment),
                )

        # -- STEP 3c: New build — confirm the job before qualifying it ----------
        # Deliberately its own step, and deliberately HERE. It used to live
        # inside generate_contextual_response, which is the tail of STEP 4 —
        # so it only fired for messages that reached the field-question path.
        # "I want to build a new house" does not: the standalone-question
        # classifier calls it a GENUINE_QUESTION and the dynamic answerer
        # replies first, with improvised copy that changes every run (prod
        # probe 2026-09-01). Everything that should outrank the confirmation
        # has already had its turn by this line — the FAQ, photos and the
        # catalogue, the out-of-scope/delay handlers, and all three pricing
        # steps — so what is left is the scope-gathering conversation this
        # question belongs to. `_asks_price_figure` is still checked: a lead
        # who asked what a new build COSTS gets the figure, not a question
        # back (CLAUDE.md — the customer's own words override any gate).
        if reply is None and not plumbot._asks_price_figure(message_body, _uclass):
            reply = plumbot._new_build_confirmation(message_body, _uclass)
            if reply:
                print(f"🏗️  New build confirmed back: '{message_body[:60]}'")

        # -- STEP 4: Normal Plumbot processing ---------------------------------
        if reply is None:
            print("Running normal Plumbot processing")
            reply = plumbot.generate_response(
                message_body,
                precomputed_service_inquiry=inquiry,
                precomputed_classification=_uclass,
                quoted_context=quoted_text,
            )

        if reply is None:
            print("🔇 Conversation complete — no reply sent")
            return

        # ── HANDLER D: the memory check ──────────────────────────────────────
        # Last gate before anything is logged or sent: strip any question that
        # asks for something this lead has already given us. Every reply path
        # converges here, which is the point — get_next_question_to_ask honours
        # stored fields but the LLM paths compose their own copy and don't.
        from bot.views.plumbot.response_mixin import (
            strip_known_questions, strip_free_visit_claims,
            strip_repeat_free_visit, ensure_visit_price_note)

        # What the mixins composed, before this chain rewrites it. Most of the
        # ~20 reply paths log their own draft the moment they build it, and
        # everything below edits that text — so the draft has to be replaced,
        # not appended to, or the transcript shows the reply twice (once
        # without the price note, once with it). See
        # Appointment.replace_draft_assistant_turns.
        _draft_reply = reply

        reply, _re_asked = strip_known_questions(reply, appointment)
        if _re_asked:
            print(f"🧠 Memory check dropped re-asked field(s): {sorted(set(_re_asked))}")

        # Never promise a free visit a tenant charges for. Inert unless the
        # tenant has set a consultation fee on their Profile. The message is
        # passed so an explicit "what does the visit cost?" gets the figure
        # again, the same override the repeat stripper below honours.
        reply, _fee_fixed = strip_free_visit_claims(reply, appointment, message_body)
        if _fee_fixed:
            print("💵 Consultation fee set — free-visit wording replaced")

        # The visit is free ONCE. After the first time we've said it, "free"
        # comes off the visit in every later reply — repeating it reads as
        # pleading and drags a lead who already accepted the visit back onto
        # the subject of money. A lead who ASKS what it costs still gets the
        # straight answer: their own words outrank the gate.
        reply, _dequalified = strip_repeat_free_visit(reply, appointment, message_body)
        if _dequalified:
            print("✂️  Free-visit claim already made — not repeating it")

        # ...and the other half of the same rule: the FIRST message says what
        # the visit costs, so the lead never has to ask. Last in the chain
        # because the note is tenant-resolved and authoritative — the fee
        # stripper above would read "FREE if we do the job" as a promise to
        # break and drop it.
        reply, _noted = ensure_visit_price_note(reply, appointment, message_body)
        if _noted:
            print("💬 Visit price stated once, up front")

        # Nobody types an em dash on a phone. Dash punctuation is the clearest
        # tell that copy was drafted rather than texted, so it comes out of
        # every reply here — the one place all of them pass through, and the
        # only place that can reach what the LLM composed. Hyphens inside
        # words ("on-site", "all-in") are left alone: those are how people
        # actually write. This runs LAST, after the price note is appended.
        from bot.utils import strip_dashes
        _undashed = strip_dashes(reply)
        if _undashed != reply:
            print("➖ Dash punctuation removed from the reply")
        reply = _undashed

        # A reply may be split into two messages (acknowledgement, then the
        # question) via MESSAGE_SPLIT_MARKER — log each piece as its own turn so
        # WAMID stamping / quote-resolution line up per message.
        reply_parts = [
            p.strip() for p in reply.split(MESSAGE_SPLIT_MARKER)
        ] if MESSAGE_SPLIT_MARKER in reply else [reply]
        reply_parts = [p for p in reply_parts if p]
        appointment.replace_draft_assistant_turns(_draft_reply, reply_parts)
        appointment.last_outbound_at = timezone.now()
        appointment.last_contacted_at = appointment.last_outbound_at
        appointment.save(update_fields=['last_outbound_at', 'last_contacted_at'])
        print("Assistant reply saved to conversation history")

        delay = get_random_delay(sender=sender)
        cancel_event = threading.Event()
        with _pending_send_lock:
            _pending_send_events[sender] = cancel_event
        print(f"Random delay: {delay // 60} minute(s)")
        threading.Thread(
            target=delayed_response,
            args=(sender, reply_parts, delay, message_id, cancel_event),
            kwargs={'tenant': tenant},
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

IMAGE_DOC_EXT_MAP = {
    'image/jpeg': '.jpg',
    'image/jpg':  '.jpg',
    'image/png':  '.png',
    'image/webp': '.webp',
    'image/gif':  '.gif',
    'application/pdf': '.pdf',
}


def handle_media_message(sender, media_data, media_type, message_id=None,
                         quoted_id=None, tenant=None):
    try:
        media_id = media_data.get('id')
        mime_type = media_data.get('mime_type', '')
        # WhatsApp lets the customer type under a photo. That caption is the
        # customer's own words and must be answered like any other message,
        # never swallowed by the canned ack (CLAUDE.md: the customer's words
        # override any holding state).
        caption = (media_data.get('caption') or '').strip()
        # A PDF is the one upload that genuinely IS a plan. It is also the one
        # format DeepSeek vision cannot read (JPEG/PNG/GIF/WebP only), so it
        # must never reach a describe call.
        is_plan_document = mime_type == 'application/pdf'
        phone_number = f"whatsapp:+{sender}"

        appointment, created = Appointment.objects.get_or_create_lead(
            phone_number, tenant=tenant,
        )

        # Resolve the highlighted-message reference before anything is logged,
        # exactly as handle_text_message does — a media message can quote an
        # earlier turn too ("like this one" + their own photo).
        quoted_text = appointment.resolve_quoted_message(quoted_id) if quoted_id else None

        file_bytes = None
        if media_id:
            try:
                from .whatsapp_cloud_api import get_client_for_tenant
                file_bytes = get_client_for_tenant(tenant).download_media(media_id)
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

                from .media_library import customer_media_path
                timestamp = timezone.now().strftime('%Y%m%d_%H%M%S_%f')  # Added %f for microseconds to avoid filename collisions
                customer_slug = ''.join(
                    c for c in (appointment.customer_name or 'customer') if c.isalnum()
                )
                filename = f"{media_type}_{customer_slug}_{appointment.id}_{timestamp}{ext}"
                storage_path = customer_media_path(appointment.tenant, media_type, filename)

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
                    # Read BEFORE the update below: afterwards plan_status is
                    # 'plan_uploaded' either way, so a second, unrelated image
                    # would look like a plan we asked for.
                    _was_pending_upload = Appointment.objects.filter(
                        pk=appointment.pk, plan_status='pending_upload'
                    ).exists()

                    Appointment.objects.filter(
                        pk=appointment.pk, plan_status='pending_upload'
                    ).update(
                        plan_status='plan_uploaded',
                        plan_uploaded_at=timezone.now(),
                    )

                    # An upload only counts as THE PLAN when we actually asked
                    # for one, or when it is a PDF drawing. Previously ANY image
                    # set has_plan/plan_file, so a photo of a leak marked the
                    # lead as having architectural plans and took the plan slot.
                    # plan_status is deliberately NOT advanced for an unprompted
                    # PDF: that flag routes all later messages to
                    # handle_post_upload_messages.
                    if is_plan_document or _was_pending_upload:
                        # Only set plan_file if still empty (first upload wins)
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

        # Look at the photo. Fails open: describe_customer_image returns None on
        # an unsupported format (a PDF plan), a missing key or any API error, and
        # the flow below is identical to the blind behaviour in that case.
        image_description = None
        if file_bytes and not is_plan_document:
            try:
                from .services.vision import describe_customer_image
                image_description = describe_customer_image(
                    file_bytes, mime_type, tenant=tenant,
                )
            except Exception as vision_err:
                print(f"Vision describe raised, continuing blind: {vision_err}")

        # A drawing is a plan whatever its MIME type. is_plan_document only knows
        # PDFs, so a plan photographed or exported as an image was filed as an
        # ordinary photo — prod acked a customer's floor plan with "Got the
        # photo... I'll have it ready for when we come round", and their next
        # message was "Quote those": they had sent it to BE QUOTED.
        if image_description and _description_is_a_plan(image_description):
            print("Vision recognised a plan drawing — filing it as the plan")
            is_plan_document = True
            if saved_path:
                Appointment.objects.filter(
                    pk=appointment.pk, plan_file='').update(plan_file=saved_path)
                Appointment.objects.filter(
                    pk=appointment.pk, plan_file__isnull=True).update(plan_file=saved_path)
            Appointment.objects.filter(
                pk=appointment.pk, has_plan__isnull=True).update(has_plan=True)
            appointment.refresh_from_db()

        # Stamp the inbound WAMID on the photo turn. Without it a customer who
        # highlights their OWN photo to ask "this one, how much?" resolves to
        # None and the reply loses the picture they were pointing at — the
        # silent-quote-break CLAUDE.md warns every new send path about.
        if image_description:
            print(f"Vision saw: {image_description[:120]}")
            appointment.add_conversation_message(
                "user", f"[Sent {media_type}] {image_description}",
                message_id=message_id, quoted=quoted_text,
                image_description=image_description,
            )
        else:
            appointment.add_conversation_message(
                "user", f"[Sent {media_type}]",
                message_id=message_id, quoted=quoted_text,
            )

        # Classify the service type off what we saw, exactly as the text path
        # does off what they typed. Without this a lead who only ever sends a
        # photo keeps a blank service type on the dashboard.
        if image_description and not appointment.project_type:
            try:
                from .service_type_classifier import classify_and_save
                classify_and_save(appointment, image_description)
            except Exception as cls_err:
                print(f"Could not classify service type from the photo: {cls_err}")

        if appointment.chatbot_paused:
            print(f"Chatbot paused for whatsapp:+{sender}; skipped media acknowledgment.")
            return

        if caption:
            # Answer the caption through the SAME path as any other text —
            # lead source, delay signals, scoring, batching and the full
            # router — instead of the canned ack, which would ask them to
            # describe what they just described.
            print(f"Caption on {media_type} from {sender}: {caption[:80]}")
            handle_text_message(
                sender, {'body': caption}, message_id=message_id,
                quoted_id=quoted_id, tenant=tenant,
            )
        else:
            # An uncaptioned photo arriving while a text batch is still open (the
            # lead typed "how much", then sent the picture 20s later): the batch
            # reply is generated AFTER this description lands in history, so it
            # already covers the photo. Sending the ack as well double-messages
            # and stacks a second question, and the two 1-5 min delays land them
            # in random order. Media never joined the cancellation protocol that
            # _enqueue_for_response uses, so nothing stopped this. Let the batch
            # answer. (photo-then-text was always fine: the next text cancels a
            # pending ack.)
            with _pending_batch_lock:
                batch_open = bool(_pending_batches.get(sender))
            with _pending_send_lock:
                send_in_flight = _pending_send_events.get(sender) is not None

            if batch_open:
                print(f"Media ack skipped for {sender} — a text batch is still "
                      f"open and its reply will cover the photo")
            elif send_in_flight and image_description:
                # The batch already flushed and a reply is sleeping out its
                # send delay — but it was generated BEFORE this photo existed,
                # so it answers blind, and the ack would arrive as a second
                # message in random order. Re-enter the batch with what we saw:
                # that cancels the stale send and produces ONE reply covering
                # their question and the picture together.
                print(f"Photo joined the running exchange for {sender} — "
                      f"replacing the reply generated before we saw it")
                _enqueue_for_response(
                    sender, image_description, None, tenant=tenant,
                )
            else:
                _schedule_media_ack(sender, appointment, media_type, is_plan_document)

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
            content = (msg.get('content') or '').strip()
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
                content = (msg.get('content') or '')[:150]
                fallback_lines.append(f"{role}: {content}")
            return "Summary unavailable. Last messages:\n" + "\n".join(fallback_lines)
        except Exception:
            return "Summary unavailable."


