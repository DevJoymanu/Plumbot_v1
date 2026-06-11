"""
bot/out_of_scope_handler.py
============================
Handles messages that fall outside Plumbot's booking scope gracefully.

Categories handled:
  out_of_scope    — services we don't offer (garages, painting, electric, etc.)
  delay_signal    — customer is not ready yet ("call me in 10 days", "I'm abroad")
  complaint       — frustration, price objection, skepticism about legitimacy
  in_scope        — normal message; this module does nothing, caller continues

Confidence layer:
  HIGH  → act immediately (reply or pass through)
  LOW   → ask a single targeted clarifying question, store pending state,
          then re-classify the customer's answer on the next turn

Pending clarification state is written to appointment.internal_notes as:
  [OOS_PENDING] category=<cat> original=<original message (url-encoded)>

Public API
----------
  classify_message(message, appointment) -> dict
      Returns {"category": str, "confidence": str, "detail": str}

  handle_out_of_scope(message, appointment) -> str | None
      Returns a reply string if the module should handle this message,
      or None if the bot should continue its normal booking flow.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote, unquote

import pytz

from django.conf import settings
from openai import OpenAI

logger = logging.getLogger(__name__)

_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")
_deepseek = (
    OpenAI(api_key=_DEEPSEEK_KEY, base_url="https://api.deepseek.com/v1")
    if _DEEPSEEK_KEY
    else None
)

PLUMBER_NUMBER_FALLBACK = "+263774819901"

# ── Pending-clarification state helpers ──────────────────────────────────────
# Stored in appointment.internal_notes as a single tagged line so no migration
# is needed.  Format:
#   [OOS_PENDING] category=out_of_scope original=Do%20you%20do%20garages%3F

_PENDING_TAG = "[OOS_PENDING]"
_DELAY_SIGNAL_TAG = "[DELAY_SIGNAL]"


def _write_pending(appointment, category: str, original_message: str) -> None:
    """Record that we asked a clarifying question and are awaiting the answer."""
    encoded = quote(original_message, safe="")
    tag_line = f"{_PENDING_TAG} category={category} original={encoded}"
    notes = (appointment.internal_notes or "").strip()
    # Remove any stale pending tag first
    notes = _clear_pending_from_text(notes)
    appointment.internal_notes = f"{notes}\n{tag_line}".strip()
    appointment.save(update_fields=["internal_notes"])
    logger.debug("OOS pending written: category=%s", category)


def _read_pending(appointment) -> Optional[dict]:
    """
    Return {"category": str, "original": str} if a clarification is pending,
    otherwise None.
    """
    notes = appointment.internal_notes or ""
    for line in notes.splitlines():
        if line.strip().startswith(_PENDING_TAG):
            rest = line.strip()[len(_PENDING_TAG):].strip()
            cat_match = re.search(r"category=(\S+)", rest)
            orig_match = re.search(r"original=(\S+)", rest)
            if cat_match:
                return {
                    "category": cat_match.group(1),
                    "original": unquote(orig_match.group(1)) if orig_match else "",
                }
    return None


def _clear_pending(appointment) -> None:
    """Remove the pending-clarification tag from internal_notes."""
    notes = appointment.internal_notes or ""
    cleaned = _clear_pending_from_text(notes)
    if cleaned != notes:
        appointment.internal_notes = cleaned
        appointment.save(update_fields=["internal_notes"])


def _clear_pending_from_text(text: str) -> str:
    lines = [l for l in text.splitlines() if not l.strip().startswith(_PENDING_TAG)]
    return "\n".join(lines).strip()


def has_delay_signal(appointment) -> bool:
    return _DELAY_SIGNAL_TAG in (appointment.internal_notes or "")


def mark_delay_signal(appointment, source_message: str = "") -> bool:
    marked = appointment.mark_delayed(source_message=source_message, save=True)
    if marked:
        logger.info("Delay signal written for appointment=%s from message='%s'",
                    getattr(appointment, 'id', None), (source_message or '')[:80])
    return marked


def send_lead_magnet_on_whatsapp(appointment) -> bool:
    """
    Send the portfolio/pricing PDF (our lead magnet) straight to the customer on
    WhatsApp as a document, instead of emailing it. Used when a lead asks to get
    it "on this number / on WhatsApp / right here". Guarded by a
    [LEAD_MAGNET_WA_SENT] note so we never double-send.
    """
    notes = appointment.internal_notes or ''
    if '[LEAD_MAGNET_WA_SENT]' in notes:
        return True
    try:
        import os as _os, tempfile as _tempfile
        from bot.whatsapp_cloud_api import whatsapp_api
        from bot.plumber_notifications import PORTFOLIO_PDF_PATH

        to = (getattr(appointment, 'phone_number', '') or '').replace('whatsapp:', '').strip()
        if not to:
            return False

        doc_path = PORTFOLIO_PDF_PATH
        if not _os.path.exists(doc_path):
            # Static asset missing — fall back to the generated portfolio bytes.
            from bot.customer_emails import generate_portfolio_pdf
            data = generate_portfolio_pdf()
            if not data:
                return False
            tmp = _tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
            tmp.write(data)
            tmp.close()
            doc_path = tmp.name

        whatsapp_api.send_local_document(
            to, doc_path,
            caption="Our portfolio of past projects plus a detailed pricing guide.",
            filename="HomeBase_Plumbers_Portfolio.pdf",
        )
        appointment.internal_notes = f'{notes}\n[LEAD_MAGNET_WA_SENT]'.strip()
        appointment.save(update_fields=['internal_notes'])
        logger.info("Lead magnet PDF sent on WhatsApp — apt %s",
                    getattr(appointment, 'pk', None))
        return True
    except Exception:
        logger.exception("send_lead_magnet_on_whatsapp failed — apt %s",
                         getattr(appointment, 'pk', None))
        return False


def _alert_plumber_no_email(appointment, iso_date=None) -> None:
    """
    Hand the plumber a lead that needs a follow-up but gave no email, so a human
    can chase it on WhatsApp. Guarded by [PLUMBER_FOLLOWUP_ALERTED] so the live
    flow fires it at most once; the reactivation cron alerts again on the agreed
    follow-up date.
    """
    notes = appointment.internal_notes or ''
    if '[PLUMBER_FOLLOWUP_ALERTED]' in notes:
        return
    try:
        from bot.plumber_notifications import send_plumber_followup_alert
        friendly = _friendly_iso(iso_date) if iso_date else None
        send_plumber_followup_alert(
            appointment, reason='no_email_followup', follow_up_date_str=friendly,
        )
        appointment.internal_notes = f'{notes}\n[PLUMBER_FOLLOWUP_ALERTED]'.strip()
        appointment.save(update_fields=['internal_notes'])
    except Exception:
        logger.exception("_alert_plumber_no_email failed — apt %s",
                         getattr(appointment, 'pk', None))


def detect_delay_signal_message(message: str, appointment=None) -> dict:
    """
    Detect whether a customer message signals they are deferring for later.

    Uses DeepSeek via classify_message for reliable natural-language intent
    detection — phrases like "am a bit tied up" or "still building" are caught
    without needing to enumerate every possible wording.

    Falls back to keyword matching only when DeepSeek is unavailable or no
    appointment context is provided.
    """
    text = (message or "").strip()
    if not text:
        return {"is_delay": False, "confidence": "LOW", "detail": "empty"}

    if appointment is not None:
        # Always use DeepSeek for substantive intent detection
        result = classify_message(text, appointment)
        return {
            "is_delay": result.get("category") == "delay_signal",
            "confidence": result.get("confidence", "LOW"),
            "detail": result.get("detail", ""),
        }

    # No appointment context — keyword fallback only
    keyword_result = _keyword_classify(text)
    return {
        "is_delay": keyword_result.get("category") == "delay_signal",
        "confidence": keyword_result.get("confidence", "LOW"),
        "detail": keyword_result.get("detail", ""),
    }

# ── Services we explicitly DO offer (used for context in the classifier) ──────
OUR_SERVICES = (
    "bathroom renovation, kitchen renovation, new plumbing installation, "
    "toilet supply and fitting, geyser installation, shower cubicle, vanity unit, "
    "bathtub installation, pipe repair, drain unblocking"
)

# ── Keyword lists — used ONLY as fallback when DeepSeek is unavailable ────────
# These are NOT used in the primary detection path. DeepSeek handles all live
# traffic. Keywords serve as a safety net when the API key is missing or the
# call fails.

_DELAY_PHRASES = (
    "call me later", "call you later", "i'll call you", "i will call",
    "will contact you", "i'll contact", "will reach out", "i'll reach out",
    "busy now", "busy at the moment", "not right now", "not ready",
    "come back to you", "i'll be in touch", "will be in touch",
    "get back to you", "i'll get back to you", "i will get back to you",
    "when i'm available", "when i am available", "when am available",
    "when i'm back", "when i am back", "when i get back", "back home",
    "in a few weeks", "in a few months", "10 days", "few days time",
    "needed to save your number", "save your number", "saved your number",
    "i'm abroad", "i am abroad", "i'm away", "i am away", "out of town",
    "travelling", "traveling", "not in harare", "not in zimbabwe",
    "ndichatumira", "ndichauya", "mangwana", "ndichaenda",
    "tied up", "a bit tied up", "bit tied up", "quite busy",
    "will notify you when", "will let you know when", "let you know when",
    "contact you when", "contact when", "when i'm done", "when am done",
    "not yet ready", "not ready yet", "still building", "still busy",
    "will come back", "come back later", "get back later",
    "when i finish", "when i am done", "when i'm finished",
)

_OOS_KEYWORDS = (
    "garage", "garages", "car port", "carport",
    "painting", "paint", "painter",
    "electrician", "electrical", "electric",
    "roofing", "roof", "tiles",
    "carpentry", "carpenter", "furniture",
    "landscaping", "garden", "gardener",
    "pest control", "security", "alarm",
    "air conditioning", "aircon", "hvac",
    "solar panels", "solar energy",
    "borehole",
)

_TRIVIAL_ACKS = {
    "ok", "okay", "k", "kk", "yes", "no", "sure", "thanks",
    "thank you", "noted", "cool", "sharp", "👍", "🙏",
    # Shona acknowledgments
    "hongu", "kwete", "zvakanaka", "zvaita", "zvaenda",
    "ndatenda", "maita", "maita basa", "mazvita",
    "ndinzwisisa", "ndanzwisisa", "inzwika",
    "ehe", "shuwa", "zvakanaka basa",
}


def _keyword_classify(message: str) -> dict:
    """
    Keyword-based classification used ONLY when DeepSeek is unavailable.
    Not called in the primary detection path.
    """
    msg = (message or "").lower()
    if any(phrase in msg for phrase in _DELAY_PHRASES):
        return {"category": "delay_signal", "confidence": "HIGH", "detail": "delay keyword matched"}
    if any(k in msg for k in _OOS_KEYWORDS):
        return {"category": "out_of_scope", "confidence": "LOW", "detail": "oos keyword matched"}
    return {"category": "in_scope", "confidence": "LOW", "detail": "keyword fallback default"}


# ── DeepSeek classifier ───────────────────────────────────────────────────────

def classify_message(message: str, appointment) -> dict:
    """
    Classify an incoming customer message into one of:
      in_scope        — normal booking / service inquiry; do nothing
      out_of_scope    — service we don't offer
      delay_signal    — customer is not ready yet
      complaint       — frustration, price objection, skepticism

    Uses DeepSeek for natural-language intent detection.
    Falls back to keyword matching only when the API is unavailable.

    Returns:
        {
            "category":   "in_scope" | "out_of_scope" | "delay_signal" | "complaint",
            "confidence": "HIGH" | "LOW",
            "detail":     short string explaining the classification
        }
    """
    # Trivial acks are always in_scope — skip the API call entirely
    msg_lower = (message or "").strip().lower()
    if msg_lower in _TRIVIAL_ACKS or len(msg_lower.split()) <= 2:
        return {"category": "in_scope", "confidence": "HIGH", "detail": "trivial ack"}

    # No DeepSeek available — use keyword fallback
    if not _deepseek:
        return _keyword_classify(message)

    # -- Conversation context for the classifier ------------------------------
    history = appointment.conversation_history or []
    recent_lines = []
    for msg in history[-6:]:
        role = "Customer" if msg.get("role") == "user" else "Bot"
        content = (msg.get("content") or "").strip()
        if content and not content.startswith("["):
            recent_lines.append(f"{role}: {content[:150]}")
    context_block = "\n".join(recent_lines) if recent_lines else "No prior conversation."

    project_type = appointment.project_type or "not yet specified"
    area = appointment.customer_area or "not yet specified"

    prompt = f"""You are a message classifier for Plumbot, the WhatsApp chatbot for Homebase Plumbers in Zimbabwe.

