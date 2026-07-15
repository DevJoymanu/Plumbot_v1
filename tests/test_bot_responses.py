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

# ── Run modes ────────────────────────────────────────────────────────────────
# PLUMBOT_GATE=1        → run ONLY the deterministic TEST 0 regression block and
#                         exit non-zero on any failure. This is the commit gate:
#                         fast, offline, and meaningful (no flaky live-LLM tests).
# PLUMBOT_MOCK_DEEPSEEK=1 → replace the DeepSeek client with a deterministic stub
#                         so the FULL suite runs offline without flaky live calls.
# Gate mode implies the mock so it never touches the network.
GATE_ONLY = os.environ.get('PLUMBOT_GATE') == '1' or '--gate' in sys.argv
if GATE_ONLY or os.environ.get('PLUMBOT_MOCK_DEEPSEEK') == '1':
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from deepseek_mock import install as _install_ds_mock
    _install_ds_mock()

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


def _finish():
    """Print the summary and exit non-zero on any failure, so this script can
    gate a commit / CI run. Without an exit code a 'failing' suite still returns
    0 and nothing stops a regression from shipping."""
    print("\n" + "=" * 60)
    print("TEST SUMMARY" + ("  (GATE — deterministic only)" if GATE_ONLY else ""))
    print("=" * 60)
    total = results.passed + results.failed
    print(f"✅ Passed: {results.passed}/{total}")
    print(f"❌ Failed: {results.failed}/{total}")
    if results.errors:
        print("\nFailed Tests:")
        for err in results.errors:
            print(f"  • {err}")
    print("=" * 60)
    sys.exit(1 if results.failed else 0)


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
    # Wall-mounted toilet = the chamber install (US$160 all-in), never
    # toilet-seat pricing (prod: "wall mounted toilet system" → US$70 seat block).
    ("install a wall mounted toilet system", "tub_sales", "wall_hung_toilet"),
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

# Wall-mounted / wall-hung toilet must resolve to the chamber-rate intent
# (US$160 all-in), never the generic 'toilet' seat block. The production bug:
# "How much is the charge for installing a wall mounted toilet system?" was
# answered with toilet-seat pricing (US$50 + US$20). Plain toilet asks are
# unchanged.
from bot.whatsapp_webhook import _keyword_product_intent
WALL_HUNG_TOILET_CASES = [
    ("How much is the charge for installing a wall mounted toilet system?",
     "wall_hung_toilet"),                                     # the bug verbatim
    ("price for a wall-hung toilet",        "wall_hung_toilet"),
    ("wall hung toilet installation cost",  "wall_hung_toilet"),
    ("concealed toilet system how much",    "wall_hung_toilet"),
    ("how much is a toilet",                "toilet"),   # plain ask unchanged
    ("toilet seat replacement price",       "toilet"),
    ("my toilet is leaking, can you fix it", "toilet_repair"),
]
for msg, expected in WALL_HUNG_TOILET_CASES:
    try:
        got = _keyword_product_intent(msg)
        results.log(
            f"_keyword_product_intent: '{msg[:38]}'",
            got == expected,
            f"resolved to {got}",
            expected=expected,
            got=got,
        )
    except Exception as e:
        results.log(f"_keyword_product_intent: '{msg[:38]}'", False, got=str(e))

