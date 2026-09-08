"""
bot/controller.py
=================
The PLANNING half of the reasoning controller (spec §1, §4, §5, §6).

`bot/unified_classifier.py` already makes ONE DeepSeek call per inbound turn and
returns comprehension + extraction. This module adds the planning layer on top
of that same call: which move comes next, and the deterministic gates that stop
a model slip from putting a fee, a price or a booking in front of a customer.

Nothing here calls an API. Every function is pure and offline-testable, which is
the point: the model proposes a move, and this module is what decides whether we
are allowed to take it.

Three rules shape the whole file:

1. **A fact the system owns beats a model opinion.** `work_shown` is not the
   model's to report — we know whether photos went out, from
   `previous_work_photos_sent_at`. Trusting the model there would either re-send
   a gallery or suppress one that never went.

2. **Planning failure must never degrade classification.** The classifier is
   load-bearing today; the planner is not yet wired to anything. So a missing or
   malformed `next_move` drops the planning half and keeps the classification.
   Anything else would turn a working classifier into a None-returning one the
   day the planning prompt drifted.

3. **Exit and complaint signals outrank flow logic** (CLAUDE.md), so they sit at
   the top of the projection ladder, not somewhere in the middle.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ── The vocabulary (spec §4) ──────────────────────────────────────────────────
# Kept as frozensets rather than enums: these values arrive as JSON strings from
# a model, and membership is the only question ever asked of them.

NEXT_MOVES = frozenset({
    'greet',
    'show_work',
    'ask_qualifying_question',
    'present_value',
    'book_visit',
    'handle_objection',
    'slow_lead_nudge',
    'escalate_to_human',
    'out_of_scope_redirect',
    'close_pleasantry',
})

WANT_LEVELS = frozenset({'cold', 'interested', 'wants_it'})

# The fields the controller is allowed to choose between. Deliberately NOT
# every question the flow knows: 'service_type' is absent because a lead who
# has said anything at all has given us something better to ask about, and
# 'complete' is a state rather than a question.
QUESTION_FIELDS = frozenset({
    'project_description', 'area', 'timeline',
    'availability_date', 'availability_time', 'name',
})

# What each choice means we must NOT already hold. This is the guard that
# matters: the single most repeated bug in this codebase is the bot asking for
# something the customer already told it, and handing question choice to a
# model is the most direct way to reintroduce it.
_QUESTION_FIELD_SOURCE = {
    'project_description': 'project_description',
    'area': 'customer_area',
    'timeline': 'timeline',
    'name': 'customer_name',
}

RESPONSE_MODES = frozenset({'freeform', 'template'})

TEMPLATE_IDS = frozenset({
    'show_examples',
    'paid_visit_close',
    'slow_lead_nudge',
    'fee_objection',
    'booking_confirm',
    'escalation_ack',
    'value_frame_price',
})

COMPREHENSION_INTENTS = frozenset({
    'greeting', 'asking_price', 'describing_want', 'objection',
    'ready_to_book', 'chitchat', 'complaint', 'out_of_scope', 'other',
})

SENTIMENTS = frozenset({'positive', 'neutral', 'hesitant', 'frustrated'})

URGENCIES = frozenset({'emergency', 'soon', 'flexible'})

# Below this, the controller's move is not trusted and the caller falls back to
# the legacy branch (spec §5). Deliberately a module constant rather than a
# literal at the call site: Phase 2 tunes this from prod agreement data, and it
# must move in one place.
CONFIDENCE_FLOOR = 0.6

# The model plans in `reasoning`, but that text shares `max_tokens` with the
# JSON body — and a truncated body is unparseable, which is exactly the failure
# that broke every classifier when thinking mode was left on (see
# bot/services/clients.py). Capped in the prompt, enforced here.
MAX_REASONING_CHARS = 400


# ── Validation (spec §4: "validate server-side, retry once") ──────────────────

def validate_turn(payload) -> list:
    """Check the PLANNING half of a controller payload.

    Returns a list of human-readable problems; empty means usable. The
    classification half is deliberately NOT validated here — it has its own
    accessors, each of which already defaults safely, and re-checking it would
    couple the planner's strictness to the classifier's reliability (rule 2 in
    the module docstring).
    """
    errors = []

    if not isinstance(payload, dict):
        return ['payload is not an object']

    move = payload.get('next_move')
    if move is None:
        errors.append('next_move missing')
    elif not isinstance(move, str) or move.strip().lower() not in NEXT_MOVES:
        errors.append(f'next_move not in vocabulary: {move!r}')

    # NOT `confidence` — that key is already taken by the CLASSIFICATION's
    # HIGH/LOW and is read by gates all over the bot. Two meanings on one key
    # would have the planner's 0.8 silently answering "is this classification
    # reliable?".
    confidence = payload.get('move_confidence')
    if confidence is None:
        errors.append('move_confidence missing')
    else:
        try:
            value = float(confidence)
        except (TypeError, ValueError):
            errors.append(f'move_confidence not a number: {confidence!r}')
        else:
            if not 0.0 <= value <= 1.0:
                errors.append(f'move_confidence out of range: {value}')

    comprehension = payload.get('comprehension')
    if comprehension is not None and not isinstance(comprehension, dict):
        errors.append('comprehension is not an object')

    state_update = payload.get('state_update')
    if state_update is not None and not isinstance(state_update, dict):
        errors.append('state_update is not an object')

    response = payload.get('response')
    if response is not None:
        if not isinstance(response, dict):
            errors.append('response is not an object')
        else:
            mode = (response.get('mode') or '').strip().lower()
            if mode and mode not in RESPONSE_MODES:
                errors.append(f'response.mode not in vocabulary: {mode!r}')
            if mode == 'template':
                template_id = (response.get('template_id') or '').strip()
                if not template_id:
                    errors.append('response.mode=template with no template_id')
                elif template_id not in TEMPLATE_IDS:
                    errors.append(f'unknown template_id: {template_id!r}')

    return errors


def planning_is_usable(payload) -> bool:
    """True when the planning half validated. Cheap wrapper for readability."""
    return not validate_turn(payload)


def planning_attempted(payload) -> bool:
    """Did the model try to plan at all?

    The difference decides whether a retry is worth paying for. A plan that is
    PRESENT but malformed is a one-off slip and a second call usually fixes it.
    A plan that is ENTIRELY ABSENT means the planning prompt is not landing —
    a stale deployment, a truncated body, a model that ignores the block — and
    retrying then doubles the call count on EVERY turn while fixing nothing.
    That is the difference between a rare retry and silently paying twice for
    the bot's busiest code path.
    """
    return isinstance(payload, dict) and 'next_move' in payload


# ── Accessors for the planning half ──────────────────────────────────────────
# Same contract as the uc_* helpers: safe on None, and "absent" is always
# distinguishable from "the model said so".

def plan_next_move(payload) -> str | None:
    move = (payload or {}).get('next_move')
    if not isinstance(move, str):
        return None
    move = move.strip().lower()
    return move if move in NEXT_MOVES else None


def plan_confidence(payload) -> float:
    """The model's confidence in its move; 0.0 when absent or unparseable.

    Zero rather than a neutral default on purpose: an absent confidence must
    read as "do not act on this", and CONFIDENCE_FLOOR then sends the caller to
    the legacy branch.
    """
    try:
        value = float((payload or {}).get('move_confidence'))
    except (TypeError, ValueError):
        return 0.0
    return min(max(value, 0.0), 1.0)


def plan_next_question(payload) -> str | None:
    """Which field the model wants to ask about, or None.

    None also means "it did not say", which callers treat as "use the
    deterministic order" — never as "ask nothing".
    """
    value = (payload or {}).get('next_question')
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in QUESTION_FIELDS else None


def choose_question(payload, appointment, default):
    """The field to ask about this turn: the model's pick, or the default.

    This is where the model is allowed to think, and where it is stopped from
    thinking something harmful. Three conditions, all of which must hold:

    1. It must be confident. Below the floor the deterministic order wins,
       because a coin-flip on what to ask next is worse than a dull-but-right
       sequence.

    2. It must be a field in the vocabulary. Anything else is a hallucinated
       question and is discarded silently.

    3. **It must be a field we do not already have.** This is the guard that
       earns its place. Asking a customer for something they already told us is
       the single most repeated bug in this codebase — it has its own resolver,
       its own detector and its own regression cases — and handing question
       choice to a model is the most direct way to bring it back. The model can
       reorder the questions; it cannot re-open a closed one.

    Note what is NOT checked: whether the default agrees. The whole point is to
    let the model pick a better question than position-in-a-list would, so
    disagreement is the feature, not the error.
    """
    picked = plan_next_question(payload)
    if picked is None:
        return default
    if plan_confidence(payload) < CONFIDENCE_FLOOR:
        logger.info("Question choice %s below the floor, keeping %s",
                    picked, default)
        return default

    source = _QUESTION_FIELD_SOURCE.get(picked)
    if source and str(getattr(appointment, source, '') or '').strip():
        logger.info("Controller wanted to ask %s, which we already hold — "
                    "keeping %s", picked, default)
        return default

    # The two date fields are held on one column, so they get their own check.
    if picked == 'availability_date' and getattr(
            appointment, 'scheduled_datetime', None):
        logger.info("Controller wanted a day we already have — keeping %s",
                    default)
        return default

    if picked != default:
        logger.info("Controller chose %s over %s", picked, default)
    return picked


def plan_want_level(payload) -> str | None:
    state = (payload or {}).get('state_update') or {}
    value = state.get('want_level')
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in WANT_LEVELS else None


def plan_reasoning(payload) -> str:
    value = (payload or {}).get('reasoning')
    if not isinstance(value, str):
        return ''
    return value.strip()[:MAX_REASONING_CHARS]


def plan_sentiment(payload) -> str | None:
    comp = (payload or {}).get('comprehension') or {}
    value = comp.get('sentiment')
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in SENTIMENTS else None


def plan_comprehension_intent(payload) -> str | None:
    comp = (payload or {}).get('comprehension') or {}
    value = comp.get('intent')
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in COMPREHENSION_INTENTS else None


def plan_state_update(payload) -> dict:
    """The newly-learned fields, filtered to the schema's own vocabulary.

    `additionalProperties: false` in the spec is enforced here rather than
    trusted: an unexpected key means the model invented a field, and writing it
    onto a lead record is how a schemaless JSON column turns into a landfill.
    `datetime` is absent by design — the deterministic datetime step owns it
    (spec §6).
    """
    state = (payload or {}).get('state_update') or {}
    if not isinstance(state, dict):
        return {}
    clean = {}
    for key in ('name', 'suburb', 'job_type', 'timeframe'):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            clean[key] = value.strip()
    urgency = state.get('urgency')
    if isinstance(urgency, str) and urgency.strip().lower() in URGENCIES:
        clean['urgency'] = urgency.strip().lower()
    want = plan_want_level(payload)
    if want:
        clean['want_level'] = want
    # work_shown is deliberately NOT carried through: it is a fact we own.
    # See work_already_shown().
    return clean


# ── Facts the system owns, never the model ───────────────────────────────────

def work_already_shown(appointment) -> bool:
    """Have this lead's example photos actually gone out?

    Read from `previous_work_photos_sent_at`, which the send path stamps, and
    never from the model's `state_update.work_shown`. The model reports what it
    believes; this reports what happened. When the two disagree the gallery
    either goes twice or never, and both are visible to the customer.
    """
    return bool(getattr(appointment, 'previous_work_photos_sent_at', None))


def description_captured(appointment) -> bool:
    """Do we know what the job actually is?

    The revised close order (spec §2, rule 1) puts the project description
    BEFORE the photos: proof lands harder when it matches what they just
    described, and a gallery sent against a bare "I want my ensuite done" is
    generic. So this gates show_work.
    """
    return bool((getattr(appointment, 'project_description', '') or '').strip())


def area_captured(appointment) -> bool:
    return bool((getattr(appointment, 'customer_area', '') or '').strip())


# A plan has ARRIVED, not merely been promised. `plan_status='pending_upload'`
# is a lead saying they'll send one; only these three mean we hold it.
_PLAN_ON_FILE = frozenset({'plan_uploaded', 'plan_reviewed', 'ready_to_book'})


def on_plan_path(appointment) -> bool:
    """Has this lead sent a real plan (spec §10)?

    The plan carries the measurements, so there is nothing to measure up and
    the paid site visit is redundant — the plumber quotes straight off it. The
    rest of the flow is unchanged: description, area and timeline are still
    collected, and the timeline still routes the lead afterwards.

    Deliberately NOT `has_plan`. That flag means "a plan is expected", which is
    true the moment a lead says one is coming — and a lead who has promised a
    plan they have not sent still has nothing we can quote from. Only a plan on
    file switches the path.
    """
    status = (getattr(appointment, 'plan_status', '') or '').strip().lower()
    return status in _PLAN_ON_FILE


def apply_plan_path_gate(next_move: str, appointment) -> str:
    """Never pitch the measure-up visit to a lead whose plan we already hold.

    `book_visit` renders the fee close — the one move that asks a plan-path
    lead to pay for a measure-up their own drawing already made unnecessary.
    Booking the JOB later is a different flow (the timeline routing in §10.6),
    reached through the scheduler, not through this move.

    Same shape as the fee gate, and for the same reason: a rule that lives only
    in a prompt is a rule that holds until the model has a bad day.
    """
    if next_move != 'book_visit' or not on_plan_path(appointment):
        return next_move
    logger.info("Plan-path gate: book_visit suppressed, the plan replaces the "
                "measure-up")
    return 'ask_qualifying_question'


def service_captured(appointment) -> bool:
    return bool((getattr(appointment, 'project_type', '') or '').strip())


# ── The fee gate (spec §5) ───────────────────────────────────────────────────

def apply_fee_gate(next_move: str, appointment, want_level: str | None) -> str:
    """Never let the fee reach a lead who has not been sold the job yet.

    The prompt states this rule and this function enforces it, because a rule
    that lives only in a prompt is a rule that holds until the model has a bad
    day. `book_visit` is the only move that states the fee, so it is the only
    one gated: below `wants_it` we go back to showing the work, or to asking
    the next qualifying question once the work has already been shown.
    """
    if next_move != 'book_visit':
        return next_move
    if want_level == 'wants_it':
        return next_move
    fallback = ('ask_qualifying_question' if work_already_shown(appointment)
                else 'show_work')
    logger.info(
        "Fee gate: book_visit held back (want_level=%s) -> %s",
        want_level, fallback,
    )
    return fallback


# ── The deterministic projection (spec §1) ───────────────────────────────────
# The spec's own observation is that most of next_move is a re-projection of
# signals the classifier already returns. That makes this table two things at
# once: the FALLBACK when the model omits or fails its planning half, and the
# BASELINE that shadow mode measures the model against. If the model cannot
# beat this, there is no reason to promote it (spec §7).

def project_next_move(uclass, appointment) -> str:
    """Derive the next move from existing signals alone. No API, no model.

    Ordered as a priority ladder, not a lookup: several signals can be true at
    once, and which one wins is the actual policy. Exit/complaint first, per
    the house rule that they outrank all flow logic.
    """
    from bot.unified_classifier import (
        uc_intent, uc_is_photo_request, uc_speech_act,
        uc_pivoted_to_timeline, uc_offered_timeframe,
    )

    intent = uc_intent(uclass)
    speech_act = uc_speech_act(uclass)

    # 1. Anger, complaints and price/quote disputes leave the flow entirely.
    if intent == 'complaint':
        return 'escalate_to_human'

    # 2. Work we do not do.
    if intent == 'out_of_scope':
        return 'out_of_scope_redirect'

    # 3. They are deferring, or they answered a field question with a timeframe
    #    instead. Both mean the same thing: lower the ask, keep the destination.
    if (intent == 'delay_signal'
            or uc_pivoted_to_timeline(uclass)
            or uc_offered_timeframe(uclass)):
        return 'slow_lead_nudge'

    # 4. A pure acknowledgement adds nothing and needs nothing added back.
    if intent == 'ack':
        return 'close_pleasantry'

    # 5. Asking us to PRICE A WHOLE JOB is not the same as asking what a
    #    fixture costs. There is no honest figure for a job before someone has
    #    seen it, so a quote request is answered with value and the visit.
    #
    #    A plain price question is NOT routed here. The visit fee is the only
    #    price we VOLUNTEER (owner rule, 2026-09-05), but a lead who asks what
    #    a shower cubicle costs still gets the from-price and the disclaimer,
    #    the same as today. Refusing them a number they asked for reads as
    #    dodging, and they go and ask someone who answers. That is the existing
    #    _asks_price_figure / _asks_for_quote split, kept.
    if speech_act == 'quote_request' and not description_captured(appointment):
        return 'present_value'

    # 6. Nothing on file at all: this is first contact.
    if not service_captured(appointment) and not description_captured(appointment):
        return 'greet' if not _has_spoken_before(appointment) else 'ask_qualifying_question'

    # 7. The revised order: description FIRST, then proof.
    if not description_captured(appointment):
        return 'ask_qualifying_question'

    # 8. Description in hand and the gallery has not gone yet — or they asked
    #    for it outright, which wins whenever we still have photos to send.
    if not work_already_shown(appointment):
        return 'show_work'
    if uc_is_photo_request(uclass):
        return 'show_work'

    # 9. Work shown and we know where they are: this is the close — unless we
    #    already hold their plan, in which case there is nothing to measure up
    #    and the fee close would be asking them to pay for work their own
    #    drawing already did (spec §10).
    if area_captured(appointment):
        return apply_plan_path_gate('book_visit', appointment)

    # 10. Everything else is one more qualifying question.
    return 'ask_qualifying_question'


def _has_spoken_before(appointment) -> bool:
    """More than the message currently being handled.

    The current turn is already logged by the time the router runs, so ">1" is
    what "they have spoken before" means here — the same convention
    `_conversation_underway` uses.
    """
    history = getattr(appointment, 'conversation_history', None) or []
    turns = 0
    for entry in history:
        if not isinstance(entry, dict) or entry.get('role') != 'user':
            continue
        if str(entry.get('content') or '').startswith('['):
            continue
        turns += 1
    return turns > 1


# ── Phase 2: letting the controller actually drive ───────────────────────────
# OFF by default, and deliberately so. The spec's own gate for Phase 2 is "high
# agreement on the happy path", measured from Phase 0 shadow logs on real
# traffic. That data does not exist yet, so the wiring ships dark: everything
# below is built, tested and reachable, and nothing runs until someone reads
# the agreement rate and turns it on.
#
# Set PLUMBOT_CONTROLLER_ROUTING=1 to enable.
import os

# ON. The owner asked for the model to do the thinking, not just the reading:
# to choose the question that moves THIS conversation forward rather than the
# next item in a fixed list, and to answer in context.
#
# What keeps that safe is not the flag, it is what sits under it. The
# confidence floor, the fee gate, the plan-path gate, and above all
# choose_question's refusal to re-open a field we already hold — the single
# most repeated bug in this codebase, and the one that handing question choice
# to a model would most directly bring back.
#
# Set PLUMBOT_CONTROLLER_ROUTING=0 to put everything back on the deterministic
# order without a deploy.
CONTROLLER_DRIVES_ROUTING = os.environ.get(
    'PLUMBOT_CONTROLLER_ROUTING', '1') != '0'

# The moves the controller is allowed to take when it is driving. The rest stay
# with the existing router, which already handles them well: show_work has the
# photo path and ask_qualifying_question is the whole booking flow. Promoting
# those buys nothing and risks a lot.
DRIVABLE_MOVES = frozenset({
    'book_visit', 'handle_objection', 'show_work',
    # Promoted 2026-09-07 on the replay evidence. `close_pleasantry` was the
    # model's single most-discarded call: 15 of the ~34 disagreements over 100
    # conversations, every one of them the model saying "this is finished" and
    # the router asking another qualifying question. Production did it again on
    # a lead who had just said "Noted" and got "Anything else on the property
    # that needs looking at?" back.
    #
    # It is also the safest to promote, because it is the only one of the
    # remaining moves that is purely a message. The others are NOT simple
    # promotions and are deliberately still excluded:
    #   slow_lead_nudge      the delay flow behind it writes a pending state
    #                        and schedules a check-back date; driving the
    #                        message alone would send the words and lose the
    #                        machinery.
    #   out_of_scope_redirect / escalate_to_human
    #                        both already fire from their own signals earlier
    #                        in the router; promoting them risks answering the
    #                        same turn twice.
    #   greet / ask_qualifying_question
    #                        ask_qualifying_question IS the fall-through, so
    #                        the model "driving" it is what already happens.
    #   present_value        the model chose it 0 times in 339 turns; there is
    #                        nothing to promote yet.
    'close_pleasantry',
})


def should_show_work(appointment) -> bool:
    """Is this the moment to send the proof?

    The close is: they describe the job, we show two or three finished ones
    like it, THEN we ask where they are, and only then name the fee.
    Showing the work is what builds the want that makes the fee reasonable,
    so a fee raised before it is a fee raised to someone who has seen
    nothing of what they are buying.

    Two conditions, both deterministic:
      - we know what the job is, or there is nothing to match photos to
      - we have not already shown them, which is a fact we own
    """
    return (description_captured(appointment)
            and not work_already_shown(appointment))


def _booking_half_made(appointment) -> bool:
    """A slot is on file but the booking never completed."""
    slot = getattr(appointment, 'scheduled_datetime', None)
    status = str(getattr(appointment, 'status', '') or '')
    return bool(slot) and status != 'confirmed'


def _asks_us_something(uclass) -> bool:
    """Did this turn carry a question for us?

    Deterministic and deliberately generous: a false positive costs one extra
    qualifying question, a false negative closes the conversation on somebody
    who just asked for something.
    """
    try:
        from bot.unified_classifier import uc_intent, uc_is_photo_request
        if uc_is_photo_request(uclass):
            return True
        if uc_intent(uclass) in ('price_question', 'out_of_scope', 'complaint'):
            return True
    except Exception:
        pass
    payload = uclass if isinstance(uclass, dict) else {}
    speech = str(payload.get('speech_act') or '').lower()
    return speech in ('question', 'price_ask', 'quote_request')


def decide_move(uclass, appointment):
    """The move to take this turn, or None to leave it to the old router.

    One place, so the confidence floor and both gates cannot be applied in
    three slightly different ways at three call sites. Returns None generously:
    a turn the controller is not sure about is a turn the existing flow should
    have, and the existing flow is not broken.
    """
    if not CONTROLLER_DRIVES_ROUTING:
        return None
    move = plan_next_move(uclass)
    if move is None:
        return None
    if plan_confidence(uclass) < CONFIDENCE_FLOOR:
        logger.info("Controller move %s below the floor, leaving it to the "
                    "old router", move)
        return None
    move = apply_fee_gate(move, appointment, plan_want_level(uclass))
    move = apply_plan_path_gate(move, appointment)

    # THE PROJECTION MAY PROMOTE show_work, AND ONLY show_work.
    #
    # Measured over 100 replayed conversations (339 turns, real DeepSeek): on
    # the 215 turns where the lead had described the job and the photos had not
    # gone yet, the model picked show_work TWICE. The deterministic projection
    # wanted it on 61 of those same turns. Left to the model alone the proof
    # step reached 3 turns in 339 — a step that is built, gated and tested, and
    # that in practice never ran.
    #
    # This is a prompt bias, not a judgement we should trust: ask_qualifying_
    # question is 77% of every move the model makes, so "ask something" wins by
    # default rather than on the merits. The projection is derived from signals
    # the classifier already returned, and is right about this one.
    #
    # Safe to promote because the proof step is self-limiting three times over:
    # should_show_work() needs the job known and the photos unsent,
    # items_for_job() returns nothing unless a photo actually matches what they
    # described, and the send path carries the next question out BEHIND the
    # images — so showing work never costs us the question the model wanted to
    # ask. Nothing else is promoted: every other move is the model's call.
    projected = project_next_move(uclass, appointment)
    if move != 'show_work' and projected == 'show_work':
        move = apply_plan_path_gate('show_work', appointment)

    # THE PROJECTION MAY ALSO PROMOTE book_visit, BUT ONLY AT wants_it.
    #
    # Same measurement, the other end of the funnel: of 12 turns the model
    # marked want_level 'wants_it', it chose book_visit on 5 and asked yet
    # another qualifying question on 6. That is the one moment this whole flow
    # exists to catch, and it was being missed about half the time.
    #
    # Narrower than the show_work promotion, because book_visit is the move
    # that states the fee. Three things must all hold: the model itself says
    # the lead wants it, the projection independently agrees this is the close
    # (which by step 9 means the work has been shown AND we know their area),
    # and the plan-path gate still gets its say. Below wants_it nothing is
    # promoted, so apply_fee_gate's rule is not weakened, only enforced from
    # the other side.
    if (move != 'book_visit' and projected == 'book_visit'
            and plan_want_level(uclass) == 'wants_it'):
        move = apply_plan_path_gate('book_visit', appointment)

    if move not in DRIVABLE_MOVES:
        return None

    # A closing pleasantry ENDS the turn, so it must never swallow a question.
    # The house rule is that the customer's own words outrank any gate: if they
    # asked us something, answering it beats acknowledging them, whatever the
    # model concluded.
    if move == 'close_pleasantry' and _asks_us_something(uclass):
        logger.info('close_pleasantry held back: the customer asked something')
        return None

    # ...and never while a booking is half-made. A slot can be captured by the
    # extraction flow (scheduled_datetime set) while the booking itself never
    # completes (status still 'pending'), and in that state the conversation is
    # the only thing still driving it forward: the confirmation, the plumber
    # alert and the name and email asks all hang off the booking completing.
    #
    # Barmak lead 1144 is the case. "Thursday afternoon, 3pm?" stored the slot
    # and left the status pending; the lead said "Thank you"; this move closed
    # the conversation; and the plumber was never told, the name and email were
    # never asked for, and the visit sat in the diary as an unconfirmed row.
    if move == 'close_pleasantry' and _booking_half_made(appointment):
        logger.info('close_pleasantry held back: a booking is still open')
        return None
    # The proof is worth sending once, and only once we know what to match it
    # against. A model that asks for it twice, or before the job is known, is
    # asking for a generic gallery — which is what the request-only path was
    # already there for.
    if move == 'show_work' and not should_show_work(appointment):
        return None
    return move


# ── Shadow mode (spec §7, Phase 0) ───────────────────────────────────────────
# Phase 0 ships NOTHING to customers. It logs what the controller would have
# done beside what the router actually did, so the agreement rate is measurable
# on real traffic before a single branch is promoted.
#
# Two lines per turn, both greppable:
#   [SHADOW-PLAN]   what the controller proposed, and what the table projected
#   [SHADOW-BRANCH] which branch the router really took, and whether they agreed
#
# The plan line is emitted unconditionally so a turn is observable even if it
# exits down a path that never reports its branch.

SHADOW_PLAN_PREFIX = '[SHADOW-PLAN]'
SHADOW_BRANCH_PREFIX = '[SHADOW-BRANCH]'

# Where the router's branch is stashed between the two log lines. A transient
# attribute on the in-memory Appointment, never a column and never saved: it
# lives for exactly one turn and a new per-turn field would otherwise mean a
# migration for a diagnostic.
_PLAN_ATTR = '_controller_plan'


def record_plan(appointment, uclass, message: str = '') -> dict:
    """Log the controller's proposal and stash it for the branch comparison.

    Returns the stashed plan so a caller can read it without touching the
    attribute directly. Never raises: shadow logging that can break a live turn
    is worse than no shadow logging.
    """
    try:
        model_move = plan_next_move(uclass)
        projected = project_next_move(uclass, appointment)
        confidence = plan_confidence(uclass)
        want = plan_want_level(uclass)
        errors = validate_turn(uclass) if uclass else ['no classifier result']

        plan = {
            'model_move': model_move,
            'projected_move': projected,
            'confidence': confidence,
            'want_level': want,
            'usable': not errors,
            'reasoning': plan_reasoning(uclass),
            # The raw pick. choose_question() validates it against what we
            # already hold at the moment the flow asks, not here, because the
            # answer can change within the turn as fields are extracted.
            'question': plan_next_question(uclass) if not errors else None,
        }
        setattr(appointment, _PLAN_ATTR, plan)

        logger.info(
            "%s model=%s projected=%s conf=%.2f want=%s usable=%s msg=%r",
            SHADOW_PLAN_PREFIX, model_move, projected, confidence,
            want, not errors, (message or '')[:60],
        )
        if errors:
            logger.info("%s planning unusable: %s", SHADOW_PLAN_PREFIX,
                        '; '.join(errors[:3]))
        return plan
    except Exception:
        logger.warning("Shadow plan logging failed", exc_info=True)
        return {}


def question_for(appointment, default):
    """The question the controller picked for this turn, or `default`.

    Reads the stash `record_plan` left on the in-memory appointment, so the
    booking flow can consult the controller without every caller having to
    carry the classification around. Returns `default` unchanged when the
    controller is off, said nothing, or picked something it may not have.
    """
    if not CONTROLLER_DRIVES_ROUTING:
        return default
    plan = getattr(appointment, _PLAN_ATTR, None) or {}
    if not plan.get('question'):
        return default
    payload = {'next_question': plan.get('question'),
               'move_confidence': plan.get('confidence', 0.0)}
    return choose_question(payload, appointment, default)


def note_branch(appointment, branch: str) -> None:
    """Record which router branch actually handled this turn.

    Called from the router's branches. Cheap by design — one log line — because
    Phase 0's entire job is to produce the agreement rate that gates Phase 2.
    """
    try:
        plan = getattr(appointment, _PLAN_ATTR, None) or {}
        model_move = plan.get('model_move')
        projected = plan.get('projected_move')
        try:
            from bot.services.clients import turn_call_count
            calls = turn_call_count()
        except Exception:
            calls = -1
        logger.info(
            "%s branch=%s model=%s projected=%s model_agreed=%s "
            "projection_agreed=%s calls=%s",
            SHADOW_BRANCH_PREFIX, branch, model_move, projected,
            model_move == branch, projected == branch, calls,
        )
    except Exception:
        logger.warning("Shadow branch logging failed", exc_info=True)