Our services: {OUR_SERVICES}

CONVERSATION SO FAR:
{context_block}

CUSTOMER'S LATEST MESSAGE:
"{message}"

APPOINTMENT STATE:
- Service type: {project_type}
- Area: {area}

Classify this message into EXACTLY ONE of the four categories below.

CATEGORIES:

in_scope
  The customer is engaging with the booking flow normally — asking about our services,
  providing details, asking about price, confirming availability, describing their project,
  answering bot questions, or asking follow-up questions about plumbing/bathroom/kitchen work.
  When in doubt, use in_scope.

out_of_scope
  The customer is asking about services we clearly DO NOT offer.
  Examples: "Do you do garages?", "Can you paint my house?", "Do you fix electrics?"
  Only use this when the message is unambiguously about a non-plumbing trade.
  Do NOT use for: plumbing services we offer, general questions, or mixed messages.

delay_signal
  The customer is signalling they are not ready right now but may be interested later.
  Examples: "I'll call you when I'm back", "Not right now, needed to save your number",
  "I'm abroad, will contact when I return", "Call me in 10 days".
  Only use when the customer is explicitly deferring — NOT for short acks like "ok thanks".

complaint
  The customer expresses frustration, price skepticism, or questions our legitimacy.
  Examples: "These prices are ridiculous", "Are you even a real plumber?",
  "I've never seen such expensive labour", "This doesn't seem right".
  Use only for clear negative sentiment directed at us, not general uncertainty.

RULES:
- Return ONLY valid JSON, no markdown, no extra text.
- If the message could be in_scope OR another category, choose in_scope.
- A short "ok" or "sure" is always in_scope.
- "Do you do X?" where X is clearly not plumbing = out_of_scope.
- Saving a number, noting they'll call later = delay_signal.

JSON FORMAT:
{{
  "category": "in_scope|out_of_scope|delay_signal|complaint",
  "confidence": "HIGH|LOW",
  "detail": "one brief phrase explaining why"
}}"""

    try:
        from bot.services.clients import deepseek_call
        import json as _json
        raw = deepseek_call(
            messages=[
                {"role": "system", "content": "Return ONLY valid JSON. No markdown, no explanation."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.1,
            max_tokens=150,
            json_response=True,
        )
        result = _json.loads(raw)

        category   = result.get("category", "in_scope")
        confidence = (result.get("confidence") or "LOW").upper()
        detail     = result.get("detail", "")

        valid_categories = {"in_scope", "out_of_scope", "delay_signal", "complaint"}
        if category not in valid_categories:
            logger.warning("Unexpected category from classifier: %s", category)
            category = "in_scope"

        logger.info(
            "OOS classifier: category=%s confidence=%s detail=%s message='%s'",
            category, confidence, detail, message[:80],
        )
        return {"category": category, "confidence": confidence, "detail": detail}

    except Exception as exc:
        logger.warning("OOS classifier failed: %s — falling back to keyword check", exc)
        return _keyword_classify(message)


# ── Clarifying question generator ────────────────────────────────────────────

# Hardcoded fallback clarifiers per category — used when DeepSeek is unavailable.
_FALLBACK_CLARIFIERS: dict[str, str] = {
    "out_of_scope": (
        "Just checking — are you looking for plumbing help? "
        "We do bathroom renovations, kitchen plumbing, geysers, and new installations. "
        "Is any of that what you had in mind?"
    ),
    "delay_signal": (
        "No problem at all! Are you still interested in getting the plumbing sorted, "
        "or would you prefer to pick this up at a later stage?"
    ),
    "complaint": (
        "Thanks for your message — happy to help sort this out. "
        "Is there something specific about the plumbing work or pricing I can clarify?"
    ),
}


def _generate_plumbing_reframe_question(message: str) -> str:
    msg = message.lower()

    if "garage" in msg or "carport" in msg:
        return "Just to check — are you looking for plumbing work in the garage like a sink, water pipes, or drainage?"

    if "paint" in msg:
        return "Just checking — is this part of a renovation where you also need plumbing like bathroom or kitchen fittings?"

    if "electric" in msg:
        return "Do you mean any plumbing work like geysers or water installations alongside the electrical work?"

    return "Just to confirm — is there any plumbing or water-related work involved in this?"

def _generate_clarifying_question(
    message: str,
    category: str,
    detail: str,
    appointment,
) -> str:
    """
    Generate a single, targeted clarifying question for a low-confidence
    classification.  The question is specific to *what* was ambiguous, not
    a generic "what do you mean?".

    Falls back to a hardcoded question if DeepSeek is unavailable.
    """
    if not _deepseek:
        return _FALLBACK_CLARIFIERS.get(category, _FALLBACK_CLARIFIERS["out_of_scope"])

    # Conversation context
    history = appointment.conversation_history or []
    recent_lines = []
    for msg in history[-4:]:
        role = "Customer" if msg.get("role") == "user" else "Bot"
        content = (msg.get("content") or "").strip()
        if content and not content.startswith("["):
            recent_lines.append(f"{role}: {content[:120]}")
    context_block = "\n".join(recent_lines) if recent_lines else "No prior conversation."

    category_guidance = {
        "out_of_scope": (
            "You are unsure whether the customer is asking about a service we offer "
            "(plumbing, bathroom/kitchen renovation) or something completely outside our trade "
            "(e.g. electrical, painting, building work). "
            "Ask ONE question to clarify what type of work they actually need."
        ),
        "delay_signal": (
            "You are unsure whether the customer is deferring (not ready yet) or still "
            "actively enquiring right now. "
            "Ask ONE question to understand whether they want to continue now or later."
        ),
        "complaint": (
            "You are unsure whether the customer is expressing frustration or simply asking "
            "a pointed question about our service or pricing. "
            "Ask ONE empathetic question to understand what specifically is concerning them."
        ),
    }

    guidance = category_guidance.get(
        category,
        "Ask ONE short question to understand what the customer means.",
    )

    prompt = f"""You are writing a WhatsApp message for Homebase Plumbers.

