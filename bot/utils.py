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


# ── WE, never "the plumber" (owner rule, restated 2026-09-18) ────────────────
# The business speaks as one: "once we see the space", "we'll come through",
# never "once the plumber sees the space" or "our plumber will come". The
# owner had asked for this before and it did not stick, because it lived only
# in the copy, and every new string or model reply brought a third party back.
# So, like strip_dashes, it is enforced on the FINAL text at the outbound
# choke points (finalise_outbound, the transcript log, delayed_response, the
# cron copy) and pinned by TEST 0. Source copy should still say "we"; this is
# the net under it, not a licence to write "the plumber".
_WE_PLUMBER_RE = re.compile(
    r"\b(the|our|your)\s+plumber(s)?\b(\s*['’]s)?", re.IGNORECASE)
# After these words "the plumber" is the OBJECT: "meet the plumber" -> "meet
# us", "speak with the plumber" -> "speak with us".
_WE_OBJECT_BEFORE_RE = re.compile(
    r"\b(to|with|for|from|by|meet|call|contact|reach|ask|tell|let|about|of|at"
    r"|see|phone|message|text|email|hear)\s*$", re.IGNORECASE)
_WE_ADVERBS = frozenset({
    'always', 'also', 'usually', 'normally', 'then', 'just', 'still', 'only',
    'first', 'now', 'typically', 'generally', 'personally', 'directly',
    'actually', 'often', 'sometimes',
})
_WE_IRREGULAR = {'is': 'are', 'was': 'were', 'has': 'have', 'does': 'do',
                 'goes': 'go', 'needs': 'need', "isn't": "aren't",
                 "wasn't": "weren't", "hasn't": "haven't", "doesn't": "don't"}
# Words ending in "s" that are not a third-person verb.
_WE_NOT_VERBS = frozenset({
    'this', 'his', 'its', 'as', 'us', 'plus', 'yes', 'perhaps', 'across',
    'various', 'previous', 'serious', 'always', 'sometimes', 'is',
})


def _we_verb(word: str) -> str:
    """'sees' -> 'see', 'is' -> 'are', 'will' -> 'will'."""
    low = word.lower()
    if low in _WE_IRREGULAR:
        return _WE_IRREGULAR[low]
    if low in _WE_NOT_VERBS or not low.endswith('s') or low.endswith('ss'):
        return word
    if low.endswith('ies') and len(low) > 4:
        return word[:-3] + 'y'
    if low.endswith(('sses', 'shes', 'ches', 'xes', 'zes', 'oes')):
        return word[:-2]
    return word[:-1]


def speak_as_we(text: str) -> str:
    """Rewrite "the plumber" / "our plumber" / "your plumber" as WE.

    Subject -> "we" with its verb agreed ("the plumber sees" -> "we see",
    "our plumber is" -> "we are"); object -> "us" ("meet the plumber" -> "meet
    us"); possessive -> "our". Two shapes are left alone on purpose: a NAME in
    apposition ("our plumber Takudzwa", the answer to a lead who asked who is
    coming) and "the plumber's name", which only ever answers that question.
    "a plumber" is generic ("still looking for a plumber?") and untouched.
    Idempotent, so the choke points can all apply it.
    """
    if not text or 'plumber' not in text.lower():
        return text
    out = []
    pos = 0
    for m in _WE_PLUMBER_RE.finditer(text):
        if m.start() < pos:
            continue
        before = text[:m.start()]
        after = text[m.end():]
        det = m.group(1)
        cap = det[:1].isupper() and (not before.strip()
                                     or before.rstrip()[-1:] in '.!?\n')
        possessive = bool(m.group(3))
        name = re.match(r"\s+([A-Z][a-z]+)\b", after)
        if (possessive and re.match(r"\s*name\b", after, re.IGNORECASE)) or (
                not possessive and name and name.group(1).lower() not in _WE_ADVERBS
                and name.group(1) not in ('I', 'We')):
            continue
        out.append(text[pos:m.start()])
        if possessive:
            out.append('Our' if cap else 'our')
            pos = m.end()
            continue
        if _WE_OBJECT_BEFORE_RE.search(before):
            out.append('us')
            pos = m.end()
            continue
        out.append('We' if cap else 'we')
        pos = m.end()
        # Agree the verb: skip adverbs ("the plumber usually comes").
        words = re.match(r"((?:\s+(?:%s))*)(\s+)([A-Za-z']+)" % '|'.join(_WE_ADVERBS),
                         text[pos:], re.IGNORECASE)
        if words:
            out.append(words.group(1) + words.group(2) + _we_verb(words.group(3)))
            pos += words.end()
        # The rest of the SENTENCE still points back at the plumber: "our
        # plumber will lock in your price when they come out" must become
        # "...when we come out", not keep a third party in the same breath.
        end = re.search(r'[.!?\n]', text[pos:])
        stop = pos + end.start() if end else len(text)
        tail = re.sub(r"\b(he|she)['’]s\b", "we're", text[pos:stop])
        out.append(re.sub(r"\b(he|she|they)\b", 'we', tail))
        pos = stop
    out.append(text[pos:])
    return ''.join(out)


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


# The house rule is ONE question per message: people answer the last one and
# the earlier one is wasted. Two questions usually arrive the same way — a
# reply that already asks something, with a short tie-down bolted on the end
# ("Sharp?", "That sit alright?"), because the tie-down is appended by a
# different code path than the question and neither can see the other.
#
# So the rule is narrow on purpose: drop a TRAILING SHORT question, and only
# when a real question already stands in front of it. Length is what separates
# a bolted-on tie-down from a genuine ask — an either/or clarifier ("you're not
# in a specific area? Or did you mean you're not sure yet?") is two questions
# by punctuation but one question by intent, and it survives because its second
# half is too long to look like a tie-down.
_TIEDOWN_MAX_WORDS = 4

# A sentence ends at . ! or ? — kept as a capture so the text can be rebuilt
# exactly, since anything else would quietly reflow the copy.
_SENTENCE_SPLIT = re.compile(r'([.!?]+)(\s+|$)')


def _sentences(text: str):
    """[(body, terminator, trailing space), ...] covering the whole string."""
    out, pos = [], 0
    for m in _SENTENCE_SPLIT.finditer(text):
        out.append((text[pos:m.start()], m.group(1), m.group(2)))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], '', ''))
    return out


def enforce_single_question(text: str) -> str:
    """Drop a trailing tie-down question when the message already asks one."""
    if not text or text.count('?') < 2:
        return text
    parts = _sentences(text)
    if len(parts) < 2:
        return text
    body, term, _space = parts[-1]
    if '?' not in term:
        return text                      # does not end on a question
    if len(body.split()) > _TIEDOWN_MAX_WORDS:
        return text                      # a real ask, not a tie-down
    if not any('?' in t for _b, t, _s in parts[:-1]):
        return text                      # nothing in front of it
    kept = ''.join(b + t + s for b, t, s in parts[:-1])
    return kept.rstrip() or text