# Guard against volunteering a price block on a carried-over intent that landed
# on a bare booking-field reply. The production bug: the area answer "Avondale"
# was classified as shower_cubicle and the bot dumped the cubicle price block.
from bot.whatsapp_webhook import _is_unprompted_carryover_pricing
_PRICING_AUTO = {
    'geyser', 'shower_cubicle', 'vanity', 'toilet', 'chamber',
    'wall_hung_toilet',
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
    ("I want to purchase 2x shower cubicles and asseries", True),  # buying signal breaks email step (appt 472)
    # Brush-off isolate question ("is it the price, timing, or something else?")
    # answers: a price answer must break out to the price tie-down handler;
    # a timing/other answer stays in the delay flow.
    ("it's the price",         True),
    ("the price",              True),
    ("the timing",             False),
    ("something else",         False),
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
    ("what about this one",      True),   # quoting a 2nd photo — must beat already-sent gate
    ("how much is this one",     True),   # quoting a 3rd photo — same
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

# Service-area gate: the business is MOBILE and travels Zimbabwe-wide; it
# declines only a short list of far cities (Gweru, Bulawayo, Mutare, Masvingo,
# Victoria Falls, Hwange, Beitbridge, Plumtree). Everywhere else — including
# Hurungwe/Magunje, Kariba, Chinhoyi — is serviceable. This pins the
# deterministic keyword fallback (AI is the primary path live). The negation
# fix still matters: a bare 'harare' in "not in Harare …" must not trip the
# shortcut, but a non-declined town there is serviceable, not declined.
# True = declined / out of service area.
from bot.views.plumbot.state_mixin import StateMixin
EXCLUDED_CITY_CASES = [
    # Decline list → out of area.
    ("Bulawayo",                   True),
    ("I'm in Gweru",               True),
    ("Mutare",                     True),
    ("Masvingo",                   True),
    ("Victoria Falls",             True),
    ("not in harare, in bulawayo", True),   # negated Harare + a declined city
    # Mobile coverage → serviceable (the Magunje correction).
    ("Not in Harare but in Hurungwe (Magunje) to be precise.", False),
    ("Hurungwe",                   False),
    ("Magunje",                    False),
    ("Kariba",                     False),
    ("Chinhoyi",                   False),
    ("outside Harare, in Chinhoyi", False),
    # Harare areas, unchanged.
    ("Avondale",                   False),
    ("Hatfield",                   False),
    ("Harare",                     False),
    ("Borrowdale, Harare",         False),
    ("Bulawayo Road",              False),  # a street in Harare, not the city
    ("Harare Mutare Road",         False),
]
for area, expected_excluded in EXCLUDED_CITY_CASES:
    try:
        got = StateMixin._is_excluded_city_keywords(area)
        is_excluded = got is not None
        results.log(
            f"_is_excluded_city_keywords: '{area[:38]}'",
            is_excluded == expected_excluded,
            f"-> {got!r}",
            expected=f"excluded={expected_excluded}",
            got=f"excluded={is_excluded} ({got!r})",
        )
    except Exception as e:
        results.log(f"_is_excluded_city_keywords: '{area[:38]}'", False, got=str(e))

# When a customer asks the price of ONE photo they were sent ("this one how
# much" on a quoted image), the bot replies with the full pricing for that piece
# — every item in the shot, verbatim from the catalogue. Single- and multi-item
# photos alike get a guide; only uncatalogued shots return None.
# API-free: a deterministic title lookup over the catalogue.
from bot import portfolio_catalog as _pc
_BUNDLE = "Black Granite Vanity & Designer Tub"  # quoted photo: vanity + tub
_SINGLE = "Walk-In Rain Shower"                  # single priced item + upsell
_TUB_TOILET = "Freestanding Tub & Wall-Hung Toilet"  # tub + wall-hung toilet
try:
    _guide = _pc.build_item_price_guide(_BUNDLE)
    _ok = bool(_guide)
    results.log("build_item_price_guide: guide for a multi-item photo", _ok, got=str(_guide)[:60])
    # Both items in the bundled shot are priced (the classifier-derived intent
    # alone would have priced only one of them).
    results.log(
        "build_item_price_guide: prices every item in the bundle",
        _ok and "tub" in _guide.lower() and "vanity" in _guide.lower(),
        got=str(_guide)[:90],
    )
    # Verbatim catalogue price — never invent figures.
    results.log(
        "build_item_price_guide: quotes catalogue price verbatim",
        _ok and _pc.get_item_by_title(_BUNDLE)['price'] in _guide,
        got=str(_guide)[:90],
    )
    # Every item shown must be priced: the tub-and-wall-hung-toilet photo prices
    # the toilet too (at the side-chamber rate, US$160), not the tub alone.
    _tt = _pc.build_item_price_guide(_TUB_TOILET)
    results.log(
        "build_item_price_guide: prices the wall-hung toilet in the shot",
        bool(_tt) and "toilet" in _tt.lower() and "US$160" in _tt,
        got=str(_tt)[:100],
    )
    # The toilet-and-basin photo prices the standalone basin too (US$70), not the
    # toilet alone — every item shown carries a price.
    _tb = _pc.build_item_price_guide("Classic Toilet & Basin Suite")
    results.log(
        "build_item_price_guide: prices the basin in the toilet-and-basin shot",
        bool(_tb) and "basin" in _tb.lower() and "US$70" in _tb,
        got=str(_tb)[:100],
    )
    # A single-product photo still gets its own full-pricing guide (we now lead
    # with it, so there's no redundant block to suppress).
    results.log(
        "build_item_price_guide: guide for a single-product photo",
        bool(_pc.build_item_price_guide(_SINGLE)),
        got=str(_pc.build_item_price_guide(_SINGLE))[:90],
    )
    # Uncatalogued shots (tidied filename, no matching title) carry no price.
    results.log(
        "build_item_price_guide: None for uncatalogued shot",
        _pc.build_item_price_guide("one of our previous work photos") is None,
        got=str(_pc.build_item_price_guide("one of our previous work photos")),
    )
except Exception as e:
    results.log("build_item_price_guide", False, got=str(e))

# The quoted-photo reply leads with the full price and closes with a
# visit-capture line — it must NOT open with the generic "we supply both..."
# affirm preamble, and it must not re-ask for the area once we have it.
# API-free: build_item_price_guide + attribute checks, no model calls.
class _FakeAppt:
    def __init__(self, area=None, has_plan=None):
        self.customer_area = area
        self.has_plan = has_plan
class _FakeSelf:
    def __init__(self, appt):
        self.appointment = appt
try:
    _r = ResponseMixin.compose_quoted_photo_price_reply(_FakeSelf(_FakeAppt()), _BUNDLE, "english")
    results.log(
        "compose_quoted_photo_price_reply: leads with the full pricing",
        bool(_r) and _r.startswith("Here's the full pricing for that piece"),
        got=str(_r)[:60],
    )
    results.log(
        "compose_quoted_photo_price_reply: no affirm preamble",
        bool(_r) and "we supply both" not in _r.lower(),
        got=str(_r)[:60],
    )
    results.log(
        "compose_quoted_photo_price_reply: asks area with accurate-free-quote close",
        bool(_r) and "accurate free quote" in _r.lower(),
        got=str(_r)[-80:],
    )
    # Area already known → don't re-ask for it (no bot loop).
    _rc = ResponseMixin.compose_quoted_photo_price_reply(_FakeSelf(_FakeAppt(area="Avondale")), _BUNDLE, "english")
    results.log(
        "compose_quoted_photo_price_reply: no area re-ask once committed",
        bool(_rc) and "what area are you in" not in _rc.lower(),
        got=str(_rc)[-80:],
    )
    # Uncatalogued quoted shot → None so the caller falls back.
    results.log(
        "compose_quoted_photo_price_reply: None for uncatalogued shot",
        ResponseMixin.compose_quoted_photo_price_reply(_FakeSelf(_FakeAppt()), "mystery photo", "english") is None,
        got="ok",
    )
except Exception as e:
    results.log("compose_quoted_photo_price_reply", False, got=str(e))

# Timeframe extraction is AI-first live (_extract_followup_date_ai, guided by a
# system prompt), with _compute_followup_date_keywords as the deterministic
# fallback that keeps the bot working when the API is down — and which powers
# this offline gate (the mock returns "{}", so the AI layer yields None and the
# wrapper falls through to the parser). These cases pin that safety net.
#
# A bare month name ("August") must resolve to a concrete future date — not
# crash the parse and leave the bot re-asking forever (production: appt 465).
from bot.out_of_scope_handler import (
    _compute_followup_date, _compute_followup_date_keywords, _message_has_timeframe,
)
from datetime import date as _date_t
MONTH_TIMEFRAME_CASES = [
    "August", "in august", "around July", "Sept", "by December", "maybe October",
]
for msg in MONTH_TIMEFRAME_CASES:
    try:
        iso, friendly = _compute_followup_date_keywords(msg)
        ok = bool(iso) and bool(friendly)
        # Must be a valid future ISO date, never None/empty.
        if ok:
            ok = _date_t.fromisoformat(iso) >= _date_t.today()
        results.log(
            f"_compute_followup_date_keywords (month): '{msg[:20]}'",
            ok,
            f"iso={iso} friendly={friendly}",
            expected="a valid future date",
            got=f"iso={iso}",
        )
    except Exception as e:
        results.log(f"_compute_followup_date_keywords (month): '{msg[:20]}'", False, got=str(e))

# "weekend" must resolve to the upcoming Saturday in the deterministic fallback
# too — not loop the same re-ask. Production (Graylands park lead): "Most
# probably during the weekend" failed to parse, the bot repeated "roughly when?"
# twice, and a human had to step in.
WEEKEND_TIMEFRAME_CASES = [
    "Most probably during the weekend, l will get in touch.",
    "this weekend", "over the weekend", "on the weekend", "next weekend",
]
for msg in WEEKEND_TIMEFRAME_CASES:
    try:
        iso, friendly = _compute_followup_date_keywords(msg)
        ok = bool(iso) and bool(friendly)
        if ok:
            d = _date_t.fromisoformat(iso)
            ok = d >= _date_t.today() and d.weekday() == 5  # a future Saturday
        # _message_has_timeframe must also flag it (skips the re-ask entirely).
        ok = ok and _message_has_timeframe(msg)
        results.log(
            f"_compute_followup_date_keywords (weekend): '{msg[:24]}'",
            ok,
            f"iso={iso} friendly={friendly}",
            expected="a future Saturday + has_timeframe=True",
            got=f"iso={iso}",
        )
    except Exception as e:
        results.log(f"_compute_followup_date_keywords (weekend): '{msg[:24]}'", False, got=str(e))

# A NEAR timeframe (<= 7 days) is readiness, not a deferral: it must steer to
# booking the visit, while anything further out keeps the parked-lead workflow.
from bot.out_of_scope_handler import _timeframe_is_near
from datetime import timedelta as _td_t
NEAR_FAR_CASES = [
    ((_date_t.today()).isoformat(),                    True),   # today
    ((_date_t.today() + _td_t(days=1)).isoformat(),    True),   # tomorrow
    ((_date_t.today() + _td_t(days=7)).isoformat(),    True),   # one week — boundary
    ((_date_t.today() + _td_t(days=8)).isoformat(),    False),  # just over a week
    ((_date_t.today() + _td_t(days=30)).isoformat(),   False),  # next month
    ((_date_t.today() - _td_t(days=2)).isoformat(),    False),  # past date — not near
    ("not-a-date",                                     False),  # unparseable
]
for iso, expected in NEAR_FAR_CASES:
    try:
        got = _timeframe_is_near(iso)
        results.log(
            f"_timeframe_is_near: '{iso}'",
            got == expected,
            expected=str(expected),
            got=str(got),
        )
    except Exception as e:
        results.log(f"_timeframe_is_near: '{iso}'", False, got=str(e))

# End to end: a deflected lead who answers the timeframe with a NEAR date must be
# pivoted to booking the visit (asks day/time, mentions the assessment) — NOT
# parked with a "check back on …" reminder.
# A specific day must NOT be re-asked (only the time); a vague near range still
# pins the day. Specific-day detection is deterministic.
from bot.out_of_scope_handler import _timeframe_names_specific_day
SPECIFIC_DAY_CASES = [
    ("tomorrow", True), ("today", True), ("this Friday", True),
    ("next Monday", True), ("the 26th", True), ("on 26/6", True),
    ("this week", False), ("this weekend", False), ("next weekend", False),
    ("soon", False), ("in a few days", False),
]
for msg, expected in SPECIFIC_DAY_CASES:
    try:
        got = _timeframe_names_specific_day(msg)
        results.log(
            f"_timeframe_names_specific_day: '{msg}'",
            got == expected, expected=str(expected), got=str(got),
        )
    except Exception as e:
        results.log(f"_timeframe_names_specific_day: '{msg}'", False, got=str(e))

# End to end: NEAR date pivots to booking (casual 20-min look, not parked).
# A named day asks only the time; a vague weekend still asks the day.
from bot.out_of_scope_handler import (
    _handle_delay_timeframe_answer, _is_self_initiated_defer,
    _is_self_initiated_defer_keywords,
)
class _FakeApptTf:
    internal_notes = ''
    customer_email = None
    project_type = 'bathroom_renovation'
    delay_followup_due_at = None
    def save(self, update_fields=None):
        pass
    def mark_delayed(self, source_message='', save=True):
        return True
    def unpark(self, save=True):
        return False
try:
    _specific = _handle_delay_timeframe_answer("tomorrow", {}, _FakeApptTf())
    results.log(
        "delay timeframe: NEAR specific day -> asks time only, casual visit, not parked",
        ("What time suits you" in _specific and "20 minutes" in _specific
         and "quick look at the bathroom" in _specific
         and "day and time" not in _specific and "check back on" not in _specific),
        got=_specific,
    )
    _vague = _handle_delay_timeframe_answer("this weekend", {}, _FakeApptTf())
    results.log(
        "delay timeframe: NEAR vague range -> still asks the day, casual visit",
        ("day and time" in _vague and "20 minutes" in _vague
         and "check back on" not in _vague),
        got=_vague,
    )
    # Self-initiated deferral ("I'll get in touch") — even with a NEAR timeframe,
    # respect it. Park gracefully (check-back date + email offer), do NOT push a
    # day/time. Production: "Most probably during the weekend, l will get in touch."
    # This exercises the AI-primary _is_self_initiated_defer via the gate's mock.
    _defer = _handle_delay_timeframe_answer(
        "Most probably during the weekend, l will get in touch.", {}, _FakeApptTf())
    results.log(
        "delay timeframe: self-initiated defer -> parked, no booking push",
        ("check back on" in _defer.lower()
         and "day and time" not in _defer
         and "what time suits you" not in _defer.lower()),
        got=_defer,
    )
    # The keyword fallback (used when DeepSeek is down) must stand on its own.
    SELF_DEFER_CASES = [
        ("Most probably during the weekend, l will get in touch.", True),
        ("I'll get back to you", True),
        ("let me get back to you next week", True),
        ("I'll let you know", True),
        ("I'll reach out once I'm ready", True),
        ("I will contact you soon", True),
        # Bare forms with no leading "I'll" (prod 2026-07-02: two successive
        # timeframe asks instead of the email pivot):
        ("Will advise.", True),
        ("Will contact you.", True),
        ("this weekend works", False),
        ("tomorrow at 2pm", False),
        ("come on Friday", False),
    ]
    _sd_ok = all(_is_self_initiated_defer_keywords(m) is e for m, e in SELF_DEFER_CASES)
    results.log(
        "self-initiated defer (keyword fallback): 'I'll get in touch' yes, plain timeframe no",
        _sd_ok,
        got="; ".join(f"{m[:22]!r}->{_is_self_initiated_defer_keywords(m)}"
                      for m, e in SELF_DEFER_CASES),
    )
    # Access-arranging deferral is detected deterministically (conv 427: "No one
    # will be home..need to make arrangements" lost the access check-in to a
    # nondeterministic category classification on some runs).
    from bot.out_of_scope_handler import _is_access_deferral_keywords
    ACCESS_CASES = [
        ("No one will be home..need to make arrangements", True),
        ("nobody will be home tomorrow", True),
        ("I need to arrange access with my tenant", True),
        ("this weekend works", False),
        ("I'll get back to you", False),
        ("Bathroom renovation", False),
    ]
    results.log(
        "access deferral keywords: access phrases yes, ordinary messages no",
        all(_is_access_deferral_keywords(m) is e for m, e in ACCESS_CASES),
        got="; ".join(f"{m[:24]!r}->{_is_access_deferral_keywords(m)}"
                      for m, e in ACCESS_CASES if _is_access_deferral_keywords(m) is not e)
            or "all as expected",
    )
except Exception as e:
    results.log("delay timeframe NEAR pivot", False, got=str(e))

# The AI-first wrapper must still yield a date offline by falling through to the
# deterministic parser (proves the fallback is wired, not just the AI path).
for msg in ("next week", "August", "this weekend"):
    try:
        iso, _f = _compute_followup_date(msg)
        ok = bool(iso) and _date_t.fromisoformat(iso) >= _date_t.today()
        results.log(
            f"_compute_followup_date wrapper falls back offline: '{msg}'",
            ok, f"iso={iso}", expected="a valid future date", got=f"iso={iso}",
        )
    except Exception as e:
        results.log(f"_compute_followup_date wrapper falls back offline: '{msg}'", False, got=str(e))

# Vague-deferral flow ("will call you"): when no timeframe is given we auto-set a
# 2-week follow-up date, and after sending the PDF on WhatsApp we schedule ONE
# contextual check-in in the LAST stretch of the lead's free-form window — 2h
# before close for ~24h organic windows, 4h before close for 72h ad windows,
# clamped into 08:00–20:00 SAST contact hours. EVERY refused-email delay lead
# gets it now (the old 2pm/2-days rule skipped 24h leads). Plus the AI-first
# email-step intent classifier's deterministic fallback contract.
import types as _types
import pytz as _pytz
from datetime import datetime as _dt_t, timedelta as _td_t
from bot.out_of_scope_handler import (
    _default_followup_iso, _compute_window_close_checkin,
    _email_step_intent_keywords, _classify_email_step_reply,
)
_sast = _pytz.timezone('Africa/Johannesburg')
_now_fixed = _sast.localize(_dt_t(2026, 6, 24, 10, 0))
try:
    _iso2w = _default_followup_iso(now=_now_fixed)
    results.log("_default_followup_iso: 2 weeks out",
                _iso2w == '2026-07-08', got=_iso2w, expected='2026-07-08')
except Exception as e:
    results.log("_default_followup_iso: 2 weeks out", False, got=str(e))

def _wcc(closes_at, now):
    return _compute_window_close_checkin(
        _types.SimpleNamespace(messaging_window_closes_at=closes_at), now=now)

WINDOW_CHECKIN_CASES = [
    # (label, closes_at, now, expected datetime-or-None)
    ("organic 24h → 2h before close",
     _now_fixed + _td_t(hours=24), _now_fixed,
     _sast.localize(_dt_t(2026, 6, 25, 8, 0))),
    ("ad 72h → 4h before close, pre-dawn pulls to prior evening 19:30",
     _now_fixed + _td_t(hours=72), _now_fixed,
     _sast.localize(_dt_t(2026, 6, 26, 19, 30))),
    ("late-night close → clamps to 19:30",
     _sast.localize(_dt_t(2026, 6, 25, 23, 0)), _sast.localize(_dt_t(2026, 6, 24, 23, 0)),
     _sast.localize(_dt_t(2026, 6, 25, 19, 30))),
    ("window nearly shut (1h left) → None",
     _now_fixed + _td_t(hours=1), _now_fixed, None),
    ("2h left → near-term touch now+45min",
     _now_fixed + _td_t(hours=2), _now_fixed,
     _now_fixed + _td_t(minutes=45)),
    ("no window info → None", None, _now_fixed, None),
]
for _label, _closes, _now_c, _expected in WINDOW_CHECKIN_CASES:
    try:
        _got = _wcc(_closes, _now_c)
        results.log(f"_compute_window_close_checkin: {_label}",
                    _got == _expected, got=str(_got), expected=str(_expected))
    except Exception as e:
        results.log(f"_compute_window_close_checkin: {_label}", False, got=str(e))

EMAIL_STEP_KW_CASES = [
    ("jones86xi@gmail.com",            "email"),
    ("just send it here on whatsapp",  "whatsapp"),
    ("send it here",                   "whatsapp"),
    ("no thanks",                      "decline"),
    ("skip",                           "decline"),
    ("I'd rather not",                 "decline"),
    ("maybe",                          "unclear"),
]
for msg, exp in EMAIL_STEP_KW_CASES:
    try:
        got = _email_step_intent_keywords(msg)
        results.log(f"_email_step_intent_keywords: '{msg[:24]}'",
                    got == exp, got=got, expected=exp)
    except Exception as e:
        results.log(f"_email_step_intent_keywords: '{msg[:24]}'", False, got=str(e))

# An actual address must classify as 'email' deterministically (never an API call);
# a decline falls back to keywords offline.
try:
    results.log("_classify_email_step_reply: address → email",
                _classify_email_step_reply("jones86xi@gmail.com") == "email", got="ok")
    results.log("_classify_email_step_reply: 'skip' → decline (kw fallback)",
                _classify_email_step_reply("skip") == "decline", got="ok")
except Exception as e:
    results.log("_classify_email_step_reply", False, got=str(e))

# A malformed email reply is routed to DeepSeek for a contextual reply / salvage
# instead of a canned line. Offline (gate), the helper must still return a
# tuple with NO bad email and a NON-empty reply (the bot must never go silent),
# and any salvaged email it does return must be a valid address.
from bot.out_of_scope_handler import _resolve_email_attempt_ai
try:
    _salv, _reply = _resolve_email_attempt_ai("jon at gmail dot com")
    ok = (_salv is None and isinstance(_reply, str) and len(_reply.strip()) > 0)
    if _salv is not None:  # if a live model salvaged one, it must be valid
        import re as _re_t
        ok = bool(_re_t.fullmatch(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', _salv))
    results.log("_resolve_email_attempt_ai: offline → non-empty reply, no bad email",
                ok, got=f"email={_salv!r} reply_len={len(_reply or '')}")
except Exception as e:
    results.log("_resolve_email_attempt_ai", False, got=str(e))

# Yes/no reply classification (delay confirm / check-in) is AI-primary live; this
# pins the deterministic keyword fallback. Affirmation is checked before the 'no'
# substring so "no problem" (agreement) isn't mis-read as a refusal.
from bot.out_of_scope_handler import _classify_affirmation_keywords
AFFIRM_CASES = [
    ("yes",             "yes"),
    ("ok that works",   "yes"),
    ("hongu",           "yes"),
    ("please do",       "yes"),
    ("no problem",      "yes"),     # agreement, not a refusal
    ("no",              "no"),
    ("nope",            "no"),
    ("kwete",           "no"),
    ("I'd rather not",  "no"),
    ("let you know",    "no"),
    ("maybe next week", "unclear"),
    ("hmm",             "unclear"),
]
for msg, exp in AFFIRM_CASES:
    try:
        got = _classify_affirmation_keywords(msg)
        results.log(f"_classify_affirmation_keywords: '{msg[:22]}'",
                    got == exp, got=got, expected=exp)
    except Exception as e:
        results.log(f"_classify_affirmation_keywords: '{msg[:22]}'", False, got=str(e))

# Meta's 131047 ("Re-engagement message") is authoritative: a CTWA lead's 72h
# window is only our local assumption. Once the closed flag is set, the free-form
# window must read closed regardless of ctwa_entry_at, so the follow-up cron stops
# firing doomed sends (no paid template fallback). It reopens when the customer
# replies (mark_customer_response clears the flag).
from bot.models import Appointment as _Appt
from django.utils import timezone as _dj_tz
try:
    _open_ctwa = _Appt(ctwa_entry_at=_dj_tz.now() - _td_t(hours=1))
    results.log("messaging_window_open: fresh CTWA lead (72h) → open",
                _open_ctwa.messaging_window_open is True,
                got=str(_open_ctwa.messaging_window_open), expected="True")

    _closed = _Appt(ctwa_entry_at=_dj_tz.now() - _td_t(hours=1),
                    internal_notes='[FREEFORM_WINDOW_CLOSED]')
    results.log("messaging_window_open: 131047 flag overrides 72h → closed",
                _closed.messaging_window_open is False,
                got=str(_closed.messaging_window_open), expected="False")

    _appt  = _Appt(internal_notes='x')
    first  = _appt.mark_freeform_window_closed(save=False)
    second = _appt.mark_freeform_window_closed(save=False)
    results.log("mark_freeform_window_closed: adds tag once (idempotent)",
                first is True and second is False
                and _Appt.FREEFORM_CLOSED_TAG in _appt.internal_notes,
                got=f"first={first} second={second}")
except Exception as e:
    results.log("messaging_window_open / mark_freeform_window_closed", False, got=str(e))

# Language detection is AI-primary live (detect_language → DeepSeek), with the
# keyword detector as the deterministic fallback. Pin the fallback contract and
# that the shared entry point always returns a valid language.
from bot.repeated_question_detector import detect_language_simple, detect_language
LANG_KEYWORD_CASES = [
    ("Can I get a quote for my bathroom",      "english"),
    ("mhoro ndinoda kugadzirisa chimbuzi changu", "shona"),  # 2+ Shona markers
    ("hongu zvakanaka",                        "shona"),
]
for msg, exp in LANG_KEYWORD_CASES:
    try:
        got = detect_language_simple(msg)
        results.log(f"detect_language_simple: '{msg[:26]}'", got == exp, got=got, expected=exp)
    except Exception as e:
        results.log(f"detect_language_simple: '{msg[:26]}'", False, got=str(e))
try:
    dl = detect_language("Hello there")
    results.log("detect_language: returns a valid language",
                dl in ('shona', 'mixed', 'english'), got=dl)
except Exception as e:
    results.log("detect_language: returns a valid language", False, got=str(e))

# Central pricing-gate policy: a buying / project statement ("I want to purchase
# 2x shower cubicles") must NOT trigger a priced auto-reply — only an explicit
# price ask should. The production bug: the standalone-question branch priced a
# purchase statement because it skipped this gate (appt 470). API-free: we pass
# price_requested explicitly and use a fake self carrying the real pure helper.
class _FakeSelfPricing:
    PRICING_AUTO_REPLY_INTENTS = ResponseMixin.PRICING_AUTO_REPLY_INTENTS
    NON_PRICING_AUTO_REPLY_INTENTS = ResponseMixin.NON_PRICING_AUTO_REPLY_INTENTS
    _PRODUCT_FAMILY_PATTERNS = ResponseMixin._PRODUCT_FAMILY_PATTERNS
    _looks_like_project_description_reply = ResponseMixin._looks_like_project_description_reply
    _product_families_in = ResponseMixin._product_families_in
    _names_multiple_products = ResponseMixin._names_multiple_products
    _is_job_quote_request = ResponseMixin._is_job_quote_request
    _asks_price_figure = ResponseMixin._asks_price_figure
    _asks_for_quote = ResponseMixin._asks_for_quote
    _should_volunteer_pricing = ResponseMixin._should_volunteer_pricing
_fp = _FakeSelfPricing()
# (intent, message, price_requested, expected: should we volunteer a price?)
VOLUNTEER_PRICING_CASES = [
    ("shower_cubicle", "I want to purchase 2x shower cubicles and accessories", False, False),  # the bug
    ("shower_cubicle", "how much for a shower cubicle",  True,  True),   # explicit price ask
    ("geyser",         "replace my geyser",              False, False),  # project statement
    ("shower_cubicle", "shower cubicle",                 False, False),  # bare name, no ask
    ("toilet",         "I need to install a new toilet", False, False),  # commitment, no ask
    ("shower_cubicle", "how much to fit a shower",       True,  True),   # explicit price ask on a JOB → still price
    ("shower_cubicle", "fit tub and shower",             False, False),  # job, no price ask → site visit
    ("location_ask",   "where are you based",            False, True),   # info intent always answers
    ("pictures",       "send me some photos",            False, True),   # info intent always answers
    ("none",           "hello there",                    False, False),  # no priceable intent
    # Tubs are now gated like every other product (no more always-answer):
    ("standalone_tub", "how much for a tub",             True,  True),   # price ask → price
    ("standalone_tub", "I want a freestanding tub",      False, False),  # commitment, no ask → no price
    ("tub_sales",      "a tub and chamber",              False, False),  # scope list → no price
]
for intent, msg, price_req, expected in VOLUNTEER_PRICING_CASES:
    try:
        got = _fp._should_volunteer_pricing(intent, msg, price_requested=price_req)
        results.log(
            f"_should_volunteer_pricing: '{msg[:30]}' [{intent}]",
            got == expected,
            f"volunteer={got}",
            expected=f"volunteer={expected}",
            got=f"volunteer={got}",
        )
    except Exception as e:
        results.log(f"_should_volunteer_pricing: '{msg[:30]}'", False, got=str(e))

# A buying statement must be recognised as a commitment (→ acknowledge & progress
# the booking flow), NOT routed to the Q&A answerer that volunteers prices/sizes.
# Production bug: "I want to purchase 2x shower cubicles" got a price+size spiel
# (appt 473). API-free: pure regex helper on a fake self.
# A short fixture-type answer ("free standing" / "built in", answering "built-in
# or freestanding?") must read as a project description so the booking flow
# captures it and advances — not loop re-asking. Production: customer said "Free
# standing" twice and the bot kept re-asking what they wanted.
class _FakeSelfDesc:
    _looks_like_project_description_reply = ResponseMixin._looks_like_project_description_reply
_fd = _FakeSelfDesc()
DESC_REPLY_CASES = [
    ("Free standing",   True),    # the bug
    ("free-standing",   True),
    ("built in",        True),
    ("standalone",      True),
    ("I want a new toilet and basin", True),  # normal description still True
    ("ok",              False),   # acks still excluded
    ("yes",             False),
    ("noted",           False),
]
for msg, expected in DESC_REPLY_CASES:
    try:
        got = _fd._looks_like_project_description_reply(msg)
        results.log(
            f"_looks_like_project_description_reply: '{msg[:30]}'",
            got == expected,
            f"desc={got}",
            expected=f"desc={expected}",
            got=f"desc={got}",
        )
    except Exception as e:
        results.log(f"_looks_like_project_description_reply: '{msg[:30]}'", False, got=str(e))

# Only a BARE product word is an availability question. A product word + descriptor
# ("shower room", "vanity unit") is a DESCRIPTION — must not be flagged, or the
# project_description save is blocked and the booking flow loops re-asking.
# Production: "Shower room" re-asked 3x because startswith("shower") flagged it.
class _FakeSelfAvail:
    _is_product_availability_question = ResponseMixin._is_product_availability_question
_fa = _FakeSelfAvail()
PROD_AVAIL_CASES = [
    ("Shower room",        False),   # the bug
    ("shower room",        False),
    ("vanity unit",        False),
    ("shower installation", False),
    ("I want to replace my toilet and shower", False),
    ("tubs",               True),    # bare product word = availability
    ("and geysers",        True),
    ("vanitys?",           True),
    ("do you have tubs",   True),    # explicit availability phrasing
    ("toilets also?",      True),
]
for msg, expected in PROD_AVAIL_CASES:
    try:
        got = _fa._is_product_availability_question(msg)
        results.log(f"_is_product_availability_question: '{msg[:28]}'", got == expected,
                    expected=str(expected), got=str(got))
    except Exception as e:
        results.log(f"_is_product_availability_question: '{msg[:28]}'", False, got=str(e))

# A corner tub is a built-in tub (same price, from US$160) — not freestanding.
class _FakeSelfTubType:
    _tub_type_in_message = ResponseMixin._tub_type_in_message
_ftt = _FakeSelfTubType()
TUB_TYPE_CASES = [
    ("corner tub how much", "built_in"),
    ("corner bath", "built_in"),
    ("built-in tub", "built_in"),
    ("freestanding tub", "freestanding"),
    ("how much tub", None),
]
for msg, expected in TUB_TYPE_CASES:
    try:
        got = _ftt._tub_type_in_message(msg)
        results.log(f"_tub_type_in_message: '{msg}'", got == expected,
                    expected=str(expected), got=str(got))
    except Exception as e:
        results.log(f"_tub_type_in_message: '{msg}'", False, got=str(e))

class _FakeSelfBuy:
    _is_purchase_commitment = ResponseMixin._is_purchase_commitment
    _is_job_quote_request = ResponseMixin._is_job_quote_request
    _PRODUCT_FAMILY_PATTERNS = ResponseMixin._PRODUCT_FAMILY_PATTERNS
    _product_families_in = ResponseMixin._product_families_in
    _names_multiple_products = ResponseMixin._names_multiple_products
    _asks_price_figure = ResponseMixin._asks_price_figure
    _asks_for_quote = ResponseMixin._asks_for_quote
_fb = _FakeSelfBuy()
# "quote" is NOT a price-figure ask — it leans to the free site visit; only
# how-much/price/cost gets chat prices. Production: "Need a quote to fit tub and
# shower" should set up the visit, not dump prices (appt 479).
PRICE_FIGURE_CASES = [
    # (message, asks_figure, asks_quote)
    ("Need a quote to fit tub and shower", False, True),   # the bug → site visit
    ("can I get a quotation",              False, True),
    ("How much tab and shower",            True,  False),  # how-much → price
    ("how much is a shower cubicle",       True,  False),
    ("price of a geyser",                  True,  False),
    ("what does a vanity cost",            True,  False),
    ("marii yeshower",                     True,  False),  # Shona 'how much'
    ("I want to fit a tub and shower",     False, False),  # neither → booking flow
]
for msg, ef, eq in PRICE_FIGURE_CASES:
    try:
        gf, gq = _fb._asks_price_figure(msg), _fb._asks_for_quote(msg)
        results.log(
            f"_asks_price_figure/quote: '{msg[:30]}'",
            gf == ef and gq == eq,
            f"figure={gf} quote={gq}",
            expected=f"figure={ef} quote={eq}",
            got=f"figure={gf} quote={gq}",
        )
    except Exception as e:
        results.log(f"_asks_price_figure/quote: '{msg[:30]}'", False, got=str(e))
# A multi-item price ask must price EVERY named item, not just one. Production
# bug: "How much tab and shower" / "quote to fit tub and shower" priced only the
# shower (appt 477). API-free: count distinct product families named.
MULTI_PRODUCT_CASES = [
    ("How much tab and shower",              True),   # tab(typo)+shower
    ("Need a quote to fit tub and shower",   True),   # tub+shower
    ("how much for a tub and a toilet",      True),   # tub+toilet
    ("shower and vanity price",              True),   # shower+vanity
    ("how much is a shower cubicle",         False),  # single (shower+cubicle = 1 family)
    ("how much for a geyser",                False),  # single
    ("is the table included",                False),  # 'tab' in 'table' must NOT count
]
for msg, expected in MULTI_PRODUCT_CASES:
    try:
        got = _fb._names_multiple_products(msg)
        results.log(
            f"_names_multiple_products: '{msg[:30]}'",
            got == expected,
            f"multi={got}",
            expected=f"multi={expected}",
            got=f"multi={got}",
        )
    except Exception as e:
        results.log(f"_names_multiple_products: '{msg[:30]}'", False, got=str(e))

# The combined reply prices the CURRENT scope, carries the ballpark disclaimer,
# and never invents figures. Wires every helper the rewritten method now uses.
class _FakeSelfCombined:
    _PRODUCT_FAMILY_PATTERNS = ResponseMixin._PRODUCT_FAMILY_PATTERNS
    _SCOPE_LABEL = ResponseMixin._SCOPE_LABEL
    _SCOPE_SHORT = ResponseMixin._SCOPE_SHORT
    _QTY_WORDS = ResponseMixin._QTY_WORDS
    _NUM_WORDS = ResponseMixin._NUM_WORDS
    _product_families_in = ResponseMixin._product_families_in
    _quantity_for_family = ResponseMixin._quantity_for_family
    _active_scope = ResponseMixin._active_scope
    _num_word = ResponseMixin._num_word
    _scope_allin_phrase = ResponseMixin._scope_allin_phrase
    _format_labour_scope = ResponseMixin._format_labour_scope
    _labour_split_seg = ResponseMixin._labour_split_seg
    _asks_about_labour = ResponseMixin._asks_about_labour
    _capture_named_products_as_description = ResponseMixin._capture_named_products_as_description
    _build_combined_price_reply = ResponseMixin._build_combined_price_reply
    _tub_type_in_message = ResponseMixin._tub_type_in_message
    def __init__(self, appointment=None):
        self.appointment = appointment
    # Phase 2.3b: prices come from tenant data via these map methods; the fake
    # pins homebase's sheet as literals so the flow pins stay DB-independent.
    def _rough_price_map(self):
        return {
            'shower': 'shower cubicle from US$170', 'tub': 'tub from US$160',
            'geyser': 'geyser from US$160', 'vanity': 'vanity from US$180',
            'toilet': 'toilet from US$70', 'chamber': 'side chamber from US$160',
        }
    def _price_components_map(self):
        return {'shower': (130, 40), 'tub': (80, 80), 'geyser': (80, 80),
                'vanity': (150, 30), 'toilet': (50, 20), 'chamber': (130, 30)}
    def _flat_price_map(self):
        return {'basin': 70}
    def _freestanding_tub_price(self):
        return (670, "tub from US$400 + mixer US$150, install from US$120")
    def _next_forward_question(self, language="english", scope=None, has_accessories=False):
        return "Whereabouts are you based?"
try:
    _cr = _FakeSelfCombined()._build_combined_price_reply("How much tab and shower", "english")
    results.log(
        "_build_combined_price_reply: prices BOTH tub and shower",
        "tub from US$160" in _cr and "shower cubicle from US$170" in _cr,
        got=_cr[:90],
    )
    results.log(
        "_build_combined_price_reply: ballpark disclaimer, not visit-gated",
        "ballpark" in _cr and "sees the space" in _cr and "approximate starting" not in _cr,
        got=_cr[-90:],
    )
    # A plain multi-item price ask must NOT dump the supply/labour split.
    results.log(
        "_build_combined_price_reply: no labour split unless asked",
        "fitted" not in _cr and "labour from" not in _cr,
        got=_cr[:120],
    )
    # Real-lead corpus (2026-07-02): "How much is it to fit a standalone tab,
    # chamber and sink in a bathroom." — the tub line must carry FREESTANDING
    # money (US$670), never built-in (US$160), and the sink/basin must be priced
    # (US$70 flat, homebase.md), not silently dropped.
    _fs = _FakeSelfCombined()._build_combined_price_reply(
        "How much is it to fit a standalone tab, chamber and sink in a bathroom.",
        "english",
    )
    results.log(
        "combined reply: standalone tub uses freestanding money, sink priced",
        ("US$670" in _fs and "Freestanding tub" in _fs
         and "Basin: from US$70" in _fs
         and "Tub: supply from US$80" not in _fs),
        got=_fs,
    )
    # Without the standalone word the tub stays built-in and basin still shows.
    _bi = _FakeSelfCombined()._build_combined_price_reply(
        "how much for a tub and sink", "english",
    )
    results.log(
        "combined reply: plain tub stays built-in; basin flat price shown",
        "tub from US$160" in _bi and "basin from US$70" in _bi and "US$670" not in _bi,
        got=_bi,
    )
except Exception as e:
    results.log("_build_combined_price_reply", False, got=str(e))

# BUG 2 — scope is the LATEST the customer named. Opening with "tub and shower"
# then narrowing to "2x shower cubicles and accessories" must price cubicles
# only (tub dropped), with quantity multiplied and the line total shown.
class _FakeApptScope:
    project_description = "shower and tub"   # stale earlier scope — must NOT win
    customer_area = "Greendale"
    project_type = "bathroom_renovation"
    scheduled_datetime = None
    conversation_history = [
        {'role': 'user', 'content': 'Need a quote to fit tub and shower'},
        {'role': 'assistant', 'content': 'Great, what area are you in?'},
        {'role': 'user', 'content': 'I want to purchase 2x shower cubicles and asseries'},
        {'role': 'user', 'content': 'Greendale'},
        {'role': 'user', 'content': 'How much is labour'},
    ]
    def save(self, update_fields=None):
        pass
try:
    _lab = _FakeSelfCombined(appointment=_FakeApptScope())._build_combined_price_reply(
        "How much is labour", "english"
    )
    results.log(
        "labour scope: prices the cubicle (current scope), drops the tub",
        ("supply from US$130, labour from US$40" in _lab
         and "tub" not in _lab.lower() and "US$160" not in _lab and "US$80" not in _lab),
        got=_lab,
    )
    results.log(
        "labour scope: quantity multiplied with a line total",
        "about US$170 fitted each" in _lab and "For two that's around US$340 all-in" in _lab,
        got=_lab,
    )
    results.log(
        "labour scope: accessories noted, ballpark, not gated behind visit",
        ("accessories on top" in _lab and "ballpark" in _lab and "sees the space" in _lab),
        got=_lab,
    )
except Exception as e:
    results.log("_build_combined_price_reply labour scope", False, got=str(e))

# _asks_about_labour fires on labour/install/fit questions, not plain how-much.
LABOUR_ASK_CASES = [
    ("How much is labour",          True),
    ("how much for installation",   True),
    ("whats the fitting cost",      True),
    ("How much tub and shower",     False),
    ("price of a geyser",           False),
]
for msg, expected in LABOUR_ASK_CASES:
    try:
        got = _FakeSelfCombined()._asks_about_labour(msg)
        results.log(
            f"_asks_about_labour: '{msg[:28]}'",
            got == expected,
            expected=str(expected),
            got=str(got),
        )
    except Exception as e:
        results.log(f"_asks_about_labour: '{msg[:28]}'", False, got=str(e))

# BUG 1 — the forward question advances to the next OPEN stage, never re-asking a
# stage already asked/answered, and never reusing wording. Driven off conversation
# state (appointment fields + assistant turns), stage order Service->Detail->Area->Booking.
class _FakeApptFwd:
    def __init__(self, project_type=None, customer_area=None, scheduled_datetime=None,
                 history=None):
        self.project_type = project_type
        self.customer_area = customer_area
        self.scheduled_datetime = scheduled_datetime
        self.conversation_history = history or []
class _FakeSelfForward:
    _FORWARD_BANK = ResponseMixin._FORWARD_BANK
    _SCOPE_LABEL = ResponseMixin._SCOPE_LABEL
    _next_forward_question = ResponseMixin._next_forward_question
    # Tie-down helpers — "ask for a yes first" leads every answer; the forward
    # question is only reached once our last turn was already a tie-down.
    _TIEDOWN_VALUE_CHECK = ResponseMixin._TIEDOWN_VALUE_CHECK
    _TIEDOWN_OPENER = ResponseMixin._TIEDOWN_OPENER
    _EXTRA_TIEDOWN_SIGNATURES = ResponseMixin._EXTRA_TIEDOWN_SIGNATURES
    _tiedown_signatures = ResponseMixin._tiedown_signatures
    _assistant_history_text = ResponseMixin._assistant_history_text
    _yes_tiedown = ResponseMixin._yes_tiedown
    _price_tiedown = ResponseMixin._price_tiedown
    _last_assistant_was_tiedown = ResponseMixin._last_assistant_was_tiedown
    def __init__(self, appt):
        self.appointment = appt
def _bot(*contents):
    return [{'role': 'assistant', 'content': c} for c in contents]
# Canonical value-check tie-down — seeded as the last turn to reach the forward
# question (the "proceed" branch).
_TD = "Anything else on the property that needs looking at?"
try:
    # No prior tie-down -> ask for a yes first (value-check), not the field question.
    _fq_td = _FakeSelfForward(_FakeApptFwd(
        history=_bot("Shower cubicles start from US$170."),
    ))._next_forward_question("english", scope=[('shower', 2)], has_accessories=True)
    results.log(
        "forward Q: no prior tie-down -> asks for a yes first (budget tie-down)",
        "with your budget" in _fq_td.lower(),
        got=str(_fq_td),
    )
    # Transcript case: area answered (Greendale) AND a day already offered
    # ("work better for you"); scope known, accessories mentioned -> every earlier
    # stage covered, so it lands on a FRESH booking question (not a repeat day push).
    # Tie-down already sent last turn -> proceed to the forward question.
    _fq = _FakeSelfForward(_FakeApptFwd(
        customer_area="Greendale",
        history=_bot("Would tomorrow or this Friday work better for you?", _TD),
    ))._next_forward_question("english", scope=[('shower', 2)], has_accessories=True)
    results.log(
        "forward Q: all stages covered -> timeframe question, no visit pitch, area not re-asked",
        _fq == "When were you hoping to get this done?"
        and "assessment" not in _fq and "visit" not in _fq,
        got=str(_fq),
    )
    # Area genuinely open (not asked, not answered) -> ask it (after the tie-down).
    _fq2 = _FakeSelfForward(_FakeApptFwd(
        history=_bot("Shower cubicles start from US$170.", _TD),
    ))._next_forward_question("english", scope=[('shower', 2)], has_accessories=True)
    results.log(
        "forward Q: open area stage -> asks area",
        _fq2 == "Whereabouts are you based?",
        got=str(_fq2),
    )
    # Booking is the terminal stage that recurs across turns: a second booking
    # nudge must use FRESH wording, never the phrasing already sent — and still
    # never pitches the visit.
    _fq3 = _FakeSelfForward(_FakeApptFwd(
        customer_area="Greendale",
        history=_bot("When were you hoping to get this done?", _TD),
    ))._next_forward_question("english", scope=[('shower', 2)], has_accessories=True)
    results.log(
        "forward Q: booking nudge rotates wording, no repeat, no visit pitch",
        _fq3 == "Are you looking to start soon, or still planning it out?"
        and "assessment" not in _fq3,
        got=str(_fq3),
    )
except Exception as e:
    results.log("_next_forward_question", False, got=str(e))

# Confirm-intent close: name the items back and confirm scope before booking.
# Two items -> "both the tub and shower, or starting with one?"; one item -> None
# (caller falls back to a generic scope question).
class _FakeSelfConfirm:
    _FAMILY_DISPLAY = ResponseMixin._FAMILY_DISPLAY
    _confirm_intent_question = ResponseMixin._confirm_intent_question
try:
    _c2 = _FakeSelfConfirm()._confirm_intent_question({'shower', 'tub'})
    results.log(
        "_confirm_intent_question: two items names both, asks both-or-one",
        _c2 == "Are you looking to do both the shower and tub, or starting with one?",
        got=str(_c2),
    )
    _c3 = _FakeSelfConfirm()._confirm_intent_question({'shower', 'tub', 'toilet'})
    results.log(
        "_confirm_intent_question: three items lists all of them",
        ("all of them" in _c3 and "shower" in _c3 and "tub" in _c3 and "toilet" in _c3),
        got=str(_c3),
    )
    _c1 = _FakeSelfConfirm()._confirm_intent_question({'shower'})
    results.log(
        "_confirm_intent_question: single item returns None (generic fallback)",
        _c1 is None,
        got=str(_c1),
    )
except Exception as e:
    results.log("_confirm_intent_question", False, got=str(e))

# The pricing close is stage-driven, with a deflection override on top. Build a
# fake that controls the stage + is_delayed and otherwise reuses the real method.
class _FakeApptStage:
    # project_type defaults to a known job so the qualifying close is the
    # property-scope one; pass project_type=None to model a cold opener.
    def __init__(self, is_delayed=False, history=None,
                 project_type="bathroom_renovation", project_description=None):
        self.is_delayed = is_delayed
        self.conversation_history = history or []
        self.project_type = project_type
        self.project_description = project_description
class _FakeSelfFollowup:
    _FAMILY_DISPLAY = ResponseMixin._FAMILY_DISPLAY
    _confirm_intent_question = ResponseMixin._confirm_intent_question
    _get_pricing_followup_prompt = ResponseMixin._get_pricing_followup_prompt
    _TIEDOWN_VALUE_CHECK = ResponseMixin._TIEDOWN_VALUE_CHECK
    _TIEDOWN_OPENER = ResponseMixin._TIEDOWN_OPENER
    _tiedown_signatures = ResponseMixin._tiedown_signatures
    _assistant_history_text = ResponseMixin._assistant_history_text
    _yes_tiedown = ResponseMixin._yes_tiedown
    _price_tiedown = ResponseMixin._price_tiedown
    _last_assistant_was_tiedown = ResponseMixin._last_assistant_was_tiedown
    _last_assistant_was_value_check = ResponseMixin._last_assistant_was_value_check
    _append_tiedown = ResponseMixin._append_tiedown
    _EXTRA_TIEDOWN_SIGNATURES = ResponseMixin._EXTRA_TIEDOWN_SIGNATURES
    _product_price_close = ResponseMixin._product_price_close
    _ensure_price_disclaimer = ResponseMixin._ensure_price_disclaimer
    _PRICED_INTENTS = ResponseMixin._PRICED_INTENTS
    _last_assistant_was_price_tiedown = ResponseMixin._last_assistant_was_price_tiedown
    _is_budget_decline = ResponseMixin._is_budget_decline
    _is_budget_decline_keywords = ResponseMixin._is_budget_decline_keywords
    _handle_budget_objection = ResponseMixin._handle_budget_objection
    _advance_after_scope = ResponseMixin._advance_after_scope
    _service_continuation_reply = ResponseMixin._service_continuation_reply
    _get_first_pass_question = ResponseMixin._get_first_pass_question
    def _set_question_retry_count(self, q, n):
        pass
    def __init__(self, stage, is_delayed=False, history=None,
                 project_type="bathroom_renovation"):
        self._stage = stage
        self.appointment = _FakeApptStage(
            is_delayed=is_delayed, history=history, project_type=project_type
        )
    def get_next_question_to_ask(self):
        return self._stage
    def _get_contextual_description_question(self):
        return "What specifically needs doing?"
    def _get_next_two_available_days(self):
        return []
try:
    # No prior tie-down -> ask for a yes first (value-check), not the field question.
    _td1 = _FakeSelfFollowup("project_description")._get_pricing_followup_prompt(
        "english", items={'shower', 'tub'}
    )
    results.log(
        "pricing close: no prior tie-down -> budget tie-down first",
        "with your budget" in _td1.lower(),
        got=str(_td1),
    )
    # Scope stage + known items, tie-down already sent -> confirm-intent names items.
    _ci = _FakeSelfFollowup("project_description", history=_bot(_TD))._get_pricing_followup_prompt(
        "english", items={'shower', 'tub'}
    )
    results.log(
        "pricing close: scope stage with items -> confirm-intent (after tie-down)",
        _ci == "Are you looking to do both the shower and tub, or starting with one?",
        got=str(_ci),
    )
    # Deflected lead at the scheduling stage -> timeline anchor, NOT a day push.
    # The deflection override sits ABOVE the tie-down gate, so no history needed.
    _ta = _FakeSelfFollowup("availability_date", is_delayed=True)._get_pricing_followup_prompt("english")
    results.log(
        "pricing close: deflected lead -> timeline anchor (no day push)",
        _ta == "Are you hoping to get this sorted soon, or still planning it out?",
        got=str(_ta),
    )
    # Engaged lead at scheduling, tie-down already sent -> asks the day (no anchor).
    _day = _FakeSelfFollowup(
        "availability_date", is_delayed=False, history=_bot(_TD)
    )._get_pricing_followup_prompt("english")
    results.log(
        "pricing close: engaged lead at scheduling -> day question (no anchor)",
        "planning it out" not in _day and "else on the property" not in _day,
        got=str(_day),
    )
except Exception as e:
    results.log("pricing close stage/deflection", False, got=str(e))

# Tie-down helpers: rotate so the same value-check never repeats, and detect when
# our last turn was already a tie-down (so the field question proceeds).
try:
    # First call with no history -> first bank line.
    _t0 = _FakeSelfFollowup("service_type")._yes_tiedown("english")
    results.log(
        "tie-down: first call -> first qualifying line",
        _t0 == "Anything else on the property that needs looking at?",
        got=str(_t0),
    )
    # First line already used -> rotates to the next, unused one.
    _t1 = _FakeSelfFollowup("service_type", history=_bot(_TD))._yes_tiedown("english")
    results.log(
        "tie-down: rotates to a fresh line when the first was used",
        _t1 == "Any other work around the place you'd want sorted while we're there?",
        got=str(_t1),
    )
    # Shona path returns a Shona tie-down.
    _ts = _FakeSelfFollowup("service_type")._yes_tiedown("shona")
    results.log(
        "tie-down: shona language -> shona line",
        "pamba" in _ts,
        got=str(_ts),
    )
    # Cold opener (no job on the table yet) -> softer "what are you looking to get
    # sorted?" instead of the presumptive "anything ELSE on the property?".
    _op = _FakeSelfFollowup("service_type", project_type=None)._yes_tiedown("english")
    results.log(
        "tie-down: cold opener (no job) -> 'what are you looking to get sorted?'",
        _op == "What are you looking to get sorted?",
        got=str(_op),
    )
    _ops = _FakeSelfFollowup("service_type", project_type=None)._yes_tiedown("shona")
    results.log(
        "tie-down: cold opener -> shona opener line",
        "kugadziriswa" in _ops,
        got=str(_ops),
    )
    # The opener close still counts as a tie-down (won't stack one next turn).
    results.log(
        "tie-down: opener close registered as a tie-down signature",
        _FakeSelfFollowup(
            "service_type", history=_bot("What are you looking to get sorted?")
        )._last_assistant_was_tiedown() is True,
        got="ok",
    )
    # Detection: last assistant turn is a tie-down -> True; a price line -> False.
    _d_yes = _FakeSelfFollowup("service_type", history=_bot("Geysers from US$X.", _TD))
    _d_no = _FakeSelfFollowup("service_type", history=_bot(_TD, "Geysers from US$X."))
    results.log(
        "tie-down: detects last turn was a tie-down",
        _d_yes._last_assistant_was_tiedown() is True
        and _d_no._last_assistant_was_tiedown() is False,
        got=f"yes={_d_yes._last_assistant_was_tiedown()} no={_d_no._last_assistant_was_tiedown()}",
    )
    # _append_tiedown (LLM / semantic-rescue answer paths): append the non-price
    # qualifying close unless our last turn was already one or the reply is empty.
    _ans = "A small repair takes a couple of hours."
    _ap1 = _FakeSelfFollowup("service_type")._append_tiedown(_ans, "english")
    results.log(
        "append tie-down: free-form answer gets the qualifying close appended",
        _ap1.startswith(_ans) and "else on the property" in _ap1,
        got=str(_ap1),
    )
    _ap2 = _FakeSelfFollowup("service_type", history=_bot(_TD))._append_tiedown(_ans, "english")
    results.log(
        "append tie-down: no stacking when last turn was already a tie-down",
        _ap2 == _ans,
        got=str(_ap2),
    )
    _ap3 = _FakeSelfFollowup("service_type")._append_tiedown("", "english")
    results.log(
        "append tie-down: empty reply unchanged",
        _ap3 == "",
        got=repr(_ap3),
    )
    # A reply that already asks a question must NOT get a second question stacked on.
    _apq = "So it's a shower you're after — full reno or just the shower?"
    _ap4 = _FakeSelfFollowup("service_type")._append_tiedown(_apq, "english")
    results.log(
        "append tie-down: no stacking when the reply already asks a question",
        _ap4 == _apq,
        got=str(_ap4),
    )
    # Job/quote request routes to the free visit and closes on the SCRIPTED next
    # question — NEVER the budget tie-down (no price was quoted). The reply is split
    # into TWO messages (acknowledgement, then the question) via the split marker,
    # and the scripted opener ("All good,"/"Great,") is dropped from the 2nd piece
    # so it doesn't read as a second canned opener.
    from bot.views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER as _SPLIT
    class _FakeSelfJQ:
        _build_job_quote_reply = ResponseMixin._build_job_quote_reply
        _get_first_pass_question = ResponseMixin._get_first_pass_question
        _already_sent_job_quote_pitch = ResponseMixin._already_sent_job_quote_pitch
        def __init__(self, nq, history=None):
            self._nq = nq
            if history is not None:
                class _Appt:
                    pass
                self.appointment = _Appt()
                self.appointment.conversation_history = history
        def get_next_question_to_ask(self):
            return self._nq
        def _capture_named_products_as_description(self, message):
            pass
        def _get_next_two_available_days(self):
            return []
        def _format_day(self, d):
            return "tomorrow"
        def _describe_project_context(self):
            return "have a quick look at the site for the installation"
    _jq = _FakeSelfJQ("area")._build_job_quote_reply(
        "english", "Need a quote to fit tub and shower")
    _jq_parts = [p.strip() for p in _jq.split(_SPLIT)]
    results.log(
        "job quote reply: two messages (ack + question), scripted opener dropped, no budget tie-down",
        len(_jq_parts) == 2
        and _jq_parts[0] == "We'll get you an exact, all-in figure free on a quick on-site visit."
        and _jq_parts[1] == "What area are you in?"
        and "budget" not in _jq.lower(),
        got=repr(_jq_parts),
    )
    # availability_date: the "Great," opener is dropped and the question capitalised,
    # matching the desired two-message shape from production.
    _jq_av = _FakeSelfJQ("availability_date")._build_job_quote_reply(
        "english", "new installation")
    _jq_av_parts = [p.strip() for p in _jq_av.split(_SPLIT)]
    results.log(
        "job quote reply: availability_date second piece starts 'What works better', no 'Great,'",
        len(_jq_av_parts) == 2
        and _jq_av_parts[1].startswith("What works better for you")
        and not _jq_av_parts[1].lower().startswith("great"),
        got=repr(_jq_av_parts),
    )
    # Never re-pitch: once the visit pitch has been sent, a later job-shaped
    # message ("...require installation of all the plumbing requirements on the
    # plan") must get ONLY the scripted next question — no pitch line, no split
    # (prod: pitch sent twice in one conversation, 2026-07-08).
    _jq_dup = _FakeSelfJQ("availability_date", history=[
        {"role": "user", "content": "I would like to request a quote for plumbing services"},
        {"role": "assistant",
         "content": "We'll get you an exact, all-in figure free on a quick on-site visit."},
    ])._build_job_quote_reply(
        "english",
        "It's a new building and we require installation of all the plumbing "
        "requirements on the plan")
    results.log(
        "job quote reply: pitch never repeats — second job message gets only the scripted question",
        _SPLIT not in _jq_dup
        and "all-in figure" not in _jq_dup
        and "what works better for you" in _jq_dup.lower(),
        got=repr(_jq_dup),
    )
    # Shona pitch in history counts too — the guard is language-agnostic.
    _jq_dup_sn = _FakeSelfJQ("area", history=[
        {"role": "assistant",
         "content": "Tinokupai quote chaiyo, yese-yese, mahara patinouya kuzoona pamba."},
    ])._build_job_quote_reply("english", "need a quote to fit tub and shower")
    results.log(
        "job quote reply: shona pitch in history also blocks a re-pitch",
        _SPLIT not in _jq_dup_sn and "all-in figure" not in _jq_dup_sn,
        got=repr(_jq_dup_sn),
    )
    # Tub sizes: a size question with NO specific tub type named must list ALL
    # measurements (built-in + free-standing + corner); naming a type gives just
    # that block. Business spec, 2026-07-01.
    class _FakeSelfTubSize:
        _tub_sizes_reply = ResponseMixin._tub_sizes_reply
        # Phase 2.3d: size blocks come from tenant data; pin homebase's blocks
        # as literals so the flow pin stays DB-independent.
        def _tub_size_blocks_map(self):
            return {
                'built_in': ("Built-in bathtubs\n"
                             "- Compact / Standard: 1700 × 700 mm\n"
                             "- Large / Luxury: 1800 × 800 mm"),
                'freestanding': ("Free-standing bathtubs\n"
                                 "- Compact: 1440 × 570 mm\n"
                                 "- Standard: 1700 × 700 to 800 mm\n"
                                 "- Large / Luxury: 1800 to 1865 × 800 to 890 mm"),
                'corner': ("Corner bathtubs\n"
                           "- Compact symmetrical: 1200 × 1200 mm to 1350 × 1350 mm\n"
                           "- Standard symmetrical: 1500 × 1500 mm\n"
                           "- Offset corner: 1500 to 1700 × 900 to 1000 mm"),
            }
    _ts = _FakeSelfTubSize()
    _all = _ts._tub_sizes_reply("english", "what sizes do your tubs come in?")
    results.log(
        "tub sizes: no type named -> all measurements (built-in, free-standing, corner)",
        all(h in _all for h in
            ("Built-in bathtubs", "Free-standing bathtubs", "Corner bathtubs"))
        and "1440 × 570 mm" in _all and "1800 to 1865 × 800 to 890 mm" in _all
        and "1200 × 1200 mm to 1350 × 1350 mm" in _all,
        got=_all,
    )
    _corner = _ts._tub_sizes_reply("english", "what size are corner tubs?")
    _free = _ts._tub_sizes_reply("english", "freestanding tub dimensions")
    _bi = _ts._tub_sizes_reply("english", "how big are built-in tubs")
    results.log(
        "tub sizes: a named type gives only that block",
        ("Corner bathtubs" in _corner and "Built-in bathtubs" not in _corner
         and "Free-standing bathtubs" not in _corner)
        and ("Free-standing bathtubs" in _free and "Corner bathtubs" not in _free)
        and ("Built-in bathtubs" in _bi and "Corner bathtubs" not in _bi
             and "Free-standing bathtubs" not in _bi),
        got=f"corner={_corner!r}",
    )
    # A bare flow ANSWER just captured by extraction ("Bathroom and kitchen
    # installations." answering the opener) must stick to the script — never get
    # hijacked into the job-quote visit pitch just because it says 'installation'.
    # An actual request ("need a quote to fit…", "I want you to fit…") still routes.
    class _FakeSelfFA:
        _is_captured_flow_answer = ResponseMixin._is_captured_flow_answer
        _asks_for_quote = ResponseMixin._asks_for_quote
        _asks_price_figure = ResponseMixin._asks_price_figure
        _is_service_type_only = staticmethod(ResponseMixin._is_service_type_only)
        def __init__(self, nq="area"):
            self._nq = nq
        def get_next_question_to_ask(self):
            return self._nq
    _fca = _FakeSelfFA()
    FLOW_ANSWER_CASES = [
        ("Bathroom and kitchen installations.", ['project_description'], True),
        ("new installation in Graylands park", ['project_description', 'area'], True),
        ("Bathroom renovation", ['service_type'], True),
        # Requests / asks keep the quote route:
        ("Need a quote to fit tub and shower", ['project_description'], False),
        ("I want you to fit a tub and shower", ['project_description'], False),
        ("Can you install geysers?", ['project_description'], False),
        ("how much to install a tub", ['project_description'], False),
        # Nothing captured this turn (not at the description stage) -> not a flow answer:
        ("Bathroom and kitchen installations.", [], False),
        ("Bathroom and kitchen installations.", ['area'], False),
        # A question WITHOUT the '?' is still a question (conv 427: got the area
        # script instead of the tub measurements):
        ("My bathroom  is small....what are the measurements of your tubs ...",
         ['project_description'], False),
        ("how big are your tubs", ['project_description'], False),
    ]
    _fca_ok = all(
        _fca._is_captured_flow_answer(m, f) is e for m, f, e in FLOW_ANSWER_CASES
    )
    results.log(
        "captured flow answer: bare answer sticks to script; requests keep the quote route",
        _fca_ok,
        got="; ".join(f"{m[:26]!r}/{f}->{_fca._is_captured_flow_answer(m, f)}"
                      for m, f, e in FLOW_ANSWER_CASES if _fca._is_captured_flow_answer(m, f) is not e)
            or "all as expected",
    )
    # The first-pass description question: generic service categories get the
    # EXACT approved script ("Got it! Can you tell me a bit more about the
    # project?") — never a multi-part contextual interrogation (prod: "bathroom
    # and kitchen installations" got a kitchen-only pipework grilling). Only
    # fault/repair types keep a targeted question.
    class _FakeApptDQ:
        def __init__(self, pt):
            self.project_type = pt
    class _FakeSelfDQ:
        _get_contextual_description_question = ResponseMixin._get_contextual_description_question
        _get_first_pass_question = ResponseMixin._get_first_pass_question
        def __init__(self, pt):
            self.appointment = _FakeApptDQ(pt)
    _DQ_SCRIPT = "Got it! Can you tell me a bit more about the project?"
    _dq_ok = all(
        _FakeSelfDQ(pt)._get_first_pass_question("project_description") == _DQ_SCRIPT
        for pt in ("kitchen_installation", "bathroom_installation",
                   "bathroom_and_kitchen_renovation", "new_plumbing_installation",
                   "bathroom_renovation", None)
    )
    _dq_drain = _FakeSelfDQ("drain_unblocking")._get_first_pass_question("project_description")
    results.log(
        "description question: generic services use the exact script; repairs stay targeted",
        _dq_ok and "Which drain is blocked" in _dq_drain,
        got=f"generic ok={_dq_ok}; drain={_dq_drain!r}",
    )
    # Visit-purpose copy: a bathroom+kitchen scope must never be described as a
    # single room — even when the classifier mislabelled project_type as
    # kitchen_installation (prod: lead was told "quick look at the kitchen
    # plumbing" on a bathroom+kitchen job). Customer's own words (the
    # description) count toward scope.
    class _FakeApptVP:
        def __init__(self, pt, desc):
            self.project_type = pt
            self.project_description = desc
    class _FakeSelfVP:
        _describe_project_context = ResponseMixin._describe_project_context
        def __init__(self, pt, desc=None):
            self.appointment = _FakeApptVP(pt, desc)
    _vp_mis = _FakeSelfVP("kitchen_installation", "Bathroom and kitchen installations")
    _vp_comb = _FakeSelfVP("bathroom_and_kitchen_renovation", "new installation")
    _vp_kit = _FakeSelfVP("kitchen_installation", "new installation")
    _vp_bath = _FakeSelfVP("bathroom_renovation", None)
    results.log(
        "visit purpose: bathroom+kitchen scope -> 'the space', single rooms stay specific",
        _vp_mis._describe_project_context() == 'have a quick look at the space'
        and _vp_comb._describe_project_context() == 'have a quick look at the space'
        and _vp_kit._describe_project_context() == 'have a quick look at the kitchen plumbing'
        and _vp_bath._describe_project_context() == 'have a quick look at the bathroom space',
        got=f"mislabeled={_vp_mis._describe_project_context()!r}; kitchen={_vp_kit._describe_project_context()!r}",
    )
    # Root cause: the service-type classifier itself. Split installation phrasing
    # ("bathroom and kitchen installations") must detect BOTH rooms, and a
    # bathroom+kitchen scope maps to the combined project_type — never a single
    # room. And it must NOT pre-fill project_description (a service-type list is
    # not a description; pre-filling skipped the scripted description question).
    from bot.service_type_classifier import classify_service_types_multi, classify_and_save
    _multi = classify_service_types_multi("Bathroom and kitchen installations.")
    _multi_norm = " ".join(_multi).lower()
    results.log(
        "service classifier: split installations phrase detects both rooms",
        'bathroom' in _multi_norm and 'kitchen' in _multi_norm,
        got=str(_multi),
    )
    class _FakeLeadST:
        id = 0
        project_type = None
        project_description = None
        def save(self, update_fields=None):
            pass
    _lead = _FakeLeadST()
    _st = classify_and_save(_lead, "Bathroom and kitchen installations.")
    results.log(
        "service classifier: bathroom+kitchen -> combined type, no description pre-fill",
        _st == 'bathroom_and_kitchen_renovation'
        and _lead.project_type == 'bathroom_and_kitchen_renovation'
        and _lead.project_description is None,
        got=f"type={_st!r} desc={_lead.project_description!r}",
    )
    _lead_k = _FakeLeadST()
    _st_k = classify_and_save(_lead_k, "kitchen installation")
    results.log(
        "service classifier: single room stays specific",
        _st_k == 'kitchen_installation',
        got=str(_st_k),
    )
    # A size/spec ask must never be treated as a service-availability question
    # (scenario suite caught: "how big are your tubs" got "Yes, we handle tub…
    # is a tub the only thing?" instead of the measurements).
    _ssq = ResponseMixin._is_size_spec_question
    results.log(
        "size spec question: sizes yes, availability/price no",
        all(_ssq(m) for m in
            ("how big are your tubs", "what sizes do tubs come in",
             "dimensions of the shower cubicle", "what size are corner tubs"))
        and not any(_ssq(m) for m in
            ("do you have tubs", "how much tub", "can you fit a tub",
             "I want a tub")),
        got=f"how_big={_ssq('how big are your tubs')} do_you_have={_ssq('do you have tubs')}",
    )
    # Identity questions (conv 369): "who am I speaking to?" / "name of the
    # plumber" must be ANSWERED (Plumbot + Takudzwa + the protected number) —
    # never steamrolled by the next booking question. Takudzwa is the single
    # plumber identity everywhere (emails are signed Takudzwa).
    class _FakeApptIdent:
        # Mirrors the Phase-2.2 Appointment helpers with homebase's values so
        # the pinned identity strings stay byte-stable.
        plumber_contact_number = None
        tenant = type('T', (), {'name': 'Homebase Plumbers'})()
        def plumber_contact(self):
            return '+263774819901'
        def plumber_display_name(self):
            return 'Takudzwa'
    class _FakeSelfIdent:
        _maybe_answer_identity_question = ResponseMixin._maybe_answer_identity_question
        def __init__(self):
            self.appointment = _FakeApptIdent()
    _idb = _FakeSelfIdent()._maybe_answer_identity_question("Also who am I speaking to?")
    _idp = _FakeSelfIdent()._maybe_answer_identity_question(
        "Also what is the name of plumber visiting the house so I pass details to mum")
    _idn = _FakeSelfIdent()._maybe_answer_identity_question("I need a geyser installed in Hatfield")
    results.log(
        "identity questions: answered with Plumbot/Takudzwa + number; no over-reach",
        _idb is not None and 'plumbot' in _idb.lower() and 'takudzwa' in _idb.lower()
        and _idp is not None and 'takudzwa' in _idp.lower() and '263774819901' in _idp
        and 'tinashe' not in (_idb + _idp).lower()
        and _idn is None,
        got=f"bot={_idb!r}",
    )
    # Quantity + accessories carried into the named-back item (prod 2026-07-02:
    # "2x shower cubicles and asseries" came back as "a shower cubicle"), with
    # plural grammar in the scripted continuation.
    class _FakeSelfSIP:
        _PRODUCT_FAMILY_PATTERNS = ResponseMixin._PRODUCT_FAMILY_PATTERNS
        _QTY_WORDS = ResponseMixin._QTY_WORDS
        _product_families_in = ResponseMixin._product_families_in
        _quantity_for_family = ResponseMixin._quantity_for_family
        _scope_item_phrase = ResponseMixin._scope_item_phrase
        _service_continuation_reply = ResponseMixin._service_continuation_reply
    _sip = _FakeSelfSIP()
    _item2 = _sip._scope_item_phrase(
        "I want to purchase 2x shower cubicles and asseries", "shower cubicle")
    _cont2 = _sip._service_continuation_reply(_item2, "english")
    _item1 = _sip._scope_item_phrase("do you have geysers", "geyser")
    results.log(
        "scope item: quantity + accessories carried; plural continuation grammar",
        _item2 == "2 shower cubicles and accessories"
        and "Are the 2 shower cubicles and accessories everything" in _cont2
        and "Is a 2" not in _cont2
        and _item1 == "geyser",
        got=f"item={_item2!r}; cont={_cont2!r}",
    )
    # A captured description satisfies the service question — never bounce a
    # lead with a known project back to the opener (prod: 'yes' after the
    # budget tie-down got "How may we assist you on plumbing services").
    class _FakeApptNQ:
        project_type = None
        project_description = "2 shower cubicles and accessories"
        customer_area = None
        scheduled_datetime = None
        customer_name = None
        status = "pending"
    from bot.views.plumbot.extraction_mixin import ExtractionMixin as _EM
    class _FakeSelfNQ:
        get_next_question_to_ask = _EM.get_next_question_to_ask
        appointment = _FakeApptNQ()
        def _time_confirmed(self):
            return False
        def _customer_name_declined(self):
            return False
    results.log(
        "next question: captured description satisfies service_type (no opener bounce)",
        _FakeSelfNQ().get_next_question_to_ask() == "area",
        got=_FakeSelfNQ().get_next_question_to_ask(),
    )
    # "that on facebook" is a price-reference question — confirmed, never
    # steamrolled (prod: got the area script). Long texts don't trigger.
    _fbr = ResponseMixin._is_facebook_price_ref
    results.log(
        "facebook price ref: short mentions yes, long descriptions no",
        _fbr("that on facebook") and _fbr("is that the fb price")
        and not _fbr("I want a bathroom renovation")
        and not _fbr("I saw a very long post about bathroom renovations on facebook "
                     "and I want everything done including tiling and a new geyser"),
        got="ok",
    )
    class _FakeSelfFB(_FakeSelfFollowup):
        _is_facebook_price_ref = staticmethod(ResponseMixin._is_facebook_price_ref)
        _facebook_price_confirm_reply = ResponseMixin._facebook_price_confirm_reply
    _fbrep2 = _FakeSelfFB("area")._facebook_price_confirm_reply("english")
    results.log(
        "facebook price ref: reply confirms FB pricing + US$800 package contents",
        "Facebook" in _fbrep2 and "US$800" in _fbrep2
        and "freestanding tub and side chamber" in _fbrep2,
        got=_fbrep2,
    )
    results.log(
        "captured flow answer: 'that on facebook' never claimed",
        _fca._is_captured_flow_answer("that on facebook", ['project_description']) is False,
        got=str(_fca._is_captured_flow_answer("that on facebook", ['project_description'])),
    )
    # Service-type-only detector: bare service categories are NOT a description;
    # anything with a concrete item or real detail is.
    _sto = ResponseMixin._is_service_type_only
    STO_CASES = [
        ("Bathroom and kitchen installations.", True),
        ("bathroom renovation", True),
        ("Kitchen installation", True),
        ("new plumbing installation", True),
        ("bathroom", True),
        ("full bathroom and kitchen renovations", True),
        ("fit tub and shower", False),
        ("all services needed on a new house", False),
        ("shower cubicle", False),
        ("replace my geyser", False),
        ("new installation in Graylands park", False),
    ]
    _sto_ok = all(_sto(m) is e for m, e in STO_CASES)
    results.log(
        "service-type-only: categories yes, concrete items/details no",
        _sto_ok,
        got="; ".join(f"{m[:24]!r}->{_sto(m)}" for m, e in STO_CASES if _sto(m) is not e)
            or "all as expected",
    )
    # At the description stage, a service-type-only reply is STILL the flow answer
    # even with nothing stored (extraction skips it on the first pass) — it must
    # route to the scripted description question, never the quote pitch.
    _fca_desc = _FakeSelfFA(nq="project_description")
    results.log(
        "captured flow answer: service-type-only at description stage -> flow answer (asks description)",
        _fca_desc._is_captured_flow_answer("Bathroom and kitchen installations.", []) is True
        and _fca_desc._is_captured_flow_answer("Need a quote to fit tub and shower", []) is False,
        got=f"svc-only={_fca_desc._is_captured_flow_answer('Bathroom and kitchen installations.', [])}",
    )
    # _product_price_close (tub / Facebook-package replies): value-check first,
    # then the open "which one?" question once a tie-down has gone out.
    _pc1 = _FakeSelfFollowup("project_description")._product_price_close("english")
    results.log(
        "product price close: no prior tie-down -> budget tie-down first",
        "with your budget" in _pc1.lower(),
        got=str(_pc1),
    )
    _pc2 = _FakeSelfFollowup(
        "project_description", history=_bot(_TD)
    )._product_price_close("english")
    results.log(
        "product price close: after a tie-down -> open 'which one?' question",
        _pc2 == "What did you have in mind?",
        got=str(_pc2),
    )
    # A budget-fit close ("looking to invest") counts as a tie-down, so the next
    # product close does NOT stack a second yes.
    _bf = "Is that around what you were looking to invest to get it sorted properly?"
    _pc3 = _FakeSelfFollowup(
        "project_description", history=_bot(_bf)
    )._product_price_close("english")
    results.log(
        "product price close: budget-fit close counts as a tie-down (no stack)",
        _pc3 == "What did you have in mind?",
        got=str(_pc3),
    )
    # Price replies close on the budget tie-down (business preference), EN + Shona.
    _pt_en = _FakeSelfFollowup("service_type")._price_tiedown("english")
    _pt_sn = _FakeSelfFollowup("service_type")._price_tiedown("shona")
    results.log(
        "price tie-down: budget-fit close (EN + Shona)",
        _pt_en == "That sit alright with your budget?" and "budget" in _pt_sn.lower(),
        got=f"en={_pt_en!r} sn={_pt_sn!r}",
    )
    # The budget tie-down counts as a tie-down for stacking purposes.
    results.log(
        "price tie-down: registered as a tie-down signature",
        _FakeSelfFollowup("service_type", history=_bot(_pt_en))._last_assistant_was_tiedown() is True,
        got="ok",
    )
except Exception as e:
    results.log("tie-down helpers", False, got=str(e))

# Pricing copy: compose snippets break down supply + install, and the price
# disclaimer is reworded to "once the plumber sees the space" (no "on-site visit").
try:
    _snips = ResponseMixin._COMPOSE_SNIPPETS
    results.log(
        "compose snippets: shower breaks down supply + install",
        "supply from US$130 + install from US$40" in _snips['shower_cubicle'],
        got=_snips['shower_cubicle'],
    )
    results.log(
        "compose snippets: vanity breaks down supply + install",
        "supply from US$150 + install from US$30" in _snips['vanity'],
        got=_snips['vanity'],
    )
    _disc = _FakeSelfFollowup("service_type")._ensure_price_disclaimer(
        'geyser', "Geysers from US$160 all-in.\n\nWhat day suits you?"
    )
    results.log(
        "price disclaimer: reworded to 'sees the space', no 'on-site visit'",
        "once the plumber sees the space" in _disc and "on-site visit" not in _disc,
        got=_disc,
    )
    # Idempotent: a reply that already carries the combined 'ballpark … sees the
    # space' disclaimer must NOT get a second 'approximate starting prices' one.
    _bp = ("Tub from US$160.\n\nThese are ballpark; the exact figure is confirmed "
           "once the plumber sees the space.\n\nThat sit alright with your budget?")
    _bpd = _FakeSelfFollowup("service_type")._ensure_price_disclaimer('combined_pricing', _bp)
    results.log(
        "price disclaimer: idempotent — no double disclaimer on the ballpark reply",
        _bpd == _bp and "approximate starting prices" not in _bpd,
        got=_bpd,
    )
    # Facebook/tub overview reply: supply+install breakdown kept, disclaimer
    # inserted BEFORE the closing budget tie-down (the bug: it had neither).
    _fbrep = (
        "Our Facebook package is US$800 — a freestanding tub and side chamber.\n\n"
        "If you're looking at just a tub — freestanding tubs from US$670 all-in "
        "(tub US$400 + mixer US$150 + install US$120), and standard built-in tubs "
        "from US$160 all-in (tub US$80 + install US$80).\n\n"
        "That sit alright with your budget?"
    )
    _fbd = _FakeSelfFollowup("project_description")._ensure_price_disclaimer('facebook_package', _fbrep)
    results.log(
        "facebook overview: breakdown kept + disclaimer before the budget tie-down",
        ("tub US$400 + mixer US$150 + install US$120" in _fbd
         and "once the plumber sees the space" in _fbd
         and _fbd.rstrip().endswith("That sit alright with your budget?")),
        got=_fbd[-140:],
    )
except Exception as e:
    results.log("pricing copy (snippets/disclaimer)", False, got=str(e))

# Budget objection: a 'no' to "That sit alright with your budget?" must be handled
# (ask their budget + tailor), not swallowed by the booking flow as a stage answer.
try:
    BUDGET_DECLINE_CASES = [
        ("not really", True), ("no", True), ("nah", True), ("too much", True),
        ("that's too expensive", True), ("a bit much honestly", True),
        ("kwete", True), ("inodhura", True),
        ("yes", False), ("sure that works", False), ("no problem", False),
        ("around $300", False), ("what about cheaper options", False),
    ]
    _bfake = _FakeSelfFollowup("project_description")
    # _is_budget_decline is AI-primary; the deterministic gate tests the keyword
    # fallback (same convention as _classify_affirmation_keywords).
    for _msg, _exp in BUDGET_DECLINE_CASES:
        _g = _bfake._is_budget_decline_keywords(_msg)
        results.log(f"_is_budget_decline_keywords: '{_msg[:24]}'", _g == _exp,
                    expected=str(_exp), got=str(_g))
    # Only fires when the last bot turn was the budget tie-down.
    _bt = _FakeSelfFollowup("project_description", history=_bot("That sit alright with your budget?"))
    _nt = _FakeSelfFollowup("project_description", history=_bot("Whereabouts are you based?"))
    results.log(
        "budget objection: detects the preceding budget tie-down",
        _bt._last_assistant_was_price_tiedown() is True
        and _nt._last_assistant_was_price_tiedown() is False,
        got=f"after_budget={_bt._last_assistant_was_price_tiedown()} after_other={_nt._last_assistant_was_price_tiedown()}",
    )
    _bo = _bfake._handle_budget_objection("english")
    results.log(
        "budget objection: reframes all-in value, offers the exact number (no negotiating)",
        ("everything in" in _bo and "supply, install" in _bo
         and "no extras on the day" in _bo and "exact number for your space" in _bo),
        got=_bo,
    )
    # After a scope answer ("a tub and chamber"), advance to the next booking field
    # using the EXACT approved script — never a paraphrase, never a price.
    _adv_area = _FakeSelfFollowup("area")._advance_after_scope("english")
    _adv_none = _FakeSelfFollowup("project_description")._advance_after_scope("english")
    results.log(
        "advance after scope: area uses the exact script (not a paraphrase), no price",
        _adv_area == "All good, what area are you in?" and "US$" not in _adv_area
        and _adv_none is None,
        got=f"area={_adv_area!r} none={_adv_none!r}",
    )
    # "No" to the value-check close ("Anything else on the property?") means
    # "nothing else, proceed" — NOT a disengagement. Detect the close, and treat a
    # bare negative/ack as complete so the webhook advances to booking instead of
    # letting semantic-rescue misread it as declining the whole job.
    _vc = _FakeSelfFollowup("area", history=_bot(_TD))
    _not_vc = _FakeSelfFollowup("area", history=_bot("All good, what area are you in?"))
    results.log(
        "value-check close: detected as last turn (and not confused with a field question)",
        _vc._last_assistant_was_value_check() is True
        and _not_vc._last_assistant_was_value_check() is False,
        got=f"after_vc={_vc._last_assistant_was_value_check()} after_field={_not_vc._last_assistant_was_value_check()}",
    )
    results.log(
        "value-check close: bare negatives/acks are 'nothing else'; items/questions are not",
        all(ResponseMixin._is_nothing_else_reply(m) for m in
            ("No", "nope", "nothing else", "that's all", "Ok", "kwete"))
        and not any(ResponseMixin._is_nothing_else_reply(m) for m in
            ("also a toilet", "how much?", "yes a geyser too")),
        got="; ".join(f"{m}={ResponseMixin._is_nothing_else_reply(m)}" for m in
                       ("No", "also a toilet", "how much?")),
    )
except Exception as e:
    results.log("budget objection", False, got=str(e))

# Date-stage timeline-pivot dispatcher (Phase 1): DeepSeek resolves offered_date,
# code does the math only. >7 days out parks; within a week keeps booking with an
# assumptive close; a soft timeframe asks them to pin the day. Deterministic.
class _FakeApptPivot:
    def __init__(self):
        self.is_delayed = False
        self.scheduled_datetime = None
        self.internal_notes = ''
        self.delay_followup_due_at = None
    def mark_delayed(self, source_message='', save=True):
        self.is_delayed = True
        return True
    def unpark(self, save=True):
        return False
    def save(self, update_fields=None):
        pass
class _FakeSelfPivot:
    _dispatch_timeline_pivot = ResponseMixin._dispatch_timeline_pivot
    _park_timeline_lead = ResponseMixin._park_timeline_lead
    _lock_visit_date = ResponseMixin._lock_visit_date
    _friendly_visit_date = ResponseMixin._friendly_visit_date
    def __init__(self):
        self.appointment = _FakeApptPivot()
try:
    _today = "2026-07-01"  # Wednesday
    _p_none = _FakeSelfPivot()._dispatch_timeline_pivot("area", "2026-07-15", None, _today)
    results.log("timeline pivot: non-date stage -> None (only fires at date stage)",
                _p_none is None, got=str(_p_none))
    # >7 days out -> park + follow-up scheduled, booking flow stops.
    _sp = _FakeSelfPivot()
    _p_far = _sp._dispatch_timeline_pivot("availability_date", "2026-07-15", None, _today)
    results.log("timeline pivot: >7 days out -> park (no date chase)",
                _p_far is not None and "reach out closer" in _p_far
                and _sp.appointment.is_delayed is True,
                got=str(_p_far))
    # <=7 days hard date -> lock the day, ask an assumptive time slot, not parked.
    _sn = _FakeSelfPivot()
    _p_near = _sn._dispatch_timeline_pivot("availability_date", "2026-07-03", None, _today)
    results.log("timeline pivot: <=7 days -> lock date + assumptive time slot",
                _p_near is not None and "morning slot" in _p_near
                and _sn.appointment.scheduled_datetime is not None
                and _sn.appointment.is_delayed is False,
                got=str(_p_near))
    # Soft timeframe only -> pin the day assumptively (echo their timeframe), not parked.
    _st = _FakeSelfPivot()
    _p_tf = _st._dispatch_timeline_pivot("availability_date", None, "end of the month", _today)
    results.log("timeline pivot: soft timeframe -> assumptive pin-the-day, not parked",
                _p_tf is not None and "end of the month" in _p_tf
                and "start of that" in _p_tf and _st.appointment.is_delayed is False,
                got=str(_p_tf))
    # No date/timeframe -> None (fall through to normal flow).
    _p_fall = _FakeSelfPivot()._dispatch_timeline_pivot("availability_date", None, None, _today)
    results.log("timeline pivot: no date/timeframe -> None (fall through)",
                _p_fall is None, got=str(_p_fall))
    # Exactly 7 days out is still 'within a week' (boundary) -> continue, not park.
    _s7 = _FakeSelfPivot()
    _p7 = _s7._dispatch_timeline_pivot("availability_date", "2026-07-08", None, _today)
    results.log("timeline pivot: exactly 7 days -> continue (boundary), not park",
                _p7 is not None and "morning slot" in _p7 and _s7.appointment.is_delayed is False,
                got=str(_p7))
    # Accessors return the signals with safe defaults.
    from bot.unified_classifier import (
        uc_pivoted_to_timeline as _ucp, uc_offered_date as _ucd,
        uc_offered_timeframe as _uct,
    )
    _uc = {"pivoted_to_timeline": True, "offered_date": "2026-07-03", "offered_timeframe": None}
    results.log("uc signal accessors: pivot/date/timeframe + safe defaults",
                _ucp(_uc) is True and _ucd(_uc) == "2026-07-03" and _uct(_uc) is None
                and _ucp(None) is False and _ucd({}) is None,
                got="ok")
except Exception as e:
    results.log("timeline pivot dispatcher", False, got=str(e))

# FAQ is answered AI-primary (ai_answer_faq, grounded in the fact) so it doesn't
# sound copy-pasted; the canned fact is the fallback. Facts are now PURE (no baked
# close); the qualifying close is appended by the caller. AI is non-deterministic,
# so the gate pins the fact + fallback shape only.
try:
    from bot.faq import lookup_faq as _lookup_faq
    _loc = _lookup_faq("where are you based")
    results.log(
        "faq fact: pure fact, no baked-in qualifying close",
        _loc is not None and "Hatfield" in _loc and "else on the property" not in _loc,
        got=str(_loc),
    )
    _faq_fallback = _FakeSelfFollowup("service_type")._append_tiedown(_loc, "english")
    results.log(
        "faq fallback: canned fact gets the qualifying close appended",
        _faq_fallback.startswith(_loc) and "else on the property" in _faq_fallback,
        got=_faq_fallback[-80:],
    )
    # Topic routing + the service-question gate (drives ai_answer_faq's item-naming
    # continuation): a specific "do you do X" is a SERVICES availability question;
    # "do you have another number" is a contact question, not a service one.
    from bot.faq import match_faq_topic as _mft
    _svc_q = (_mft("do you have shower rooms") == 'services'
              and _fa._is_product_availability_question("do you have shower rooms"))
    _contact_q = (_mft("do you have another number") == 'services')
    results.log(
        "faq service-question gate: 'do you have shower rooms' -> services availability",
        _svc_q is True and _contact_q is False,
        got=f"service={_svc_q} contact_as_service={_contact_q}",
    )
    # First-pass service continuation is the EXACT script (item filled in); only a
    # repeat ask paraphrases (ai_answer_faq). Consistency first, vary on retry.
    _scr = _FakeSelfFollowup("service_type")._service_continuation_reply("shower cubicle", "english")
    results.log(
        "service continuation: exact scripted first-pass reply (item filled in)",
        _scr == ("Yes, we handle shower cubicle and all related plumbing work.\n\n"
                 "Is a shower cubicle the only thing you're looking to get sorted?"),
        got=_scr,
    )
    # The service they asked about is captured as the project so a following "Yes"
    # advances instead of re-asking. _derive_service_item pulls the item out.
    from bot.whatsapp_webhook import _derive_service_item as _dsi
    DERIVE_CASES = [
        ("do you have shower rooms", "shower rooms"),
        ("do you do renovations", "renovations"),
        ("do you install geysers", "geysers"),
        ("do you sell vanities?", "vanities"),
        ("shower room", "shower room"),
    ]
    _dok = all(_dsi(_m) == _e for _m, _e in DERIVE_CASES)
    results.log(
        "derive service item: strips the availability prefix to the project phrase",
        _dok,
        got="; ".join(f"{_m!r}->{_dsi(_m)!r}" for _m, _e in DERIVE_CASES),
    )
    # "No, also a toilet" -> the extra item is pulled out and appended to the project.
    from bot.whatsapp_webhook import _derive_additional_items as _dai
    ADD_CASES = [
        ("No, also a toilet", "toilet"),
        ("and a geyser too", "geyser too"),
        ("no just add a vanity", "vanity"),
        ("also a shower", "shower"),
    ]
    _aok = all(_dai(_m) == _e for _m, _e in ADD_CASES)
    results.log(
        "derive additional items: strips the 'no/also/and' lead-in to the extra item",
        _aok,
        got="; ".join(f"{_m!r}->{_dai(_m)!r}" for _m, _e in ADD_CASES),
    )
    # A dynamic answer that opens by echoing the customer's message gets the echo
    # stripped (prod: bot parroted "Hello! Do you for shower rooms" back).
    class _FakeSelfEcho:
        _strip_leading_echo = ResponseMixin._strip_leading_echo
    _fe = _FakeSelfEcho()
    _e1 = _fe._strip_leading_echo(
        "Hello! Do you for shower rooms\n\nYes, we do shower rooms.",
        "Hello! Do you for shower rooms")
    _e2 = _fe._strip_leading_echo("Yes, we do shower rooms.", "Hello! Do you for shower rooms")
    results.log(
        "strip leading echo: removes a parroted message, leaves a clean answer alone",
        _e1 == "Yes, we do shower rooms." and _e2 == "Yes, we do shower rooms.",
        got=f"{_e1!r} | {_e2!r}",
    )
    # A dynamic answer cut off by max_tokens mid-sentence ('...property in') must
    # have the dangling fragment trimmed before a booking nudge is appended —
    # otherwise the lead sees "...come to your property in\n\nWould tomorrow…?".
    _tis = ResponseMixin._trim_incomplete_sentence
    _trunc = ("For a new house we handle the full package. The best way to start "
              "is for our plumber to come to your property in")
    _trimmed = _tis(_trunc)
    results.log(
        "trim incomplete sentence: drops a max_tokens-truncated dangling fragment",
        _trimmed == "For a new house we handle the full package."
        and not _trimmed.endswith(" in"),
        got=repr(_trimmed),
    )
    results.log(
        "trim incomplete sentence: leaves complete / unpunctuated-but-whole replies untouched",
        _tis("Yes, we can help with that.") == "Yes, we can help with that."
        and _tis("Shower cubicles from US$170 all-in (supply + install)")
            == "Shower cubicles from US$170 all-in (supply + install)"
        and _tis("Yes we can sort that out") == "Yes we can sort that out",
        got=f"{_tis('Yes we can sort that out')!r}",
    )
except Exception as e:
    results.log("faq ai-primary fallback", False, got=str(e))

# When the lead names the items, record them as the project_description so the
# follow-up advances to the next step (area/visit) instead of re-asking "what are
# you targeting?". Production: "Need a quote to fit tub and shower" then re-asked
# what they wanted. API-free: a tiny fake appointment.
class _FakeApptDesc:
    def __init__(self, desc=None):
        self.project_description = desc
        self._saved = None
    def save(self, update_fields=None):
        self._saved = update_fields
class _FakeSelfCapture:
    _PRODUCT_FAMILY_PATTERNS = ResponseMixin._PRODUCT_FAMILY_PATTERNS
    _product_families_in = ResponseMixin._product_families_in
    _capture_named_products_as_description = ResponseMixin._capture_named_products_as_description
    def __init__(self, appt):
        self.appointment = appt
try:
    _ap = _FakeApptDesc(desc=None)
    _FakeSelfCapture(_ap)._capture_named_products_as_description("Need a quote to fit tub and shower")
    results.log(
        "capture: records named items as the description when empty",
        _ap.project_description == "shower and tub",
        got=str(_ap.project_description),
    )
    _ap2 = _FakeApptDesc(desc="full bathroom redo")
    _FakeSelfCapture(_ap2)._capture_named_products_as_description("How much tab and shower")
    results.log(
        "capture: leaves an existing description untouched",
        _ap2.project_description == "full bathroom redo",
        got=str(_ap2.project_description),
    )
except Exception as e:
    results.log("_capture_named_products_as_description", False, got=str(e))
# Job / multi-item quotes route to the free on-site quote (no chat price block);
# single-product price questions still price. Production bug: "Need a quote to fit
# tub and shower" dumped a shower-cubicle price block (appt 475). API-free regex.
JOB_QUOTE_CASES = [
    ("Need a quote to fit tub and shower",   True),   # the bug: labour + 2 items
    ("quote to install a geyser",            True),   # labour verb
    ("can you renovate my bathroom",         True),   # labour verb
    ("how much for a tub and a toilet",      True),   # 2 product families
    ("How much tab and shower",              True),   # 'tab' typo for tub/tap → 2 items
    ("How Tab and shower",                   True),   # same, no price word
    ("redo my bathroom",                     True),
    ("how much is a shower cubicle",         False),  # single product → still prices
    ("shower cubicle price",                 False),  # single product
    ("do you sell geysers",                  False),  # single product availability
    ("how much for a vanity",                False),  # single product
    ("benefit of a shower",                  False),  # 'fit' inside 'benefit' must NOT match
    ("is the table included",                False),  # 'tab' inside 'table' must NOT match
]
for msg, expected in JOB_QUOTE_CASES:
    try:
        got = _fb._is_job_quote_request(msg)
        results.log(
            f"_is_job_quote_request: '{msg[:30]}'",
            got == expected,
            f"job={got}",
            expected=f"job={expected}",
            got=f"job={got}",
        )
    except Exception as e:
        results.log(f"_is_job_quote_request: '{msg[:30]}'", False, got=str(e))
PURCHASE_COMMITMENT_CASES = [
    ("I want to purchase 2x shower cubicles and asseries", True),   # the bug
    ("I want to buy a geyser",        True),
    ("I'd like to order a vanity",    True),
    ("can I buy two toilets",         True),
    ("I want 3 shower cubicles",      True),
    ("looking to install a new tub",  True),
    ("I'll take it",                  True),
    ("do you install geysers in garages", False),  # a QUESTION, must still be answered
    ("how much for a shower cubicle", False),       # price ask, not a commitment route
    ("I want to get more information", False),       # 'get' is not a buy verb
    ("where are you based",           False),
]
for msg, expected in PURCHASE_COMMITMENT_CASES:
    try:
        got = _fb._is_purchase_commitment(msg)
        results.log(
            f"_is_purchase_commitment: '{msg[:30]}'",
            got == expected,
            f"commit={got}",
            expected=f"commit={expected}",
            got=f"commit={got}",
        )
    except Exception as e:
        results.log(f"_is_purchase_commitment: '{msg[:30]}'", False, got=str(e))

# A delay-signal lead who was offered the portfolio and replies "send it on
# WhatsApp / to this number" must be routed to the lead-magnet PDF, not the
# photo gallery. The webhook gates the gallery handlers on this deterministic
# delivery-channel check; an email reply (they chose email) must NOT trip it.
from bot.out_of_scope_handler import wants_whatsapp_delivery
# (message, expected: is this a "send it here on WhatsApp" delivery request?)
WA_DELIVERY_CASES = [
    ("You can send a pdf on this number", True),   # the production case
    ("send it on whatsapp",               True),
    ("just send it here",                 True),
    ("send through this app",             True),
    ("yes send them over",                True),
    ("jones86xi@gmail.com",               False),  # chose email, not WhatsApp
    ("email it to me at a@b.com",         False),  # email address present
    ("next week",                         False),  # timeframe, not a delivery ask
    ("no thanks",                         False),
]
for msg, expected in WA_DELIVERY_CASES:
    try:
        got = wants_whatsapp_delivery(msg)
        results.log(
            f"wants_whatsapp_delivery: '{msg[:30]}'",
            got == expected,
            f"wa={got}",
            expected=f"wa={expected}",
            got=f"wa={got}",
        )
    except Exception as e:
        results.log(f"wants_whatsapp_delivery: '{msg[:30]}'", False, got=str(e))

# CTWA (Click-to-WhatsApp ad) follow-up cadence. Ad leads must use the longer
# 72h schedule — absolute offsets from the lead's last response: FU1 4h, FU2 8h,
# FU3 24h, FU4 48h (2 in 0-24h, 1 in 24-48h, 1 in 48-72h) — while non-ad leads
# keep the original tier cadence. Pinned API-free with a stub lead.
from datetime import timedelta as _td
from django.utils import timezone as _tz
from bot.management.commands.send_followups import (
    Command as _FollowupCmd, CTWA_FOLLOWUP_OFFSETS as _CTWA_OFFS,
)
from bot.models import LeadStatus as _LS

class _StubLead:
    """Minimal duck-typed lead for the follow-up timing helpers (no DB)."""
    def __init__(self, ctwa, followup_count, hours_since_resp,
                 is_lead_active=True, status='pending', followup_stage=None):
        self.id = 4242  # stable id -> deterministic jitter
        self.lead_status = _LS.COLD
        self.followup_count = followup_count
        self.is_lead_active = is_lead_active
        self.status = status
        self.followup_stage = followup_stage
        ref = _tz.now() - _td(hours=hours_since_resp)
        self.last_customer_response = ref
        self.last_followup_sent = ref
        self.created_at = ref
        self.ctwa_entry_at = ref if ctwa else None

_fu = _FollowupCmd()

# (label, ctwa, followup_count, hours_since_resp, expected_ready)
# Use ±1.5h margins so deterministic jitter (3-57 min) never flips the result.
_CTWA_CADENCE_CASES = [
    ("CTWA FU1 before 4h",  True, 0, 2.0,  False),
    ("CTWA FU1 after 4h",   True, 0, 6.0,  True),
    ("CTWA FU2 before 8h",  True, 1, 6.0,  False),
    ("CTWA FU2 after 8h",   True, 1, 10.0, True),
    ("CTWA FU3 before 24h", True, 2, 22.0, False),
    ("CTWA FU3 after 24h",  True, 2, 26.0, True),
    ("CTWA FU4 before 48h", True, 3, 46.0, False),
    ("CTWA FU4 after 48h",  True, 3, 50.0, True),
    # Non-ad COLD lead must NOT use the 72h offsets: at 26h with 2 prior sends
    # it'd be "after 24h" under CTWA, but the tier path measures from the last
    # send (here = last response) with a 6h step, so it IS ready — proving the
    # branch only changes ad leads. The discriminating case is FU1 timing:
    ("non-CTWA FU1 before 4h", False, 0, 2.0, False),  # COLD tier[0]=4h
    ("non-CTWA FU1 after 4h",  False, 0, 6.0, True),
]
for label, ctwa, cnt, hrs, expected in _CTWA_CADENCE_CASES:
    try:
        got, _reason = _fu._is_ready_for_followup(_StubLead(ctwa, cnt, hrs), None, force=True)
        results.log(
            f"followup cadence: {label}",
            got == expected,
            f"ready={got}",
            expected=f"ready={expected}",
            got=f"ready={got}",
        )
    except Exception as e:
        results.log(f"followup cadence: {label}", False, got=str(e))

# Offsets themselves are the contract — pin them so a refactor can't silently
# change the schedule.
results.log(
    "followup cadence: CTWA offsets are (4, 8, 24, 48)",
    _CTWA_OFFS == (4, 8, 24, 48),
    f"offsets={_CTWA_OFFS}",
    expected="(4, 8, 24, 48)",
    got=str(_CTWA_OFFS),
)

# next_followup_due_at powers the UI "next follow-up" chip. It must agree with the
# cron's timing core and return None when the lead is not in the auto flow.
def _due(lead):
    return _fu.next_followup_due_at(lead)

# CTWA lead, no follow-ups yet → attempt 1, due ~4h after last response, ad flag set.
_info = _due(_StubLead(True, 0, 0.0))
results.log(
    "next_followup_due_at: CTWA FU1 attempt+flag",
    bool(_info) and _info['attempt'] == 1 and _info['max'] == 4 and _info['is_ctwa'] is True,
    got=str(_info),
)
# The displayed due time is clamped to the daily contact window (it only sends
# when the window is open), so it must always land inside a CONTACT_WINDOW.
_due_local = _tz.localtime(_info['due_at']) if _info else None
results.log(
    "next_followup_due_at: due time lands inside the contact window",
    _due_local is not None and _fu._in_contact_window(_due_local),
    got=_due_local.strftime('%H:%M') if _due_local else 'None',
)

# _next_window_open: a due moment outside 08:21-20:53 rolls to the next opening.
import pytz as _pytz
_sast = _pytz.timezone('Africa/Johannesburg')
def _win(h, m):
    dt = _sast.localize(__import__('datetime').datetime(2026, 6, 23, h, m))
    return _tz.localtime(_fu._next_window_open(dt)).strftime('%Y-%m-%d %H:%M')
results.log("next_window_open: 01:52 -> same-day 08:21",
            _win(1, 52) == '2026-06-23 08:21', got=_win(1, 52))
results.log("next_window_open: 12:00 stays 12:00 (in window)",
            _win(12, 0) == '2026-06-23 12:00', got=_win(12, 0))
results.log("next_window_open: 21:30 -> next-day 08:21",
            _win(21, 30) == '2026-06-24 08:21', got=_win(21, 30))
# Non-CTWA COLD lead, no follow-ups → attempt 1, ad flag false.
_info2 = _due(_StubLead(False, 0, 0.0))
results.log(
    "next_followup_due_at: non-CTWA flag false",
    bool(_info2) and _info2['is_ctwa'] is False,
    got=str(_info2),
)
# Retired / not-in-flow leads return None.
results.log("next_followup_due_at: None when count>=max",
            _due(_StubLead(True, 4, 0.0)) is None, got=str(_due(_StubLead(True, 4, 0.0))))
results.log("next_followup_due_at: None when inactive",
            _due(_StubLead(True, 0, 0.0, is_lead_active=False)) is None)
results.log("next_followup_due_at: None when booked",
            _due(_StubLead(True, 0, 0.0, status='confirmed')) is None)
results.log("next_followup_due_at: None when stage completed",
            _due(_StubLead(True, 0, 0.0, followup_stage='completed')) is None)

# Messaging-window tags: 24h standard (reset by last message) vs 72h CTWA ad
# window (from ad entry, extended by later messages — whichever is later).
from bot.models import Appointment as _Appt

def _mk_appt(ctwa_hours_ago=None, last_msg_hours_ago=None):
    a = _Appt()
    if last_msg_hours_ago is not None:
        a.last_inbound_at = _tz.now() - _td(hours=last_msg_hours_ago)
    if ctwa_hours_ago is not None:
        a.ctwa_entry_at = _tz.now() - _td(hours=ctwa_hours_ago)
    return a

# Organic lead, messaged 1h ago → 24h window, open, closes ~23h out.
_o = _mk_appt(last_msg_hours_ago=1)
results.log("messaging window: organic kind=24h",
            _o.messaging_window_kind == '24h', got=_o.messaging_window_kind)
results.log("messaging window: organic open within 24h",
            _o.messaging_window_open is True)
_o_h = (_o.messaging_window_closes_at - _tz.now()).total_seconds() / 3600
results.log("messaging window: organic closes ~23h out",
            22.5 <= _o_h <= 23.5, got=f"{_o_h:.2f}h")

# Organic lead, messaged 25h ago → closed.
results.log("messaging window: organic closed after 25h",
            _mk_appt(last_msg_hours_ago=25).messaging_window_open is False)

# Fresh ad lead (entry 1h ago) → 72h window, closes ~71h out (entry+72h wins).
_ad = _mk_appt(ctwa_hours_ago=1, last_msg_hours_ago=1)
results.log("messaging window: ad kind=72h",
            _ad.messaging_window_kind == '72h', got=_ad.messaging_window_kind)
_ad_h = (_ad.messaging_window_closes_at - _tz.now()).total_seconds() / 3600
results.log("messaging window: ad closes ~71h out (72h from entry)",
            70.5 <= _ad_h <= 71.5, got=f"{_ad_h:.2f}h")

# Ad lead 80h past entry but messaged 1h ago → 24h rule keeps it open (max wins).
_ad2 = _mk_appt(ctwa_hours_ago=80, last_msg_hours_ago=1)
results.log("messaging window: ad past 72h but recent msg stays open",
            _ad2.messaging_window_open is True)
results.log("messaging window: still tagged 72h (lead type)",
            _ad2.messaging_window_kind == '72h')

# Ad lead 80h past entry and last message 30h ago → fully closed.
results.log("messaging window: ad fully closed",
            _mk_appt(ctwa_hours_ago=80, last_msg_hours_ago=30).messaging_window_open is False)

# Outbound send retry: a transient reset (ECONNRESET, the reported prod error)
# must be retried, not silently dropped; a permanent 4xx must NOT be retried.
try:
    import bot.whatsapp_cloud_api as _wce
    _api = _wce.WhatsAppCloudAPI()
    _api._RETRY_BASE_DELAY = 0  # no real backoff sleeps in the test

    class _FakeResp:
        def __init__(self, status): self.status_code = status
    _orig_post = _wce.requests.post
    try:
        # Reset twice, then succeed → the helper should retry through to the 200.
        _calls = {'n': 0}
        def _flaky_post(*a, **k):
            _calls['n'] += 1
            if _calls['n'] < 3:
                raise _wce.requests.exceptions.ConnectionError('reset by peer')
            return _FakeResp(200)
        _wce.requests.post = _flaky_post
        _ok = _api._post_with_retry('http://x', {'m': 1}, label='test')
        results.log(
            "send retry: recovers after transient resets (no silent drop)",
            _ok.status_code == 200 and _calls['n'] == 3,
            got=f"status={_ok.status_code} attempts={_calls['n']}",
        )
        # A 4xx (bad token/payload) is permanent — returned on the first try, no retry.
        _calls2 = {'n': 0}
        def _bad_post(*a, **k):
            _calls2['n'] += 1
            return _FakeResp(401)
        _wce.requests.post = _bad_post
        _r = _api._post_with_retry('http://x', {'m': 1}, label='test')
        results.log(
            "send retry: does NOT retry a permanent 4xx",
            _r.status_code == 401 and _calls2['n'] == 1,
            got=f"status={_r.status_code} attempts={_calls2['n']}",
        )
    finally:
        _wce.requests.post = _orig_post
except Exception as e:
    results.log("send retry", False, got=str(e))

# In gate mode we stop here: TEST 0 above is the API-free deterministic
# regression block (every production bug we've fixed is pinned there). The
# TEST 1+ sections below exercise the live LLM's accuracy — valuable as a quality
# signal, but inherently fuzzy, so they are NOT a commit gate.
if GATE_ONLY:
    _finish()

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
checks = ['800', 'US$', 'approximate']
passed = check_response_quality('facebook_package', resp, checks)
results.log("pricing: facebook_package contains US$800 + disclaimer", passed, got=resp[:100])

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

# "Which plumber is coming?" must name the plumber + protected contact number.
# Unified on Takudzwa (2026-07-02): emails are signed Takudzwa, the FAQ and the
# dynamic prompts say Takudzwa — a chat naming a different person than the email
# signature was the real inconsistency (conv 369 got both names in two turns).
_r = bot._maybe_answer_identity_question("Which plumber is coming to my house?")
results.log(
    "direct-q: 'which plumber is coming?' names the plumber (conv 369)",
    _r is not None and 'takudzwa' in _r.lower() and '263774819901' in _r,
    expected="answer naming Takudzwa + number", got=str(_r),
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

_finish()