SITUATION:
{guidance}

RECENT CONVERSATION:
{context_block}

CUSTOMER MESSAGE:
"{message}"

WHY IT'S AMBIGUOUS:
{detail}

TASK:
Write ONE short clarifying question that steers the conversation toward plumbing services.

STRICT RULES:
1. ALWAYS assume the customer probably needs plumbing help
2. ALWAYS mention at least ONE specific plumbing service:
   - bathroom renovation
   - kitchen plumbing
   - geyser installation/repair
   - toilet/shower installation
   - pipe repair or drain blockage
3. ALWAYS give a simple choice (A/B or yes/no)
4. Keep it under 2 sentences
5. Sound natural, like WhatsApp (friendly, simple)
6. Use local tone ("sorted", "keen", "sharp")
7. DO NOT be vague or generic
8. DO NOT ask open-ended questions

GOOD EXAMPLES:
- "Just to check — are you looking for bathroom plumbing or something like a geyser installation?"
- "Got you — is this for a pipe issue or were you thinking of a full bathroom renovation?"
- "Just checking, is this something like a blocked drain or a new installation you want sorted?"

BAD EXAMPLES:
- "Can you clarify?"
- "What do you mean?"
- "Could you explain more?"

OUTPUT:
Only the question text. No quotes, no labels."""

    try:
        response = _deepseek.chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write short WhatsApp messages. Sound human. "
                        "Output only the message text."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=100,
        )
        question = response.choices[0].message.content.strip()
        question = question.replace("**", "").replace("__", "")
        logger.info("Generated clarifying question for category=%s: '%s'", category, question[:80])
        return question

    except Exception as exc:
        logger.warning("Clarifying question generation failed: %s", exc)
        return _FALLBACK_CLARIFIERS.get(category, _FALLBACK_CLARIFIERS["out_of_scope"])


# ── Pending-answer resolver ───────────────────────────────────────────────────

def _resolve_pending_clarification(answer: str, pending: dict, appointment) -> Optional[str]:
    """
    The customer has answered our clarifying question.
    Re-classify the answer in context of the original message and the pending
    category hint, then act on the result.

    Returns a reply string if the module should handle it, or None to pass
    control back to the normal booking flow.
    """
    original = pending.get("original", "")
    hint_category = pending.get("category", "")

    # Re-classify the answer — combined with the original message for context
    combined = f"{original} / {answer}".strip(" /")

    # Clear pending state before re-classifying so we don't loop
    _clear_pending(appointment)

    # Re-run the full classifier on the combined context
    result = classify_message(combined, appointment)
    new_category = result["category"]
    new_confidence = result["confidence"]

    logger.info(
        "Pending resolved: hint=%s new_category=%s confidence=%s answer='%s'",
        hint_category, new_category, new_confidence, answer[:60],
    )

    # If the clarification resolves to in_scope, pass through to booking flow
    if new_category == "in_scope":
        return None

    # If still LOW confidence after a clarification attempt, err on the side of
    # passing through rather than asking a third question — avoid infinite loops
    if new_confidence == "LOW":
        logger.info(
            "Still LOW confidence after clarification — passing through to booking flow"
        )
        return None

    # Act on the resolved category
    if new_category == "out_of_scope":
        lower = answer.lower()

        # 🟢 If user shows ANY plumbing intent → DO NOT reject
        plumbing_signals = [
            "sink", "pipe", "water", "drain", "toilet",
            "bathroom", "kitchen", "geyser", "install", "fix"
        ]

        if any(sig in lower for sig in plumbing_signals):
            logger.info("Plumbing intent detected after OOS — treating as in_scope")
            return None  # continue booking flow

        # 🟡 If still ambiguous → ASK a final confirmation instead of rejecting
        logger.info("OOS still uncertain after clarification — re-confirm plumbing intent")

        return (
            "Just to be sure — is this actually for any plumbing work like pipes, "
            "drainage, or installation, or is it something outside plumbing?"
        )
    if new_category == "delay_signal":
        return _build_delay_reply(answer, appointment)
    if new_category == "complaint":
        return _build_complaint_reply(answer, appointment)

    return None


# ── Response builders ─────────────────────────────────────────────────────────

def _build_oos_reply(message: str, appointment) -> str:
    """Warm redirect for services we don't offer."""
    plumber_number = (
        getattr(appointment, "plumber_contact_number", None) or PLUMBER_NUMBER_FALLBACK
    ).replace("+", "").replace("whatsapp:", "")

    # Try to identify what they asked about for a personalised reply
    msg_lower = (message or "").lower()

    service_map = {
        ("garage", "carport", "car port"): "garage work",
        ("paint", "painter", "painting"): "painting",
        ("electric", "electrician", "electrical"): "electrical work",
        ("roof", "roofing"): "roofing",
        ("solar",): "solar panel installation",
        ("borehole",): "borehole drilling",
        ("landscap", "garden"): "landscaping",
        ("carpent", "furniture"): "carpentry",
        ("pest",): "pest control",
        ("aircon", "air condition", "hvac"): "air conditioning",
    }

    asked_for = "that service"
    for keywords, label in service_map.items():
        if any(kw in msg_lower for kw in keywords):
            asked_for = label
            break

    return (
        f"We specialise in plumbing and bathroom/kitchen renovations, so "
        f"{asked_for} is outside what we do.\n\n"
        f"For that you'd need a specialist — we wouldn't want to steer you wrong.\n\n"
        f"If there's ever a plumbing job we can help with, just send us a message."
    )


_WEEKDAY_MAP = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}


def _extract_future_weekday(message: str):
    """
    Return (day_name, target_date, next_date) if a specific weekday is mentioned,
    otherwise None.  Always returns the *next* occurrence of that weekday.
    """
    msg_lower = (message or "").lower()
    tz = pytz.timezone('Africa/Johannesburg')
    today = datetime.now(tz).date()
    for day_name, day_num in _WEEKDAY_MAP.items():
        if day_name in msg_lower:
            days_ahead = day_num - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = today + timedelta(days=days_ahead)
            next_day = target + timedelta(days=1)
            return (day_name.capitalize(), target, next_day)
    return None


