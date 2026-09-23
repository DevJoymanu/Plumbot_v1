"""
Deterministic DeepSeek stub for offline / reproducible test runs.

The bot funnels every LLM call through `bot.services.clients.deepseek_client.
chat.completions.create`. In production that hits api.deepseek.com; in tests a
live call is slow, costs money, and — worst of all — is non-deterministic, so a
"failing" test could be model drift rather than a real regression. That made the
suite useless as a commit gate.

This module replaces that one entry point with a deterministic fake that returns
sensible canned answers based on the prompt shape (language detection, the
price-request gate, etc.). It does NOT try to be a smart classifier — the
deterministic regression contract lives in TEST 0, which is API-free by design.
The fake just keeps incidental LLM calls (e.g. language detection inside a
pricing reply) from being flaky or hitting the network.

Activate by importing and calling `install()` before the bot makes any call,
or by setting PLUMBOT_MOCK_DEEPSEEK=1 (the test runner installs it for you).
"""

import json
import re


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)
        self.finish_reason = "stop"


class _FakeUsage:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


_SHONA_HINTS = (
    'marii', 'mari', 'mutengo', 'zvinodhura', 'zvese', 'imarii', 'ndoda',
    'munoita', 'muri', 'kupi', 'ndinoda', 'mangwana', 'mhoro', 'makadii',
    'maswera', 'tatenda', 'ko ', 'kuisa', 'inodhura',
)
_PRICE_HINTS = (
    'how much', 'price', 'cost', 'quote', 'quotation', 'charge', 'rate',
    'hw much', 'hw mch', 'howmuch', 'marii', 'mari', 'mutengo', 'zvinodhura',
)
# Phrases signalling the customer will make the next contact themselves — used by
# the self-initiated-defer classifier gate below.
_DEFER_HINTS = (
    'get in touch', 'get back to you', 'get back to u', 'let you know',
    'reach out', 'touch base', 'contact you', 'call you', 'message you',
    'text you', 'revert', 'be in touch', 'will advise', 'ndichaku',
    'ndinokutaurira',
)
# Purely social wording — used by the "does this need a reply?" gate below for
# the turns the deterministic ack list can't enumerate.
_SOCIAL_ACK_HINTS = (
    'appreciate', 'no problem', 'that works', 'works for now', 'speak then',
    'speak on', 'shout when', 'cheers', 'all good', 'will do', 'noted',
    'much obliged', 'you too', 'have a good',
)


# ── Unified-turn fidelity ────────────────────────────────────────────────────
# The unified call returns the classification EVERY downstream handler reads, so
# a stub that answers `service_type: null, product_intent: "none"` to every
# message routes every offline conversation down the "we learned nothing" path.
# That is not a harmless simplification: the pricing reply, the budget ladder
# and the whole qualification order hang off these two fields, so the offline
# scenario suite could not reach the branches that carry the owner's rules.
#
# These maps are deliberately KEYWORD-SHALLOW. They are not an attempt to be a
# classifier - the deterministic contract still lives in TEST 0. They exist so
# a replayed conversation takes the same BRANCH it takes in production, which
# is the thing `bot/test_scenarios.py` is measuring. First match wins, so the
# more specific phrase is listed first.
_SERVICE_TYPE_HINTS = (
    ('bathroom_and_kitchen_renovation', ('bathroom and kitchen', 'kitchen and bathroom')),
    ('new_plumbing_installation', ('new build', 'new house', 'building a house',
                                   'new installation', 'new plumbing', 'from scratch')),
    ('bathroom_renovation', ('bathroom', 'renovate a bathroom', 'bathroom renovation',
                             'imba yekugezera')),
    ('kitchen_renovation', ('kitchen', 'kitchen renovation')),
    ('geyser_repair', ('geyser', 'gwedza')),
)
# Product families the bot prices directly. The webhook's own deterministic
# `_keyword_product_intent` already overrides this for the customer's own product
# word, so these only have to be good enough not to CONTRADICT it.
_PRODUCT_INTENT_HINTS = (
    ('wall_hung_toilet', ('wall hung toilet', 'wall-hung toilet', 'wall mounted toilet')),
    ('shower_cubicle', ('shower cubicle', 'shower', 'cubicle')),
    ('standalone_tub', ('freestanding tub', 'free standing tub', 'standalone tub')),
    ('tub_sales', ('tub', 'bathtub', 'bath tub')),
    ('vanity', ('vanity', 'basin')),
    ('toilet_seat', ('toilet seat', 'toilet')),
    ('geyser_repair', ('geyser',)),
)


