# tests/test_bot_responses.py
"""
Test suite based on real conversation data from production appointments.
Tests: service inquiry detection, pricing responses, plan detection, language detection.
"""

import os
import sys
import json

from dotenv import load_dotenv
load_dotenv(r'D:\SAAS\CRMs\Plumbing\Plumbing_CRM\.env')

# ✅ THIS LINE is what fixes "No module named 'Plumbing_CRM'"
sys.path.insert(0, r'D:\SAAS\CRMs\Plumbing\Plumbing_CRM')

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Plumbing_CRM.settings')
django.setup()

from bot.models import Appointment
from bot.views import Plumbot
# ============================================================
# TEST DATA - extracted from real failing conversations
# ============================================================

# From Appointment 66 - Bot completely ignored the tub question
TUB_QUESTIONS = [
    "U have stand alone tub 1.5 hw much",
    "I want standalone tub only 1.5m",
    "U have or not",
    "How much stand alone",          # Appointment 91
    "May I please have pricing nad pictures of your free standing tubs",  # Apt 71
    "Do u sell Tubs or just fitting",  # Apt 81
    "Want to buy a Bath Tub",         # Apt 81
]

# From Appointment 67 - Bot ignored "Do you sell tubs"
TUB_SALES_QUESTIONS = [
    "Do you sell tubs for small bathrooms",
    "Do u sell Tubs or just fitting",
]

# From Appointment 74 - Bot gave generic response instead of pricing
PRICING_QUESTIONS = [
    "How much is it to fit a standalone tab, chamber and sink in a bathroom.",
    "How much zvese zvakadai",   # Apt 72 - Shona mixed
    "How much kuisa toilet",     # Apt 54 - Shona
    "That bathroom tub is how much",  # Apt 79
    "Bathrm tub on facebk pls",      # Apt 79 - Facebook ad reference
    "Ok bathroom seiri papic how much Shud I have",  # Apt 69
]

# From Appointment 86 - Bot gave vague location
LOCATION_QUESTIONS = [
    "Where are you located",
    "Whre ar u located",       # Apt 66 - typo
    "Ko when can I come ku office, muri kupi imimi",  # Shona mixed
]

# From Appointment 65 - Bot didn't show shower cubicle pricing
SHOWER_QUESTIONS = [
    "Shower cubicles?",
    "Shower  cubicles",
]

# From Appointment 71 - Bot ignored vanity question
VANITY_QUESTIONS = [
    "And vanitys if you have",
    "Do you do vanity?",
]

# From Appointment 84 - completely irrelevant (should NOT trigger service inquiry)
NON_SERVICE_MESSAGES = [
    "Yes",
    "Sure",
    "Ok",
    "Hi",
    "I will come back to u when my finances permit",
    "Wil contact you in due course",
]

# From Appointment 62 - student inquiry (should be redirected)
OFF_TOPIC_MESSAGES = [
    "Greetings do you offer attachment for student doing plumbing",
    "My name is Riley and l would like to develop a 3d modern fliers and logos",
    "We have 20 dollar package which contains 3 social media post",
]

# From Appointment 54 - Shona mixed messages
SHONA_MESSAGES = [
    "How much kuisa toilet",
    "Ko when can I come ku office, muri kupi imimi",
    "How much zvese zvakadai",
]

# From Appointment 79 - Facebook ad reference
FACEBOOK_QUESTIONS = [
    "Bathroom you advertised on facebk",
    "Bathrm tub on facebk pls",
]

# From Appointment 85 - catalogue request
CATALOGUE_REQUESTS = [
    "Catalogue please",
]

# ============================================================
# TEST RUNNER
# ============================================================

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def log(self, test_name, passed, message="", expected="", got=""):
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} | {test_name}")
        if not passed:
            self.failed += 1
            error = f"  Expected: {expected}\n  Got: {got}\n  Note: {message}"
            print(error)
            self.errors.append(f"{test_name}: {message}")
        else:
            self.passed += 1
            if message:
                print(f"  → {message}")

results = TestResult()


def get_test_appointment():
    """Get or create a test appointment."""
    appt, _ = Appointment.objects.get_or_create(
        phone_number="whatsapp:+263000000000",
        defaults={
            'status': 'pending',
            'project_type': None,
            'has_plan': None,
            'customer_area': None,
        }
    )
    # Reset for clean test — include the dispatcher-gating fields so pricing
    # inquiries are not skipped because a prior test marked the intent as sent.
    appt.project_type = None
    appt.has_plan = None
    appt.customer_area = None
    appt.conversation_history = []
    appt.sent_pricing_intents = []
    appt.pricing_overview_sent = False
    appt.status = 'pending'
    appt.scheduled_datetime = None
    appt.is_delayed = False
    appt.delay_followup_due_at = None
    appt.internal_notes = ''
    appt.save()
    return appt


def get_bot(appt):
    # Plumbot.__init__ takes a phone_number and resolves its own appointment via
    # get_or_create. Passing the phone string makes the bot operate on the SAME
    # row get_test_appointment() just reset (otherwise it resolves a junk row that
    # accumulates state across runs and makes the e2e checks flaky).
    return Plumbot(appt.phone_number)


