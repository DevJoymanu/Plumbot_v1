"""
Portfolio catalogue — individual, requestable previous-work pieces.

Each item carries a title, an indicative price, a short description, and a
relatable back-story so the bot can answer contextually when a customer asks
about a *specific* photo ("how much was the black bathtub?", "the gold taps
one", "that marble shower") rather than just dumping the whole gallery.

Design notes
------------
- Image files live in ``bot/previous_work_photos/`` (the same folder the batch
  portfolio send reads from), so every catalogued item is ALSO part of the
  generic "send me your portfolio" gallery automatically.
- Matching is keyword/synonym based (English + Shona), so it works with no
  extra API calls. ``match_portfolio_item`` returns the single best match only
  when the customer clearly points at one piece; ambiguous or generic requests
  return ``None`` and the caller falls back to the normal gallery flow.
- PRICES BELOW ARE THE BUSINESS'S OWN "from" RATES, taken verbatim from the
  pricing table in ``bot/sales_profiles/homebase.md`` (the source of truth).
  Per that profile: never invent prices beyond the table — quote "from" and
  defer the exact figure to the free on-site quote. Keep these in sync if the
  profile table changes. No emojis in any customer-facing copy (house rule).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

PORTFOLIO_IMAGES_DIR = os.environ.get(
    'PREVIOUS_WORK_IMAGES_DIR',
    os.path.join(os.path.dirname(__file__), 'previous_work_photos'),
)


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue data
# ─────────────────────────────────────────────────────────────────────────────
# Each item:
#   id          stable slug (used in logs / dedup)
#   filename    file inside PORTFOLIO_IMAGES_DIR (camelCase to match existing)
#   title       short customer-facing name
#   price       indicative "from" guide — ADJUST to real quoting
#   description one-line spec of what's in the photo
#   story       warm, relatable back-story (1–2 sentences, no emojis)
#   keywords    lowercase trigger terms (English + Shona) used for matching

PORTFOLIO_ITEMS: list[dict] = [
    {
        'id': 'modern-kitchen-island',
        'filename': 'Full_kitchen_renovation.jpeg',
        'title': 'Modern Open-Plan Kitchen',
        'price': 'kitchen renovation from US$600',
        'description': 'White quartz island, pendant lighting, built-in oven and gas hob, full marble-look floor.',
        'story': (
            'This was a young family in Borrowdale who wanted the kitchen to be the heart of the home. '
            'We built the island so the parents can cook while still chatting to the kids at the breakfast bar. '
            'The quartz tops wipe clean in seconds and have handled three years of family life without a mark.'
        ),
        'keywords': [
            'kitchen', 'island', 'breakfast bar', 'pendant', 'quartz', 'open plan',
            'open-plan', 'cooking', 'hob', 'oven', 'kicheni',
        ],
    },
    {
        'id': 'navy-shaker-kitchen',
        'filename': 'Kitchen_installation.jpeg',
        'title': 'Navy Shaker Kitchen',
        'price': 'kitchen renovation from US$600',
        'description': 'Navy shaker cabinetry, white quartz tops, gas hob on the island, soft patterned tile floor.',
        'story': (
            'The owner had seen navy kitchens online and was worried they would look too dark in a Harare home. '
            'We balanced it with white tops and a light floor, and the result is warm rather than heavy. '
            'It has become the photo everyone asks us about.'
        ),
        'keywords': [
            'navy', 'blue kitchen', 'dark kitchen', 'shaker', 'gas hob',
            'patterned floor', 'blue cupboards', 'blue cabinets',
        ],
    },
    {
        'id': 'freestanding-tub-hex',
        'filename': 'standalone_freestanding_tub(2).jpg',
        'title': 'Freestanding Tub & Wall-Hung Toilet',
        'price': 'freestanding tub from US$670 + wall-hung toilet from US$160; full bathroom renovation from US$600',
        'description': 'Oval freestanding tub, black wall-mounted mixer, hexagon mosaic border, wall-hung toilet.',
        'story': (
            'A couple in Mount Pleasant wanted a hotel-style soak after long days. '
            'We set the freestanding tub against a marble-look wall with a black mixer for contrast, '
            'and floated the toilet off the floor so cleaning underneath takes seconds.'
        ),
        'keywords': [
            'freestanding', 'free standing', 'free-standing', 'oval tub', 'soak',
            'wall hung toilet', 'wall-hung', 'hexagon', 'hex', 'black tap',
            'black mixer', 'standalone', 'stand alone', 'stand-alone',
        ],
    },
    {
        'id': 'gold-tap-double-vanity',
        'filename': 'custom_double_vanity.jpg',
        'title': 'Gold-Tap Double Vanity',
        'price': 'vanity unit from US$180; full bathroom renovation from US$600',
        'description': 'Twin white vessel basins on black granite, brushed-gold square mixers, marble-look walls.',
        'story': (
            'This was the en-suite of a busy professional couple who were tired of fighting over one basin. '
            'Two basins ended the morning rush, and the gold taps against the black granite give it that '
            'boutique-hotel feel they wanted without redoing the whole house.'
        ),
        'keywords': [
            'gold tap', 'gold taps', 'gold mixer', 'brass', 'double vanity',
            'two basins', 'twin basin', 'double sink', 'vessel basin',
            'his and hers', 'gold', ' goridhe',
        ],
    },
    {
        'id': 'black-granite-vanity-tub',
        'filename': 'standalone_freestanding_tub.jpg',
        'title': 'Black Granite Vanity & Designer Tub',
        'price': 'freestanding tub from US$670 + vanity from US$180; full renovation from US$600',
        'description': 'Floating black granite vanity, twin white vessel basins, black wall taps, sculpted black freestanding tub.',
        'story': (
            'The client wanted something dramatic and one-of-a-kind for the master bathroom. '
            'We floated the granite vanity off the wall and paired it with a sculpted black tub. '
            'It is the room they show off first when guests visit.'
        ),
        'keywords': [
            'black tub', 'black bath', 'black bathtub', 'black granite',
            'floating vanity', 'black vanity', 'designer tub', 'black sinks',
            'sculpted', 'boat tub', 'dramatic', 'black taps',
        ],
    },
    {
        'id': 'backlit-guest-toilet',
        'filename': 'backlitGuestToilet.jpeg',
        'title': 'Backlit Guest Toilet',
        'price': 'toilet & cistern from US$70, vanity from US$180',
        'description': 'Compact guest WC with a backlit stone feature wall, close-coupled toilet and slim vanity.',
        'story': (
            'It was a tiny guest toilet under a staircase that the owner thought was a lost cause. '
            'We added a backlit stone wall and a slim vanity, and now it is the little surprise that '
            'makes visitors comment every time.'
        ),
        'keywords': [
            'guest toilet', 'guest wc', 'cloakroom', 'small toilet', 'backlit',
            'feature wall', 'stone wall', 'under stairs', 'powder room', 'compact',
        ],
    },
    {
        'id': 'classic-toilet-basin',
        'filename': 'chamber_and_sink.jpg',
        'title': 'Classic Toilet & Basin Suite',
        'price': 'toilet & cistern from US$70',
        'description': 'Clean close-coupled toilet with a matching pedestal basin on neutral floor tiles.',
        'story': (
            'A landlord needed a reliable, smart-looking bathroom for a rental without overspending. '
            'We fitted a durable close-coupled toilet and pedestal basin that look tidy and handle '
            'tenant after tenant with no fuss.'
        ),
        'keywords': [
            'pedestal', 'pedestal basin', 'standard toilet', 'simple toilet',
            'basic', 'rental', 'close coupled', 'close-coupled', 'budget',
            'affordable bathroom', 'toilet and basin',
        ],
    },
    {
        'id': 'clawfoot-tub-feature-wall',
        'filename': 'full_bathroom_renovation.jpg',
        'title': 'Vintage Clawfoot Tub Bathroom',
        'price': 'freestanding tub from US$670 + wall-hung toilet from US$160; full bathroom renovation from US$600',
        'description': 'White roll-top clawfoot tub, brick-effect feature tiling, wall-hung toilet and corner basin.',
        'story': (
            'The owner loved old-world charm but in a newly built home. '
            'We sourced a roll-top clawfoot tub and wrapped the room in brick-effect tiles for that '
            'timeless cottage feel, with modern plumbing hidden behind it all.'
        ),
        'keywords': [
            'clawfoot', 'claw foot', 'claw-foot', 'roll top', 'roll-top',
            'vintage', 'classic tub', 'old style', 'brick tile', 'cottage',
            'antique tub',
        ],
    },
    {
        'id': 'walk-in-rain-shower',
        'filename': 'Cubicle.jpg',
        'title': 'Walk-In Rain Shower',
        'price': 'shower cubicle from US$170; full bathroom renovation from US$600',
        'description': 'Frameless glass walk-in shower, overhead rain head, mosaic feature stripe, level drainage.',
        'story': (
            'A client with a small second bathroom wanted to drop the old tub for an easy walk-in shower. '
            'We built a frameless glass walk-in with a rain head and level drainage, so it feels far bigger '
            'than the space it actually takes.'
        ),
        'keywords': [
            'rain shower', 'walk in', 'walk-in', 'walkin', 'glass shower',
            'frameless', 'shower', 'overhead shower', 'wet room', 'shawa',
        ],
    },
    {
        'id': 'marble-builtin-tub',
        'filename': 'ordinar_tub(built-in)_2.jpg',
        'title': 'Marble Built-In Bathtub',
        'price': 'standard bathtub from US$160; full bathroom renovation from US$600',
        'description': 'Built-in bathtub clad in marble-look tile, chrome telephone mixer, bright airy finish.',
        'story': (
            'A family bathroom that needed to work hard for both kids and adults. '
            'We built in the tub and clad it in marble-look tile so it is easy to clean and still feels '
            'high-end, with a handheld mixer that makes bath time with little ones simple.'
        ),
        'keywords': [
            'built in tub', 'built-in', 'marble bath', 'marble tub', 'family bathroom',
            'chrome tap', 'telephone mixer', 'handheld', 'white marble', 'tiled bath',
        ],
    },
    {
        'id': 'marble-tub-black-tap-vanity',
        'filename': 'odinary_tub(built-in).jpg',
        'title': 'Marble Bathtub & Black-Tap Vanity',
        'price': 'standard bathtub from US$160 + vanity from US$180; full renovation from US$600',
        'description': 'Built-in marble-look bathtub beside a white vanity with a matte-black vessel mixer.',
        'story': (
            'The owner wanted a calm, bright bathroom but with one bold detail. '
            'We kept the walls clean and marble-bright and added a matte-black tap on the vanity as the '
            'single accent. It is understated luxury that still photographs beautifully.'
        ),
        'keywords': [
            'black tap vanity', 'matte black', 'matt black', 'black faucet',
            'white vanity', 'marble bathroom', 'bright bathroom', 'vessel mixer',
            'accent tap',
        ],
    },
]


_ITEMS_BY_ID = {item['id']: item for item in PORTFOLIO_ITEMS}


# ─────────────────────────────────────────────────────────────────────────────
# Lookups
# ─────────────────────────────────────────────────────────────────────────────
def get_item_by_id(item_id: str) -> Optional[dict]:
    return _ITEMS_BY_ID.get(item_id)


def image_path_for(item: dict) -> str:
    return os.path.join(PORTFOLIO_IMAGES_DIR, item['filename'])


def item_is_available(item: dict) -> bool:
    """True only when the image file actually exists on disk."""
    return os.path.exists(image_path_for(item))


def available_items() -> list[dict]:
    return [it for it in PORTFOLIO_ITEMS if item_is_available(it)]


# ─────────────────────────────────────────────────────────────────────────────
# Matching — does the customer point at ONE specific piece?
# ─────────────────────────────────────────────────────────────────────────────
# Generic gallery asks ("send your portfolio", "any pics?") must NOT match a
# single item — those fall through to the batch send. We only return a single
# item when the message contains distinctive feature words.

_GENERIC_ONLY = re.compile(
    r'^\s*(can i (see|get)|send|share|show( me)?|do you have|got)?\s*'
    r'(your |some |any |the )?'
    r'(pics?|pictures?|photos?|portfolio|gallery|examples?|previous work|work)\??\s*$',
    re.IGNORECASE,
)

# The customer must actually want to SEE or REFERENCE a specific shown piece —
# otherwise a plain new-service price ask ("how much for a pedestal basin") would
# wrongly hijack the tuned pricing/qualification flow. Any of these signals the
# intent to view/refer to a particular photo.
_REFERENCE_SIGNAL = re.compile(
    r'\b('
    r'see|show|send|share|view|look(\s+at)?|pic|pics|picture|pictures|photo|photos|'
    r'image|images|gallery|portfolio|example|examples|'
    r'that|this|those|the\s+\w+\s+one|which\s+one|'
    r'ona|ratidz\w*|ndiratidz\w*|'  # Shona: see / show me
    r'how\s+much\s+(was|were|is)\s+the'
    r')\b',
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    return re.sub(r'[^a-z0-9\s-]', ' ', (text or '').lower())


def match_portfolio_item(message: str) -> Optional[dict]:
    """
    Return the single best-matching catalogue item for a message, or None.

    Returns None when the request is generic (whole-gallery) or ambiguous
    (top two candidates tie), so the caller can fall back to the batch send.
    Only items whose image file exists on disk are considered.
    """
    if not message:
        return None

    norm = _normalise(message)
    if not norm.strip():
        return None

    # A bare "send me your portfolio" should go to the gallery, not one item.
    if _GENERIC_ONLY.match(message.strip()):
        return None

    # Must show intent to view/reference a specific piece, else let the normal
    # pricing/qualification flow handle plain service inquiries.
    if not _REFERENCE_SIGNAL.search(message):
        return None

    scored: list[tuple[int, dict]] = []
    for item in available_items():
        score = 0
        for kw in item['keywords']:
            # word/phrase boundary match to avoid partial-word false hits
            if re.search(rf'(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])', norm):
                score += 2 if ' ' in kw else 1
        if score:
            scored.append((score, item))

    if not scored:
        return None

    scored.sort(key=lambda s: s[0], reverse=True)
    if len(scored) >= 2 and scored[0][0] == scored[1][0]:
        # Tie → ambiguous, let the caller decide (gallery fallback).
        return None
    return scored[0][1]


# ─────────────────────────────────────────────────────────────────────────────
# Customer-facing copy
# ─────────────────────────────────────────────────────────────────────────────
def build_item_caption(item: dict) -> str:
    """WhatsApp caption for a single portfolio piece — product/service name only.
    No pricing, no emojis (house rule)."""
    return item['title']


def build_gallery_caption(filename: str) -> Optional[str]:
    """Per-image caption for a whole-gallery send, looked up by image filename:
    the product/service name only (no pricing). Returns None when the image isn't
    a catalogued piece, so the caller can fall back to a generic caption.
    """
    base = os.path.splitext(os.path.basename(filename or ''))[0].lower()
    if not base:
        return None
    for item in PORTFOLIO_ITEMS:
        if os.path.splitext(item['filename'])[0].lower() == base:
            return item['title']
    return None


def get_item_by_title(title: str) -> Optional[dict]:
    """Map a sent-image description back to its catalogue item, or None.

    The whole-gallery / portfolio sends record each image under its curated
    catalogue title (see ``_describe_work_image``), so an exact title match
    recovers the piece — and therefore its 'from' price — for an item the
    customer was actually sent. Uncatalogued shots (tidied filenames) match
    nothing and return None.
    """
    if not title:
        return None
    t = title.strip().lower()
    for item in PORTFOLIO_ITEMS:
        if item['title'].lower() == t:
            return item
    return None


def _bundles_multiple_priced_items(price: str) -> bool:
    """True when a catalogue ``price`` lists two or more distinct priced items
    in its primary segment (e.g. "freestanding tub from US$670 + vanity from
    US$180"), as opposed to a single product plus the standard renovation upsell
    ("vanity unit from US$180; full bathroom renovation from US$600").

    The renovation upsell is always tacked on after a ';', so we count the
    "from US$…" figures in the part BEFORE the first ';' only.
    """
    primary = (price or '').split(';', 1)[0]
    return len(re.findall(r'from\s+US\$', primary, re.IGNORECASE)) >= 2


def build_item_price_guide(title: str, language: str = 'english') -> Optional[str]:
    """Price guide for a MULTI-ITEM photo a customer pointed at — the shot they
    quoted that bundles more than one priced item (e.g. a vanity-and-tub photo).

    The single-intent reply the classifier produces prices only one of those
    items, so this fills in the rest from the catalogue ``price`` string, which
    enumerates the piece's contents. Prices are verbatim from the catalogue
    (source of truth); we never invent figures. No emojis (house rule).

    Returns None when the title isn't a catalogued piece, OR when the piece is a
    single product — in that case the targeted reply already covers it and a
    second price line would just repeat it.
    """
    item = get_item_by_title(title)
    if item is None or not _bundles_multiple_priced_items(item['price']):
        return None
    if language == 'shona':
        header = "Hezvino mutengo wakazara wechikamu ichi, nezvese zviri mupicture:"
    else:
        header = "Here's the full pricing for that piece, covering everything in the photo:"
    return f"{header}\n- {item['price']}"


def catalogue_overview() -> Optional[str]:
    """Short text menu of the pieces we can send, for 'what can you show me?'.

    Returns None when nothing is on disk yet, so the caller can fall back.
    """
    items = available_items()
    if not items:
        return None
    lines = ["Here are some of the pieces I can send you:"]
    for item in items:
        lines.append(f"- {item['title']}")
    lines.append("\nTell me which one you'd like to see and I'll send it across.")
    return "\n".join(lines)


# Customer asking WHAT we can show (doesn't know what to request yet) — distinct
# from "send me your portfolio" (a command to send the whole gallery).
_MENU_REQUEST = re.compile(
    r'\b('
    r'what can (you|u|yu) (show|send)( me)?|'
    r'what (do|have) (you|u) (got|have)( to show)?|'
    r'what (options|examples|kinds?|types?|styles?)|'
    r'what kind of work|'
    r'which (ones?|pics?|photos?|pieces?|examples?) (can|do)|'
    r'what can i see|'
    r'list (of )?(your )?(work|pics|photos|examples)'
    r')\b',
    re.IGNORECASE,
)


def is_catalogue_menu_request(message: str) -> bool:
    """True if the customer is asking what we can show (wants the menu)."""
    if not message:
        return False
    return bool(_MENU_REQUEST.search(message))
