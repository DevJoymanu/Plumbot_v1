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
    business_hours={'days': 'Sunday-Friday', 'open': '08:00', 'close': '18:00', 'closed': ['sat']},
    timezone_name='Africa/Johannesburg',
    excluded_areas=['gweru', 'bulawayo', 'mutare', 'masvingo', 'victoria falls', 'hwange', 'beitbridge', 'plumtree'],
    currency='US$',
    licensed_claim_enabled=True,
    email_from_name='Takudzwa',
)


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


def get_config(tenant=None) -> TenantConfig:
    """Build the config reader for a tenant (None → a reader that answers
    'absent' for everything — callers omit gracefully)."""
    return TenantConfig(tenant)
