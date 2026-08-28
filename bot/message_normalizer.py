"""
Inbound language normalisation — the English rendering of what the customer wrote.

Every deterministic resolver in this codebase (delay signals, "send it here",
purchase commitments, price asks, breakout inquiries) is a list of ENGLISH
phrases. Customers write Shona. That mismatch is not an occasional miss, it is
structural: each resolver silently fails on half the traffic until somebody
hand-writes the Shona phrases into it, one list at a time, after a lead has
already been mishandled in production (barmak, 2026-08-28: "Muno sender zvenyu
ipapa apa" — send it right here — was answered by re-asking the same question,
twice).

So the message is normalised ONCE per turn and every resolver gets to scan the
English rendering alongside the customer's own words. DeepSeek already reads the
message in `unified_classify`, so the rendering rides along on that existing
call — no extra round trip, no extra latency, no new failure mode.

Two rules make this safe:

  1. The translation is for the RULE ENGINE ONLY. It must never reach
     customer-facing copy or a reply prompt — the bot mirrors the lead's own
     language (Shona in, Shona out). This is the mirror image of the
     `quoted_context` rule in CLAUDE.md: the quote goes only to the LLM, the
     translation goes only to the rules.

  2. It is ADDITIVE, never a replacement. The Shona phrase lists stay exactly
     where they are. When DeepSeek is down there is no rendering to scan, and a
     resolver that had been leaning on the translation would go blind at
     precisely the moment the keyword net is the only thing left — which is the
     whole reason the keyword fallbacks exist.

Nothing here calls an API. `remember()` is fed by whoever ran the classifier;
readers get '' when the turn produced no rendering.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

# Bounded so a long-running gunicorn worker cannot grow one of these forever.
_MAX_ENTRIES = 512

_lock = threading.Lock()
_cache: "OrderedDict[str, str]" = OrderedDict()


def _key(message: str) -> str:
    return " ".join((message or "").lower().split())


def remember(message: str, english: str) -> None:
    """Store the English rendering of `message` for this turn's resolvers.

    A rendering identical to the message (already English) is stored as '' —
    there is nothing extra for a resolver to scan, and keeping it would just
    double every phrase check.
    """
    key = _key(message)
    if not key:
        return
    english = " ".join((english or "").split())
    if _key(english) == key:
        english = ""
    with _lock:
        _cache[key] = english
        _cache.move_to_end(key)
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)


def english_for(message: str) -> str:
    """The English rendering of `message`, or '' when there is none on file
    (DeepSeek was down, the message was already English, or this code path
    never ran the classifier)."""
    key = _key(message)
    if not key:
        return ""
    with _lock:
        english = _cache.get(key, "")
        if english:
            _cache.move_to_end(key)
    return english


def rule_texts(message: str) -> tuple:
    """The lower-cased texts a phrase-matching resolver should scan: what the
    customer actually wrote, plus its English rendering when one exists."""
    raw = (message or "").lower()
    english = english_for(message).lower()
    return (raw, english) if english else (raw,)


def contains_any(message: str, phrases) -> bool:
    """True when any phrase appears in the message OR in its English rendering.

    The drop-in replacement for `any(p in msg.lower() for p in PHRASES)`.
    """
    texts = rule_texts(message)
    return any(phrase in text for text in texts for phrase in phrases)


def contains_word_any(message: str, phrases) -> bool:
    """contains_any, but each phrase must match on WORD boundaries.

    Short tokens are unusable as bare substrings: 'no' matches inside "muno",
    "pano", "know", "phone" and "not", so a Shona lead writing "muno chaja
    seyi" (how do you charge here) read as declining. Use this for any list
    holding a word shorter than about five characters.
    """
    import re
    pattern = re.compile(
        r'\b(?:' + '|'.join(re.escape(p) for p in phrases if p) + r')\b'
    )
    return search_any(message, pattern)


def search_any(message: str, pattern) -> bool:
    """Compiled-regex equivalent of contains_any."""
    return any(pattern.search(text) for text in rule_texts(message))


def forget_all() -> None:
    """Drop everything — tests only, so one case cannot leak into the next."""
    with _lock:
        _cache.clear()
