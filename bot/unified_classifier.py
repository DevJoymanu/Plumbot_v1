"""
bot/unified_classifier.py
=========================
Single DeepSeek call that replaces the following separate calls per message:
  1. out_of_scope_handler.classify_message      (OOS intent)
  2. views.detect_service_inquiry               (product intent)
  3. views.extract_all_available_info_with_ai   (booking data)
  4. is_previous_work_photo_request             (photo flag)
  5. repeated_question pre-classifier           (is this a repeat?)
  6. handle_plan_later_response pre-check       (plan-later flag)

Returns a single dict that all downstream functions consume.
Falls back gracefully to None — callers must handle None by running their
own individual classifiers as before.
"""

import json
import logging
import os

from django.conf import settings
from openai import OpenAI

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if key:
            _client = OpenAI(api_key=key, base_url="https://api.deepseek.com/v1")
    return _client


_SYSTEM = """\
You are a message classifier for HomeBase Plumbers (Zimbabwe).
Customers write in English, Shona, or a mix. TODAY = {today}.
Return ONLY valid JSON — no markdown, no explanation.

─── INTENT (pick one) ────────────────────────────────────────────────────────
in_scope      Normal plumbing inquiry, product question, booking info, or
              any message that should continue the booking conversation.
              Shona examples: "Ndoda kubhukisha" (I want to book),
              "Ndoda kushandura chimbuzi" (I want to change the toilet),
              "Mutengo weshower chii?" (What's the shower price?),
              "Mune tub here?" (Do you have tubs?),
              "Ndichatumira plan mangwana" (I'll send the plan tomorrow).
out_of_scope  Service we do NOT offer: painting, electrical, roofing,
              solar, borehole, gardening, carpentry, pest control, etc.
delay_signal  Customer is deferring: "call me later", "not ready yet",
              "will come back", "I'm busy", "abroad", "still building".
              Shona examples: "Ndiri kunze kwenyika" (I'm out of the country),
              "Ndichadzokezai" (I'll call you back), "Mbichana" (just a bit later),
              "Ndichaita contact" (I'll make contact), "Tichataura" (we'll talk),
              "Ndisati ndagadzirira" (I'm not ready yet),
              "Ndicharidza rini ndichadzoka" (I'll call when I return).
complaint     Frustration, price objection, or legitimacy question.
              Shona examples: "Mutengo unodhura zvakanyanya" (price is way too expensive),
              "Musatikwashura" (don't cheat us), "Hamusi vaplumber chaiwo here?" (Are you real plumbers?),
              "Munoitirei inodhura kudaro?" (why is it that expensive?).
ack           Pure acknowledgment with zero booking intent:
              "ok", "sharp", "thanks", "noted", "fine", "sure", "👍", "👌".
              Shona acks: "maita", "maita basa", "ndatenda", "mazvita",
              "zvakanaka", "zvaita", "ndinzwisisa", "hongu", "ehe", "shuwa".
              Only use "ack" when the message adds NOTHING to the conversation.

─── SERVICE TYPE (one or null) ───────────────────────────────────────────────
bathroom_renovation, kitchen_renovation, bathroom_and_kitchen_renovation,
new_plumbing_installation, drain_unblocking, pipe_repair, geyser_repair,
toilet_repair

─── PRODUCT INTENT (most specific, or "none") ────────────────────────────────
tub_sales        Any message asking about tub price/cost — "how much tub",
                 "tub price", "how much is a tub", "do you sell tubs".
                 ⚠ Prefer tub_sales over combined_pricing whenever "tub" is mentioned.
standalone_tub   Specifically freestanding/standalone tub.
geyser, shower_cubicle, vanity, bathtub_installation, toilet, chamber,
drain_unblocking, pipe_repair, geyser_repair, toilet_repair,
location_ask, location_visit, pictures, combined_pricing, none

─── EXTRACT (null when not present in message) ───────────────────────────────
area              Suburb, neighbourhood, or city name.
                  Zimbabwe examples: Hatfield, Avondale, Borrowdale, Ziko,
                  Highfields, Glen View, Mbare, Chitungwiza, Ruwa, Gweru.
                  ⚠ When next_question is "area", treat short unknown words
                  as suburb names — NOT customer names.
availability      Date+time → YYYY-MM-DDTHH:MM  |  Date only → YYYY-MM-DDT00:00
                  "available all day" / "anytime" / "whole day" → null.
customer_name     Only if explicitly given: "my name is X", "I'm X", "call me X".
project_description  Verbatim project detail (max 120 chars).

─── FLAGS ────────────────────────────────────────────────────────────────────
is_photo_request  true if customer asks to see photos/pictures/portfolio of
                  our PREVIOUS work (not product pictures).
is_plan_later     true if customer says they'll send their plan/blueprint/
                  drawing at a later time ("I'll send the plan later").
is_repeat_question  true if the customer is asking something that has clearly
                  already been answered earlier in the conversation.

─── OUTPUT FORMAT (return exactly this structure) ────────────────────────────
{
  "intent": "in_scope",
  "confidence": "HIGH",
  "service_type": null,
  "product_intent": "none",
  "is_photo_request": false,
  "is_plan_later": false,
  "is_repeat_question": false,
  "extracted": {
    "area": null,
    "availability": null,
    "customer_name": null,
    "project_description": null
  }
}
confidence is HIGH when the classification is clear, LOW when ambiguous.\
"""