# ============================================================
# TEST 0: Deterministic Intent Correction (no API)
# ------------------------------------------------------------
# Locks the guard that overrides an unstable LLM guess using the
# customer's own product words. API-free on purpose: the DeepSeek
# classifier is flaky on short product questions, so the regression
# we care about ("Did you sell bathroom cubicles" coming back as
# tub_sales → wrong bathtub spiel) must be pinned at the deterministic
# layer, not left to a live model call.
# ============================================================

print("\n" + "="*60)
print("TEST 0: DETERMINISTIC INTENT CORRECTION")
print("="*60)

from bot.views.plumbot.response_mixin import ResponseMixin

# (message, intent the LLM returned, expected intent after correction)
INTENT_CORRECTION_CASES = [
    # The production bug: a "cubicle" message misclassified as a tub.
    ("Did you sell bathroom cubicles", "tub_sales", "shower_cubicle"),
    ("shower cubicle price",           "tub_sales", "shower_cubicle"),
    # Genuine tub words must pass through untouched.
    ("how much tub",                   "tub_sales",      "tub_sales"),
    ("do you sell baths",              "tub_sales",      "tub_sales"),
    ("I want a freestanding tub",      "standalone_tub", "standalone_tub"),
    # "bathroom" must NOT be read as the tub word "bath".
    ("bathroom renovation, no plan",   "tub_sales", "none"),
    ("do you do toilets",              "tub_sales", "toilet"),
    # Non-tub intents are never touched.
    ("shower cubicle price",           "shower_cubicle", "shower_cubicle"),
]

for msg, llm_intent, expected in INTENT_CORRECTION_CASES:
    try:
        got = ResponseMixin._correct_service_intent(msg, llm_intent).get('intent')
        results.log(
            f"_correct_service_intent: '{msg[:38]}' [{llm_intent}]",
            got == expected,
            f"corrected to {got}",
            expected=expected,
            got=got,
        )
    except Exception as e:
        results.log(f"_correct_service_intent: '{msg[:38]}'", False, got=str(e))

# Guard against volunteering a price block on a carried-over intent that landed
# on a bare booking-field reply. The production bug: the area answer "Avondale"
# was classified as shower_cubicle and the bot dumped the cubicle price block.
from bot.whatsapp_webhook import _is_unprompted_carryover_pricing
_PRICING_AUTO = {
    'geyser', 'shower_cubicle', 'vanity', 'toilet', 'chamber',
    'drain_unblocking', 'pipe_repair', 'geyser_repair', 'toilet_repair',
    'facebook_package',
}
# (message, classified intent, price_requested, expected: should we SKIP the price?)
CARRYOVER_PRICING_CASES = [
    ("Avondale",               "shower_cubicle", False, True),   # the bug
    ("Hatfield",               "shower_cubicle", False, True),
    ("need to make arrangements", "shower_cubicle", False, True),
    ("shower cubicle",         "shower_cubicle", False, False),  # names product → price ok
    ("how much for a cubicle", "shower_cubicle", True,  False),  # price asked → price ok
    ("Avondale",               "none",           False, False),  # not a priceable intent
]
for msg, intent, price_req, expected in CARRYOVER_PRICING_CASES:
    try:
        got = _is_unprompted_carryover_pricing(intent, msg, price_req, _PRICING_AUTO)
        results.log(
            f"_is_unprompted_carryover_pricing: '{msg[:30]}' [{intent}]",
            got == expected,
            f"skip={got}",
            expected=f"skip={expected}",
            got=f"skip={got}",
        )
    except Exception as e:
        results.log(f"_is_unprompted_carryover_pricing: '{msg[:30]}'", False, got=str(e))

# A genuine question must break a delay holding pattern, not be force-fit as a
# timeframe answer. The production bug: "This one how much" (on a quoted tub
# photo) got re-asked "when are you hoping to get this sorted?" instead of priced.
from bot.out_of_scope_handler import _delay_breakout_inquiry
# (message, expected: should it BREAK OUT of the delay flow?)
DELAY_BREAKOUT_CASES = [
    ("This one how much",      True),   # the bug
    ("how much",               True),
    ("freestanding tub price", True),
    ("do you sell tubs",       True),
    ("next week",              False),  # real timeframe — stay in flow
    ("end of the month",       False),
    ("Thursday",               False),
    ("ok thanks",              False),
    ("jones86xi@gmail.com",    False),  # email capture, not a breakout
]
for msg, expected in DELAY_BREAKOUT_CASES:
    try:
        got = _delay_breakout_inquiry(msg)
        results.log(
            f"_delay_breakout_inquiry: '{msg[:30]}'",
            got == expected,
            f"breakout={got}",
            expected=f"breakout={expected}",
            got=f"breakout={got}",
        )
    except Exception as e:
        results.log(f"_delay_breakout_inquiry: '{msg[:30]}'", False, got=str(e))

