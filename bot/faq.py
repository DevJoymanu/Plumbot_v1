import logging

logger = logging.getLogger(__name__)

# ── Business facts ─────────────────────────────────────────────────────────────
# Plain strings — no LLM, no API call, always correct.
_FACTS = {
    'location': (
        "We're in Hatfield, Harare \n\n"
        "We come to you though — just let us know which area you're in "
        "and we'll get the visit sorted."
    ),
    'hours': (
        "We're available Sunday to Friday, 8am–6pm.\n\n"
        "Send us a message anytime and we'll get you booked in."
    ),
    'contact': (
        "You can reach Takudzwa directly on +263774819901 if you'd like to "
        "chat about the job first."
    ),
    'services': (
        "Yes, we handle all plumbing work — vanities, tubs, geysers, showers, toilets, "
        "renovations, repairs, new installations, you name it.\n\n"
        "What are you looking for?"
    ),
    'payment': (
        "Cash, EcoCash, and bank transfer — all good \n\n"
        "You'll get the full price before anything starts, no surprises."
    ),
    'free_quote': (
        "Yes, the site visit and quote are completely free \n\n"
        "We come to you, have a look, and give you a fixed price on the spot "
        "before any work starts."
    ),
    'job_duration': (
        "It depends on the scope of work — a small repair can be done in a few hours, "
        "while a full bathroom renovation typically takes a few days.\n\n"
        "We'll give you a clearer timeline once we've seen the space."
    ),
    'licensed': (
        "Yes, we're fully licensed and registered \n\n"
        "Happy to share our credentials — just ask."
    ),
}

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


def lookup_faq(message: str) -> str | None:
    """
    Check whether a message matches a known business FAQ.

    Returns the answer string if matched, or None if the message should
    continue through the normal flow. No API calls — pure string matching.
    """
    text = message.lower().strip()

    # Ignore very short messages — they're almost always booking-flow replies
    # (e.g. "Yes", "Monday", "Hatfield") not FAQ questions.
    if len(text) < 8:
        return None

    for topic, triggers in _TRIGGERS.items():
        if any(trigger in text for trigger in triggers):
            logger.info("FAQ match: topic=%s message='%s'", topic, message[:80])
            return _FACTS[topic]

    return None
