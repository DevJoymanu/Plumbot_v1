# bot/service_type_classifier.py
#
# Classifies an incoming customer message into one of three service types:
#   - "Bathroom Renovation"
#   - "Kitchen Renovation"
#   - "New Plumbing Installation"
#   - None  (cannot be classified with confidence)
#
# Strategy:
#   1. Fast keyword/phrase matching  → instant result, no API call
#   2. DeepSeek classification       → for messages that are plumbing-related
#                                       but don't hit any keyword
#   3. Returns None                  → if DeepSeek also cannot classify
#
# Usage (from your message handler / views.py):
#
#   from bot.service_type_classifier import classify_service_type
#
#   service_type = classify_service_type(customer_message)
#   if service_type:
#       lead.project_type = service_type
#       lead.save(update_fields=['project_type'])

from __future__ import annotations
import os
import logging
import re
from openai import OpenAI

logger = logging.getLogger(__name__)

# ── DeepSeek client (optional — falls back gracefully if key not set) ─────────
_DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY')
_deepseek = (
    OpenAI(api_key=_DEEPSEEK_KEY, base_url='https://api.deepseek.com/v1')
    if _DEEPSEEK_KEY else None
)

# ── Service type constants (match Appointment.project_type values) ────────────
BATHROOM_RENOVATION      = 'Bathroom Renovation'
KITCHEN_RENOVATION       = 'Kitchen Renovation'
NEW_PLUMBING_INSTALLATION = 'New Plumbing Installation'

# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD RULES
# Each entry is a tuple: (service_type, [list of keyword/phrase patterns])
# Patterns are matched case-insensitively against the full message.
# Longer / more specific phrases are listed first to avoid false positives.
# ─────────────────────────────────────────────────────────────────────────────

_KEYWORD_RULES: list[tuple[str, list[str]]] = [

    # ── NEW PLUMBING INSTALLATION ─────────────────────────────────────────────
    # Check FIRST — "new bathroom" should map here, not bathroom renovation
    (NEW_PLUMBING_INSTALLATION, [
        'plumbing for a new house',
        'plumbing for new house',
        'plumbing for new build',
        'plumbing for an extension',
        'plumbing for extension',
        'new building plumbing',
        'new bathroom in a new',
        'new bathroom for a new',
        'full house piping',
        'whole house piping',
        'entire house plumbing',
        'underground piping',
        'new water line',
        'new geyser system',
        'new geyser installation',
        'new plumbing',
        'install plumbing',
        'plumbing installation',
        'from scratch',
        'new property',
        'new structure',
        'new house',
        'new home',
        'new build',
        'building plumbing',
        'extension plumbing',
        'house extension',
        'building extension',
        'i.*m building.*extension',  # "I'm building an extension"
        'building an extension',
        'adding an extension',
        # Shona / mixed
        'imba itsva',                 # new house
        'kugadzira imba itsva',       # building a new house
        'kuvaka imba',                # building a house
        'plumbing yeimba itsva',
        'mapombi matsva',             # new pipes
        'kuisa mapombi',              # install pipes
        'kuisa plumbing',
    ]),

    # ── KITCHEN RENOVATION ────────────────────────────────────────────────────
    # Check BEFORE bathroom so "kitchen sink" doesn't get caught by bathroom's
    # generic 'sink installation' rule.
    (KITCHEN_RENOVATION, [
        'kitchen renovation',
        'kitchen remodel',
        'kitchen plumbing',
        'kitchen sink installation',
        'install kitchen sink',
        'replace kitchen sink',
        'kitchen tap',
        'kitchen faucet',
        'dishwasher plumbing',
        'plumb.*dishwasher',
        'dishwasher installation',
        'kitchen pipe',
        'kitchen drainage',
        'kitchen drain',
        'water line for fridge',
        'fridge water line',
        'water line.*fridge',
        'plumbing.*fridge',
        'kitchen water',
        'kitchen basin',
        'kitchen sink',              # unambiguous — must be before generic 'sink' rules
        # Shona / mixed
        'kicheni',                    # kitchen
        'sink yekicheni',
        'bheseni rekicheni',
        'tap yekicheni',
        'pombi dzekicheni',
    ]),

    # ── BATHROOM RENOVATION ───────────────────────────────────────────────────
    (BATHROOM_RENOVATION, [
        # multi-word phrases first
        'bathroom renovation',
        'bathroom upgrade',
        'bathroom remodel',
        'bathroom piping',
        'bathroom tiling',
        'bathroom tile',
        'full bathroom',
        'install a toilet',
        'toilet installation',
        'toilet replacement',
        'replace toilet',
        'replace a toilet',
        'new toilet',
        'toilet price',
        'toilet cost',
        'how much.*toilet',          # regex-style — see _keyword_match()
        'install.*bathtub',
        'bathtub installation',
        'bath installation',
        'install.*shower',
        'shower installation',
        'shower replacement',
        'replace.*shower',
        'basin installation',
        'install.*basin',
        'sink installation',         # basin/sink = bathroom context (kitchen already caught above)
        'install.*sink',
        'chamber',                   # chamber = bathroom soil/drain chamber in ZW/SA context
        'cistern',
        'bathroom drainage',
        'bathroom drain',
        'shower',                    # any mention of shower → bathroom reno
        'toilet',                    # any mention of toilet → bathroom reno
        'bathtub',
        'bath tub',
        'shower tray',
        'shower head',
        'shower enclosure',
        'bidet',
        'urinal',
        # Shona / mixed
        'chimbuzi',                  # toilet
        'bhavhu',                    # bath tub
        'bhavu',                     # alt spelling
        'shawa',                     # shower
        'bheseni',                   # basin/sink
        'sink yemubathroom',
        'bheseni remubathroom',
    ]),
]