def _compute_followup_date(timeframe_message: str):
    """
    Parse a customer's timeframe text and return (iso_date, friendly_str).
    Falls back to DeepSeek for unusual phrasings, then defaults to 2 weeks.

    Fixed bugs vs previous version:
    - "in a month" now gives today+30 days (was first-of-next-month+7)
    - "next month"  now gives 15th of next month
    - "end of month" now handles being at the end of the month already
    - "end of next month" now matched explicitly
    - Ordinal day ("the 26th") now parsed before DeepSeek fallback
    - "next week" always produces a future date
    """
    import calendar as _cal
    from datetime import date as _date

    tz    = pytz.timezone('Africa/Johannesburg')
    today = datetime.now(tz).date()
    msg   = (timeframe_message or '').lower()

    def _nm(ref):
        """Return (year, month) for the calendar month after ref."""
        return (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)

    def _safe(year, month, day):
        """Clamp day to the last valid day of the month and return a date."""
        return _date(year, month, min(day, _cal.monthrange(year, month)[1]))

    # ── 0. Tomorrow / mangwana ────────────────────────────────────────────────
    if re.search(r'\btomorrow\b|\bmangwana\b', msg, re.IGNORECASE):
        target = today + timedelta(days=1)
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 1. Specific weekday ("on a Tuesday", "next Friday") ──────────────────
    weekday_info = _extract_future_weekday(timeframe_message)
    if weekday_info:
        _, target, _ = weekday_info
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 2. Ordinal day of month: "the 26th", "around the 26th", "by the 25th"
    m = re.search(r'\b(\d{1,2})\s*(?:st|nd|rd|th)\b', msg)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            last_this = _cal.monthrange(today.year, today.month)[1]
            if day <= last_this:
                candidate = today.replace(day=day)
                if candidate > today:
                    return candidate.isoformat(), candidate.strftime('%A %d %B')
            # Try next month
            ny, nm = _nm(today)
            last_next = _cal.monthrange(ny, nm)[1]
            if day <= last_next:
                target = _safe(ny, nm, day)
                return target.isoformat(), target.strftime('%A %d %B')

    # ── 3. "in X days" ───────────────────────────────────────────────────────
    m = re.search(r'in\s+(\d+)\s*day', msg)
    if m:
        target = today + timedelta(days=int(m.group(1)))
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 4. "in X weeks" / "X weeks" ─────────────────────────────────────────
    m = re.search(r'(\d+)\s*week', msg)
    if m:
        target = today + timedelta(weeks=int(m.group(1)))
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 5. "a week" / "in a week" / "one week" ──────────────────────────────
    if re.search(r'\ba week\b|in a week|one week', msg):
        target = today + timedelta(weeks=1)
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 6. "next week" → Wednesday of next calendar week ────────────────────
    if 'next week' in msg:
        days_to_next_monday = (7 - today.weekday()) % 7 or 7
        target = today + timedelta(days=days_to_next_monday + 2)
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 7. "in X months" (digit) ─────────────────────────────────────────────
    m = re.search(r'in\s+(\d+)\s*month', msg)
    if m:
        target = today + timedelta(days=30 * int(m.group(1)))
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 8. "in a month" / "a month" / "one month" → today + 30 days ─────────
    if re.search(r'\bin a month\b|\ba month\b|one month', msg):
        target = today + timedelta(days=30)
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 9. "end of next month" ───────────────────────────────────────────────
    if re.search(r'end.{0,12}next.{0,8}month|next month.{0,8}end', msg):
        ny, nm = _nm(today)
        last = _cal.monthrange(ny, nm)[1]
        target = _safe(ny, nm, last - 2)
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 10. "next month" → 15th of next month ───────────────────────────────
    if 'next month' in msg:
        ny, nm = _nm(today)
        target = _safe(ny, nm, 15)
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 11. "end of the month" / "end of month" / "this month" ──────────────
    if re.search(r'end of.{0,10}month|this month', msg):
        last = _cal.monthrange(today.year, today.month)[1]
        end_day = last - 2
        end_candidate = today.replace(day=end_day) if end_day >= 1 else today
        if end_candidate <= today:
            # Already at or past end of current month — use end of next month
            ny, nm = _nm(today)
            last_nm = _cal.monthrange(ny, nm)[1]
            target = _safe(ny, nm, last_nm - 2)
        else:
            target = end_candidate
        return target.isoformat(), target.strftime('%A %d %B')

    # ── 12. DeepSeek fallback ────────────────────────────────────────────────
    if _deepseek:
        try:
            response = _deepseek.chat.completions.create(
                model=settings.DEEPSEEK_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return ONLY a date in YYYY-MM-DD format. "
                            "No explanation, no other text."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Today is {today.isoformat()}. "
                            f"A customer said: '{timeframe_message}'. "
                            "Return one specific follow-up date within their stated timeframe."
                        ),
                    },
                ],
                temperature=0.1,
                max_tokens=15,
            )
            raw = response.choices[0].message.content.strip()[:10]
            parsed = _date.fromisoformat(raw)
            return parsed.isoformat(), parsed.strftime('%A %d %B')
        except Exception as exc:
            logger.warning("_compute_followup_date DeepSeek failed: %s", exc)

    # ── 13. No timeframe could be determined ─────────────────────────────────
    # Do NOT fabricate a date. The caller re-asks the customer for a rough
    # timeframe rather than silently scheduling an arbitrary follow-up.
    logger.info(
        "No timeframe parseable from '%s' — returning None",
        (timeframe_message or '')[:60],
    )
    return None, None


_TIMEFRAME_RE = re.compile(
    r'\d+\s*days?'                      # "10 days", "2 day"
    r'|\d+\s*weeks?'                    # "2 weeks"
    r'|\d+\s*months?'                   # "3 months"
    r'|next\s+week'                     # "next week"
    r'|next\s+month'                    # "next month"
    r'|end\s+of\s+(the\s+)?month'       # "end of the month"
    r'|in\s+a\s+week'                   # "in a week"
    r'|in\s+a\s+month'                  # "in a month"
    r'|in\s+two\s+weeks'                # "in two weeks"
    r'|fortnight'                       # "fortnight"
    r'|tomorrow'                        # "tomorrow", "call you tomorrow"
    r'|monday|tuesday|wednesday|thursday|friday|saturday|sunday'  # day names
    r'|mangwana'                        # Shona: tomorrow
    r'|svondo\s+rinouya'                # Shona: next week
    r'|mwedzi\s+unotevera',             # Shona: next month
    re.IGNORECASE,
)


def _message_has_timeframe(message: str) -> bool:
    return bool(_TIMEFRAME_RE.search(message))


def _message_has_timeframe_ai(message: str) -> bool:
    """
    Ask DeepSeek whether the message already contains a time reference the customer
    will be available (day name, date, relative week/month expression, etc.).
    Falls back to the regex if the API call fails.
    """
    from bot.services.clients import deepseek_call
    try:
        result = deepseek_call(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a yes/no classifier. "
                        "Reply with only the single word 'yes' or 'no'."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Does the following message mention a specific time or date "
                        "when the person will be available or back in touch? "
                        "(Examples that count: a day name like 'Thursday', a date, "
                        "'next week', 'end of month', 'tomorrow', 'in two weeks'.)\n\n"
                        f"Message: {message}"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=5,
        )
        return result.strip().lower().startswith('yes')
    except Exception as exc:
        logger.warning("_message_has_timeframe_ai failed (%s) — falling back to regex", exc)
        return _message_has_timeframe(message)


def _has_travel_negation(message: str) -> bool:
    """True when the customer explicitly denies being away (corrects a travel
    assumption), e.g. conv 427 'We are not out of town but we go to work'."""
    m = (message or '').lower()
    return any(p in m for p in (
        'not out of town', 'not away', 'not abroad', 'not travel', 'not going anywhere',
        'we are not out', "aren't out of town", 'not on holiday', 'not on vacation',
    ))


def _delay_subtype_keywords(message: str) -> str:
    """
    Keyword fallback for the delay sub-type (used when DeepSeek is unavailable).
    Returns one of: 'travelling' | 'access' | 'busy' | 'brush_off' | 'unknown'.
    """
    m = (message or '').lower()
    if any(p in m for p in (
        'other quote', 'another quote', 'more quotes', 'few quotes', 'get quotes',
        'getting quotes', 'quotation', 'compare', 'comparing', 'comparison',
        'shop around', 'other plumber', 'another plumber', 'second opinion',
        'source other', 'check around', 'see other',
    )):
        return 'comparison_shopping'
    if any(p in m for p in (
        'not interested', 'maybe later', 'some other time', 'will see',
        'think about it', 'just saving', 'saving your number', 'saved your number',
        'needed to save', 'window shopping', 'leave it',
    )):
        return 'brush_off'
    if not _has_travel_negation(message) and any(p in m for p in (
        'out of town', 'abroad', 'overseas', 'travelling', 'traveling', 'on a trip',
        'holiday', 'vacation', 'not in harare', 'not in zimbabwe', 'out of the country',
        'when i return', 'when i get back', 'back home',
    )):
        return 'travelling'
    if any(p in m for p in (
        'arrange access', 'tenant', 'landlord', 'keys', 'my wife', 'my husband',
        'my spouse', 'still building', 'not plastered', 'under construction',
        'speak to my', 'check with my', 'consult my',
    )):
        return 'access'
    if any(p in m for p in (
        'go to work', 'at work', 'working', 'during the day', 'i work', 'we work',
        'busy at work', 'shift', 'knock off', 'after work', 'tied up', 'quite busy',
        'busy now', 'busy at the moment',
    )):
        return 'busy'
    return 'unknown'


# Each reply funnels into the existing `delay_timeframe` step (an open timeframe
# question), so the downstream delay flow (steps 2-4) is unchanged. None of them
# assume the customer is travelling unless that is actually the sub-type.
_DELAY_SUBTYPE_REPLIES = {
    'busy': (
        "Totally understand — plenty of our clients are at work during the day, "
        "so we also do evenings and weekends. When would suit you best to get this sorted?"
    ),
    'access': (
        "No problem at all — just sort the access on your side and we'll work around you. "
        "When would be a good time for us to come through?"
    ),
    'travelling': "No problem at all. Roughly when do you think you'll be back?",
    'unknown':    "No problem at all. When would suit you to pick this up?",
}


