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
from bot.out_of_scope_handler import _handle_delay_timeframe_answer
class _FakeApptTf:
    internal_notes = ''
    customer_email = None
    project_type = 'bathroom_renovation'
    def save(self, update_fields=None):
        pass
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
# 2-week follow-up date, and after sending the PDF on WhatsApp we schedule a
# near-term afternoon check-in ONLY if the messaging window is still open ~2 days
# out (72h ad leads) — organic 24h leads keep the longer date. Plus the AI-first
# email-step intent classifier's deterministic fallback contract.
import types as _types
import pytz as _pytz
from datetime import datetime as _dt_t, timedelta as _td_t
from bot.out_of_scope_handler import (
    _default_followup_iso, _compute_afternoon_checkin,
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

try:
    _organic = _types.SimpleNamespace(messaging_window_closes_at=_now_fixed + _td_t(hours=24))
    _ad      = _types.SimpleNamespace(messaging_window_closes_at=_now_fixed + _td_t(hours=72))
    _ci_org  = _compute_afternoon_checkin(_organic, now=_now_fixed)
    _ci_ad   = _compute_afternoon_checkin(_ad, now=_now_fixed)
    results.log("_compute_afternoon_checkin: organic 24h → skip (None)",
                _ci_org is None, got=str(_ci_org), expected="None")
    results.log("_compute_afternoon_checkin: ad 72h → 2pm check-in",
                _ci_ad is not None and _ci_ad.hour == 14,
                got=str(_ci_ad), expected="a 14:00 datetime")
except Exception as e:
    results.log("_compute_afternoon_checkin", False, got=str(e))

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
    _FAMILY_ROUGH_PRICE = ResponseMixin._FAMILY_ROUGH_PRICE
    _FAMILY_PRICE_COMPONENTS = ResponseMixin._FAMILY_PRICE_COMPONENTS
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
    _asks_about_labour = ResponseMixin._asks_about_labour
    _capture_named_products_as_description = ResponseMixin._capture_named_products_as_description
    _build_combined_price_reply = ResponseMixin._build_combined_price_reply
    def __init__(self, appointment=None):
        self.appointment = appointment
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
        "ballpark" in _cr and "free on-site" in _cr and "approximate starting" not in _cr,
        got=_cr[-90:],
    )
    # A plain multi-item price ask must NOT dump the supply/labour split.
    results.log(
        "_build_combined_price_reply: no labour split unless asked",
        "fitted" not in _cr and "labour from" not in _cr,
        got=_cr[:120],
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
        ("accessories on top" in _lab and "ballpark" in _lab and "free on-site" in _lab),
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
    def __init__(self, appt):
        self.appointment = appt
def _bot(*contents):
    return [{'role': 'assistant', 'content': c} for c in contents]
try:
    # Transcript case: area answered (Greendale) AND a day already offered
    # ("work better for you"); scope known, accessories mentioned -> every earlier
    # stage covered, so it lands on a FRESH booking question (not a repeat day push).
    _fq = _FakeSelfForward(_FakeApptFwd(
        customer_area="Greendale",
        history=_bot("Would tomorrow or this Friday work better for you?"),
    ))._next_forward_question("english", scope=[('shower', 2)], has_accessories=True)
    results.log(
        "forward Q: all stages covered -> timeframe question, no visit pitch, area not re-asked",
        _fq == "When were you hoping to get this done?"
        and "assessment" not in _fq and "visit" not in _fq,
        got=str(_fq),
    )
    # Area genuinely open (not asked, not answered) -> ask it.
    _fq2 = _FakeSelfForward(_FakeApptFwd(
        history=_bot("Shower cubicles start from US$170."),
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
        history=_bot("When were you hoping to get this done?"),
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
    def __init__(self, is_delayed=False):
        self.is_delayed = is_delayed
class _FakeSelfFollowup:
    _FAMILY_DISPLAY = ResponseMixin._FAMILY_DISPLAY
    _confirm_intent_question = ResponseMixin._confirm_intent_question
    _get_pricing_followup_prompt = ResponseMixin._get_pricing_followup_prompt
    def __init__(self, stage, is_delayed=False):
        self._stage = stage
        self.appointment = _FakeApptStage(is_delayed=is_delayed)
    def get_next_question_to_ask(self):
        return self._stage
    def _get_contextual_description_question(self):
        return "What specifically needs doing?"
    def _get_next_two_available_days(self):
        return []
try:
    # Scope stage + known items -> confirm-intent names the items.
    _ci = _FakeSelfFollowup("project_description")._get_pricing_followup_prompt(
        "english", items={'shower', 'tub'}
    )
    results.log(
        "pricing close: scope stage with items -> confirm-intent",
        _ci == "Are you looking to do both the shower and tub, or starting with one?",
        got=str(_ci),
    )
    # Deflected lead at the scheduling stage -> timeline anchor, NOT a day push.
    _ta = _FakeSelfFollowup("availability_date", is_delayed=True)._get_pricing_followup_prompt("english")
    results.log(
        "pricing close: deflected lead -> timeline anchor (no day push)",
        _ta == "Are you hoping to get this sorted soon, or still planning it out?",
        got=str(_ta),
    )
    # Engaged lead at the scheduling stage -> still asks the day (no override).
    _day = _FakeSelfFollowup("availability_date", is_delayed=False)._get_pricing_followup_prompt("english")
    results.log(
        "pricing close: engaged lead at scheduling -> day question (no anchor)",
        "planning it out" not in _day,
        got=str(_day),
    )
except Exception as e:
    results.log("pricing close stage/deflection", False, got=str(e))

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

_finish()