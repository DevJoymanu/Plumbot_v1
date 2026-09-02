import os
import base64
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import connection
from django.templatetags.static import static
from django.utils import timezone


def _to_decimal(value, default='0.00'):
    """Convert API numeric inputs to Decimal safely."""
    if value in (None, ''):
        return Decimal(default)
    try:
        cleaned = (
            str(value).strip()
            .replace('US$', '')
            .replace('$', '')
            .replace(',', '')
            .replace(' ', '')
        )
        return Decimal(cleaned)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _to_float(value, default=0.0):
    """Safe float conversion using decimal normalizer."""
    try:
        return float(_to_decimal(value, default=str(default)))
    except Exception:
        return float(default)


def _safe_logo_url():
    """Return static logo URL without crashing when manifest entry is missing."""
    for path in ('images/logo.jpg', 'logo.jpg'):
        try:
            return static(path)
        except ValueError:
            continue
    return '/static/images/logo.jpg'


def _safe_logo_data_uri():
    """Return inline data URI for logo when static serving is unavailable."""
    logo_candidates = [
        os.path.join(settings.BASE_DIR, 'bot', 'static', 'images', 'logo.jpg'),
        os.path.join(settings.BASE_DIR, 'bot', 'static', 'logo.jpg'),
        os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.jpg'),
    ]
    logo_path = next((p for p in logo_candidates if os.path.exists(p)), None)
    if not logo_path:
        return ''
    try:
        with open(logo_path, 'rb') as f:
            encoded = base64.b64encode(f.read()).decode('ascii')
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return ''


def _reset_pk_sequence(model):
    """Reset Postgres PK sequence to current MAX(id) for a model table."""
    if connection.vendor != 'postgresql':
        return False
    table_name = model._meta.db_table
    pk_column = model._meta.pk.column
    quoted_table = connection.ops.quote_name(table_name)
    quoted_pk = connection.ops.quote_name(pk_column)
    sql = (
        f"SELECT setval(pg_get_serial_sequence('{table_name}', '{pk_column}'), "
        f"COALESCE(MAX({quoted_pk}), 1), true) FROM {quoted_table};"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql)
    return True


def business_name_for(obj, default='the plumbing team') -> str:
    """The business name to put in front of a customer, from this lead's OWN
    tenant.

    Accepts an Appointment, a Tenant, or None. Every customer-facing string and
    LLM prompt should route its company name through here — hardcoding
    "Homebase Plumbers" meant every other tenant's leads were greeted, signed
    off and followed up by a business they had never contacted.
    """
    tenant = getattr(obj, 'tenant', obj)
    return (getattr(tenant, 'name', '') or '').strip() or default


# The no-emoji house rule is enforced in the prompt AND here. A prompt rule is a
# request, not a guarantee: four generative prompts used to ASK for "one emoji
# max" and only one send path stripped what came back, so follow-ups, retry
# re-asks and repeat-question clarifications could each put an emoji in front of
# a customer. Belt and braces — the prompts now forbid it, and every free-text
# generator runs its output through here before the copy can be sent.
_EMOJI_RE = re.compile(
    '['
    '\U0001F000-\U0001FAFF'   # pictographs, symbols, transport, supplemental
    '\U00002600-\U000027BF'   # misc symbols + dingbats
    '\U0001F1E6-\U0001F1FF'   # regional indicators (flags)
    '\U00002B00-\U00002BFF'   # arrows/shapes
    '←-⇿'           # arrows — the test suite's own emoji set includes these
    '⌀-⏿'           # misc technical (⌚, ⏰)
    '️'                  # variation selector: turns a plain glyph into emoji
    ']'
)


def strip_emojis(text: str) -> str:
    """Remove emojis to honour the no-emoji house rule on customer-facing copy.

    Whitespace is re-collapsed because removing a glyph usually leaves a double
    space or a line ending in one; line breaks are preserved, since follow-up and
    confirmation copy is written in paragraphs.
    """
    if not text:
        return text
    cleaned = _EMOJI_RE.sub('', text)
    cleaned = re.sub(r'[^\S\n]+', ' ', cleaned)
    cleaned = re.sub(r' *\n', '\n', cleaned)
    return cleaned.strip()


# ── Dashes: the punctuation that makes copy read as written, not texted ─────
# Nobody types an em dash on a phone. A reply full of them reads as drafted
# prose, which is exactly the tell we do not want on WhatsApp, so every dash
# used as PUNCTUATION comes out of customer-facing copy.
#
# Hyphens INSIDE words stay: "on-site", "all-in", "wall-hung", "call-out",
# "20-minute" are how people actually write, and stripping them would produce
# "onsite" and "allin". The rule is about the dash that joins clauses, not the
# hyphen that joins words.

