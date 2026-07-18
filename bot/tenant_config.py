"""
TenantConfig — the config access seam (docs/MULTI_TENANT_PLAN.md §3.3).

Everything the code used to hardcode for Homebase is read through here, per
tenant. Phase 2 moves the constants into TenantProfile slice by slice; each
accessor returns the tenant's own value or signals absence.

THE NULLABILITY RULE (docs/CLIENT_ONBOARDING_CHECKLIST.md): business facts
(names, numbers, prices, locations, claims) have NO cross-tenant fallback —
absent means the caller gracefully omits the topic. Generic copy (openers,
question scripts) may fall back to platform defaults. A missing fact must
never resolve to another tenant's value.

HOMEBASE_* seeds below are the single in-code source for what migrations and
the test-DB hook write into homebase's profile — keep them byte-identical to
production behaviour (TEST 0 pins them).
"""

from __future__ import annotations

# ── Homebase seed data ────────────────────────────────────────────────────────
# Verbatim the strings the code shipped with (bot/faq.py::_FACTS et al.).
# Written to homebase's TenantProfile by migration 0045 and by the test-DB
# post_migrate hook (bot/apps.py) so both real and test databases agree.

HOMEBASE_FAQ_FACTS = {
    'location': "We're in Hatfield, Harare.",
    'hours': (
        "We're available Sunday to Friday, 8am–6pm.\n\n"
        "Easy to find a slot that fits you."
    ),
    'contact': (
        "You can reach Takudzwa directly on +263774819901 if you'd like to "
        "chat about the job first."
    ),
    'services': (
        "Yes, we handle all plumbing work — vanities, tubs, geysers, showers, toilets, "
        "renovations, repairs, new installations, you name it."
    ),
    'payment': (
        "Cash, EcoCash, and bank transfer — all good.\n\n"
        "You'll get the full price before anything starts, no surprises."
    ),
    'free_quote': (
        "Yes, the site visit and quote are completely free.\n\n"
        "We come to you, have a look, and give you a fixed price on the spot "
        "before any work starts."
    ),
    'job_duration': (
        "It depends on the scope of work — a small repair can be done in a few hours, "
        "while a full bathroom renovation typically takes a few days."
    ),
    'licensed': (
        "Yes, we're fully licensed and registered."
    ),
}

HOMEBASE_PROFILE_FIELDS = dict(
    plumber_name='Takudzwa',
    plumber_contact='+263774819901',
    business_whatsapp='+263776255077',
    location_line="We're in Hatfield, Harare.",
    location_area='Hatfield',
    location_city='Harare',
    business_hours={'days': 'Sunday-Friday', 'open': '08:00', 'close': '18:00', 'closed': ['sat']},
    timezone_name='Africa/Johannesburg',
    excluded_areas=['gweru', 'bulawayo', 'mutare', 'masvingo', 'victoria falls', 'hwange', 'beitbridge', 'plumtree'],
    currency='US$',
    licensed_claim_enabled=True,
    email_from_name='Takudzwa',
)


