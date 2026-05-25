"""
Repeated Question Detector
==========================
Uses DeepSeek to detect when a customer is asking the same question again
(even if worded differently), then generates a reassuring clarification response
that explains the previous answer and gently redirects to the plumber for
technical details.

CHANGE LOG
----------
- Added _classify_message_intent() — DeepSeek pre-classifier that runs BEFORE
  the repeat-question check.  Any message that expresses a booking action,
  provides project details, or is a short affirmative/negative is immediately
  returned as None (not a repeat), preventing the false-positive matches
  seen in production (e.g. "I would like to book an appointment" matching
  "May I have pictures of your work").
"""

import json
import logging
from typing import Optional
from django.conf import settings
from bot.services.clients import deepseek_client as _classifier

logger = logging.getLogger(__name__)

_classifier_model = getattr(settings, 'DEEPSEEK_MODEL', 'deepseek-chat')

PLUMBER_NUMBER_FALLBACK = '+263774819901'

# How many recent assistant messages to look back through
LOOKBACK_MESSAGES = 10

_INTAKE_PROMPT_SNIPPETS = (
    'what exactly do you want done',
    'which service are you interested in',
    'what service are you looking for',
    'tell me a bit more about the job',
    'the more detail, the more accurate',
)

_PROJECT_DETAIL_MARKERS = (
    'want', 'need', 'change', 'replace', 'install', 'fix', 'repair',
    'renovation', 'renovate', 'bathroom', 'shower', 'chamber', 'toilet',
    'bathtub', 'tub', 'geyser', 'vanity', 'cubicle', 'basin', 'sink',
    'pipe', 'drain',
)

_GENERIC_INFO_REQUEST_SNIPPETS = (
    'more information',
    'more info',
    'tell me more',
    'can i get more information',
    'may i get more information',
    'need more information',
)


# ─────────────────────────────────────────────────────────────────────────────
# NEW: DeepSeek intent pre-classifier
# ─────────────────────────────────────────────────────────────────────────────