# Booking fields. A stub that answers `extracted: {area: null, ...}` to every
# message models a classifier that NEVER extracts anything — and since a null is
# indistinguishable from "not present" to every caller (there is no per-field
# confidence), an offline conversation could never get past the area question,
# so no scenario could reach the availability, booking or confirmation copy.
# Production measures ~100% on these four fields, so "extracts nothing" is the
# unrealistic model, not this.
#
# Written with the stub's OWN small regexes rather than by calling the bot's
# resolvers (`_area_from_reply`, `_keyword_availability_date`): those resolvers
# are the deterministic FLOOR that exists for when this call fails, and a stub
# that delegated to them would be scoring them against themselves.
_SUBURB_HINTS = (
    'hatfield', 'bluffhill', 'borrowdale', 'avondale', 'mount pleasant',
    'greendale', 'highlands', 'marlborough', 'westgate', 'glen lorne',
    'chisipite', 'belvedere', 'waterfalls', 'budiriro', 'kuwadzana',
    'msasa', 'eastlea', 'milton park', 'ziko', 'ruwa', 'norton', 'chitungwiza',
)
_DAY_HINTS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday',
              'saturday', 'sunday', 'tomorrow', 'today', 'next week',
              'this week', 'midweek', 'weekend')


def _extracted(user, user_l):
    """Shallow booking-field extraction — see the note above _SUBURB_HINTS."""
    area = next((s.title() for s in _SUBURB_HINTS if s in user_l), None)

    availability = next((d for d in _DAY_HINTS if d in user_l), None)

    name = None
    m = re.search(r"(?:my name is|i am|i'm|this is|im)\s+([A-Z][a-z]+)", user or '')
    if m:
        name = m.group(1)

    # The description is the customer's own sentence when it carries a job word,
    # which is what the real classifier returns (normalised, not verbatim).
    # Read from the CUSTOMER'S MESSAGE only (the prompt's closing
    # 'Customer message: "..."' line), never the whole prompt: scanning the
    # prompt matched the bot's own opener ("Installations, renovations") and
    # stored the ENTIRE prompt as the job description, so every replay that
    # opened with the old opener sailed past the description question by
    # accident. "redone" joins the list because the real model reads "I need
    # my bathroom redone" as a description; the list is otherwise unchanged,
    # so no other replay changes path.
    said = re.search(r'Customer message:\s*"(.*)"\s*$', user or '', re.DOTALL)
    said = said.group(1).strip() if said else (user or '').strip()
    said_l = said.lower()
    description = None
    if any(w in said_l for w in ('renovate', 'install', 'fix', 'repair', 'replace',
                                 'build', 'leak', 'burst', 'blocked', 'quote for',
                                 'redone')):
        description = said

    return {"area": area, "availability": availability,
            "customer_name": name, "project_description": description}


def _first_hint(text, table):
    """Return the label whose first matching keyword appears in `text`, else None."""
    for label, needles in table:
        if any(n in text for n in needles):
            return label
    return None


def _last_user(messages):
    for m in reversed(messages or []):
        if m.get('role') == 'user':
            return m.get('content', '') or ''
    return ''


def _system(messages):
    for m in (messages or []):
        if m.get('role') == 'system':
            return m.get('content', '') or ''
    return ''


