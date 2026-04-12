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
from typing import Optional
from urllib.parse import quote, unquote

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

# ── Services we explicitly DO offer (used for context in the classifier) ──────
OUR_SERVICES = (
    "bathroom renovation, kitchen renovation, new plumbing installation, "
    "toilet supply and fitting, geyser installation, shower cubicle, vanity unit, "
    "bathtub installation, pipe repair, drain unblocking"
)

# ── Keywords that are a near-certain delay signal (fast pre-filter) ───────────
_DELAY_PHRASES = (
    "call me later", "call you later", "i'll call you", "i will call",
    "will contact you", "i'll contact", "will reach out", "i'll reach out",
    "busy now", "busy at the moment", "not right now", "not ready",
    "come back to you", "i'll be in touch", "will be in touch",
    "when i'm back", "when i am back", "when i get back", "back home",
    "in a few weeks", "in a few months", "10 days", "few days time",
    "needed to save your number", "save your number", "saved your number",
    "i'm abroad", "i am abroad", "i'm away", "i am away", "out of town",
    "travelling", "traveling", "not in harare", "not in zimbabwe",
    "ndichatumira", "ndichauya", "mangwana", "ndichaenda",
)

# ── Keywords that suggest a completely out-of-scope service ───────────────────
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


# ── Fast pre-filters (no API call needed) ────────────────────────────────────

def _fast_delay_check(message: str) -> bool:
    msg = (message or "").lower()
    return any(phrase in msg for phrase in _DELAY_PHRASES)



# ── DeepSeek classifier ───────────────────────────────────────────────────────