def unified_classify(
    message: str,
    appointment=None,
    conversation_history=None,
    today_date: str = "",
    next_question: str = "",
) -> dict | None:
    """
    Make one DeepSeek call and return a classification + extraction dict.

    Returns None on any failure — callers fall back to individual classifiers.
    """
    client = _get_client()
    if not client:
        return None

    # ── Appointment state summary ─────────────────────────────────────────────
    state_parts = []
    if appointment:
        if getattr(appointment, "project_type", None):
            state_parts.append(f"service={appointment.project_type}")
        if getattr(appointment, "customer_area", None):
            state_parts.append(f"area={appointment.customer_area}")
        if getattr(appointment, "scheduled_datetime", None):
            state_parts.append("datetime=set")
        if getattr(appointment, "status", None):
            state_parts.append(f"status={appointment.status}")
    apt_state = ", ".join(state_parts) if state_parts else "new lead"

    if next_question:
        apt_state += f" | next_question={next_question}"

    # ── Recent conversation (last 6 turns, 80 chars each) ────────────────────
    history = conversation_history or []
    lines = []
    for turn in history[-6:]:
        role    = "Customer" if turn.get("role") == "user" else "Bot"
        content = (turn.get("content") or "").strip()[:80]
        if content and not content.startswith("["):
            lines.append(f"{role}: {content}")
    context = "\n".join(lines) if lines else "(start of conversation)"

    user_content = (
        f"Appointment: {apt_state}\n"
        f"Conversation:\n{context}\n\n"
        f"Customer message: \"{message}\""
    )

    try:
        from bot.services.clients import deepseek_call
        raw = deepseek_call(
            messages=[
                {"role": "system", "content": _SYSTEM.replace("{today}", today_date)},
                {"role": "user",   "content": user_content},
            ],
            temperature=0.0,
            max_tokens=260,
            json_response=True,
        )
        result = json.loads(raw)
        logger.debug("unified_classify result: %s", result)
        return result
    except Exception as exc:
        logger.warning("unified_classify failed: %s", exc)
        return None


# ── Accessor helpers (safe — return sensible defaults when result is None) ────

def uc_intent(r: dict | None) -> str:
    return (r or {}).get("intent", "in_scope")

def uc_confidence(r: dict | None) -> str:
    return (r or {}).get("confidence", "HIGH")

def uc_service_type(r: dict | None) -> str | None:
    return (r or {}).get("service_type") or None

def uc_product_intent(r: dict | None) -> str:
    return (r or {}).get("product_intent") or "none"

def uc_is_photo_request(r: dict | None) -> bool:
    return bool((r or {}).get("is_photo_request", False))

def uc_is_plan_later(r: dict | None) -> bool:
    return bool((r or {}).get("is_plan_later", False))

def uc_is_repeat(r: dict | None) -> bool:
    return bool((r or {}).get("is_repeat_question", False))

def uc_extracted(r: dict | None) -> dict:
    return (r or {}).get("extracted") or {}

def uc_as_service_inquiry(r: dict | None) -> dict:
    """Format the result as the dict that detect_service_inquiry() would return."""
    return {
        "intent":     uc_product_intent(r),
        "confidence": uc_confidence(r),
    }

def uc_as_oos_classification(r: dict | None) -> dict:
    """Format the result as the dict that classify_message() would return."""
    intent = uc_intent(r)
    # Map unified intent names to OOS handler category names
    cat_map = {
        "in_scope":     "in_scope",
        "out_of_scope": "out_of_scope",
        "delay_signal": "delay_signal",
        "complaint":    "complaint",
        "ack":          "in_scope",   # acks should fall through normally
    }
    return {
        "category":   cat_map.get(intent, "in_scope"),
        "confidence": uc_confidence(r),
        "detail":     "",
    }
