import logging
import os
import time

from openai import OpenAI

logger = logging.getLogger(__name__)

# WhatsApp is sent exclusively through the Meta Cloud API, per tenant — see
# bot/whatsapp_cloud_api.get_client_for_tenant. Twilio was removed entirely;
# credentials now live on TenantWhatsAppChannel, not in env vars.
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')

GOOGLE_CALENDAR_CREDENTIALS = {}

deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1",
)

# ── Disable DeepSeek "thinking" mode on every call ───────────────────────────
# deepseek-v4-flash defaults to thinking mode: it emits chain-of-thought into a
# separate `reasoning_content` field BEFORE the answer. That reasoning consumes
# the max_tokens budget, so `content` comes back empty (finish_reason=length) on
# small-budget calls and JSON gets truncated on larger ones — which broke every
# classifier/extractor in the bot. We never read reasoning_content, so we turn
# thinking off for ALL calls here (covers all 11 call sites, current + future)
# while staying on the non-deprecated v4-flash model. Set DEEPSEEK_THINKING=
# enabled to re-enable. Docs: https://api-docs.deepseek.com/guides/thinking_mode
_DEEPSEEK_THINKING = os.environ.get("DEEPSEEK_THINKING", "disabled")
_orig_completions_create = deepseek_client.chat.completions.create


def _completions_create_no_thinking(*args, **kwargs):
    # Applies to the VISION model too. DeepSeek's pricing page says vision has
    # no thinking mode, but measured 2026-08-22 it emits 450-700 reasoning
    # tokens when left alone — which on a 150-token budget consumed the whole
    # allowance and returned empty content (finish_reason=length) for four of
    # five test photos. It accepts thinking:disabled and then answers in ~40
    # tokens. Trust the measurement, not the doc.
    extra = dict(kwargs.get("extra_body") or {})
    extra.setdefault("thinking", {"type": _DEEPSEEK_THINKING})
    kwargs["extra_body"] = extra
    return _orig_completions_create(*args, **kwargs)


try:
    deepseek_client.chat.completions.create = _completions_create_no_thinking
    logger.info("DeepSeek thinking mode set to '%s' for all calls", _DEEPSEEK_THINKING)
except Exception:  # pragma: no cover — SDK shape changed; calls still work, just with thinking on
    logger.warning("Could not install DeepSeek thinking-disable wrapper — calls keep default mode")



# The sampling dials for copy a CUSTOMER reads, as opposed to a classification
# the rule engine reads. Variety is the point: at low temperature the model
# reaches for the same opening every time, which is exactly what makes a reply
# read as canned, and a lead who gets four follow-ups notices. top_p keeps the
# looseness from wandering into nonsense and frequency_penalty discourages
# reusing the same phrasing.
#
# ONLY for free-text generation. Classifiers, extractors and any call whose
# output the rule engine parses stay at temperature 0 — a business fact must come
# back the same every time, and a JSON schema is not improved by creativity.
HUMAN_VOICE = {
    'temperature': 1.0,
    'top_p': 0.9,
    'frequency_penalty': 0.3,
}


def deepseek_call(
    messages,
    *,
    model=None,
    temperature=0.1,
    max_tokens=150,
    json_response=False,
    retries=3,
    timeout=15,
    top_p=None,
    frequency_penalty=None,
):
    """
    Wrapper around deepseek_client.chat.completions.create with:
      - Per-attempt timeout (default 15 s)
      - Retry with exponential backoff on failure or empty response
      - Treats empty content as a failure (retries instead of returning silently)

    `top_p` / `frequency_penalty` default to None and are omitted from the
    request entirely when unset, so every existing caller sends the exact same
    payload it did before. Pass **HUMAN_VOICE to opt a customer-facing generator
    in.

    Raises the last exception after all retries — callers keep their
    existing except blocks and fallback logic unchanged.
    """
    from django.conf import settings
    _model = model or getattr(settings, 'DEEPSEEK_MODEL', 'deepseek-v4-flash')

    last_exc = None
    for attempt in range(retries):
        try:
            kwargs = dict(
                model=_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            if json_response:
                kwargs['response_format'] = {'type': 'json_object'}
            if top_p is not None:
                kwargs['top_p'] = top_p
            if frequency_penalty is not None:
                kwargs['frequency_penalty'] = frequency_penalty

            resp = deepseek_client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            content = choice.message.content

            if not content or not content.strip():
                # Diagnostic: surface WHY it's empty so we can act —
                #   finish_reason='length'         → truncated (raise max_tokens)
                #   finish_reason='content_filter' → filtered
                #   finish_reason='stop' + completion_tokens≈0 → genuinely empty
                #                                    completion (DeepSeek-side degradation)
                #   reasoning_content present but content empty → reasoning model
                #                                    put everything in the wrong field
                finish    = getattr(choice, "finish_reason", "?")
                usage     = getattr(resp, "usage", None)
                reasoning = getattr(choice.message, "reasoning_content", None)
                usage_str = (
                    f"prompt={getattr(usage, 'prompt_tokens', '?')},"
                    f"completion={getattr(usage, 'completion_tokens', '?')}"
                    if usage else "n/a"
                )
                logger.warning(
                    "DeepSeek EMPTY content | model=%s finish_reason=%s usage=%s "
                    "reasoning_len=%s max_tokens=%s",
                    _model, finish, usage_str,
                    len(reasoning) if reasoning else 0, max_tokens,
                )
                raise ValueError(f"empty response from DeepSeek (finish_reason={finish})")

            return content.strip()

        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                wait = 2 ** attempt  # 1 s, then 2 s
                logger.warning(
                    "DeepSeek attempt %d/%d failed (%s) — retrying in %ds",
                    attempt + 1, retries, exc, wait,
                )
                time.sleep(wait)

    raise last_exc


def deepseek_detects_price_request(message: str):
    """
    DeepSeek-backed check for whether a customer message is asking about price /
    cost / a quote. Robust to spelling errors, abbreviations ("hw much"), and
    Shona/English mixing in a way a keyword list can't be.

    Returns:
        True / False  — the model's classification
        None          — DeepSeek unavailable or unparseable (caller should fall
                         back to keyword matching so detection never goes dark)
    """
    import json

    if not DEEPSEEK_API_KEY or not (message or '').strip():
        return None

    try:
        raw = deepseek_call(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You classify WhatsApp messages from plumbing customers. "
                        "Decide whether the customer is asking about price, cost, "
                        "a quote, or how much something costs. Account for typos, "
                        "abbreviations (e.g. 'hw much' = 'how much'), and mixed "
                        "English/Shona (e.g. 'marii', 'mutengo'). Reply with strict "
                        "JSON only, no prose."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f'Message: "{message}"\n\n'
                        'Is the customer asking about price/cost/a quote? '
                        'Respond ONLY as JSON: {"price_request": true} or '
                        '{"price_request": false}.'
                    ),
                },
            ],
            temperature=0,
            max_tokens=20,
            json_response=True,
            retries=1,   # fast gate check — fall back to keywords quickly on failure
            timeout=8,
        )
        return bool(json.loads(raw).get("price_request"))
    except Exception as exc:  # noqa: BLE001 — any failure → let caller fall back
        logger.warning("deepseek_detects_price_request failed (%s) — caller falls back", exc)
        return None