def _respond(messages, json_response):
    """Pick a deterministic response for the given prompt."""
    system = _system(messages).lower()
    user = _last_user(messages)
    user_l = user.lower()

    # ── Language detection ("Reply with ONLY 'shona', 'english', or 'mixed'")
    if 'language' in system and ('shona' in system or 'english' in system):
        return 'shona' if any(h in user_l for h in _SHONA_HINTS) else 'english'

    # ── Price-request gate (strict JSON {"price_request": bool})
    if 'price_request' in system or 'price_request' in user_l:
        asked = any(h in user_l for h in _PRICE_HINTS)
        return json.dumps({"price_request": asked})

    # ── Self-initiated-defer classifier ("will the customer make the next
    # contact themselves?"). Look ONLY at the actual message (after the 'Message:'
    # marker) so the in-prompt examples don't leak into the keyword check.
    if 'next contact' in user_l:
        tail = user_l.split('message:')[-1]
        return 'yes' if any(h in tail for h in _DEFER_HINTS) else 'no'

    # ── "Needs no reply?" gate (should_hold_silently's ambiguous middle). Only
    # the tail after the 'Message:' marker is scanned so the in-prompt examples
    # don't leak into the keyword check.
    if 'acknowledgement or a sign-off' in user_l:
        tail = user_l.split('message:')[-1]
        return 'yes' if any(h in tail for h in _SOCIAL_ACK_HINTS) else 'no'

    # ── The unified turn MUST be matched before the generic yes/no branch
    # below. The unified prompt contains the sentence "One reading if you can
    # only see one, as a yes or no", which matches that branch's
    # `yes\s+or\s+no` regex — so every unified call offline returned the
    # bare string "NO", failed to parse as JSON, and `unified_turn` handed back
    # None. The ONE call every downstream handler reads was dead in every
    # offline run, and nothing reported it: TEST 0 exercises resolvers directly,
    # so it stayed green while the replayed conversations silently took the
    # no-classification path. Specific markers go above generic ones here.
    # ── The unified turn (classification + plan). Returning "{}" here would
    # exercise only the planning-absent path, so the stub emits a minimally
    # valid payload instead: enough for bot.controller.validate_turn to pass,
    # so the offline suite covers the branch prod actually takes.
    if 'planning (what we should do next)' in system:
        return json.dumps({
            "reasoning": "",
            "next_move": "ask_qualifying_question",
            # The stub does not try to be clever about WHICH question: null
            # means "no opinion", which is the path most turns take and the one
            # that must keep the deterministic order working.
            "next_question": None,
            "move_confidence": 0.8,
            "comprehension": {"intent": "describing_want",
                              "sentiment": "neutral",
                              "language": "en"},
            "state_update": {"want_level": "interested"},
            "intent": "in_scope",
            "confidence": "HIGH",
            # Derived from the message rather than hardcoded null — see the
            # note on _SERVICE_TYPE_HINTS above.
            "service_type": _first_hint(user_l, _SERVICE_TYPE_HINTS),
            "product_intent": _first_hint(user_l, _PRODUCT_INTENT_HINTS) or "none",
            "is_photo_request": False,
            "is_plan_later": False,
            "is_repeat_question": False,
            "english": "",
            "extracted": _extracted(user, user_l),
        })

    # ── Yes/No style gates (photo request, standalone-question, exit intent…)
    if re.search(r'reply\s+(only\s+)?(with\s+)?(yes|no)', system) or \
       re.search(r'\byes\s+or\s+no\b', system):
        return "NO"

    # ── Anything expecting JSON we don't model → empty object (callers fall back)
    if json_response or 'json' in system:
        return "{}"

    # ── Free-form prose → a short, safe, price-free acknowledgement.
    return "Thanks for that. The next step is a quick free on-site visit so we can help properly."


def _make_fake_create(passthrough=None):
    def _fake_create(*args, **kwargs):
        messages = kwargs.get('messages')
        if messages is None and args:
            messages = args[0]
        json_response = bool(kwargs.get('response_format'))
        content = _respond(messages or [], json_response)
        return _FakeCompletion(content)
    return _fake_create


_installed = False


def install():
    """Monkeypatch the shared DeepSeek client with the deterministic fake."""
    global _installed
    if _installed:
        return
    from bot.services import clients
    fake = _make_fake_create()
    clients.deepseek_client.chat.completions.create = fake
    # EVERY DeepSeek client, not only the shared one. Several modules build
    # their own OpenAI client from DEEPSEEK_API_KEY (out_of_scope_handler, the
    # webhook's translation and spam checks, unified_classifier), and tests
    # load the real .env, so a test that reached one of them made a real,
    # PAID call nobody chose to make (found 2026-09-23 while measuring the
    # live-test cost). Patching the library class answers them all with the
    # same fake; the owner's rule is no paid call without asking, every time.
    import openai.resources.chat.completions as _occ
    _occ.Completions.create = lambda self, *a, **k: fake(*a, **k)
    _installed = True
    print("🧪 DeepSeek mock installed — LLM calls are deterministic/offline")