# A demonstrative reply to a quoted portfolio photo ("this one?", "and this
# one?") must be treated as a price ask on the quoted item — otherwise it has no
# explicit price word, reads as a project description, and the price is skipped.
from bot.whatsapp_webhook import _is_quoted_item_reference
QUOTED_REF_CASES = [
    ("And this one?",            True),   # the production case
    ("this one",                 True),
    ("And this one how much",    True),
    ("how about this",           True),
    ("what about that one",      True),
    ("I want a full bathroom with this and a new toilet for the house", False),  # real desc
    ("avondale",                 False),
    ("next week",                False),
    ("yes",                      False),
]
for msg, expected in QUOTED_REF_CASES:
    try:
        got = _is_quoted_item_reference(msg)
        results.log(
            f"_is_quoted_item_reference: '{msg[:30]}'",
            got == expected,
            f"ref={got}",
            expected=f"ref={expected}",
            got=f"ref={got}",
        )
    except Exception as e:
        results.log(f"_is_quoted_item_reference: '{msg[:30]}'", False, got=str(e))

# When a customer asks the price of ONE photo they were sent, the bot answers
# that piece and then lists the other pieces from the same gallery so they can
# compare and choose. The list must: cover only images actually sent (by title),
# drop the piece just priced, dedupe shared prices, and quote catalogue prices
# verbatim. API-free: it's a deterministic lookup over the recorded media index.
from bot import portfolio_catalog as _pc
_CLAWFOOT = "Vintage Clawfoot Tub Bathroom"      # freestanding-tub priced piece
_VANITY = "Gold-Tap Double Vanity"               # another catalogued piece
_SENT_TITLES = [_CLAWFOOT, _VANITY, "Walk-In Rain Shower", "one of our previous work photos"]
try:
    _guide = _pc.build_sent_prices_list(_SENT_TITLES, exclude_title=_CLAWFOOT)
    _ok = bool(_guide)
    results.log("build_sent_prices_list: returns a guide", _ok, got=str(_guide)[:60])
    # The priced piece is excluded; the others are listed by title.
    results.log(
        "build_sent_prices_list: excludes the priced piece",
        _ok and _CLAWFOOT not in _guide,
        got=("present" if (_guide and _CLAWFOOT in _guide) else "excluded"),
    )
    results.log(
        "build_sent_prices_list: lists the other sent pieces",
        _ok and _VANITY in _guide and "Walk-In Rain Shower" in _guide,
        got=str(_guide)[:80],
    )
    # Uncatalogued shots (no matching title) carry no price → never listed.
    results.log(
        "build_sent_prices_list: skips uncatalogued shots",
        _ok and "previous work photos" not in _guide,
        got=str(_guide)[:80],
    )
    # Nothing else priceable left → None (don't append an empty guide).
    results.log(
        "build_sent_prices_list: None when nothing else to list",
        _pc.build_sent_prices_list([_CLAWFOOT], exclude_title=_CLAWFOOT) is None,
        got=str(_pc.build_sent_prices_list([_CLAWFOOT], exclude_title=_CLAWFOOT)),
    )
except Exception as e:
    results.log("build_sent_prices_list", False, got=str(e))

# ============================================================
# TEST 1: Service Inquiry Detection
# ============================================================

print("\n" + "="*60)
print("TEST 1: SERVICE INQUIRY DETECTION")
print("="*60)

appt = get_test_appointment()
bot = get_bot(appt)

# Test tub questions
for msg in TUB_QUESTIONS:
    try:
        result = bot.detect_service_inquiry(msg)
        intent = result.get('intent')
        confidence = result.get('confidence')
        passed = intent in ['standalone_tub', 'tub_sales', 'bathtub_installation'] and confidence == 'HIGH'
        results.log(
            f"detect_service_inquiry: '{msg[:40]}'",
            passed,
            f"intent={intent}, confidence={confidence}",
            expected="standalone_tub/tub_sales/bathtub_installation with HIGH confidence",
            got=f"{intent} ({confidence})"
        )
    except Exception as e:
        results.log(f"detect_service_inquiry: '{msg[:40]}'", False, got=str(e))

# Test pricing questions
for msg in PRICING_QUESTIONS:
    try:
        result = bot.detect_service_inquiry(msg)
        intent = result.get('intent')
        passed = intent != 'none'
        results.log(
            f"detect_pricing: '{msg[:40]}'",
            passed,
            f"intent={intent}",
            expected="any non-none intent",
            got=intent
        )
    except Exception as e:
        results.log(f"detect_pricing: '{msg[:40]}'", False, got=str(e))

# Test location questions
for msg in LOCATION_QUESTIONS:
    try:
        result = bot.detect_service_inquiry(msg)
        intent = result.get('intent')
        passed = intent == 'location_visit' and result.get('confidence') == 'HIGH'
        results.log(
            f"detect_location: '{msg[:40]}'",
            passed,
            f"intent={intent}",
            expected="location_visit HIGH",
            got=intent
        )
    except Exception as e:
        results.log(f"detect_location: '{msg[:40]}'", False, got=str(e))

