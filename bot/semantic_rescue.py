"""
bot/semantic_rescue.py
======================
Semantic rescue layer for messages that normal extraction could not classify.

Called when extract_all_available_info_with_ai() returns no useful fields and
the bot would otherwise fall through to a generic "Sorry, I didn't catch that."

Public API
----------
  rescue(message, next_question, conversation_context) -> dict
      Returns:
        input_type       — product_mention | reference_link | price_query |
                           partial_answer | general_question | unclear
        plumbing_mapping — best-guess fixture/product string, or None
        service_type     — normalised service_type value if identifiable, or None
        suggested_reply  — context-aware WhatsApp reply string, or None
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# ── Product keyword → service_type map ────────────────────────────────────────
# Ordered most-specific first.
_PRODUCT_SERVICE_MAP: list[tuple[str, str, str]] = [
    # (keyword, service_type, display_label)
    ("freestanding tub",         "bathroom_renovation", "Freestanding tub"),
    ("free standing tub",        "bathroom_renovation", "Freestanding tub"),
    ("free-standing tub",        "bathroom_renovation", "Freestanding tub"),
    ("standalone tub",           "bathroom_renovation", "Standalone tub"),
    ("freestanding bath",        "bathroom_renovation", "Freestanding bath"),
    ("free standing bath",       "bathroom_renovation", "Freestanding bath"),
    ("rain shower",              "bathroom_renovation", "Rain shower"),
    ("rainfall shower",          "bathroom_renovation", "Rainfall shower"),
    ("double vanity",            "bathroom_renovation", "Double vanity"),
    ("vanity unit",              "bathroom_renovation", "Vanity unit"),
    ("shower cubicle",           "bathroom_renovation", "Shower cubicle"),
    ("shower enclosure",         "bathroom_renovation", "Shower enclosure"),
    ("shower tray",              "bathroom_renovation", "Shower tray"),
    ("toilet suite",             "bathroom_renovation", "Toilet suite"),
    ("cistern",                  "bathroom_renovation", "Toilet cistern"),
    ("freestanding",             "bathroom_renovation", "Freestanding fixture"),
    ("free standing",            "bathroom_renovation", "Freestanding fixture"),
    ("free-standing",            "bathroom_renovation", "Freestanding fixture"),
    ("kitchen sink",             "kitchen_renovation",  "Kitchen sink"),
    ("kitchen tap",              "kitchen_renovation",  "Kitchen tap"),
    ("kitchen faucet",           "kitchen_renovation",  "Kitchen faucet"),
    ("dishwasher",               "kitchen_renovation",  "Dishwasher"),
]

_URL_RE = re.compile(
    r'https?://\S+|www\.\S+|drive\.google|dropbox\.com|photos\.app|wa\.me|bit\.ly',
    re.IGNORECASE,
)

_PRICE_WORDS = (
    "how much", "price", "cost", "rate", "charge", "quote",
    "mutengo", "mari", "dollar",  # Shona
)


def _is_url(message: str) -> bool:
    return bool(_URL_RE.search(message))


def _keyword_rescue(message: str) -> dict | None:
    """
    Fast keyword-based rescue — no API call.
    Returns a result dict or None if no match.
    """
    msg_lower = message.lower().strip()

    # URL / reference link
    if _is_url(message):
        return {
            "input_type": "reference_link",
            "plumbing_mapping": None,
            "service_type": None,
            "suggested_reply": (
                "Thanks for sharing that — I'll make sure the team sees it.\n\n"
                "To keep things moving, which room are we focusing on — "
                "bathroom, kitchen, or a new installation?"
            ),
        }

    # Product mention
    for keyword, svc, label in _PRODUCT_SERVICE_MAP:
        if keyword in msg_lower:
            room = "bathroom" if "bathroom" in svc else "kitchen" if "kitchen" in svc else "space"
            return {
                "input_type": "product_mention",
                "plumbing_mapping": keyword,
                "service_type": svc,
                "suggested_reply": (
                    f"{label} — great choice! Is this for a full {room} renovation "
                    f"or are you just looking at pricing for the {label.lower()} itself?"
                ),
            }

    # Price query without a service
    if any(w in msg_lower for w in _PRICE_WORDS):
        return {
            "input_type": "price_query",
            "plumbing_mapping": None,
            "service_type": None,
            "suggested_reply": (
                "Happy to give you a sense of pricing — it depends on the job. "
                "Which service are you looking at: bathroom renovation, kitchen plumbing, "
                "or a new installation?"
            ),
        }

    return None


def _deepseek_rescue(
    message: str,
    next_question: str,
    conversation_context: str,
) -> dict | None:
    """DeepSeek-powered rescue for inputs that keyword matching couldn't handle."""
    try:
        from bot.services.clients import deepseek_call
    except ImportError:
        return None

    prompt = f"""A plumbing booking chatbot received a message it could not classify.

Next question the bot was trying to ask: {next_question or "unknown"}
Recent conversation:
{conversation_context or "(start of conversation)"}

Customer message: "{message}"

Classify this message and return ONLY valid JSON:
{{
  "input_type": "product_mention|reference_link|price_query|partial_answer|general_question|unclear",
  "plumbing_mapping": "specific fixture or product they likely mean, or null",
  "service_type": "bathroom_renovation|bathroom_installation|kitchen_renovation|kitchen_installation|new_plumbing_installation|null",
  "suggested_reply": "1-2 sentence WhatsApp reply acknowledging what they sent and moving forward"
}}

input_type rules:
- product_mention: they named a specific fixture/product (freestanding tub, rain shower, vanity, etc.)
- reference_link: they shared a URL or link
- price_query: asking about cost without naming a service
- partial_answer: a fragment that partially answers the bot's current question
- general_question: off-flow but plumbing-related question
- unclear: genuinely unintelligible

For suggested_reply:
- product_mention → confirm the product, ask if full renovation or just that item
- reference_link  → acknowledge briefly, redirect to booking question
- price_query     → note pricing depends on job, ask which service
- partial_answer  → confirm interpretation, ask for confirmation
- Keep it warm, brief, Zimbabwean English ("sorted", "sharp", "keen")
- No markdown, no bullet points"""

    try:
        raw = deepseek_call(
            messages=[
                {"role": "system", "content": "Return ONLY valid JSON. No markdown, no explanation."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=200,
            json_response=True,
        )
        return json.loads(raw)
    except Exception as exc:
        logger.warning("semantic_rescue DeepSeek call failed: %s", exc)
        return None


def rescue(
    message: str,
    next_question: str = "",
    conversation_context: str = "",
) -> dict:
    """
    Attempt to semantically rescue an unclassified customer message.

    Tries keyword matching first (free), then DeepSeek (paid) if needed.
    Never raises — returns an 'unclear' result on all failures.
    """
    _empty = {
        "input_type": "unclear",
        "plumbing_mapping": None,
        "service_type": None,
        "suggested_reply": None,
    }

    if not message or not message.strip():
        return _empty

    # 1. Fast keyword rescue (no API call)
    result = _keyword_rescue(message)
    if result:
        logger.info(
            "semantic_rescue: keyword hit input_type=%s mapping=%s",
            result["input_type"], result.get("plumbing_mapping"),
        )
        return result

    # 2. DeepSeek rescue for edge cases
    result = _deepseek_rescue(message, next_question, conversation_context)
    if result and result.get("input_type") not in ("unclear", None):
        logger.info(
            "semantic_rescue: DeepSeek input_type=%s mapping=%s",
            result.get("input_type"), result.get("plumbing_mapping"),
        )
        # Ensure all required keys exist
        result.setdefault("plumbing_mapping", None)
        result.setdefault("service_type", None)
        result.setdefault("suggested_reply", None)
        return result

    return _empty