def _classify_delay_subtype(message: str, appointment) -> str:
    """
    Split the single delay signal into a specific sub-type so we don't reply to a
    busy/at-work or access-arranging customer with a travel-assuming question.

    Returns: 'busy' | 'access' | 'travelling' | 'brush_off' | 'unknown'.
    DeepSeek handles nuance/negation; falls back to keywords when unavailable.
    """
    kw = _delay_subtype_keywords(message)
    valid = {'busy', 'access', 'travelling', 'brush_off', 'comparison_shopping', 'unknown'}

    sub = kw
    if _deepseek:
        try:
            from bot.services.clients import deepseek_call
            import json as _json
            raw = deepseek_call(
                messages=[
                    {"role": "system", "content": "Return ONLY valid JSON. No markdown."},
                    {"role": "user", "content": (
                        "A plumbing customer is deferring a booking. Which ONE sub-type best "
                        "fits their reason?\n"
                        "- busy: they have time but are working / tied up during the day\n"
                        "- access: they need to arrange access (tenant, keys, spouse, still building)\n"
                        "- travelling: they are genuinely away / out of town / abroad\n"
                        "- comparison_shopping: they want to get or compare other quotes, shop "
                        "around, or check other plumbers before deciding\n"
                        "- brush_off: a soft no / not really interested / just saved the number\n"
                        "- unknown: none of the above is clear\n\n"
                        "Do NOT pick 'travelling' if they say they are NOT out of town.\n\n"
                        f"Message: \"{message}\"\n\n"
                        '{"subtype": "busy|access|travelling|comparison_shopping|brush_off|unknown"}'
                    )},
                ],
                temperature=0.0,
                max_tokens=20,
                json_response=True,
            )
            parsed = (_json.loads(raw).get('subtype') or '').strip().lower()
            if parsed in valid:
                sub = parsed
        except Exception as exc:
            logger.warning("delay sub-type classify failed (%s) — using keywords", exc)
            sub = kw

    # Never assume travel when the customer explicitly negates it (conv 427).
    if sub == 'travelling' and _has_travel_negation(message):
        return kw if kw != 'travelling' else 'busy'
    return sub


def _build_delay_reply(message: str, appointment) -> str:
    """
    Step 1 of the delay follow-up flow.
    If the message already contains a timeframe, skip straight to step 2.
    Otherwise classify the delay sub-type and reply appropriately — never
    assuming travel for a busy/at-work or access-arranging customer (conv 427).
    Does NOT mark the lead as delayed yet — that happens at the end of the flow.
    """
    if _message_has_timeframe_ai(message):
        return _handle_delay_timeframe_answer(message, {}, appointment)

    subtype = _classify_delay_subtype(message, appointment)

    if subtype == 'brush_off':
        # Soft brush-off — instead of just letting the lead go, make one
        # value-add attempt: offer the portfolio (past projects + full pricing)
        # by email so they have something detailed to weigh while they decide or
        # when they come back. Park the lead so the scheduler stays silent
        # (P0 state-guard) if they ghost; parking does not block inbound replies.
        try:
            appointment.mark_parked(save=True)
        except Exception:
            pass
        # Already have their email → send the portfolio now, then ask for a rough
        # follow-up date so we check back in proactively.
        if getattr(appointment, 'customer_email', None):
            try:
                from bot.customer_emails import send_delay_quote_email_async
                send_delay_quote_email_async(appointment)
            except Exception:
                logger.exception("brush_off portfolio email failed — apt %s",
                                 getattr(appointment, 'pk', None))
            notes = appointment.internal_notes or ''
            if '[DELAY_QUOTE_SENT]' not in notes:
                appointment.internal_notes = f'{notes}\n[DELAY_QUOTE_SENT]'.strip()
                appointment.save(update_fields=['internal_notes'])
            _write_pending(appointment, 'delay_timeframe', '')
            return (
                "No worries at all — we'll leave it with you.\n\n"
                "I've just emailed you our portfolio of past projects with photos, "
                "plus a more detailed pricing guide, so you've got everything to look "
                "over.\n\n"
                "So we check back in at the right time — roughly when are you hoping "
                "to get this sorted? Even a rough idea like next week or end of the "
                "month is perfect."
            )
        # Otherwise ask for their email; the reply funnels into the existing
        # delay_email step, which captures it and sends the portfolio PDF.
        _write_pending(appointment, 'delay_email', '')
        return (
            "No worries at all — we'll leave it with you.\n\n"
            "Before you go — we've got a portfolio of past projects with photos, plus "
            "a more detailed pricing guide, that's worth a look while you decide. Want "
            "me to email it over? Just share your email and I'll send it across."
        )

    if subtype == 'comparison_shopping':
        # Price-shopping objection — don't fight it. Send the portfolio over
        # WhatsApp so they weigh us on quality (not just price), arm them to
        # compare like-for-like, then funnel into the timeframe step so we keep
        # a return date. (Hormozi: agree → reframe the comparison → low-risk ask.)
        photos_ok = False
        try:
            from bot.whatsapp_webhook import send_previous_work_photos
            clean_phone = (appointment.phone_number or '').replace('whatsapp:', '')
            photos_ok = send_previous_work_photos(clean_phone, appointment)
        except Exception:
            logger.exception("comparison_shopping portfolio send failed — apt %s",
                             getattr(appointment, 'pk', None))
        _write_pending(appointment, 'delay_timeframe', message)
        intro = (
            "Smart to compare. I'm sending through some of our past jobs now so "
            "you can weigh us on quality, not just price.\n\n"
            if photos_ok else
            "Smart to compare.\n\n"
        )
        return (
            f"{intro}"
            "One tip while you compare — check the others are all-in (parts + labour) "
            "and guarantee the work; that's usually where cheaper quotes catch people "
            "out. Ours is fixed before we start, nothing added later.\n\n"
            "When are you hoping to get it sorted by?"
        )

    if subtype == 'access':
        # They want the work but need to arrange access (no one home, tenant,
        # keys, spouse). Don't push a 14-day reactivation — propose a concrete
        # near-term check-in so we can lock in a slot once access is sorted.
        return _build_access_checkin_reply(message, appointment)

    _write_pending(appointment, 'delay_timeframe', message)
    return _DELAY_SUBTYPE_REPLIES.get(subtype, _DELAY_SUBTYPE_REPLIES['unknown'])


def _store_delay_followup_date(appointment, iso_date):
    """
    Persist the agreed follow-up date on the appointment: a [FOLLOW_UP_DATE] note
    tag plus an override of delay_followup_due_at so the reactivation cron fires
    on the customer's agreed date. A concrete date means the lead is owned by the
    reactivation queue, so any prior parked/brush-off suppression is lifted.
    """
    if not iso_date:
        return
    # A scheduled follow-up and "parked" are mutually exclusive — unpark so the
    # reactivation cron (which excludes parked leads) can fire on the date.
    try:
        appointment.unpark(save=True)
    except Exception:
        pass
    notes = appointment.internal_notes or ''
    tag   = f"[FOLLOW_UP_DATE] {iso_date}"
    if tag not in notes:
        appointment.internal_notes = f"{notes}\n{tag}".strip()
        appointment.save(update_fields=['internal_notes'])

    try:
        from datetime import date as _d
        _tz_sast = pytz.timezone('Africa/Johannesburg')
        agreed_date = _d.fromisoformat(iso_date)
        agreed_dt   = _tz_sast.localize(
            datetime(agreed_date.year, agreed_date.month, agreed_date.day, 9, 0)
        )
        appointment.delay_followup_due_at = agreed_dt
        appointment.save(update_fields=['delay_followup_due_at'])
        logger.info(
            "delay_followup_due_at updated to agreed date %s for appointment=%s",
            iso_date, getattr(appointment, 'id', None),
        )
    except Exception as _exc:
        logger.warning(
            "Could not parse agreed follow-up date '%s': %s", iso_date, _exc
        )


def _friendly_iso(iso_date):
    """ISO date → 'Friday 12 June', or None if unparseable/empty."""
    if not iso_date:
        return None
    try:
        from datetime import date as _d
        return _d.fromisoformat(iso_date).strftime('%A %d %B')
    except Exception:
        return None


# ── Near-term check-in scheduling (access / "no one home" deferrals) ───────────
_CHECKIN_TZ           = 'Africa/Johannesburg'
_CHECKIN_MIN_HOURS    = 12   # don't pester before they've had time to arrange
_CHECKIN_MAX_HOURS    = 23   # stay inside WhatsApp's 24h free-message window
_CHECKIN_PREFER_HOUR  = 18   # 6pm — natural evening check-in
_CHECKIN_CIVIL_HOURS  = range(8, 21)  # 8am–8pm: civil hours to contact a customer