# Homebase price sheet (Phase 2.3) — verbatim from bot/sales_profiles/homebase.md
# and the response_mixin price tables (which cite it as their source). Written
# to TenantPriceItem rows by migration 0047 + the test-DB hook. Numbers only;
# sentences are rendered by platform copy.
HOMEBASE_PRICE_ITEMS = [
    # family, variant, label, supply, labour, flat, allin, parts
    dict(family='shower', variant='', label='shower cubicle', supply=130, labour=40, allin=170),
    dict(family='tub', variant='', label='tub', supply=80, labour=80, allin=160,
         sizes=['Built-in bathtubs',
                '- Compact / Standard: 1700 × 700 mm',
                '- Large / Luxury: 1800 × 800 mm']),
    dict(family='tub', variant='freestanding', label='freestanding tub', allin=670,
         parts=[{'name': 'tub', 'amount': 400}, {'name': 'mixer', 'amount': 150},
                {'name': 'install', 'amount': 120}],
         sizes=['Free-standing bathtubs',
                '- Compact: 1440 × 570 mm',
                '- Standard: 1700 × 700 to 800 mm',
                '- Large / Luxury: 1800 to 1865 × 800 to 890 mm']),
    # Corner tub: priced as a built-in (owner rule) — this row carries only
    # the measurement block for size questions.
    dict(family='tub', variant='corner', label='corner tub',
         sizes=['Corner bathtubs',
                '- Compact symmetrical: 1200 × 1200 mm to 1350 × 1350 mm',
                '- Standard symmetrical: 1500 × 1500 mm',
                '- Offset corner: 1500 to 1700 × 900 to 1000 mm']),
    dict(family='geyser', variant='', label='geyser', supply=80, labour=80, allin=160),
    dict(family='vanity', variant='', label='vanity unit', short_label='vanity', supply=150, labour=30, allin=180),
    dict(family='toilet', variant='', label='toilet seat', short_label='toilet', supply=50, labour=20, allin=70),
    dict(family='toilet', variant='wall_hung', label='wall-hung toilet (chamber install)',
         supply=130, labour=30, allin=160),
    dict(family='chamber', variant='', label='side chamber', supply=130, labour=30, allin=160),
    dict(family='basin', variant='', label='basin (pedestal / corner)', flat=70),
    # Renovations & packages
    dict(family='renovation', variant='bathroom', label='Bathroom Renovation', flat=900),
    dict(family='renovation', variant='kitchen', label='Kitchen Renovation', flat=600),
    dict(family='package', variant='full_bathroom', label='Full Bathroom Package', flat=800),
    dict(family='package', variant='facebook', label='Facebook Package', flat=800,
         parts=[{'name': 'freestanding tub'}, {'name': 'side chamber'}]),
    # Geyser services
    dict(family='geyser_service', variant='supply_install', label='Geyser Supply & Installation', allin=160),
    dict(family='geyser_service', variant='replacement', label='Full Geyser Replacement', allin=350),
    dict(family='geyser_service', variant='pressure_valve', label='Pressure Valve Replacement', labour=25),
    dict(family='geyser_service', variant='thermostat', label='Thermostat Replacement', labour=30),
    dict(family='geyser_service', variant='element', label='Element Replacement', labour=40),
    # Repairs & maintenance
    dict(family='repair', variant='leaking_tap', label='Leaking Tap', labour=15),
    dict(family='repair', variant='toilet_seat_replacement', label='Toilet Seat Replacement', supply=20, labour=10),
    dict(family='repair', variant='cistern', label='Cistern Repair', labour=20),
    dict(family='repair', variant='toilet_base', label='Leaking Toilet Base', labour=25),
    dict(family='repair', variant='full_toilet_replacement', label='Full Toilet Replacement', supply=60, labour=40),
    dict(family='repair', variant='drain_simple', label='Drain Unblocking (simple)', labour=20),
    dict(family='repair', variant='drain_severe', label='Drain Unblocking (severe)', labour=50),
    dict(family='repair', variant='jetting', label='High-Pressure Jetting', flat=80),
    dict(family='repair', variant='minor_pipe_leak', label='Minor Pipe Leak Repair', labour=20),
    dict(family='repair', variant='burst_pipe', label='Burst Pipe Repair', labour=40),
    dict(family='repair', variant='pipe_section', label='Pipe Section Replacement', labour=50),
]


# Money columns an owner fills in themselves — never auto-filled from homebase.
_PRICE_MONEY_FIELDS = ('supply', 'labour', 'flat', 'allin')


def blank_priced_catalog():
    """The homebase catalogue as item skeletons — identity/label only, no
    figures — for prefilling a NEW tenant's price sheet as a fill-in template
    (they set their own prices; blank stays unquoted). Everything that CAN be
    auto-filled without inventing the tenant's money is; the four money fields
    are left empty for the owner."""
    skeleton = []
    for order, row in enumerate(HOMEBASE_PRICE_ITEMS):
        item = {k: v for k, v in row.items() if k not in _PRICE_MONEY_FIELDS}
        # Components (e.g. freestanding tub = tub + mixer + install) keep their
        # names as a ready breakdown; amounts blank for the owner to fill.
        if item.get('parts'):
            item['parts'] = [{'name': p['name']} for p in item['parts'] if p.get('name')]
        item.setdefault('variant', '')
        item['sort_order'] = order
        skeleton.append(item)
    return skeleton