# Test that generic messages do NOT trigger service inquiry
for msg in NON_SERVICE_MESSAGES:
    try:
        result = bot.detect_service_inquiry(msg)
        intent = result.get('intent')
        confidence = result.get('confidence')
        passed = intent == 'none' or confidence == 'LOW'
        results.log(
            f"detect_non_service: '{msg[:30]}'",
            passed,
            f"intent={intent}, confidence={confidence}",
            expected="none or LOW confidence",
            got=f"{intent} ({confidence})"
        )
    except Exception as e:
        results.log(f"detect_non_service: '{msg[:30]}'", False, got=str(e))

# ============================================================
# TEST 2: Pricing Responses Contain Key Info
# ============================================================

print("\n" + "="*60)
print("TEST 2: PRICING RESPONSE CONTENT")
print("="*60)

appt = get_test_appointment()
bot = get_bot(appt)

def check_response_quality(intent, response, checks):
    """Check response contains required elements."""
    all_passed = True
    for check in checks:
        if check.lower() not in response.lower():
            print(f"  ⚠️  Missing '{check}' in response")
            all_passed = False
    return all_passed

# Standalone tub — headlines the all-in US$670 (homebase.md source of truth)
# with the US$400 tub component shown, plus the approximate-price disclaimer.
resp = bot.handle_service_inquiry('standalone_tub', "standalone tub price")
checks = ['400', '670', 'US$', 'approximate', 'site visit']
passed = check_response_quality('standalone_tub', resp, checks)
results.log("pricing: standalone_tub contains US$670 all-in (US$400 component) + disclaimer", passed, got=resp[:120])

# Geyser
resp = bot.handle_service_inquiry('geyser', "geyser installation")
checks = ['80', 'US$', 'approximate']
passed = check_response_quality('geyser', resp, checks)
results.log("pricing: geyser contains US$80 + disclaimer", passed, got=resp[:100])

# Shower cubicle
resp = bot.handle_service_inquiry('shower_cubicle', "shower cubicle")
checks = ['130', '40', 'US$', '900mm', 'approximate']
passed = check_response_quality('shower_cubicle', resp, checks)
results.log("pricing: shower cubicle contains US$130 + US$40 + disclaimer", passed, got=resp[:100])

# Vanity
resp = bot.handle_service_inquiry('vanity', "vanity units")
checks = ['150', '30', 'US$', 'custom', 'approximate']
passed = check_response_quality('vanity', resp, checks)
results.log("pricing: vanity contains US$150 + US$30 + disclaimer", passed, got=resp[:100])

# Bathtub installation
resp = bot.handle_service_inquiry('bathtub_installation', "bathtub install")
checks = ['80', '450', '150', '120', 'US$', 'approximate']
passed = check_response_quality('bathtub_installation', resp, checks)
results.log("pricing: bathtub_installation contains all prices + disclaimer", passed, got=resp[:100])

# Toilet
resp = bot.handle_service_inquiry('toilet', "toilet installation")
checks = ['50', '20', 'US$', 'approximate']
passed = check_response_quality('toilet', resp, checks)
results.log("pricing: toilet contains US$50 + US$20 + disclaimer", passed, got=resp[:100])

# Facebook package
resp = bot.handle_service_inquiry('facebook_package', "bathroom on facebook ad")
checks = ['600', 'US$', 'approximate']
passed = check_response_quality('facebook_package', resp, checks)
results.log("pricing: facebook_package contains US$600 + disclaimer", passed, got=resp[:100])

# Location
resp = bot.handle_service_inquiry('location_visit', "where are you located")
checks = ['Hatfield', 'Harare', 'appointment']
passed = check_response_quality('location_visit', resp, checks)
results.log("location: contains Hatfield + appointment mention", passed, got=resp[:100])

# Tub sales - must NOT falsely claim retail; should qualify the tub type first
# (built-in vs freestanding) or clarify supply-and-install.
resp = bot.handle_service_inquiry('tub_sales', "do you sell tubs")
_r = resp.lower()
passed = (
    ('built-in' in _r and 'freestanding' in _r)
    or 'supply and install' in _r
    or 'retail' in _r
)
results.log("tub_sales: engages on tub types (built-in vs freestanding), no false retail claim", passed, got=resp[:150])

# ============================================================
# TEST 3: Disclaimer Attached to All Pricing
# ============================================================

print("\n" + "="*60)
print("TEST 3: MANDATORY DISCLAIMER ON ALL PRICING")
print("="*60)

appt = get_test_appointment()
bot = get_bot(appt)

pricing_intents = ['standalone_tub', 'geyser', 'shower_cubicle', 'vanity',
                   'bathtub_installation', 'toilet', 'facebook_package', 'tub_sales']

for intent in pricing_intents:
    resp = bot.handle_service_inquiry(intent, "price")
    has_disclaimer = 'approximate' in resp.lower() or 'may vary' in resp.lower()
    results.log(f"disclaimer present: {intent}", has_disclaimer, got=resp[-100:] if not has_disclaimer else "✓")

# ============================================================
# TEST 4: Shona Language Detection
# ============================================================