def _friendly_checkin(dt, now) -> str:
    """A check-in datetime → 'this evening at 8' / 'tomorrow afternoon at 1'."""
    hour = dt.hour
    part = 'morning' if hour < 12 else ('afternoon' if hour < 17 else 'evening')
    h12  = hour % 12 or 12
    tstr = f"{h12}" if dt.minute == 0 else f"{h12}:{dt.minute:02d}"
    if dt.date() == now.date():
        when = f"this {part}"
    elif dt.date() == (now + timedelta(days=1)).date():
        when = f"tomorrow {part}"
    else:
        when = f"{dt.strftime('%A')} {part}"
    return f"{when} at {tstr}"


def _compute_access_checkin(now=None):
    """
    Pick a near-term check-in time for an access / "no one home" deferral.

    Strict window: at least _CHECKIN_MIN_HOURS after the conversation and no more
    than _CHECKIN_MAX_HOURS (keeps us inside WhatsApp's 24h free-message window).
    Within that window prefer 6pm; otherwise the civil-hour time closest to 6pm.
    Returns (checkin_dt_sast, friendly_str).
    """
    tz = pytz.timezone(_CHECKIN_TZ)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = tz.localize(now)

    earliest = now + timedelta(hours=_CHECKIN_MIN_HOURS)
    latest   = now + timedelta(hours=_CHECKIN_MAX_HOURS)

    best, best_score = None, None
    for day_offset in (0, 1):
        day = (now + timedelta(days=day_offset)).date()
        for hour in _CHECKIN_CIVIL_HOURS:
            cand = tz.localize(datetime(day.year, day.month, day.day, hour, 0))
            if earliest <= cand <= latest:
                score = abs(hour - _CHECKIN_PREFER_HOUR)
                if best is None or score < best_score or (score == best_score and cand < best):
                    best, best_score = cand, score

    if best is None:
        # No civil-hour slot fits (e.g. the window is entirely overnight).
        # Fall back to the earliest allowed time, nudged to the next civil hour.
        cand = earliest.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if cand.hour < 8:
            cand = cand.replace(hour=8)
        elif cand.hour > 20:
            cand = (cand + timedelta(days=1)).replace(hour=8)
        best = cand

    return best, _friendly_checkin(best, now)


def _build_access_checkin_reply(message: str, appointment) -> str:
    """
    Access / "no one home" deferral: instead of pushing a 14-day reactivation,
    propose a concrete near-term check-in (12–24h out, prefer evening) so we can
    lock in a time once the customer has sorted access on their side. Marks the
    lead delayed and schedules delay_followup_due_at to the check-in time.
    """
    checkin_dt, friendly = _compute_access_checkin()

    mark_delay_signal(appointment, message)

    notes     = appointment.internal_notes or ''
    additions = []
    if '[DELAY_KIND] access_checkin' not in notes:
        additions.append('[DELAY_KIND] access_checkin')
    tag = f"[FOLLOW_UP_DATE] {checkin_dt.date().isoformat()}"
    if tag not in notes:
        additions.append(tag)
    if additions:
        appointment.internal_notes = (notes + '\n' + '\n'.join(additions)).strip()
    # Overwrite the default 14-day reactivation date with the agreed check-in time.
    appointment.delay_followup_due_at = checkin_dt
    appointment.save(update_fields=['internal_notes', 'delay_followup_due_at'])

    _write_pending(appointment, 'delay_checkin', checkin_dt.isoformat())
    logger.info(
        "Access check-in scheduled for %s (appointment=%s)",
        checkin_dt.isoformat(), getattr(appointment, 'id', None),
    )
    return (
        "No problem at all. Is it okay if we check in with you about the access "
        f"arrangements {friendly}? That gives you time to sort things on your side "
        "and we'll lock in a slot to come through."
    )


def _handle_delay_checkin_answer(message: str, pending: dict, appointment) -> str:
    """
    Reply to the access check-in proposal ("is it okay if we check in {time}?").
    Yes → confirm the scheduled check-in. A different time → recompute via the
    standard timeframe flow. A flat no → leave the door open and stop the
    scheduled check-in so we don't pester them.
    """
    _clear_pending(appointment)

    iso_dt    = pending.get('original', '') or ''
    msg_lower = (message or '').strip().lower()

    no_signals = ('no', 'nope', "don't", 'do not', 'not necessary', 'no need',
                  'kwete', 'please don', 'leave it', "i'll message", 'i will message',
                  "i'll let you know", 'let you know', 'rather not')
    is_no = any(s in msg_lower for s in no_signals)

    _YES_TOKENS = {
        'yes', 'yep', 'yeah', 'yup', 'sure', 'ok', 'okay', 'okey', 'perfect',
        'fine', 'great', 'cool', 'that works', 'sounds good', 'hongu', 'ehe',
        'zvakanaka', 'no problem', 'works', 'please do',
    }
    is_affirmation = (
        msg_lower in _YES_TOKENS
        or (len(msg_lower.split()) <= 4
            and any(t in msg_lower.split() for t in
                    ('yes', 'ok', 'okay', 'sure', 'perfect', 'fine', 'great', 'cool')))
    )

    # A different time / day ("make it tomorrow", "after 7", "Friday") → recompute
    # through the standard timeframe handler so we honour their preference.
    _TF_WORDS = ('tomorrow', 'week', 'month', 'monday', 'tuesday', 'wednesday',
                 'thursday', 'friday', 'saturday', 'sunday', 'later', 'tonight',
                 "o'clock", 'around', 'pm', 'am')
    has_tf = any(w in msg_lower for w in _TF_WORDS)

    if is_no and not has_tf:
        appointment.clear_delayed(save=True)
        return (
            "No worries at all. Whenever you've sorted access on your side, just "
            "send us a message and we'll jump straight on it."
        )

    if has_tf and not is_affirmation:
        return _handle_delay_timeframe_answer(message, {}, appointment)

    # Affirmation — the check-in is already scheduled; confirm it warmly.
    friendly = ''
    try:
        dt = datetime.fromisoformat(iso_dt)
        friendly = ' ' + _friendly_checkin(dt, datetime.now(dt.tzinfo))
    except Exception:
        pass
    return (
        f"Perfect, we'll check in with you{friendly}. Take your time sorting "
        "access and we'll line everything up from there."
    )


# Number of times we re-ask for a timeframe in-session before leaving the door
# open (the within-window nudges keep gently asking after that).
_DELAY_TF_REASK_TAG = '[DELAY_TF_REASK]'
_DELAY_TF_MAX_REASKS = 2


def _read_delay_reask(appointment) -> int:
    m = re.search(r'\[DELAY_TF_REASK\] (\d+)', appointment.internal_notes or '')
    return int(m.group(1)) if m else 0


def _clear_delay_reask(appointment) -> None:
    notes = appointment.internal_notes or ''
    if _DELAY_TF_REASK_TAG in notes:
        appointment.internal_notes = re.sub(
            r'\n?\[DELAY_TF_REASK\] \d+', '', notes).strip()
        appointment.save(update_fields=['internal_notes'])


def _reask_delay_timeframe(message: str, appointment) -> str:
    """
    The customer deferred but gave no usable timeframe. Ask for a rough one
    instead of fabricating a date. After _DELAY_TF_MAX_REASKS attempts, leave the
    door open without scheduling anything (no arbitrary follow-up date).
    """
    count = _read_delay_reask(appointment)

    if count >= _DELAY_TF_MAX_REASKS:
        # Asked enough — don't loop or invent a date. Park the lead so it gets
        # gentle re-engagement rather than sitting in delay limbo with no date.
        _clear_pending(appointment)
        _clear_delay_reask(appointment)
        try:
            appointment.clear_delayed(save=True)
            appointment.mark_parked(save=True)
        except Exception:
            logger.exception("Failed to park lead after timeframe re-asks — apt %s",
                             getattr(appointment, 'pk', None))
        return (
            "No problem at all — I'll leave this with you. Whenever you have a "
            "rough idea of timing, just send us a message and we'll pick up right "
            "where we left off."
        )

    # Record the attempt, keep the timeframe step pending, and ask more concretely.
    notes = re.sub(r'\n?\[DELAY_TF_REASK\] \d+', '', appointment.internal_notes or '').strip()
    appointment.internal_notes = f"{notes}\n{_DELAY_TF_REASK_TAG} {count + 1}".strip()
    appointment.save(update_fields=['internal_notes'])
    _write_pending(appointment, 'delay_timeframe', message)
    logger.info("Delay timeframe re-ask #%s (no timeframe in '%s')",
                count + 1, message[:60])
    return (
        "No problem at all. Roughly when are you thinking of getting it sorted? "
        "Even a rough idea works — say next week, end of the month, or a specific "
        "day, and I'll set a reminder to check in with you then."
    )


