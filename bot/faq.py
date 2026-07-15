from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Business facts ─────────────────────────────────────────────────────────────
# Facts live in each tenant's TenantProfile.faq_facts (Phase 2 slice 1 —
# seeded for homebase by migration 0045, verified on prod 2026-07-15). At the
# call site they are answered AI-primary (ai_answer_faq grounds on the fact
# and rephrases naturally); the profile sentence is the fallback wording. The
# non-price qualifying close is appended by the caller. A tenant without a
# fact for a topic gets None → the caller skips the topic (nullability rule:
# business facts never fall back to another tenant's values).

# ── Trigger phrases per topic ──────────────────────────────────────────────────
# Phrases are checked as substrings of the lowercased message.
# Keep triggers specific enough to avoid false matches during normal booking flow.
_TRIGGERS = {
    'location': [
        'where are you',
        'where is homebase',
        'where you based',
        'where are you based',
        'where you guys',
        'your address',
        'where do you operate',
        'where can i find you',
        'where you located',
        'where are you located',
        'where is your office',
        'where do you work from',
        'homebase location',
        'office address',
        'which area are you',
        'where in harare',
    ],
    'hours': [
        'what time do you',
        'what are your hours',
        'when do you open',
        'when are you open',
        'working hours',
        'business hours',
        'office hours',
        'when can i contact',
        'are you open on',
        'when do you close',
        'what time are you',
        'what hours do',
        'do you work on weekends',
        'do you work on saturday',
        'do you work on sunday',
        'are you available on',
        'open on sunday',
        'open on saturday',
    ],
    'contact': [
        'phone number',
        'contact number',
        'can i call you',
        'contact details',
        'speak to someone',
        'speak to a person',
        'talk to takudzwa',
        'speak to takudzwa',
        'real person',
        'human being',
        'can i speak to',
        'another number',
        'direct number',
        'takudzwa number',
        'takudzwa contact',
    ],
    'services': [
        'what services',
        'what do you do',
        'what do you offer',
        'what can you fix',
        'what do you repair',
        'what work do you do',
        'what kind of work',
        'list of services',
        'what jobs do you',
        'do you fix',
        'do you do renovations',
        'do you do tiling',
        'do you do electrical',
        'do you do roofing',
        'do you install',
        'do you handle',
        'can you do',
        'do you have',
        'do you sell',
        'do you stock',
        'do you supply',
        'do you carry',
    ],
    'payment': [
        'payment methods',
        'how do you charge',
        'do you accept cash',
        'can i pay with',
        'ecocash payment',
        'do you take ecocash',
        'do you take cash',
        'bank transfer',
        'deposit required',
        'do you need a deposit',
        'how to pay',
        'payment options',
        'do you accept usd',
        'do you accept zig',
        'do you accept bond',
    ],
    'free_quote': [
        'free quote',
        'free estimate',
        'free assessment',
        'is the quote free',
        'do you charge for a quote',
        'cost of quote',
        'do you charge for the visit',
        'is the visit free',
        'free site visit',
        'do you charge to come',
        'how much is the quote',
        'how much to come',
    ],
    'job_duration': [
        'how long does it take',
        'how long will it take',
        'how long does the job take',
        'how long does a renovation take',
        'how many days',
        'how long for',
        'turnaround time',
        'how quickly',
        'how soon can you',
        'when will it be done',
        'completion time',
    ],
    'licensed': [
        'are you licensed',
        'are you registered',
        'are you certified',
        'do you have a license',
        'are you qualified',
        'qualifications',
        'credentials',
        'are you legit',
        'proof of registration',
        'registered plumber',
        'certified plumber',
    ],
}


def match_faq_topic(message: str) -> str | None:
    """Return the matched FAQ topic (e.g. 'location', 'services'), or None. Same
    matching as lookup_faq — pure string matching, no API calls."""
    text = message.lower().strip()

    # Ignore very short messages — they're almost always booking-flow replies
    # (e.g. "Yes", "Monday", "Hatfield") not FAQ questions.
    if len(text) < 8:
        return None

    for topic, triggers in _TRIGGERS.items():
        if any(trigger in text for trigger in triggers):
            logger.info("FAQ match: topic=%s message='%s'", topic, message[:80])
            return topic

    return None


def faq_fact(topic: str, tenant=None) -> str | None:
    """The fact sentence for a matched topic, from the TENANT's profile.
    None → the caller skips/deflects the topic. tenant=None (pre-threading
    callers: gate tests, REPL) resolves to the homebase seed tenant."""
    from .tenant_config import get_config
    if tenant is None:
        from .models import Tenant
        tenant = Tenant.objects.filter(slug='homebase').first()
    return get_config(tenant).faq_fact(topic)


def lookup_faq(message: str, tenant=None) -> str | None:
    """
    Check whether a message matches a known business FAQ.

    Returns the answer string if matched, or None if the message should
    continue through the normal flow. No API calls — pure string matching.
    """
    topic = match_faq_topic(message)
    return faq_fact(topic, tenant=tenant) if topic else None