print("\n" + "="*60)
print("TEST 4: SHONA / MIXED LANGUAGE HANDLING")
print("="*60)

appt = get_test_appointment()
bot = get_bot(appt)

# "How much kuisa toilet" - from real Apt 54 - should detect toilet intent
result = bot.detect_service_inquiry("How much kuisa toilet")
intent = result.get('intent')
results.log(
    "shona: 'How much kuisa toilet' → toilet intent",
    intent == 'toilet',
    expected="toilet", got=intent
)

# "muri kupi imimi" - should detect location intent
result = bot.detect_service_inquiry("Ko when can I come ku office, muri kupi imimi")
intent = result.get('intent')
results.log(
    "shona: 'muri kupi imimi' → location_visit intent",
    intent == 'location_visit',
    expected="location_visit", got=intent
)

# "How much zvese zvakadai" - general pricing, not a specific intent
result = bot.detect_service_inquiry("How much zvese zvakadai")
intent = result.get('intent')
results.log(
    "shona: 'How much zvese zvakadai' → some pricing intent (not none)",
    intent != 'none',
    expected="pricing-related intent", got=intent
)

# ============================================================
# TEST 5: Plan Later Detection (the Site Visit bug)
# ============================================================

print("\n" + "="*60)
print("TEST 5: PLAN LATER DETECTION (Site Visit Bug Fix)")
print("="*60)

# These should NOT trigger "has plan" = True
should_NOT_be_plan_later = [
    "Site visit tomorrow",               # The bug from your logs!
    "A site visit would be ideal",       # Apt 55
    "A visit will do.l don't have a plan",  # Apt 75
    "Come tomorrow for the visit",
    "I do not have a plan",
    "Kwete, uye utarise",               # No, come and look (Shona)
]

# These SHOULD trigger "has plan" = True (will send later)
should_BE_plan_later = [
    "I'll send the plan later",
    "Let try to send the plan when I get home",  # Apt 58
    "Ok will do so tomorrow",           # Customer will send plan
    "Will send the pic",                # Apt 67
]

for msg in should_NOT_be_plan_later:
    appt = get_test_appointment()
    bot = get_bot(appt)
    result = bot.handle_plan_later_response(msg)
    results.log(
        f"NOT plan_later: '{msg[:40]}'",
        result == False,
        expected="False (not sending plan later)",
        got=str(result)
    )

for msg in should_BE_plan_later:
    appt = get_test_appointment()
    bot = get_bot(appt)
    result = bot.handle_plan_later_response(msg)
    results.log(
        f"IS plan_later: '{msg[:40]}'",
        result == True,
        expected="True (customer will send plan)",
        got=str(result)
    )

# ============================================================
# TEST 6: generate_response End-to-End (Real Scenarios)
# ============================================================

print("\n" + "="*60)
print("TEST 6: END-TO-END generate_response (Real Scenarios)")
print("="*60)

# Scenario A: Apt 66 replay - standalone tub question should get pricing
appt = get_test_appointment()
bot = get_bot(appt)
response = bot.generate_response("U have stand alone tub 1.5 hw much")
passed = 'US$' in response and ('400' in response or '450' in response)
results.log(
    "e2e: standalone tub question gets pricing (not generic response)",
    passed,
    got=response[:200]
)

# Scenario B: Apt 86 replay - location question should get Hatfield address
appt = get_test_appointment()
bot = get_bot(appt)
response = bot.generate_response("Where are you located")
passed = 'Hatfield' in response
results.log(
    "e2e: location question gets Hatfield in response",
    passed,
    got=response[:200]
)

# Scenario C: Apt 54 replay - "How much kuisa toilet" should get toilet pricing
appt = get_test_appointment()
bot = get_bot(appt)
response = bot.generate_response("How much kuisa toilet")
passed = 'US$' in response and ('50' in response or '20' in response)
results.log(
    "e2e: 'How much kuisa toilet' gets toilet pricing",
    passed,
    got=response[:200]
)

# Scenario D: Apt 75 - "A visit will do" should NOT loop on plan question
appt = get_test_appointment()
appt.project_type = 'bathroom_renovation'
appt.customer_area = 'Westgate'
appt.save()
bot = get_bot(appt)
response = bot.generate_response("A site visit will do, I do not have a plan")
# Should progress, not ask plan question again
passed = 'plan' not in response.lower() or 'already confirmed' in response.lower() or 'timeline' in response.lower() or 'property' in response.lower()
results.log(
    "e2e: 'site visit, no plan' does not ask plan question again",
    passed,
    got=response[:200]
)

# ============================================================
# TEST 7: Delay nudge never renders "None" (conv 421 — null-date)
# ============================================================

print("\n" + "="*60)
print("TEST 7: DELAY NUDGE DATE RENDERING (null-date fix, conv 421)")
print("="*60)

from urllib.parse import quote as _quote
from bot.management.commands.send_followups import Command as _FollowupCommand

_cmd = _FollowupCommand()

