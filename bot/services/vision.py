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

# From settings (DEEPSEEK_VISION_MODEL, default 'deepseek-flash'). It was the
# hardcoded 'deepseek-v4-flash-vision-exp', a name DeepSeek has retired and
# now serves through V4.1 Flash; read at call time so an env change needs no
# deploy.
def _vision_model():
    from django.conf import settings
    return getattr(settings, 'DEEPSEEK_VISION_MODEL', '') or 'deepseek-flash'

# Images may appear only in `user` messages — an image in a system or assistant
# message is a 400 — so the instruction rides along with the image itself.
_INSTRUCTION = (
    "This photo was sent by a customer to a plumbing company on WhatsApp. "
    "In one or two plain sentences, say what plumbing fixtures or fittings are "
    "visible and their condition if anything is obviously wrong. Name fixtures "
    "in ordinary words a plumber would use: bath, corner bath, freestanding "
    "bath, shower cubicle, shower tray, toilet, wall-hung toilet, basin, "
    "vanity, geyser, tap, mixer, pipe. If it is a drawing or floor plan rather "
    "than a photo, say so. If it is a handwritten or printed list of materials "
    "or items, say it is a written materials list and do not read out its "
    "lines. If there is no plumbing in it, say so plainly. "
    "Describe only what you can see. Do not guess, do not price anything, and "
    "do not address the customer."
)

# A written materials list, read line by line. This is the ONE case that gets a
# second call per image: the describe call runs at detail=low (512x512), which
# is enough to see that a page is a list and nowhere near enough to read forty
# handwritten lines, and the priced reply needs every one of them. Only an
# image the first call already called a list pays for this.
_LIST_INSTRUCTION = (
    "This is a customer's handwritten or printed list of plumbing materials. "
    "Transcribe it. One item per line, written as: <quantity> x <item>. Keep "
    "every size exactly as written (15mm, 22 x 15mm, 3/4). Write 'cu' as "
    "copper. Correct obvious spelling mistakes, but never change a quantity or "
    "a size. Keep brand notes in brackets as written, e.g. (cap). A heading on "
    "the page goes on its own line starting with #. Output only the lines, "
    "nothing else."
)


# The company's OWN previous-work photo, not a customer's. Different job from
# _INSTRUCTION: nothing is wrong with this installation, and the description is
# what a customer's "this one, how much?" gets classified against — so it needs
# the fixtures named, not an assessment.
_PORTFOLIO_INSTRUCTION = (
    "This is a plumbing company's own photo of work they have completed, shown "
    "to customers as an example.\n"
    "Reply with exactly two lines and nothing else.\n"
    "Line 1: a short name for the main job shown, two to four words, no "
    "punctuation — for example 'Freestanding tub', 'Shower cubicle', "
    "'Borehole pump', 'Vanity unit', 'Geyser install'.\n"
    "Line 2: one or two plain sentences naming the plumbing fixtures, fittings "
    "or installation visible, in ordinary words a plumber would use: bath, "
    "corner bath, freestanding bath, shower cubicle, shower tray, toilet, "
    "wall-hung toilet, basin, vanity, geyser, tap, mixer, pipe, borehole pump, "
    "pressure tank, storage tank.\n"
    "Describe only what you can see. Do not guess, do not price anything, do "
    "not praise the work, and do not address anyone."
)

# A label longer than this is prose, not a name — the model ignored line 1.
MAX_VISION_LABEL_CHARS = 40


def describe_portfolio_image(file_bytes, mime_type, tenant=None):
    """Look at the tenant's OWN previous-work photo.

    Returns (label, description) — a short name for the job and one or two
    sentences about it — or (None, None). The label matters because a tenant
    may upload a photo WITHOUT naming it, and then vision is the only thing
    that knows what the photo is: it becomes the row's title, which is what a
    customer's quoted-photo question resolves against and what the price
    lookup matches on.

    Same contract as describe_customer_image otherwise: never raises.
    """
    raw = _describe(file_bytes, mime_type, _PORTFOLIO_INSTRUCTION,
                    log_label="a portfolio image")
    if not raw:
        return None, None
    lines = [ln.strip(' .-*#') for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return None, None
    label = lines[0]
    description = ' '.join(lines[1:]).strip() or label
    # The model sometimes answers in one prose block, ignoring the two-line
    # shape. Then there is no name to trust — keep the prose as the description
    # and let the caller fall back rather than titling a row with a sentence.
    if len(label) > MAX_VISION_LABEL_CHARS or len(lines) == 1:
        return None, raw.strip()
    return label, description


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


def transcribe_materials_list(file_bytes, mime_type, tenant=None):
    """The lines of a written materials list, or [] when it cannot be read.

    Same contract as describe_customer_image: never raises, and an empty
    result means the caller carries on without it.
    """
    raw = _describe(file_bytes, mime_type, _LIST_INSTRUCTION,
                    log_label="a materials list", detail="high",
                    max_tokens=1500, timeout=45)
    if not raw:
        return []
    lines = []
    for line in raw.splitlines():
        line = line.strip().strip('`').strip()
        if line and not line.lower().startswith(('here is', "here's", 'sure')):
            lines.append(line)
    return lines


def _describe(file_bytes, mime_type, instruction, log_label,
              detail="low", max_tokens=150, timeout=20):
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
                            "detail": detail,
                        },
                    },
                ],
            }],
            model=_vision_model(),
            temperature=0,
            max_tokens=max_tokens,
            retries=2,
            timeout=timeout,
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