# A range: "8am - 6pm", "Sun–Fri", "8:00–18:00". Reads as "to" in speech, so
# that is what it becomes. Checked BEFORE the clause dash, since "Sun–Fri" has
# no spaces and would otherwise survive.
_RANGE_DASH_RE = re.compile(
    r'(?<=[\w.])\s*[—–-]\s*(?=[\w])'
    r'(?=(?:\d|Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))',
    re.IGNORECASE,
)

# A dash joining clauses: em, en, or a hyphen with a space on at least one
# side. "US$20 - free if we do the job", "the visit is free — we come round".
_CLAUSE_DASH_RE = re.compile(r'\s*[—–]\s*|\s+-{1,2}\s+|\s+-(?=\s)|(?<=\s)-{2,}\s*')

# A dash opening a line is a bullet, not punctuation. It keeps its job as a
# list marker but stops being a dash.
_BULLET_DASH_RE = re.compile(r'^([ \t]*)[—–-]{1,2}[ \t]+', re.MULTILINE)

# What follows the dash decides the replacement. A clause that stands on its
# own becomes its own sentence; a fragment hangs off a comma.
_INDEPENDENT_RE = re.compile(
    r'^(?:and\s+|so\s+)?(?:we|i|you|they|he|she|it|there|here|now|then|'
    # An interrogative always opens a sentence of its own.
    r'what|when|where|which|how|who|why|shall|would|could|'
    r'ndeipi|munoda|unoda|mungada|chii|rinhi|'
    r'that|this|these|those|the|our|your|my|his|her|its|no|yes|just|'
    r'nothing|everything|ndi|ti|va|mu|zvi|hapana|hongu|kwete)\b',
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r'[.!?]["\')\]]?$')


def _dash_replacement(before: str, after: str) -> str:
    """Comma or full stop, whichever the two halves actually want."""
    if not after:
        return ''
    before = before.rstrip()
    if not before or _SENTENCE_END_RE.search(before):
        return ' '
    # A short interjection wants its own stop: "Perfect - thanks, Tendai"
    # reads as "Perfect. Thanks, Tendai", never "Perfect, thanks, Tendai".
    if len(before.split()) <= 3 and not before.endswith(','):
        return '. '
    # A trailing fragment with no verb of its own hangs off a comma; a clause
    # that could stand alone gets to be its own sentence.
    if _INDEPENDENT_RE.match(after.lstrip()) and len(after.split()) > 2:
        return '. '
    return ', '


def strip_dashes(text: str) -> str:
    """Take the dash punctuation out of customer-facing copy.

    Ranges become "to", bullet leaders become a bullet, and a clause dash
    becomes the comma or full stop the sentence was reaching for. Intra-word
    hyphens are left exactly as they are.
    """
    if not text:
        return text
    out = _BULLET_DASH_RE.sub(r'\1• ', text)
    out = _RANGE_DASH_RE.sub(' to ', out)

    # Walk the clause dashes so each replacement can see its own context.
    result = []
    pos = 0
    for m in _CLAUSE_DASH_RE.finditer(out):
        before = out[pos:m.start()]
        after = out[m.end():]
        # A line break already does the dash's job; don't add punctuation.
        if '\n' in m.group(0):
            result.append(before)
            result.append(m.group(0))
            pos = m.end()
            continue
        rep = _dash_replacement(''.join(result) + before, after)
        result.append(before)
        if rep == '. ':
            # The new sentence has to start like one.
            tail = after.lstrip()
            result.append('. ')
            out = out[:m.end()] + tail[:1].upper() + tail[1:]
            pos = m.end()
            continue
        result.append(rep)
        pos = m.end()
    result.append(out[pos:])
    cleaned = ''.join(result)

    # Removing a dash can leave doubled punctuation or a space before it.
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    cleaned = re.sub(r'[ \t]+([.,!?;:])', r'\1', cleaned)
    cleaned = re.sub(r',\s*([.!?])', r'\1', cleaned)
    cleaned = re.sub(r'\.\s*\.', '.', cleaned)
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
    return cleaned.strip()


def _append_admin_note(appointment, message):
    timestamp = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M')
    existing = appointment.admin_notes or ''
    appointment.admin_notes = f"[{timestamp}] {message}\n{existing}".strip()
    appointment.save(update_fields=['admin_notes'])


def clean_phone_number(phone):
    """Convert phone number to WhatsApp Cloud API format (no prefix, no +)."""
    return phone.replace('whatsapp:', '').replace('+', '').strip()


def format_phone_number_for_storage(phone):
    """Format phone number for database storage with whatsapp: prefix."""
    if not phone.startswith('whatsapp:'):
        return f"whatsapp:+{phone}"
    return phone