# Reproduce exactly what out_of_scope_handler._write_pending stores at delay_confirm:
# the original is url-encoded, so the "|iso" separator becomes %7C.
_iso = '2026-06-15'
_encoded = _quote(f'next week|{_iso}', safe='')
_notes = f'[OOS_PENDING] category=delay_confirm original={_encoded}'

_step, _friendly = _cmd._parse_delay_step(_notes)
results.log(
    "null-date: delay_confirm note decodes to the real follow-up date",
    _step == 'delay_confirm' and bool(_friendly) and 'June' in (_friendly or ''),
    expected="friendly date containing 'June' (e.g. 'Monday 15 June')",
    got=f"step={_step}, date={_friendly}",
)

# The customer-facing nudge body must contain the date and must NOT contain "None".
_template = _cmd._DELAY_NUDGE_MESSAGES['delay_confirm'][0]
_body = _template.format(date=_friendly)
results.log(
    "null-date: rendered nudge body must_include date, must_exclude 'None'",
    'None' not in _body and 'June' in _body,
    expected="contains real date, never the literal 'None'",
    got=_body,
)

# A note missing the iso part must yield no date, so the send guard skips it
# (rather than sending "reach out to you on None").
_bad_notes = f'[OOS_PENDING] category=delay_confirm original={_quote("next week", safe="")}'
_, _bad_friendly = _cmd._parse_delay_step(_bad_notes)
results.log(
    "null-date: missing iso yields no date so the nudge is skipped (not 'None')",
    _bad_friendly is None,
    expected="None (guard suppresses the {date} nudge)",
    got=str(_bad_friendly),
)

# ============================================================
# TEST 8: Follow-up scheduler state guard (conv 378 + 411)
# ============================================================

print("\n" + "="*60)
print("TEST 8: SCHEDULER STATE GUARD (conv 378 handed-off / parked / confirmed)")
print("="*60)

from django.utils import timezone as _tz
from datetime import timedelta as _td
import pytz as _pytz
_SA = _pytz.timezone('Africa/Johannesburg')
_now_local = _tz.now().astimezone(_SA)

_cmd2 = _FollowupCommand()

def _reset_guard_lead():
    g = get_test_appointment()
    g.internal_notes = ''
    g.is_delayed = False
    g.delay_followup_due_at = None
    g.chatbot_paused = False
    g.followup_stage = 'none'
    g.is_lead_active = True
    g.status = 'pending'
    g.last_customer_response = _tz.now() - _td(hours=5)
    g.save()
    return g

# A handed-off lead must be excluded by the shared state guard (conv 411)
_g = _reset_guard_lead()
_g.internal_notes = '[HANDED_OFF]'
_g.save(update_fields=['internal_notes'])
_kept = _cmd2._exclude_suppressed_states(Appointment.objects.filter(pk=_g.pk)).exists()
results.log("state-guard: [HANDED_OFF] lead suppressed from follow-ups (conv 411)",
            _kept is False, expected="excluded", got=f"kept={_kept}")

# A parked lead must be excluded by the shared state guard
_g.internal_notes = '[PARKED]'
_g.save(update_fields=['internal_notes'])
_kept = _cmd2._exclude_suppressed_states(Appointment.objects.filter(pk=_g.pk)).exists()
results.log("state-guard: [PARKED] lead suppressed from follow-ups",
            _kept is False, expected="excluded", got=f"kept={_kept}")

# A clean lead must NOT be excluded by the state guard
_g.internal_notes = ''
_g.save(update_fields=['internal_notes'])
_kept = _cmd2._exclude_suppressed_states(Appointment.objects.filter(pk=_g.pk)).exists()
results.log("state-guard: clean lead still eligible (no over-suppression)",
            _kept is True, expected="kept", got=f"kept={_kept}")

# A lead with an agreed future re-contact date is parked out of normal follow-ups (conv 378)
_g = _reset_guard_lead()
_g.delay_followup_due_at = _tz.now() + _td(days=3)
_g.save(update_fields=['delay_followup_due_at'])
_eligible_now = _cmd2._get_eligible_leads(_now_local, force=True)
results.log("state-guard: future delay date parks lead from normal follow-ups (conv 378)",
            not _eligible_now.filter(pk=_g.pk).exists(),
            expected="excluded from normal follow-ups", got="present" )

# Control: same lead with NO future date IS eligible for normal follow-ups
_g.delay_followup_due_at = None
_g.save(update_fields=['delay_followup_due_at'])
_eligible_now = _cmd2._get_eligible_leads(_now_local, force=True)
results.log("state-guard: lead without a parked date remains eligible",
            _eligible_now.filter(pk=_g.pk).exists(),
            expected="eligible", got="excluded")

# ============================================================
# TEST 9: Webhook dedup + lead-score idempotency (conv 369)
# ============================================================

print("\n" + "="*60)
print("TEST 9: WEBHOOK DEDUP / NO DOUBLE-COUNT (conv 369)")
print("="*60)

from bot.models import WhatsAppInboundEvent
from bot.services.lead_scoring import calculate_lead_score, refresh_lead_score
from django.db import IntegrityError as _IntegrityError, transaction as _txn

