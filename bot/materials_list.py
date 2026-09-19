"""
bot/materials_list.py
=====================
A customer's written materials list, priced from the business's OWN figures.

Leads photograph the list their builder or plumber wrote out ("3x 22mm cu
pipes, 60x 15mm cu elbows (cap), 3x wash hand basins...") and ask what it
comes to. The fixture price list cannot answer that: it knows a basin, never a
15mm female elbow. The figures that CAN answer it are already in the system,
typed by the business itself: the unit prices on its own quotes and quote
templates. So every figure here is one the tenant set, and a line nothing
matches is said to be priced on the quote, never guessed (owner decision,
2026-09-18: "tenant's own quotes"; the sales profile forbids inventing one).

Four parts, all deterministic except the transcription:
  * `looks_like_materials_list` reads vision's one-line description;
  * `parse_line` / `list_lines_in_history` turn the transcription into lines;
  * `price_list` matches each line against `tenant_price_book`;
  * `build_list_price_reply` writes the WhatsApp reply.

Matching is strict on purpose. A 15mm elbow priced at the 22mm rate, or a
female coupling at the male one, is a wrong number the customer will hold us
to, while a line left for the quote costs nothing. So sizes must agree
exactly, the item nouns must agree exactly, and a conflicting qualifier
(male/female, copper/pvc) rejects the match outright.

Nothing here volunteers a price: the webhook only calls the reply builder when
the customer asked for a figure (never-volunteer-price rule).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# Vision's one-line description is asked to say "written materials list" when
# it sees one (services/vision._INSTRUCTION). Read deterministically.
_LIST_RE = re.compile(
    r'\b(?:(?:materials?|shopping|parts?|items?|written|handwritten|hand-written)'
    r'\s+(?:\w+\s+){0,2}list|bill of quantities|list of (?:plumbing )?'
    r'(?:materials?|items?|parts?|fittings?))\b')


def looks_like_materials_list(description: str) -> bool:
    return bool(_LIST_RE.search((description or '').lower()))


# ── Parsing ──────────────────────────────────────────────────────────────────

# The lookahead keeps a SIZE from being read as a quantity: "22mm copper
# pipe" is one pipe, not 22 of "mm copper pipe".
_QTY_RE = re.compile(
    r'^\s*(\d+(?:\.\d+)?)(?!\s*(?:mm|/|"|in\b|inch|\d))\s*'
    r'(x|×|\*|pairs?(?:\s+of)?|kgs?|kilos?|lengths?|ltrs?|litres?|rolls?|m\b)?'
    r'\s*(.+?)\s*$', re.IGNORECASE)

_UNIT_WORDS = {
    'pair': 'pair', 'pairs': 'pair', 'pair of': 'pair', 'pairs of': 'pair',
    'kg': 'kg', 'kgs': 'kg', 'kilo': 'kg', 'kilos': 'kg',
    'length': 'length', 'lengths': 'length',
    'ltr': 'litre', 'ltrs': 'litre', 'litre': 'litre', 'litres': 'litre',
    'roll': 'roll', 'rolls': 'roll', 'm': 'm',
}


def parse_line(raw: str):
    """'6 x 22 x 15mm copper reducers' -> (Decimal(6), '', '22 x 15mm copper reducers').

    None for a heading or an empty line. A line with no leading number is one
    of it: people write "Silicone sealant" and mean one tube.
    """
    text = (raw or '').strip().lstrip('-*• ').strip()
    if not text or text.startswith('#'):
        return None
    m = _QTY_RE.match(text)
    if m and m.group(3):
        try:
            qty = Decimal(m.group(1))
        except InvalidOperation:
            qty = Decimal(1)
        unit = _UNIT_WORDS.get((m.group(2) or '').lower().strip(), '')
        item = m.group(3).strip()
        # "3x3/4 brass stop cocks": the number after the x is a SIZE, and the
        # regex must not have eaten it as part of the quantity.
        return (qty if qty > 0 else Decimal(1)), unit, item
    return Decimal(1), '', text


def heading_of(raw: str) -> str:
    text = (raw or '').strip()
    return text.lstrip('#').strip() if text.startswith('#') else ''


# ── Normalising an item for matching ────────────────────────────────────────

# The nouns that say WHAT a line is. Two lines match only when these agree.
_HEAD_NOUNS = frozenset({
    'pipe', 'elbow', 'tee', 'coupling', 'reducer', 'adaptor', 'bend',
    'socket', 'union', 'cock', 'valve', 'mixer', 'basin', 'tub', 'bath',
    'sink', 'trap', 'waste', 'connector', 'pedestal', 'cistern', 'toilet',
    'tank', 'sealant', 'putty', 'rose', 'arm', 'combination', 'bolt', 'wire',
    'flux', 'comb', 'tap', 'geyser', 'cubicle', 'vanity', 'chamber',
    'clip', 'bracket', 'tape', 'glue', 'cement', 'plug', 'seat', 'hose',
    'nipple', 'bush', 'shower', 'solder', 'gutter', 'pump', 'drain',
    # A close-coupled toilet is written "close couple set" on lists and
    # "water closet close couple" on quotes: the item word is 'coupled'.
    # Without it neither side had a noun, and the plumber's own quoted price
    # lost to the fixture price list.
    'coupled',
    # NOT 'cap': on these lists "(cap)" is the fitting's BRAND ("15mm cu
    # elbows (cap)"), and as a head noun it would stop every such line
    # matching the business's plain "15mm copper elbow".
})
# Qualifiers that make two otherwise identical lines DIFFERENT items.
# STRICT groups must agree even when one side says nothing: a "22mm female
# elbow" is a threaded fitting, not the plain copper elbow, and pricing it at
# the plain rate is a wrong number. A MATERIAL left unsaid is only unsaid, so
# it rejects only when both sides name one and they differ.
_STRICT_GROUPS = (
    frozenset({'male', 'female'}),
    frozenset({'single', 'double'}),
)
_MATERIAL_GROUP = frozenset({'copper', 'pvc', 'upvc', 'brass', 'galvanised',
                             'pex', 'hdpe', 'polycop', 'chrome', 'plastic'})
_SYNONYMS = {
    'cu': 'copper', 'adapter': 'adaptor', 'adapters': 'adaptor',
    'galvanized': 'galvanised', 'soldering': 'solder', 'bathtub': 'tub',
    'faucet': 'tap', 'stopcock': 'cock', 'couple': 'coupled',
    'coupler': 'coupling', 'reticulation': '',
    # The plumber's own shorthand, read off Barmak's sent quotes (2026-09-18):
    # a bath IS a tub on every line that names one, "silicone" is the
    # sealant, and "chasing com" is the quote's own spelling of comb.
    'bath': 'tub', 'silicone': 'sealant', 'com': 'comb',
}
# Trade abbreviations spelled out before tokenising: c.f.i / c.m.i are copper
# x female iron / copper x male iron fittings, so "15mm c.f.i elbow" IS the
# list's "15mm female elbow".
_ABBREVIATIONS = (
    # Word-bounded and space-free: a looser pattern reads "basic fitting" as
    # "basi c f i tting" and "basic mixer" as a male fitting.
    (re.compile(r'\bc\.?f\.?i\b'), ' copper female '),
    (re.compile(r'\bc\.?m\.?i\b'), ' copper male '),
)
_STOPWORDS = frozenset({'and', 'the', 'for', 'with', 'of', 'a', 'an', 'set',
                        'sets', 'hole', 'hand', 'wash', 'type', 'size', 'x'})


def _singular(word: str) -> str:
    if len(word) <= 3 or word.endswith('ss'):
        return word
    if word.endswith('ies'):
        return word[:-3] + 'y'
    if word.endswith(('ches', 'shes', 'xes', 'sses')):
        return word[:-2]
    if word.endswith('s'):
        return word[:-1]
    return word


def _sizes(text: str) -> frozenset:
    """Every size on the line, normalised: '22 x 15mm' -> {22mm, 15mm}."""
    low = text.lower()
    found = set()
    for a, b in re.findall(r'(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)', low):
        found.update({f'{a}mm', f'{b}mm'})
    for n in re.findall(r'(\d+(?:\.\d+)?)\s*mm\b', low):
        found.add(f'{n}mm')
    for f in re.findall(r'\b(\d+/\d+)', low):
        found.add(f'{f}in')
    return frozenset(found)


def item_signature(item: str):
    """(sizes, head nouns, all tokens) for one item description."""
    low = (item or '').lower()
    for pattern, spelled in _ABBREVIATIONS:
        low = pattern.sub(spelled, low)
    sizes = _sizes(low)
    stripped = re.sub(r'\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?\s*(?:mm)?', ' ', low)
    stripped = re.sub(r'\d+(?:[./]\d+)?\s*(?:mm|in|m|")?', ' ', stripped)
    tokens = set()
    for raw in re.findall(r"[a-z]+", stripped):
        word = _SYNONYMS.get(raw, raw)
        word = _SYNONYMS.get(_singular(word), _singular(word))
        if word and word not in _STOPWORDS and len(word) > 1:
            tokens.add(word)
    # Words that name the same item from another angle:
    #   a VSP is an adaptor ("25mm vsp" on the quote, "VSP adaptors" on lists);
    #   a combination IS the trap ("tub combination p-trap" = "bath combination");
    #   flux is flux whether or not it says it is for soldering.
    if 'vsp' in tokens:
        tokens.add('adaptor')
    if 'combination' in tokens:
        tokens -= {'trap', 'p'}
    if 'flux' in tokens:
        tokens.discard('solder')
    heads = frozenset(t for t in tokens if t in _HEAD_NOUNS)
    return sizes, heads, frozenset(tokens)


# Words that never tell two items apart: brand notes and filler.
_NEUTRAL_MODIFIERS = frozenset({'cap', 'pre', 'clear', 'type', 'standard',
                                'ordinary', 'normal', 'paste'})


def _conflicts(a: frozenset, b: frozenset) -> bool:
    for group in _STRICT_GROUPS:
        if (a & group) != (b & group):
            return True
    ma, mb = a & _MATERIAL_GROUP, b & _MATERIAL_GROUP
    if ma and mb and ma != mb:
        return True
    # Both sides say WHICH kind and share none of it: a "float valve" is not
    # the business's "angle valve", though both are valves (live Barmak run,
    # 2026-09-18, priced one as the other). One side silent is fine: "basin
    # mixers" still takes the business's "basin mixer pillar type".
    skip = _HEAD_NOUNS | _MATERIAL_GROUP | _NEUTRAL_MODIFIERS
    for group in _STRICT_GROUPS:
        skip = skip | group
    da, db = a - skip, b - skip
    return bool(da and db and not (da & db))


def match_score(line_item: str, candidate: str, ignore_size: bool = False) -> float:
    """0 when the candidate is a different item, else token overlap (0..1]."""
    ls, lh, lt = item_signature(line_item)
    cs, ch, ct = item_signature(candidate)
    if not lh or lh != ch or (ls != cs and not ignore_size) or _conflicts(lt, ct):
        return 0.0
    return len(lt & ct) / max(1, len(lt | ct))


# ── The business's own prices ───────────────────────────────────────────────

# Fixtures the tenant's price list knows, for lines that name the fixture
# itself. Accessory words mean the line is PART of a fixture ("basin mixers",
# "bath wastes"), which the fixture price does not cover.
_FIXTURE_FAMILIES = (
    ('basin', {'basin'}), ('tub', {'tub', 'bath'}), ('toilet', {'toilet', 'coupled'}),
    ('geyser', {'geyser'}), ('vanity', {'vanity'}), ('chamber', {'chamber'}),
    ('shower', {'cubicle'}), ('sink', {'sink'}),
)
_ACCESSORY_WORDS = frozenset({
    'mixer', 'waste', 'trap', 'rose', 'arm', 'combination', 'connector',
    'valve', 'bolt', 'pedestal', 'seat', 'cistern', 'tap', 'plug', 'hose',
})


def fixture_family(item: str):
    _s, _h, tokens = item_signature(item)
    if tokens & _ACCESSORY_WORDS:
        return None
    for family, words in _FIXTURE_FAMILIES:
        if tokens & words:
            return family
    return None


def tenant_price_book(tenant) -> list:
    """[(description, unit_price, source)] from the tenant's OWN documents.

    Newest quote lines first, because that is the figure the business charged
    most recently; then its own templates. Global templates are excluded: the
    operator wrote them, so their prices are not this business's.
    """
    if tenant is None:
        return []
    book = []
    try:
        from bot.models import QuotationItem, QuotationTemplateItem
        for desc, price in (QuotationItem.objects
                            .filter(quotation__tenant=tenant, unit_price__gt=0)
                            .order_by('-quotation__created_at', '-id')
                            .values_list('description', 'unit_price')[:2000]):
            book.append((str(desc or ''), Decimal(price), 'quote'))
        for desc, price in (QuotationTemplateItem.objects
                            .filter(template__tenant=tenant,
                                    template__is_global=False,
                                    unit_price__gt=0)
                            .order_by('-id')
                            .values_list('description', 'unit_price')[:2000]):
            book.append((str(desc or ''), Decimal(price), 'template'))
    except Exception:
        logger.warning('Could not read the tenant price book', exc_info=True)
    return book


def _price_row(tenant_cfg, family):
    if tenant_cfg is None or not family:
        return None
    try:
        return tenant_cfg.price_item(family)
    except Exception:
        return None


def _size_silent_price(item: str, book: list):
    """A line that names NO size ("basin wastes") takes the business's sized
    line ("32mm basin waste outlet") only when every such line agrees on the
    price. Two different sizes at two different prices is a question for the
    quote, not a figure to pick between."""
    if _sizes((item or '').lower()):
        return None
    prices = set()
    for desc, price, _src in book:
        if _sizes((desc or '').lower()) and match_score(item, desc, ignore_size=True) > 0:
            prices.add(Decimal(price))
    return prices.pop() if len(prices) == 1 else None


def price_list(lines: list, book: list, tenant_cfg=None) -> dict:
    """Price each list line from the business's own figures.

    Returns {'rows': [...], 'unpriced': [...], 'materials_total': Decimal,
    'labour': [...], 'labour_total': Decimal}. A row is (qty, unit, item,
    unit_price, line_total); a labour entry is (qty, item, labour_each).
    """
    rows, unpriced, labour = [], [], []
    total = Decimal(0)
    labour_total = Decimal(0)
    for raw in lines:
        parsed = parse_line(raw)
        if parsed is None:
            continue
        qty, unit, item = parsed

        best, best_score = None, 0.0
        for desc, price, _src in book:
            score = match_score(item, desc)
            if score > best_score:
                best, best_score = price, score
        if best is None:
            best = _size_silent_price(item, book)

        family = fixture_family(item)
        row = _price_row(tenant_cfg, family)
        if best is None and row is not None and row.supply is not None:
            # The fixture price list is the business's own figure too.
            best = Decimal(row.supply)

        if best is None:
            unpriced.append(item)
        else:
            line_total = (qty * best).quantize(Decimal('0.01'))
            rows.append((qty, unit, item, best, line_total))
            total += line_total

        if row is not None and row.labour is not None:
            each = Decimal(row.labour)
            labour.append((qty, item, each))
            labour_total += qty * each

    return {'rows': rows, 'unpriced': unpriced, 'materials_total': total,
            'labour': labour, 'labour_total': labour_total}


# ── Reading the list back out of the transcript ─────────────────────────────

def _page_key(lines):
    return frozenset(re.sub(r'\s+', ' ', l.lower()).strip() for l in lines
                     if parse_line(l) is not None)


def list_lines_in_history(appointment, within: int = 40) -> list:
    """Every list line the customer has sent us recently, one copy per page.

    Leads photograph the same page twice (the second shot is the sharper one),
    so a page whose lines mostly repeat an earlier page is dropped rather than
    priced twice.
    """
    history = getattr(appointment, 'conversation_history', None) or []
    pages = []
    for entry in history[-within:] if within else history:
        if (isinstance(entry, dict) and entry.get('role') == 'user'
                and isinstance(entry.get('materials_list'), list)):
            page = [str(l) for l in entry['materials_list'] if str(l).strip()]
            if page:
                pages.append(page)
    kept, seen = [], []
    for page in pages:
        key = _page_key(page)
        if any(key and len(key & other) >= 0.8 * min(len(key), len(other))
               for other in seen):
            continue
        seen.append(key)
        kept.extend(page)
    return kept


# ── The reply ───────────────────────────────────────────────────────────────

def _money(currency: str, amount) -> str:
    amount = Decimal(amount).quantize(Decimal('0.01'))
    if amount == amount.to_integral_value():
        return f"{currency}{int(amount):,}"
    return f"{currency}{amount:,.2f}"


def _qty(q) -> str:
    q = Decimal(q)
    return str(int(q)) if q == q.to_integral_value() else f"{q.normalize()}"


_UNIT_LABEL = {'pair': ('pairs', 'pair'), 'kg': ('kg', 'kg'),
               'length': ('lengths', 'length'), 'litre': ('litres', 'litre'),
               'roll': ('rolls', 'roll'), 'm': ('m', 'm')}


def _about(currency: str, amount) -> str:
    """A total said with "about" carries no cents."""
    return _money(currency, Decimal(amount).quantize(Decimal('1')))


_MATERIAL_DISPLAY = {'pvc': 'PVC', 'upvc': 'uPVC', 'hdpe': 'HDPE', 'pex': 'PEX',
                     'copper': 'copper', 'brass': 'brass',
                     'galvanised': 'galvanised', 'chrome': 'chrome'}


def display_item(item: str) -> str:
    """The item as the CUSTOMER should read it (owner-approved copy, 2026-09-18).

    The transcription keeps the list's own notes because matching needs them;
    the customer does not. Brand notes in brackets go ("(cap)", "(Nasco red)",
    "(clear)"), a material in brackets moves in front of the item where a
    person would say it ("25mm plain tees (pvc)" -> "25mm PVC plain tees"),
    "cu" is copper and "&" is "and".
    """
    text = item or ''
    material = ''
    for inner in re.findall(r'\(([^)]*)\)', text):
        key = inner.strip().lower()
        if key in _MATERIAL_DISPLAY:
            material = _MATERIAL_DISPLAY[key]
    text = re.sub(r'\([^)]*\)', ' ', text).replace(')', ' ').replace('(', ' ')
    text = re.sub(r'\bcu\b', 'copper', text, flags=re.IGNORECASE)
    text = text.replace('&', ' and ')
    text = re.sub(r'\s+', ' ', text).strip()
    if material and material.lower() not in text.lower():
        m = re.match(r'^((?:\d+(?:\.\d+)?\s*(?:mm|m|")?\s*(?:x\s*)?)+)', text)
        text = (f"{m.group(1).strip()} {material} {text[m.end():].strip()}"
                if m else f"{material} {text}")
    return text


def list_close(is_shona: bool, visit_booked: bool) -> str:
    """The closing line. A lead who has already booked the visit is never
    pitched it again (CLAUDE.md), so they are told when the rest gets done
    instead of being asked for a day they have already given."""
    if visit_booked:
        return ("Tichapedza zvimwe zvese patinouya kuzoona nzvimbo." if is_shona
                else "We'll go through the rest when we come through for the visit.")
    return ("Mungada kuti tiuye riini kuzoona, vhiki rino kana vhiki rinouya?"
            if is_shona else
            "When would suit you for us to come and have a look, this week or next week?")


def build_list_price_reply(priced: dict, currency: str, include_labour: bool,
                           is_shona: bool, visit_booked: bool = False,
                           visit_cost: str = '', **_legacy) -> str:
    """The priced list, in the owner's approved shape (2026-09-18).

      These are the prices I have for now on your list:
      <qty> x <item>: <line total> (<unit> each)
      That comes to about <total> for these items.
      For everything else on the list and the final price, we would need to
      come and see the place. That also lets us give you an accurate figure
      for the labour. Because it's all one job, the labour usually comes in
      lower than pricing each fitting on its own.
      When would suit you for us to come and have a look, this week or next week?

    What varies is only what the conversation changes: the unpriced items are
    never listed (the owner does not want "still getting prices"), a list with
    every line priced drops "everything else", a list with nothing priced goes
    straight to the visit, rough fixture labour appears only when they asked
    about labour, a booked lead is not pitched the visit, and a Shona lead
    reads Shona. Plain lines, no bullets, no emoji, no dash, one question.
    """
    rows = priced['rows']
    unpriced = priced['unpriced']
    parts = []

    if rows:
        parts.append("Heino mitengo yandinayo parizvino pa list yenyu:" if is_shona
                     else "These are the prices I have for now on your list:")
        lines = []
        for qty, unit, item, each, line_total in rows:
            name = display_item(item)
            if unit in _UNIT_LABEL:
                many, one = _UNIT_LABEL[unit]
                label = f"{_qty(qty)} {many} {name}"
                each_note = f" ({_money(currency, each)} per {one})"
            else:
                label = f"{_qty(qty)} x {name}"
                each_note = f" ({_money(currency, each)} each)" if qty != 1 else ''
            lines.append(f"{label}: {_money(currency, line_total)}{each_note}")
        parts.append('\n'.join(lines))
        total = _about(currency, priced['materials_total'])
        parts.append(f"Izvi zvinosvika {total} pazvinhu izvi." if is_shona
                     else f"That comes to about {total} for these items.")
        if include_labour and priced['labour']:
            parts.append(_labour_lines(priced, currency, is_shona))

    see = _see_the_place(bool(rows), bool(unpriced), is_shona, visit_booked)
    # A business that charges for the visit says so once, before asking for a
    # day (the caller passes '' for a free visit or a fee already stated).
    parts.append(f"{see} {visit_cost}".strip() if visit_cost else see)
    parts.append(list_close(is_shona, visit_booked))
    return '\n\n'.join(p for p in parts if p)


def _see_the_place(any_priced: bool, any_unpriced: bool, is_shona: bool,
                   visit_booked: bool) -> str:
    """Why the rest waits for the visit, and the bundling point on labour the
    owner wants made. No figure is put on that saving: nobody set one."""
    if is_shona:
        if not any_priced:
            head = "Kuti tikupei mitengo yezvese zviri pa list yenyu, tinoda kuuya kuzoona nzvimbo."
        elif any_unpriced:
            head = "Pazvimwe zvese zviri pa list nemutengo wekupedzisira, tinoda kuuya kuzoona nzvimbo."
        else:
            head = "Pamutengo wekupedzisira, tinoda kuuya kuzoona nzvimbo."
        return (f"{head} Izvi zvinotibatsirawo kukupai mari yebasa chaiyo. "
                "Sezvo riri basa rimwe chete, mari yebasa inowanzodzikira pane "
                "kuita chimwe nechimwe chega.")
    see = ("we'll confirm it when we come and see the place" if visit_booked
           else "we would need to come and see the place")
    if not any_priced:
        head = f"To price everything on your list properly, {see}."
    elif any_unpriced:
        head = f"For everything else on the list and the final price, {see}."
    else:
        head = f"For the final price, {see}."
    return (f"{head} That also lets us give you an accurate figure for the "
            "labour. Because it's all one job, the labour usually comes in "
            "lower than pricing each fitting on its own.")


def _labour_lines(priced: dict, currency: str, is_shona: bool) -> str:
    """Rough fixture labour, only when the lead asked about labour. The
    accurate figure is still the visit's, which the next paragraph says."""
    lines = [f"Fitting {_qty(q)} x {display_item(item)}: {_money(currency, q * each)}"
             for q, item, each in priced['labour']]
    head = ("Mari yebasa yezvinhu zvikuru, zvinongofungidzirwa:" if is_shona
            else "Labour on the main fittings, roughly:")
    total = _about(currency, priced['labour_total'])
    tail = (f"Izvi zvinosvika {total}." if is_shona else f"That's about {total}.")
    return f"{head}\n" + '\n'.join(lines) + f"\n{tail}"