def _as_int(value):
    """Prices are whole-dollar 'from' rates; render without trailing .00."""
    if value is None:
        return None
    return int(value) if value == int(value) else float(value)


class TenantConfig:
    """Per-tenant config reader. Cheap to construct; caches the profile row
    for its own lifetime (build one per turn, like the Appointment)."""

    def __init__(self, tenant=None):
        self.tenant = tenant
        self._profile = None
        self._profile_loaded = False

    @property
    def profile(self):
        if not self._profile_loaded:
            self._profile_loaded = True
            if self.tenant is not None:
                from .models import TenantProfile
                self._profile = TenantProfile.objects.filter(tenant=self.tenant).first()
        return self._profile

    def _field(self, name, default=''):
        if self.profile is None:
            return default
        value = getattr(self.profile, name, None)
        return value if value not in (None, '') else default

    # ── Identity (business facts — no fallback) ─────────────────────────────
    @property
    def plumber_name(self) -> str:
        return self._field('plumber_name')

    @property
    def plumber_contact(self) -> str:
        return self._field('plumber_contact')

    @property
    def business_whatsapp(self) -> str:
        return self._field('business_whatsapp')

    @property
    def location_line(self) -> str:
        return self._field('location_line')

    @property
    def location_area(self) -> str:
        return self._field('location_area')

    @property
    def location_city(self) -> str:
        return self._field('location_city')

    def location_short(self) -> str:
        """'Hatfield, Harare' — for composing sentences; '' when unknown."""
        parts = [p for p in (self.location_area, self.location_city) if p]
        return ', '.join(parts)

    # ── Hours (rendered from business_hours JSON in the formats the copy
    # uses; '' when the tenant has no hours on file → callers omit) ─────────
    def _hours_parts(self):
        hours = self._field('business_hours', None)
        if not hours or not hours.get('days') or not hours.get('open') or not hours.get('close'):
            return None
        day_start, _, day_end = hours['days'].partition('-')
        def clock(value, style):
            hh, _, mm = value.partition(':')
            hour12 = int(hh) % 12 or 12
            suffix = 'AM' if int(hh) < 12 else 'PM'
            if style == 'long':       # "8:00 AM"
                return f"{hour12}:{mm or '00'} {suffix}"
            if style == 'short':      # "8 AM"
                return f"{hour12} {suffix}"
            return f"{hour12}{suffix.lower()}"  # "8am"
        return day_start.strip(), day_end.strip(), clock(hours['open'], 'long'), clock(hours['close'], 'long'), \
            clock(hours['open'], 'short'), clock(hours['close'], 'short'), \
            clock(hours['open'], 'tiny'), clock(hours['close'], 'tiny')

    def hours_sentence(self) -> str:
        """'Sunday to Friday, 8:00 AM – 6:00 PM'"""
        p = self._hours_parts()
        if p is None:
            return ''
        return f"{p[0]} to {p[1]}, {p[2]} – {p[3]}"

    def hours_medium(self) -> str:
        """'Sunday–Friday, 8 AM–6 PM'"""
        p = self._hours_parts()
        if p is None:
            return ''
        return f"{p[0]}–{p[1]}, {p[4]}–{p[5]}"

    def hours_compact(self) -> str:
        """'Sun–Fri 8am–6pm'"""
        p = self._hours_parts()
        if p is None:
            return ''
        return f"{p[0][:3]}–{p[1][:3]} {p[6]}–{p[7]}"

    @property
    def currency(self) -> str:
        return self._field('currency', 'US$')

    @property
    def licensed_claim_enabled(self) -> bool:
        return bool(self.profile and self.profile.licensed_claim_enabled)

    def excluded_areas(self) -> list:
        return list(self._field('excluded_areas', []) or [])

    # ── FAQ facts (business facts — no fallback) ─────────────────────────────
    def faq_fact(self, topic: str):
        """The tenant's own fact sentence for a topic, or None → the caller
        skips/deflects the topic (never answers with another tenant's fact).
        The 'licensed' topic is additionally gated on certification docs
        being on file (licensed_claim_enabled)."""
        if self.profile is None:
            return None
        if topic == 'licensed' and not self.profile.licensed_claim_enabled:
            return None
        fact = (self.profile.faq_facts or {}).get(topic)
        return fact or None


    # ── Prices (business facts — no fallback; Phase 2.3) ─────────────────────
    def price_items(self) -> list:
        """The tenant's active price rows, cached for this config's lifetime."""
        if not hasattr(self, '_price_items'):
            if self.tenant is None:
                self._price_items = []
            else:
                from .models import TenantPriceItem
                self._price_items = list(
                    TenantPriceItem.objects.filter(tenant=self.tenant, is_active=True)
                )
        return self._price_items

    def price_item(self, family: str, variant: str = ''):
        for item in self.price_items():
            if item.family == family and item.variant == variant:
                return item
        return None

    def price_components(self) -> dict:
        """{family: (supply, labour)} for default-variant fittings with a
        split — the shape _FAMILY_PRICE_COMPONENTS consumers expect. Missing
        family → caller deflects to the free site visit."""
        out = {}
        for item in self.price_items():
            if item.variant == '' and item.supply is not None and item.labour is not None:
                out[item.family] = (_as_int(item.supply), _as_int(item.labour))
        return out

    def flat_prices(self) -> dict:
        """{family: flat} for default-variant items priced without a split —
        the _FAMILY_FLAT_PRICE shape."""
        return {
            item.family: _as_int(item.flat)
            for item in self.price_items()
            if item.variant == '' and item.flat is not None
        }

    def rough_price_lines(self) -> dict:
        """{family: 'label from US$X'} — the _FAMILY_ROUGH_PRICE shape,
        rendered from allin (or flat) figures."""
        out = {}
        for item in self.price_items():
            if item.variant != '':
                continue
            figure = item.allin if item.allin is not None else item.flat
            name = item.short_label or item.label
            if figure is None or not name:
                continue
            out[item.family] = f"{name} from {self.currency}{_as_int(figure)}"
        return out

    def labour_breakdown_lines(self) -> dict:
        """{family: 'Label: supply from US$X, labour from US$Y'} — the
        _FAMILY_LABOUR_BREAKDOWN shape."""
        out = {}
        for item in self.price_items():
            if item.variant == '' and item.supply is not None and item.labour is not None:
                label = (item.label or item.family).capitalize()
                out[item.family] = (
                    f"{label}: supply from {self.currency}{_as_int(item.supply)}, "
                    f"labour from {self.currency}{_as_int(item.labour)}"
                )
        return out

    def tub_size_blocks(self) -> dict:
        """{'built_in'|'freestanding'|'corner': rendered measurement block} —
        was ResponseMixin._TUB_SIZE_BLOCKS. Rows without sizes are omitted."""
        out = {}
        for item in self.price_items():
            if item.family == 'tub' and item.sizes:
                out[item.variant or 'built_in'] = "\n".join(item.sizes)
        return out

    def freestanding_tub(self):
        """(allin, split_sentence) for the freestanding tub, or None — the
        _FREESTANDING_TUB_* pair."""
        item = self.price_item('tub', 'freestanding')
        if item is None or item.allin is None:
            return None
        named = {p.get('name'): p.get('amount') for p in (item.parts or [])}
        if all(k in named for k in ('tub', 'mixer', 'install')):
            split = (
                f"tub from {self.currency}{_as_int(named['tub'])} + "
                f"mixer {self.currency}{_as_int(named['mixer'])}, "
                f"install from {self.currency}{_as_int(named['install'])}"
            )
        else:
            split = f"freestanding tub from {self.currency}{_as_int(item.allin)} all-in"
        return (_as_int(item.allin), split)


def get_config(tenant=None) -> TenantConfig:
    """Build the config reader for a tenant (None → a reader that answers
    'absent' for everything — callers omit gracefully)."""
    return TenantConfig(tenant)
