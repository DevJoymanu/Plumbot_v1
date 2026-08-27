"""
Turn a customer's photo into one line of text.

Design note (docs/VISION_PLAN.md): this is the ONLY multimodal call in the bot.
The image becomes text once, here, and every downstream consumer — the unified
classifier, the deterministic intent resolvers, the pricing gates,
generate_response — keeps working on text it already understands. Nothing else
in the codebase learns that images exist.

Deliberately prose, not JSON: `response_format={"type": "json_object"}` is not
documented as supported on the vision model, and structured extraction is
better done by the existing text resolvers anyway.
"""

import base64
import logging

logger = logging.getLogger(__name__)

# The vision model accepts these and nothing else. A PDF plan is the upload that
# most looks like it should work and does not — it must never reach this module.
VISION_IMAGE_MIMES = {
    'image/jpeg', 'image/jpg', 'image/png', 'image/webp', 'image/gif',
}

VISION_MODEL = 'deepseek-v4-flash-vision-exp'

# Images may appear only in `user` messages — an image in a system or assistant
# message is a 400 — so the instruction rides along with the image itself.
_INSTRUCTION = (
    "This photo was sent by a customer to a plumbing company on WhatsApp. "
    "In one or two plain sentences, say what plumbing fixtures or fittings are "
    "visible and their condition if anything is obviously wrong. Name fixtures "
    "in ordinary words a plumber would use: bath, corner bath, freestanding "
    "bath, shower cubicle, shower tray, toilet, wall-hung toilet, basin, "
    "vanity, geyser, tap, mixer, pipe. If it is a drawing or floor plan rather "
    "than a photo, say so. If there is no plumbing in it, say so plainly. "
    "Describe only what you can see. Do not guess, do not price anything, and "
    "do not address the customer."
)


# The company's OWN previous-work photo, not a customer's. Different job from
# _INSTRUCTION: nothing is wrong with this installation, and the description is
# what a customer's "this one, how much?" gets classified against — so it needs
# the fixtures named, not an assessment.
_PORTFOLIO_INSTRUCTION = (
    "This is a plumbing company's own photo of work they have completed, shown "
    "to customers as an example. In one or two plain sentences, name the "
    "plumbing fixtures, fittings or installation visible, in ordinary words a "
    "plumber would use: bath, corner bath, freestanding bath, shower cubicle, "
    "shower tray, toilet, wall-hung toilet, basin, vanity, geyser, tap, mixer, "
    "pipe, borehole pump, pressure tank, storage tank. Describe only what you "
    "can see. Do not guess, do not price anything, do not praise the work, and "
    "do not address anyone."
)


def describe_portfolio_image(file_bytes, mime_type, tenant=None):
    """One-line description of the tenant's OWN previous-work photo, or None.

    Same contract as describe_customer_image: never raises, None means "we did
    not see it". Run once when a photo is added to the gallery, not per send.
    """
    return _describe(file_bytes, mime_type, _PORTFOLIO_INSTRUCTION,
                     log_label="a portfolio image")


def describe_customer_image(file_bytes, mime_type, tenant=None):
    """
    Return a one-line description of a customer's photo, or None.

    Returns None — never raises — on an unsupported format, a missing key, or
    any API failure. Callers must treat None as "we did not see the image" and
    fall back to their existing behaviour: vision is additive and must never be
    able to break the media path.
    """
    return _describe(file_bytes, mime_type, _INSTRUCTION,
                     log_label="a customer image")


def _describe(file_bytes, mime_type, instruction, log_label):
    """Shared single multimodal call. See the module docstring."""
    if not file_bytes:
        return None

    mime = (mime_type or '').lower().split(';')[0].strip()
    if mime not in VISION_IMAGE_MIMES:
        logger.info("Vision skipped — %s is not a supported image type", mime or '?')
        return None

    try:
        from django.conf import settings
        if not getattr(settings, 'DEEPSEEK_API_KEY', ''):
            return None

        b64 = base64.b64encode(file_bytes).decode('ascii')
        from .clients import deepseek_call
        description = deepseek_call(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {
                        "type": "image_url",
                        # detail=low downscales to 512x512: cheaper and faster,
                        # and identifying a fixture does not need full res.
                        "image_url": {
                            "url": f"data:{mime};base64,{b64}",
                            "detail": "low",
                        },
                    },
                ],
            }],
            model=VISION_MODEL,
            temperature=0,
            max_tokens=150,
            retries=2,
            timeout=20,
        )
    except Exception as exc:
        logger.warning("Vision describe failed (%s) — continuing without it", exc)
        return None

    description = (description or '').strip().replace('**', '')
    if not description:
        return None

    # Internal metadata, not copy: this is never sent to the customer, so it is
    # only ever read by classifiers and shown to the plumber.
    logger.info("Vision described %s: %s", log_label, description[:120])
    return description