# 1) WAMID dedup is active: the same message_id can never be stored twice.
_wamid = 'wamid.TESTDEDUP369'
WhatsAppInboundEvent.objects.filter(message_id=_wamid).delete()
WhatsAppInboundEvent.objects.create(message_id=_wamid, sender='263000000000')
_second_insert_blocked = False
try:
    with _txn.atomic():
        WhatsAppInboundEvent.objects.create(message_id=_wamid, sender='263000000000')
except _IntegrityError:
    _second_insert_blocked = True
WhatsAppInboundEvent.objects.filter(message_id=_wamid).delete()
results.log(
    "webhook-dedup: duplicate message_id rejected by unique constraint",
    _second_insert_blocked, expected="IntegrityError on 2nd insert", got=str(_second_insert_blocked),
)

# 2) Lead score is idempotent: recomputing never inflates it (duplicates can't double-count).
_appt = get_test_appointment()
_appt.project_type = 'bathroom_renovation'
_appt.customer_area = 'Hatfield'
_appt.save()
_s1, _ = calculate_lead_score(_appt)
_s2, _ = calculate_lead_score(_appt)
refresh_lead_score(_appt); refresh_lead_score(_appt)
results.log(
    "webhook-dedup: lead score is idempotent (field-based, no per-message count)",
    _s1 == _s2 == _appt.lead_score,
    expected="stable score across recomputes", got=f"{_s1}/{_s2}/{_appt.lead_score}",
)

# 3) conversation_history never doubles a back-to-back identical inbound line.
_appt = get_test_appointment()  # resets conversation_history to []
_appt.add_conversation_message("user", "U have stand alone tub 1.5 hw much")
_appt.add_conversation_message("user", "U have stand alone tub 1.5 hw much")  # the double-add
_dupes = sum(
    1 for m in _appt.conversation_history
    if m.get("role") == "user" and m.get("content") == "U have stand alone tub 1.5 hw much"
)
results.log(
    "webhook-dedup: identical back-to-back user line stored once (conv 369)",
    _dupes == 1, expected="1 stored entry", got=f"{_dupes} entries",
)

# Control: a genuine repeat separated by an assistant reply is preserved.
_appt = get_test_appointment()
_appt.add_conversation_message("user", "ok")
_appt.add_conversation_message("assistant", "Great — what area are you in?")
_appt.add_conversation_message("user", "ok")
_ok_count = sum(1 for m in _appt.conversation_history if m.get("role") == "user" and m.get("content") == "ok")
results.log(
    "webhook-dedup: genuine repeat (separated by reply) is preserved",
    _ok_count == 2, expected="2 entries", got=f"{_ok_count} entries",
)

# ============================================================
# TEST 10: Delay intent split (conv 427 / 415 / 421 / 378)
# ============================================================

print("\n" + "="*60)
print("TEST 10: DELAY INTENT SPLIT (busy / access / travelling / brush-off)")
print("="*60)

from bot.out_of_scope_handler import (
    _delay_subtype_keywords, _DELAY_SUBTYPE_REPLIES, _has_travel_negation,
)

# conv 427: "We are not out of town but we go to work" must NOT be read as travel.
_sub = _delay_subtype_keywords("We are not out of town but we go to work")
results.log(
    "delay-split: 'not out of town but we go to work' -> busy (conv 427)",
    _sub == 'busy', expected="busy", got=_sub,
)
results.log(
    "delay-split: explicit travel negation detected (conv 427)",
    _has_travel_negation("We are not out of town but we go to work") is True,
    expected="True", got=str(_has_travel_negation("We are not out of town but we go to work")),
)

# Each distinct situation maps to its own sub-type.
for _msg, _want in [
    ("I'm abroad, will contact when I return", 'travelling'),
    ("I need to arrange access with my tenant first", 'access'),
    ("I work during the day so it's tricky", 'busy'),
    ("Maybe later, just saving your number for now", 'brush_off'),
]:
    _got = _delay_subtype_keywords(_msg)
    results.log(f"delay-split: '{_msg[:38]}' -> {_want}", _got == _want,
                expected=_want, got=_got)

# The busy and access replies must NOT assume travel ("back in town").
for _st in ('busy', 'access'):
    _r = _DELAY_SUBTYPE_REPLIES[_st]
    results.log(f"delay-split: '{_st}' reply does not assume travel",
                'back in town' not in _r.lower() and 'back?' not in _r.lower(),
                expected="no travel assumption", got=_r)

# The travelling reply is still allowed to ask when they'll be back.
results.log("delay-split: 'travelling' reply still asks about return",
            'back' in _DELAY_SUBTYPE_REPLIES['travelling'].lower(),
            expected="asks about return", got=_DELAY_SUBTYPE_REPLIES['travelling'])

# ============================================================
# TEST 11: Answer direct questions first (conv 369 / 411)
# ============================================================

print("\n" + "="*60)
print("TEST 11: ANSWER DIRECT QUESTIONS FIRST (identity, conv 369/411)")
print("="*60)

appt = get_test_appointment()
bot = get_bot(appt)