def _normalise(text: str) -> str:
    """Lower-case and collapse whitespace for matching."""
    return re.sub(r'\s+', ' ', text.lower().strip())


def _keyword_match(message: str) -> str | None:
    """
    Return service type if any keyword/pattern matches, else None.
    Patterns that look like regex (contain .* or similar) are treated as
    regex patterns; plain strings are substring-matched.
    """
    norm = _normalise(message)
    for service_type, patterns in _KEYWORD_RULES:
        for pattern in patterns:
            if '.*' in pattern or pattern.startswith('^') or pattern.endswith('$'):
                # regex pattern
                if re.search(pattern, norm):
                    return service_type
            else:
                # plain substring
                if pattern in norm:
                    return service_type
    return None


def _deepseek_classify(message: str) -> str | None:
    """
    Ask DeepSeek to classify the message.
    Returns one of the three service type strings, or None if unclassifiable.
    Only called when keyword matching yields no result.
    """
    if not _deepseek:
        logger.debug('DeepSeek client not available — skipping AI classification')
        return None

    prompt = f"""You are a classification assistant for a plumbing company in Zimbabwe/South Africa called Homebase Plumbers.

A customer sent this message:
\"\"\"
{message}
\"\"\"

Decide which ONE of the following service types best describes what the customer is asking about.
Only classify it if you are reasonably confident (>70%) the customer is asking about plumbing work.

SERVICE TYPES:
1. Bathroom Renovation
   → Any installation, replacement, upgrade, or pricing query involving: toilet, cistern, chamber, bathtub, shower, basin, sink (not kitchen), bathroom tiling, bathroom piping, bathroom drainage, bidet, urinal, bathroom geyser.
   → Even if they only mention one item (e.g. "how much is a toilet?"), classify as Bathroom Renovation unless it's clearly a minor repair.

2. Kitchen Renovation
   → Any installation, replacement, upgrade, or pricing query involving: kitchen sink, kitchen tap, kitchen faucet, dishwasher plumbing, kitchen pipe, kitchen drainage, fridge water line, kitchen water supply.

3. New Plumbing Installation
   → Plumbing being installed FROM SCRATCH: new house, new building, extension plumbing, full house piping, underground piping, new geyser system, new water lines for a new structure.

INSTRUCTIONS:
- Reply with ONLY the exact label: "Bathroom Renovation", "Kitchen Renovation", or "New Plumbing Installation"
- If the message is NOT about plumbing work (e.g. it's a greeting, question about price in general, or unrelated topic), reply with exactly: "UNCLASSIFIABLE"
- Do NOT add any explanation, punctuation, or extra words."""

    try:
        response = _deepseek.chat.completions.create(
            model='deepseek-chat',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You are a precise classifier. '
                        'Output ONLY one of the four allowed labels. '
                        'No explanation. No punctuation after the label.'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            max_tokens=20,
            temperature=0.0,   # deterministic — classification needs consistency
        )

        raw = response.choices[0].message.content.strip().strip('"').strip("'")
        logger.debug(f'DeepSeek classification raw output: {raw!r}')

        if raw in (BATHROOM_RENOVATION, KITCHEN_RENOVATION, NEW_PLUMBING_INSTALLATION):
            return raw
        if raw.upper() == 'UNCLASSIFIABLE':
            return None

        # Fuzzy safety net in case model adds minor punctuation / casing drift
        raw_lower = raw.lower()
        if 'bathroom' in raw_lower:
            return BATHROOM_RENOVATION
        if 'kitchen' in raw_lower:
            return KITCHEN_RENOVATION
        if 'new plumbing' in raw_lower or 'installation' in raw_lower:
            return NEW_PLUMBING_INSTALLATION

        logger.warning(f'Unexpected DeepSeek classification output: {raw!r}')
        return None

    except Exception as exc:
        logger.warning(f'DeepSeek classification failed: {exc}')
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def classify_service_type(message: str) -> str | None:
    """
    Classify a customer message into a service type.

    Returns one of:
        "Bathroom Renovation"
        "Kitchen Renovation"
        "New Plumbing Installation"
        None  — if the message cannot be confidently classified

    Strategy:
        1. Keyword matching  (fast, no API call)
        2. DeepSeek fallback (for plumbing-aligned but keyword-missing messages)

    Example usage in your message handler:

        from bot.service_type_classifier import classify_service_type

        service_type = classify_service_type(incoming_text)
        if service_type and not lead.project_type:
            lead.project_type = service_type
            lead.save(update_fields=['project_type'])
    """
    if not message or not message.strip():
        return None

    # 1. Fast keyword match
    result = _keyword_match(message)
    if result:
        logger.debug(f'Service type classified by keyword: {result}')
        return result

    # 2. AI fallback for edge cases
    result = _deepseek_classify(message)
    if result:
        logger.debug(f'Service type classified by DeepSeek: {result}')
    else:
        logger.debug('Service type could not be classified')

    return result


def classify_and_save(lead, message: str) -> str | None:
    """
    Convenience wrapper: classify the message and save to lead.project_type
    only if it isn't already set.

    Returns the classified service type (or None if unchanged / unclassifiable).

    Example:
        from bot.service_type_classifier import classify_and_save

        classify_and_save(lead, customer_message)
    """
    if lead.project_type:
        # Already classified — don't overwrite
        return lead.project_type

    service_type = classify_service_type(message)
    if service_type:
        lead.project_type = service_type
        lead.save(update_fields=['project_type'])
        logger.info(f'Lead {lead.id} project_type set to "{service_type}" from message: {message[:80]!r}')

    return service_type