def _handle_delay_timeframe_answer(message: str, pending: dict, appointment) -> str:
    """
    Step 2 (merged): customer gave their timeframe ("next week", "end of the
    month", etc.). Compute a specific follow-up date, presumptively commit to it
    (Hormozi presumptive close — no separate yes/no permission round-trip), and
    ask for the email in the same message. The reply funnels straight into the
    delay_email step, saving a full back-and-forth.
    """
    iso_date, friendly_date = _compute_followup_date(message)

    # No timeframe could be detected — ask the customer rather than inventing a
    # date. (Replaces the old silent 14-day default.)
    if not iso_date:
        return _reask_delay_timeframe(message, appointment)

    _clear_pending(appointment)
    _clear_delay_reask(appointment)
    logger.info("Delay timeframe parsed: '%s' → follow-up %s", message[:60], iso_date)

    # Presumptively commit: mark the lead delayed and store the agreed date now,
    # rather than gating on a separate confirmation step.
    mark_delay_signal(appointment, message)
    _store_delay_followup_date(appointment, iso_date)

    # Email already on file → confirm the date. Skip the quote email if we already
    # sent the portfolio earlier in this delay flow (avoids a duplicate send).
    if getattr(appointment, 'customer_email', None):
        if '[DELAY_QUOTE_SENT]' in (appointment.internal_notes or ''):
            return (
                f"Perfect — we'll check back in with you on {friendly_date}. "
                "Take your time with the portfolio; if anything changes just send "
                "us a message and we'll pick up right where we left off."
            )
        from bot.customer_emails import send_delay_quote_email_async
        send_delay_quote_email_async(appointment, follow_up_date_str=friendly_date)
        return (
            f"Got it, no problem. We'll check back on {friendly_date}.\n\n"
            "I've also sent a written quote and our portfolio — past projects plus "
            "a more detailed pricing guide — to your email. "
            "If anything changes just send us a message — we'll be right here."
        )

    # Ask for the email alongside the presumptive date confirmation.
    _write_pending(appointment, 'delay_email', iso_date or '')
    return (
        f"Got it, no problem. We'll check back on {friendly_date} — and I'll "
        "send a written quote and our portfolio over too, with a more detailed "
        "pricing guide and past projects, easier to save and share with whoever "
        "else needs to see it.\n\n"
        "What's the best email for that?"
    )


def _handle_delay_confirm_answer(message: str, pending: dict, appointment) -> str:
    """
    Step 3: customer replied yes/no to the follow-up permission question.
    Mark as delayed, store follow-up date, then ask for email (Step 4).
    """
    _clear_pending(appointment)

    original_info = pending.get('original', '')
    parts         = original_info.split('|')
    iso_date      = parts[-1].strip() if len(parts) > 1 else None

    msg_lower  = (message or '').strip().lower()
    no_signals = ('no', 'nope', "don't", 'not necessary', 'no need', 'kwete', 'please don')
    is_no      = any(s in msg_lower for s in no_signals)

    # Detect whether the customer is also providing a corrected timeframe.
    # "No, I'll be back end of next month" → date correction, not a flat refusal.
    _TIMEFRAME_WORDS = (
        'week', 'month', 'day', 'next', 'around', 'end of', 'beginning',
        'january', 'february', 'march', 'april', 'may', 'june', 'july',
        'august', 'september', 'october', 'november', 'december',
        'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
        'soon', 'later', 'after', 'back', 'return',
    )
    has_timeframe = any(word in msg_lower for word in _TIMEFRAME_WORDS)

    mark_delay_signal(appointment, message)

    if is_no and not has_timeframe:
        # Flat refusal — no alternative timeframe given
        return (
            "No worries at all. Whenever you're ready, just send us a message and "
            "we'll be happy to help."
        )

    # If the message contains a timeframe but is not a plain confirmation ("yes", "ok", etc.),
    # treat it as a step-2 timeframe answer and recompute the follow-up date.
    # This handles "Reach out in a month" arriving at step 3 instead of a confirmation.
    _YES_TOKENS = {
        'yes', 'yep', 'yeah', 'yup', 'sure', 'ok', 'okay', 'perfect', 'fine',
        'great', 'that works', 'sounds good', 'hongu', 'ehe', 'zvakanaka',
    }
    is_simple_yes = (
        msg_lower.strip() in _YES_TOKENS
        or (len(msg_lower.split()) <= 3
            and any(tok in msg_lower.split() for tok in ('yes', 'ok', 'okay', 'sure', 'perfect')))
    )
    if has_timeframe and not is_simple_yes:
        logger.info(
            "Delay confirm: timeframe without clear confirmation — re-running step 2: '%s'",
            message[:80],
        )
        return _handle_delay_timeframe_answer(message, {}, appointment)

    # Store follow-up date — in notes AND in delay_followup_due_at so the cron fires correctly
    _store_delay_followup_date(appointment, iso_date)

    # If email already captured, skip Step 4
    if getattr(appointment, 'customer_email', None):
        from bot.customer_emails import send_delay_quote_email_async
        friendly = _friendly_iso(iso_date)
        send_delay_quote_email_async(appointment, follow_up_date_str=friendly)
        return (
            "Perfect, we'll do that. "
            "We've also sent a quote to your email. "
            "If anything changes just send us a message — we'll be right here."
        )

    # Step 4 — ask for email with quote framing
    _write_pending(appointment, 'delay_email', iso_date or '')
    return (
        "Perfect.\n\n"
        "We'll also send you a proper written quote and our portfolio — with a "
        "more detailed pricing guide and past projects — easier to save and share "
        "with whoever else needs to see it. "
        "What's the best email to reach you on?"
    )


def _handle_delay_email_answer(message: str, pending: dict, appointment) -> str:
    """
    Step 4: customer provided (or declined) their email after the delay flow.
    Save email, send quote email, return closing message.
    """
    _clear_pending(appointment)

    iso_date  = pending.get('original', '') or None
    msg       = (message or '').strip()
    msg_lower = msg.lower()

    # First: did they ask to receive it HERE on WhatsApp rather than by email?
    # That's a positive "send it to me on this number" — honour it by sending the
    # lead magnet PDF directly, instead of treating it as a flat decline.
    wa_delivery_signals = ('whatsapp', 'this number', 'this platform', 'this chat',
                           'this app', 'use this', 'on here', 'over here',
                           'send it here', 'send it', 'send them', 'send through',
                           'just send', 'right here', 'send here')
    wants_whatsapp = ('@' not in msg) and any(s in msg_lower for s in wa_delivery_signals)

    if wants_whatsapp:
        send_lead_magnet_on_whatsapp(appointment)
        # Keep the lead in the reactivation queue so we still check back on the
        # agreed date. Restore the delay signal (cleared before the OOS handler ran).
        notes = appointment.internal_notes or ''
        if _DELAY_SIGNAL_TAG not in notes:
            notes = f'{notes}\n{_DELAY_SIGNAL_TAG}'.strip()
        appointment.internal_notes = notes
        appointment.is_delayed = True
        appointment.save(update_fields=['internal_notes', 'is_delayed'])
        if iso_date:
            _store_delay_followup_date(appointment, iso_date)
        # No email captured but a follow-up is needed → hand it to the plumber now;
        # the cron alerts again on the agreed date (no-email branch).
        _alert_plumber_no_email(appointment, iso_date)
        if iso_date:
            return (
                "Done — I've sent our portfolio and pricing guide straight to you "
                f"here on WhatsApp. We'll also check back in around {_friendly_iso(iso_date)}. "
                "If anything changes just send us a message."
            )
        _write_pending(appointment, 'delay_timeframe', '')
        return (
            "Done — I've sent our portfolio and pricing guide straight to you here "
            "on WhatsApp.\n\nSo we check back at the right time — roughly when are "
            "you hoping to get this sorted? Even a rough idea like next week or end "
            "of the month is perfect."
        )

    # Detect skip / refusal — a flat decline of email (not a request to receive
    # it on WhatsApp, handled above).
    skip_signals = ('skip', 'no', 'nope', 'nah', 'dont have', "don't have",
                    'prefer not', 'rather not', 'na',
                    'message me', 'text me')
    is_skip = any(s in msg_lower for s in skip_signals) and '@' not in msg

    if is_skip:
        # They'd rather not share an email. Respect it. If they agreed a follow-up
        # date, keep the lead in the reactivation queue so the plumber is alerted
        # on that date; otherwise park for gentle re-engagement.
        if iso_date:
            notes = appointment.internal_notes or ''
            if _DELAY_SIGNAL_TAG not in notes:
                notes = f'{notes}\n{_DELAY_SIGNAL_TAG}'.strip()
            appointment.internal_notes = notes
            appointment.is_delayed = True
            appointment.save(update_fields=['internal_notes', 'is_delayed'])
            _store_delay_followup_date(appointment, iso_date)
        else:
            try:
                appointment.clear_delayed(save=False)
                appointment.mark_parked(save=True)
            except Exception:
                logger.exception("Failed to park lead after email skip — apt %s",
                                 getattr(appointment, 'pk', None))
        # Requires a follow-up but no email → alert the plumber to chase on WhatsApp.
        _alert_plumber_no_email(appointment, iso_date)
        return (
            "No problem at all. Whenever you're ready, just send us a message."
        )

    # Try to extract a valid email address
    import re as _re
    m = _re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', msg)
    if not m:
        _write_pending(appointment, 'delay_email', iso_date or '')
        return (
            "That doesn't look quite right — could you double-check the email? "
            "Or just say 'skip' if you'd prefer not to share."
        )

    email = m.group(0).lower()
    appointment.customer_email = email
    appointment.save(update_fields=['customer_email'])
    logger.info("Delay email captured: %s for appointment=%s", email,
                getattr(appointment, 'id', None))

    # Send quote email
    friendly = None
    if iso_date:
        try:
            from datetime import date as _d
            friendly = _d.fromisoformat(iso_date).strftime('%A %d %B')
        except Exception:
            pass

    from bot.customer_emails import send_delay_quote_email_async
    send_delay_quote_email_async(appointment, follow_up_date_str=friendly)

    # Restore delay signal — cleared by the webhook before the OOS handler runs.
    # Re-writing the tag blocks regular follow-ups; restoring is_delayed=True
    # ensures _process_delayed_reactivations fires on the agreed date.
    # [DELAY_QUOTE_SENT] guards against re-sending the same portfolio email if the
    # customer then gives a timeframe (the timeframe step checks for this tag).
    notes = appointment.internal_notes or ''
    if _DELAY_SIGNAL_TAG not in notes:
        notes = f'{notes}\n{_DELAY_SIGNAL_TAG}'.strip()
    if '[DELAY_QUOTE_SENT]' not in notes:
        notes = f'{notes}\n[DELAY_QUOTE_SENT]'.strip()
    appointment.internal_notes = notes
    appointment.is_delayed = True
    appointment.save(update_fields=['internal_notes', 'is_delayed'])

    if iso_date:
        return (
            "Got it! I'll have that sent across to you shortly.\n\n"
            "We'll also check back in with you on the agreed date. "
            "Speak soon!"
        )
    # No agreed date yet (e.g. soft brush-off led with the portfolio offer) — ask
    # for a rough follow-up date so we check back proactively instead of waiting.
    _write_pending(appointment, 'delay_timeframe', '')
    return (
        "Got it! I'll send our portfolio of past projects — plus a more detailed "
        "pricing guide — across to you shortly.\n\n"
        "So we check back in at the right time — roughly when are you hoping to "
        "get this sorted? Even a rough idea like next week or end of the month is "
        "perfect."
    )


