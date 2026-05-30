import logging
import os
import time

from twilio.rest import Client
from openai import OpenAI

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')
# Backward-compatible aliases used in older code paths.
ACCOUNT_SID = TWILIO_ACCOUNT_SID
AUTH_TOKEN = TWILIO_AUTH_TOKEN

GOOGLE_CALENDAR_CREDENTIALS = {}

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1",
)



def deepseek_call(
    messages,
    *,
    model=None,
    temperature=0.1,
    max_tokens=150,
    json_response=False,
    retries=3,
    timeout=15,
):
    """
    Wrapper around deepseek_client.chat.completions.create with:
      - Per-attempt timeout (default 15 s)
      - Retry with exponential backoff on failure or empty response
      - Treats empty content as a failure (retries instead of returning silently)

    Raises the last exception after all retries — callers keep their
    existing except blocks and fallback logic unchanged.
    """
    from django.conf import settings
    _model = model or getattr(settings, 'DEEPSEEK_MODEL', 'deepseek-chat')

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

            resp = deepseek_client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content

            if not content or not content.strip():
                raise ValueError("empty response from DeepSeek")

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


