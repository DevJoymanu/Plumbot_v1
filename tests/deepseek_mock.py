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
    clients.deepseek_client.chat.completions.create = _make_fake_create()
    _installed = True
    print("🧪 DeepSeek mock installed — LLM calls are deterministic/offline")
