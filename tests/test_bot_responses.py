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

# ── Run modes ────────────────────────────────────────────────────────────────
# PLUMBOT_GATE=1        → run ONLY the deterministic TEST 0 regression block and
#                         exit non-zero on any failure. This is the commit gate:
#                         fast, offline, and meaningful (no flaky live-LLM tests).
# PLUMBOT_MOCK_DEEPSEEK=1 → replace the DeepSeek client with a deterministic stub
#                         so the FULL suite runs offline without flaky live calls.
# Gate mode implies the mock so it never touches the network.
GATE_ONLY = os.environ.get('PLUMBOT_GATE') == '1' or '--gate' in sys.argv
OFFLINE = GATE_ONLY or os.environ.get('PLUMBOT_MOCK_DEEPSEEK') == '1'

# An offline run gets its own throwaway database, decided BEFORE django.setup()
# because settings keys TEST MODE off `'test' in sys.argv`. Tenant config —
# prices, FAQ facts, declined areas — lives in the DB since Phase 2, so the
# deterministic block reads it on most assertions. Left pointing at whatever
# DATABASE_URL happens to be set, the gate silently grades against the
# developer's own data and fails wholesale on CI's empty database. TEST MODE
# gives it in-memory SQLite with the schema built from the models, and
# bot/apps.py's post_migrate hook seeds the homebase tenant from the same
# HOMEBASE_* constants the production migrations use.
if OFFLINE and 'test' not in sys.argv:
    sys.argv.append('test')

import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Plumbing_CRM.settings')
django.setup()

if OFFLINE:
    from django.db import connection
    connection.creation.create_test_db(verbosity=0, autoclobber=True, serialize=False)
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
            # The summary at the bottom is what a CI log gets read for — fall
            # back to what we actually got (often an exception string) so a
            # failure isn't just a bare test name.
            self.errors.append(f"{test_name}: {message or got or 'failed'}")
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

# A bare ack while a delay hold is on must keep the hold — including a BATCH of
# acks, which is how they usually arrive once the debounce joins separate taps
# into one turn. Both gates used exact whole-string set membership, so "Alright"
# + "Thank you" (each suppressed alone) combined into a turn matching neither,
# cleared the hold, and re-pitched the site visit to a lead who had already said
# they'd come back to us (prod, +263717781175, 2026-08-29).
from bot.out_of_scope_handler import _is_bare_acknowledgement
_NL = chr(10)
# (message, expected: is this turn nothing but acknowledgement?)
BARE_ACK_CASES = [
    ("Alright" + _NL + "Thank you", True),   # the bug
    ("Alright",                     True),
    ("Thank you",                   True),
    ("ok thanks",                   True),
    ("Ok. Thanks!",                 True),
    ("Sure, thank you",             True),
    ("Noted. Thanks a lot!",        True),
    ("thanks \U0001f44d",           True),
    ("\U0001f44d",                  True),
    ("maita basa",                  True),   # Shona
    ("Zvakanaka" + _NL + "Ndatenda", True),  # batched Shona
    # Must NOT be swallowed — the customer's own words outrank the hold.
    ("ok, how much for a geyser?",  False),
    ("thanks, when can you come?",  False),
    ("Alright" + _NL + "I want to book Monday", False),
    ("Sorry, we'll speak Monday evening",      False),
    ("respnachinodakufa@gmail.com", False),   # email capture, not an ack
    ("Dzivarasekwa extension",      False),   # an area answer
    ("yes please come tomorrow",    False),
    ("",                            False),
    ("   ",                         False),
]
for msg, expected in BARE_ACK_CASES:
    try:
        got = _is_bare_acknowledgement(msg)
        results.log(
            f"_is_bare_acknowledgement: '{msg[:30]}'",
            got == expected,
            f"ack={got}",
            expected=f"ack={expected}",
            got=f"ack={got}",
        )
    except Exception as e:
        results.log(f"_is_bare_acknowledgement: '{msg[:30]}'", False, got=str(e))

# should_hold_silently decides re-pitch vs stay-silent while a hold is on.
# DeepSeek makes the call on ambiguous turns, but it is WIDEN-ONLY: both failure
# directions are expensive here (re-pitching a parked lead drove one away; going
# silent on a lead who came back ready is worse), so per the symmetry rule the
# classifier may not overturn either deterministic verdict.
from bot.out_of_scope_handler import should_hold_silently
# (message, expected: should we stay SILENT and keep the hold?)
HOLD_SILENTLY_CASES = [
    # 1. Deterministic ack — silent, and the classifier is never consulted.
    ("Alright" + _NL + "Thank you", True),   # the production bug
    ("ok thanks",                   True),
    ("maita basa",                  True),
    # 2. A real inquiry always gets a reply — no classifier verdict may silence it.
    ("how much for a geyser?",      False),
    ("This one how much",           False),
    ("I want to book",              False),
    ("can you come tomorrow",       False),
    ("freestanding tub price",      False),
    # 3. The ambiguous middle — DeepSeek widens silence to what no list enumerates.
    ("Cool, that works for now",    True),
    ("Perfect, appreciate the help", True),
    ("No problem, I'll shout when I'm ready", True),
    # ...but still answers anything carrying real content.
    ("actually make it Tuesday",    False),
    ("my geyser is leaking now",    False),
]
for msg, expected in HOLD_SILENTLY_CASES:
    try:
        got = should_hold_silently(msg)
        results.log(
            f"should_hold_silently: '{msg[:30]}'",
            got == expected,
            f"silent={got}",
            expected=f"silent={expected}",
            got=f"silent={got}",
        )
    except Exception as e:
        results.log(f"should_hold_silently: '{msg[:30]}'", False, got=str(e))

# With DeepSeek down the gate must still hold the two deterministic rules — the
# ambiguous middle simply falls back to "reply", which is the pre-AI behaviour.
import bot.out_of_scope_handler as _oos_hold
import bot.services.clients as _clients_hold
_saved_call = _clients_hold.deepseek_call
try:
    def _call_down(*_a, **_kw):
        raise RuntimeError("DeepSeek unavailable")
    _clients_hold.deepseek_call = _call_down
    OFFLINE_HOLD_CASES = [
        ("Alright" + _NL + "Thank you", True),    # rule 1 still holds
        ("how much for a geyser?",      False),   # rule 2 still holds
        ("Cool, that works for now",    False),   # middle degrades to a reply
    ]
    for msg, expected in OFFLINE_HOLD_CASES:
        got = _oos_hold.should_hold_silently(msg)
        results.log(
            f"should_hold_silently (API down): '{msg[:30]}'",
            got == expected,
            f"silent={got}",
            expected=f"silent={expected}",
            got=f"silent={got}",
        )
finally:
    _clients_hold.deepseek_call = _saved_call

# The date resolver answers WHICH DAY; _extract_followup_time answers WHAT TIME,
# so a lead who said "Monday evening" is not checked back on at 9am. Bare numbers
# must never read as times — "the 21st" and "in 2 weeks" are dates.
from bot.out_of_scope_handler import _extract_followup_time
FOLLOWUP_TIME_CASES = [
    # the production wording (+263717781175)
    ("Let me update you end of day on Monday",   (17, 0)),
    ("Sorry, we'll speak Monday evening",        (18, 0)),
    # explicit clocks
    ("call me at 9pm",                           (21, 0)),
    ("9 pm works",                               (21, 0)),
    ("9:30pm",                                   (21, 30)),
    ("try me at 9am",                            (9, 0)),
    ("21:00 is fine",                            (21, 0)),
    ("12am",                                     (6, 0)),   # clamped to civil hours
    ("11pm",                                     (22, 0)),  # clamped
    # dayparts, English and Shona
    ("give me a call in the morning",            (9, 0)),
    ("sometime in the afternoon",                (14, 0)),  # must beat 'noon'
    ("around lunchtime",                         (12, 0)),
    ("manheru",                                  (18, 0)),
    ("mangwanani",                               (9, 0)),
    ("after work",                               (17, 0)),
    ("first thing Tuesday",                      (8, 0)),
    ("tonight",                                  (19, 0)),
    # an explicit clock outranks a daypart word in the same message
    ("Monday evening, say 8pm",                  (20, 0)),
    # no time named -> None, so the caller keeps its default
    ("next week",                                None),
    ("the 21st",                                 None),
    ("in 2 weeks",                               None),
    ("I'll get in touch",                        None),
    ("",                                         None),
]
for msg, expected in FOLLOWUP_TIME_CASES:
    try:
        got = _extract_followup_time(msg)
        results.log(
            f"_extract_followup_time: '{msg[:30]}'",
            got == expected,
            f"time={got}",
            expected=f"time={expected}",
            got=f"time={got}",
        )
    except Exception as e:
        results.log(f"_extract_followup_time: '{msg[:30]}'", False, got=str(e))

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

# A CONFIRMED visit the customer defers must be released — status back to
# pending, the slot given up, the plumber told not to travel — and the exit
# acknowledgement must stop restating it. Production 2026-08-30 (Magunje lead):
# booked Monday 9am, then "our priority was glazing can I give my new date
# later"; the delay flow parked them and set a 14-day check-back but left the
# booking standing, so the next bare "Great" was answered with "Perfect — see
# you on Monday, August 31 at 09:00 AM" and the plumber stayed booked to drive.
import pytz as _pytz_rv
from datetime import datetime as _dt_rv, timedelta as _td_rv
from bot.out_of_scope_handler import release_deferred_visit as _release_rv
_SAST_RV = _pytz_rv.timezone('Africa/Johannesburg')

class _FakeApptBooked:
    """Just enough Appointment for the release contract, reusing the real
    model methods so the state transition under test is the shipped one."""
    PARKED_TAG = Appointment.PARKED_TAG
    RELEASED_TAG = Appointment.RELEASED_TAG
    deferred_slot_field = Appointment.deferred_slot_field
    release_deferred_visit = Appointment.release_deferred_visit

    def __init__(self, slot):
        self.status = 'confirmed'
        self.scheduled_datetime = slot
        self.job_scheduled_datetime = None
        self.appointment_type = 'site_visit'
        self.internal_notes = ''
        self.is_lead_active = False
        self.chatbot_paused = True
        self.google_calendar_event_id = ''
        self.pk = None
    def save(self, update_fields=None):
        pass

try:
    _slot_rv = _SAST_RV.localize(_dt_rv(2026, 8, 31, 9, 0))
    # Check-back AFTER the booked visit → the visit is not what they're waiting
    # for; release it.
    _late = _FakeApptBooked(_slot_rv)
    _released = _release_rv(_late, reason='test',
                            checkin_dt=_slot_rv + _td_rv(days=14))
    results.log(
        "deferred visit: check-back after the slot -> released, back to pending",
        (_released is True and _late.status == 'pending'
         and _late.scheduled_datetime is None
         and _late.RELEASED_TAG in _late.internal_notes
         and _late.is_lead_active is True and _late.chatbot_paused is False),
        got=f"released={_released} status={_late.status} "
            f"slot={_late.scheduled_datetime} notes={_late.internal_notes!r}",
    )
    # Check-back BEFORE the booked visit is just a nudge ahead of it — the
    # booking stands. (Releasing here would cancel a live visit.)
    _early = _FakeApptBooked(_slot_rv)
    _kept = _release_rv(_early, reason='test',
                        checkin_dt=_slot_rv - _td_rv(days=2))
    results.log(
        "deferred visit: check-back before the slot -> booking stands",
        (_kept is False and _early.status == 'confirmed'
         and _early.scheduled_datetime == _slot_rv),
        got=f"released={_kept} status={_early.status} slot={_early.scheduled_datetime}",
    )
    # A brush-off has no check-back date at all — release outright.
    _brush = _FakeApptBooked(_slot_rv)
    results.log(
        "deferred visit: brush-off with no check-back -> released",
        (_release_rv(_brush, reason='brush-off') is True
         and _brush.status == 'pending' and _brush.scheduled_datetime is None),
        got=f"status={_brush.status} slot={_brush.scheduled_datetime}",
    )
    # Nothing booked → nothing to release, and no crash on a lead mid-flow.
    _unbooked = _FakeApptBooked(None)
    _unbooked.status = 'pending'
    results.log(
        "deferred visit: nothing booked -> no-op",
        _release_rv(_unbooked, reason='test') is False,
        got=f"status={_unbooked.status}",
    )
    # A job customer holds two datetimes; the one a deferral releases is the job.
    _job = _FakeApptBooked(_slot_rv)
    _job.appointment_type = 'job_appointment'
    _job.job_scheduled_datetime = _slot_rv + _td_rv(days=3)
    _release_rv(_job, reason='test', checkin_dt=_slot_rv + _td_rv(days=30))
    results.log(
        "deferred visit: a job releases the job slot, not the completed visit",
        (_job.job_scheduled_datetime is None
         and _job.scheduled_datetime == _slot_rv),
        got=f"job={_job.job_scheduled_datetime} visit={_job.scheduled_datetime}",
    )
except Exception as e:
    results.log("deferred visit release", False, got=str(e))

# Our opening hours are only ever an answer about WHEN. The availability retry
# used to send them to any message it could not read as a date: prod answered
# "Currently I need plumbing material" and "Sorry doors were already fitted"
# with "When would work best for you? We're open Monday-Sunday, 8 AM-6 PM."
try:
    from bot.views.plumbot.response_mixin import _is_timing_reply as _itr
    TIMING_CASES = [
        # Genuinely about timing → the hours nudge is a sensible answer.
        ("tomorrow",                      True),
        ("Monday",                        True),
        ("9 am",                          True),
        ("9",                             True),
        ("ok",                            True),   # short unclear day answer
        ("hmm",                           True),
        ("what time are you open",        True),
        ("I'm busy this week",            True),
        ("the 15th",                      True),
        # Not about timing at all → answering with hours ignores what they said.
        ("Currently I need plumbing material",  False),
        ("Sorry doors were already fitted",     False),
        ("Hope you understood it's Magunje not Harare", False),
        ("9 inch sized doors x2",               False),
        ("do you sell taps and fittings",       False),
        ("I am looking for materials",          False),
    ]
    results.log(
        "timing reply: hours answer only for messages about WHEN",
        all(_itr(m) is e for m, e in TIMING_CASES),
        got="; ".join(f"{m[:30]!r}->{_itr(m)}"
                      for m, e in TIMING_CASES if _itr(m) is not e) or "all as expected",
    )
except Exception as e:
    results.log("timing reply: hours answer only for messages about WHEN", False, got=str(e))

# A materials request is acted on whatever stage the flow is parked at, and is
# answered with the route to a written quote — never with a price sheet.
try:
    class _FakeSelfMat:
        _MATERIAL_WORDS = ResponseMixin._MATERIAL_WORDS
        _MATERIAL_NEED_WORDS = ResponseMixin._MATERIAL_NEED_WORDS
        _is_material_supply_request = ResponseMixin._is_material_supply_request
        _already_offered_material_quote = ResponseMixin._already_offered_material_quote
        _build_material_supply_reply = ResponseMixin._build_material_supply_reply
        def __init__(self, history=None):
            class _Appt:
                pass
            self.appointment = _Appt()
            self.appointment.conversation_history = history or []
    _mat = _FakeSelfMat()
    MATERIAL_CASES = [
        ("Currently I need plumbing material", True),
        ("I need materials only",              True),
        ("do you supply materials",            True),
        ("materials",                          True),
        ("ndinoda material",                   True),
        # Not a request for parts.
        ("the materials you used look really good", False),
        ("Bathroom renovation",                False),
        ("tomorrow at 2pm",                    False),
        # Sourcing them themselves — a fact about the job, not an order.
        ("I have my own materials",            False),
        ("I already bought the materials",     False),
    ]
    results.log(
        "materials request: a parts ask is recognised, a passing mention is not",
        all(_mat._is_material_supply_request(m) is e for m, e in MATERIAL_CASES),
        got="; ".join(f"{m[:30]!r}->{_mat._is_material_supply_request(m)}"
                      for m, e in MATERIAL_CASES
                      if _mat._is_material_supply_request(m) is not e) or "all as expected",
    )
    _mat_reply = _mat._build_material_supply_reply("english")
    results.log(
        "materials reply: confirms supply, asks for the list, quotes no figures",
        ("supply the materials" in _mat_reply
         and "list of what you need" in _mat_reply
         and "US$" not in _mat_reply),
        got=repr(_mat_reply),
    )
    # Asked again after we already offered → a shorter re-ask, not the same block.
    _mat_again = _FakeSelfMat(history=[
        {"role": "assistant", "content": _mat_reply},
    ])._build_material_supply_reply("english")
    results.log(
        "materials reply: a repeat is a shorter re-ask, not the same message again",
        _mat_again != _mat_reply and "list of items you need" in _mat_again,
        got=repr(_mat_again),
    )
except Exception as e:
    results.log("materials request handling", False, got=str(e))

# A question asked alongside a photo request must be answered — the photo step
# returns outright, so it rides in on the intro line. Prod: "What's a mixer can
# I have a pic" got fifteen photos and no answer.
try:
    from bot.whatsapp_webhook import _definition_answer, _description_is_a_plan
    results.log(
        "definition: 'what's a mixer' is answered in plain English",
        (_definition_answer("What's a mixer can I have a pic") or '').startswith(
            "A mixer is the tap"),
        got=repr(_definition_answer("What's a mixer can I have a pic")),
    )
    DEFINITION_CASES = [
        ("what is a pedestal",          True),
        ("whats a cistern",             True),
        ("What's a mixer",              True),
        # Not a definition question — don't hijack a normal request.
        ("send me a pic of your mixers", False),
        ("how much is a vanity",         False),
        ("what area do you cover",       False),
    ]
    results.log(
        "definition: only actual 'what is X' questions get a glossary answer",
        all((_definition_answer(m) is not None) is e for m, e in DEFINITION_CASES),
        got="; ".join(f"{m[:28]!r}->{_definition_answer(m) is not None}"
                      for m, e in DEFINITION_CASES
                      if (_definition_answer(m) is not None) is not e) or "all as expected",
    )
    # The glossary is generic trade vocabulary — no tenant's prices or names.
    for _terms, _answer in __import__('bot.whatsapp_webhook', fromlist=['x'])._FIXTURE_GLOSSARY:
        assert 'US$' not in _answer and 'Homebase' not in _answer
    results.log("definition: glossary carries no prices and no business name", True)
    # A drawing is a plan whatever its MIME type.
    PLAN_SIGHT_CASES = [
        ("This is a floor plan drawing, not a photo. It shows a kitchen with a sink",
         True),
        ("A blueprint of a two bedroom house", True),
        ("An architectural drawing showing the bathroom layout", True),
        # A photo of the real thing is not a plan.
        ("A freestanding bathtub in a tiled bathroom", False),
        ("A leaking pipe under a kitchen sink", False),
        ("A hand drawing of a tap", False),
    ]
    results.log(
        "plan by sight: a drawing is filed as the plan, a photo is not",
        all(_description_is_a_plan(d) is e for d, e in PLAN_SIGHT_CASES),
        got="; ".join(f"{d[:30]!r}->{_description_is_a_plan(d)}"
                      for d, e in PLAN_SIGHT_CASES
                      if _description_is_a_plan(d) is not e) or "all as expected",
    )
    # ...and the ack says what happens to it, instead of filing it for the visit.
    from bot.whatsapp_webhook import _compose_media_ack
    _plan_ack = _compose_media_ack('complete', 'confirmed', 'image',
                                   is_plan_document=True)
    _photo_ack = _compose_media_ack('complete', 'confirmed', 'image',
                                    is_plan_document=False)
    results.log(
        "plan ack: a plan is quoted, an ordinary photo is still kept for the visit",
        ("written quotation" in _plan_ack
         and "when we come round" in _photo_ack
         and "written quotation" not in _photo_ack),
        got=f"plan={_plan_ack!r} photo={_photo_ack!r}",
    )
except Exception as e:
    results.log("definition / plan-by-sight handling", False, got=str(e))

# Doors, windows and glazing are joinery. Without them on the out-of-scope list
# the bot answered "9 inch sized doors x2" with "We can definitely sort out two
# 9-inch doors for your bathroom project".
try:
    from bot.out_of_scope_handler import OOS_SERVICE_TERMS, _OOS_KEYWORDS
    results.log(
        "out of scope: doors/windows/glazing are on the list the classifier sees",
        any('door' in t for t in OOS_SERVICE_TERMS) and 'glazing' in OOS_SERVICE_TERMS,
        got=str(OOS_SERVICE_TERMS),
    )
    # ...but never so broadly that a shower door reads as joinery.
    results.log(
        "out of scope: 'shower door' and 'window sill' are still ours",
        not any(k in 'i need a new shower door for the cubicle' for k in _OOS_KEYWORDS)
        and not any(k in 'the window sill in the bathroom' for k in _OOS_KEYWORDS),
        got=str([k for k in _OOS_KEYWORDS
                 if k in 'i need a new shower door for the cubicle'
                 or k in 'the window sill in the bathroom']),
    )
    # ...and adding them must not turn a DEFERRAL that happens to name another
    # trade into an out-of-scope reply. "Our priority was glazing, can I give my
    # new date later" is a deferral; the keyword fallback used to call it
    # out-of-scope the moment 'glazing' joined the list.
    from bot.out_of_scope_handler import _keyword_classify, _is_explicit_deferral
    _defer_msg = ("It's ok but currently our priority was glazing "
                  "can I give my new date later")
    results.log(
        "out of scope: a deferral naming another trade is still a deferral",
        (_is_explicit_deferral(_defer_msg) is True
         and _keyword_classify(_defer_msg)['category'] == 'delay_signal'),
        got=str(_keyword_classify(_defer_msg)),
    )
    DEFER_DATE_CASES = [
        ("can I give my new date later",        True),
        ("I'll give you a new date next week",  True),
        # Ordinary date talk is not a deferral.
        ("is the date later this week?",        False),
        ("tomorrow works for me",               False),
        ("can you come at a later date this week", False),
    ]
    results.log(
        "deferral: postponing the date is caught, ordinary date talk is not",
        all(_is_explicit_deferral(m) is e for m, e in DEFER_DATE_CASES),
        got="; ".join(f"{m[:30]!r}->{_is_explicit_deferral(m)}"
                      for m, e in DEFER_DATE_CASES
                      if _is_explicit_deferral(m) is not e) or "all as expected",
    )
except Exception as e:
    results.log("out of scope: joinery terms", False, got=str(e))

# The out-of-scope clarification always asks about THEIR subject, never "this".
# Prod: "Cost of wiring a new 4 bedroom house" was answered with "Just to
# confirm — is there any plumbing or water-related work involved in this?",
# leaving the customer to work out what "this" meant and whether we had read the
# word 'wiring' at all.
try:
    from bot.out_of_scope_handler import (
        _generate_plumbing_reframe_question as _reframe, _oos_subject,
    )
    _wiring = _reframe("Cost of wiring a new 4 bedroom house")
    results.log(
        "clarifier: names the customer's own subject back",
        (_wiring == "Just to clarify, is the wiring you're asking about "
                    "related to plumbing or water systems in the house?"),
        got=repr(_wiring),
    )
    # Their word, not our label: a lead who wrote 'wiring' is asked about the
    # wiring, not "the electrical work".
    results.log(
        "clarifier: echoes their vocabulary, not ours",
        "electrical" not in _wiring.lower(),
        got=repr(_wiring),
    )
    SUBJECT_CASES = [
        ("Cost of wiring a new 4 bedroom house", 'wiring'),
        ("Tiling",                               'tiling'),
        ("do you do roofing",                    'roofing'),
        ("I need painting done",                 'painting'),
        ("can you build a garage",               'garage'),
        ("solar panels installation",            'solar panels'),
        ("pest control please",                  'pest control'),
        ("I want electrical work done",          'electrical work'),
        # Nothing nameable → no subject, and the abstract question stands.
        ("can you help with the thing at my place", None),
    ]
    results.log(
        "clarifier: the subject is pulled from the customer's own words",
        all(_oos_subject(m) == s for m, s in SUBJECT_CASES),
        got="; ".join(f"{m[:28]!r}->{_oos_subject(m)!r}"
                      for m, s in SUBJECT_CASES if _oos_subject(m) != s)
            or "all as expected",
    )
    # Longest match wins, so a compound subject is never truncated mid-phrase.
    results.log(
        "clarifier: 'electrical work' beats 'electrical', 'solar panels' beats 'solar'",
        (_oos_subject("I want electrical work done") == 'electrical work'
         and _oos_subject("solar panels installation") == 'solar panels'),
        got=f"{_oos_subject('I want electrical work done')!r} / "
            f"{_oos_subject('solar panels installation')!r}",
    )
    # A plural subject reads "are the ...", never "is the doors".
    PLURAL_CASES = [
        ("how much for burglar bars", 'are the burglar bars'),
        ("do you fit doors",          'are the doors'),
        ("tiles for my bathroom floor", 'are the tiles'),
        ("Cost of wiring a house",    'is the wiring'),
        ("pest control please",       'is the pest control'),
    ]
    results.log(
        "clarifier: singular/plural agreement holds for every subject",
        all(frag in _reframe(m) for m, frag in PLURAL_CASES),
        got="; ".join(f"{m[:26]!r}->{_reframe(m)[:46]!r}"
                      for m, frag in PLURAL_CASES if frag not in _reframe(m))
            or "all as expected",
    )
    # No emojis, one question, no price — the copy rules hold for every branch.
    results.log(
        "clarifier: one question, no price, no emoji",
        all(_reframe(m).count('?') == 1 and 'US$' not in _reframe(m)
            for m, _s in SUBJECT_CASES),
        got=repr(_reframe("Tiling")),
    )
    # The abstract question survives only for a message naming nothing.
    results.log(
        "clarifier: the unnameable case keeps the original question",
        _reframe("can you help with the thing at my place")
        == "Just to confirm — is there any plumbing or water-related work involved in this?",
        got=repr(_reframe("can you help with the thing at my place")),
    )
    # ── The other two paths that can ask a clarifying question ───────────────
    # (1) DeepSeek down / generation failed → the offline fallback. Being
    # offline is no excuse for asking about "this": the subject resolver is
    # deterministic and always available.
    from bot.out_of_scope_handler import _fallback_clarifier, _subject_echo_rule
    results.log(
        "clarifier: the offline fallback is contextual too",
        ("the wiring you're asking about"
         in _fallback_clarifier('out_of_scope', 'Cost of wiring a new 4 bedroom house')),
        got=repr(_fallback_clarifier('out_of_scope',
                                     'Cost of wiring a new 4 bedroom house')),
    )
    results.log(
        "clarifier: an unknown category still gets a contextual fallback",
        "the roofing you're asking about" in _fallback_clarifier('mystery',
                                                                 'do you do roofing'),
        got=repr(_fallback_clarifier('mystery', 'do you do roofing')),
    )
    results.log(
        "clarifier: the delay/complaint fallbacks are unchanged",
        _fallback_clarifier('delay_signal', 'maybe next month').startswith(
            "No problem at all!"),
        got=repr(_fallback_clarifier('delay_signal', 'maybe next month')[:50]),
    )
    # (2) LOW confidence → DeepSeek writes it. The model is handed the exact
    # word to echo rather than left to pick one, so both paths name the same
    # subject.
    _echo = _subject_echo_rule("Cost of wiring a new 4 bedroom house")
    results.log(
        "clarifier: the LLM prompt is told the exact subject to say back",
        ('"wiring"' in _echo and 'MUST contain the word' in _echo
         and 'Never replace it with' in _echo),
        got=repr(_echo),
    )
    results.log(
        "clarifier: no subject means no instruction to invent one",
        _subject_echo_rule("can you help with the thing at my place") == "",
        got=repr(_subject_echo_rule("can you help with the thing at my place")),
    )
    # The prompt's own examples must not teach the pattern we just banned.
    import inspect as _inspect_cl
    from bot.out_of_scope_handler import _generate_clarifying_question as _gcq
    _src = _inspect_cl.getsource(_gcq)
    results.log(
        "clarifier: the prompt's GOOD examples name a subject, and 'this' is a BAD one",
        ("is the wiring you're asking about" in _src
         and 'BAD EXAMPLES' in _src
         and _src.index('BAD EXAMPLES')
             < _src.index('is there any plumbing work involved in this?')),
        got="prompt examples not in the expected shape",
    )
except Exception as e:
    results.log("clarifier: contextual out-of-scope question", False, got=str(e))

# The exit acknowledgement reads that state: a parked lead is never told the
# visit is still on, however the slot came to be left on the row.
try:
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM_rv
    class _FakeSelfAck:
        _get_delay_acknowledgment = _RM_rv._get_delay_acknowledgment
        def __init__(self, appt):
            self.appointment = appt
        def _customer_said_they_will_reach_out(self):
            return False
    _live = _FakeApptBooked(_SAST_RV.localize(_dt_rv(2026, 8, 31, 9, 0)))
    _ack_live = _FakeSelfAck(_live)._get_delay_acknowledgment()
    _parked_appt = _FakeApptBooked(_SAST_RV.localize(_dt_rv(2026, 8, 31, 9, 0)))
    _parked_appt.internal_notes = Appointment.PARKED_TAG
    _ack_parked = _FakeSelfAck(_parked_appt)._get_delay_acknowledgment()
    results.log(
        "delay ack: live booking restated, parked booking never is",
        ("see you on" in _ack_live and "see you on" not in _ack_parked),
        got=f"live={_ack_live[:40]!r} parked={_ack_parked[:40]!r}",
    )
except Exception as e:
    results.log("delay ack: parked booking never restated", False, got=str(e))

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
    # AI-primary: _is_job_quote_request now consults the classifier first and
    # only then this keyword resolver. These fakes pass no classification, so
    # every case below exercises the FALLBACK — which is exactly what the
    # offline gate should be pinning.
    _job_quote_request_fallback = ResponseMixin._job_quote_request_fallback
    # First contact keeps its scripted greeting: a vague opener is never a
    # quote request, whatever the classifier says.
    _conversation_underway = ResponseMixin._conversation_underway
    _is_greeting_or_opener = staticmethod(ResponseMixin._is_greeting_or_opener)
    _asks_for_quote = ResponseMixin._asks_for_quote
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

# A bare affirmation ANSWERS our tie-down; it never asks a new question. The
# unified classifier keeps product_intent alive across turns, and that stale intent
# routed a bare "Yes" into the services-availability answer — the lead agreed the
# tub price and got "Yes, we handle tub and all related plumbing work" back
# (prod 2026-07-29, lead 670). This resolver is the gate on that AI route, so it
# must fire on a pure yes and stay off anything carrying real content.
BARE_AFFIRM_CASES = [
    ("Yes",              True),    # the bug
    ("yes",              True),
    ("Yes.",             True),
    ("yeah",             True),
    ("sure",             True),
    ("that works",       True),
    ("hongu",            True),
    ("ok",               True),    # bare ack counts — it asks nothing either
    ("noted",            True),
    ("yes I need a tub", False),   # names a product = real content, route it
    ("yes how much",     False),   # a price ask rides along
    ("do you have tubs", False),   # a genuine availability question
    ("tubs",             False),
    ("no",               False),   # a decline is not an affirmation
    ("",                 False),
]
for msg, expected in BARE_AFFIRM_CASES:
    try:
        got = ResponseMixin._is_bare_affirmation(msg)
        results.log(f"_is_bare_affirmation: '{msg[:28]}'", got == expected,
                    expected=str(expected), got=str(got))
    except Exception as e:
        results.log(f"_is_bare_affirmation: '{msg[:28]}'", False, got=str(e))

# The AI service-question route (whatsapp_webhook `_ai_service_q`) must never fire
# on that bare affirmation, no matter what product_intent the classifier carried
# over — but must still catch the typo'd availability question it exists for.
class _FakeSelfAiRoute:
    # _asks_price_figure takes self; the other two are staticmethods, called
    # straight off ResponseMixin below.
    _asks_price_figure = ResponseMixin._asks_price_figure
_far = _FakeSelfAiRoute()

def _ai_service_route_fires(msg, product_intent, next_question='project_description'):
    """Mirrors the `_ai_service_q` gate's message-level conditions."""
    _PRODUCT_LABEL_KEYS = ('shower_cubicle', 'geyser', 'vanity', 'toilet',
                           'chamber', 'tub_sales', 'standalone_tub',
                           'bathtub_installation')
    return (
        product_intent in _PRODUCT_LABEL_KEYS
        and not _far._asks_price_figure(msg)
        and not ResponseMixin._is_size_spec_question(msg)
        and not ResponseMixin._is_bare_affirmation(msg)
        and next_question in ('service_type', 'project_description')
    )
AI_SERVICE_ROUTE_CASES = [
    ("Yes",                  'tub_sales',      False),   # the bug
    ("ok",                   'shower_cubicle', False),
    ("Do you for shower rooms", 'shower_cubicle', True),  # why the route exists
    ("do you have geysers",  'geyser',         True),
    ("how much for a tub",   'tub_sales',      False),   # price ask, not availability
]
for msg, intent, expected in AI_SERVICE_ROUTE_CASES:
    try:
        got = _ai_service_route_fires(msg, intent)
        results.log(f"_ai_service_q gate: '{msg[:28]}' [{intent}]", got == expected,
                    f"fires={got}", expected=f"fires={expected}", got=f"fires={got}")
    except Exception as e:
        results.log(f"_ai_service_q gate: '{msg[:28]}' [{intent}]", False, got=str(e))

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
    # AI-primary: _is_job_quote_request now consults the classifier first and
    # only then this keyword resolver. These fakes pass no classification, so
    # every case below exercises the FALLBACK — which is exactly what the
    # offline gate should be pinning.
    _job_quote_request_fallback = ResponseMixin._job_quote_request_fallback
    # First contact keeps its scripted greeting: a vague opener is never a
    # quote request, whatever the classifier says.
    _conversation_underway = ResponseMixin._conversation_underway
    _is_greeting_or_opener = staticmethod(ResponseMixin._is_greeting_or_opener)
    _asks_for_quote = ResponseMixin._asks_for_quote
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
    # _build_combined_price_reply asks what the customer's photo showed before
    # deciding freestanding-vs-built-in tub money. No photo in this fake.
    _recent_image_description = ResponseMixin._recent_image_description
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
        "US$160 in total" in _cr and "US$170 in total" in _cr,
        got=_cr[:120],
    )
    results.log(
        "_build_combined_price_reply: plain-English disclaimer, not visit-gated",
        "starting prices" in _cr.lower() and "sees the space" in _cr
        and "ballpark" not in _cr.lower(),
        got=_cr[-90:],
    )
    # Owner rule (2026-08-23): supply and install are ALWAYS shown separately,
    # not only when the customer asks about labour. A single item already split
    # them, so a two-item answer giving one combined figure was the odd one out —
    # and its label said "(supply + install)" while showing neither.
    results.log(
        "_build_combined_price_reply: supply and install are always split out",
        "supply from US$" in _cr and "install from US$" in _cr,
        got=_cr[:160],
    )
    results.log(
        "_build_combined_price_reply: 'labour' is never shown to the customer",
        "labour" not in _cr.lower(), got=_cr[:160],
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
        "US$160 in total" in _bi and "US$70" in _bi and "US$670" not in _bi,
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
        ("supply from US$130, install from US$40" in _lab
         and "tub" not in _lab.lower() and "US$160" not in _lab and "US$80" not in _lab),
        got=_lab,
    )
    results.log(
        "labour scope: quantity multiplied with a line total",
        "US$170 in total each" in _lab and "For two that's about US$340 in total" in _lab,
        got=_lab,
    )
    results.log(
        "labour scope: accessories noted, ballpark, not gated behind visit",
        ("accessories on top" in _lab and "starting price" in _lab.lower()
         and "sees the space" in _lab),
        got=_lab,
    )
except Exception as e:
    results.log("_build_combined_price_reply labour scope", False, got=str(e))

# A None content in conversation_history must never crash a history reader.
# Production 2026-07-29 (+27610318200): a handler failed to build a reply, the
# None was stored as an assistant turn, and the next inbound message died on
# `.get('content', '').lower()` — the default only covers a MISSING key, not a
# None value — so _generate_and_schedule_reply raised and the lead got no reply
# at all, permanently. Readers coalesce with `or ''`; the writer refuses empties.
class _FakeApptPoisoned:
    project_description = "tub"
    project_type = "bathroom_renovation"
    conversation_history = [
        {'role': 'user', 'content': 'Where are you located'},
        {'role': 'assistant'},                        # key missing entirely
        # The poison must be the LAST assistant turn: the readers below take the
        # most recent one, so any earlier ordering lets them dodge the None.
        {'role': 'assistant', 'content': None},
        {'role': 'user', 'content': 'Do you have shower cubicles'},
    ]
    def save(self, update_fields=None):
        pass
class _FakeSelfHistory:
    _PRICE_TIEDOWN = ResponseMixin._PRICE_TIEDOWN
    _BUDGET_FIT_CLOSE = ResponseMixin._BUDGET_FIT_CLOSE
    _lang_key = ResponseMixin._lang_key
    _price_tiedown_signatures = ResponseMixin._price_tiedown_signatures
    _TIEDOWN_VALUE_CHECK = ResponseMixin._TIEDOWN_VALUE_CHECK
    _TIEDOWN_OPENER = ResponseMixin._TIEDOWN_OPENER
    _EXTRA_TIEDOWN_SIGNATURES = ResponseMixin._EXTRA_TIEDOWN_SIGNATURES
    _tiedown_signatures = ResponseMixin._tiedown_signatures
    _assistant_history_text = ResponseMixin._assistant_history_text
    _last_assistant_was_tiedown = ResponseMixin._last_assistant_was_tiedown
    _last_assistant_was_value_check = ResponseMixin._last_assistant_was_value_check
    _last_assistant_was_price_tiedown = ResponseMixin._last_assistant_was_price_tiedown
    def __init__(self, appt):
        self.appointment = appt