# "Who am I speaking to?" must be answered (Plumbot identity), not ignored.
_r = bot._maybe_answer_identity_question("Who am I speaking to?")
results.log(
    "direct-q: 'who am I speaking to?' is answered (conv 369)",
    _r is not None and ('plumbot' in _r.lower() or 'homebase' in _r.lower()),
    expected="identity answer mentioning Plumbot/Homebase", got=str(_r),
)

# "Which plumber is coming?" must name/route to Tinashe (protected handoff).
_r = bot._maybe_answer_identity_question("Which plumber is coming to my house?")
results.log(
    "direct-q: 'which plumber is coming?' names the plumber (conv 369)",
    _r is not None and 'tinashe' in _r.lower(),
    expected="answer naming Tinashe", got=str(_r),
)

# A normal booking message must NOT trigger the identity handler (no over-reach).
_r = bot._maybe_answer_identity_question("I need a geyser installed in Hatfield")
results.log(
    "direct-q: non-identity message is not hijacked",
    _r is None, expected="None", got=str(_r),
)

# ============================================================
# TEST 12: Adaptive tub pricing (conv 427)
# ============================================================

print("\n" + "="*60)
print("TEST 12: ADAPTIVE TUB PRICING (built-in vs freestanding, conv 427)")
print("="*60)

appt = get_test_appointment()
bot = get_bot(appt)

# Type detection from the customer's wording.
results.log("adaptive-pricing: 'built-in tub' detected as built_in",
            bot._tub_type_in_message("how much for a built-in tub") == 'built_in',
            expected="built_in", got=str(bot._tub_type_in_message("how much for a built-in tub")))
results.log("adaptive-pricing: 'freestanding tub' detected as freestanding",
            bot._tub_type_in_message("price of a freestanding tub") == 'freestanding',
            expected="freestanding", got=str(bot._tub_type_in_message("price of a freestanding tub")))
results.log("adaptive-pricing: plain 'a tub' has no specific type",
            bot._tub_type_in_message("how much for a tub") is None,
            expected="None", got=str(bot._tub_type_in_message("how much for a tub")))

# When the customer asked about a built-in tub, the reply must LEAD with built-in
# (US$160), not the freestanding US$400.
_r = bot._tub_price_reply('built_in', 'english')
_built_idx = _r.lower().find('built-in')
_free_idx = _r.lower().find('freestanding')
results.log("adaptive-pricing: built-in question leads with built-in price (conv 427)",
            '160' in _r and _built_idx != -1 and (_free_idx == -1 or _built_idx < _free_idx),
            expected="built-in (US$160) leads", got=_r)

# Freestanding/unspecified still leads with freestanding, headlined at the
# all-in US$670 (homebase.md source of truth) with the US$400 tub component shown.
_r = bot._tub_price_reply('freestanding', 'english')
results.log("adaptive-pricing: freestanding leads with all-in US$670 (US$400 tub component shown)",
            '670' in _r and '400' in _r and _r.lower().find('freestanding') < _r.lower().find('standard'),
            expected="freestanding (US$670 all-in) leads", got=_r)

# ============================================================
# TEST 13: Input-format validation at the name step (conv 410)
# ============================================================

print("\n" + "="*60)
print("TEST 13: INPUT FORMAT VALIDATION (email at name step, conv 410)")
print("="*60)

bot = get_bot(get_test_appointment())
# Reset the row the bot actually operates on (Plumbot resolves its own appointment).
bot.appointment.customer_name = None
bot.appointment.customer_email = None
bot.appointment.conversation_history = []
bot.appointment.save()

_r = bot._handle_name_step("john.doe@example.com", updated_fields=[])
# Must NOT be the bare name re-ask; must acknowledge the email and ask the name.
results.log(
    "input-format: email at name step is captured + name asked (conv 410)",
    'email' in _r.lower() and 'name' in _r.lower() and 'one last thing' not in _r.lower(),
    expected="acknowledges email, asks name", got=_r,
)
bot.appointment.refresh_from_db()
results.log(
    "input-format: email stored when typed at the name step",
    bot.appointment.customer_email == "john.doe@example.com",
    expected="john.doe@example.com", got=str(bot.appointment.customer_email),
)

# A real name at the name step is still handled normally (no over-reach).
bot = get_bot(get_test_appointment())
bot.appointment.customer_name = None
bot.appointment.customer_email = None
bot.appointment.save()
_r2 = bot._handle_name_step("Tapiwa", updated_fields=[])
results.log(
    "input-format: a normal name is still accepted",
    'email' in _r2.lower() or 'confirm' in _r2.lower(),  # proceeds to email/confirm step
    expected="proceeds past the name step", got=_r2,
)

# ============================================================
# SUMMARY
# ============================================================

print("\n" + "="*60)
print("TEST SUMMARY")
print("="*60)
total = results.passed + results.failed
print(f"✅ Passed: {results.passed}/{total}")
print(f"❌ Failed: {results.failed}/{total}")

if results.errors:
    print("\nFailed Tests:")
    for err in results.errors:
        print(f"  • {err}")

print("="*60)