def classify_message(message: str, appointment) -> dict:
    """
    Classify an incoming customer message into one of:
      in_scope        — normal booking / service inquiry; do nothing
      out_of_scope    — service we don't offer
      delay_signal    — customer is not ready yet
      complaint       — frustration, price objection, skepticism

    Returns:
        {
            "category":   "in_scope" | "out_of_scope" | "delay_signal" | "complaint",
            "confidence": "HIGH" | "LOW",
            "detail":     short string explaining the classification
        }
    """

    # -- Fast-path checks (no API call) --------------------------------------
    if _fast_delay_check(message):
        logger.info("Fast delay signal detected: '%s'", message[:60])
        return {"category": "delay_signal", "confidence": "HIGH", "detail": "delay phrase matched"}

    def _fast_oos_check(message: str) -> bool:
        msg = (message or "").lower()
        return any(k in msg for k in _OOS_KEYWORDS)
        
    # -- Skip DeepSeek for very short neutral messages -----------------------
    msg_lower = (message or "").strip().lower()
    trivial_acks = {
        "ok", "okay", "k", "kk", "yes", "no", "sure", "thanks",
        "thank you", "noted", "cool", "sharp", "👍", "🙏",
        "hongu", "kwete", "zvakanaka",
    }
    if msg_lower in trivial_acks or len(msg_lower.split()) <= 2:
        return {"category": "in_scope", "confidence": "HIGH", "detail": "trivial ack"}

    if not _deepseek:
        return {"category": "in_scope", "confidence": "LOW", "detail": "no DeepSeek key"}

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

    prompt = f"""You are a message classifier for Plumbot, the WhatsApp chatbot for Homebase Plumbers in Zimbabwe/South Africa.

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
        response = _deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": "Return ONLY valid JSON. No markdown, no explanation.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=80,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        import json
        result = json.loads(raw)

        category = result.get("category", "in_scope")
        confidence = (result.get("confidence") or "LOW").upper()
        detail = result.get("detail", "")

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
        logger.warning("OOS classifier failed: %s", exc)
        return {"category": "in_scope", "confidence": "LOW", "detail": "classifier error"}


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
            model="deepseek-chat",
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
        f"{asked_for} is unfortunately outside what we do. 🔧\n\n"
        f"For that you'd need a specialist — we wouldn't want to steer you wrong.\n\n"
        f"If there's ever a plumbing job we can help with, just send us a message!"
    )


def _build_delay_reply(message: str, appointment) -> str:
    """Graceful acknowledgment when the customer is not ready."""
    # Check for a specific timeframe so we can acknowledge it
    import re
    msg_lower = (message or "").lower()

    timeframe = None
    patterns = [
        r"(\d+)\s*day",
        r"(\d+)\s*week",
        r"(\d+)\s*month",
    ]
    for pat in patterns:
        m = re.search(pat, msg_lower)
        if m:
            num = m.group(1)
            unit = "day" if "day" in pat else "week" if "week" in pat else "month"
            timeframe = f"in {num} {unit}{'s' if int(num) != 1 else ''}"
            break

    if timeframe:
        return (
            f"No problem at all — we'll be here {timeframe}. 😊\n\n"
            f"Whenever you're ready, just send us a message and we'll pick up right where we left off."
        )

    # Generic delay ack
    return (
        "No problem at all! Whenever you're ready, just send us a message and "
        "we'll pick up right where we left off. 😊"
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
            "company based in Harare. 😊\n\n"
            "I'm the booking assistant handling initial enquiries, which is why "
            "my answers are fairly structured. For anything technical or to speak "
            f"directly with our senior plumber, Tinashe, you can reach him on "
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

def handle_out_of_scope(message: str, appointment) -> Optional[str]:
    """
    Check whether this message falls outside the normal booking scope.

    Decision tree:
      1. If a clarifying question is pending from the previous turn,
         resolve it first — re-classify the answer and act on the result.
      2. Classify the incoming message (fast-path keywords → DeepSeek).
      3. HIGH confidence + non-in_scope → act immediately.
      4. LOW confidence + non-in_scope → generate a targeted clarifying
         question, store pending state, return the question.
         On the NEXT turn, step 1 picks it up.
      5. in_scope → return None (caller continues its normal flow).

    Returns:
        str  — a reply to send to the customer (caller should send this and stop)
        None — message is in scope; caller should continue its normal booking flow

    Never asks more than one clarifying question per ambiguous message —
    if the answer is still LOW confidence, the module passes through to avoid loops.
    """
    # ── Step 1: resolve a pending clarification from the previous turn ────────
    pending = _read_pending(appointment)
    if pending:
        logger.info(
            "Resolving pending clarification: category=%s original='%s' answer='%s'",
            pending.get("category"), pending.get("original", "")[:60], message[:60],
        )
        return _resolve_pending_clarification(message, pending, appointment)

    # ── Step 2: classify the current message ──────────────────────────────────
    classification = classify_message(message, appointment)
    category = classification["category"]
    confidence = classification["confidence"]
    detail = classification.get("detail", "")

    # ── Step 3: in scope — do nothing ─────────────────────────────────────────
    if category == "in_scope":
        return None

    # ── Step 4: HIGH confidence — act immediately ─────────────────────────────
    # ── Step 4: HIGH confidence — act immediately ─────────────────────────────
    if confidence == "HIGH":

        if category == "out_of_scope":
            logger.info("OOS detected — forcing plumbing clarification step first")

            clarifying_q = _generate_plumbing_reframe_question(message)
            _write_pending(appointment, "out_of_scope", message)

            return clarifying_q

        if category == "delay_signal":
            logger.info("HIGH delay: '%s'", message[:80])
            return _build_delay_reply(message, appointment)

        if category == "complaint":
            logger.info("HIGH complaint: '%s'", message[:80])
            return _build_complaint_reply(message, appointment)

    # ── Step 5: LOW confidence — ask a targeted clarifying question ───────────
    # Exception: if the classifier is unsure about an in_scope vs delay/complaint
    # reading we prefer to keep the booking flow alive rather than interrupting it
    # with a clarifying question.  We only clarify when the ambiguity is between
    # in_scope and out_of_scope, or when the message is genuinely confusing.

    # Do NOT clarify low-confidence complaints — pass through to the booking flow
    # so the bot can continue collecting details; the plumber sees it in the logs.
    if category == "complaint":
        logger.info(
            "LOW complaint suppressed — passing to booking flow: '%s'", message[:80]
        )
        return None

    # For low-confidence out_of_scope or delay_signal: ask one clarifying question
    logger.info(
        "LOW confidence %s — generating clarifying question for: '%s'",
        category, message[:80],
    )
    clarifying_q = _generate_clarifying_question(message, category, detail, appointment)

    # Persist the pending state so the next turn knows what we asked about
    _write_pending(appointment, category, message)

    return clarifying_q