def _classify_message_intent(message: str) -> str:
    """
    Use DeepSeek to classify the customer's message into one of:

        booking_action   — customer is booking, confirming, or scheduling
        project_detail   — customer is describing their project / answering a question
        short_response   — very short affirmative, negative, or acknowledgment
        repeat_candidate — could genuinely be a repeated question; proceed with check
        other            — anything else that should pass through normally

    Returns the intent string.  On any error, returns 'other' (safe default —
    lets the repeat check proceed rather than silently suppressing it).
    """
    if not _classifier:
        return 'other'

    prompt = f"""You are a message intent classifier for a Zimbabwean plumbing company's WhatsApp chatbot.
Customers write in English, Shona, or a mix of both.

Classify the customer message below into EXACTLY ONE of these intents:

booking_action
  The customer is taking a step towards booking or confirming an appointment.
  Examples: "I would like to book an appointment", "Let's go ahead", "I'm ready to book",
            "Can we schedule?", "Yes I want to proceed", "Ndoda kubhukisha" (Shona: I want to book),
            "Book me in", "Confirm the appointment", "I'd like to go ahead with the site visit".

project_detail
  The customer is describing their project, answering a question about what work they need,
  providing specific details about fixtures, rooms, or scope of work,
  OR asking whether you carry/sell a specific product (even if phrased as a question).
  Examples: "I need a new shower cubicle and toilet", "The bathroom is 3x3 metres",
            "We want to renovate the whole bathroom", "Replace the geyser",
            "Ndoda kushandura chimbuzi" (Shona: I want to change the toilet),
            "And vanitys if you have", "do you have tubs", "geysers?", "and chambers".

short_response
  A very short reply (1-3 words) that is an affirmative, negative, or simple acknowledgment
  with NO product or service name in it.
  Examples: "Yes", "No", "Ok", "Sure", "Noted", "Thanks", "Nope", "Yeah", "Ok cool",
            "Hongu" (Shona: yes), "Kwete" (Shona: no).
  NOT short_response if it names a product: "vanitys", "and vanitys", "shower cubicle",
  "geyser" — those are project_detail even if short.

repeat_candidate
  The message looks like it might be asking the same question the customer already asked,
  possibly rephrased.  Typically longer and phrased as a question.
  Examples: "But how much does it actually cost?", "I still don't understand the price",
            "What exactly is included in the site visit?", "Can you tell me again how it works?"

other
  Anything that doesn't fit the above — general chat, new questions, location info, etc.

RULES:
- Return ONLY the intent label, nothing else.
- If unsure between booking_action and other, choose booking_action.
- If unsure between short_response and anything else, choose short_response.
- Never return repeat_candidate for booking or project messages.

Customer message: "{message}"

Intent:"""

    try:
        response = _classifier.chat.completions.create(
            model=_classifier_model,
            messages=[
                {
                    'role': 'system',
                    'content': 'Return ONLY the intent label. No explanation, no punctuation.',
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.0,
            max_tokens=10,
        )
        raw = response.choices[0].message.content.strip().lower()

        valid_intents = {
            'booking_action', 'project_detail', 'short_response',
            'repeat_candidate', 'other',
        }
        if raw in valid_intents:
            logger.info(f"Intent pre-classifier: '{message[:60]}' → {raw}")
            return raw

        # Fuzzy safety net
        if 'booking' in raw:
            return 'booking_action'
        if 'project' in raw or 'detail' in raw:
            return 'project_detail'
        if 'short' in raw:
            return 'short_response'
        if 'repeat' in raw:
            return 'repeat_candidate'

        logger.warning(f"Intent pre-classifier returned unexpected value: {raw!r}")
        return 'other'

    except Exception as exc:
        logger.warning(f"Intent pre-classifier failed: {exc}")
        return 'other'


# ─────────────────────────────────────────────────────────────────────────────
# Existing helpers (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_recent_qa_pairs(conversation_history: list) -> list[dict]:
    """
    Walk the conversation history backwards and collect (customer_question, bot_answer) pairs.
    Returns up to 5 pairs, oldest-first, skipping system/media messages.
    """
    pairs = []
    history = conversation_history or []

    i = len(history) - 1
    while i >= 1 and len(pairs) < 5:
        msg = history[i]
        if msg.get('role') == 'assistant':
            content = (msg.get('content') or '').strip()
            skip_prefixes = (
                '[AUTO FOLLOW-UP]', '[MANUAL FOLLOW-UP]', '[BULK MANUAL FOLLOW-UP]',
                '[PLAN FOLLOW-UP', '[PLAN PIVOT', '[Sent ', '[FILE UPLOADED]',
                '[VIDEO UPLOADED]', 'APPOINTMENT CONFIRMED', 'NEW APPOINTMENT BOOKED',
            )
            if content and not any(content.startswith(p) for p in skip_prefixes):
                j = i - 1
                while j >= 0:
                    prev = history[j]
                    if prev.get('role') == 'user':
                        q = (prev.get('content') or '').strip()
                        if q and not q.startswith('[Sent '):
                            pairs.append({
                                'question': q[:400],
                                'answer': content[:600],
                            })
                        break
                    j -= 1
        i -= 1

    pairs.reverse()
    return pairs


def _is_generic_intake_answer(answer: str) -> bool:
    text = (answer or '').strip().lower()
    if not text:
        return False
    return any(snippet in text for snippet in _INTAKE_PROMPT_SNIPPETS)


def _looks_like_project_detail_message(message: str) -> bool:
    text = (message or '').strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _PROJECT_DETAIL_MARKERS)


def _is_generic_info_request(message: str) -> bool:
    text = (message or '').strip().lower()
    if not text:
        return False
    return any(snippet in text for snippet in _GENERIC_INFO_REQUEST_SNIPPETS)


# ─────────────────────────────────────────────────────────────────────────────
# Main public function — updated with intent pre-classifier gate
# ─────────────────────────────────────────────────────────────────────────────

def detect_repeated_question(
    new_message: str,
    conversation_history: list,
) -> Optional[dict]:
    """
    Use DeepSeek to determine whether `new_message` is semantically the same
    as a question the customer already asked.

    Returns:
        None  — if it's a fresh question (no action needed)
        dict  — {
                    'is_repeat': True,
                    'matched_question': str,
                    'matched_answer': str,
                }
    """
    if not _classifier:
        return None

    # ── GATE: classify intent before doing the expensive repeat check ─────────
    intent = _classify_message_intent(new_message)

    # Booking actions, project details, and short responses are NEVER repeats.
    # Only proceed to the full repeat-check for repeat_candidate or other.
    if intent in ('booking_action', 'project_detail', 'short_response'):
        logger.info(
            f"Repeat-question check skipped — intent={intent}, "
            f"message='{new_message[:80]}'"
        )
        return None
    # ─────────────────────────────────────────────────────────────────────────

    pairs = _extract_recent_qa_pairs(conversation_history)
    if not pairs:
        return None

    history_text = "\n".join(
        f"Q{idx + 1}: {p['question']}\nA{idx + 1}: {p['answer'][:300]}"
        for idx, p in enumerate(pairs)
    )

    prompt = f"""You are a repeat-question detector for a Zimbabwean plumbing chatbot.

PREVIOUS Q&A PAIRS (most recent last):
{history_text}

NEW CUSTOMER MESSAGE:
"{new_message}"

TASK:
Is the new message asking essentially the same thing as any of the previous questions — even if the wording is completely different?

Consider these as the SAME question:
- Rephrasing with different words
- Asking for confirmation of a previous answer
- Adding "but seriously" / "I still don't understand" etc.
- Asking the same thing in Shona when they asked in English before (or vice versa)

Consider these as DIFFERENT questions:
- A genuine follow-up asking for new information
- Choosing between options the bot presented
- Confirming an appointment detail
- Providing their name, area, or availability
- Booking or scheduling an appointment
- Describing project work they want done

Return ONLY valid JSON:
{{
  "is_repeat": true or false,
  "matched_index": null or 1-based index into the Q&A pairs (e.g. 2 means Q2/A2),
  "confidence": "HIGH" or "LOW"
}}"""

    try:
        response = _classifier.chat.completions.create(
            model=_classifier_model,
            messages=[
                {
                    'role': 'system',
                    'content': 'Return ONLY valid JSON. No markdown, no explanation.',
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.1,
            max_tokens=80,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()
        result = json.loads(raw)

        is_repeat = result.get('is_repeat') is True
        confidence = (result.get('confidence') or 'LOW').upper()
        matched_index = result.get('matched_index')

        logger.info(
            f"Repeat-question check: is_repeat={is_repeat}, confidence={confidence}, "
            f"matched_index={matched_index}, message='{new_message[:80]}'"
        )

        if is_repeat and confidence == 'HIGH' and matched_index:
            idx = int(matched_index) - 1
            if 0 <= idx < len(pairs):
                matched_pair = pairs[idx]
                if (
                    _is_generic_intake_answer(matched_pair['answer']) and
                    _looks_like_project_detail_message(new_message)
                ):
                    logger.info(
                        "Repeat-question suppressed because matched answer was a "
                        "generic intake prompt and new message looks like project detail."
                    )
                    return None
                if (
                    _is_generic_info_request(matched_pair['question']) and
                    _is_generic_intake_answer(matched_pair['answer']) and
                    _looks_like_project_detail_message(new_message)
                ):
                    logger.info(
                        "Repeat-question suppressed because prior exchange was "
                        "generic info request -> intake prompt, and new message "
                        "looks like a specific service/detail answer."
                    )
                    return None
                return {
                    'is_repeat': True,
                    'matched_question': matched_pair['question'],
                    'matched_answer': matched_pair['answer'],
                }

        return None

    except Exception as exc:
        logger.warning(f"Repeat-question detection failed: {exc}")
        return None


def generate_repeat_clarification(
    new_message: str,
    matched_question: str,
    matched_answer: str,
    plumber_number: str = PLUMBER_NUMBER_FALLBACK,
    language_hint: str = 'english',
) -> str:
    """
    Generate a warm, reassuring response for a repeated question.
    """
    if not _classifier:
        return _fallback_repeat_response(plumber_number)

    lang_instruction = (
        'Respond in Shona.' if language_hint == 'shona'
        else 'Respond in both Shona and English (Shona first, then English).' if language_hint == 'mixed'
        else 'Respond in English.'
    )

    prompt = f"""You are a friendly WhatsApp assistant for Homebase Plumbers in Zimbabwe.

The customer asked essentially the same question twice (different wording).

THEIR ORIGINAL QUESTION:
"{matched_question}"

WHAT THE BOT SAID BEFORE:
"{matched_answer}"

THEIR NEW (REPEATED) MESSAGE:
"{new_message}"

TASK:
Write a warm, natural WhatsApp reply that does ALL FOUR of the following — in this order:

1. REASSURE & ACKNOWLEDGE — Make them feel heard. Acknowledge that their question is totally valid. 1-2 sentences.
2. EXPLAIN THE PREVIOUS ANSWER — In simple, friendly language, briefly explain WHY the you gave that answer (not just repeat it verbatim). 2-3 sentences max.
3. ASK IF THEY NEED MORE CLARITY — One short, open question inviting them to say what's still unclear.
4. REDIRECT TO THE PLUMBER — Gently explain you're just an assistant handling bookings, and for anything technical or project-specific they should speak directly to the plumber. Include their number: {plumber_number}

RULES:
- Sound like a real, warm human — not a corporate helpdesk
- Keep the whole message under 180 words
- Use simple everyday language — no jargon
- No markdown headers, no bullet points
- One emoji max, only if it feels natural
- {lang_instruction}
- Never say "I'm just a bot" — say "I'm the booking assistant" or similar
- Zimbabwean English tone ("sorted", "sharp", "no worries")

Write the message now:"""

    try:
        response = _classifier.chat.completions.create(
            model=_classifier_model,
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You write warm, concise WhatsApp messages. '
                        'Sound human. Follow instructions exactly.'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.6,
            max_tokens=280,
        )
        reply = response.choices[0].message.content.strip()
        reply = reply.replace('**', '').replace('__', '')
        logger.info(f"Generated repeat clarification ({len(reply)} chars)")
        return reply

    except Exception as exc:
        logger.warning(f"Failed to generate repeat clarification: {exc}")
        return _fallback_repeat_response(plumber_number)


def _fallback_repeat_response(plumber_number: str) -> str:
    clean = plumber_number.replace('+', '').replace('whatsapp:', '')
    return (
        "No worries at all — totally understand if the earlier answer wasn't clear. "
        "I'm the booking assistant so I can help with appointments and general info, "
        "but for anything technical or specific to your project it's best to speak "
        f"directly with our plumber on {clean}. "
        "Is there anything else I can help clarify?"
    )


def detect_language_simple(message: str) -> str:
    """
    Quick language hint without an API call.
    Returns 'shona', 'mixed', or 'english'.
    """
    shona_markers = [
        # greetings / farewells
        'mhoro', 'makadii', 'makadini', 'wakadii', 'ndeipi', 'waswera sei',
        'masikati', 'mangwanani', 'manheru', 'usiku',
        # acknowledgments
        'hongu', 'kwete', 'zvakanaka', 'zvaita', 'zvaenda', 'ndatenda',
        'maita', 'maita basa', 'mazvita', 'ndinzwisisa', 'ndanzwisisa',
        'inzwika', 'ehe', 'shuwa',
        # intent / action words
        'ndinoda', 'ndoda', 'ndinogona', 'handina', 'ndinofarira',
        'ndichatumira', 'ndinotumira', 'ndichaenda', 'ndinoenda',
        'tiuye', 'mauya', 'mungauya', 'munouya', 'mauya rini',
        # questions
        'chii', 'riini', 'rinhi', 'sei', 'kupi', 'papi', 'mangani',
        'marii', 'mari', 'mutengo',
        # time
        'mangwana', 'nhasi', 'nezuro', 'mauro', 'vhiki', 'mwedzi',
        'gore', 'kwapera', 'nguva',
        # household / property
        'imba', 'musha', 'nzvimbo', 'bhizimisi', 'bhavhu',
        # plumbing / fixtures (Shona-ised or local terms)
        'chimbuzi', 'shawa', 'kicheni', 'mapombi', 'pombi', 'mvura',
        'chidhinha', 'zvindori',
        # pricing / commerce
        'zvinodhura', 'inodhura', 'bhadhara', 'dhora', 'peni', 'zviri nani',
        # classifier markers
        'zvinoita', 'zvese', 'zvakadai',
    ]
    msg_lower = message.lower()
    shona_count = sum(1 for m in shona_markers if m in msg_lower)
    english_words = len([w for w in msg_lower.split() if w.isalpha()])

    if shona_count >= 2:
        return 'shona'
    if shona_count == 1 and english_words > 2:
        return 'mixed'
    return 'english'