try:
    _fh = _FakeSelfHistory(_FakeApptPoisoned())
    _readers = {
        '_last_assistant_was_value_check': _fh._last_assistant_was_value_check,
        '_last_assistant_was_tiedown': _fh._last_assistant_was_tiedown,
        '_last_assistant_was_price_tiedown': _fh._last_assistant_was_price_tiedown,
        '_assistant_history_text': _fh._assistant_history_text,
    }
    for _name, _fn in _readers.items():
        try:
            _fn()
            results.log(f"history reader survives a None content: {_name}", True)
        except Exception as e:
            results.log(f"history reader survives a None content: {_name}", False,
                        got=f"{type(e).__name__}: {e}")
except Exception as e:
    results.log("history readers vs None content", False, got=str(e))

# The writer side: an empty/None turn is refused, so the poison never lands.
class _FakeApptWrite:
    def __init__(self):
        self.conversation_history = []
        self.saves = 0
    def save(self, update_fields=None):
        self.saves += 1
try:
    from bot.models import Appointment as _Appt
    _aw = _FakeApptWrite()
    for _bad in (None, '', '   '):
        _Appt.add_conversation_message(_aw, 'assistant', _bad)
    results.log(
        "add_conversation_message: refuses None/empty content",
        _aw.conversation_history == [],
        got=str(_aw.conversation_history),
    )
    _Appt.add_conversation_message(_aw, 'assistant', 'Real reply')
    results.log(
        "add_conversation_message: still stores a real message",
        len(_aw.conversation_history) == 1
        and _aw.conversation_history[0]['content'] == 'Real reply',
        got=str(_aw.conversation_history),
    )
except Exception as e:
    results.log("add_conversation_message empty guard", False, got=str(e))

# The "full bathroom or just that item?" scope question is retired (owner call,
# 2026-07-29). It made the lead settle scope that changes nothing — the free visit
# prices whatever is there — and in production it spawned "What's the difference?"
# and a tangent instead of a booking date. Four surfaces could emit it; none may.
_RETIRED_SCOPE_PHRASES = (
    'full bathroom or just', "the setup you're working", 'full bathroom setup',
    'full bathroom renovation or', 'or are you just looking at pricing for',
    'kana full bathroom',
)
def _has_retired_scope_question(text):
    low = (text or '').lower()
    return [p for p in _RETIRED_SCOPE_PHRASES if p in low]
try:
    from bot.semantic_rescue import _keyword_rescue as _kr
    _kr_out = _kr("I want a freestanding tub")
    results.log(
        "retired scope Q: semantic_rescue product_mention acknowledges only",
        (_kr_out is not None
         and not _has_retired_scope_question(_kr_out.get('suggested_reply'))
         and '?' not in (_kr_out.get('suggested_reply') or '')),
        got=str(_kr_out and _kr_out.get('suggested_reply')),
    )
except Exception as e:
    results.log("retired scope Q: semantic_rescue product_mention", False, got=str(e))
try:
    import bot.semantic_rescue as _sr_mod
    import inspect as _inspect
    _prompt_src = _inspect.getsource(_sr_mod._deepseek_rescue)
    results.log(
        "retired scope Q: rescue prompt no longer instructs it",
        'ask if full renovation or just that item' not in _prompt_src.lower(),
        got="prompt still instructs the scope question"
            if 'ask if full renovation or just that item' in _prompt_src.lower() else "clean",
    )
except Exception as e:
    results.log("retired scope Q: rescue prompt", False, got=str(e))
try:
    _bank_texts = " ".join(
        t for bank in ResponseMixin._FORWARD_BANK.values() for t, _ in bank
    )
    results.log(
        "retired scope Q: not in the forward-question bank",
        not _has_retired_scope_question(_bank_texts),
        got=str(_has_retired_scope_question(_bank_texts)),
    )
except Exception as e:
    results.log("retired scope Q: forward bank", False, got=str(e))
class _FakeSelfAffirmProgress:
    _affirm_and_progress = ResponseMixin._affirm_and_progress
    def _next_forward_question(self, language="english", scope=None, has_accessories=False):
        return "Whereabouts are you based?"
try:
    _fap = _FakeSelfAffirmProgress()
    _en = _fap._affirm_and_progress('shower_cubicle', 'english')
    _sn = _fap._affirm_and_progress('shower_cubicle', 'shona')
    results.log(
        "retired scope Q: availability affirm progresses instead (EN + Shona)",
        (not _has_retired_scope_question(_en) and not _has_retired_scope_question(_sn)
         and 'Whereabouts are you based?' in _en and 'Whereabouts are you based?' in _sn),
        got=f"EN={_en!r} SN={_sn!r}",
    )
except Exception as e:
    results.log("retired scope Q: _affirm_and_progress", False, got=str(e))

# An out-of-area lead gets a polite decline, not a crash. `{_city(self)}` called a
# str as a function, so the whole reply raised inside generate_response's outer
# except and the lead got "Sorry, dropped that on our end" instead — every
# decline-list town, every time (prod 2026-07-29, Bulawayo).
class _FakeApptExcluded:
    internal_notes = '[EXCLUDED_AREA:Bulawayo]'
    def __init__(self):
        self.logged = []
    def add_conversation_message(self, role, content, **kw):
        self.logged.append((role, content))
try:
    import re as _re_x
    _appt_x = _FakeApptExcluded()
    _m_x = _re_x.search(r'\[EXCLUDED_AREA:([^\]]+)\]', _appt_x.internal_notes or '')
    _city_x = _m_x.group(1) if _m_x else 'that area'
    # Mirrors the reply built in generate_response's EXCLUDED AREA branch.
    _excl_reply = (
        f"Ah, sorry — {_city_x} is a bit far for our team to travel to, "
        f"so we can't take this one on properly.\n\n"
        f"If you've got a project nearer our side in future, we'd be "
        f"glad to help."
    )
    import inspect as _inspect_x
    # Comments are part of getsource, and the fix's own comment quotes the old
    # broken expression — scan executable lines only.
    _gr_code = "\n".join(
        line.split('#', 1)[0]
        for line in _inspect_x.getsource(ResponseMixin.generate_response).splitlines()
    )
    results.log(
        "excluded area: decline no longer calls the city string as a function",
        '_city(' not in _gr_code,
        got="_city(...) called in code" if '_city(' in _gr_code else "clean",
    )
    results.log(
        "excluded area: decline names the town and offers the door back",
        ('Bulawayo' in _excl_reply and 'too far' not in _excl_reply.lower()
         and 'nearer our side' in _excl_reply
         and 'dropped that on our end' not in _excl_reply),
        got=_excl_reply,
    )
except Exception as e:
    results.log("excluded area decline", False, got=str(e))

# A business fact must never come back as a bare yes/no. "Is the quote free" was
# answered "No", then "yes" two minutes later (prod 2026-07-29) — the AI rephrase
# ran at temperature 0.4 with no substance check. Degenerate answers now fall back
# to the canned fact, which is complete and identical every time.
FAQ_SUBSTANCE_CASES = [
    ("No",                                              False),   # the bug
    ("yes",                                             False),   # the contradiction
    ("Yes.",                                            False),
    ("nope",                                            False),
    ("kwete",                                           False),
    ("ok",                                              False),
    ("",                                                False),
    ("Yes, the quote is completely free.",              True),
    ("No charge at all — the assessment is on us.",      True),
    ("Hongu, quote yedu ndeye mahara zvachose.",         True),
]
for _ans, _expected in FAQ_SUBSTANCE_CASES:
    try:
        _got = ResponseMixin._is_substantive_faq_answer(_ans)
        results.log(f"_is_substantive_faq_answer: {_ans[:28]!r}", _got == _expected,
                    expected=str(_expected), got=str(_got))
    except Exception as e:
        results.log(f"_is_substantive_faq_answer: {_ans[:28]!r}", False, got=str(e))
try:
    _faq_src = _inspect_x.getsource(ResponseMixin.ai_answer_faq)
    results.log(
        "ai_answer_faq: facts answered deterministically (temperature 0)",
        'temperature=0,' in _faq_src and 'temperature=0.4' not in _faq_src,
        got="still non-deterministic" if 'temperature=0.4' in _faq_src else "temperature=0",
    )
