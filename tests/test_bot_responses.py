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
    # Reset for clean test
    appt.project_type = None
    appt.has_plan = None
    appt.customer_area = None
    appt.conversation_history = []
    appt.save()
    return appt


def get_bot(appt):
    return Plumbot(appt)


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

# Standalone tub
resp = bot.handle_service_inquiry('standalone_tub', "standalone tub price")
checks = ['400', 'US$', 'approximate', 'plan', 'site visit']
passed = check_response_quality('standalone_tub', resp, checks)
results.log("pricing: standalone_tub contains US$400 + disclaimer", passed, got=resp[:100])

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

# Tub sales - should NOT claim we sell retail
resp = bot.handle_service_inquiry('tub_sales', "do you sell tubs")
passed = 'retail' in resp.lower() or "don't operate as a retail" in resp.lower() or "supply and install" in resp.lower()
results.log("tub_sales: clarifies not a retail store + supply+install", passed, got=resp[:150])

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