def _build_complaint_reply(message: str, appointment) -> str:
    """
    Empathetic response to frustration, price complaints, or legitimacy questions.
    Acknowledges the concern, provides reassurance, and redirects to the plumber
    for anything that requires a human conversation.
    """
    plumber_number = (
        getattr(appointment, "plumber_contact_number", None) or PLUMBER_NUMBER_FALLBACK
    ).replace("+", "").replace("whatsapp:", "")

    msg_lower = (message or "").lower()

    # Price-specific complaint
    if any(w in msg_lower for w in (
        "ridiculous", "expensive", "too much", "overpriced", "rip off",
        "rip-off", "never seen", "such prices", "inodhura", "pricey",
    )):
        return (
            "Thanks for your message, and that's a totally fair point to raise. "
            "The prices in our earlier message were general guides — "
            "every job is different and the actual cost depends heavily on your "
            "specific setup, the fixtures you choose, and the scope of work.\n\n"
            "Labour on its own can start from as little as US$20 for a simple fitting. "
            "The best way to get a fair, fixed price is a free on-site visit "
            "where the plumber sees the space and gives you a number on the spot "
            "— no surprises.\n\n"
            f"If you'd like to speak directly with the plumber about the costs, "
            f"you can reach Tinashe on +{plumber_number}."
        )

    # Legitimacy / "are you real" complaint
    if any(w in msg_lower for w in (
        "real plumber", "are you a plumber", "are you real", "not a plumber",
        "fake", "scam", "legitimate", "trust",
    )):
        return (
            "That's a completely fair question — and yes, we're a real plumbing "
            "company based in Harare.\n\n"
            "I'm the booking assistant handling initial enquiries. For anything "
            "technical or to speak directly with the team, you can reach Takudzwa on "
            f"+{plumber_number}.\n\n"
            "He'll be able to answer any questions you have about the work."
        )

    # Generic frustration / complaint
    return (
        "Thanks for flagging that — I hear you, and I appreciate you being upfront.\n\n"
        "I'm the booking assistant, so if anything I've said doesn't seem right or "
        "you'd like to speak with the plumber directly, that's the best next step.\n\n"
        f"You can reach Tinashe directly on +{plumber_number} — "
        "he'll sort it out properly."
    )


# ── Main public function ──────────────────────────────────────────────────────

def handle_out_of_scope(
    message: str,
    appointment,
    precomputed: dict | None = None,
) -> Optional[str]:
    """
    Check whether this message falls outside the normal booking scope.

    precomputed: optional dict from uc_as_oos_classification(). When provided,
                 skips the internal classify_message() API call entirely.

    Decision tree:
      1. If a clarifying question is pending, resolve it first.
      2. Classify (uses precomputed if available, otherwise calls DeepSeek).
      3. HIGH confidence + non-in_scope → act immediately.
      4. LOW confidence + non-in_scope → ask clarifying question.
      5. in_scope → return None.
    """
    # ── Step 1: pending states (no API call — reads from internal_notes) ─────
    pending = _read_pending(appointment)
    if pending:
        pending_cat = pending.get("category", "")

        if pending_cat == "delay_timeframe":
            logger.info("Delay flow step 2 — timeframe answer: '%s'", message[:60])
            return _handle_delay_timeframe_answer(message, pending, appointment)

        if pending_cat == "delay_confirm":
            logger.info("Delay flow step 3 — confirm answer: '%s'", message[:60])
            return _handle_delay_confirm_answer(message, pending, appointment)

        if pending_cat == "delay_checkin":
            logger.info("Delay flow — access check-in answer: '%s'", message[:60])
            return _handle_delay_checkin_answer(message, pending, appointment)

        if pending_cat == "delay_email":
            logger.info("Delay flow step 4 — email answer: '%s'", message[:60])
            return _handle_delay_email_answer(message, pending, appointment)

        logger.info(
            "Resolving pending clarification: category=%s original='%s' answer='%s'",
            pending_cat, pending.get("original", "")[:60], message[:60],
        )
        return _resolve_pending_clarification(message, pending, appointment)

    # ── Step 2: classify (use precomputed to skip the API call) ───────────────
    if precomputed:
        classification = precomputed
        logger.debug("OOS: using precomputed classification — %s", precomputed)
    else:
        classification = classify_message(message, appointment)

    category   = classification["category"]
    confidence = classification["confidence"]
    detail     = classification.get("detail", "")

    # ── Step 3: in scope — do nothing ─────────────────────────────────────────
    if category == "in_scope":
        return None

    # ── Step 4: delay signal at any confidence → start two-step follow-up flow ─
    if category == "delay_signal":
        logger.info("Delay signal (%s confidence): '%s'", confidence, message[:80])
        if _message_has_timeframe(message):
            logger.info("Timeframe already in message — skipping Step 1")
            return _handle_delay_timeframe_answer(message, {}, appointment)
        return _build_delay_reply(message, appointment)

    # ── Step 5: HIGH confidence — act immediately ─────────────────────────────
    if confidence == "HIGH":

        if category == "out_of_scope":
            logger.info("OOS detected — forcing plumbing clarification step first")
            clarifying_q = _generate_plumbing_reframe_question(message)
            _write_pending(appointment, "out_of_scope", message)
            return clarifying_q

        if category == "complaint":
            logger.info("HIGH complaint: '%s'", message[:80])
            return _build_complaint_reply(message, appointment)

    # ── Step 6: LOW confidence — ask a targeted clarifying question ───────────
    # delay_signal is handled above at any confidence level.
    # For out_of_scope: ask a clarifying question.
    # For complaint: pass through (plumber sees it in logs).

    if category == "complaint":
        logger.info(
            "LOW complaint suppressed — passing to booking flow: '%s'", message[:80]
        )
        return None

    # For low-confidence out_of_scope: ask one clarifying question
    logger.info(
        "LOW confidence %s — generating clarifying question for: '%s'",
        category, message[:80],
    )
    clarifying_q = _generate_clarifying_question(message, category, detail, appointment)

    # Persist the pending state so the next turn knows what we asked about
    _write_pending(appointment, category, message)

    return clarifying_q