except Exception as e:
    results.log("ai_answer_faq temperature", False, got=str(e))

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
    _PRICE_TIEDOWN = ResponseMixin._PRICE_TIEDOWN
    _BUDGET_FIT_CLOSE = ResponseMixin._BUDGET_FIT_CLOSE
    _lang_key = ResponseMixin._lang_key
    _price_tiedown_signatures = ResponseMixin._price_tiedown_signatures
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
        _fq in [t for t, _sig in ResponseMixin._FORWARD_BANK['booking']]
        and "assessment" not in _fq and "visit" not in _fq,
        got=str(_fq),
    )
    # Phase 2 (assumptive close everywhere): the FIRST ask of the booking stage
    # is the closed this-or-that; the open "when were you hoping" survives as the
    # retry variant, and its fragment stays in the asked-detection list so a lead
    # who already heard either wording is never asked again.
    results.log(
        "forward Q: the booking stage opens with a closed this-or-that",
        _fq == "Are you looking to start soon, or still planning it out?",
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
    _PRICE_TIEDOWN = ResponseMixin._PRICE_TIEDOWN
    _BUDGET_FIT_CLOSE = ResponseMixin._BUDGET_FIT_CLOSE
    _lang_key = ResponseMixin._lang_key
    _price_tiedown_signatures = ResponseMixin._price_tiedown_signatures
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
        _quote_route_followup = ResponseMixin._quote_route_followup
        _has_plan_on_file = ResponseMixin._has_plan_on_file
        def __init__(self, nq, history=None, area=None, plan=False):
            self._nq = nq
            class _Appt:
                pass
            self.appointment = _Appt()
            self.appointment.conversation_history = history or []
            self.appointment.customer_area = area
            self.appointment.plan_file = 'plans/house.jpg' if plan else ''
            self.appointment.plan_status = 'plan_uploaded' if plan else ''
        def get_next_question_to_ask(self):
            return self._nq
        def _capture_named_products_as_description(self, message):
            pass
        def _set_question_retry_count(self, question, count):
            # _quote_route_followup records the ask now, so the fake must expose
            # this — without it the scripted question repeats verbatim next turn.
            self.asked = getattr(self, 'asked', {})
            self.asked[question] = count
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
    # The follow-up must never re-ask a field we already hold. The stages with no
    # scripted question ('name', 'complete') used to fall back on the AREA
    # question unconditionally — prod asked "What area are you in?" of a lead who
    # had answered "Magunje" four times, right after "Sorry I think I asked for a
    # quotation first".
    _jq_named = _FakeSelfJQ("name", area="Magunje", history=[
        {"role": "assistant",
         "content": "We'll get you an exact, all-in figure free on a quick on-site visit."},
    ])._build_job_quote_reply("english", "Sorry I think I asked for a quotation first")
    results.log(
        "job quote reply: area already on file is never re-asked",
        "what area are you in" not in _jq_named.lower()
        and "name should we put on the booking" in _jq_named.lower(),
        got=repr(_jq_named),
    )
    _jq_done = _FakeSelfJQ("complete", area="Magunje", history=[
        {"role": "assistant",
         "content": "We'll get you an exact, all-in figure free on a quick on-site visit."},
    ])._build_job_quote_reply("english", "quotation please")
    results.log(
        "job quote reply: nothing left to collect -> the visit, not the area again",
        "what area are you in" not in _jq_done.lower(),
        got=repr(_jq_done),
    )
    # Area genuinely missing at a scriptless stage → still ask for it.
    _jq_noarea = _FakeSelfJQ("complete", area=None, history=[
        {"role": "assistant",
         "content": "We'll get you an exact, all-in figure free on a quick on-site visit."},
    ])._build_job_quote_reply("english", "quotation please")
    results.log(
        "job quote reply: area still missing -> the area question is asked",
        "what area are you in" in _jq_noarea.lower(),
        got=repr(_jq_noarea),
    )
    # A lead who has SENT their plan is quoted off it, not pitched the visit
    # again ("Sorry I think I asked for a quotation first", plan sent an hour
    # earlier).
    _jq_plan = _FakeSelfJQ("availability_date", area="Magunje", plan=True
                           )._build_job_quote_reply("english", "quote those please")
    results.log(
        "job quote reply: a plan on file is quoted, not answered with the visit pitch",
        ("written quotation" in _jq_plan.lower()
         and "all-in figure free on a quick on-site visit" not in _jq_plan),
        got=repr(_jq_plan),
    )
    _jq_noplan = _FakeSelfJQ("availability_date", area="Magunje", plan=False
                             )._build_job_quote_reply("english", "quote those please")
    results.log(
        "job quote reply: no plan on file -> the free-visit route is unchanged",
        "all-in figure free on a quick on-site visit" in _jq_noplan,
        got=repr(_jq_noplan),
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
    # Price Conditional Rule: "Do you have ceramic tubs" is a SERVICE_INQUIRY,
    # not a PRICE_QUERY, so the continuation reply must carry NO figure. The
    # scoping question is what earns the next turn.
    _svc = _sip._service_continuation_reply("shower cubicle", "english")
    results.log(
        "service continuation: a service inquiry is never answered with a price",
        "US$" not in _svc and "$" not in _svc
        and "Yes, we handle shower cubicle" in _svc
        and _svc.rstrip().endswith("?"),
        got=repr(_svc),
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
        # The offer now composes from the tenant's package row (per-tenant
        # Facebook offer); the fake reads homebase's real row.
        @property
        def tenant_cfg(self):
            from bot.models import Tenant
            from bot.tenant_config import get_config
            return get_config(Tenant.objects.filter(slug='homebase').first())
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
    # Vague "how much" overview: the tenant's own FB offer is the anchor and
    # is SUFFICIENT on its own — tub lines only render when the tenant has
    # tub prices; no offer at all -> None (router deflects to the free visit).
    class _FakeCfgItem:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    class _FakeCfgFBOnly:
        def price_item(self, family, variant=''):
            if (family, variant) == ('package', 'facebook'):
                return _FakeCfgItem(flat=350, label='winter special',
                                    parts=[{'name': 'geyser'}, {'name': 'thermostat'}])
            return None
    class _FakeSelfOverview:
        _compose_pricing_overview = ResponseMixin._compose_pricing_overview
        tenant_cfg = _FakeCfgFBOnly()
        def _freestanding_tub_price(self):
            return None
        def _price_components_map(self):
            return {}
        def _product_price_close(self, lang):
            return 'CLOSE'
        def _ensure_price_disclaimer(self, intent, reply):
            return reply
    _ovr = _FakeSelfOverview()._compose_pricing_overview('english')
    results.log(
        "pricing overview: FB offer alone anchors the vague 'how much' reply",
        _ovr is not None
        and "Our Winter special is US$350 — a geyser and thermostat." in _ovr
        and "tub" not in _ovr and _ovr.endswith("CLOSE"),
        got=_ovr,
    )
    class _FakeCfgNoOffer:
        def price_item(self, family, variant=''):
            return None
    class _FakeSelfOverviewNone(_FakeSelfOverview):
        tenant_cfg = _FakeCfgNoOffer()
    results.log(
        "pricing overview: no offer -> None (deflect to free visit)",
        _FakeSelfOverviewNone()._compose_pricing_overview('english') is None,
        got="None as expected",
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
    # Phase 2.4: snippets render from tenant data — build them with a fake
    # carrying the pinned homebase figures via the price-map methods.
    class _FakeSelfSnips:
        _compose_snippets = ResponseMixin._compose_snippets
        tenant_cfg = None  # unused: _compose_snippets reads via pricing_copy._figures
        def __init__(self):
            from bot.models import Tenant
            from bot.tenant_config import get_config
            self.appointment = None
            self.tenant_cfg = get_config(Tenant.objects.filter(slug='homebase').first())
    _snips = _FakeSelfSnips()._compose_snippets()
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
        # Case-insensitive: the copy reads "That's everything in. Supply,
        # install, ..." now that the dash became a full stop, and the
        # assertion is about what the reframe SAYS, not how it is cased.
        ("everything in" in _bo and "supply, install" in _bo.lower()
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

# A SINGLE-product price ask must capture too. It didn't, so after "How much tub"
# the description stayed empty, the flow still sat on project_description, and the
# carried-over product_intent had an open lane into the availability answer
# (prod 2026-07-29, lead 670). The multi-item and quote paths always captured;
# this pins the single-product entry point alongside them.
class _FakeSelfPriceCapture:
    _PRODUCT_FAMILY_PATTERNS = ResponseMixin._PRODUCT_FAMILY_PATTERNS
    _product_families_in = ResponseMixin._product_families_in
    _capture_named_products_as_description = ResponseMixin._capture_named_products_as_description
    handle_service_inquiry = ResponseMixin.handle_service_inquiry
    def __init__(self, appt):
        self.appointment = appt
    def _handle_service_inquiry_impl(self, intent, message):
        return "Built-in bathtubs from US$160 all-in."
    def _ensure_price_disclaimer(self, intent, reply):
        return reply
try:
    _ap3 = _FakeApptDesc(desc=None)
    _reply3 = _FakeSelfPriceCapture(_ap3).handle_service_inquiry('tub_sales', "How much tub")
    results.log(
        "price reply: single-product ask captures the item as the description",
        _ap3.project_description == "tub" and "US$160" in _reply3,
        expected="desc='tub', reply intact",
        got=f"desc={_ap3.project_description!r}, reply={_reply3[:40]!r}",
    )
    _ap4 = _FakeApptDesc(desc="kitchen sink swap")
    _FakeSelfPriceCapture(_ap4).handle_service_inquiry('tub_sales', "How much tub")
    results.log(
        "price reply: does not overwrite a description already captured",
        _ap4.project_description == "kitchen sink swap",
        got=str(_ap4.project_description),
    )
except Exception as e:
    results.log("price reply: single-product capture", False, got=str(e))
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
    # Shona "just send it right here". Both of these were answered by re-asking
    # the identical email-or-WhatsApp question, twice in a row (prod, barmak
    # 2026-08-28) — the English substring list could not see a send verb and a
    # "here" word separated by other words.
    ("Munongo senda ipapa handi wanzo gara ne data", True),
    ("Muno sender zvenyu ipapa apa",                 True),
    ("tumirai pano",                                 True),
    ("Ndiri kuchitungwiza",                          False),  # an area, not a channel
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

# ─────────────────────────────────────────────────────────────────────────────
# A service only ONE tenant sells must be priceable in chat. Barmak's price
# sheet carries "Tiling per square meter" (US$15 supply + US$5 labour), but no
# hardcoded intent named it, so "how much to tile a bathroom" resolved to
# nothing and got the tub/Facebook-package overview instead — a price for work
# the lead never asked about (prod, barmak, 2026-08-28). The tenant's own rows
# now render their own pricing block and the lead's own word resolves to it.
# Fully offline: a stub config, no DB, no API.
from bot.pricing_copy import (
    build_structured_pricing as _bsp_ti, tenant_custom_items as _tci,
    tenant_item_label as _til, is_tenant_item_intent as _is_ti,
)
from bot.out_of_scope_handler import _word_stem as _wstem
from bot.whatsapp_webhook import _keyword_product_intent as _kpi


class _StubPriceRow:
    def __init__(self, family, variant='', label='', supply=None, labour=None,
                 allin=None, flat=None, keywords=None, short_label='', parts=None):
        self.family, self.variant, self.label = family, variant, label
        self.short_label, self.keywords = short_label, keywords or []
        self.supply, self.labour, self.allin, self.flat = supply, labour, allin, flat
        self.parts = parts or []


class _StubCfg:
    currency = 'US$'

    def __init__(self, rows):
        self._rows = rows

    def price_items(self):
        return self._rows

    def price_item(self, family, variant=''):
        for row in self._rows:
            if row.family == family and row.variant == variant:
                return row
        return None


_TI_KEY = 'tenant_item:other:tiling_per_square_meter'
_ti_cfg = _StubCfg([
    _StubPriceRow('other', 'tiling_per_square_meter', 'Tiling per square meter',
                  supply=15, labour=5, allin=20),
    _StubPriceRow('pvc-value-gutters', '', 'Pvc value gutters',
                  supply=600, labour=320, allin=920),
    # A standard family — priced by its own intent, never as a custom item.
    _StubPriceRow('tub', '', 'Built-in tub', supply=150, labour=85, allin=235),
])
_bare_cfg = _StubCfg([_StubPriceRow('tub', '', 'Built-in tub',
                                    supply=150, labour=85, allin=235)])

try:
    _custom = _tci(_ti_cfg)
    results.log(
        "tenant_custom_items: own services only",
        set(_custom) == {_TI_KEY, 'tenant_item:pvc-value-gutters:'},
        expected="tiling + gutters, tub excluded (standard family)",
        got=str(sorted(_custom)),
    )
except Exception as e:
    results.log("tenant_custom_items: own services only", False, got=str(e))

try:
    _block = _bsp_ti(_ti_cfg).get(_TI_KEY) or {}
    # Labour-first shape: no bullets, labour then the supplied-too all-in
    # figure, then the rough-guide caveat. Still the tenant's OWN figures.
    _ok = (
        _block.get('breakdown_lines') == []
        and _block.get('total_line') == (
            'Tiling per square meter: labour from US$5, parts from US$15, '
            'so from US$20 all-in.')
        and 'rough guide' in (_block.get('cheapest_line') or '')
        and 'kubva US$5' in (_block.get('sn_total_line') or '')
    )
    results.log(
        "structured pricing: tenant item renders the tenant's own figures",
        _ok,
        expected="labour US$5 first, then US$20 all-in, EN + SN",
        got=str(_block.get('total_line')),
    )
except Exception as e:
    results.log("structured pricing: tenant item renders the tenant's own figures",
                False, got=str(e))

# (message, cfg, expected intent) — the lead's own word resolves to the
# tenant's row; a standard product word still wins; a tenant WITHOUT the row is
# completely unaffected (homebase must never gain a tiling price).
TENANT_ITEM_INTENT_CASES = [
    ("how much to install tiles",  _ti_cfg,   _TI_KEY),
    ("tiling price",               _ti_cfg,   _TI_KEY),
    ("how much are gutters",       _ti_cfg,   'tenant_item:pvc-value-gutters:'),
    ("how much for a tub",         _ti_cfg,   'tub_sales'),
    ("how much shower cubicle",    _ti_cfg,   'shower_cubicle'),
    ("how much to install tiles",  _bare_cfg, None),
    ("how much to install tiles",  None,      None),
    ("ok",                         _ti_cfg,   None),
]
for _msg, _cfg, _expected in TENANT_ITEM_INTENT_CASES:
    try:
        _got = _kpi(_msg, _cfg)
        results.log(
            f"_keyword_product_intent(tenant): '{_msg[:28]}'",
            _got == _expected,
            expected=str(_expected),
            got=str(_got),
        )
    except Exception as e:
        results.log(f"_keyword_product_intent(tenant): '{_msg[:28]}'", False, got=str(e))

try:
    results.log(
        "tenant_item_label / is_tenant_item_intent",
        _til(_ti_cfg, _TI_KEY) == 'Tiling per square meter'
        and _is_ti(_TI_KEY) and not _is_ti('tub_sales') and not _is_ti(None),
        expected="label resolves, prefix check is exact",
        got=f"label={_til(_ti_cfg, _TI_KEY)!r}",
    )
except Exception as e:
    results.log("tenant_item_label / is_tenant_item_intent", False, got=str(e))

# "tiles" must find a row labelled "Tiling per square meter" — plain substring
# matching missed it, so a tenant who tiles was told tiling is out of scope.
STEM_PAIRS = [('tiles', 'tiling'), ('roof', 'roofing'), ('gutter', 'gutters'),
              ('tub', 'tubs'), ('paint', 'painting')]
for _a, _b in STEM_PAIRS:
    try:
        results.log(
            f"_word_stem: '{_a}' == '{_b}'",
            _wstem(_a) == _wstem(_b),
            expected="same stem",
            got=f"{_wstem(_a)!r} vs {_wstem(_b)!r}",
        )
    except Exception as e:
        results.log(f"_word_stem: '{_a}' == '{_b}'", False, got=str(e))

try:
    results.log(
        "_word_stem: distinct services stay distinct",
        _wstem('tiles') != _wstem('toilet') and _wstem('geyser') != _wstem('gutter'),
        expected="no collision between unrelated services",
        got=f"tiles={_wstem('tiles')!r} toilet={_wstem('toilet')!r}",
    )
except Exception as e:
    results.log("_word_stem: distinct services stay distinct", False, got=str(e))

# The Shona way of deferring — "I'll get back to you once I've sorted the
# money" — must reach the delay branch even with DeepSeek down. The offline
# keyword classifier read it as a normal in-scope message (prod, barmak,
# 2026-08-28).
from bot.out_of_scope_handler import _keyword_classify as _kwc
DELAY_KEYWORD_CASES = [
    ("Ndiri kuchitungwiza ndichakubatayi  ndapedza kuronga nyayadze mari", 'delay_signal'),
    ("ndichakufona",                       'delay_signal'),
    ("kana ndawana mari ndichakubata",     'delay_signal'),
    ("call me later",                      'delay_signal'),
    ("how much is a tub",                  'in_scope'),
    ("Ndiri kuchitungwiza",                'in_scope'),   # an area alone is not a delay
]
for _msg, _expected in DELAY_KEYWORD_CASES:
    try:
        _got = _kwc(_msg).get('category')
        results.log(
            f"_keyword_classify: '{_msg[:32]}'",
            _got == _expected,
            expected=_expected,
            got=str(_got),
        )
    except Exception as e:
        results.log(f"_keyword_classify: '{_msg[:32]}'", False, got=str(e))

# A message that BOTH answers a question and defers ("Ndiri kuChitungwiza
# ndichakubatayi ndapedza kuronga mari" — my area is Chitungwiza, I'll get back
# to you once I've sorted the money) must reach the delay flow whatever the
# category classifier said that run. Deterministic override, same pattern as the
# access deferral. Broad words that also appear in BOOKING messages ("mangwana"
# = tomorrow) must NOT trip it.
from bot.out_of_scope_handler import _is_explicit_deferral as _ixd
EXPLICIT_DEFERRAL_CASES = [
    ("Ndiri kuchitungwiza ndichakubatayi  ndapedza kuronga nyayadze mari", True),
    ("ndichakubata mangwana",              True),
    ("kana ndawana mari ndichauya",        True),
    ("I'll get back to you",               True),
    ("call me later",                      True),
    ("mangwana",                           False),  # tomorrow — a booking word
    ("ndouya mangwana",                    False),  # I'm coming tomorrow
    ("how much is a tub",                  False),
    ("Ndiri kuchitungwiza",                False),  # an area alone
]
for _msg, _expected in EXPLICIT_DEFERRAL_CASES:
    try:
        results.log(
            f"_is_explicit_deferral: '{_msg[:32]}'",
            _ixd(_msg) == _expected,
            expected=f"deferral={_expected}",
            got=f"deferral={_ixd(_msg)}",
        )
    except Exception as e:
        results.log(f"_is_explicit_deferral: '{_msg[:32]}'", False, got=str(e))

# The override must be wired into handle_out_of_scope, not just defined.
import inspect as _insp_d
import bot.out_of_scope_handler as _oos_d
_src_d = _insp_d.getsource(_oos_d.handle_out_of_scope)
results.log("delay override: explicit deferral outranks an in_scope verdict",
            '_is_explicit_deferral(message)' in _src_d
            and _src_d.find('_is_explicit_deferral') < _src_d.find('if category == "in_scope"'))

# ─────────────────────────────────────────────────────────────────────────────
# Inbound language normalisation. Every deterministic resolver matches ENGLISH
# phrases while customers write Shona, so each one silently failed until its
# Shona phrases were hand-written in after a lead had already been mishandled.
# The classifier's English rendering is now scanned alongside the customer's own
# words. Two properties are pinned here, and the second matters as much as the
# first: with NO rendering on file (DeepSeek down) the Shona keyword net must
# still carry the known phrasings, because that is exactly when it is all we
# have. Offline — nothing here calls an API.
import bot.message_normalizer as _mn
from bot.out_of_scope_handler import (
    wants_whatsapp_delivery as _wwd_n,
    _is_explicit_deferral as _ixd_n,
    _keyword_classify as _kwc_n,
    _email_step_intent_keywords as _esk_n,
)
from bot.whatsapp_webhook import _keyword_product_intent as _kpi_n

# A Shona phrasing none of the hand-written lists anticipates. Without a
# rendering it is invisible; with one, every English resolver sees it.
_UNSEEN_WA = "Zvitume imomo mandiri"          # "just put it through to me there"
# (Whatever is picked here must be a phrasing NO list covers — the previous
# choice, "handisati ndagadzirira", later became a known phrase and quietly
# invalidated this case, which is the point being made: hand-written lists only
# ever cover what someone already thought of.)
_UNSEEN_DEFER = "Ndichange ndichizvifunga"         # "I will be thinking it over"
_UNSEEN_PRODUCT = "Ndoda kugadzirisa chimbuzi"     # "I want to fix my toilet"

_mn.forget_all()
try:
    results.log(
        "normalizer: an unseen Shona phrasing is invisible with no rendering",
        _wwd_n(_UNSEEN_WA) is False
        and _ixd_n(_UNSEEN_DEFER) is False
        and _kpi_n(_UNSEEN_PRODUCT) is None,
        expected="no rendering -> the English lists cannot see it",
        got=f"wa={_wwd_n(_UNSEEN_WA)} defer={_ixd_n(_UNSEEN_DEFER)} "
            f"product={_kpi_n(_UNSEEN_PRODUCT)}",
    )
except Exception as e:
    results.log("normalizer: an unseen Shona phrasing is invisible with no rendering",
                False, got=str(e))

try:
    _mn.remember(_UNSEEN_WA, "Just send it here to me")
    _mn.remember(_UNSEEN_DEFER, "I will be thinking it over and will get in touch")
    _mn.remember(_UNSEEN_PRODUCT, "I want to fix my toilet")
    results.log(
        "normalizer: the rendering makes every English resolver see it",
        _wwd_n(_UNSEEN_WA) is True
        and _ixd_n(_UNSEEN_DEFER) is True
        and _kpi_n(_UNSEEN_PRODUCT) == 'toilet_repair',
        expected="wa=True defer=True product=toilet_repair",
        got=f"wa={_wwd_n(_UNSEEN_WA)} defer={_ixd_n(_UNSEEN_DEFER)} "
            f"product={_kpi_n(_UNSEEN_PRODUCT)}",
    )
except Exception as e:
    results.log("normalizer: the rendering makes every English resolver see it",
                False, got=str(e))

# The keyword net is NOT redundant: with DeepSeek down there is no rendering at
# all, and the phrases already written in must still carry these on their own.
_mn.forget_all()
KEYWORD_NET_CASES = [
    ("wants_whatsapp_delivery", lambda: _wwd_n("Muno sender zvenyu ipapa apa"), True),
    ("wants_whatsapp_delivery", lambda: _wwd_n("Munongo senda ipapa"), True),
    ("_is_explicit_deferral", lambda: _ixd_n("ndichakubatayi ndapedza kuronga mari"), True),
    ("_keyword_classify", lambda: _kwc_n("ndichakufona").get('category'), 'delay_signal'),
    ("_email_step_intent_keywords", lambda: _esk_n("tumirai pano"), 'whatsapp'),
]
for _name, _call, _expected in KEYWORD_NET_CASES:
    try:
        _got = _call()
        results.log(
            f"normalizer: keyword net still carries {_name} with DeepSeek down",
            _got == _expected,
            expected=str(_expected),
            got=str(_got),
        )
    except Exception as e:
        results.log(f"normalizer: keyword net still carries {_name} with DeepSeek down",
                    False, got=str(e))

# 'no' and 'na' sit in the email-step decline list, and as bare substrings they
# fire inside "muno", "pano", "know", "phone" and "not" — so a Shona lead asking
# "muno chaja seyi" (how do you charge here) read as declining the email. Those
# tokens are word-boundary matched now.
DECLINE_WORD_CASES = [
    ("Imariyi kuisa ma tails muno chaja seyi", 'unclear'),   # "muno" is not "no"
    ("pano",                                   'unclear'),
    ("my phone number is 077",                 'unclear'),
    ("know what",                              'unclear'),
    ("no thanks",                              'decline'),
    ("nah",                                    'decline'),
    ("skip it",                                'decline'),
    ("not interested",                         'decline'),
]
for _msg, _expected in DECLINE_WORD_CASES:
    try:
        _got = _esk_n(_msg)
        results.log(
            f"email step decline is word-matched: '{_msg[:30]}'",
            _got == _expected,
            expected=_expected,
            got=str(_got),
        )
    except Exception as e:
        results.log(f"email step decline is word-matched: '{_msg[:30]}'", False, got=str(e))

# A rendering must never invent intent that is in neither text.
try:
    _mn.remember("Ndatenda", "Thank you")
    results.log(
        "normalizer: a rendering with no signal resolves nothing",
        _wwd_n("Ndatenda") is False and _ixd_n("Ndatenda") is False
        and _kpi_n("Ndatenda") is None,
        expected="a plain thank-you stays inert",
        got=f"wa={_wwd_n('Ndatenda')} defer={_ixd_n('Ndatenda')}",
    )
except Exception as e:
    results.log("normalizer: a rendering with no signal resolves nothing", False, got=str(e))

# Bookkeeping: an English message stores nothing (there is no second text to
# scan), lookups are whitespace/case-insensitive, and the cache is bounded so a
# long-running worker cannot grow it forever.
_mn.forget_all()
try:
    _mn.remember("Just send it here", "Just send it here")
    _no_dupe = _mn.english_for("Just send it here") == ''
    _mn.remember("Muno sender ipapa", "Just send it here")
    _case = (_mn.english_for("  MUNO   sender ipapa ") == 'Just send it here')
    _texts = _mn.rule_texts("Muno sender ipapa")
    results.log(
        "normalizer: already-English stores nothing, lookup ignores case/spacing",
        _no_dupe and _case and len(_texts) == 2,
        expected="'' for English, hit for messy lookup, two texts to scan",
        got=f"no_dupe={_no_dupe} case={_case} texts={len(_texts)}",
    )
except Exception as e:
    results.log("normalizer: already-English stores nothing, lookup ignores case/spacing",
                False, got=str(e))

try:
    _mn.forget_all()
    for _i in range(_mn._MAX_ENTRIES + 50):
        _mn.remember(f"shona message {_i}", f"english {_i}")
    results.log(
        "normalizer: the cache is bounded and evicts oldest first",
        len(_mn._cache) == _mn._MAX_ENTRIES
        and _mn.english_for("shona message 0") == ''
        and _mn.english_for(f"shona message {_mn._MAX_ENTRIES + 49}")
        == f"english {_mn._MAX_ENTRIES + 49}",
        expected=f"{_mn._MAX_ENTRIES} entries, oldest gone, newest kept",
        got=f"size={len(_mn._cache)}",
    )
except Exception as e:
    results.log("normalizer: the cache is bounded and evicts oldest first", False, got=str(e))
finally:
    _mn.forget_all()

# The rendering is for the RULE ENGINE only — it must never be handed to a
# customer or dropped into a reply prompt (the bot answers in the lead's own
# language). Pinned against the one place that stores it.
try:
    import inspect as _insp_n
    import bot.whatsapp_webhook as _wwh_n
    _src_n = _insp_n.getsource(_wwh_n._generate_and_schedule_reply)
    # Every CALL of it (the accessor import line aside) must be the one that
    # hands it to remember() — anything else would be a route to the customer.
    _uses = [l.strip() for l in _src_n.splitlines()
             if 'uc_english(' in l and not l.strip().startswith(('from ', 'import '))]
    results.log(
        "normalizer: the rendering is stored for rules and never sent onward",
        len(_uses) == 1 and '_remember_english' in _uses[0],
        expected="uc_english() is called once, into remember()",
        got=str(_uses),
    )
except Exception as e:
    results.log("normalizer: the rendering is stored for rules and never sent onward",
                False, got=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# Asking for an email must give the LEAD a reason to want to give it. Every ask
# used to be a bare extraction — "what's the best email to reach you on?" — with
# nothing in it for them, and conv 846 asked four times and got "just send it
# here" twice. Each ask now carries _EMAIL_VALUE_CLAUSE: it keeps (a document in
# a chat is gone when the phone changes), it travels (these jobs are rarely a
# one-person decision), it compares (a quote-collector can put ours beside the
# others). Offline — plain string checks.
import re as _re_em
from bot.out_of_scope_handler import (
    _EMAIL_VALUE_CLAUSE as _EVC,
    _DELIVERY_CHOICE_QUESTION as _DCQ,
    _DELIVERY_CHOICE_MARKER as _DCM,
)

# The three benefits, each named so a future rewrite cannot quietly drop one.
EMAIL_VALUE_BENEFITS = [
    ("it keeps",    ('open it any time', 'keep')),
    ("it travels",  ('whoever else', 'pass on', 'send it on')),
    ("it compares", ('other quotes', 'against any other')),
]
for _label, _markers in EMAIL_VALUE_BENEFITS:
    try:
        results.log(
            f"email ask states the benefit — {_label}",
            any(m in _EVC.lower() for m in _markers),
            expected=f"one of {_markers}",
            got=_EVC[:80],
        )
    except Exception as e:
        results.log(f"email ask states the benefit — {_label}", False, got=str(e))

# The clause has to actually REACH every ask. Source-level, because the asks are
# assembled from f-strings at import time in branches a test cannot easily run.
try:
    import inspect as _insp_em
    import bot.out_of_scope_handler as _oos_em
    _src_em = _insp_em.getsource(_oos_em)
    # Every "what's the best email" ask in the file.
    _asks = [l.strip() for l in _src_em.splitlines()
             if _re_em.search(r"best email|email should we|what email", l, _re_em.I)
             and not l.strip().startswith('#')]
    _uses = _src_em.count('_EMAIL_VALUE_CLAUSE')
    results.log(
        "every email ask is paired with the value clause",
        # one definition + one use per ask site
        _uses >= len(_asks),
        expected=f"{len(_asks)} ask(s) -> at least that many clause uses",
        got=f"asks={len(_asks)} clause_uses={_uses - 1}",
    )
except Exception as e:
    results.log("every email ask is paired with the value clause", False, got=str(e))

# The delivery choice RECOMMENDS email rather than shrugging ("either works"
# gave the lead no reason to pick) — while still taking WhatsApp for an answer,
# because a lead who asks for it here must never be argued with.
try:
    _low = _DCQ.lower()
    results.log(
        "delivery choice recommends email but still honours WhatsApp",
        ('suggest' in _low or 'recommend' in _low)
        and 'whatsapp' in _low
        and 'right here' in _low
        and 'either works' not in _low,
        expected="a recommendation + WhatsApp still on the table",
        got=_DCQ[:90],
    )
except Exception as e:
    results.log("delivery choice recommends email but still honours WhatsApp",
                False, got=str(e))

# The choice also names OUR reason for wanting the address — keeping the quote
# on file and following up cleanly. Owner-written and deliberate: said plainly
# it reads as straight dealing, and "followed up properly" is the lead's benefit
# as much as ours.
try:
    _low_own = _DCQ.lower()
    results.log(
        "delivery choice states our own reason plainly too",
        'on file' in _low_own and 'follow' in _low_own,
        expected="keeps the quote on file / follows up cleanly",
        got=_DCQ[:120],
    )
except Exception as e:
    results.log("delivery choice states our own reason plainly too", False, got=str(e))

# The timeframe ask rides along ONLY when no check-back date is on file. With a
# date already agreed we have just said "we'll check back on <date>", and asking
# again reads as not listening (conv 415/566). Two questions in one message is
# otherwise against the copy rules, so this stays conditional.
try:
    from bot.out_of_scope_handler import _delivery_choice_question as _dcq_fn
    _no_date = _dcq_fn(None)
    _with_date = _dcq_fn('2026-09-11')
    results.log(
        "timeframe ask rides along only when no date is on file",
        'ready to go ahead' in _no_date
        and 'ready to go ahead' not in _with_date
        and _with_date == _DCQ,
        expected="tail with no date, single ask once a date is agreed",
        got=f"no_date_has_tail={'ready to go ahead' in _no_date} "
            f"with_date_has_tail={'ready to go ahead' in _with_date}",
    )
except Exception as e:
    results.log("timeframe ask rides along only when no date is on file",
                False, got=str(e))

# Ask a question, handle its answer: having asked when they will be ready, a
# timeframe reply must be captured, not force-fit as a failed email address.
try:
    import inspect as _insp_tf
    import bot.out_of_scope_handler as _oos_tf
    _src_tf = _insp_tf.getsource(_oos_tf._handle_delay_email_answer)
    results.log(
        "a timeframe answer at the email step is captured, not force-fit",
        '_message_has_timeframe(msg)' in _src_tf
        and _src_tf.find('_message_has_timeframe(msg)')
            < _src_tf.find('_classify_email_step_reply'),
        expected="timeframe check runs before the email classifier",
        got="present" if '_message_has_timeframe(msg)' in _src_tf else "MISSING",
    )
except Exception as e:
    results.log("a timeframe answer at the email step is captured, not force-fit",
                False, got=str(e))

# The loop guard finds the question by this marker; a copy rewrite that drops it
# would silently re-enable the duplicate-question loop it was written to stop.
try:
    results.log(
        "delivery choice marker still matches its own question",
        _DCM in _DCQ,
        expected="marker is a substring of the question",
        got=f"marker={_DCM!r}",
    )
except Exception as e:
    results.log("delivery choice marker still matches its own question", False, got=str(e))

# House rules: no emojis anywhere in this copy, and no Homebase-only value
# (name, plumber, place, figure) — out_of_scope_handler serves every tenant.
_EMOJI_RE = _re_em.compile(
    '[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF]')
for _name, _text in (('value clause', _EVC), ('delivery choice', _DCQ)):
    try:
        _leak = [w for w in ('homebase', 'takudzwa', 'harare', 'us$', '263')
                 if w in _text.lower()]
        results.log(
            f"{_name}: no emojis, no tenant-specific value",
            not _EMOJI_RE.search(_text) and not _leak,
            expected="emoji-free and tenant-neutral",
            got=f"emoji={bool(_EMOJI_RE.search(_text))} leak={_leak}",
        )
    except Exception as e:
        results.log(f"{_name}: no emojis, no tenant-specific value", False, got=str(e))

# The first delay-email nudge carries the reason too — it is the ask that goes
# out to a lead who already ignored one, so a bare re-ask is the weakest thing
# it could say. Later nudges stay short by design, and the last one concedes.
try:
    from bot.management.commands.send_followups import Command as _FUCmd_em
    _nudges = _FUCmd_em._DELAY_NUDGE_MESSAGES['delay_email']
    _first = _nudges[0].lower()
    results.log(
        "first delay-email nudge gives a reason, not a bare re-ask",
        ('whoever else' in _first or 'keep' in _first)
        and 'other quotes' in _first
        and not _EMOJI_RE.search(_nudges[0]),
        expected="benefits named before the ask",
        got=_nudges[0][:90],
    )
    results.log(
        "the last delay-email nudge still concedes gracefully",
        'rather not' in _nudges[-1].lower() and 'whatsapp' in _nudges[-1].lower(),
        expected="no pressure, falls back to WhatsApp",
        got=_nudges[-1][:80],
    )
except Exception as e:
    results.log("first delay-email nudge gives a reason, not a bare re-ask",
                False, got=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# A delay signal must outrank the scope question we just asked. Prod (barmak,
# +263773263897, 2026-08-28): asked "Is a tub the only thing you're looking to
# get sorted?", the lead replied "No my main bedroom is not yet sorted will get
# in touch ndasvika pa stage iyoyo thanx" — not ready, will come back. The
# leading "No" was read as a scope answer and the bot pushed for MORE work
# ("what else would you like sorted while we're there?"), because that branch
# answers and returns before STEP 1b's delay handler ever runs.
#
# Two independent faults, both pinned below: the wording was in no list ("will
# BE in touch" was, "will GET in touch" was not), and the pending state
# swallowed the message even when the wording matched.
from bot.out_of_scope_handler import (
    _is_explicit_deferral as _ixd2, _keyword_classify as _kwc2,
)
from bot.message_normalizer import forget_all as _forget_sc

_forget_sc()
_PROD_846B = ("No my main bedroom is not yet sorted will get in touch "
              "ndasvika pa stage iyoyo thanx")
_PROD_TUB = ("I want to ask kuti type yema tub ayo inoita kuvakira here "
             "..handisati hangu ndasvika ku plumbing")

# (message, is it a deferral?) — the False rows are the ones that matter most:
# "get in touch" as a bare substring swept up an EAGER lead.
NOT_YET_STAGE_CASES = [
    (_PROD_846B,                                        True),
    (_PROD_TUB,                                         True),   # words between
    ("will get in touch",                               True),
    ("I'll get in touch once the builder is done",      True),
    ("I will be in touch",                              True),
    ("handisati ndagadzirira",                          True),
    ("not yet sorted, still building",                  True),
    ("I want to get in touch with your plumber today",  False),  # eager, not delay
    ("can you get in touch with me now",                False),
    ("how much is a tub",                               False),
    ("can you come Wednesday",                          False),
    ("yes please book me in",                           False),
    ("ndiri kuChitungwiza",                             False),
]
for _msg, _expected in NOT_YET_STAGE_CASES:
    try:
        results.log(
            f"not-yet-stage deferral: '{_msg[:34]}'",
            _ixd2(_msg) == _expected,
            expected=f"deferral={_expected}",
            got=f"deferral={_ixd2(_msg)}",
        )
    except Exception as e:
        results.log(f"not-yet-stage deferral: '{_msg[:34]}'", False, got=str(e))

# The offline net shares the one resolver, so it agrees without DeepSeek.
try:
    results.log(
        "offline classifier reads the prod message as a delay",
        _kwc2(_PROD_846B).get('category') == 'delay_signal'
        and _kwc2("how much is a tub").get('category') == 'in_scope',
        expected="delay_signal for the deferral, in_scope for a price ask",
        got=str(_kwc2(_PROD_846B).get('category')),
    )
except Exception as e:
    results.log("offline classifier reads the prod message as a delay", False, got=str(e))

# The structural half: the service-confirm branch must yield to a delay signal,
# and must do so BEFORE it can answer-and-return.
try:
    import inspect as _insp_sc
    import bot.whatsapp_webhook as _wwh_sc
    _src_sc = _insp_sc.getsource(_wwh_sc._generate_and_schedule_reply)
    _override_at = _src_sc.find('_sc_delay_override =')
    _branch_at = _src_sc.find("and not _sc_delay_override")
    _whatelse_at = _src_sc.find('what else would you like sorted')
    results.log(
        "service-confirm hold yields to a delay signal",
        _override_at != -1 and _branch_at != -1
        and _override_at < _branch_at < _whatelse_at,
        expected="override computed, then gates the branch that replies",
        got=f"override={_override_at} branch={_branch_at} reply={_whatelse_at}",
    )
    results.log(
        "the delay override consults both the LLM and the lead's own words",
        "uc_intent(_uclass) == 'delay_signal'" in _src_sc
        and '_is_explicit_deferral(message_body)' in _src_sc,
        expected="classifier verdict OR deterministic resolver",
        got="present" if '_is_explicit_deferral(message_body)' in _src_sc else "MISSING",
    )
    # Leaving the tags set would re-fire the scope question on their answer to
    # "roughly when are you hoping to get this sorted?".
    _ovr_block = _src_sc[_override_at:_branch_at]
    results.log(
        "the scope tags are cleared when the delay flow takes over",
        "_remove_notes_tag('[SERVICE_CONFIRM_PENDING]')" in _ovr_block
        and "_remove_notes_tag('[AWAITING_MORE_ITEMS]')" in _ovr_block,
        expected="both pending tags cleared",
        got=_ovr_block[-120:].strip(),
    )
except Exception as e:
    results.log("service-confirm hold yields to a delay signal", False, got=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# A parked lead coming back READY must break the delay hold. The breakout check
# only knew price asks and named products — built for fresh QUESTIONS — so a
# commitment carried none of the signals it looked for. Traced 2026-08-29 on the
# real lead 846 (parked in delay_email): "I want to get in touch with your
# plumber today" was answered with the email-or-WhatsApp delivery pitch. That is
# the best message in the whole flow and it was met with a filing request.
#
# Ordering is what makes this safe, and it is pinned below: a deferral is tested
# BEFORE re-engagement, because "I'll be ready to go ahead next month" carries
# re-engagement words while being the exact opposite.
from bot.out_of_scope_handler import (
    _delay_breakout_inquiry as _dbi2, _is_reengagement_signal as _irs2,
)
from bot.message_normalizer import forget_all as _forget_re

_forget_re()
# (message, should it break the holding pattern?)
BREAKOUT_CASES = [
    # Ready to move — every one of these used to be swallowed by the hold.
    ("I want to get in touch with your plumber today", True),
    ("I want to speak to the plumber",                 True),
    ("can I talk to your plumber",                     True),
    ("can you come today",                             True),
    ("when can you come",                              True),
    ("I want to book",                                 True),
    ("ndoda kubhukisha",                               True),
    # Still the old breakouts — a price ask and a named product.
    ("how much is a tub",                              True),
    ("what about the shower cubicle",                  True),
    # NOT breakouts: a timeframe is the answer we asked for, and a deferral
    # wearing re-engagement words is still a deferral.
    ("I'll be ready to go ahead next month",           False),
    ("next month",                                     False),
    ("end of the month",                               False),
    ("will get in touch when I am ready",              False),
    ("handisati ndagadzirira",                         False),
    ("ok thanks",                                      False),
    ("jones@gmail.com",                                False),
]
for _msg, _expected in BREAKOUT_CASES:
    try:
        results.log(
            f"delay breakout: '{_msg[:36]}'",
            _dbi2(_msg) == _expected,
            expected=f"breakout={_expected}",
            got=f"breakout={_dbi2(_msg)}",
        )
    except Exception as e:
        results.log(f"delay breakout: '{_msg[:36]}'", False, got=str(e))

# The deferral test must come first in the source, not just happen to win.
try:
    import inspect as _insp_re
    import bot.out_of_scope_handler as _oos_re
    _src_re = _insp_re.getsource(_oos_re._delay_breakout_inquiry)
    _defer_at = _src_re.find('_is_explicit_deferral(message)')
    _time_at = _src_re.find('_message_has_timeframe(message)')
    _reeng_at = _src_re.find('_is_reengagement_signal(message)')
    results.log(
        "a deferral is ruled out before re-engagement is considered",
        -1 < _defer_at < _time_at < _reeng_at,
        expected="deferral, then timeframe, then re-engagement",
        got=f"defer={_defer_at} timeframe={_time_at} reengage={_reeng_at}",
    )
except Exception as e:
    results.log("a deferral is ruled out before re-engagement is considered",
                False, got=str(e))

# Wanting the plumber is re-engagement on its own, with no booking word in it.
try:
    _plumber_ask = _irs2("can I get the plumber's number")
    results.log(
        "asking for the plumber counts as re-engagement",
        _plumber_ask and _irs2("I want to speak to someone")
        and not _irs2("how much is a tub"),
        expected="plumber/human asks yes, a bare price ask no",
        got=f"plumber={_plumber_ask}",
    )
except Exception as e:
    results.log("asking for the plumber counts as re-engagement", False, got=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# The shared FAQ trigger lists must carry NO tenant's proper nouns. They held
# Homebase's outright — "talk to takudzwa", "where is homebase" — which for
# every other tenant could never fire, while a lead typing their own plumber's
# name matched nothing. Names are generated per lead in _tenant_triggers()
# (crossing is covered with a real DB in TenantConfigTests). Offline here.
from bot.faq import (
    _TRIGGERS as _FAQ_TRIGGERS, match_faq_topic as _mft2,
    _composed_contact_fact as _ccf,
)

try:
    _proper_nouns = ('takudzwa', 'homebase', 'barmak', 'kudakwashe')
    _offenders = [
        f"{topic}:{trigger}"
        for topic, triggers in _FAQ_TRIGGERS.items()
        for trigger in triggers
        if any(noun in trigger.lower() for noun in _proper_nouns)
    ]
    results.log(
        "FAQ triggers carry no tenant's proper nouns",
        not _offenders,
        expected="every shared trigger is generic",
        got=str(_offenders),
    )
except Exception as e:
    results.log("FAQ triggers carry no tenant's proper nouns", False, got=str(e))

# Asking to reach the plumber in the obvious words matched NOTHING before —
# the contact list only knew the plumber by name plus "speak to someone".
PLUMBER_CONTACT_CASES = [
    ("I want to get in touch with your plumber today", 'contact'),
    ("can I speak to the plumber",                     'contact'),
    ("what is the plumber's number",                   'contact'),
    ("can I call your plumber",                        'contact'),
    ("can I speak to someone",                         'contact'),
    ("how much is a tub",                              None),
    ("can you come Wednesday",                         None),
    ("I want to book a plumber for Friday",            None),
]
for _msg, _expected in PLUMBER_CONTACT_CASES:
    try:
        results.log(
            f"FAQ contact topic: '{_msg[:34]}'",
            _mft2(_msg) == _expected,
            expected=str(_expected),
            got=str(_mft2(_msg)),
        )
    except Exception as e:
        results.log(f"FAQ contact topic: '{_msg[:34]}'", False, got=str(e))

# A tenant holding a plumber but no hand-written 'contact' fact used to skip the
# topic entirely, so a lead asking got nothing while we held the number. Absent
# NUMBER still omits — composing from their own data is not borrowing.
class _StubContactCfg:
    def __init__(self, name='', contact=''):
        self.plumber_name, self.plumber_contact = name, contact

try:
    _named = _ccf(_StubContactCfg('Kudakwashe Marange', '+263773871503'))
    _unnamed = _ccf(_StubContactCfg('', '+263773871503'))
    results.log(
        "contact fact composes from the tenant's own plumber",
        '+263773871503' in _named and 'Kudakwashe Marange' in _named
        and 'Takudzwa' not in _named
        and _unnamed is not None and 'the plumber' in _unnamed,
        expected="their number and name, never another tenant's",
        got=repr(_named),
    )
    results.log(
        "no number on file means no contact fact at all",
        _ccf(_StubContactCfg('Kudakwashe Marange', '')) is None
        and _ccf(_StubContactCfg('', '')) is None,
        expected="None, never a borrowed number",
        got=str(_ccf(_StubContactCfg('Kudakwashe Marange', ''))),
    )
except Exception as e:
    results.log("contact fact composes from the tenant's own plumber", False, got=str(e))

# The FAQ answers BEFORE the delay handler, so a contact request must clear any
# holding state itself — otherwise the lead gets the plumber's number and then
# walks back into the delay flow on their next message.
try:
    import inspect as _insp_ct
    import bot.whatsapp_webhook as _wwh_ct
    _src_ct = _insp_ct.getsource(_wwh_ct._generate_and_schedule_reply)
    _faq_at = _src_ct.find('match_faq_topic(message_body, tenant=tenant)')
    _clear_at = _src_ct.find("_faq_topic == 'contact'")
    results.log(
        "a contact request clears the delay hold",
        _faq_at != -1 and _clear_at > _faq_at
        and '_clear_pending(appointment)' in _src_ct[_clear_at:_clear_at + 700],
        expected="tenant threaded in, hold cleared on a contact match",
        got=f"faq={_faq_at} clear={_clear_at}",
    )
except Exception as e:
    results.log("a contact request clears the delay hold", False, got=str(e))

# ─────────────────────────────────────────────────────────────────────────────
# Nothing about Meta messaging may cost money (owner rule, 2026-08-29: the
# business runs on click-to-WhatsApp ads and intends to keep doing so past the
# service-message charge date).
#
# The bug this guards: messaging_window_open takes max(24h CSW, 72h FEP) and
# answers "may we send". Nobody answered "what does it cost". Those are two
# different Meta windows — the free entry point governs PRICE, the customer
# service window governs PERMISSION — so an ad lead who taps on Monday and
# writes back on Friday is permitted but billable. Proactive sends now wait for
# a free window instead of buying one.
from datetime import date as _d_free, timedelta as _td_free
from unittest.mock import patch as _mp_free
from django.utils import timezone as _tz_free
from bot.models import Appointment as _Apt_free
from bot.whatsapp_window import (
    may_send_proactively as _msp, paid_sends_allowed as _psa,
)

_NOW_FREE = _tz_free.now()
_CHARGE_OFF = _d_free(2099, 1, 1)   # service messages still free
_CHARGE_ON = _d_free(2000, 1, 1)    # service messages chargeable


def _free_lead(ctwa_hours_ago=None, inbound_hours_ago=None, notes=''):
    lead = _Apt_free(phone_number='whatsapp:+263700000000')
    lead.ctwa_entry_at = (_NOW_FREE - _td_free(hours=ctwa_hours_ago)
                          if ctwa_hours_ago is not None else None)
    lead.last_inbound_at = (_NOW_FREE - _td_free(hours=inbound_hours_ago)
                            if inbound_hours_ago is not None else None)
    lead.internal_notes = notes
    return lead


# (label, ctwa hours ago, inbound hours ago, charge date, window open?, free?)
COST_MATRIX = [
    # Today: every open window is free, so nothing changes for anyone.
    ("ad lead inside its 72h window",   10, 1, _CHARGE_OFF, True, True),
    ("ad lead past 72h, wrote 1h ago", 100, 1, _CHARGE_OFF, True, True),
    ("organic lead, wrote 1h ago",    None, 1, _CHARGE_OFF, True, True),
    ("organic lead, window shut",     None, 30, _CHARGE_OFF, False, False),
    # After the charge starts: only the ad free entry point is still free.
    ("ad lead inside its 72h window",   10, 1, _CHARGE_ON, True, True),
    ("ad lead past 72h, wrote 1h ago", 100, 1, _CHARGE_ON, True, False),
    ("organic lead, wrote 1h ago",    None, 1, _CHARGE_ON, True, False),
    ("organic lead, window shut",     None, 30, _CHARGE_ON, False, False),
]
for _label, _ctwa, _inb, _charge, _open, _free in COST_MATRIX:
    try:
        _lead = _free_lead(_ctwa, _inb)
        _when = 'charged' if _charge == _CHARGE_ON else 'free era'
        with _mp_free.object(_Apt_free, '_service_charge_date',
                             classmethod(lambda cls, _c=_charge: _c)):
            _got_open, _got_free = _lead.messaging_window_open, _lead.messaging_is_free
        results.log(
            f"messaging cost [{_when}]: {_label}",
            _got_open == _open and _got_free == _free,
            expected=f"open={_open} free={_free}",
            got=f"open={_got_open} free={_got_free}",
        )
    except Exception as e:
        results.log(f"messaging cost: {_label}", False, got=str(e))

# A closed window is never "free" — the only way through would be a paid
# template, and we never send one.
try:
    _shut = _free_lead(None, 30, notes=_Apt_free.FREEFORM_CLOSED_TAG)
    with _mp_free.object(_Apt_free, '_service_charge_date',
                         classmethod(lambda cls: _CHARGE_OFF)):
        results.log(
            "a closed window is never counted as free",
            _shut.messaging_is_free is False
            and _shut.messaging_cost_reason == 'window_closed',
            expected="free=False, reason=window_closed",
            got=f"free={_shut.messaging_is_free} reason={_shut.messaging_cost_reason}",
        )
except Exception as e:
    results.log("a closed window is never counted as free", False, got=str(e))

# The proactive gate: allowed AND free, unless paid sends are switched on.
try:
    _billable = _free_lead(100, 1)
    with _mp_free.object(_Apt_free, '_service_charge_date',
                         classmethod(lambda cls: _CHARGE_ON)):
        _blocked = _msp(_billable)
        from django.test import override_settings
        with override_settings(WHATSAPP_ALLOW_PAID_SENDS=True):
            _allowed = _msp(_billable)
    results.log(
        "proactive sends stop at billable, unless paid sends are enabled",
        _blocked is False and _allowed is True,
        expected="blocked by default, allowed with the setting on",
        got=f"default={_blocked} with_setting={_allowed}",
    )
except Exception as e:
    results.log("proactive sends stop at billable, unless paid sends are enabled",
                False, got=str(e))

# Default policy is FREE-ONLY: the setting must default off, or the guard is
# decorative.
try:
    results.log(
        "paid sends are off by default",
        _psa() is False,
        expected="False without the setting",
        got=str(_psa()),
    )
except Exception as e:
    results.log("paid sends are off by default", False, got=str(e))

# Non-Appointment objects have no cost model and must behave exactly as before,
# or the reminder commands would silently stop sending for unrelated objects.
try:
    class _NoCostModel:
        messaging_window_closes_at = None
        last_inbound_at = _NOW_FREE - _td_free(hours=1)
    results.log(
        "objects with no cost model fall back to the permission check",
        _msp(_NoCostModel()) is True,
        expected="True (unchanged behaviour)",
        got=str(_msp(_NoCostModel())),
    )
except Exception as e:
    results.log("objects with no cost model fall back to the permission check",
                False, got=str(e))

# Every PROACTIVE path must consult the gate. A new follow-up or reminder path
# that forgets it would quietly start spending.
try:
    import inspect as _insp_free
    from bot.management.commands import send_followups as _fu_free
    from bot.management.commands import send_reminders as _rem_free
    from bot.management.commands import send_job_reminders as _job_free
    _paths = {
        'send_followups': _insp_free.getsource(_fu_free),
        'send_reminders': _insp_free.getsource(_rem_free),
        'send_job_reminders': _insp_free.getsource(_job_free),
    }
    for _name, _src in _paths.items():
        results.log(
            f"{_name} gates its sends on cost, not just permission",
            'messaging_is_free' in _src or 'may_send_proactively' in _src,
            expected="uses the free-window gate",
            got="MISSING" if 'free' not in _src else "present",
        )
except Exception as e:
    results.log("proactive paths gate on cost", False, got=str(e))

# CTWA (Facebook/Instagram click-to-WhatsApp ad) follow-up cadence. An ad tap
# opens a 72h free-form window instead of 24h, so ad leads get SIX touches on
# absolute offsets from their last response — 4h, 8h, 20h (day 1), 32h, 48h
# (day 2), 66h (day 3) — while non-ad leads keep the tier cadence over 24h. The
# earlier schedule stopped at 48h and wasted the whole third day of the window.
# Pinned API-free with a stub lead.
from datetime import timedelta as _td
from django.utils import timezone as _tz
from bot.management.commands.send_followups import (
    Command as _FollowupCmd, CTWA_FOLLOWUP_OFFSETS as _CTWA_OFFS,
    max_followups_for as _max_fu_ctwa,
)
from bot.models import LeadStatus as _LS
import bot.management.commands.send_followups as _fu_mod
from datetime import datetime as _dt

# The cadence helpers read the wall clock, and every due moment is rolled into
# CONTACT_WINDOWS (08:21-20:53). Run against the REAL clock these cases pass by
# day and fail every night, because a touch due at 02:00 is pushed to the next
# window opening. Freeze the clock at a fixed in-window moment so the schedule
# is what is under test, not the hour the suite happens to run.
_FU_NOW = _fu_mod.SA_TIMEZONE.localize(_dt(2026, 8, 19, 14, 0))  # Wednesday


class _FrozenClock:
    """Stands in for the `timezone` module inside send_followups only."""
    def __init__(self, real, frozen):
        self._real, self._frozen = real, frozen

    def __getattr__(self, name):
        return getattr(self._real, name)

    def now(self):
        return self._frozen


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
        ref = _FU_NOW - _td(hours=hours_since_resp)
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
    ("CTWA FU3 before 20h", True, 2, 18.0, False),
    ("CTWA FU3 after 20h",  True, 2, 22.0, True),
    ("CTWA FU4 before 32h", True, 3, 30.0, False),
    ("CTWA FU4 after 32h",  True, 3, 34.0, True),
    ("CTWA FU5 before 48h", True, 4, 46.0, False),
    ("CTWA FU5 after 48h",  True, 4, 50.0, True),
    # The touch that only exists because the window is 72h: day three, the last
    # chance to reach an ad lead before free-form sending shuts off.
    ("CTWA FU6 before 66h", True, 5, 64.0, False),
    ("CTWA FU6 after 66h",  True, 5, 68.0, True),
    # Non-ad COLD lead must NOT use the 72h offsets: at 26h with 2 prior sends
    # it'd be "after 24h" under CTWA, but the tier path measures from the last
    # send (here = last response) with a 6h step, so it IS ready — proving the
    # branch only changes ad leads. The discriminating case is FU1 timing:
    ("non-CTWA FU1 before 4h", False, 0, 2.0, False),  # COLD tier[0]=4h
    ("non-CTWA FU1 after 4h",  False, 0, 6.0, True),
]
_real_tz = _fu_mod.timezone
try:
    _fu_mod.timezone = _FrozenClock(_real_tz, _FU_NOW)
    for label, ctwa, cnt, hrs, expected in _CTWA_CADENCE_CASES:
        try:
            got, _reason = _fu._is_ready_for_followup(_StubLead(ctwa, cnt, hrs), None, force=True)
            results.log(
                f"followup cadence: {label}",
                got == expected,
                f"ready={got}",
                expected=f"ready={expected}",
                got=f"ready={got} ({_reason})",
            )
        except Exception as e:
            results.log(f"followup cadence: {label}", False, got=str(e))
finally:
    _fu_mod.timezone = _real_tz

# Offsets themselves are the contract — pin them so a refactor can't silently
# change the schedule.
results.log(
    "followup cadence: CTWA offsets are (4, 8, 20, 32, 48, 66)",
    _CTWA_OFFS == (4, 8, 20, 32, 48, 66),
    f"offsets={_CTWA_OFFS}",
    expected="(4, 8, 20, 32, 48, 66)",
    got=str(_CTWA_OFFS),
)
# The 72h window must actually be worked: touches on all three days, and the
# last one late enough to use the final day without crowding the close.
results.log(
    "followup cadence: CTWA works all three days of the 72h window",
    (len([o for o in _CTWA_OFFS if o < 24]) >= 2 and
     any(24 <= o < 48 for o in _CTWA_OFFS) and
     any(48 <= o <= 70 for o in _CTWA_OFFS)),
    got=str(_CTWA_OFFS),
)
results.log(
    "followup cadence: an ad lead gets more touches than an organic one",
    _max_fu_ctwa(_StubLead(True, 0, 0.0)) > _max_fu_ctwa(_StubLead(False, 0, 0.0)),
    got=f"ctwa={_max_fu_ctwa(_StubLead(True, 0, 0.0))} "
        f"organic={_max_fu_ctwa(_StubLead(False, 0, 0.0))}",
)

# next_followup_due_at powers the UI "next follow-up" chip. It must agree with the
# cron's timing core and return None when the lead is not in the auto flow.
def _due(lead):
    return _fu.next_followup_due_at(lead)

# CTWA lead, no follow-ups yet → attempt 1, due ~4h after last response, ad flag set.
_info = _due(_StubLead(True, 0, 0.0))
results.log(
    "next_followup_due_at: CTWA FU1 attempt+flag",
    bool(_info) and _info['attempt'] == 1 and _info['max'] == 6 and _info['is_ctwa'] is True,
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
results.log("next_followup_due_at: an ad lead is NOT retired at 4 — the 72h window has more",
            _due(_StubLead(True, 4, 0.0)) is not None, got=str(_due(_StubLead(True, 4, 0.0))))
results.log("next_followup_due_at: None when count>=max",
            _due(_StubLead(True, 6, 0.0)) is None, got=str(_due(_StubLead(True, 6, 0.0))))
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

# Every threading.Thread(target=delayed_response, ...) call site MUST thread
# tenant, or the send silently falls back to the env/homebase client — the
# exact bug that had tenant jd3's replies going out on homebase's number.
# Structural (AST) check over the source so a new call site missing the kwarg
# fails the gate immediately, without needing to reproduce a live send.
try:
    import ast as _ast
    import inspect as _inspect

    import bot.whatsapp_webhook as _wh
    _src = _inspect.getsource(_wh)
    _tree = _ast.parse(_src)
    _bad_sites = []
    for _node in _ast.walk(_tree):
        if not (isinstance(_node, _ast.Call)
                and isinstance(_node.func, _ast.Attribute)
                and _node.func.attr == 'Thread'):
            continue
        _target_kw = next((kw for kw in _node.keywords if kw.arg == 'target'), None)
        if not (_target_kw and isinstance(_target_kw.value, _ast.Name)
                and _target_kw.value.id == 'delayed_response'):
            continue
        # tenant lives inside the kwargs={...} dict literal, not as a direct
        # keyword on Thread() itself — check that dict's own keys.
        _kwargs_kw = next((kw for kw in _node.keywords if kw.arg == 'kwargs'), None)
        _has_tenant = bool(
            _kwargs_kw and isinstance(_kwargs_kw.value, _ast.Dict)
            and any(isinstance(k, _ast.Constant) and k.value == 'tenant'
                   for k in _kwargs_kw.value.keys)
        )
        if not _has_tenant:
            _bad_sites.append(_node.lineno)
    results.log(
        "delayed_response: every threading.Thread call site threads tenant",
        not _bad_sites,
        got=f"missing at line(s): {_bad_sites}" if _bad_sites else "all call sites OK",
    )
except Exception as e:
    results.log("delayed_response tenant threading check", False, got=str(e))

# Reply pacing mirrors the lead's own tempo: a lead who came back inside 5 min
# gets an answer 1-2 min after the batch window; a slower lead gets 5 min. The
# delay is picked AFTER the batch window has elapsed, so these sit on top of it.
try:
    import bot.whatsapp_webhook as _wh_pace

    _pace_sender = '263771000999'
    _orig_latency = dict(_wh_pace._lead_reply_latency)
    try:
        _wh_pace._lead_reply_latency[_pace_sender] = 150  # replied in 2.5 min
        _wh_pace._fast_reply_turn.pop(_pace_sender, None)
        _fast = [_wh_pace.get_random_delay(sender=_pace_sender) for _ in range(6)]
        results.log(
            "reply pacing: fast lead alternates 1, 2, 1, 2 min (not random)",
            _fast == [60, 120, 60, 120, 60, 120],
            got=f"delays={_fast}",
        )
        # Each lead keeps its own beat — one lead's turn must not shunt another's.
        _other = '263771000997'
        _wh_pace._lead_reply_latency[_other] = 150
        _wh_pace._fast_reply_turn.pop(_other, None)
        results.log(
            "reply pacing: the 1/2 alternation is tracked per lead",
            _wh_pace.get_random_delay(sender=_other) == 60
            and _wh_pace.get_random_delay(sender=_pace_sender) == 60,
            got=f"other={_wh_pace._fast_reply_turn.get(_other)} main={_wh_pace._fast_reply_turn.get(_pace_sender)}",
        )
        _wh_pace._lead_reply_latency.pop(_other, None)
        _wh_pace._fast_reply_turn.pop(_other, None)

        # Under a minute: they are in the chat right now, so the batch window is
        # the entire wait — no added delay on top.
        for _instant in (0, 5, 30, 59):
            _wh_pace._lead_reply_latency[_pace_sender] = _instant
            results.log(
                f"reply pacing: {_instant}s reply = batch window only, no added delay",
                _wh_pace.get_random_delay(sender=_pace_sender) == 0,
                got=str(_wh_pace.get_random_delay(sender=_pace_sender)),
            )
        # 60s exactly is no longer instant — it starts the 1/2 alternating band.
        _wh_pace._lead_reply_latency[_pace_sender] = 60
        _wh_pace._fast_reply_turn.pop(_pace_sender, None)
        results.log(
            "reply pacing: the 1-minute boundary leaves the instant band",
            _wh_pace.get_random_delay(sender=_pace_sender) == 60,
            got=str(_wh_pace.get_random_delay(sender=_pace_sender)),
        )

        _wh_pace._lead_reply_latency[_pace_sender] = 40 * 60  # replied after 40 min
        _slow = {_wh_pace.get_random_delay(sender=_pace_sender) for _ in range(10)}
        results.log(
            "reply pacing: slow lead (>5 min) gets batch window + 5 min",
            _slow == {300},
            got=f"delays={sorted(_slow)}",
        )

        # Exactly 5 min counts as slow — the boundary must not fall into fast.
        _wh_pace._lead_reply_latency[_pace_sender] = 5 * 60
        results.log(
            "reply pacing: the 5-minute boundary is slow, not fast",
            _wh_pace.get_random_delay(sender=_pace_sender) == 300,
            got=str(_wh_pace.get_random_delay(sender=_pace_sender)),
        )

        # No recorded latency (first contact) falls back to the old 1-5 min.
        _wh_pace._lead_reply_latency.pop(_pace_sender, None)
        _unknown = {_wh_pace.get_random_delay(sender=_pace_sender) for _ in range(40)}
        results.log(
            "reply pacing: unknown latency falls back to 1-5 min",
            _unknown and all(60 <= d <= 300 for d in _unknown),
            got=f"delays={sorted(_unknown)}",
        )
    finally:
        _wh_pace._lead_reply_latency.clear()
        _wh_pace._lead_reply_latency.update(_orig_latency)
except Exception as e:
    results.log("reply pacing by lead latency", False, got=str(e))

# _record_lead_reply_latency reads the gap off conversation_history. The opening
# message of a conversation has no gap to measure — it must still pace fast, not
# fall back to the old up-to-5-minute wait.
try:
    import datetime as _dt
    import types as _types_p

    import bot.whatsapp_webhook as _wh_rec

    def _pace_appt(history):
        return _types_p.SimpleNamespace(conversation_history=history)

    _rec_sender = '263771000998'
    _orig_rec = dict(_wh_rec._lead_reply_latency)

    def _latency_for(history):
        _wh_rec._lead_reply_latency.pop(_rec_sender, None)
        _wh_rec._record_lead_reply_latency(_rec_sender, _pace_appt(history))
        return _wh_rec._lead_reply_latency.get(_rec_sender)

    try:
        # Conversation opener: no assistant turn anywhere in history.
        _opener = _latency_for([{'role': 'user', 'content': 'hi',
                                 'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat()}])
        results.log(
            "reply pacing: conversation opener paces fast, never instant",
            _opener == _wh_rec.OPENER_LATENCY_SECONDS
            and _wh_rec.get_random_delay(sender=_rec_sender) in (60, 120),
            got=f"latency={_opener}",
        )
        # Empty history (brand-new appointment) is the same case.
        results.log(
            "reply pacing: empty history paces fast",
            _latency_for([]) == _wh_rec.OPENER_LATENCY_SECONDS,
            got=str(_latency_for([])),
        )
        # A real gap since our last message is measured, not assumed.
        _then = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=30)
        _measured = _latency_for([
            {'role': 'assistant', 'content': 'When suits you?', 'timestamp': _then.isoformat()},
            {'role': 'user', 'content': 'sorry, was busy'},
        ])
        results.log(
            "reply pacing: measures the gap since our last message",
            _measured is not None and 1750 < _measured < 1850
            and _wh_rec.get_random_delay(sender=_rec_sender) == 300,
            got=f"latency={_measured}",
        )
        # PROD BUG (conv 678, 8 Aug): assistant turns are logged when GENERATED
        # but sent after their own delay, so measuring from `timestamp` charged
        # our 5-min delay to the lead. A lead who came back 54s after seeing the
        # message was scored 354s and paced slow. Measure from sent_at.
        _gen = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=354)
        _sent = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=54)
        _real = _latency_for([
            {'role': 'assistant', 'content': 'Can you tell me more?',
             'timestamp': _gen.isoformat(), 'sent_at': _sent.isoformat()},
        ])
        results.log(
            "reply pacing: measures from send time, not generation time (conv 678)",
            _real is not None and _real < 90
            and _wh_rec.get_random_delay(sender=_rec_sender) == 0,
            got=f"latency={_real} (must be ~54s, not ~354s)",
        )
        # A reply logged but never sent (cancelled mid-wait) is skipped — the
        # lead is answering the last message that actually reached them.
        _skip = _latency_for([
            {'role': 'assistant', 'content': 'sent one',
             'timestamp': _gen.isoformat(), 'sent_at': _sent.isoformat()},
            {'role': 'user', 'content': 'wait'},
            {'role': 'assistant', 'content': 'never sent',
             'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat()},
        ])
        results.log(
            "reply pacing: skips a reply that was logged but never sent",
            _skip is not None and 40 < _skip < 90,
            got=f"latency={_skip}",
        )
        # We spoke but the timestamp is unusable — fall back, don't guess fast.
        results.log(
            "reply pacing: unusable timestamp falls back rather than guessing",
            _latency_for([{'role': 'assistant', 'content': 'hi', 'timestamp': 'not-a-date'}]) is None,
            got=str(_latency_for([{'role': 'assistant', 'content': 'hi', 'timestamp': 'not-a-date'}])),
        )
    finally:
        _wh_rec._lead_reply_latency.clear()
        _wh_rec._lead_reply_latency.update(_orig_rec)
except Exception as e:
    results.log("reply pacing latency recorder", False, got=str(e))

# Pacing only works if the sender reaches get_random_delay — a new call site that
# forgets it silently reverts that lead to the old random 1-5 min.
try:
    import ast as _ast_d
    import inspect as _inspect_d

    import bot.whatsapp_webhook as _wh_d
    _tree_d = _ast_d.parse(_inspect_d.getsource(_wh_d))
    _no_sender = [
        n.lineno for n in _ast_d.walk(_tree_d)
        if isinstance(n, _ast_d.Call)
        and isinstance(n.func, _ast_d.Name)
        and n.func.id == 'get_random_delay'
        and not any(kw.arg == 'sender' for kw in n.keywords)
    ]
    results.log(
        "get_random_delay: every call site passes sender",
        not _no_sender,
        got=f"missing at line(s): {_no_sender}" if _no_sender else "all call sites OK",
    )
except Exception as e:
    results.log("get_random_delay sender threading check", False, got=str(e))

# Half-hour opening times must survive every hours rendering (Harare Plumbing
# Solutions opens 07:30 — the short/tiny clocks used to drop the minutes and
# state "7am", a wrong business fact). Homebase's 08:00 hid this.
try:
    from bot.tenant_config import TenantConfig as _TC

    class _FakeHoursProfile:
        business_hours = {'days': 'Monday-Sunday', 'open': '07:30',
                          'close': '18:00', 'closed': []}

    _hcfg = _TC()
    _hcfg._profile = _FakeHoursProfile()
    _hcfg._profile_loaded = True
    _rendered = (_hcfg.hours_sentence(), _hcfg.hours_medium(), _hcfg.hours_compact())
    results.log(
        "hours rendering: 07:30 keeps its minutes in every format",
        _rendered == ('Monday to Sunday, 7:30 AM – 6:00 PM',
                      'Monday–Sunday, 7:30 AM–6 PM',
                      'Mon–Sun 7:30am–6pm'),
        got=str(_rendered),
    )
except Exception as e:
    results.log("hours rendering: half-hour opening time", False, got=str(e))

# ── Closed days are the TENANT's, never hardcoded Saturday ───────────────────
# Production (Barmak, 2026-08-21, lead on +27610318200): the lead picked one of
# the slots we had just offered — "Sunday at 12" — and got back "We unfortunately
# don't operate on Saturdays. Our working hours are Monday to Sunday, 8:00 AM –
# 8:00 PM." Two bugs in one line: the closed-day copy was hardcoded to Homebase's
# Saturday while the hours line was tenant-aware (so the reply contradicted
# itself), and the branch fired on ANY booking failure with no alternatives.
try:
    from bot.tenant_config import TenantConfig as _TCd
    from bot.views.plumbot.response_mixin import _named_closed_day as _ncd

    def _cfg_for(closed, days='Monday-Sunday', open_='08:00', close='20:00'):
        class _P:
            business_hours = {'days': days, 'open': open_, 'close': close, 'closed': closed}
        c = _TCd()
        c._profile = _P()
        c._profile_loaded = True
        return c

    class _FakeMixinDays:
        def __init__(self, cfg):
            self.tenant_cfg = cfg

    _seven_day = _FakeMixinDays(_cfg_for([]))            # Barmak: open all week
    _sat_closed = _FakeMixinDays(_cfg_for(['sat'], days='Sunday-Friday', close='18:00'))
    _weekend_closed = _FakeMixinDays(_cfg_for(['sat', 'sun'], days='Monday-Friday'))

    CLOSED_DAY_CASES = [
        # (mixin, message, expected closed-day name or None)
        (_seven_day,      "Sunday at 12",            None),   # the bug
        (_seven_day,      "Saturday at 12",          None),   # tenant works Saturdays
        (_seven_day,      "Yes\nSunday at 12",       None),   # batched reply
        (_sat_closed,     "Sunday at 12",            None),   # Sunday IS a working day
        (_sat_closed,     "Saturday at 12",          'Saturday'),
        (_sat_closed,     "can you come sat?",       'Saturday'),
        (_sat_closed,     "I'm satisfied with that", None),   # 'sat' inside a word
        (_sat_closed,     "we can do that on mugovera", 'Saturday'),
        (_weekend_closed, "Sunday at 12",            'Sunday'),
        (_weekend_closed, "Tuesday morning",         None),
    ]
    for _mx, _msg, _expected in CLOSED_DAY_CASES:
        _got = _ncd(_mx, _msg)
        results.log(
            f"_named_closed_day (closed={sorted(_mx.tenant_cfg.closed_weekdays())}): '{_msg[:24]}'",
            _got == _expected, expected=str(_expected), got=str(_got),
        )

    # The config layer itself: a tenant open all week has NO closed days and no
    # closed-day phrase to put in copy.
    results.log("closed_weekdays: open all week → empty",
                _seven_day.tenant_cfg.closed_weekdays() == frozenset(),
                got=str(_seven_day.tenant_cfg.closed_weekdays()))
    results.log("closed_days_phrase: open all week → '' (no Saturday copy)",
                _seven_day.tenant_cfg.closed_days_phrase() == '',
                got=repr(_seven_day.tenant_cfg.closed_days_phrase()))
    results.log("closed_days_phrase: two closed days reads naturally",
                _weekend_closed.tenant_cfg.closed_days_phrase() == 'Saturdays or Sundays',
                got=repr(_weekend_closed.tenant_cfg.closed_days_phrase()))
    results.log("is_open_on: seven-day tenant is open on Saturday",
                _seven_day.tenant_cfg.is_open_on(5) is True,
                got=str(_seven_day.tenant_cfg.is_open_on(5)))
    # Booking slots follow the tenant's own window, not a hardcoded 8–18.
    results.log("booking_hours: 08:00–20:00 tenant offers a 6 PM slot",
                _seven_day.tenant_cfg.booking_hours() == [8, 10, 12, 14, 16, 18],
                got=str(_seven_day.tenant_cfg.booking_hours()))
    results.log("booking_hours: 08:00–18:00 tenant stops at 4 PM",
                _sat_closed.tenant_cfg.booking_hours() == [8, 10, 12, 14, 16],
                got=str(_sat_closed.tenant_cfg.booking_hours()))
    # No hours on file → the legacy Homebase week, so untouched tenants behave
    # exactly as before this change.
    _bare = _TCd()
    results.log("no profile → legacy week (closed Sat, 8–18)",
                _bare.closed_weekdays() == frozenset({5}) and
                (_bare.open_hour(), _bare.close_hour()) == (8, 18),
                got=f"{_bare.closed_weekdays()} {_bare.open_hour()}-{_bare.close_hour()}")
except Exception as e:
    results.log("closed days are tenant-scoped", False, got=str(e))

# ── 24/7 emergency cover is a per-tenant opt-in on top of the regular week ──
# A tenant that answers callouts round the clock ticks it on the profile; it
# rides on the same business_hours JSON. It must show up in the hours copy and
# in the "that time doesn't work" replies, and must NEVER appear for a tenant
# without it on file (no borrowing another tenant's promise).
try:
    from bot.tenant_config import TenantConfig as _TCe
    from bot.views.plumbot.response_mixin import (
        _emergency_fact as _ef, _hours_clause as _hc, _quick_hours as _qh,
        _working_hours_line as _whl,
    )
    from bot.views.platform import _compose_business_hours as _cbh

    def _ecfg(**extra):
        class _P:
            business_hours = dict(
                {'days': 'Monday-Friday', 'open': '08:00', 'close': '17:00',
                 'closed': ['sat', 'sun']}, **extra)
            faq_facts = {'hours': 'We are open Monday to Friday, 8 AM to 5 PM.'}
            licensed_claim_enabled = False
        c = _TCe()
        c._profile = _P()
        c._profile_loaded = True
        return c

    class _FakeEmergencyMixin:
        def __init__(self, cfg):
            self.tenant_cfg = cfg

    _emerg = _FakeEmergencyMixin(_ecfg(emergency_24h=True))
    _plain = _FakeEmergencyMixin(_ecfg())

    results.log("emergency_24h: reads the flag off business_hours",
                _emerg.tenant_cfg.emergency_24h() is True and
                _plain.tenant_cfg.emergency_24h() is False,
                got=f"{_emerg.tenant_cfg.emergency_24h()} / {_plain.tenant_cfg.emergency_24h()}")
    results.log("no flag → no 24/7 claim anywhere in the hours copy",
                all('24/7' not in text for text in
                    (_whl(_plain), _hc(_plain), _qh(_plain), _ef(_plain))),
                got=repr((_whl(_plain), _hc(_plain), _qh(_plain), _ef(_plain))))
    results.log("flag → working-hours line still states the real week, plus 24/7",
                'Monday to Friday' in _whl(_emerg) and '24/7' in _whl(_emerg),
                got=repr(_whl(_emerg)))
    results.log("flag → quick hours keeps the clock and adds the cover",
                _qh(_emerg).startswith("We're open Monday to Friday") and
                '24/7' in _qh(_emerg),
                got=repr(_qh(_emerg)))
    results.log("flag → the LLM prompt gets an emergency bullet",
                '24/7' in _ef(_emerg) and _ef(_plain) == '',
                got=repr(_ef(_emerg)))
    # The typed hours FAQ fact gains the cover once, never twice.
    _already = _ecfg(emergency_24h=True)
    _already._profile.faq_facts = {'hours': 'Mon-Fri 8-5, and 24/7 for emergencies.'}
    results.log("hours FAQ fact: cover appended once, not duplicated",
                _emerg.tenant_cfg.faq_fact('hours').count('24/7') == 1 and
                _already.faq_fact('hours').count('24/7') == 1,
                got=f"{_emerg.tenant_cfg.faq_fact('hours')!r} | {_already.faq_fact('hours')!r}")
    # The editor round-trip: the tick survives, and stands alone when the week
    # was left blank.
    _composed = _cbh({'days': ['monday', 'tuesday'], 'open': '08:00',
                      'close': '17:00', 'emergency_24h': True})
    results.log("_compose_business_hours: tick rides along with the week",
                _composed.get('emergency_24h') is True and _composed['open'] == '08:00',
                got=str(_composed))
    results.log("_compose_business_hours: tick alone survives an empty week",
                _cbh({'days': [], 'open': '', 'close': '', 'emergency_24h': True})
                == {'emergency_24h': True},
                got=str(_cbh({'days': [], 'open': '', 'close': '', 'emergency_24h': True})))
    results.log("_compose_business_hours: nothing filled in stays None",
                _cbh({'days': [], 'open': '', 'close': ''}) is None,
                got=str(_cbh({'days': [], 'open': '', 'close': ''})))
except Exception as e:
    results.log("24/7 emergency cover", False, got=str(e))

# The deterministic availability backfill must not drop a day the tenant works.
# Same root cause: it hardcoded "Saturday → None".
try:
    from bot.whatsapp_webhook import _keyword_availability_date as _kad
    _sat_only = frozenset({5})
    _none_closed = frozenset()
    results.log("_keyword_availability_date: Saturday kept for a 7-day tenant",
                _kad("saturday works", _none_closed) is not None,
                got=str(_kad("saturday works", _none_closed)))
    results.log("_keyword_availability_date: Saturday dropped when closed then",
                _kad("saturday works", _sat_only) is None,
                got=str(_kad("saturday works", _sat_only)))
    results.log("_keyword_availability_date: Sunday always resolves for a 7-day tenant",
                _kad("sunday at 12", _none_closed) is not None,
                got=str(_kad("sunday at 12", _none_closed)))
except Exception as e:
    results.log("_keyword_availability_date closed-day handling", False, got=str(e))

# ── Follow-up cadence: at least 4, spread across the messaging window ────────
# Every lead gets a minimum of four touches, and the spacing is derived from the
# lead's own WhatsApp free-form window (24h standard, 72h CTWA) rather than a
# fixed hour count. The old fixed cadence (COLD: 4+6+6+6 = 22h, plus up to ~1h
# of jitter per step) could push the fourth touch past the 24h close, where a
# free-form send bounces with 131047 — so the lead silently got three.
try:
    from bot.management.commands.send_followups import (
        Command as _FuCmd2, max_followups_for as _max_fu,
        FOLLOWUP_MIN_COUNT as _FU_MIN,
        FOLLOWUP_WINDOW_MARGIN_HOURS as _FU_MARGIN,
        FOLLOWUP_MIN_GAP_HOURS as _FU_GAP,
    )
    from datetime import timedelta as _td2
    from django.utils import timezone as _tz2
    from bot.models import LeadStatus as _LS2

    class _WindowLead:
        """Duck-typed lead carrying a real messaging window (no DB)."""
        CTWA_WINDOW_HOURS = 72

        def __init__(self, status=_LS2.COLD, ctwa=False, count=0, hours_ago=0.0,
                     last_followup_hours_ago=None, entry_hours_ago=None):
            self.id = 4242
            self.lead_status = status
            self.followup_count = count
            # Fixed reference, not the wall clock: reading now() here and again
            # inside _followup_offsets left the window a few ms short of 72h, so
            # the proportional scaling fired and the tuned offsets came back as
            # 31.9/47.9/65.9. Latent flake — it passed or failed on timing.
            ref = _FU_NOW - _td2(hours=hours_ago)
            self.last_customer_response = ref
            self.last_inbound_at = ref
            self.created_at = ref
            self.last_followup_sent = (
                None if last_followup_hours_ago is None
                else _tz2.now() - _td2(hours=last_followup_hours_ago)
            )
            self.ctwa_entry_at = (
                None if not ctwa
                else (ref if entry_hours_ago is None
                      else _FU_NOW - _td2(hours=entry_hours_ago))
            )
            self.is_lead_active = True
            self.status = 'pending'
            self.followup_stage = None

        @property
        def messaging_window_closes_at(self):
            closes = [self.last_inbound_at + _td2(hours=24)]
            if self.ctwa_entry_at:
                closes.append(self.ctwa_entry_at + _td2(hours=self.CTWA_WINDOW_HOURS))
            return max(closes)

    _fu2 = _FuCmd2()
    # Freeze the module clock for the whole block so the schedule under test is
    # the schedule, not the millisecond the suite happened to run.
    _real_tz2 = _fu_mod.timezone
    _fu_mod.timezone = _FrozenClock(_real_tz2, _FU_NOW)

    results.log("followup minimum: FOLLOWUP_MIN_COUNT is 4", _FU_MIN == 4, got=str(_FU_MIN))

    # Every tier gets at least four attempts, and the whole schedule fits the window.
    for _st in (_LS2.VERY_HOT, _LS2.HOT, _LS2.WARM, _LS2.COLD):
        _lead = _WindowLead(status=_st)
        _offs = _fu2._followup_offsets(_lead)
        _win = _fu2._messaging_window_hours(_lead)
        results.log(f"followup cadence [{_st}]: at least 4 touches",
                    len(_offs) >= 4 and _max_fu(_lead) >= 4,
                    got=f"{len(_offs)} offsets, max={_max_fu(_lead)}")
        results.log(f"followup cadence [{_st}]: strictly increasing",
                    all(b > a for a, b in zip(_offs, _offs[1:])),
                    got=str([round(o, 1) for o in _offs]))
        # +1h covers the worst-case jitter on the final touch.
        results.log(f"followup cadence [{_st}]: last touch lands inside the 24h window",
                    _offs[-1] + 1.0 <= _win - 0.5,
                    expected=f"<= {_win - 0.5:.1f}h", got=f"{_offs[-1]:.1f}h")
        results.log(f"followup cadence [{_st}]: first touch is not instant",
                    _offs[0] >= 1.0, got=f"{_offs[0]:.1f}h")

    # Hotter leads are chased sooner than colder ones.
    _hot_offs = _fu2._followup_offsets(_WindowLead(status=_LS2.VERY_HOT))
    _cold_offs = _fu2._followup_offsets(_WindowLead(status=_LS2.COLD))
    results.log("followup cadence: very hot is chased sooner than cold",
                all(h < c for h, c in zip(_hot_offs, _cold_offs)),
                got=f"hot={[round(o,1) for o in _hot_offs]} cold={[round(o,1) for o in _cold_offs]}")

    # A 72h CTWA window spreads wider than a 24h one — the spacing follows the
    # window, which is the whole point.
    _ctwa_offs = _fu2._followup_offsets(_WindowLead(ctwa=True))
    results.log("followup cadence: a 72h window spreads further than a 24h one",
                _ctwa_offs[-1] > _cold_offs[-1] * 2,
                got=f"ctwa={[round(o,1) for o in _ctwa_offs]}")
    results.log("followup cadence: CTWA keeps its tuned band offsets on a full window",
                tuple(round(o, 1) for o in _ctwa_offs) == (4.0, 8.0, 20.0, 32.0, 48.0, 66.0),
                got=str([round(o, 1) for o in _ctwa_offs]))
    results.log("followup cadence: the 72h window earns extra touches (6 vs 4)",
                len(_ctwa_offs) == 6 and _max_fu(_WindowLead(ctwa=True)) == 6,
                got=f"{len(_ctwa_offs)} offsets")
    results.log("followup cadence: the last CTWA touch still clears the 72h close",
                _ctwa_offs[-1] + 1.0 <= 72 - 1.5,
                got=f"{_ctwa_offs[-1]:.1f}h")
    # A CTWA lead whose last message left less than the full 72h ahead gets the
    # same shape, squeezed — never a schedule that runs past the close.
    # Ad tapped 60h ago, lead last messaged 30h ago → only ~12h of the ad window
    # is left ahead of us, so the six touches must compress into what remains.
    _short = _WindowLead(ctwa=True, hours_ago=30.0, entry_hours_ago=60.0)
    _short_offs = _fu2._followup_offsets(_short)
    _short_win = _fu2._messaging_window_hours(_short)
    results.log("followup cadence: a partly-spent ad window is scaled, not overrun",
                len(_short_offs) == 6 and _short_offs[-1] <= _short_win - 1.0,
                got=f"window={_short_win:.1f}h offsets={[round(o,1) for o in _short_offs]}")

    # A per-status override below four is floored back up to four.
    class _OddStatus:
        lead_status = 'made_up_status'
    results.log("followup minimum: unknown status still gets 4",
                _max_fu(_OddStatus()) == 4, got=str(_max_fu(_OddStatus())))

    # Offsets are ABSOLUTE from the window opening, so a late attempt never
    # pushes the rest past the close (the old per-send reference drifted).
    _late = _WindowLead(status=_LS2.COLD, count=1, hours_ago=12.0,
                        last_followup_hours_ago=9.0)
    _idx, _wait, _ref = _fu2._followup_wait_and_reference(_late)
    results.log("followup cadence: reference is the window start, not the last send",
                _ref == _late.last_customer_response, got=str(_ref))
    results.log("followup cadence: attempt 2 sits at its window position, so a "
                "12h-old lead is due now",
                _wait < 12.0, got=f"wait={_wait:.1f}h")

    # ── Sending hours: nothing is left stranded for the lead's next message ──
    # Production complaint: a follow-up that came due during the nightly quiet
    # hours sat unsent — and by the time we could send again the 24h messaging
    # window had closed, so it went out only once the lead messaged again,
    # landing as a stale "just checking in" on top of their live message. The
    # schedule is now reconciled with the hours we can actually send in, and a
    # touch that cannot survive the night is brought forward instead.
    # Frozen clock so the roll-forward/pull-back maths is deterministic.
    import datetime as _dt_fz
    from unittest import mock as _mock_fz
    import pytz as _pytz_fz
    from bot.management.commands.send_followups import (
        LAST_CALL_GRACE_MINUTES as _LC_GRACE,
        LAST_CALL_MIN_GAP_HOURS as _LC_GAP,
        FOLLOWUP_LIVE_CONVERSATION_MINUTES as _LIVE_MIN,
        FOLLOWUP_QUIET_AFTER_OUTBOUND_HOURS as _QUIET_OUT,
    )
    _sast_fz = _pytz_fz.timezone('Africa/Johannesburg')

    def _at(h, m=0, day=23):
        return _sast_fz.localize(_dt_fz.datetime(2026, 6, day, h, m))

    class _ClockLead:
        """Lead with explicit timestamps and a real 24h window (no DB, no now())."""
        def __init__(self, last_msg, count=0, last_followup=None, last_outbound=None,
                     status=_LS2.COLD):
            self.id = 4242
            self.lead_status = status
            self.followup_count = count
            self.last_customer_response = last_msg
            self.last_inbound_at = last_msg
            self.created_at = last_msg
            self.last_followup_sent = last_followup
            self.last_outbound_at = last_outbound
            self.ctwa_entry_at = None
            self._now = None

        @property
        def messaging_window_closes_at(self):
            return self.last_inbound_at + _td2(hours=24)

        @property
        def messaging_window_open(self):
            return self.messaging_window_closes_at > (self._now or _tz2.now())

    def _frozen(now_dt):
        return _mock_fz.patch(
            'bot.management.commands.send_followups.timezone.now',
            side_effect=lambda: now_dt,
        )

    # The mirror of _next_window_open: the last minute we may still send.
    results.log("sending hours: 06:00 rolls BACK to the previous evening 20:52",
                _fu2._window_moment_before(_at(6, 0)).strftime('%d %H:%M') == '22 20:52',
                got=str(_fu2._window_moment_before(_at(6, 0))))
    results.log("sending hours: a midday deadline stays where it is",
                _fu2._window_moment_before(_at(12, 0)).strftime('%d %H:%M') == '23 12:00',
                got=str(_fu2._window_moment_before(_at(12, 0))))
    results.log("sending hours: 22:30 rolls back to the same evening's 20:52",
                _fu2._window_moment_before(_at(22, 30)).strftime('%d %H:%M') == '23 20:52',
                got=str(_fu2._window_moment_before(_at(22, 30))))

    # A lead who wrote at 09:00: the last touch would naturally land ~04:00, in
    # the quiet hours, and the window shuts at 09:00 before the next opening.
    # It must be scheduled for the evening BEFORE, not left for 08:21.
    _stranded = _ClockLead(_at(9, 0, day=22), count=3,
                           last_followup=_at(17, 0, day=22))
    with _frozen(_at(19, 0, day=22)):
        _stranded._now = _at(19, 0, day=22)
        _due_fz = _fu2._scheduled_due_at(_stranded)
        _deadline = _fu2._last_sendable_moment(_stranded)
    results.log("sending hours: the last touch is pulled back before the window shuts",
                _due_fz is not None and _due_fz <= _at(20, 53, day=22),
                expected="on the 22nd, before 20:53",
                got=str(_due_fz.astimezone(_sast_fz)) if _due_fz else 'None')
    results.log("sending hours: the pull-back leaves the cron room to catch it",
                _deadline is not None and _due_fz <= _deadline - _td2(minutes=_LC_GRACE - 1),
                got=f"due={_due_fz} deadline={_deadline}")

    # In that final stretch the spacing rule relaxes — a touch that must go now
    # or never is worth a tighter gap than one with a day of window ahead.
    with _frozen(_at(20, 30, day=22)):
        _stranded._now = _at(20, 30, day=22)
        results.log("sending hours: the final stretch counts as a last call",
                    _fu2._is_last_call(_stranded) is True)
        results.log("sending hours: last call relaxes the spacing rule",
                    _fu2._min_gap_hours(_stranded) == _LC_GAP,
                    got=str(_fu2._min_gap_hours(_stranded)))
    _roomy = _ClockLead(_at(9, 0, day=23), count=1, last_followup=_at(12, 0, day=23))
    with _frozen(_at(13, 0, day=23)):
        _roomy._now = _at(13, 0, day=23)
        results.log("sending hours: mid-window is NOT a last call",
                    _fu2._is_last_call(_roomy) is False)
        results.log("sending hours: normal spacing applies mid-window",
                    _fu2._min_gap_hours(_roomy) == _FU_GAP,
                    got=str(_fu2._min_gap_hours(_roomy)))
        # Two touches never fire back to back: a touch whose own slot has
        # passed is still held until the gap since the last send has elapsed.
        _crowded = _ClockLead(_at(9, 0, day=22), count=1,
                              last_followup=_at(12, 40, day=23))
        _crowded._now = _at(13, 0, day=23)
        _ready, _why = _fu2._is_ready_for_followup(_crowded, None, force=True)
        _crowded_due = _fu2._scheduled_due_at(_crowded)
        results.log("followup cadence: min gap blocks back-to-back sends",
                    _ready is False and
                    _crowded_due >= _at(12, 40, day=23) + _td2(hours=_FU_GAP),
                    expected=f"held until at least {_FU_GAP}h after the last send",
                    got=f"ready={_ready} due={_crowded_due} ({_why})")
        # ...and one that is properly spaced does fire.
        _spaced = _ClockLead(_at(9, 0, day=22), count=1,
                             last_followup=_at(10, 0, day=23))
        _spaced._now = _at(13, 0, day=23)
        results.log("followup cadence: a properly spaced attempt still fires",
                    _fu2._is_ready_for_followup(_spaced, None, force=True)[0] is True,
                    got=str(_fu2._is_ready_for_followup(_spaced, None, force=True)))

        # A follow-up never lands on top of a live exchange: not right after the
        # lead's own message, and not right after ours. This is what made the
        # stranded touch read as redundant when it finally went out.
        # (Forced overdue, so the guard is what's under test — not the clock.)
        _live = _ClockLead(_at(12, 55, day=23), count=2,
                           last_followup=_at(9, 0, day=23))
        _live._now = _at(13, 0, day=23)
        with _mock_fz.patch.object(_fu2, '_scheduled_due_at',
                                   return_value=_at(9, 30, day=23)):
            _ready_live, _why_live = _fu2._is_ready_for_followup(_live, None, force=True)
        results.log("sending hours: no follow-up while the lead is mid-conversation",
                    _ready_live is False and 'live' in _why_live,
                    got=f"ready={_ready_live} ({_why_live})")
        _just_replied = _ClockLead(_at(9, 0, day=22), count=2,
                                   last_followup=_at(8, 30, day=23),
                                   last_outbound=_at(12, 50, day=23))
        _just_replied._now = _at(13, 0, day=23)
        with _mock_fz.patch.object(_fu2, '_scheduled_due_at',
                                   return_value=_at(9, 30, day=23)):
            _ready_out, _why_out = _fu2._is_ready_for_followup(
                _just_replied, None, force=True)
        results.log("sending hours: no follow-up right after our own message",
                    _ready_out is False and 'we last messaged' in _why_out,
                    got=f"ready={_ready_out} ({_why_out})")
    results.log("sending hours: the live-conversation guard is minutes, not seconds",
                _LIVE_MIN >= 15 and _QUIET_OUT >= 1.0,
                got=f"live={_LIVE_MIN}min quiet_after_outbound={_QUIET_OUT}h")

    # The whole schedule is planned against sendable hours, so a lead whose
    # window tail falls in the quiet hours still gets every touch.
    _tight = _ClockLead(_at(7, 0, day=23))
    with _frozen(_at(7, 0, day=23)):
        _tight._now = _at(7, 0, day=23)
        _tight_offs = _fu2._followup_offsets(_tight)
        _tight_sendable = _fu2._sendable_hours(_tight)
    results.log("sending hours: the schedule fits the SENDABLE span, not just the window",
                _tight_offs[-1] <= _tight_sendable,
                expected=f"last offset <= {_tight_sendable:.1f}h of sendable time",
                got=str([round(o, 1) for o in _tight_offs]))

    # Delay-flow and parked nudges follow the same window rule and also do 4.
    _dl = _fu2._delay_nudge_offsets(_WindowLead())
    results.log("delay nudges: 4 touches, all inside the window",
                len(_dl) >= 4 and _dl[-1] <= 24 - _FU_MARGIN,
                got=str([round(o, 1) for o in _dl]))
    _pk = _fu2._parked_nudge_offsets(_WindowLead())
    results.log("parked nudges: 4 touches, all inside the window",
                len(_pk) >= 4 and _pk[-1] <= 24 - _FU_MARGIN,
                got=str([round(o, 1) for o in _pk]))
    results.log("parked nudges: sit in the back half (they asked for space)",
                _pk[0] >= 24 * 0.3, got=f"first={_pk[0]:.1f}h")
    results.log("parked nudges: enough copy for every touch",
                len(_FuCmd2._PARKED_NUDGE_MESSAGES) >= 4,
                got=str(len(_FuCmd2._PARKED_NUDGE_MESSAGES)))

    # An ad lead only earns the six-touch cadence while the long window is
    # genuinely still ahead of us. One who replied late — most of the 72h spent,
    # a standard 24h left — falls back to the four-touch tier schedule that is
    # tuned for 24h, rather than cramming six touches into one day.
    from bot.management.commands.send_followups import (
        has_extended_window as _has_ext, CTWA_EXTENDED_MIN_HOURS as _EXT_MIN,
    )
    _late_ad = _WindowLead(ctwa=True, hours_ago=2.0, entry_hours_ago=50.0)
    results.log("ad window: a nearly-spent ad window drops back to the 24h cadence",
                _has_ext(_late_ad) is False and _max_fu(_late_ad) == 4,
                got=f"window={_fu2._messaging_window_hours(_late_ad):.1f}h "
                    f"extended={_has_ext(_late_ad)} touches={_max_fu(_late_ad)}")
    results.log("ad window: a fresh ad lead is on the extended cadence",
                _has_ext(_WindowLead(ctwa=True)) is True and _max_fu(_WindowLead(ctwa=True)) == 6)
    results.log("ad window: an organic lead is never on the extended cadence",
                _has_ext(_WindowLead()) is False, got=str(_has_ext(_WindowLead())))
    # Half the ad window left is still worth the extra touches.
    _mid_ad = _WindowLead(ctwa=True, hours_ago=30.0, entry_hours_ago=60.0)
    results.log("ad window: 42h of ad window left still earns 6 touches",
                _has_ext(_mid_ad) is True and _max_fu(_mid_ad) == 6,
                got=f"window={_fu2._messaging_window_hours(_mid_ad):.1f}h")
    results.log("ad window: the extended threshold sits above a standard window",
                _EXT_MIN > 24, got=str(_EXT_MIN))
    # Whatever the shape, consecutive touches never breach the minimum gap.
    for _lbl, _ld in (("fresh ad", _WindowLead(ctwa=True)),
                      ("mid ad", _mid_ad),
                      ("late ad", _late_ad),
                      ("organic", _WindowLead())):
        _o = _fu2._followup_offsets(_ld)
        results.log(f"ad window [{_lbl}]: touches respect the minimum gap",
                    all(b - a >= _FU_GAP for a, b in zip(_o, _o[1:])),
                    got=str([round(x, 1) for x in _o]))

    # A lead with no usable timestamps must not crash or schedule at zero.
    class _BareLead:
        id = 7
        lead_status = _LS2.COLD
        followup_count = 0
        last_customer_response = None
        last_inbound_at = None
        last_followup_sent = None
        created_at = None
        ctwa_entry_at = None
        messaging_window_closes_at = None
    _bare_offs = _fu2._followup_offsets(_BareLead())
    results.log("followup cadence: no timestamps → assumes a 24h window",
                len(_bare_offs) >= 4 and _bare_offs[-1] < 24,
                got=str([round(o, 1) for o in _bare_offs]))
    results.log("followup cadence: no reference time → not ready (never sends blind)",
                _fu2._is_ready_for_followup(_BareLead(), None, force=True)[0] is False,
                got=str(_fu2._is_ready_for_followup(_BareLead(), None, force=True)))
    _fu_mod.timezone = _real_tz2
except Exception as e:
    try:
        _fu_mod.timezone = _real_tz2   # never leave the clock frozen for later blocks
    except NameError:
        pass
    results.log("followup cadence: window-derived spacing", False, got=str(e))

# ── No Homebase data on another tenant's messages ────────────────────────────
# Every figure, number, place and business name a customer sees must come from
# THEIR lead's tenant. These five paths were still hardcoded to Homebase, so a
# second tenant's customers were quoted Homebase's prices, given Homebase's
# plumber and told the company was based in Homebase's city.
try:
    from bot.tenant_config import TenantConfig as _TCl

    class _PricedProfile:
        """A tenant with its own (deliberately different) figures."""
        business_hours = {'days': 'Monday-Sunday', 'open': '08:00', 'close': '18:00', 'closed': []}
        plumber_contact = '+263700000001'
        plumber_name = 'Rudo'
        location_area = 'Kensington'
        location_city = 'Bulawayo'
        currency = 'US$'
        faq_facts = {}
        excluded_areas = []
        licensed_claim_enabled = False

    class _Row:
        def __init__(self, family, variant='', label='', supply=None, labour=None,
                     flat=None, allin=None, parts=None, short_label=''):
            self.family, self.variant, self.label = family, variant, label
            self.short_label = short_label
            self.supply, self.labour, self.flat, self.allin = supply, labour, flat, allin
            self.parts, self.sizes = parts or [], []

    def _cfg_with_rows(rows):
        c = _TCl()
        c._profile = _PricedProfile()
        c._profile_loaded = True
        c._price_items = rows
        return c

    _other = _cfg_with_rows([
        _Row('toilet', '', 'toilet seat', supply=11, labour=12, allin=23),
        _Row('tub', '', 'tub', supply=13, labour=14, allin=27),
        _Row('tub', 'freestanding', 'freestanding tub', allin=99,
             parts=[{'name': 'tub', 'amount': 50}, {'name': 'mixer', 'amount': 25},
                    {'name': 'install', 'amount': 24}]),
        _Row('repair', 'leaking_tap', 'Leaking Tap', labour=7),   # cheapest labour
        _Row('renovation', 'bathroom', 'Bathroom Renovation', flat=1234),
    ])
    _empty = _cfg_with_rows([])

    # 1. Catalogue price list — was a hardcoded copy of Homebase's sheet sent
    # alongside the (correctly tenant-scoped) catalogue photos.
    _cat = _other.catalogue_price_lines()
    results.log("tenant data: catalogue prices come from the tenant's own rows",
                'US$11' in _cat and 'US$13' in _cat and 'US$50' in _cat,
                got=_cat.replace('\n', ' | '))
    results.log("tenant data: catalogue prices carry no Homebase figures",
                not any(f'US${n}' in _cat for n in (160, 170, 180, 130, 150, 400, 670)),
                got=_cat.replace('\n', ' | '))
    results.log("tenant data: catalogue list is products only, not renovations",
                'US$1234' not in _cat, got=_cat.replace('\n', ' | '))
    results.log("tenant data: no prices on file → no price list at all",
                _empty.catalogue_price_lines() == '',
                got=repr(_empty.catalogue_price_lines()))

    from bot.whatsapp_webhook import build_catalogue_price_text as _bcpt
    _txt_none = _bcpt('Shall I book you in?', tenant=None)
    results.log("tenant data: a tenant with no price sheet gets no invented prices",
                'US$' not in _txt_none and 'catalogue' in _txt_none.lower(),
                got=_txt_none[:120])

    # 2. Tub pricing — was US$160/US$670 hardcoded, in English and Shona.
    class _FakeTub(ResponseMixin):
        def __init__(self, cfg):
            self._tenant_cfg = cfg
            self.appointment = None
        def _last_assistant_was_tiedown(self):
            return False

    _tubbot = _FakeTub(_other)
    for _kind in ('built_in', 'freestanding', None):
        _reply = _tubbot._tub_price_reply(_kind, 'english')
        results.log(f"tenant data: tub price [{_kind}] uses the tenant's figures",
                    'US$27' in _reply and 'US$99' in _reply,
                    got=_reply.replace('\n', ' ')[:150])
        results.log(f"tenant data: tub price [{_kind}] never quotes Homebase",
                    'US$160' not in _reply and 'US$670' not in _reply,
                    got=_reply.replace('\n', ' ')[:150])
    _sn = _tubbot._tub_price_reply('built_in', 'shona')
    results.log("tenant data: the Shona tub reply is tenant-priced too",
                'US$27' in _sn and 'US$160' not in _sn, got=_sn.replace('\n', ' ')[:150])
    _no_tub = _FakeTub(_empty)._tub_price_reply('built_in', 'english')
    results.log("tenant data: no tub prices on file → free-visit deflection, not a borrowed price",
                'US$' not in _no_tub and 'free' in _no_tub.lower(),
                got=_no_tub[:140])

    # 3. Plumber alerts — both fell back to a hardcoded 263774819901.
    from bot.whatsapp_webhook import _plumber_wa_number

    class _LeadWithPlumber:
        def __init__(self, override='', tenant_number='+263700000001'):
            self.plumber_contact_number = override
            self._tenant_number = tenant_number
        def plumber_contact(self):
            return self.plumber_contact_number or self._tenant_number

    results.log("tenant data: plumber alert uses the tenant's number",
                _plumber_wa_number(_LeadWithPlumber()) == '263700000001',
                got=_plumber_wa_number(_LeadWithPlumber()))
    results.log("tenant data: a per-lead plumber override still wins",
                _plumber_wa_number(_LeadWithPlumber('+263711111111')) == '263711111111',
                got=_plumber_wa_number(_LeadWithPlumber('+263711111111')))
    results.log("tenant data: no number on file → no number, never Homebase's",
                _plumber_wa_number(_LeadWithPlumber('', '')) == '',
                got=repr(_plumber_wa_number(_LeadWithPlumber('', ''))))

    # 4. Classifier prompts named HomeBase for every tenant.
    from bot.unified_classifier import _SYSTEM as _UC_SYSTEM
    results.log("tenant data: the unified classifier prompt is not hardwired to HomeBase",
                'HomeBase' not in _UC_SYSTEM and '{business}' in _UC_SYSTEM,
                got=_UC_SYSTEM.splitlines()[0] if _UC_SYSTEM else '')
    import inspect as _inspect_l

    def _code_lines(fn):
        """Source minus comment lines — a comment ABOUT the old hardcoded value
        must not read as the value still being in the copy."""
        return "\n".join(
            line for line in _inspect_l.getsource(fn).splitlines()
            if not line.strip().startswith('#')
        )

    from bot.service_type_classifier import _deepseek_classify as _dsc
    _dsc_src = _code_lines(_dsc)
    _dsc_prompt = _dsc_src[_dsc_src.index('prompt = f'):] if 'prompt = f' in _dsc_src else _dsc_src
    results.log("tenant data: the service-type prompt takes the business name",
                'Homebase' not in _dsc_prompt and '{business}' in _dsc_prompt)

    # 5. Customer-facing copy carried Homebase's city and cheapest labour rate.
    results.log("tenant data: cheapest labour rate comes from the tenant's rows",
                _other.cheapest_labour_rate() == 7, got=str(_other.cheapest_labour_rate()))
    results.log("tenant data: no labour rows → no 'starts from' claim to make",
                _empty.cheapest_labour_rate() is None,
                got=str(_empty.cheapest_labour_rate()))
    results.log("tenant data: the tenant's own location is available for the copy",
                _other.location_short() == 'Kensington, Bulawayo',
                got=_other.location_short())
    import bot.out_of_scope_handler as _oos_l
    _complaint_src = _code_lines(_oos_l._build_complaint_reply)
    results.log("tenant data: the legitimacy reply no longer hardcodes Harare",
                'based in Harare' not in _complaint_src and '_based_in' in _complaint_src)
    results.log("tenant data: the price-objection reply no longer hardcodes US$20 labour",
                'as little as US$20' not in _complaint_src and '_labour_line' in _complaint_src)
except Exception as e:
    results.log("tenant data: no Homebase values reach other tenants", False, got=str(e))

# ---- Media uploads: the ack must respect what we already know ----------
# The media path used to be a SECOND reply path with hardcoded copy and no
# state awareness: it asked "could you describe what you'd like done" even when
# project_description was already captured, dropped the WhatsApp caption
# entirely, and sent via a direct send_text_message (so no WAMID was stamped).
try:
    from bot.whatsapp_webhook import _compose_media_ack, _MEDIA_ACK_QUESTIONS
    from bot.views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER as _SPLIT

    _ack_no_desc = _compose_media_ack('project_description', 'pending', 'image')
    results.log("media ack: no description yet still asks for one",
                "describe what you'd like done" in _ack_no_desc,
                got=_ack_no_desc)

    _ack_have_desc = _compose_media_ack('area', 'pending', 'image')
    results.log("media ack: description already captured is never re-asked",
                "describe what you'd like done" not in _ack_have_desc
                and "Whereabouts are you based?" in _ack_have_desc,
                got=_ack_have_desc)

    _ack_confirmed = _compose_media_ack('area', 'confirmed', 'image')
    results.log("media ack: a confirmed booking gets no question at all",
                '?' not in _ack_confirmed and _SPLIT not in _ack_confirmed,
                got=_ack_confirmed)

    _ack_complete = _compose_media_ack('complete', 'pending', 'image')
    results.log("media ack: nothing outstanding gets no question at all",
                '?' not in _ack_complete, got=_ack_complete)

    _ack_plan = _compose_media_ack('area', 'pending', 'document',
                                   is_plan_document=True)
    results.log("media ack: a PDF is acknowledged as the plan, not as a photo",
                _ack_plan.startswith("Got the plan"), got=_ack_plan)

    _ack_split = _compose_media_ack('availability_date', 'pending', 'image')
    results.log("media ack: ack and question go as two messages, not one block",
                _ack_split.count(_SPLIT) == 1, got=_ack_split)

    # No plumber name may appear in ack copy: a literal is a homebase value and
    # would reach another tenant's customer (CLAUDE.md: absent means omit).
    _all_acks = [
        _compose_media_ack(q, s, m, p)
        for q in list(_MEDIA_ACK_QUESTIONS) + ['complete', 'name', None]
        for s in ('pending', 'confirmed')
        for m in ('image', 'video', 'document')
        for p in (True, False)
    ]
    results.log("media ack: no acknowledgement ever names the plumber",
                not any('takudzwa' in a.lower() for a in _all_acks))
    results.log("media ack: no acknowledgement carries an emoji",
                all(all(ord(c) < 0x2190 for c in a) for a in _all_acks))
except Exception as e:
    results.log("media ack: state-aware acknowledgement", False, got=str(e))

# ---- Media uploads: a caption is the customer's own words ---------------
try:
    import inspect as _inspect_m
    import bot.whatsapp_webhook as _wh
    _hmm_src = _inspect_m.getsource(_wh.handle_media_message)
    results.log("media caption: the WhatsApp caption is read, not discarded",
                "media_data.get('caption')" in _hmm_src)
    results.log("media caption: a captioned upload routes through the dispatcher",
                'handle_text_message(' in _hmm_src)
    # A PDF must never reach a vision describe call — DeepSeek accepts
    # JPEG/PNG/GIF/WebP only.
    results.log("media: PDFs are flagged so they never reach a vision call",
                "mime_type == 'application/pdf'" in _hmm_src)
    # Defect C: any image used to mark the lead as having architectural plans.
    results.log("media: only a requested plan or a PDF claims the plan slot",
                'if is_plan_document or _was_pending_upload:' in _hmm_src)
    _ack_src = _inspect_m.getsource(_wh._schedule_media_ack)
    results.log("media ack: goes out via delayed_response so the WAMID is stamped",
                'delayed_response(' in _ack_src
                and 'send_text_message' not in _ack_src)
except Exception as e:
    results.log("media caption: routed through the main dispatcher", False, got=str(e))

# ---- Vision: the photo is evidence, the customer's words are testimony --
try:
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM
    _csi = _RM._correct_service_intent

    # Gap-fill: they named nothing, the LLM misfired to a tub, the photo shows
    # a cubicle. Before vision this dropped to 'none' and pitched nothing.
    _v1 = _csi("how much for this?", "tub_sales",
               vision_context="A shower cubicle with a glass door, tiled tray.")
    results.log("vision: a photo fills the gap when the customer named nothing",
                _v1.get("intent") == "shower_cubicle"
                and _v1.get("confidence") == "MEDIUM", got=str(_v1))

    # Testimony beats evidence: they said toilet, the photo shows a cubicle.
    _v2 = _csi("how much for the toilet", "tub_sales",
               vision_context="A shower cubicle with a glass door.")
    results.log("vision: a named product always beats what the photo shows",
                _v2.get("intent") == "toilet", got=str(_v2))

    # Real descriptions from bot/previous_work_photos, captured 2026-08-22.
    # Freestanding is named explicitly whenever it is one (3/3), never on a
    # built-in (0/2), so the word carries the US$670-vs-US$160 decision.
    _v3a = _csi("how much for this?", "tub_sales",
                vision_context="A freestanding bath with a floor-standing mixer "
                               "tap is visible.")
    results.log("vision: a freestanding tub in a photo prices as freestanding",
                _v3a.get("intent") == "standalone_tub", got=str(_v3a))

    _v3b = _csi("how much for this?", "tub_sales",
                vision_context="A white bath is visible, fitted into a tiled "
                               "surround, with a wall-mounted mixer tap.")
    results.log("vision: a bath in a tiled surround prices as the built-in job",
                _v3b.get("intent") == "tub_sales", got=str(_v3b))

    # A bath the description cannot place: abstain. A 4x price gap is not worth
    # a guess, and the free visit prices it properly.
    _v3c = _csi("how much for this?", "tub_sales",
                vision_context="A white bath with chrome taps against a wall.")
    results.log("vision: an unplaceable bath abstains rather than guess",
                _v3c.get("intent") == "none", got=str(_v3c))

    # A whole-bathroom photo names several fixtures; pricing one is picking for
    # them. This is the real shape of most portfolio photos.
    _v3d = _csi("how much for this?", "tub_sales",
                vision_context="Two white vessel basins sit on a dark floating "
                               "vanity, alongside a black freestanding bath.")
    results.log("vision: a photo naming several fixtures prices none of them",
                _v3d.get("intent") == "none", got=str(_v3d))

    # No photo at all behaves exactly as before.
    _v4 = _csi("how much for this?", "tub_sales")
    results.log("vision: with no photo the resolver is unchanged",
                _v4.get("intent") == "none", got=str(_v4))
    _v5 = _csi("Did you sell bathroom cubicles", "tub_sales")
    results.log("vision: the original cubicle misfire fix still holds",
                _v5.get("intent") == "shower_cubicle", got=str(_v5))
except Exception as e:
    results.log("vision: photo feeds intent without overriding the customer", False, got=str(e))

try:
    from bot.models import Appointment as _Appt

    class _FakeAppt:
        conversation_history = [
            {"role": "user", "content": "[Sent image] a shower cubicle",
             "image_description": "a shower cubicle"},
            {"role": "assistant", "content": "Got the photo, thanks."},
            {"role": "user", "content": "how much for this?"},
        ]
    results.log("vision: the stored description is read back off history",
                _Appt.latest_image_description(_FakeAppt()) == "a shower cubicle")

    class _StaleAppt:
        conversation_history = (
            [{"role": "user", "content": "[Sent image] a geyser",
              "image_description": "a geyser"}]
            + [{"role": "user", "content": "later turn"} for _ in range(8)]
        )
    results.log("vision: a photo from earlier in the thread goes stale",
                _Appt.latest_image_description(_StaleAppt()) is None)
except Exception as e:
    results.log("vision: description storage and staleness", False, got=str(e))

try:
    from bot.services.vision import describe_customer_image, VISION_IMAGE_MIMES, VISION_MODEL
    results.log("vision: a PDF is refused before any API call",
                describe_customer_image(b"%PDF-1.4", "application/pdf") is None)
    results.log("vision: empty bytes are refused before any API call",
                describe_customer_image(b"", "image/jpeg") is None)
    results.log("vision: only DeepSeek's four image formats are accepted",
                VISION_IMAGE_MIMES == {"image/jpeg", "image/jpg", "image/png",
                                       "image/webp", "image/gif"})
    results.log("vision: the model id is the vision model, not flash",
                VISION_MODEL == "deepseek-v4-flash-vision-exp")

    # The vision model has NO thinking mode; the shared patch must not send it
    # one. Exercise the wrapper directly: in gate mode the DeepSeek stub has
    # replaced client.chat.completions.create, so going through the client would
    # measure the mock rather than this logic.
    import bot.services.clients as _clients
    _seen = {}

    def _capture(*a, **kw):
        _seen.clear()
        _seen.update(kw)
        return None
    _real = _clients._orig_completions_create
    try:
        _clients._orig_completions_create = _capture
        for _m in (VISION_MODEL, "deepseek-v4-flash"):
            _clients._completions_create_no_thinking(model=_m, messages=[])
            _has_thinking = "thinking" in (_seen.get("extra_body") or {})
            results.log(
                "vision: thinking is disabled for the vision model too"
                if "vision" in _m else
                "vision: thinking is still disabled for the text models",
                _has_thinking,
                got=f"{_m} -> {_seen.get('extra_body')}")
    finally:
        _clients._orig_completions_create = _real
except Exception as e:
    results.log("vision: describe helper guards and the thinking patch", False, got=str(e))

# ---- Vision: a photo of a whole bathroom prices every fixture in it -----
# _active_scope skipped ANY history turn starting with "[", which is exactly the
# "[Sent image] ..." format the vision description rides on — so a photo showing
# a tub, a vanity and basins put none of them in scope.
try:
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM2
    from bot.models import Appointment as _Appt2

    _WHOLE_ROOM = ("Two white vessel basins sit on a dark floating vanity with "
                   "wall-mounted black mixer taps, alongside a black "
                   "freestanding bath with a floor-standing tap.")

    class _PhotoAppt:
        project_description = ''
        conversation_history = [
            {"role": "user", "content": "[Sent image] " + _WHOLE_ROOM,
             "image_description": _WHOLE_ROOM},
            {"role": "user", "content": "how much for this?"},
        ]

        def latest_image_description(self, within=6):
            return _Appt2.latest_image_description(self, within)

    class _PhotoBot(_RM2):
        appointment = _PhotoAppt()

    _pb = _PhotoBot()
    _scope, _ = _pb._active_scope("how much for this?")
    _fams = {f for f, _q in _scope}
    results.log("vision: every fixture in a whole-room photo lands in scope",
                {'tub', 'vanity', 'basin'} <= _fams, got=str(_scope))
    results.log("vision: a photo puts 2+ families in play, so all get priced",
                len(_pb._context_product_families("how much for this?")) >= 2)
    results.log("vision: a freestanding tub in a photo carries freestanding money",
                _pb._tub_type_in_message(_WHOLE_ROOM) == 'freestanding')

    # Precedence: anything the customer typed still outranks the picture.
    class _SaidAppt(_PhotoAppt):
        project_description = 'I need a geyser'
    class _SaidBot(_RM2):
        appointment = _SaidAppt()
    results.log("vision: what they typed still beats what the photo showed",
                _SaidBot()._context_product_families("") == {'geyser'},
                got=str(_SaidBot()._context_product_families("")))

    # Other bracket markers must still be ignored — only [Sent x] is unwrapped.
    class _NoteAppt:
        project_description = ''
        conversation_history = [
            {"role": "user", "content": "[FILE UPLOADED] tub_plan.pdf | URL: x"},
            {"role": "user", "content": "how much for this?"},
        ]
        def latest_image_description(self, within=6):
            return None
    class _NoteBot(_RM2):
        appointment = _NoteAppt()
    _nscope, _ = _NoteBot()._active_scope("how much for this?")
    results.log("vision: internal bracket markers are still not read as scope",
                _nscope == [], got=str(_nscope))
except Exception as e:
    results.log("vision: whole-room photo scope", False, got=str(e))

# ---- Media: "how much" then a photo 20s later must not double-reply ----
# Batch window is 45s, media debounce 8s. The ack fired at ~28s and the batch
# reply at ~45s, each with its own 1-5 min delay, so the lead got two messages
# in random order with a question stacked across them. The batch reply is
# generated after the description lands in history, so it already covers the
# photo.
try:
    import inspect as _inspect_r
    import bot.whatsapp_webhook as _wh_r
    _src_r = _inspect_r.getsource(_wh_r.handle_media_message)
    results.log("media race: an open text batch suppresses the duplicate ack",
                '_pending_batches.get(sender)' in _src_r
                and 'batch_open' in _src_r)
    # The arrival-time guard is not enough on its own: a text landing during the
    # 8s media debounce opens the batch AFTER that check, and the already-armed
    # ack timer still fired. Prod 2026-08-23 (barmak-plumbing): a photo then
    # "How much" a second later sent the lead THREE messages. The ack must
    # re-check when the timer FIRES.
    _ack_src_r = _inspect_r.getsource(_wh_r._schedule_media_ack)
    results.log("media race: the ack re-checks at fire time, not only at arrival",
                '_pending_batches.get(sender)' in _ack_src_r
                and '_pending_send_events.get(sender)' in _ack_src_r)
    results.log("media race: the fire-time check happens before the reply is built",
                _ack_src_r.index('_pending_batches.get(sender)')
                < _ack_src_r.index('_media_ack_reply('))
    results.log("media race: the batch window still outlasts the media debounce",
                _wh_r.MESSAGE_BATCH_WINDOW_SECONDS > _wh_r.MEDIA_DEBOUNCE_SECONDS,
                got=f"batch={_wh_r.MESSAGE_BATCH_WINDOW_SECONDS}s "
                    f"media={_wh_r.MEDIA_DEBOUNCE_SECONDS}s")
except Exception as e:
    results.log("media race: text then photo does not double-reply", False, got=str(e))

# ---- A bare photo: name back what we saw, do not ask them to describe it --
# We just looked at the picture. Asking "could you describe what you'd like
# done" after seeing a freestanding tub is the same absurdity as asking it after
# they send the plan.
try:
    from bot.whatsapp_webhook import _compose_media_ack as _cma2
    from bot.views.plumbot.response_mixin import (
        ResponseMixin as _RM3, MESSAGE_SPLIT_MARKER as _S3)

    class _SeeBot(_RM3):
        appointment = None

    def _seen_q(desc):
        _b = _SeeBot()
        _f = {x for x in _b._product_families_in(desc) if x in _b._FAMILY_DISPLAY}
        _q = _b._confirm_intent_question(_f)
        if _q is None and len(_f) == 1:
            _q = (f"Is it the {_b._FAMILY_DISPLAY[next(iter(_f))]} "
                  f"you're looking to get sorted?")
        return _q

    _one = _cma2('service_type', 'pending', 'image',
                 seen_question=_seen_q("A shower cubicle with a glass panel, a "
                                       "shower tray, a mixer and a shower head."))
    results.log("bare photo: one fixture is named back, not re-asked",
                "Is it the shower" in _one
                and "describe what you'd like done" not in _one, got=_one)

    _two = _cma2('service_type', 'pending', 'image',
                 seen_question=_seen_q("A freestanding bath with a floor-standing "
                                       "mixer tap and a standard close-coupled "
                                       "toilet are visible."))
    results.log("bare photo: two fixtures get the scope confirm, not a describe ask",
                "both the tub and toilet" in _two
                and "describe what you'd like done" not in _two, got=_two)

    # A cubicle photo also matches the 'tap' family. That must not read as two
    # items — the customer sees one thing in that picture.
    _b3 = _SeeBot()
    results.log("bare photo: accessory families never inflate the fixture count",
                {x for x in _b3._product_families_in(
                    "A shower cubicle with a mixer and a shower head.")
                 if x in _b3._FAMILY_DISPLAY} == {'shower'})

    # Nothing plumbing-related in frame: fall back to the generic ask.
    _none = _cma2('service_type', 'pending', 'image', seen_question=_seen_q("A garden fence."))
    results.log("bare photo: an unreadable photo still asks the generic question",
                "describe what you'd like done" in _none, got=_none)

    # The picture cannot answer where they live or when they are free.
    _area = _cma2('area', 'pending', 'image', seen_question="Is it the tub you're looking to get sorted?")
    results.log("bare photo: a seen fixture never displaces the area question",
                "Whereabouts are you based?" in _area, got=_area)

    # Still no price: they showed us a tub, they did not ask what it costs.
    results.log("bare photo: showing us a fixture never volunteers a price",
                'US$' not in _one and 'US$' not in _two)
except Exception as e:
    results.log("bare photo: names back what vision saw", False, got=str(e))

# ---- An inbound photo must carry its WAMID, or quoting it breaks silently -
try:
    import inspect as _inspect_w
    import bot.whatsapp_webhook as _wh_w
    _hm = _inspect_w.getsource(_wh_w.handle_media_message)
    results.log("photo quote: the inbound photo turn is stamped with its WAMID",
                'message_id=message_id' in _hm)
    results.log("photo quote: a media message resolves its own quoted reference",
                'resolve_quoted_message' in _hm)
    # The resolve must happen BEFORE the turn is logged or it is always None.
    results.log("photo quote: the quote is resolved before the turn is logged",
                _hm.index('resolve_quoted_message')
                < _hm.index('add_conversation_message'))
except Exception as e:
    results.log("photo quote: inbound photos carry their WAMID", False, got=str(e))

# ---- The free-form answer prompt must not pitch or name-drop -----------
# Live run 2026-08-23: the opener came back as "our plumber Takudzwa can come
# out for a free site assessment to give you a fixed quote on the spot" —
# naming the plumber unprompted and pitching the visit formally, against both
# the no-name rule and the casual-visit rule.
try:
    import inspect as _inspect_p
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM4
    _dyn = _inspect_p.getsource(_RM4._answer_standalone_question)

    results.log("dynamic answer: the visit is described casually, not pitched",
                'quick look at the space' in _dyn)
    results.log("dynamic answer: the formal assessment pitch is ruled out",
                'NEVER pitch it as a "free site assessment"' in _dyn)
    results.log("dynamic answer: the plumber is not named unprompted",
                'ONLY say it if they ask who is coming' in _dyn)
    results.log("dynamic answer: the direct number is not volunteered",
                'ONLY give this out if they ask for a number' in _dyn)
    # The name itself must still RESOLVE per tenant — never a literal.
    results.log("dynamic answer: the name still comes from the lead's own tenant",
                'plumber_display_name()' in _dyn and 'Takudzwa' not in _dyn)
except Exception as e:
    results.log("dynamic answer: no name-drop, no formal pitch", False, got=str(e))

# ---- "who is coming?" is a question about people, not a quote request ---
# The labour marker r'do\s+(?:my|the|a|up)' matched "do THE work", so
# "who would be coming to do the work?" got the on-site-quote pitch — a price
# answer to a question about who, with the cold-open greeting stapled on after
# it via an unsplit MESSAGE_SPLIT_MARKER (probe 2026-08-23).
try:
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM5

    class _JobQAppt:
        project_type = None
        project_description = None
        customer_area = None
        # Two customer turns = the conversation is underway, which is the regime
        # where the classifier decides on its own.
        conversation_history = [
            {"role": "user", "content": "good afternoon"},
            {"role": "assistant", "content": "Hello,"},
            {"role": "user", "content": "..."},
        ]

    class _JobQ(_RM5):
        appointment = _JobQAppt()

    _jq = _JobQ()
    results.log("who-question: asking who is coming is not a quote request",
                _jq._is_job_quote_request("who would be coming to do the work?") is False)
    results.log("who-question: 'who is doing the job' is not a quote request",
                _jq._is_job_quote_request("who will be doing the job?") is False)
    # Naming a product still reads as a job — the guard must not swallow those.
    results.log("who-question: naming a product still routes as a job quote",
                _jq._is_job_quote_request("who can fit my shower cubicle?") is True)
    results.log("who-question: the ordinary job request is unchanged",
                _jq._is_job_quote_request("I need someone to do my bathroom") is True)
    # "Do you do X?" asks whether we OFFER it. 'renovat' matched the labour
    # markers and pitched the on-site quote at a lead who had asked for nothing.
    results.log("capability: 'do you do bathroom renovations' is not a quote ask",
                _jq._is_job_quote_request("Do you do bathroom renovations?") is False)
    results.log("capability: 'do you install geysers' is not a quote ask",
                _jq._is_job_quote_request("Do you install geysers?") is False)
    results.log("capability: an actual request to do the work still is one",
                _jq._is_job_quote_request("Can you renovate my bathroom") is True)

    # AI-primary: the classifier decides the speech act; the regex above is only
    # the fallback. No extra API call — the result is already computed per turn.
    def _uc(act):
        return {"speech_act": act}
    results.log("ai-primary: a classified quote_request routes to the visit",
                _jq._is_job_quote_request("anything at all", _uc("quote_request")) is True)
    results.log("ai-primary: a classified capability question does not",
                _jq._is_job_quote_request("I need someone to do my bathroom",
                                          _uc("capability")) is False)
    results.log("ai-primary: a classified logistics question does not",
                _jq._is_job_quote_request("I need someone to do my bathroom",
                                          _uc("logistics")) is False)
    # The classifier OVERRIDES the keyword layer — that is the whole point of
    # demoting it. "do my bathroom" trips the labour marker and must still lose.
    results.log("ai-primary: the classifier outranks the keyword marker",
                _jq._job_quote_request_fallback("I need someone to do my bathroom") is True
                and _jq._is_job_quote_request("I need someone to do my bathroom",
                                              _uc("capability")) is False)
    # A failed call, or "other", must fall back rather than read as a decision.
    results.log("ai-primary: a failed classification falls back to keywords",
                _jq._is_job_quote_request("Can you renovate my bathroom", None) is True)
    results.log("ai-primary: speech_act 'other' falls back rather than deciding",
                _jq._is_job_quote_request("Can you renovate my bathroom",
                                          _uc("other")) is True)
    results.log("ai-primary: a junk speech_act value falls back safely",
                _jq._is_job_quote_request("Can you renovate my bathroom",
                                          {"speech_act": 12345}) is True)

    # FIRST CONTACT is stricter: with no context at all, both signals must agree
    # before we abandon the scripted opener. "Hi, I need a plumber" reads as a
    # quote_request to the classifier and pitched an all-in figure at someone who
    # had asked for nothing (scenarios/wall_hung_toilet_chamber_price).
    class _ColdAppt:
        project_type = None
        project_description = None
        customer_area = None
        conversation_history = [{"role": "user", "content": "Hi, I need a plumber"}]

    class _ColdJobQ(_RM5):
        appointment = _ColdAppt()
    _cjq = _ColdJobQ()

    results.log("first contact: a stated need alone does not pitch the quote",
                _cjq._is_job_quote_request("Hi, I need a plumber",
                                           _uc("quote_request")) is False)
    results.log("first contact: a named job still pitches on the opening message",
                _cjq._is_job_quote_request("can you renovate my bathroom",
                                           _uc("quote_request")) is True)
    results.log("first contact: the keyword layer alone is not enough either",
                _cjq._is_job_quote_request("can you renovate my bathroom",
                                           _uc("capability")) is False)
except Exception as e:
    results.log("who-question: not mistaken for a quote request", False, got=str(e))

# ---- No formal visit pitch anywhere the customer reads ------------------
# The rule is to refer to the visit casually ("come round and have a quick look
# at the space"). Repeated formal pitching reads pushy and puts leads off. A
# BOOKING CONFIRMATION is the deliberate exception: once they have committed,
# naming the appointment formally is correct.
try:
    import inspect as _inspect_v
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM11

    _pitch = 'free on-site assessment'
    _offenders = []
    for _name in ('_answer_standalone_question', '_handle_oos_reply',
                  '_answer_product_question', 'handle_service_inquiry',
                  '_get_pricing_followup_prompt', '_build_job_quote_reply'):
        _fn = getattr(_RM11, _name, None)
        if _fn is None:
            continue
        # Comments and docstrings DESCRIBE the rule ("NOT a repeated 'free
        # on-site assessment' pitch"); only emitted strings can reach a customer.
        _src_v = _inspect_v.getsource(_fn)
        if _fn.__doc__:
            _src_v = _src_v.replace(_fn.__doc__, '')
        _code = chr(10).join(
            ln for ln in _src_v.splitlines()
            if not ln.strip().startswith('#')
        )
        # The free-form prompt names the phrase in order to BAN it.
        if 'NEVER' in _code and _pitch in _code:
            continue
        if _pitch in _code.lower():
            _offenders.append(_name)
    results.log("visit copy: no chat path pitches a 'free on-site assessment'",
                not _offenders, got=str(_offenders))

    # And the casual wording is the one actually used.
    _sq3 = _inspect_v.getsource(_RM11._answer_standalone_question)
    results.log("visit copy: the casual wording is what the copy uses",
                'quick look at the space' in _sq3)
except Exception as e:
    results.log("visit copy: no formal pitch in chat", False, got=str(e))

# ---- The cold opener belongs to first contact only ---------------------
# The greeting rule was unconditional in BOTH the user prompt and the system
# message, so the model answered "I would like to request a quote for plumbing
# services" with "Hello, How may we assist you..." and answered an AREA reply
# ("We are in Chitungwiza") with it too — restarting a live conversation, and in
# one case arriving as a second reply on top of a real one.
try:
    import inspect as _inspect_g
    from bot.views.plumbot.response_mixin import (
        ResponseMixin as _RM9, COLD_OPENER as _CO, _COLD_OPENER_RULE as _COR)
    _sq2 = _inspect_g.getsource(_RM9._answer_standalone_question)

    results.log("cold opener: the greeting rule is chosen per conversation state",
                'opener_rule' in _sq2 and '_prior_turns' in _sq2)
    results.log("cold opener: an underway conversation gets a NO-GREETING rule",
                'NO GREETING' in _sq2)
    results.log("cold opener: the system message no longer repeats it blindly",
                'How may we assist you on plumbing services' not in _sq2.split(
                    '"role": "system"')[-1])
    # A system message outranks the user prompt, so the two must not disagree.
    results.log("cold opener: the system message defers to the computed rule",
                'CRITICAL RULE in the user message' in _sq2)
    # The deterministic short-circuit stays: a real greeting at the very start
    # still answers instantly with no API call at all.
    results.log("cold opener: a genuine first-contact greeting still short-circuits",
                '_is_greeting_or_opener(message)' in _sq2
                and 'get_next_question_to_ask() == "service_type"' in _sq2)
    results.log("cold opener: the opener text is one shared constant",
                _CO.startswith('Hello,') and 'How may we assist' in _CO
                and 'How may we assist' in _COR)
except Exception as e:
    results.log("cold opener: first contact only", False, got=str(e))

# ---- A general quote request must not fall back to the greeting slot ----
try:
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM10

    class _ApptQ:
        project_type = None
        project_description = None
        customer_area = None
        scheduled_datetime = None
        conversation_history = []
        def save(self, *a, **kw):
            pass

    class _UnderwayBot(_RM10):
        def __init__(self, **kw):
            self.appointment = _ApptQ()
            for k, v in kw.items():
                setattr(self.appointment, k, v)

    # The service_type FIRST-PASS script is the cold-open greeting, so any turn
    # falling back to service_type mid-conversation re-greeted the lead.
    _cold = _UnderwayBot()
    _cold.appointment.conversation_history = [{"role": "user", "content": "hi"}]
    results.log("greeting: genuine first contact still gets the cold opener",
                _cold._conversation_underway() is False
                and _cold._get_first_pass_question("service_type").startswith("Hello,"))

    _warm = _UnderwayBot()
    _warm.appointment.conversation_history = [
        {"role": "user", "content": "good afternoon"},
        {"role": "assistant", "content": "Hello,"},
        {"role": "user", "content": "I would like a quote"},
    ]
    results.log("greeting: an underway conversation is never re-greeted",
                _warm._conversation_underway() is True
                and "Hello," not in _warm._get_first_pass_question("service_type"))
    results.log("greeting: mid-conversation it asks the service, as a choice",
                " or " in _warm._get_first_pass_question("service_type"))

    # Any captured field means they have engaged, whatever the transcript holds.
    for _f in ("project_type", "project_description", "customer_area"):
        results.log(f"greeting: a captured {_f} counts as underway",
                    _UnderwayBot(**{_f: "x"})._conversation_underway() is True)

    # A photo turn is not the customer speaking — bracket markers do not count.
    _photo = _UnderwayBot()
    _photo.appointment.conversation_history = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "[Sent image] a shower cubicle"},
    ]
    results.log("greeting: a bracket marker turn does not count as speaking",
                _photo._conversation_underway() is False)
except Exception as e:
    results.log("greeting: cold opener is first contact only", False, got=str(e))

# ---- Assumptive close at EVERY stage (AI-primary refactor, Phase 2) -----
# Every stage that CAN close on a this-or-that now does. Area and name are the
# deliberate exceptions: we cannot enumerate suburbs or guess a name, so those
# stay open questions rather than a fake choice.
try:
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM8

    class _ApptStage:
        is_delayed = False
        conversation_history = []
        project_type = None
        customer_area = None
        scheduled_datetime = None

    class _StageBot(_RM8):
        def __init__(self, nq):
            self.appointment = _ApptStage()
            self._nq = nq
        def get_next_question_to_ask(self):
            return self._nq
        def _last_assistant_was_tiedown(self):
            return True   # skip the tie-down gate; we are testing the stage copy
        def _confirm_intent_question(self, items, is_shona=False):
            return None
        def _get_contextual_description_question(self):
            return "What are you looking to get sorted?"
        def _get_next_two_available_days(self):
            return []

    def _ask(nq, lang="english"):
        return _StageBot(nq)._get_pricing_followup_prompt(lang)

    _svc = _ask("service_type")
    results.log("assumptive: service_type offers a choice, not an open ask",
                ' or ' in _svc and 'which service are you looking at' not in _svc.lower(),
                got=_svc)
    _time = _ask("availability_time")
    results.log("assumptive: availability_time offers morning or afternoon",
                'morning' in _time.lower() and 'afternoon' in _time.lower(),
                got=_time)

    # The stage fallback asked a yes/no AND pitched the visit formally — against
    # the presumptive-close rule and the casual-visit rule at the same time.
    _fb = _ask("complete")
    results.log("assumptive: the fallback closes on a choice, not a yes/no",
                ' or ' in _fb and not _fb.lower().startswith('want me to'),
                got=_fb)
    results.log("assumptive: the fallback drops the formal assessment pitch",
                'assessment' not in _fb.lower()
                and 'quick look at the space' in _fb.lower(), got=_fb)

    # Shona must close the same way, not fall back to an English-only improvement.
    for _nq in ("service_type", "availability_time", "complete"):
        _sn = _ask(_nq, "shona")
        results.log(f"assumptive: shona closes on a choice at {_nq}",
                    ' kana ' in _sn, got=_sn)

    # Never a fake choice where we genuinely need free text.
    _area = _ask("area")
    results.log("assumptive: area stays an open question, no invented choice",
                'whereabouts' in _area.lower() and ' or ' not in _area, got=_area)
    _name = _ask("name")
    results.log("assumptive: the name ask stays open too",
                'name' in _name.lower() and ' or ' not in _name, got=_name)

    # Rulebook: one question per reply, never two stacked.
    for _nq in ("service_type", "availability_time", "complete", "area", "name"):
        _q = _ask(_nq)
        results.log(f"assumptive: {_nq} asks exactly one question",
                    _q.count('?') == 1, got=_q)
except Exception as e:
    results.log("assumptive: every stage closes on a choice", False, got=str(e))

# ---- The price gate: AI may WIDEN it, never close it --------------------
# Pricing is the most-regressed area here and the two failure directions are
# not symmetric. Missing a price question costs one awkward turn; wrongly
# deciding someone asked leads with money at a lead who never mentioned it,
# which is the rule that keeps getting broken. So the classifier can add a
# price ask the keywords missed, but an explicit "how much" must survive a
# flaky or failed call.
try:
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM7

    class _PriceGate(_RM7):
        appointment = None
    _pg = _PriceGate()

    results.log("price gate: an explicit ask needs no classifier at all",
                _pg._asks_price_figure("how much is a shower cubicle") is True)
    results.log("price gate: an explicit ask survives a FAILED classification",
                _pg._asks_price_figure("how much is a shower cubicle", None) is True)
    # The classifier must not be able to talk us out of a stated price ask.
    results.log("price gate: the classifier cannot close the gate on 'how much'",
                _pg._asks_price_figure("how much is a shower cubicle",
                                       {"speech_act": "capability"}) is True)
    results.log("price gate: Shona 'marii' still works with no classification",
                _pg._asks_price_figure("marii yeshower") is True)

    # Widening: a price ask carrying none of the markers.
    results.log("price gate: AI catches a price ask with no marker in it",
                _pg._asks_price_figure("what would that set me back?",
                                       {"speech_act": "price_ask"}) is True)
    results.log("price gate: that same message is missed without the classifier",
                _pg._asks_price_figure("what would that set me back?") is False)

    # And it must NOT fire on the acts that are not price questions.
    for _act in ("capability", "logistics", "booking_answer", "quote_request", "other"):
        results.log(f"price gate: '{_act}' is not treated as a price ask",
                    _pg._asks_price_figure("I need a shower cubicle",
                                           {"speech_act": _act}) is False)
except Exception as e:
    results.log("price gate: AI widens but never closes", False, got=str(e))

# ---- The unified call carries the speech act, so routing need not guess ---
try:
    from bot.unified_classifier import uc_speech_act as _ucsa, _SYSTEM as _UCS
    results.log("speech act: the one existing call now returns it",
                '"speech_act"' in _UCS and 'quote_request' in _UCS
                and 'capability' in _UCS and 'logistics' in _UCS)
    results.log("speech act: the misrouted cases are worked examples in the prompt",
                'who would be coming to do the work?' in _UCS
                and 'Do you do bathroom renovations?' in _UCS)
    results.log("speech act: a missing value reads as 'no answer', not a default",
                _ucsa(None) is None and _ucsa({}) is None)
    results.log("speech act: values are normalised, not trusted raw",
                _ucsa({"speech_act": "  Capability  "}) == 'capability')
except Exception as e:
    results.log("speech act: classifier carries the routing signal", False, got=str(e))

# ---- A split reply is stored as its parts, never as one marked-up blob ----
try:
    from bot.models import Appointment as _Appt3
    from bot.views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER as _S5

    class _LogAppt:
        def __init__(self):
            self.conversation_history = []
        def save(self, *a, **kw):
            pass
        add_conversation_message = _Appt3.add_conversation_message

    _la = _LogAppt()
    _la.add_conversation_message("assistant", f"Got it.{_S5}What area are you in?")
    results.log("split log: the marker is split into separate turns",
                len(_la.conversation_history) == 2, got=str(_la.conversation_history))
    results.log("split log: no control character survives into the transcript",
                all(_S5 not in e["content"] for e in _la.conversation_history))

    # generate_response logs the parts, then the webhook logs them AGAIN. A
    # last-entry-only dedup let part one back in, because by then part two was
    # the final entry.
    _la.add_conversation_message("assistant", "Got it.")
    _la.add_conversation_message("assistant", "What area are you in?")
    results.log("split log: re-logging the same split reply does not double it",
                len(_la.conversation_history) == 2,
                got=str([e["content"] for e in _la.conversation_history]))

    # A genuine repeat separated by the customer's turn is still preserved.
    _la.add_conversation_message("user", "sorry, what?")
    _la.add_conversation_message("assistant", "What area are you in?")
    results.log("split log: a genuine repeat after the customer speaks is kept",
                len(_la.conversation_history) == 4,
                got=str([e["content"] for e in _la.conversation_history]))
except Exception as e:
    results.log("split log: split replies stored as parts", False, got=str(e))

# ---- Photo turns feed the same machinery a typed message does ----------
try:
    import inspect as _inspect_c
    import bot.whatsapp_webhook as _wh_c
    _hm_c = _inspect_c.getsource(_wh_c.handle_media_message)
    results.log("photo: service type is classified from what vision saw",
                'classify_and_save(appointment, image_description)' in _hm_c)
    # A photo landing on a running exchange must not produce a SECOND reply on
    # top of one generated before the photo existed.
    results.log("photo: a photo mid-exchange re-enters the batch, not a 2nd reply",
                'send_in_flight' in _hm_c and '_enqueue_for_response(' in _hm_c)
    results.log("photo: a cold photo still gets the acknowledgement path",
                '_schedule_media_ack(sender, appointment, media_type' in _hm_c)
except Exception as e:
    results.log("photo: feeds classification and batching", False, got=str(e))

# ---- The free-form answer prompt shows the register instead of naming it -
try:
    import inspect as _inspect_h
    from bot.views.plumbot.response_mixin import ResponseMixin as _RM6
    _sq = _inspect_h.getsource(_RM6._answer_standalone_question)
    results.log("humanness: the prompt carries worked examples, not adjectives",
                'HOW IT SHOULD SOUND' in _sq and 'Weak:' in _sq and 'Good:' in _sq)
    results.log("humanness: assistant-register tells are banned outright",
                'NEVER WRITE' in _sq and "I'd be happy to" in _sq
                and 'Feel free to' in _sq)
    results.log("humanness: no greeting is stapled onto a running conversation",
                'No greeting unless this is their first message' in _sq)
    # The worked examples came from HOMEBASE transcripts, and this prompt is
    # served to every tenant. A figure in an example is a figure the model can
    # reuse for someone else's customer — the leak class CLAUDE.md forbids.
    # Every price must come from the tenant's own pricing guide instead.
    _examples = _sq[_sq.index('HOW IT SHOULD SOUND'):] if 'HOW IT SHOULD SOUND' in _sq else ''
    results.log("humanness: the worked examples carry no hardcoded prices",
                'US$' not in _examples,
                got=[ln.strip() for ln in _examples.splitlines() if 'US$' in ln])
    results.log("humanness: the examples say where a real price must come from",
                'Never reuse a number from an example' in _sq)
except Exception as e:
    results.log("humanness: prompt shows the register", False, got=str(e))

# ---- Reschedules: the deterministic half --------------------------------
# Three methods this flow called did not exist anywhere (the keyword fallback,
# the plumber alert, the calendar move), and every failure was eaten by a bare
# except: the customer was told the new time was confirmed while the plumber
# kept the old one. These pin the deterministic pieces.
try:
    import re as _re_r
    import pytz as _pytz_r
    from datetime import datetime as _dt_r
    from bot.views.plumbot.reschedule_mixin import RescheduleMixin as _RSM

    _SAST_R = _pytz_r.timezone('Africa/Johannesburg')
    _EMOJI_R = _re_r.compile('[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u27BF\u2B00-\u2BFF\uFE0F]')

    class _FakeReschedCfg:
        def hours_sentence(self):    return 'Sunday to Friday, 8am to 6pm'
        def emergency_sentence(self): return ''

    class _FakeReschedApt:
        def __init__(self, **kw):
            self.id = 1
            self.status = 'confirmed'
            self.appointment_type = 'site_visit'
            self.scheduled_datetime = _SAST_R.localize(_dt_r(2026, 9, 6, 9, 30))
            self.job_scheduled_datetime = None
            self.job_duration_hours = 4
            self.customer_name = 'Tinashe'
            self.customer_area = 'Borrowdale'
            self.project_type = 'bathroom_renovation'
            self.job_description = ''
            self.contact = '+263774819901'
            self.__dict__.update(kw)

        def plumber_contact(self):
            return self.contact

    class _FakeResched(_RSM):
        """Fake self for the reschedule resolvers — carries every attribute the
        copy builders reach for (language, SAST formatting, tenant hours)."""
        tenant_cfg = _FakeReschedCfg()

        def __init__(self, apt=None, language='english'):
            self.appointment = apt or _FakeReschedApt()
            self.phone_number = 'whatsapp:+263771111111'
            self._language = language

        def _lead_language(self):
            return self._language

        def format_datetime_for_display(self, dt):
            return dt.astimezone(_SAST_R)

    # The keyword fallback: it runs exactly when DeepSeek is unreachable, so it
    # must be deterministic AND must exist (it was called but never defined).
    RESCHEDULE_KEYWORD_CASES = [
        ("Something came up, can we move it?",   True),
        ("I need to reschedule",                 True),
        ("can't make Thursday",                  True),
        ("could we do another day",              True),
        ("ndinoda kuchinja zuva",                True),
        ("handikwanisi neChishanu",              True),
        ("Thanks for confirming",                False),
        ("How much will it cost?",               False),
        ("Do you need directions?",              False),
    ]
    _rs = _FakeResched()
    for _msg, _expected in RESCHEDULE_KEYWORD_CASES:
        _got = _rs.detect_reschedule_request(_msg)
        results.log(f"reschedule keywords: '{_msg[:34]}'",
                    _got == _expected, expected=_expected, got=_got)

    # No confirmed slot = nothing to move, whatever the words say.
    results.log("reschedule keywords: silent without a confirmed appointment",
                _FakeResched(_FakeReschedApt(status='pending'))
                .detect_reschedule_request('can we reschedule?') is False)

    # A booked JOB keeps its finished site visit in scheduled_datetime. The bot
    # quoted and moved THAT, leaving the jobs board on the old job time.
    _job_apt = _FakeReschedApt(
        appointment_type='job_appointment',
        job_scheduled_datetime=_SAST_R.localize(_dt_r(2026, 9, 10, 8, 0)))
    results.log("reschedule slot: a booked job moves job_scheduled_datetime",
                _FakeResched(_job_apt)._reschedule_slot()[0] == 'job_scheduled_datetime',
                got=_FakeResched(_job_apt)._reschedule_slot()[0])
    results.log("reschedule slot: a site visit moves scheduled_datetime",
                _rs._reschedule_slot()[0] == 'scheduled_datetime')

    # Copy: no emojis, no placeholder US number, no hardcoded week.
    _texts = [
        _rs._build_reschedule_confirmation(
            _rs.appointment.scheduled_datetime,
            _SAST_R.localize(_dt_r(2026, 9, 8, 14, 0))),
        _rs._build_reschedule_clarification('Sunday, September 06 at 09:30 AM'),
        _rs._build_reschedule_unavailable_reply([]),
        _rs._build_reschedule_unavailable_reply([{'display': 'Monday at 10:00 AM'}]),
        _rs._reschedule_breakdown_reply(),
    ]
    results.log("reschedule copy: no emojis anywhere",
                all(_EMOJI_R.search(t) is None for t in _texts),
                got=[t for t in _texts if _EMOJI_R.search(t)])
    results.log("reschedule copy: no '(555) PLUMBING' placeholder number",
                all('555' not in t and 'PLUMBING' not in t for t in _texts),
                got=[t for t in _texts if '555' in t or 'PLUMBING' in t])
    results.log("reschedule copy: hours come from the tenant, not a hardcoded week",
                'Monday to Friday' not in _texts[2] and 'Sunday to Friday' in _texts[2],
                got=_texts[2])
    results.log("reschedule copy: the breakdown reply offers the tenant's own line",
                '263774819901' in _texts[4], got=_texts[4])
    results.log("reschedule copy: a tenant with no number gets no number",
                '263774819901' not in _FakeResched(_FakeReschedApt(contact=''))
                ._reschedule_breakdown_reply())
    results.log("reschedule copy: Shona lead gets a Shona confirmation",
                'Tichakufonerai' in _FakeResched(language='shona')
                ._build_reschedule_confirmation(
                    _rs.appointment.scheduled_datetime,
                    _SAST_R.localize(_dt_r(2026, 9, 8, 14, 0))))

    # Every method the reschedule flow calls must actually exist — this is the
    # bug class that shipped three times over (AttributeError swallowed by an
    # except, the side effect silently skipped).
    from bot.views.plumbot.base import Plumbot as _PB_R
    _REQUIRED = ('detect_reschedule_request', 'notify_team_about_reschedule',
                 'update_google_calendar_appointment', 'parse_datetime',
                 '_reschedule_slot', '_reschedule_availability')
    _missing = [m for m in _REQUIRED if not hasattr(_PB_R, m)]
    results.log("reschedule: every method the flow calls is defined",
                not _missing, got=_missing)

    import inspect as _inspect_r
    _psr = _inspect_r.getsource(_RSM.process_successful_reschedule)
    results.log("reschedule: the move is written to the RESOLVED slot field",
                'setattr(self.appointment, field' in _psr)
    results.log("reschedule: a failed save never claims the move happened",
                '_reschedule_breakdown_reply()' in _psr)
except Exception as e:
    results.log("reschedule: deterministic half", False, got=str(e))

# ── Quoted portfolio photo prices from the tenant's OWN row ──────────────────
# Prod 2026-08-27 (barmak): the bot sent its gallery, the customer quoted the
# "Borehole" photo and asked "How much". No product FAMILY matched (the family
# list is Homebase's: tub / shower / geyser / …), so it fell through to the
# pricing overview and answered a borehole question with the bathroom package.
try:
    from bot import whatsapp_webhook as _wwh

    class _FakePortfolioItem:
        def __init__(self, title, price_line, item_id='borehole-1'):
            self.title, self.price_line, self.item_id = title, price_line, item_id

    class _FakePlumbot:
        catalogued = None

        def _product_price_close(self, language='english'):
            return "Does that sit where you expected?"

        def compose_quoted_photo_price_reply(self, title, language='english'):
            return self.catalogued

        def _ensure_price_disclaimer(self, intent, reply):
            if not reply or '$' not in reply or 'sees the space' in reply.lower():
                return reply
            line = ("These are starting prices. The exact price is confirmed "
                    "once the plumber sees the space.")
            parts = reply.split('\n\n')
            if len(parts) >= 2:
                parts.insert(len(parts) - 1, line)
                return '\n\n'.join(parts)
            return f"{reply}\n\n{line}"

        def _no_price_on_file_reply(self, language='english'):
            return ("Mitengo inosiyana. Toona nzvimbo mahara?"
                    if language == 'shona' else
                    "Pricing depends on exactly what you're after. "
                    "We can come through and give you a fixed price, free of charge.")

    _held = {'item': _FakePortfolioItem('Borehole', 'Borehole installation from US$1 200')}
    _orig_lookup = _wwh._quoted_portfolio_item
    _wwh._quoted_portfolio_item = lambda tenant, quoted: _held['item']
    try:
        class _Appt:
            tenant = object()
        _reply = _wwh._quoted_portfolio_price_reply(
            _FakePlumbot(), _Appt(), 'Borehole', 'How much')
        results.log("quoted photo: priced from the photo's own line",
                    _reply is not None and 'US$1 200' in _reply, got=_reply)
        results.log("quoted photo: never falls back to the package price",
                    _reply is not None and 'Facebook package' not in _reply, got=_reply)
        results.log("quoted photo: names the piece the customer pointed at",
                    _reply is not None and 'Borehole' in _reply, got=_reply)
        results.log("quoted photo: no emojis in the reply",
                    _reply is not None and not any(ord(c) > 0x2100 for c in _reply),
                    got=_reply)
        # Same script as every other priced answer: money, blank line,
        # starting-prices disclaimer, blank line, one closing question.
        _blocks = (_reply or '').split('\n\n')
        results.log("quoted photo: follows the standard priced-reply shape",
                    len(_blocks) == 3 and 'US$1 200' in _blocks[0]
                    and 'starting prices' in _blocks[1].lower()
                    and _blocks[2].endswith('?'), got=_blocks)
        results.log("quoted photo: the price is not run into the question",
                    'US$1 200 Does that' not in (_reply or ''), got=_reply)
        # A catalogued photo uses the existing composer, which prices every
        # item in the shot rather than just the headline one.
        _fp = _FakePlumbot()
        _fp.catalogued = 'Shower cubicle from US$305\nVanity from US$250\n\nWhat area are you in?'
        _cat = _wwh._quoted_portfolio_price_reply(_fp, _Appt(), 'Borehole', 'How much')
        results.log("quoted photo: a catalogued photo uses the richer composer",
                    _cat is not None and 'Vanity from US$250' in _cat, got=_cat)
        results.log("quoted photo: the catalogued reply still gets the disclaimer",
                    _cat is not None and 'starting prices' in _cat.lower(), got=_cat)
        # A "pricing" reply with no figure in it is not a pricing reply. Prod
        # 2026-08-28: the composer emitted its header and a bare "- ", and the
        # customer got "…covering everything in the photo:\n-" with no price.
        _fp2 = _FakePlumbot()
        _fp2.catalogued = ("Here's the full pricing for that piece, covering "
                           "everything in the photo:\n- \n\nWhat area are you in?")
        _held['item'] = _FakePortfolioItem('Borehole', 'Borehole from US$500')
        _empty = _wwh._quoted_portfolio_price_reply(_fp2, _Appt(), 'Borehole', 'How much')
        results.log("quoted photo: a priceless composer result is rejected",
                    _empty is not None and 'US$500' in _empty, got=_empty)
        results.log("quoted photo: the empty bullet never reaches the customer",
                    _empty is not None and '\n- \n' not in _empty, got=_empty)
        # A recognised photo we hold no price for must NOT invent one — and must
        # NOT fall through either. Prod 2026-08-27: returning None here let the
        # multi-item branch answer a borehole question with shower and tub
        # prices carried over from an earlier photo in the same conversation.
        _held['item'] = _FakePortfolioItem('Borehole', '')
        _unpriced = _wwh._quoted_portfolio_price_reply(
            _FakePlumbot(), _Appt(), 'Borehole', 'How much')
        results.log("quoted photo: an unpriced photo still answers about THAT photo",
                    _unpriced is not None and 'Borehole' in _unpriced, got=_unpriced)
        results.log("quoted photo: an unpriced photo quotes no figure at all",
                    _unpriced is not None and 'US$' not in _unpriced, got=_unpriced)
        results.log("quoted photo: an unpriced photo offers the visit instead",
                    _unpriced is not None and 'free of charge' in _unpriced,
                    got=_unpriced)
        # Nothing matched → fall through to the existing steps.
        _wwh._quoted_portfolio_item = lambda tenant, quoted: None
        results.log("quoted photo: an unmatched quote falls through",
                    _wwh._quoted_portfolio_price_reply(
                        _FakePlumbot(), _Appt(), 'Something else', 'How much') is None)
    finally:
        _wwh._quoted_portfolio_item = _orig_lookup

    # The bot's own photos get described too, so a quoted reply carries real
    # text and not just a one-word title — but the TITLE must stay in front, or
    # _quoted_portfolio_item can no longer resolve the row.
    _src_d = _inspect_r.getsource(_wwh._describe_work_image)
    results.log("sent photo: the description leads with the title",
                "f\"{item['title']} - {vision}\"" in _src_d)

    # Vision must be able to NAME a photo, not just describe it — an upload
    # with no caption has no other identity.
    from bot.services import vision as _vis

    def _parse(raw):
        _orig = _vis._describe
        _vis._describe = lambda *a, **k: raw
        try:
            return _vis.describe_portfolio_image(b'x', 'image/jpeg')
        finally:
            _vis._describe = _orig
    _lbl, _desc = _parse("Borehole pump\nA borehole pump and pressure tank.")
    results.log("vision: a two-line answer yields a short name",
                _lbl == 'Borehole pump' and 'pressure tank' in _desc,
                got=(_lbl, _desc))
    _lbl2, _desc2 = _parse("The photo shows a long rambling prose answer that "
                           "ignored the requested two-line shape entirely.")
    results.log("vision: prose is never used as a title",
                _lbl2 is None and _desc2, got=(_lbl2, _desc2))
    results.log("vision: nothing back means nothing claimed",
                _parse('') == (None, None))

    class _Row:
        def __init__(self, title, price_line='x', item_id='i'):
            self.title, self.price_line, self.item_id = title, price_line, item_id

    # Title-prefix resolution: exercised through the real function with a stub
    # queryset, so the ordering rule (longest title wins) is what is pinned.
    class _StubManager:
        rows = []

        def filter(self, **kwargs):
            return list(self.rows)

    class _StubModel:
        objects = _StubManager()

    import sys as _sys_v
    _fake_mod = type(_sys_v)('bot.models')
    _fake_mod.TenantPortfolioItem = _StubModel
    _real_models = _sys_v.modules.get('bot.models')
    _sys_v.modules['bot.models'] = _fake_mod
    try:
        _StubModel.objects.rows = [_Row('Borehole'), _Row('Borehole and tank')]
        _hit = _wwh._quoted_portfolio_item(object(), 'Borehole and tank - a pump')
        results.log("sent photo: the longest matching title wins",
                    _hit is not None and _hit.title == 'Borehole and tank',
                    got=getattr(_hit, 'title', None))
        _hit = _wwh._quoted_portfolio_item(object(), 'Borehole - a borehole pump')
        results.log("sent photo: an enriched description still resolves",
                    _hit is not None and _hit.title == 'Borehole',
                    got=getattr(_hit, 'title', None))
        _StubModel.objects.rows = [_Row('Shower'), _Row('Shower')]
        results.log("sent photo: two identical titles resolve to nothing",
                    _wwh._quoted_portfolio_item(object(), 'Shower') is None)
    finally:
        if _real_models is not None:
            _sys_v.modules['bot.models'] = _real_models
        else:
            _sys_v.modules.pop('bot.models', None)

    # A price entered in the tenant's CONFIG must reach a quoted photo even
    # when nobody linked the two. Prod 2026-08-27: barmak's sheet carried
    # borehole at US$500 all-in and their gallery had a "Borehole" photo, but
    # with price_refs=[] and a blank price_line the customer could not be told.
    from bot import media_library as _ml

    class _PriceRow:
        def __init__(self, family, allin=None, label='', variant='',
                     short_label='', keywords=None, flat=None,
                     supply=None, labour=None):
            self.family, self.variant, self.label = family, variant, label
            self.short_label, self.keywords = short_label, keywords or []
            self.allin, self.flat = allin, flat
            self.supply, self.labour = supply, labour

    class _Photo:
        def __init__(self, title, price_line='', price_refs=None):
            self.title, self.price_line = title, price_line
            self.price_refs = price_refs or []

    _rows = [_PriceRow('borehole', allin=500, label='Borehole'),
             # A split row, so the live-vs-stored precedence case has a
             # breakdown to prove came from the price sheet.
             _PriceRow('shower', allin=305, label='Shower cubicle',
                       supply=220, labour=85)]
    _orig_filter = _ml.TenantPriceItem if hasattr(_ml, 'TenantPriceItem') else None
    import bot.models as _bm
    _orig_objects = _bm.TenantPriceItem.objects
    _orig_cur = _ml._tenant_currency
    _ml._tenant_currency = lambda tenant: 'US$'

    class _Objs:
        def filter(self, **kw):
            return _rows
    _bm.TenantPriceItem.objects = _Objs()
    try:
        results.log("photo price: an unlinked photo resolves from the price sheet",
                    _ml.price_line_for_item(None, _Photo('Borehole'))
                    == 'Borehole from US$500',
                    got=_ml.price_line_for_item(None, _Photo('Borehole')))
        results.log("photo price: a hand-typed line is kept when there are no refs",
                    _ml.price_line_for_item(
                        None, _Photo('Borehole', price_line='Borehole from US$650'))
                    == 'Borehole from US$650')

        # Every priced answer shows what the supply and the labour each cost —
        # a photo's line was the one place quoting a bare all-in figure.
        _split_row = _PriceRow('borehole', label='Borehole', supply=300, labour=200)
        results.log("breakdown: a split row prints supply and install",
                    _ml.price_sentence('Borehole', _split_row, 'US$')
                    == 'Borehole from US$500 all-in (supply from US$300 + install from US$200)',
                    got=_ml.price_sentence('Borehole', _split_row, 'US$'))
        results.log("breakdown: an all-in-only row is never given an invented split",
                    _ml.price_sentence('Borehole', _PriceRow('borehole', allin=500), 'US$')
                    == 'Borehole from US$500')
        results.log("breakdown: the tenant's own currency is used",
                    _ml.price_sentence('Borehole', _split_row, 'R').startswith('Borehole from R500'))
        results.log("breakdown: a row with no figure at all prints nothing",
                    _ml.price_sentence('Borehole', _PriceRow('borehole'), 'US$') == '')
        # A stored line predating the split must not mask the live one.
        _stale = _Photo('Shower cubicle', price_line='Shower cubicle from US$305',
                        price_refs=[{'family': 'shower', 'variant': ''}])
        _fresh = _ml.price_line_for_item(None, _stale)
        results.log("breakdown: a stale stored line loses to the live price sheet",
                    'supply from' in (_fresh or ''), got=_fresh)
        results.log("photo price: an unmatched title stays blank",
                    _ml.price_line_for_item(None, _Photo('Kitchen renovation')) == '')
        results.log("photo price: matching is strict, not substring",
                    _ml.price_line_for_item(None, _Photo('Repairing a shower door')) == '')
        results.log("photo price: a title with trailing words still matches",
                    _ml.price_line_for_item(None, _Photo('Borehole installation'))
                    == 'Borehole from US$500')

        # An UNNAMED upload has only what the bot saw. One priced job named in
        # the description is enough; two is a coin flip, so it abstains.
        class _Seen(_Photo):
            def __init__(self, vision, title=_ml.PENDING_TITLE):
                super().__init__(title)
                self.vision_description = vision
        results.log("photo price: vision alone can price an unnamed photo",
                    _ml.price_line_for_item(
                        None, _Seen('A borehole pump and pressure tank plumbed '
                                    'to a storage tank.')) == 'Borehole from US$500')
        results.log("photo price: two priced jobs in view abstains",
                    _ml.price_line_for_item(
                        None, _Seen('A shower cubicle beside a borehole pump.')) == '')
        results.log("photo price: vision naming nothing priced stays blank",
                    _ml.price_line_for_item(
                        None, _Seen('A tiled wall with no fittings.')) == '')
    finally:
        _bm.TenantPriceItem.objects = _orig_objects
        _ml._tenant_currency = _orig_cur

    # A highlighted photo we have never looked at is described ON DEMAND, so
    # photos uploaded before vision existed still answer properly — no backfill.
    class _Row2:
        def __init__(self, title, vision='', item_id='p1'):
            self.title, self.vision_description, self.item_id = title, vision, item_id

    _orig_lookup2 = _wwh._quoted_portfolio_item
    _orig_ml = _sys_v.modules.get('bot.media_library')
    _fake_ml = type(_sys_v)('bot.media_library')
    _calls = []

    def _fake_describe(item):
        _calls.append(item.item_id)
        item.vision_description = 'A borehole pump and pressure tank.'
        return item.vision_description
    _fake_ml.describe_portfolio_item = _fake_describe
    _sys_v.modules['bot.media_library'] = _fake_ml
    try:
        _undescribed = _Row2('Borehole')
        _wwh._quoted_portfolio_item = lambda tenant, quoted: _undescribed
        _out = _wwh._enrich_quoted_photo(type('_A', (), {'tenant': object()})(), 'Borehole')
        results.log("highlighted photo: an undescribed one is looked at on demand",
                    _calls == ['p1'] and 'pressure tank' in _out, got=_out)
        results.log("highlighted photo: the title still leads the quote",
                    _out.startswith('Borehole -'), got=_out)

        _calls.clear()
        _already = _Row2('Borehole', vision='Already seen.')
        _wwh._quoted_portfolio_item = lambda tenant, quoted: _already
        _wwh._enrich_quoted_photo(type('_A', (), {'tenant': object()})(), 'Borehole')
        results.log("highlighted photo: a described one is never re-described",
                    _calls == [])

        _wwh._quoted_portfolio_item = lambda tenant, quoted: None
        results.log("highlighted photo: a quote that is not ours is untouched",
                    _wwh._enrich_quoted_photo(
                        type('_A', (), {'tenant': object()})(), 'hello') == 'hello')
        results.log("highlighted photo: no quote does nothing",
                    _wwh._enrich_quoted_photo(
                        type('_A', (), {'tenant': object()})(), None) is None)
    finally:
        _wwh._quoted_portfolio_item = _orig_lookup2
        if _orig_ml is not None:
            _sys_v.modules['bot.media_library'] = _orig_ml
        else:
            _sys_v.modules.pop('bot.media_library', None)

    # Vision writes PROSE, and prose names fixtures incidentally: a storage
    # tank "on a steel tower structure with pipe" resolved to pipe_repair and
    # priced pipe work. The deterministic resolver sees the title only.
    # The builder itself must refuse an unpriced piece — the OTHER caller
    # (STEP 0b) would emit the same empty bullet otherwise.
    from bot import portfolio_catalog as _pc
    _orig_by_title = _pc.get_item_by_title
    try:
        _pc.get_item_by_title = lambda title, tenant=None: {
            'title': 'Borehole', 'price': ''}
        results.log("price guide: an unpriced piece yields no guide at all",
                    _pc.build_item_price_guide('Borehole') is None)
        _pc.get_item_by_title = lambda title, tenant=None: {
            'title': 'Borehole', 'price': 'Borehole from US$500'}
        _guide = _pc.build_item_price_guide('Borehole')
        results.log("price guide: a priced piece still builds its guide",
                    _guide is not None and 'US$500' in _guide, got=_guide)
    finally:
        _pc.get_item_by_title = _orig_by_title

    results.log("quoted title: vision sentences are stripped for the keyword resolver",
                _wwh._quoted_title('Borehole - Storage tank on a tower with pipe')
                == 'Borehole')
    results.log("quoted title: a legacy title-only quote is unchanged",
                _wwh._quoted_title('Borehole') == 'Borehole')
    results.log("quoted title: incidental prose no longer resolves a product",
                _wwh._keyword_product_intent(
                    _wwh._quoted_title('Borehole - Storage tank on a tower with pipe'))
                is None)
    results.log("quoted title: the raw prose WOULD have mis-resolved",
                _wwh._keyword_product_intent(
                    'Borehole - Storage tank on a tower with pipe') == 'pipe_repair')

    _src_q = _inspect_r.getsource(_wwh._generate_and_schedule_reply)
    results.log("highlighted photo: enrichment runs before any reply step",
                _src_q.find('_enrich_quoted_photo') != -1
                and _src_q.find('_enrich_quoted_photo') < _src_q.find('STEP 1c'))
    # Nothing between STEP 1c and STEP 3 guards on `reply is None`, so the
    # quoted-photo answer must SEND and return, not set a variable for later
    # steps to overwrite.
    _step1c = _src_q[_src_q.find('STEP 1c'):_src_q.find('STEP 2: Service-specific')]
    results.log("quoted photo: the reply is sent and returned, not left to be overwritten",
                'delayed_response' in _step1c and 'return' in _step1c)
    # The title, never the vision prose after it. (The call also carries the
    # lead's own tenant config now, so match the opening of the call only.)
    results.log("quoted photo: the keyword resolver reads the title only",
                '_keyword_product_intent(_quoted_title(quoted_text)' in _src_q)
    results.log("quoted photo: the price step runs BEFORE the family steps",
                _src_q.find('STEP 1c') != -1
                and _src_q.find('STEP 1c') < _src_q.find('STEP 2: Service-specific'))
except Exception as e:
    results.log("quoted photo pricing", False, got=str(e))

# ── The repeat-price recap is the TENANT's offer, not Homebase's ─────────────
# It was a hardcoded "Our Facebook package is US$800 - freestanding tub and side
# chamber" sent to every tenant's customers. Barmak's is US$900 with different
# contents, so a lead who asked twice got the right figure and then a wrong one.
try:
    from bot.views.plumbot.response_mixin import ResponseMixin as _PB_P

    class _Cfg:
        def __init__(self, amount):
            self._amount = amount

        def price_item(self, family, variant=''):
            if self._amount is None:
                return None
            return type('_I', (), {
                'flat': self._amount,
                'label': 'Facebook package',
                'parts': [{'name': 'freestanding tub'}, {'name': 'shower cubicle'}],
            })()

    class _RecapBot:
        _pricing_overview_recap = _PB_P._pricing_overview_recap

        def __init__(self, amount):
            self.tenant_cfg = _Cfg(amount)

        def _get_pricing_followup_prompt(self, language='english'):
            return "What did you have in mind?"

    _recap = _RecapBot(900)._pricing_overview_recap('english')
    results.log("price recap: uses the tenant's own offer figure",
                _recap is not None and 'US$900' in _recap, got=_recap)
    results.log("price recap: never restates Homebase's US$800 package",
                _recap is not None and 'US$800' not in _recap, got=_recap)
    results.log("price recap: no tenant offer means no recap at all",
                _RecapBot(None)._pricing_overview_recap('english') is None)
    _recap_sn = _RecapBot(900)._pricing_overview_recap('shona')
    results.log("price recap: mirrors the lead's language",
                _recap_sn is not None and 'US$900' in _recap_sn
                and 'fixed price' not in _recap_sn, got=_recap_sn)
    results.log("price recap: no emojis",
                _recap is not None and not any(ord(c) > 0x2100 for c in _recap))

    _src_r = _inspect_r.getsource(_wwh._generate_and_schedule_reply)
    results.log("price recap: the webhook no longer hardcodes a package price",
                'US$800' not in _src_r and '_pricing_overview_recap(' in _src_r)
except Exception as e:
    results.log("price recap tenant-safety", False, got=str(e))

# ── The out-of-scope list is the TENANT's, not Homebase's ────────────────────
# Same prod incident: "borehole" is hardcoded out-of-scope, but barmak sells
# borehole work and shows it in their own gallery.
try:
    from bot import out_of_scope_handler as _oos

    results.log("oos: borehole is still out of scope with no tenant",
                'borehole' in _oos.out_of_scope_terms_for(None))

    _orig_sells = _oos.tenant_sells
    _oos.tenant_sells = lambda tenant, term: term == 'borehole'
    try:
        _terms = _oos.out_of_scope_terms_for(object())
        results.log("oos: a term the tenant sells drops off their list",
                    'borehole' not in _terms)
        results.log("oos: everything else stays out of scope",
                    'painting' in _terms and 'roofing' in _terms, got=_terms)
        results.log("oos: keyword fallback no longer declines the tenant's own work",
                    _oos._keyword_classify('do you do borehole', tenant=object()
                                           )['category'] == 'in_scope')
        results.log("oos: keyword fallback still declines real out-of-scope work",
                    _oos._keyword_classify('do you do roofing', tenant=object()
                                           )['category'] == 'out_of_scope')
    finally:
        _oos.tenant_sells = _orig_sells

    results.log("oos: tenant_sells is safe with no tenant",
                _oos.tenant_sells(None, 'borehole') is False)

    from bot import unified_classifier as _uc
    results.log("oos: the prompt no longer hardcodes a service list",
                'borehole' not in _uc._SYSTEM
                and '{out_of_scope_services}' in _uc._SYSTEM)
    results.log("oos: the prompt list falls back to the full list",
                'painting' in _uc._out_of_scope_services(None))
except Exception as e:
    results.log("oos tenant-awareness", False, got=str(e))

# In gate mode we stop here: TEST 0 above is the API-free deterministic
# regression block (every production bug we've fixed is pinned there). The
# TEST 1+ sections below exercise the live LLM's accuracy — valuable as a quality
# signal, but inherently fuzzy, so they are NOT a commit gate.
# ============================================================
# TEST 0 — first contact, out-of-scope declines, and the hard stop
# ============================================================
# Every case here is a lead that died in the last 50 production conversations.

from bot.views.plumbot.response_mixin import (
    build_cold_opener as _bco, build_cold_opener_rule as _bcor,
    strip_known_questions as _skq,
)

# The bare greeting killed 13 of the last 50 leads on turn one (26%). The
# replacement is specific, but under the Price Conditional Rule it carries no
# figures — a greeting is not a price question.
_opener = _bco()
results.log(
    "cold opener: no longer the dead generic greeting",
    "How may we assist you on plumbing services" not in _opener
    and "bathroom and kitchen plumbing" in _opener,
    got=repr(_opener),
)
results.log(
    "cold opener: carries NO price (price conditional rule)",
    "$" not in _opener and "US" not in _opener,
    got=repr(_opener),
)
results.log(
    "cold opener: ends on one this-or-that question, not an open one",
    _opener.rstrip().endswith("?") and _opener.count("?") == 1,
    got=repr(_opener),
)
results.log(
    "cold opener: no emojis in customer-facing copy",
    all(ord(ch) < 0x2190 for ch in _opener),
    got=repr(_opener),
)
_opener_sn = _bco(is_shona=True)
results.log(
    "cold opener: mirrors Shona and stays price-free",
    "Mhoro" in _opener_sn and "$" not in _opener_sn,
    got=repr(_opener_sn),
)
results.log(
    "cold opener: the LLM rule carries the same opener text",
    "bathroom and kitchen plumbing" in _bcor() and "$" not in _bcor(),
    got=repr(_bcor()[:180]),
)

# ── Handler D: the memory check ─────────────────────────────────────────────
# "what area are you in" was the most re-asked question in the last 50
# conversations (4x), and lead 872 was asked for an area it had already given.
class _FakeApptKnown:
    customer_area = "Bulawayo"
    customer_name = "Mrs Ncube"
    project_type = "bathroom_renovation"
    project_description = ""


class _FakeApptBlank:
    customer_area = ""
    customer_name = ""
    project_type = ""
    project_description = ""


_known, _blank = _FakeApptKnown(), _FakeApptBlank()

_r, _caught = _skq("Great, we can sort that. What area are you in?", _known)
results.log(
    "memory check: a known area is never asked for again",
    "What area are you in?" not in _r and "we can sort that" in _r
    and _caught == ['area'],
    got=f"{_r!r} caught={_caught}",
)
_r2, _c2 = _skq("Great, we can sort that. What area are you in?", _blank)
results.log(
    "memory check: an unknown area is still asked for",
    "What area are you in?" in _r2 and _c2 == [],
    got=f"{_r2!r} caught={_c2}",
)
_r3, _c3 = _skq("One last thing, what name should we put on the booking?", _known)
results.log(
    "memory check: a known name is never asked for again (lead 874)",
    "what name" not in _r3.lower() and _c3 == ['name']
    and "Mrs Ncube" in _r3,
    got=f"{_r3!r} caught={_c3}",
)
_r4, _c4 = _skq("Which service are you interested in?", _known)
results.log(
    "memory check: a whole-reply repeat becomes an acknowledgement, never empty",
    _r4.strip() != "" and "?" not in _r4
    and "bathroom renovation" in _r4 and _c4 == ['service'],
    got=f"{_r4!r} caught={_c4}",
)
_r7, _c7 = _skq("What area are you in?", _blank)
results.log(
    "memory check: a first ask with nothing stored is sent untouched",
    _r7 == "What area are you in?" and _c7 == [],
    got=f"{_r7!r} caught={_c7}",
)
_r5, _c5 = _skq("Thanks for that. We'll get you a fixed price on the visit.", _known)
results.log(
    "memory check: a reply that asks nothing is untouched",
    _r5 == "Thanks for that. We'll get you a fixed price on the visit." and _c5 == [],
    got=f"{_r5!r}",
)
# The split marker must survive the guard, or the two-message send collapses.
from bot.views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER as _MK
_r6, _c6 = _skq(f"Noted, thanks.{_MK}What area are you in?", _known)
results.log(
    "memory check: the split marker survives and the re-ask is dropped",
    _r6 == "Noted, thanks." and _c6 == ['area'],
    got=f"{_r6!r} caught={_c6}",
)

# ── Tie-down signatures must match the copy they identify ───────────────────
# The whole sale-progression ladder turns on "was our last turn a tie-down?",
# which is a substring scan of the previous assistant message. If a close is
# reworded and its signature is not, the gate stops recognising the bot's own
# copy: it either stacks two closes or never advances to the booking question.
# These cases make that reword fail the commit gate instead of production.
from bot.views.plumbot.response_mixin import ResponseMixin as _RMT

_banks = {
    '_TIEDOWN_VALUE_CHECK': _RMT._TIEDOWN_VALUE_CHECK,
    '_TIEDOWN_OPENER': _RMT._TIEDOWN_OPENER,
}
_bad = [
    (name, lang, text, sig)
    for name, bank in _banks.items()
    for lang, pairs in bank.items()
    for text, sig in pairs
    if sig.lower() not in text.lower()
]
results.log(
    "tie-down: every rotating close contains its own signature",
    not _bad,
    got=str(_bad),
)

_pair_tables = {
    '_PRICE_TIEDOWN': _RMT._PRICE_TIEDOWN,
    '_BUDGET_FIT_CLOSE': _RMT._BUDGET_FIT_CLOSE,
}
_bad2 = [
    (name, lang, text, sig)
    for name, table in _pair_tables.items()
    for lang, (text, sig) in table.items()
    if sig.lower() not in text.lower()
]
results.log(
    "tie-down: the price and budget closes contain their own signatures",
    not _bad2,
    got=str(_bad2),
)

# The derived list must cover every language in both tables — a new language
# added to a table and forgotten here would be invisible to the gate.
_expected_extra = set()
for _t in _pair_tables.values():
    _expected_extra |= {sig for _, sig in _t.values()}
results.log(
    "tie-down: _EXTRA_TIEDOWN_SIGNATURES is derived, not hand-written",
    set(_RMT._EXTRA_TIEDOWN_SIGNATURES) == _expected_extra,
    got=f"declared={sorted(_RMT._EXTRA_TIEDOWN_SIGNATURES)} expected={sorted(_expected_extra)}",
)
results.log(
    "tie-down: the price signatures resolve from the table",
    set(_RMT._price_tiedown_signatures())
    == {sig for _, sig in _RMT._PRICE_TIEDOWN.values()},
    got=str(_RMT._price_tiedown_signatures()),
)

# The builders must return the table text verbatim, so no caller can drift.
class _FakeSelfTD:
    _PRICE_TIEDOWN = _RMT._PRICE_TIEDOWN
    _BUDGET_FIT_CLOSE = _RMT._BUDGET_FIT_CLOSE
    _lang_key = _RMT._lang_key
    _price_tiedown = _RMT._price_tiedown
    _budget_fit_close = _RMT._budget_fit_close


_td = _FakeSelfTD()
results.log(
    "tie-down: builders return the table copy for both languages",
    _td._price_tiedown('english') == _RMT._PRICE_TIEDOWN['english'][0]
    and _td._price_tiedown('shona') == _RMT._PRICE_TIEDOWN['shona'][0]
    and _td._budget_fit_close('shona') == _RMT._BUDGET_FIT_CLOSE['shona'][0],
    got=repr(_td._price_tiedown('shona')),
)
# An unknown language falls back to English rather than raising mid-reply.
results.log(
    "tie-down: an unknown language falls back to English, never a KeyError",
    _td._price_tiedown('ndebele') == _RMT._PRICE_TIEDOWN['english'][0],
    got=repr(_td._price_tiedown('ndebele')),
)

# ── "Where are you?" is answered with the tenant's own address ──────────────
# Prod lead 863 asked "Your location please" and was answered about email
# delivery: the phrase matched no trigger, and barmak had an address on the
# Profile but no faq_facts['location'], so the fact resolved to None.
import types as _types
from bot.tenant_config import TenantConfig as _TC
from bot.faq import match_faq_topic as _mft


def _locfact(line, short=''):
    return _TC._location_fact(
        _types.SimpleNamespace(location_line=line, location_short=lambda: short))


results.log(
    "location: a bare address is framed into a sentence",
    _locfact('20398 Budiriro 5B Cabs Harare', 'Budiriro, Harare')
    == "We're based at 20398 Budiriro 5B Cabs Harare.",
    got=repr(_locfact('20398 Budiriro 5B Cabs Harare', 'Budiriro, Harare')),
)
results.log(
    "location: a tenant who already wrote a sentence is not re-framed",
    _locfact("We're in Hatfield, Harare.", 'Hatfield, Harare')
    == "We're in Hatfield, Harare.",
    got=repr(_locfact("We're in Hatfield, Harare.", 'Hatfield, Harare')),
)
results.log(
    "location: an address ending in a full stop does not crash",
    _locfact('12 Samora Machel Ave.', 'Harare')
    == "We're based at 12 Samora Machel Ave.",
    got=repr(_locfact('12 Samora Machel Ave.', 'Harare')),
)
results.log(
    "location: falls back to area, city when there is no street address",
    _locfact('', 'Bulawayo CBD') == "We're based in Bulawayo CBD.",
    got=repr(_locfact('', 'Bulawayo CBD')),
)
results.log(
    "location: absent means omit — no address at all yields no fact",
    _locfact('', '') is None,
    got=repr(_locfact('', '')),
)

_loc_asks = ['Your location please', 'your location', 'muri kupi',
             'can i have your location', 'send me your location',
             'where is your shop', 'whereabouts are you', 'where are you based',
             'what is your location', 'nzvimbo yenyu']
results.log(
    "location: every way a lead asks where we are matches the topic",
    all(_mft(m) == 'location' for m in _loc_asks),
    got=str([(m, _mft(m)) for m in _loc_asks if _mft(m) != 'location']),
)
# The customer naming THEIR OWN area must never be read as asking for ours.
_not_loc = ['I am in Budiriro', 'how much is a toilet', 'Rockview park near sunway city',
            'my area is Chitungwiza', 'Helensvale']
results.log(
    "location: a lead stating their own area is not asking for ours",
    all(_mft(m) != 'location' for m in _not_loc),
    got=str([(m, _mft(m)) for m in _not_loc if _mft(m) == 'location']),
)

# ── No function-level import may shadow a module-level one in the webhook ───
# Prod 2026-09-01: a `from bot.repeated_question_detector import
# detect_language_simple` was added inside the hard-stop branch of
# _generate_and_schedule_reply. Python then treats that name as LOCAL for the
# whole function, so every later use — the FAQ language pick, _advance_after_
# scope, the reschedule path — raised UnboundLocalError on any message that did
# not take that branch. Which was most of them: "where are you located" died on
# it in production.
#
# whatsapp_webhook.py is clean, so this is enforced with no allowlist.
import ast as _ast
import io as _io

# utf-8-sig: this file carries a UTF-8 BOM, and ast.parse rejects U+FEFF.
_wh_src = _io.open('bot/whatsapp_webhook.py', encoding='utf-8-sig').read()
_wh_tree = _ast.parse(_wh_src)
_wh_top = set()
for _n in _wh_tree.body:
    if isinstance(_n, (_ast.Import, _ast.ImportFrom)):
        for _a in _n.names:
            _wh_top.add(_a.asname or _a.name.split('.')[0])
# An import at the top of a function always runs, so it is harmless. The
# dangerous shape is an import inside a CONDITIONAL block (if/try/for/while)
# whose name is then used outside that block: the name is local for the whole
# function, but the binding only happens on one path. That is exactly the bug.
def _wh_conditional_shadow(tree, top_names):
    """Uses of a locally-imported module-level name that no import reaches.

    A use is safe when some import of that name either runs unconditionally in
    the function, or sits in the same conditional block as the use (that is the
    send_previous_work_photos shape: `if appointment is not None:` imports
    timezone and uses it two lines later). It is dangerous when the only import
    is in a branch the use is NOT inside — the name is local to the whole
    function, but only bound on one path.
    """
    _BLOCKS = (_ast.If, _ast.Try, _ast.For, _ast.While, _ast.With)
    bad = []
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        # Every local import of a shadowing name, with the line-set of the
        # conditional block enclosing it (None = unconditional in this function).
        imports = []
        for stmt in _ast.walk(fn):
            if not isinstance(stmt, (_ast.Import, _ast.ImportFrom)):
                continue
            for alias in stmt.names:
                name = alias.asname or alias.name.split('.')[0]
                if name not in top_names:
                    continue
                scope = None
                for blk in _ast.walk(fn):
                    if isinstance(blk, _BLOCKS):
                        lines = {n.lineno for n in _ast.walk(blk)
                                 if hasattr(n, 'lineno')}
                        if stmt.lineno in lines:
                            scope = lines if scope is None else (scope & lines)
                imports.append((name, stmt.lineno, scope))
        if not imports:
            continue
        for use in _ast.walk(fn):
            if not isinstance(use, _ast.Name) or use.id not in top_names:
                continue
            reached = any(
                nm == use.id and ln <= use.lineno and (sc is None or use.lineno in sc)
                for nm, ln, sc in imports
            )
            if not reached and any(nm == use.id for nm, _, _ in imports):
                bad.append((fn.name, use.id, use.lineno))
    return bad


_shadowed = _wh_conditional_shadow(_wh_tree, _wh_top)
results.log(
    "webhook: no conditional import shadows a module-level name used elsewhere",
    not _shadowed,
    expected="none — a conditional import binds on one path but is local to all",
    got=str(_shadowed),
)

# ── A defer is asked for a DATE before it is asked for an email ─────────────
# Prod lead 881 (+263786318169, Barmak): "Ndiri kuchitungwiza ndichakubatayi
# ndapedza kuronga nyaya dze mari" — I'm in Chitungwiza, I'll get back to you
# once I've sorted the money — was answered "What's the best email for it?"
# without anyone ever asking when. A self-initiated defer used to skip the
# timeframe question entirely. A date books the follow-up; an email asked for
# out of nowhere reads as an extraction.
from bot.out_of_scope_handler import _reask_delay_timeframe as _rdt


class _FakeDeferAppt:
    pk = 881
    customer_email = ''
    internal_notes = ''
    delay_followup_due_at = None
    is_delayed = False

    def save(self, update_fields=None):
        pass

    def mark_delayed(self, source_message=None, save=True):
        self.is_delayed = True
        return True


_defer_msg = 'Ndiri kuchitungwiza ndichakubatayi ndapedza kuronga nyaya dze mari'
_da = _FakeDeferAppt()
_turn1 = _rdt(_defer_msg, _da)
results.log(
    "defer: a self-initiated defer is asked for a date, not an email",
    'when are you thinking' in _turn1.lower() and 'email' not in _turn1.lower(),
    got=repr(' '.join(_turn1.split())[:130]),
)
_turn2 = _rdt('will let you know', _da)
results.log(
    "defer: the SECOND miss pivots to the catalog/email offer",
    'email' in _turn2.lower() and 'when are you thinking' not in _turn2.lower(),
    got=repr(' '.join(_turn2.split())[:130]),
)
results.log(
    "defer: nobody is asked 'when?' twice",
    not ('when are you thinking' in _turn1.lower()
         and 'when are you thinking' in _turn2.lower()),
    got='turn1/turn2 both ask when' ,
)

# ── A check-back inside the free window is done on WhatsApp, not by email ───
# If we can reach them here at the moment they named, an email buys nothing —
# and it is the ask leads push back on ("just send it here, I don't usually
# have data"). Outside the window email is the only way, so it is still asked.
from datetime import timedelta as _td
from django.utils import timezone as _tz
from bot.out_of_scope_handler import _handle_delay_timeframe_answer as _hdta


class _FakeWindowAppt(_FakeDeferAppt):
    project_type = 'Bathroom Renovation'

    def __init__(self, closes_in_hours):
        self.internal_notes = ''
        self.free_messaging_window_closes_at = _tz.now() + _td(hours=closes_in_hours)


# The date parser is an LLM call and returns nothing under the offline stub, so
# it is pinned here — this case is about the CHANNEL branch, not about parsing.
import bot.out_of_scope_handler as _oos
_real_compute = _oos._compute_followup_date
try:
    def _fixed_date(_msg, _days=None):
        d = (_tz.now() + _td(days=2)).date()
        return d.isoformat(), d.strftime('%A %d %B')

    _oos._compute_followup_date = _fixed_date

    _inside = _FakeWindowAppt(72)      # window open for three more days
    _r_in = _hdta('ndichakubatayi', {}, _inside)
    results.log(
        "check-back: inside the free window we say we'll message here, no email ask",
        'email' not in _r_in.lower() and 'check back with you' in _r_in.lower()
        and '[DELAY_CHANNEL] whatsapp' in _inside.internal_notes,
        got=repr(' '.join(_r_in.split())[:140]),
    )
    results.log(
        "check-back: the WhatsApp confirmation still names the agreed day",
        any(d in _r_in for d in ('Monday', 'Tuesday', 'Wednesday', 'Thursday',
                                 'Friday', 'Saturday', 'Sunday')),
        got=repr(' '.join(_r_in.split())[:140]),
    )

    _outside = _FakeWindowAppt(4)      # window shuts tonight
    _r_out = _hdta('ndichakubatayi', {}, _outside)
    results.log(
        "check-back: outside the window the email is still asked for",
        'email' in _r_out.lower()
        and '[DELAY_CHANNEL] whatsapp' not in _outside.internal_notes,
        got=repr(' '.join(_r_out.split())[:140]),
    )
finally:
    _oos._compute_followup_date = _real_compute

# ── We say WHEN in the lead's own words, not as a diary entry ───────────────
# Prod 2026-09-01: "Let me update you tomorrow morning" was answered "We'll
# check back with you right here on Wednesday 02 September". Accurate, and the
# one line that made the whole thread read as a machine. Mirror their wording;
# keep the formal date only for dates too far out to name naturally.
import re as _re
from datetime import date as _pdate
from bot.out_of_scope_handler import (
    _checkback_when_phrase as _cwp, _named_daypart as _ndp,
)

_WHEN_TODAY = _pdate(2026, 9, 1)          # a Tuesday
WHEN_CASES = [
    # (agreed iso date, the customer's own words, the phrase we say back)
    ('2026-09-02', 'Let me update you tomorrow morning', 'tomorrow morning'),
    ('2026-09-02', 'tomorrow',                           'tomorrow'),
    ('2026-09-01', 'later today in the evening',         'this evening'),
    ('2026-09-01', 'tonight',                            'tonight'),
    ('2026-09-04', 'Friday afternoon',                   'on Friday afternoon'),
    ('2026-09-04', 'Friday',                             'on Friday'),
    ('2026-09-05', 'over the weekend',                   'on Saturday'),
    # A bare clock time is an hour, not a daypart word — don't echo one back.
    ('2026-09-04', 'Friday at 2',                        'on Friday'),
    # Far enough out that a weekday alone is ambiguous: date, and the daypart
    # drops with it ("on Wednesday 30 September morning" is not a sentence).
    ('2026-09-30', 'end of the month, in the morning',   'on Wednesday 30 September'),
]
for _iso, _said, _want in WHEN_CASES:
    try:
        _got = _cwp(_iso, _said, today=_WHEN_TODAY)
        results.log(
            f"check-back phrase: {_said[:34]!r} -> {_want!r}",
            _got == _want, expected=_want, got=str(_got),
        )
    except Exception as _e:
        results.log(f"check-back phrase: {_said[:34]!r}", False, got=str(_e))

results.log(
    "check-back phrase: bare form drops the preposition for subject position",
    _cwp('2026-09-04', 'Friday morning', bare=True, today=_WHEN_TODAY) == 'Friday morning',
    got=str(_cwp('2026-09-04', 'Friday morning', bare=True, today=_WHEN_TODAY)),
)
results.log(
    "check-back phrase: a Shona lead hears it in Shona",
    _cwp('2026-09-02', 'mangwana mangwanani', is_shona=True, today=_WHEN_TODAY)
    == 'mangwana mangwanani',
    got=str(_cwp('2026-09-02', 'mangwana mangwanani', is_shona=True, today=_WHEN_TODAY)),
)
results.log(
    "check-back phrase: 'fortnight' is not a daypart",
    _ndp('in a fortnight') is None and _ndp('tomorrow night') == 'night',
    got=f"fortnight->{_ndp('in a fortnight')}, tomorrow night->{_ndp('tomorrow night')}",
)

# End to end: the reply itself carries the lead's words, and no formal date.
_real_defer = _oos._is_self_initiated_defer
try:
    def _tomorrow_date(_msg, _days=None):
        d = (_tz.now() + _td(days=1)).date()
        return d.isoformat(), d.strftime('%A %d %B')

    _oos._compute_followup_date    = _tomorrow_date
    _oos._is_self_initiated_defer  = lambda _m: True     # they said THEY'd update us

    _appt = _FakeWindowAppt(72)
    _reply = _hdta('Let me update you tomorrow morning', {}, _appt)
    results.log(
        "check-back reply: mirrors 'tomorrow morning', no formal date, no 'right here'",
        ('check back with you tomorrow morning' in _reply.lower()
         and 'right here' not in _reply.lower()
         and not _re.search(r'\d{1,2}\s+(January|February|March|April|May|June|July|'
                            r'August|September|October|November|December)', _reply)),
        got=repr(' '.join(_reply.split())[:150]),
    )
finally:
    _oos._compute_followup_date   = _real_compute
    _oos._is_self_initiated_defer = _real_defer

# The keyword fallback must catch "let me update you" on its own — before this
# it fell through and only the LLM parked the lead.
results.log(
    "self-initiated defer (keyword fallback): 'let me update you tomorrow'",
    _is_self_initiated_defer_keywords('Let me update you tomorrow morning')
    and _is_self_initiated_defer_keywords('I will update you')
    and not _is_self_initiated_defer_keywords('tomorrow morning works'),
    got=str(_is_self_initiated_defer_keywords('Let me update you tomorrow morning')),
)

# ── A new build is confirmed back, not run through "bathroom or kitchen?" ───
# A structure with no plumbing in it yet is a different job from a refit, so
# the lead who says "new house" / "new building" gets the scope confirmed in
# their own noun — one micro-yes — instead of a this-or-that they can't answer
# or a "tell me more about the project" that throws their own words back.
class _FakeNBAppt:
    project_type = None

    def __init__(self, history=None, project_type=None,
                 scheduled_datetime=None, status='pending'):
        self.conversation_history = history or []
        self.project_type = project_type
        self.scheduled_datetime = scheduled_datetime
        self.status = status

    def save(self, update_fields=None):
        pass


# The default fake is MID-conversation: the greeting only leads on a lead's
# very first turn, so a fake with an empty history would silently test the
# first-contact branch everywhere.
_NB_MIDCONVO = [
    {'role': 'user', 'content': 'Hello'},
    {'role': 'assistant', 'content': 'Hello, How may we assist you on plumbing services'},
    {'role': 'user', 'content': 'It is a new building'},
]


class _FakeNB(ResponseMixin):
    def __init__(self, nq='service_type', history=None, underway=True,
                 project_type=None, scheduled_datetime=None, status='pending'):
        self.appointment = _FakeNBAppt(
            list(_NB_MIDCONVO) if history is None else history,
            project_type, scheduled_datetime, status)
        self._nq, self._underway = nq, underway

    def get_next_question_to_ask(self):
        return self._nq

    def _conversation_underway(self):
        return self._underway


_nb = _FakeNB()
NEW_BUILD_CASES = [
    # (message, the noun we say back — None means the flow is untouched)
    ("It's a new building and we require installation of all the plumbing "
     "requirements on the plan",                              'building'),
    ('l just need all the services needed on a new house',     'house'),
    ("I'm building a new house in Ruwa, 3 bathrooms",          'house'),
    ('building a house in Norton',                             'house'),
    ('the house is still under construction',                  'house'),
    ('brand new property',                                     'property'),
    ('newly built home',                                       'home'),
    ('imba itsva',                                             'house'),
    ('ndiri kuvaka imba',                                      'house'),
    # Must NOT fire: a suburb whose name ends in "extension", a refit, a
    # single fixture, and a ROOM done from scratch (that is not a new house).
    ('Dzivarasekwa extension',                                 None),
    ('I need a new bathroom in my house',                      None),
    ('renovating my house',                                    None),
    ('new shower cubicle',                                     None),
    ('how much for a new toilet',                              None),
    ('doing the bathroom from scratch',                        None),
]
for _msg, _want in NEW_BUILD_CASES:
    _got = _nb._new_build_subject_fallback(_msg)
    results.log(
        f"new build (offline fallback): {_msg[:34]!r} -> {_want}",
        _got == _want, expected=str(_want), got=str(_got),
    )

# ── DeepSeek is the primary; the regex is add-only underneath it ─────────────
# The classifier answers off the `new_build` field of the SAME unified_classify
# result the turn already computed — no extra round trip — and it EARNS its
# place: "Cost of wiring a new 4 bedroom house" has words between "new" and
# "house", which the adjacency-bound fallback cannot match without also
# swallowing "a new bathroom in my house".
from bot.unified_classifier import uc_new_build as _ucnb

results.log(
    "new build: the classifier widens what the regex cannot reach",
    _nb._new_build_subject_fallback('Cost of wiring a new 4 bedroom house') is None
    and _nb._new_build_subject(
        'Cost of wiring a new 4 bedroom house',
        {'new_build': 'house'}) == 'house',
    got=str(_nb._new_build_subject('Cost of wiring a new 4 bedroom house',
                                   {'new_build': 'house'})),
)
results.log(
    "new build: the classifier can never CLOSE a phrase the fallback knows",
    # Add-only (the symmetry rule): missing a build DECLINES a live lead, while
    # over-firing costs one waved-off question. A null/absent verdict, a failed
    # call and a disagreeing call all leave the fallback's answer standing.
    _nb._new_build_subject("It's a new building", {'new_build': None}) == 'building'
    and _nb._new_build_subject("It's a new building", None) == 'building'
    and _nb._new_build_subject("It's a new building", {}) == 'building',
    got=str(_nb._new_build_subject("It's a new building", {'new_build': None})),
)
results.log(
    "new build: a failed classification is 'no answer', never a definite no",
    _ucnb(None) is None and _ucnb({}) is None and _ucnb({'new_build': None}) is None,
    got=f"{_ucnb(None)}/{_ucnb({})}/{_ucnb({'new_build': None})}",
)
results.log(
    "new build: the accessor takes the noun out of a wordy answer",
    _ucnb({'new_build': 'a new house'}) == 'house'
    and _ucnb({'new_build': ' Building '}) == 'building'
    and _ucnb({'new_build': 'duplex'}) == 'house',   # unlisted noun still counts
    got=str(_ucnb({'new_build': 'a new house'})),
)

results.log(
    "new build: the confirmation is the approved script, in their own noun",
    (_nb._new_build_confirm_question('house')
     == 'So you need a new plumbing installation for a new house?'
     and _nb._new_build_confirm_question('building')
     == 'So you need a new plumbing installation for a new building?'),
    got=_nb._new_build_confirm_question('building'),
)

# The whole gate: fires once, records the service type so a "yes" advances,
# then never fires again — and stays out of the later stages entirely.
_nb1 = _FakeNB(nq='service_type')
_r1 = _nb1._new_build_confirmation("It's a new building, plumbing on the plan")
results.log(
    "new build: confirmed at the scope stage, and the service type is recorded",
    _r1 == 'So you need a new plumbing installation for a new building?'
    and _nb1.appointment.project_type == 'New Plumbing Installation',
    got=f"{_r1!r} / {_nb1.appointment.project_type!r}",
)
_nb2 = _FakeNB(nq='project_description', history=[
    {'role': 'assistant',
     'content': 'So you need a new plumbing installation for a new house?'},
])
results.log(
    "new build: never asked twice",
    _nb2._new_build_confirmation('yes, a new house') is None,
    got=str(_nb2._new_build_confirmation('yes, a new house')),
)

# The stage is NOT the gate — a booked slot is. Prod 2026-09-01: the lead who
# answered our wiring clarification with "It's a new building" already had a
# project_type and a (wiring) project_description captured off their opener, so
# the flow sat on `area` and they got "All good, what area are you in?".
_nb_area = _FakeNB(nq='area')._new_build_confirmation("It's a new building")
results.log(
    "new build: confirmed even when the flow has moved on to the area",
    _nb_area == 'So you need a new plumbing installation for a new building?',
    got=repr(_nb_area),
)
results.log(
    "new build: a captured description does not suppress it",
    _FakeNB(nq='availability_date')._new_build_confirmation('new house')
    == 'So you need a new plumbing installation for a new house?',
    got=repr(_FakeNB(nq='availability_date')._new_build_confirmation('new house')),
)
results.log(
    "new build: never raised over a lead who has already booked a slot",
    _FakeNB(nq='area', scheduled_datetime='2026-09-04T09:00')
    ._new_build_confirmation('new house') is None
    and _FakeNB(nq='area', status='confirmed')
    ._new_build_confirmation('new house') is None,
    got='committed-lead gate',
)

# ── "No" to the request for project detail ──────────────────────────
# Prod probe 2026-09-01: the retry path re-sent the question verbatim with a
# second one bolted on — "Can you tell me a bit more about the project? Or is
# it a simple fix or a full install?" — repeating a question AND stacking two.
# They will not elaborate and we do not need them to: the visit prices whatever
# is there. Record what we know and move to the next field.
_DETAIL_ASK = [{'role': 'assistant',
                'content': 'Got it! Can you tell me a bit more about the project?'}]


class _FakeDetail(_FakeNB):
    def _advance_after_scope(self, language='english'):
        return 'All good, what area are you in?'


_d_no = _FakeDetail(nq='project_description', history=list(_DETAIL_ASK),
                    project_type='new_plumbing_installation')
_d_out = _d_no._handle_no_to_detail_request('No')
results.log(
    "detail 'no': the flow advances instead of re-asking",
    _d_out == 'All good, what area are you in?',
    got=repr(_d_out),
)
results.log(
    "detail 'no': what we already know becomes the description, nothing is lost",
    _d_no.appointment.project_description == 'new plumbing installation',
    got=repr(_d_no.appointment.project_description),
)
results.log(
    "detail 'no': a real answer is never swallowed by this branch",
    _FakeDetail(nq='project_description', history=list(_DETAIL_ASK))
    ._handle_no_to_detail_request('No tiling, just the tub and shower') is None,
    got='carries detail',
)
results.log(
    "detail 'no': only right after WE asked for detail",
    _FakeDetail(nq='project_description', history=[
        {'role': 'assistant', 'content': 'All good, what area are you in?'}])
    ._handle_no_to_detail_request('No') is None,
    got='ask gate',
)

# ── "No" / "neither" to a day or time offer ─────────────────────────
# Prod probe 2026-09-01: "No" to "what works better: 9AM or 2PM?" was answered
# "9AM or 2PM tomorrow?", and "No" to the day offer got an improvised "you're
# not keen on either tomorrow or Thursday?" — the same question again either
# way. The slots we named don't work, so the question opens up.
_DAY_OFFER = [{'role': 'assistant', 'content':
               'Great, what works better for you, tomorrow or this Thursday, '
               'for us to come through and have a quick look at the bathroom?'}]
_TIME_OFFER = [{'role': 'assistant', 'content':
                'Perfect, for tomorrow — what works better: 9AM or 2PM?'}]

results.log(
    "slot offer 'no': the DAY question opens up instead of repeating",
    _FakeNB(nq='availability_date', history=list(_DAY_OFFER))
    ._handle_no_to_slot_offer('No') == 'No problem. What day would suit you better?',
    got=repr(_FakeNB(nq='availability_date', history=list(_DAY_OFFER))
             ._handle_no_to_slot_offer('No')),
)
results.log(
    "slot offer 'neither': the TIME question opens up instead of repeating",
    _FakeNB(nq='availability_time', history=list(_TIME_OFFER))
    ._handle_no_to_slot_offer('neither')
    == 'No problem. What time would suit you better that day?',
    got=repr(_FakeNB(nq='availability_time', history=list(_TIME_OFFER))
             ._handle_no_to_slot_offer('neither')),
)
results.log(
    "slot offer: a no that CARRIES the answer goes to the date parser",
    _FakeNB(nq='availability_date', history=list(_DAY_OFFER))
    ._handle_no_to_slot_offer('No, Friday please') is None,
    got='carries an answer',
)
results.log(
    "slot offer: only fires when we actually offered slots",
    _FakeNB(nq='availability_date', history=[
        {'role': 'assistant', 'content': 'All good, what area are you in?'}])
    ._handle_no_to_slot_offer('No') is None
    and _FakeNB(nq='area', history=list(_DAY_OFFER))
    ._handle_no_to_slot_offer('No') is None,
    got='offer gate',
)

# ── A scripted question must record that it was asked ───────────────────────
# _quote_route_followup was the only path emitting a first-pass question
# without _set_question_retry_count, so retry_count stayed 0 and the NEXT turn
# re-emitted the identical string. Reproduced on three separate conversations:
# a lead who did not answer the quote pitch's "What area are you in?" got it
# back word for word — a bot loop on every non-answer, not only on a "no".
import inspect as _insp
_qrf_src = _insp.getsource(ResponseMixin._quote_route_followup)
results.log(
    "quote route: the scripted question it sends is recorded as asked",
    _qrf_src.count('_set_question_retry_count') >= 2,
    expected='>=2 (the scripted branch and the area fallback)',
    got=str(_qrf_src.count('_set_question_retry_count')),
)

# ── "No" to the confirmation is an answer, not noise ────────────────────────
# Prod 2026-09-01: the no was not detected at all — the flow moved straight on
# to "All good, what area are you in?", booking a visit for a job we could not
# name, on a service type the lead had just rejected (and which WE wrote
# presumptively in order to ask the question).
_NB_CONFIRMED = [{'role': 'assistant',
                  'content': 'So you need a new plumbing installation for a new building?'}]

_nb_no = _FakeNB(nq='area', history=list(_NB_CONFIRMED),
                 project_type='New Plumbing Installation')
_nb_no.appointment.project_description = 'Cost of wiring a new 4 bedroom house'
_nb_no_reply = _nb_no._handle_new_build_rejection('No')
results.log(
    "new build 'no': the lead is asked what the plumbing job actually is",
    _nb_no_reply == "Ah, my mistake. What's the plumbing side you're looking to get sorted?",
    got=repr(_nb_no_reply),
)
results.log(
    "new build 'no': the presumptive service type and the misread description go",
    _nb_no.appointment.project_type is None
    and _nb_no.appointment.project_description is None,
    got=f"{_nb_no.appointment.project_type!r} / {_nb_no.appointment.project_description!r}",
)

# A no that CARRIES the correction falls through — the fields are cleared by
# then, so the normal flow reads what they said instead of us guessing twice.
_nb_corr = _FakeNB(nq='area', history=list(_NB_CONFIRMED),
                   project_type='New Plumbing Installation')
_nb_corr.appointment.project_description = 'Cost of wiring a new 4 bedroom house'
results.log(
    "new build 'no, it's a renovation': falls through with the guess cleared",
    _nb_corr._handle_new_build_rejection("No, it's a renovation of my bathroom") is None
    and _nb_corr.appointment.project_type is None
    and _nb_corr.appointment.project_description is None,
    got=f"{_nb_corr.appointment.project_type!r}",
)

# A YES is not a rejection, and nothing is cleared.
_nb_yes = _FakeNB(nq='area', history=list(_NB_CONFIRMED),
                  project_type='New Plumbing Installation')
results.log(
    "new build 'yes': nothing is cleared and the flow carries on",
    _nb_yes._handle_new_build_rejection('Yes') is None
    and _nb_yes.appointment.project_type == 'New Plumbing Installation',
    got=repr(_nb_yes.appointment.project_type),
)
results.log(
    "new build 'no': only answers to OUR confirmation count",
    _FakeNB(nq='area')._last_assistant_was_new_build_confirm() is False
    and _FakeNB(nq='area', history=list(_NB_CONFIRMED))
    ._last_assistant_was_new_build_confirm() is True,
    got='last-turn gate',
)
results.log(
    "new build 'no': bare negatives only, in both languages",
    all(_nb._is_bare_negative(m) for m in ('No', 'nope', 'Nah.', 'kwete', 'not really'))
    and not any(_nb._is_bare_negative(m) for m in
                ("No, it's a renovation", 'no bathroom yet', 'nothing else')),
    got=str(_nb._is_bare_negative('No')),
)
# A second "no" cannot loop: our last turn is now the scope question.
results.log(
    "new build 'no': the rejection reply cannot be re-triggered by another no",
    _FakeNB(nq='service_type', history=[
        {'role': 'assistant',
         'content': "Ah, my mistake. What's the plumbing side you're looking to get sorted?"},
    ])._last_assistant_was_new_build_confirm() is False,
    got='no loop',
)

# First contact is counted off the lead's OWN turns, not _conversation_underway
# — that short-circuits on a filled project_type, and classify_and_save fills
# one from "a new house" before this runs, so an OPENING message was treated as
# mid-conversation and lost its greeting (prod probe 2026-09-01).
results.log(
    "new build: first contact still greets before confirming",
    _FakeNB(history=[{'role': 'user', 'content': 'I want to build a new house'}],
            project_type='New Plumbing Installation')
    ._new_build_confirmation('I want to build a new house')
    == 'Hello,\n\nSo you need a new plumbing installation for a new house?',
    got=repr(_FakeNB(history=[{'role': 'user', 'content': 'I want to build a new house'}],
                     project_type='New Plumbing Installation')
             ._new_build_confirmation('I want to build a new house')),
)
results.log(
    "new build: no greeting once the lead has spoken before",
    _FakeNB(history=[{'role': 'user', 'content': 'Hello'},
                     {'role': 'assistant', 'content': 'Hello, How may we assist you'},
                     {'role': 'user', 'content': 'I want to build a new house'}])
    ._new_build_confirmation('I want to build a new house')
    == 'So you need a new plumbing installation for a new house?',
    got=repr(_FakeNB(history=[{'role': 'user', 'content': 'Hello'},
                              {'role': 'assistant', 'content': 'Hello, How may we assist you'},
                              {'role': 'user', 'content': 'I want to build a new house'}])
             ._new_build_confirmation('I want to build a new house')),
)
results.log(
    "new build: a Shona lead is confirmed in Shona",
    _FakeNB()._new_build_confirmation('ndiri kuvaka imba itsva')
    == 'Saka muri kuda plumbing itsva yeimba itsva?',
    got=repr(_FakeNB()._new_build_confirmation('ndiri kuvaka imba itsva')),
)

# ── A new build beats the out-of-scope hold ─────────────────────────────────
# Prod 2026-09-01: "Cost of wiring a new 4 bedroom house" -> we asked whether
# the wiring was plumbing-related -> "It's a new building" -> DECLINED, and a
# live new-build lead was sent off to find a specialist. The wiring isn't ours;
# the building is. Same recurring bug: a holding state outranking the
# customer's own words.
from bot.out_of_scope_handler import (
    _resolve_pending_clarification as _rpc, _mentions_new_build as _mnb,
)


class _FakeOOSAppt:
    internal_notes = ''
    conversation_history = []

    def save(self, update_fields=None):
        pass


_real_classify = _oos.classify_message
try:
    # Pin the classifier: this case is about what we do with an out_of_scope
    # verdict, not about reaching one.
    _oos.classify_message = lambda *_a, **_k: {
        'category': 'out_of_scope', 'confidence': 'HIGH', 'detail': 'electrical',
    }
    _pending = {'category': 'out_of_scope',
                'original': 'Cost of wiring a new 4 bedroom house'}
    _out = _rpc("It's a new building", dict(_pending), _FakeOOSAppt())
    results.log(
        "oos: a new build named in the answer is never declined",
        _out is None, expected='None (back to the booking flow)', got=repr(_out),
    )
    # The classifier reaches the decline too — an answer the regex cannot read
    # ("we've just finished the slab") still stops it, off the same result.
    _ai_out = _rpc("we've just finished the slab", dict(_pending), _FakeOOSAppt(),
                   {'new_build': 'house'})
    results.log(
        "oos: the classifier's new_build signal stops the decline as well",
        _ai_out is None, expected='None', got=repr(_ai_out),
    )
    # The guard is narrow: an answer with no build in it still declines, or the
    # module would never turn anyone away again.
    _still_oos = _rpc("No, it's the electrical wiring", dict(_pending), _FakeOOSAppt())
    results.log(
        "oos: an answer with no build in it is still declined",
        _still_oos is not None and 'outside what we do' in (_still_oos or ''),
        got=repr(' '.join((_still_oos or '').split())[:110]),
    )
finally:
    _oos.classify_message = _real_classify

results.log(
    "oos: the decline and the confirmation share one new-build resolver",
    _mnb("It's a new building") and _mnb('a new house')
    and not _mnb('Dzivarasekwa extension') and not _mnb('the electrical wiring'),
    got=str(_mnb("It's a new building")),
)

# ── Price answers lead with labour, then the supplied-too figure ────────────
from bot.pricing_copy import _tenant_item_block as _tib
import types as _ty

_row = _ty.SimpleNamespace(label='Toilet install', short_label='', family='toilet',
                           supply=90, labour=50, allin=140, flat=None)
_cfgp = _ty.SimpleNamespace(currency='US$')
_row.variant = ''
_blk = _tib(_cfgp, _row)
results.log(
    "price answer: labour to install X, then the supplied-too figure",
    _blk['total_line'] == ('Labour to install a toilet is US$50. '
                           'The toilet on its own is from US$90, '
                           'so US$140 all-in.'),
    got=repr(_blk['total_line']),
)
# The noun comes from the FAMILY, not the tenant's label, so "Toilet install"
# never becomes "install a Toilet install".
results.log(
    "price answer: the noun is the family's, not the tenant's label",
    'Toilet install' not in _blk['total_line'],
    got=repr(_blk['total_line']),
)
# A variant that changes the everyday noun is honoured.
_row_fs = _ty.SimpleNamespace(label='Freestanding tub', short_label='', family='tub',
                              variant='freestanding', supply=270, labour=50,
                              allin=320, flat=None)
results.log(
    "price answer: a variant with its own noun is used",
    _tib(_cfgp, _row_fs)['total_line'].startswith(
        'Labour to install a freestanding tub is US$50.'),
    got=repr(_tib(_cfgp, _row_fs)['total_line']),
)
results.log(
    "price answer: carries the rough-guide caveat and no bullets",
    _blk['breakdown_lines'] == []
    and 'rough guide' in _blk['cheapest_line']
    and 'visit' in _blk['cheapest_line'],
    got=repr(_blk['cheapest_line']),
)
# A label like "Element replacement" must not be forced into "install a ...".
_row2 = _ty.SimpleNamespace(label='Element replacement', short_label='',
                            family='geyser_service', variant='element',
                            supply=10, labour=30, allin=40, flat=None)
results.log(
    "price answer: a repair/service never becomes 'install a <service>'",
    _tib(_cfgp, _row2)['total_line'] ==
    'Element replacement: labour from US$30, parts from US$10, so from US$40 all-in.',
    got=repr(_tib(_cfgp, _row2)['total_line']),
)
# Every family the "install a" wording claims must actually read as English:
# the noun must be a bare noun phrase, never a label carrying a verb.
import re
# The supply figure is quoted separately ONLY when it reconciles with the
# all-in. One tenant's freestanding tub is supply 150 + labour 50 but 320
# all-in (the all-in bundles a mixer), so naming all three would publish
# arithmetic that does not add up. Those keep the two-figure form.
_row_odd = _ty.SimpleNamespace(label='Freestanding tub', short_label='', family='tub',
                               variant='freestanding', supply=150, labour=50,
                               allin=320, flat=None)
_odd_line = _tib(_cfgp, _row_odd)['total_line']
results.log(
    "price answer: a bundle still quotes its supply price on its own",
    'US$150' in _odd_line and 'US$50' in _odd_line and 'US$320' in _odd_line,
    got=repr(_odd_line),
)
results.log(
    "price answer: a bundle never CLAIMS a sum ('so X all-in')",
    'so US$' not in _odd_line,
    got=repr(_odd_line),
)
# The real invariant: wherever the copy joins the figures with "so <total>
# all-in", that claim must be true. Stating three figures separately is fine;
# asserting a sum that does not hold is not.
_money = re.compile(r'US\$(\d+)')
_sum_errors = []
for _f, _v, _s, _l, _a in (
    ('toilet', '', 90, 50, 140), ('shower', '', 130, 40, 170),
    ('basin', '', 35, 30, 65), ('geyser', '', 120, 80, 200),
    ('tub', 'freestanding', 150, 50, 320),
    ('geyser_service', 'element', 10, 30, 40),
    ('repair', 'cistern', 40, 20, 60),
):
    _it = _ty.SimpleNamespace(label=_f.title(), short_label='', family=_f,
                              variant=_v, supply=_s, labour=_l, allin=_a, flat=None)
    _ln = _tib(_cfgp, _it)['total_line']
    if 'so US$' not in _ln:
        continue                      # no sum claimed, nothing to verify
    _fig = [int(x) for x in _money.findall(_ln)]
    if len(_fig) == 3 and _fig[0] + _fig[1] != _fig[2]:
        _sum_errors.append((_f, _v, _ln))
results.log(
    "price answer: any sum the copy claims actually adds up",
    not _sum_errors,
    got=str(_sum_errors),
)

from bot.pricing_copy import _INSTALL_NOUNS as _NOUNS
_bad_nouns = [n for n in set(_NOUNS.values())
              if re.search(r'\b(install|installation|supply|replacement|repair|'
                           r'service|per|&)\b', n, re.I)]
results.log(
    "price answer: no install-noun carries a verb or a service word",
    not _bad_nouns,
    got=str(_bad_nouns),
)
# The disclaimer must not stack on top of the block's own caveat.
from bot.views.plumbot.response_mixin import ResponseMixin as _RMD


class _FakeSelfDisc:
    _PRICED_INTENTS = _RMD._PRICED_INTENTS
    _ensure_price_disclaimer = _RMD._ensure_price_disclaimer


_priced_reply = ('Toilet install: labour from US$50. If we supply it too, from '
                 'US$140 all-in.\n\nThis is a rough guide, we confirm the exact '
                 'price on a visit.')
results.log(
    "price answer: the shared disclaimer does not stack a second caveat",
    _FakeSelfDisc()._ensure_price_disclaimer('toilet', _priced_reply) == _priced_reply,
    got=repr(_FakeSelfDisc()._ensure_price_disclaimer('toilet', _priced_reply)),
)

# ── Consultation fee: never promise a visit is free when it is not ──────────
import re
from bot.views.plumbot.response_mixin import strip_free_visit_claims as _sfv
import bot.tenant_config as _tcmod


class _CfgFee:
    def __init__(self, fee):
        self.fee = fee
        self.currency = 'US$'

    def visit_is_free(self):
        return not self.fee

    @property
    def consultation_fee(self):
        return self.fee

    def visit_cost_sentence(self, is_shona=False):
        return '' if not self.fee else f'The call-out to quote is US${self.fee}.'

    # The once-only rule reads both wordings of the same fact (the opening
    # note and the sentence), so the fake has to expose both.
    def visit_fee_waived_on_job(self):
        return False

    def visit_price_note(self, is_shona=False):
        return ('Just a quick note: *the call-out is free.*' if not self.fee
                else f'Just a quick note: *US${self.fee} call-out fee.*')


_free_claims = [
    'We give you an exact, all-in quote free on a quick on-site visit.',
    'The visit is free and takes about 20 minutes.',
    'Our site visit and quotation are provided free of charge.',
]
_appt_stub = _ty.SimpleNamespace(tenant=None)
_real_gc = _tcmod.get_config
try:
    # Default: no fee set anywhere -> completely inert.
    _tcmod.get_config = lambda tenant=None: _CfgFee(None)
    results.log(
        "consultation fee: absent means every reply is untouched",
        all(_sfv(s, _appt_stub) == (s, False) for s in _free_claims),
        got=str([_sfv(s, _appt_stub) for s in _free_claims]),
    )
    # Fee set -> no reply may still call the visit free.
    _tcmod.get_config = lambda tenant=None: _CfgFee(20)
    _outs = [_sfv(s, _appt_stub)[0] for s in _free_claims]
    results.log(
        "consultation fee: no surviving 'free' claim about the visit",
        not any(re.search(r'\bfree\b', o, re.I) for o in _outs),
        got=str(_outs),
    )
    results.log(
        "consultation fee: the figure is stated instead",
        all('US$20' in o for o in _outs),
        got=str(_outs),
    )
    # A visit offer with no free claim still gets the fee stated once.
    _offer = ('What works better, tomorrow or Friday, for us to come round '
              'and look at the space?')
    _o, _c = _sfv(_offer, _appt_stub)
    results.log(
        "consultation fee: a visit offer states the fee once, keeping the question",
        _c and _o.count('US$20') == 1 and _offer in _o,
        got=repr(_o),
    )
    # "Freestanding" is not "free", and a price reply is not a visit claim.
    for _untouched in ('Freestanding tubs from US$670 all-in.',
                       'Toilet install: labour from US$50.'):
        results.log(
            f"consultation fee: leaves unrelated copy alone ({_untouched[:22]}...)",
            _sfv(_untouched, _appt_stub) == (_untouched, False),
            got=str(_sfv(_untouched, _appt_stub)),
        )
    # The FIGURE is stated once too, on the same rule as the word "free": a
    # price restated on every turn is the same complaint whichever it is.
    _fee_told = _ty.SimpleNamespace(tenant=None, conversation_history=[
        {'role': 'assistant',
         'content': 'Would Tuesday work? The call-out to quote is US$20.'},
    ])
    _later = 'What works better for the visit, morning or afternoon?'
    results.log(
        "consultation fee: the figure is not restated on every later turn",
        _sfv(_later, _fee_told, 'morning') == (_later, False),
        got=str(_sfv(_later, _fee_told, 'morning')),
    )
    results.log(
        "consultation fee: an explicit ask gets the figure again",
        'US$20' in _sfv(_later, _fee_told, 'how much is the visit?')[0],
        got=repr(_sfv(_later, _fee_told, 'how much is the visit?')[0]),
    )
    # Safety is unchanged: a free promise is dropped whether or not the fee is
    # repeated, so a fee tenant can never be made to look free.
    _late_free, _ = _sfv('No problem, the site visit is free.', _fee_told, 'ok')
    results.log(
        "consultation fee: a late free promise is still dropped once the fee is known",
        'free' not in _late_free.lower(),
        got=repr(_late_free),
    )

    # Never send an empty message, even if the whole reply was a free claim.
    _o2, _ = _sfv('The visit is free.', _appt_stub)
    results.log(
        "consultation fee: a reply that was only a free claim is replaced, not emptied",
        _o2.strip() != '' and 'free' not in _o2.lower(),
        got=repr(_o2),
    )
finally:
    _tcmod.get_config = _real_gc

# ── The free visit is said ONCE, then only when asked ───────────────────────
from bot.views.plumbot.response_mixin import (
    strip_repeat_free_visit as _srfv,
    asks_visit_cost as _avc,
    free_visit_already_stated as _fvas,
)

_told = _ty.SimpleNamespace(conversation_history=[
    {'role': 'user', 'content': 'hi'},
    {'role': 'assistant',
     'content': 'We can come round for a free site visit and give you a fixed price.'},
])
_untold = _ty.SimpleNamespace(conversation_history=[
    {'role': 'user', 'content': 'hi'},
    {'role': 'assistant', 'content': 'We handle bathroom and kitchen plumbing.'},
])

results.log(
    "free visit: the FIRST mention goes out untouched",
    _srfv('Would you like to book a free site visit?', _untold, 'ok') ==
    ('Would you like to book a free site visit?', False),
    got=str(_srfv('Would you like to book a free site visit?', _untold, 'ok')),
)

results.log(
    "free visit: a customer saying 'free' is not us having said it",
    _fvas(_ty.SimpleNamespace(conversation_history=[
        {'role': 'user', 'content': 'is the site visit free?'}])) is False,
)

_repeats = {
    'Would Monday or Tuesday work for a free site visit?':
        'Would Monday or Tuesday work for a site visit?',
    "If you're ready, a free on-site visit and fixed quote is one message away.":
        "If you're ready, an on-site visit and fixed quote is one message away.",
    "We'll get you an exact, all-in figure free on a quick on-site visit.":
        "We'll get you an exact, all-in figure on a quick on-site visit.",
    'Want us to come take a look and lock in a fixed price? The assessment is free.':
        'Want us to come take a look and lock in a fixed price?',
    'Munogara kunzvimbo ipi kuti tironge visit toita free quote yakarurama?':
        'Munogara kunzvimbo ipi kuti tironge visit toita quote yakarurama?',
}
for _src, _want in _repeats.items():
    _got, _changed = _srfv(_src, _told, 'monday works')
    results.log(
        f"free visit: not repeated ({_src[:34]}...)",
        _changed and _got == _want,
        got=repr(_got),
    )

results.log(
    "free visit: the pitch survives, only the price claim comes off",
    all('visit' in _srfv(s, _told, 'ok')[0] or 'assessment' in _srfv(s, _told, 'ok')[0]
        or 'quote' in _srfv(s, _told, 'ok')[0] or 'look' in _srfv(s, _told, 'ok')[0]
        for s in _repeats),
    got=str([_srfv(s, _told, 'ok')[0] for s in _repeats]),
)

# The customer's own words override the gate — they asked, so they get told.
for _ask in ('is the visit free?', 'do you charge for a quote',
             'how much is the site visit', 'marii kuuya'):
    results.log(
        f"free visit: an explicit ask still gets the answer ({_ask})",
        _avc(_ask) and _srfv('The site visit is free.', _told, _ask) ==
        ('The site visit is free.', False),
        got=f"asks={_avc(_ask)} out={_srfv('The site visit is free.', _told, _ask)}",
    )

for _not_ask in ('monday works', 'how much is a toilet', 'i want a quote',
                 'what areas do you cover'):
    results.log(
        f"free visit: not every message is a cost question ({_not_ask})",
        _avc(_not_ask) is False,
    )

# Never mangle copy that has nothing to do with the visit's price.
for _untouched in ('A freestanding tub is from US$670 all-in, confirmed on the visit.',
                   'Shower cubicles from US$170 all-in (supply from US$130 + install from US$40).',
                   'Great, Monday at 10am works. The plumber will be there.'):
    results.log(
        f"free visit: leaves unrelated copy alone ({_untouched[:26]}...)",
        _srfv(_untouched, _told, 'ok') == (_untouched, False),
        got=str(_srfv(_untouched, _told, 'ok')),
    )

# The shapes the claim takes beyond the plain adjective, each of which used to
# leave the sentence broken when the word was simply deleted.
_shapes = {
    # leading clause -> the rest of the sentence is promoted
    'The site visit and quote are free — our plumber will come and look at the space.':
        'Our plumber will come and look at the space.',
    # trailing clause -> dropped, the sentence before it stands
    'The plumber gives a fixed quote after seeing the space, and the site visit is free.':
        'The plumber gives a fixed quote after seeing the space.',
    # adverbial
    'Our plumber will confirm the exact figures free when they come out to you.':
        'Our plumber will confirm the exact figures when they come out to you.',
    # a label parenthetical goes whole; an informative one only loses the adjective
    'Do you know the size, or should we measure up?\n(Site assessment is free)':
        'Do you know the size, or should we measure up?',
    'Rough prices (final cost confirmed after a free site visit):':
        'Rough prices (final cost confirmed after a site visit):',
}
for _src, _want in _shapes.items():
    _got, _ = _srfv(_src, _told, 'monday works')
    results.log(
        f"free visit: de-qualified cleanly ({_src[:34]}...)",
        _got == _want,
        got=repr(_got),
    )

_hanging = _srfv(
    'Yes, the site visit and quote are completely free.\n\nWe come to you '
    'and give a fixed price on the spot.', _told, 'ok')[0]
results.log(
    "free visit: a removal never leaves a sentence hanging on its verb",
    not re.search(r'\\b(?:is|are)\\s*[.!?]?$', _hanging)
    and 'fixed price on the spot' in _hanging,
    got=repr(_hanging),
)

# A reply whose whole substance was the claim is kept, never emptied.
_only, _ = _srfv('The site visit is free.', _told, 'monday works')
results.log(
    "free visit: a reply that was only the claim is kept, not emptied",
    _only.strip() != '',
    got=repr(_only),
)

# The prompt stops asserting it too, so the model isn't fighting the stripper.
from bot.views.plumbot.response_mixin import _visit_fact_line as _vfl
results.log(
    "free visit: the LLM grounding fact drops 'is free' after the first time",
    'The visit is free:' in _vfl(_ty.SimpleNamespace(appointment=_untold))
    and 'The visit is free:' not in _vfl(_ty.SimpleNamespace(appointment=_told)),
    got=repr(_vfl(_ty.SimpleNamespace(appointment=_told))[:80]),
)

# ── No dash punctuation in anything the customer reads ─────────────────────
from bot.utils import strip_dashes as _sd

# The clause dash becomes the comma or the full stop the sentence wanted.
_dash_cases = {
    'The way we land a fair price is an on-site visit — the plumber sees the space.':
        'The way we land a fair price is an on-site visit. The plumber sees the space.',
    'Perfect — thanks, Tendai.': 'Perfect. Thanks, Tendai.',
    "Said I'd check in — here I am.": "Said I'd check in. Here I am.",
    'Wall-hung toilet, all-in from US$160 - supply US$130 plus install US$30.':
        'Wall-hung toilet, all-in from US$160, supply US$130 plus install US$30.',
    # ranges read as "to" in speech
    'Business hours: 8:00 - 18:00, Sun–Fri.': 'Business hours: 8:00 to 18:00, Sun to Fri.',
    "We're open Mon-Fri, 8am-6pm.": "We're open Mon to Fri, 8am to 6pm.",
}
for _src, _want in _dash_cases.items():
    results.log(
        f"dashes: taken out cleanly ({_src[:38]}...)",
        _sd(_src) == _want,
        got=repr(_sd(_src)),
    )

# A dash opening a line is a bullet doing a job, not punctuation.
results.log(
    "dashes: a bullet leader keeps its job without keeping the dash",
    _sd('It depends on:\n- the fixtures\n- the size') == 'It depends on:\n• the fixtures\n• the size',
    got=repr(_sd('It depends on:\n- the fixtures\n- the size')),
)

# Hyphens INSIDE words are how people write. Stripping them would give
# "onsite" and "allin", so they are left exactly alone.
for _keep in ('A corner tub is a built-in tub, from US$160 all-in.',
              'Wall-hung toilet, supply and install.',
              'A quick 20-minute look at the on-site space.',
              'Call us on +263774819901.',
              'Would Monday or Tuesday work for a site visit?'):
    results.log(
        f"dashes: intra-word hyphens survive ({_keep[:34]}...)",
        _sd(_keep) == _keep,
        got=repr(_sd(_keep)),
    )

results.log(
    "dashes: empty and None pass through without raising",
    _sd('') == '' and _sd(None) is None,
)

# No customer-facing reply may leave the choke point with dash punctuation.
_dashy = re.compile(r'—|–|(?<= )-(?= )')
results.log(
    "dashes: nothing dash-punctuated survives the stripper",
    not any(_dashy.search(_sd(t)) for t in list(_dash_cases) + [
        'a — b — c', 'One thing — and another — and a third.']),
    got=str([_sd(t) for t in ['a — b — c', 'One thing — and another — and a third.']]),
)

# The prompts must stop the model reaching for a dash, not just repair it after.
from bot.views.plumbot.response_mixin import build_cold_opener as _bco
import bot.views.plumbot.response_mixin as _rm_mod
# Read the module by its own __file__ so the check does not depend on cwd.
_rm_src = open(_rm_mod.__file__, encoding='utf-8-sig').read()
results.log(
    "dashes: the reply prompt tells the model not to use one",
    'Never use a dash as punctuation' in _rm_src,
)
results.log(
    "dashes: the approved cold opener carries none",
    not _dashy.search(_bco()) and not _dashy.search(_bco(is_shona=True)),
    got=repr(_bco()),
)

# ── The visit price is stated ONCE, in the first message ────────────────────
from bot.views.plumbot.response_mixin import (
    ensure_visit_price_note as _evpn,
    visit_price_already_stated as _vpas,
)


class _CfgNote:
    """Stands in for the three offers a tenant can have."""
    currency = 'US$'

    def __init__(self, fee=None, waived=False):
        self.fee, self.waived = fee, waived

    @property
    def consultation_fee(self):
        return self.fee

    def visit_is_free(self):
        return not self.fee

    def visit_fee_waived_on_job(self):
        return bool(self.fee) and self.waived

    def visit_price_note(self, is_shona=False):
        if not self.fee:
            return 'Just a quick note: *the call-out is free.*'
        if self.visit_fee_waived_on_job():
            return f'Just a quick note: *US${self.fee} call-out fee — FREE if we do the job.*'
        return f'Just a quick note: *US${self.fee} call-out fee.*'

    def visit_cost_sentence(self, is_shona=False):
        return '' if not self.fee else f'The call-out to quote is US${self.fee}.'


_opener = ('Hello,\nWe handle bathroom and kitchen plumbing.\n\nA new '
           'installation, or a renovation of what you have?')
_fresh = lambda: _ty.SimpleNamespace(tenant=None, conversation_history=[])
_real_gc2 = _tcmod.get_config
try:
    for _label, _cfg, _want in (
        ('free', _CfgNote(), 'the call-out is free'),
        ('flat fee', _CfgNote(20), 'US$20 call-out fee'),
        ('waived on the job', _CfgNote(20, True), 'US$20 call-out fee — FREE if we do the job'),
    ):
        _tcmod.get_config = lambda tenant=None, _c=_cfg: _c
        _out, _added = _evpn(_opener, _fresh(), 'hie')
        results.log(
            f"visit price note: the first message states it ({_label})",
            _added and _want in _out and _out.startswith(_opener),
            got=repr(_out),
        )

    # Once it has been said, no later reply carries it again.
    _tcmod.get_config = lambda tenant=None: _CfgNote()
    _told_note = _ty.SimpleNamespace(tenant=None, conversation_history=[
        {'role': 'assistant',
         'content': _opener + '\n\nJust a quick note: *the call-out is free.*'},
    ])
    _second = 'Borrowdale is well within our area. Tuesday or Thursday?'
    results.log(
        "visit price note: never repeated on a later turn",
        _evpn(_second, _told_note, 'borrowdale') == (_second, False),
        got=str(_evpn(_second, _told_note, 'borrowdale')),
    )
    results.log(
        "visit price note: the transcript read sees it in either wording",
        _vpas(_told_note, _CfgNote()) is True
        and _vpas(_fresh(), _CfgNote()) is False,
    )
    # A reply that already prices the visit is not given a second note.
    _prices_it = 'The site visit is free, so there is nothing to lose.'
    results.log(
        "visit price note: a reply that already states it is left alone",
        _evpn(_prices_it, _fresh(), 'hi') == (_prices_it, False),
        got=str(_evpn(_prices_it, _fresh(), 'hi')),
    )
    # A fee tenant's note must not be read back as a free promise and dropped:
    # the note is appended AFTER both strippers for exactly this reason.
    _tcmod.get_config = lambda tenant=None: _CfgNote(20, True)
    _noted, _ = _evpn(_opener, _fresh(), 'hie')
    results.log(
        "visit price note: the waived-fee note survives the fee stripper's order",
        'FREE if we do the job' in _noted,
        got=repr(_noted),
    )
    # The fee stripper de-qualifies before it drops, so the pitch keeps its
    # question instead of the whole sentence being deleted.
    _pitch = 'Good one. Would you like us to come round for a free site visit?'
    _kept, _ = _sfv(_pitch, _ty.SimpleNamespace(tenant=None, conversation_history=[]), 'ok')
    results.log(
        "consultation fee: the visit pitch keeps its question, minus the claim",
        'free' not in _kept.lower() and 'come round for a site visit?' in _kept,
        got=repr(_kept),
    )
finally:
    _tcmod.get_config = _real_gc2

# ── Out of scope: decline, never loop ───────────────────────────────────────
from bot.out_of_scope_handler import (
    _is_affirmative as _isaff, is_inbound_sales_pitch as _isp,
    build_sales_pitch_reply as _bspr, is_hard_stop_request as _ishs,
)

# Prod lead 847: answered "Yes" to "is there plumbing involved?" and was asked
# the identical question again, twice.
results.log(
    "oos: a plain yes closes the plumbing reframe",
    _isaff("Yes") and _isaff("yes") and _isaff("Hongu") and _isaff("Ehe"),
    got="affirmatives",
)
results.log(
    "oos: a negative answer is not an affirmative",
    not _isaff("No, It's for a house build")
    and not _isaff("Not at present. I thought you were a construction company"),
    got="negatives",
)

# Prod lead 855: a marketing agency was asked twice whether its social media
# package involved water-related work.
results.log(
    "oos: an inbound sales pitch is recognised",
    _isp("We have a 20 dollar package which contains 3 social media post")
    and _isp("We offer digital marketing services"),
    got="pitches",
)
results.log(
    "oos: a real enquiry is never read as a sales pitch",
    not _isp("how much for a shower cubicle")
    and not _isp("Am requesting for quote for roofing")
    and not _isp("I need my toilet fixed"),
    got="enquiries",
)
_pitch_reply = _bspr()
results.log(
    "oos: the pitch decline asks no qualifying question and has no emoji",
    "?" not in _pitch_reply and all(ord(ch) < 0x2190 for ch in _pitch_reply),
    got=repr(_pitch_reply),
)

# ── The hard stop ───────────────────────────────────────────────────────────
# Prod lead 872 wrote this and received three more automated pitches.
results.log(
    "hard stop: an explicit stop request is caught regardless of length",
    _ishs("Ok send hear and please dont say anything more")
    and _ishs("please stop messaging me")
    and _ishs("leave me alone")
    and _ishs("remove me"),
    got="stop requests",
)
results.log(
    "hard stop: ordinary messages are never read as a stop",
    not _ishs("Ok")
    and not _ishs("Thank you")
    and not _ishs("send me more info")
    and not _ishs("stop by tomorrow and take a look"),
    got="non-stops",
)

# The crons must read the decision, not just the handler that made it.
import inspect as _inspect
from bot.management.commands.send_followups import Command as _FUCmd
_supp_src = _inspect.getsource(_FUCmd._exclude_suppressed_states)
results.log(
    "hard stop: follow-up eligibility excludes STOP_REQUESTED and EXCLUDED_AREA",
    '[STOP_REQUESTED]' in _supp_src and '[EXCLUDED_AREA' in _supp_src
    and '[OOS_DECLINED]' in _supp_src,
    got=_supp_src,
)

# Handler C: a stop gets ONE confirmation — never silence, never a catalogue
# offer and never a question.
from bot.out_of_scope_handler import build_hard_stop_reply as _bhsr
_stop_reply = _bhsr()
results.log(
    "hard stop: confirms once, asks nothing, offers no catalogue",
    "?" not in _stop_reply
    and "catalog" not in _stop_reply.lower()
    and "email" not in _stop_reply.lower()
    and all(ord(ch) < 0x2190 for ch in _stop_reply),
    got=repr(_stop_reply),
)
results.log(
    "hard stop: the confirmation leaves the door open",
    "Hey" in _stop_reply and "Mhoro" not in _stop_reply,
    got=repr(_stop_reply),
)
results.log(
    "hard stop: mirrors Shona",
    "Hesi" in _bhsr(is_shona=True),
    got=repr(_bhsr(is_shona=True)),
)
_fu_src = _inspect.getsource(_FUCmd)
results.log(
    "hard stop: the delay and parked nudge queries honour it too",
    _fu_src.count("[STOP_REQUESTED]") >= 3
    and _fu_src.count("[EXCLUDED_AREA") >= 3,
    got=f"stop={_fu_src.count('[STOP_REQUESTED]')} area={_fu_src.count('[EXCLUDED_AREA')}",
)

# ── No emojis in customer-facing copy, enforced not merely requested ─────────
# The house rule was in the prompts of some generators and not others: four
# customer-facing prompts asked for "one emoji max / only if it fits naturally",
# and only ONE send path stripped what came back. Follow-ups, retry re-asks,
# repeat-question clarifications and the legacy contextual reply could each put
# an emoji in front of a customer. Pinned in both halves — the prompts must not
# invite one, and the stripper must remove one that arrives anyway.
from bot.utils import strip_emojis as _strip

results.log(
    "no-emoji: the stripper removes emoji and keeps the words",
    _strip("Sure thing 👍 we can sort that ✅") == "Sure thing we can sort that",
    got=repr(_strip("Sure thing 👍 we can sort that ✅")),
)
results.log(
    "no-emoji: paragraph breaks survive stripping",
    _strip("Got it 😊\n\nWhat day suits you?") == "Got it\n\nWhat day suits you?",
    got=repr(_strip("Got it 😊\n\nWhat day suits you?")),
)
results.log(
    "no-emoji: ordinary copy is returned untouched",
    _strip("Shower cubicles from US$170 all-in.") == "Shower cubicles from US$170 all-in.",
    got=repr(_strip("Shower cubicles from US$170 all-in.")),
)
results.log(
    "no-emoji: empty and None pass through without raising",
    _strip("") == "" and _strip(None) is None,
    got=repr(_strip("")),
)

# No customer-facing prompt may ASK for an emoji. Catches the exact wording that
# shipped ("At most one emoji", "One emoji max", "Emojis only when they fit").
import bot.out_of_scope_handler as _oos_mod
import bot.repeated_question_detector as _rqd
import bot.unified_classifier as _uc_mod
import bot.views.plumbot.response_mixin as _rm_mod
_emoji_invites = re.compile(
    r"(at most|max(imum)?|only)\s+(one\s+)?emoji"
    r"|one emoji max"
    r"|emojis? only when",
    re.IGNORECASE,
)
for _label, _mod in (
    ("follow-up copy", _FUCmd),
    ("repeat clarification", _rqd),
    ("response mixin", _rm_mod),
    ("out-of-scope", _oos_mod),
):
    _src = _inspect.getsource(_mod)
    results.log(
        f"no-emoji: the {_label} prompt never invites an emoji",
        _emoji_invites.search(_src) is None,
        got=(_emoji_invites.search(_src).group(0) if _emoji_invites.search(_src) else "clean"),
    )

# ── Part 5 runtime parameters reach the customer-facing generators ───────────
# temperature/top_p/frequency_penalty are the variety dials. They belong on
# free-text generation and must stay OFF the classifiers, whose output the rule
# engine parses — a business fact has to come back the same every time.
from bot.services.clients import HUMAN_VOICE as _HV, deepseek_call as _dsc

results.log(
    "voice preset: the documented variety dials",
    _HV == {'temperature': 1.0, 'top_p': 0.9, 'frequency_penalty': 0.3},
    got=repr(_HV),
)
_sig = _inspect.signature(_dsc).parameters
results.log(
    "voice preset: deepseek_call passes the dials through, defaulting to unset",
    _sig['top_p'].default is None and _sig['frequency_penalty'].default is None,
    got=f"top_p={_sig['top_p'].default} freq={_sig['frequency_penalty'].default}",
)
results.log(
    "voice preset: the vary-on-retry generator uses it",
    "**HUMAN_VOICE" in _inspect.getsource(_rm_mod.ResponseMixin._generate_retry_response),
    got="present" if "**HUMAN_VOICE" in _inspect.getsource(
        _rm_mod.ResponseMixin._generate_retry_response) else "missing",
)
results.log(
    "voice preset: the unified classifier stays deterministic",
    "temperature=0.0" in _inspect.getsource(_uc_mod.unified_classify)
    and "HUMAN_VOICE" not in _inspect.getsource(_uc_mod),
    got="deterministic",
)

# ── A rejected WhatsApp reminder falls back to email ─────────────────────────
# The email branch was an `elif` on the WhatsApp branch, so it was reachable
# only when the window was already known closed. A send Meta REJECTED against a
# window we believed open (131047) dropped the reminder entirely: no flag set,
# no email, and once the ±10 minute _in_window passed it never fired again.
# Barmak lead 858 lost its 2-day reminder that way with a good address on file.
import bot.management.commands.send_reminders as _srm

_srm_src = _inspect.getsource(_srm.Command.handle)
results.log(
    "reminders: WhatsApp and email are no longer mutually exclusive branches",
    "_deliver_customer_reminder" in _srm_src
    and "elif has_email:" not in _srm_src,
    got=("helper wired" if "_deliver_customer_reminder" in _srm_src
         else "still branching inline"),
)
_deliver_src = _srm_src[_srm_src.index("def _deliver_customer_reminder"):]
_deliver_src = _deliver_src[:_deliver_src.index("# ─────")]
results.log(
    "reminders: a failed send with an email on file still reaches the email path",
    "if not has_email:" in _deliver_src
    and _deliver_src.index("if not has_email:") < _deliver_src.index("send_customer_reminder_email"),
    got="fallback ordered before the email send",
)
results.log(
    "reminders: the sent flag is only stamped after a real send",
    _deliver_src.count("_mark_sent_customer") == 2
    and "if not dry_run:" in _deliver_src,
    got=f"marks={_deliver_src.count('_mark_sent_customer')}",
)
results.log(
    "reminders: a raising email is logged, not swallowed, and never marks sent",
    "logger.warning" in _deliver_src and "ok = False" in _deliver_src,
    got="guarded",
)
# Both reminder families must go through the one helper — the 2-hour block had
# its own copy of the same broken elif.
results.log(
    "reminders: the 2-hour reminder uses the same delivery path",
    _srm_src.count("_deliver_customer_reminder(") >= 3,
    got=f"call sites={_srm_src.count('_deliver_customer_reminder(') - 1}",
)

# ── The 2-day and 1-day reminders have separate flags ────────────────────────
# They both wrote reminder_1_day_sent ("reuse closest field"), which made them
# mutually exclusive: the 2-day fired, stamped the flag, and the 1-day was then
# skipped as already sent. Anyone booked 2+ days out got one nudge and then
# nothing until the morning.
results.log(
    "reminders: the 2-day reminder reads its own flag",
    _srm._already_sent_customer(
        type('A', (), {'reminder_2_days_sent': True, 'reminder_1_day_sent': False})(),
        'lead_2days') is True,
    got="own column",
)
results.log(
    "reminders: a sent 2-day reminder no longer suppresses the 1-day",
    _srm._already_sent_customer(
        type('A', (), {'reminder_2_days_sent': True, 'reminder_1_day_sent': False})(),
        'lead_1day') is False,
    got="1-day still due",
)
results.log(
    "reminders: the read and write maps agree on every reminder type",
    _inspect.getsource(_srm._already_sent_customer).count('reminder_2_days_sent')
    == _inspect.getsource(_srm._mark_sent_customer).count('reminder_2_days_sent') == 1,
    got="maps in step",
)

# ── Morning-of and 2-hours-before must not double-send ───────────────────────
# Separate flags + overlapping windows meant an early appointment got both on
# the same tick. A 09:00 visit is the worst case: the 2-hour window (06:55-07:05)
# sits entirely inside the morning window (06:50-07:10).
import pytz as _pytz
from datetime import datetime as _dt, timedelta as _td
_cat = _pytz.timezone('Africa/Harare')
_morning_7am = _cat.localize(_dt(2026, 9, 2, 7, 0))

results.log(
    "reminders: a 09:00 appointment collides with the morning reminder",
    _srm._morning_collides_with_2h(_morning_7am, _cat.localize(_dt(2026, 9, 2, 9, 0))),
    got="collision detected",
)
results.log(
    "reminders: a midday appointment does not collide",
    not _srm._morning_collides_with_2h(_morning_7am, _cat.localize(_dt(2026, 9, 2, 13, 0))),
    got="no collision",
)
results.log(
    "reminders: a late-afternoon appointment does not collide",
    not _srm._morning_collides_with_2h(_morning_7am, _cat.localize(_dt(2026, 9, 2, 16, 30))),
    got="no collision",
)
results.log(
    "reminders: the collision skip stamps the flag so it cannot fire later",
    "[2-hour reminder covers it]" in _srm_src
    and _srm_src.index("_morning_collides_with_2h") < _srm_src.index("[2-hour reminder covers it]")
    and "_mark_sent_customer(apt, rtype)" in _srm_src.split("_morning_collides_with_2h")[1][:900],
    got="marked, not merely skipped",
)

# ── The dashboard must not project delay emails for a lead who left the queue ─
# delay_followup_due_at deliberately outlives clear_delayed() (send_followups
# uses it to keep a parked lead out of normal follow-ups), so keying the
# projection off that field alone showed a permanent "overdue" delay email on
# every lead who had since booked — Barmak 858, booked, is_delayed False, no
# delay email ever sent.
import bot.models as _bot_models
_gue_src = _inspect.getsource(_bot_models.Appointment.get_upcoming_emails)
results.log(
    "dashboard: the delay sequence is gated on the lead still being delayed",
    "self.is_delayed or reengaged or last_checked" in _gue_src,
    got="gated",
)
results.log(
    "dashboard: a delay email that really was sent still shows in history",
    "reengaged" in _gue_src and "last_checked" in _gue_src,
    got="history preserved",
)
results.log(
    "dashboard: a reminder moment predating the lead's own row is not 'overdue'",
    "when < self.created_at" in _gue_src,
    got="phantom guard present",
)
results.log(
    "dashboard: the 2-day row reads the 2-day flag, not the 1-day one",
    "self.reminder_2_days_sent" in _gue_src,
    got="own flag",
)

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