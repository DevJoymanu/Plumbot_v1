"""
bot/unified_classifier.py
=========================
Single DeepSeek call that replaces the following separate calls per message:
  1. out_of_scope_handler.classify_message      (OOS intent)
  2. views.detect_service_inquiry               (product intent)
  3. views.extract_all_available_info_with_ai   (booking data)
  4. is_previous_work_photo_request             (photo flag)
  5. repeated_question pre-classifier           (is this a repeat?)
  6. handle_plan_later_response pre-check       (plan-later flag)

Returns a single dict that all downstream functions consume.
Falls back gracefully to None — callers must handle None by running their
own individual classifiers as before.
"""

from __future__ import annotations

import json
import logging
import os

from django.conf import settings
from openai import OpenAI

from .utils import business_name_for

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if key:
            _client = OpenAI(api_key=key, base_url="https://api.deepseek.com/v1")
    return _client


_SYSTEM = """\
You are a message classifier for {business} (Zimbabwe).
Customers write in English, Shona, or a mix. TODAY = {today}.
Return ONLY valid JSON — no markdown, no explanation.

─── INTENT (pick one) ────────────────────────────────────────────────────────
in_scope      Normal plumbing inquiry, product question, booking info, or
              any message that should continue the booking conversation.
              Shona examples: "Ndoda kubhukisha" (I want to book),
              "Ndoda kushandura chimbuzi" (I want to change the toilet),
              "Mutengo weshower chii?" (What's the shower price?),
              "Mune tub here?" (Do you have tubs?),
              "Ndichatumira plan mangwana" (I'll send the plan tomorrow).
out_of_scope  Service we do NOT offer: {out_of_scope_services}, etc.
delay_signal  Customer is deferring: "call me later", "not ready yet",
              "will come back", "I'm busy", "abroad", "still building".
              Shona examples: "Ndiri kunze kwenyika" (I'm out of the country),
              "Ndichadzokezai" (I'll call you back), "Mbichana" (just a bit later),
              "Ndichaita contact" (I'll make contact), "Tichataura" (we'll talk),
              "Ndisati ndagadzirira" (I'm not ready yet),
              "Ndicharidza rini ndichadzoka" (I'll call when I return).
              ⚠ NOT a delay if the customer names a specific day or time they
              WILL be available — even alongside a temporary absence. "I'm out of
              Harare but Wednesday I'm available", "away this week, free Sunday",
              "ndiri kunze asi neChina ndinenge ndiripo" → in_scope, and put that
              day into extracted.availability. Open-ended absence with NO stated
              day stays delay_signal.
complaint     Frustration, price objection, or legitimacy question.
              Shona examples: "Mutengo unodhura zvakanyanya" (price is way too expensive),
              "Musatikwashura" (don't cheat us), "Hamusi vaplumber chaiwo here?" (Are you real plumbers?),
              "Munoitirei inodhura kudaro?" (why is it that expensive?).
ack           Pure acknowledgment with zero booking intent:
              "ok", "sharp", "thanks", "noted", "fine", "sure", "👍", "👌".
              Shona acks: "maita", "maita basa", "ndatenda", "mazvita",
              "zvakanaka", "zvaita", "ndinzwisisa", "hongu", "ehe", "shuwa".
              Only use "ack" when the message adds NOTHING to the conversation.

─── SERVICE TYPE (one or null) ───────────────────────────────────────────────
bathroom_renovation   — upgrading or remodelling an EXISTING bathroom
bathroom_installation — fitting out a brand new bathroom space from scratch
kitchen_renovation    — upgrading or remodelling an EXISTING kitchen
kitchen_installation  — fitting out a brand new kitchen space from scratch
bathroom_and_kitchen_renovation, new_plumbing_installation,
drain_unblocking, pipe_repair, geyser_repair, toilet_repair

Use bathroom_installation / kitchen_installation when the customer says
"install a bathroom", "build a bathroom", "new bathroom in a new room",
"want a kitchen installed", "fit a kitchen" etc.
Use bathroom_renovation / kitchen_renovation when they say "renovate",
"redo", "upgrade", "remodel", or describe work in an existing bathroom/kitchen.

─── PRODUCT INTENT (most specific, or "none") ────────────────────────────────
tub_sales        Any message asking about tub price/cost — "how much tub",
                 "tub price", "how much is a tub", "do you sell tubs".
                 ⚠ Prefer tub_sales over combined_pricing whenever "tub" is mentioned.
standalone_tub   Specifically freestanding/standalone tub. A CORNER tub is NOT
                 standalone — it's a built-in tub, so use tub_sales, not standalone_tub.
geyser, shower_cubicle, vanity, bathtub_installation, toilet, chamber,
drain_unblocking, pipe_repair, geyser_repair, toilet_repair,
location_ask, location_visit, pictures, combined_pricing, none
{tenant_services}

─── EXTRACT (null when not present in message) ───────────────────────────────
area              Suburb, neighbourhood, or city name.
                  Zimbabwe examples: Hatfield, Avondale, Borrowdale, Ziko,
                  Highfields, Glen View, Mbare, Chitungwiza, Ruwa, Gweru.
                  ⚠ When next_question is "area", treat short unknown words
                  as suburb names — NOT customer names.
availability      Date+time → YYYY-MM-DDTHH:MM  |  Date only → YYYY-MM-DDT00:00
                  A bare weekday ("Wed", "Wednesday", "neChitatu") → the NEXT
                  future date with that weekday relative to TODAY.
                  Shona weekdays — map exactly, do NOT guess:
                    Svondo=Sunday, Muvhuro=Monday, Chipiri=Tuesday,
                    Chitatu=Wednesday, China=Thursday, Chishanu=Friday,
                    Mugovera=Saturday. The "ne" prefix means "on"
                    (neChina = on Thursday, neChipiri = on Tuesday).
                  Shona times: mangwanani=morning, masikati=afternoon,
                    manheru=evening. "mangwana"=tomorrow, "nhasi"=today.
                  "available all day" / "anytime" / "whole day" → null.
customer_name     Only if explicitly given: "my name is X", "I'm X", "call me X".
project_description  Verbatim project detail (max 120 chars).

─── ENGLISH (the rule engine reads this, the customer never does) ────────────
english           A plain, literal English rendering of the customer's message.
                  "" when the message is already English. Translate what they
                  SAID, do not answer it, summarise it or tidy it up: keep the
                  intent words intact, because deterministic rules downstream
                  match on phrases like "send it here", "I will get back to
                  you", "how much". Keep names, places, numbers and prices
                  exactly as written. Shona notes: "ipapa"/"pano"/"apa" = here,
                  "senda"/"tumira" = send, "ndichakubata" = I will get in touch,
                  "kuronga mari" = sort out the money, "marii"/"imarii" = how
                  much, "ma-" is just a plural prefix ("matiles" = tiles).

─── FLAGS ────────────────────────────────────────────────────────────────────
is_photo_request  true if customer asks to see photos/pictures/portfolio of
                  our PREVIOUS work (not product pictures).
is_plan_later     true if customer says they'll send their plan/blueprint/
                  drawing at a later time ("I'll send the plan later").
is_repeat_question  true if the customer is asking something that has clearly
                  already been answered earlier in the conversation.

─── QUALIFICATION SIGNALS (judged against next_question) ─────────────────────
answered_current_question  true if the message actually answers next_question
                  (next_question=area → they name a suburb; =availability_date /
                  availability_time → they give a day/time). false if they ignored
                  it or asked something else instead.
pivoted_to_timeline  true if — INSTEAD of answering — the customer asked about or
                  raised WHEN / scheduling: "when can you come", "how soon", "are
                  you free this week", "you free Friday?", or gave a timeframe in
                  place of the field asked. Shona: "munouya rini", "nguvai",
                  "mungakwanisa here svondo rino".
offered_date      If the message implies a specific calendar day, resolve it to an
                  absolute YYYY-MM-DD relative to TODAY (same weekday / relative
                  rules as availability). "next Thursday", "the 8th", "this Friday",
                  "neChina" → the date. A vague timeframe ("next week", "sometime
                  soon") → null (see offered_timeframe). Date only — no time here.
offered_timeframe A soft, non-specific timeframe when NO hard date is given, e.g.
                  "sometime next week", "end of the month", "in a couple of weeks",
                  "kupera kwemwedzi". Else null.
speech_act        WHAT KIND of message this is — what the customer is DOING, not
                  what it is about. Pick exactly one:
                  "quote_request"  asking us to price a JOB, or asking us to come
                                   and do work: "I'd like a quote for plumbing
                                   services", "can you renovate my bathroom",
                                   "I need someone to fit a tub and shower".
                  "capability"     asking WHETHER we do something, not for a price:
                                   "do you do bathroom renovations?", "do you
                                   install geysers?", "mune tub here?".
                  "price_ask"      asking what something COSTS: "how much is a
                                   shower cubicle", "marii", "mutengo weshower".
                  "logistics"      asking WHO comes, WHEN, HOW LONG it takes, or
                                   about guarantees/payment: "who would be coming
                                   to do the work?", "how long does it take?",
                                   "is your work guaranteed?".
                  "booking_answer" answering a question we asked (an area, a day,
                                   a name, a yes/no to our question).
                  "other"          anything else.
                  A message can mention work WITHOUT being a quote_request —
                  "who would be coming to do the work?" is logistics, and "do you
                  do bathroom renovations?" is capability. Read the verb that
                  belongs to the CUSTOMER, not the words about the job.
new_build         The customer's own word for a structure that is NEW or still
                  going up, when plumbing would go INTO it. Return ONE lowercase
                  noun — EXACTLY the word they used, so it can be said back to
                  them (they wrote "building" → "building", never "house").
                  Otherwise null.
                  Yes: "new house", "it's a new building", "cost of wiring a new
                  4 bedroom house", "I'm building a place in Ruwa", "the house is
                  still under construction", "we've just finished the slab",
                  "imba itsva", "ndiri kuvaka imba", "chivakwa chitsva".
                  A structure counts even when the customer asks about a trade we
                  do NOT do (wiring, roofing): the building still needs plumbing,
                  and intent is judged separately.
                  No → null: a REFIT of something that already exists ("a new
                  bathroom in my house", "renovating my house", "doing the
                  bathroom from scratch" — a room is not a structure); a new
                  FIXTURE ("new toilet", "new geyser", "new shower cubicle"); and
                  a PLACE NAME that merely contains the word — the Harare suburb
                  "Dzivarasekwa Extension" is an area, not a building.

─── WORKED EXAMPLES (input → output) ─────────────────────────────────────────
These show the EXACT reasoning for the cases that get misclassified most often.
Match the pattern, do not copy values blindly.

# Asking WHO comes is logistics, never a request for a quote. The words "do the
# work" describe the job, but the customer is asking about people:
"who would be coming to do the work?"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"speech_act":"logistics","extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# Asking WHETHER we do something is capability, not a quote request:
"Do you do bathroom renovations?"
{"intent":"in_scope","confidence":"HIGH","service_type":"bathroom_renovation","product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"speech_act":"capability","extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# Asking us to DO the work is a quote request:
"I need someone to renovate my bathroom"
{"intent":"in_scope","confidence":"HIGH","service_type":"bathroom_renovation","product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"speech_act":"quote_request","extracted":{"area":null,"availability":null,"customer_name":null,"project_description":"renovate my bathroom"}}

# Short product price ask — map the product word, even with typos:
"standalone tub hw much"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"standalone_tub","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# "rain shower" / "rain head" is a SHOWER, never a tub:
"how much for a rain shower"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"shower_cubicle","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# "cubicle" / "shower cubicle" / "bathroom cubicle" is a SHOWER, never a tub:
"did you sell bathroom cubicles"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"shower_cubicle","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# Code-switched booking — pull every field present:
"Ndoda kubhukisha geyser repair, ndiri Hatfield, Sunday morning"
{"intent":"in_scope","confidence":"HIGH","service_type":"geyser_repair","product_intent":"geyser_repair","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":"Hatfield","availability":null,"customer_name":null,"project_description":"geyser repair"}}

# Partial answer when next_question=area — a short unknown word is a SUBURB, not a name:
(Appointment: ... | next_question=area)  "Ziko"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":"Ziko","availability":null,"customer_name":null,"project_description":null}}

# Genuinely ambiguous one-word message with no context — LOW confidence so the
# deterministic layer can take over:
"this one"
{"intent":"in_scope","confidence":"LOW","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# Temporary absence BUT a named available day → in_scope booking, NOT a delay.
# Capture the weekday as the next matching date (here TODAY=2026-06-11 Thursday,
# so "Wed" → 2026-06-17; always recompute against the real TODAY):
"M out of Hre but Wed I will be available"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":null,"availability":"2026-06-17T00:00","customer_name":null,"project_description":null}}

# Shona delay signal (open-ended, no day named):
"Ndichadzokezai, ndisati ndagadzirira"
{"intent":"delay_signal","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# Pure Shona acknowledgment — adds nothing to the conversation:
"maita basa"
{"intent":"ack","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# Date-stage pivot — soft far-out timeframe, no hard day (TODAY=2026-07-01,
# next_question=availability_date). offered_date stays null:
(Appointment: service=bathroom_renovation, area=Hatfield | next_question=availability_date)  "maybe end of the month"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"answered_current_question":false,"pivoted_to_timeline":true,"offered_date":null,"offered_timeframe":"end of the month","extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# Date-stage pivot — near specific day. TODAY=2026-07-01 (Wednesday), "this
# Friday" → 2026-07-03; recompute against the real TODAY:
(Appointment: service=bathroom_renovation, area=Hatfield | next_question=availability_date)  "are you free this Friday?"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"answered_current_question":false,"pivoted_to_timeline":true,"offered_date":"2026-07-03","offered_timeframe":null,"extracted":{"area":null,"availability":"2026-07-03T00:00","customer_name":null,"project_description":null}}

# A new build is a new build even when the trade they asked about is not ours.
# The wiring is out of scope; the house going up is a full plumbing job, and the
# noun comes back as THEY wrote it:
"Cost of wiring a new 4 bedroom house"
{"intent":"out_of_scope","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"speech_act":"price_ask","new_build":"house","extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# Answering our clarification with the structure — still a new build:
(Appointment: ... | next_question=service_type)  "It's a new building"
{"intent":"in_scope","confidence":"HIGH","service_type":"new_plumbing_installation","product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"speech_act":"booking_answer","new_build":"building","extracted":{"area":null,"availability":null,"customer_name":null,"project_description":null}}

# A ROOM being redone is not a new structure — new_build stays null:
"I need a new bathroom in my house"
{"intent":"in_scope","confidence":"HIGH","service_type":"bathroom_renovation","product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"speech_act":"quote_request","new_build":null,"extracted":{"area":null,"availability":null,"customer_name":null,"project_description":"new bathroom in my house"}}

# A suburb whose NAME contains a building word is an area, not a build:
(Appointment: ... | next_question=area)  "Dzivarasekwa extension"
{"intent":"in_scope","confidence":"HIGH","service_type":null,"product_intent":"none","is_photo_request":false,"is_plan_later":false,"is_repeat_question":false,"speech_act":"booking_answer","new_build":null,"extracted":{"area":"Dzivarasekwa Extension","availability":null,"customer_name":null,"project_description":null}}

─── PLANNING (what we should DO next) ────────────────────────────────────────
You are also the sales planner. After classifying, decide the next move.

reasoning     Plan before you commit to next_move. Internal only, never shown
              to the customer. HARD LIMIT 40 words — this shares the token
              budget with the JSON body, and an overrun truncates the whole
              object into unparseable text.
want_level    How badly they want the job: "cold" (asking around),
              "interested" (engaging, asked about the work), "wants_it" (they
              have described the job and are leaning in). This is the FEE GATE:
              the visit fee is never raised below "wants_it".
next_move     Exactly one of:
  greet                    first contact, nothing on file yet
  ask_qualifying_question  we still need the project description, the area or
                           the timeline. ONE question at a time.
  show_work                send 2-3 example photos. ONLY once the project
                           description is known: proof lands harder when it
                           matches what they just described.
  present_value            they asked us to price a WHOLE JOB before anyone
                           has seen it. There is no honest figure for that yet,
                           so answer with value and the visit. This is NOT for
                           a plain price question: a lead who asks what a
                           fixture costs gets the price, and the move for that
                           is ask_qualifying_question while the system answers.
  book_visit               description known, work shown, area known, they are
                           leaning in. The system states the fee and offers the
                           days. You never name either.
  handle_objection         they hesitated on the fee or the visit
  slow_lead_nudge          they are deferring, or answered with a timeframe
                           instead of the field we asked for
  escalate_to_human        anger, a dispute about a quote they were given, or
                           they asked for a person
  out_of_scope_redirect    work we do not do
  close_pleasantry         a pure acknowledgement that needs nothing back
next_question  WHICH field to ask about, when next_move is
              ask_qualifying_question. One of: project_description, area,
              timeline, availability_date, availability_time, name. null for
              any other move.
              Pick the one that moves THIS conversation forward, not the next
              one in a list. A lead who has just described a leak in detail
              does not need "what needs doing"; a lead who named their suburb
              two messages ago does not need "where are you". Never choose a
              field the appointment state above already shows as filled.
move_confidence  0.0-1.0, how sure you are of next_move. Below 0.6 the system
              ignores your move and uses its own rules, so a low number is a
              useful answer, not a failure. NOTE this is a different field from
              "confidence" above, which stays HIGH/LOW and describes the
              CLASSIFICATION. Return both.

WHEN SOMETHING IS UNCLEAR, ASSUME. Never ask an open "what do you mean?" or
"could you clarify?" — that hands the customer the job of working out what you
missed, and most people answer it by repeating themselves. Offer the two most
likely readings and let them pick:
  "Do you mean the tub on its own, or fitted?"
  "Is it the whole bathroom, or just the shower?"
Use their own words back. One reading if you can only see one, as a yes or no.
Set next_move to ask_qualifying_question for these.

IF THEY ASK SOMETHING YOU ALREADY ANSWERED, the first answer did not land. Do
not repeat it. Say the same fact in plainer words, more concretely, then one
assumptive clarifier as above.

ORDER THAT MATTERS: description first, then show the work, then the area, then
the close. Never the fee before "wants_it".

PRICE: the visit fee is the only price we ever bring up on our own. Never lead
with a job price and never guess one. But when the customer ASKS what something
costs, they get an answer: the system holds the real figures and states them.
Do not put any figure in your own text.

─── OUTPUT FORMAT (return exactly this structure) ────────────────────────────
{
  "reasoning": "",
  "next_move": "ask_qualifying_question",
  "next_question": "area",
  "move_confidence": 0.8,
  "state_update": {"want_level": "interested"},
  "intent": "in_scope",
  "confidence": "HIGH",
  "service_type": null,
  "product_intent": "none",
  "is_photo_request": false,
  "is_plan_later": false,
  "is_repeat_question": false,
  "answered_current_question": false,
  "pivoted_to_timeline": false,
  "offered_date": null,
  "offered_timeframe": null,
  "speech_act": "other",
  "new_build": null,
  "english": "",
  "extracted": {
    "area": null,
    "availability": null,
    "customer_name": null,
    "project_description": null
  }
}
confidence is HIGH when the classification is clear, LOW when ambiguous.\
"""


def _out_of_scope_services(appointment) -> str:
    """The prompt's 'services we do NOT offer' list, for THIS lead's tenant.

    Falls back to the full list on any error — the safe direction, since an
    over-broad list only declines work, while an under-broad one would have the
    bot accept jobs the tenant can't do.
    """
    from .out_of_scope_handler import OOS_SERVICE_TERMS, out_of_scope_terms_for
    try:
        terms = out_of_scope_terms_for(getattr(appointment, 'tenant', None))
    except Exception:
        terms = OOS_SERVICE_TERMS
    return ", ".join(terms or OOS_SERVICE_TERMS)


def _tenant_product_intents(appointment) -> str:
    """The product-intent keys for services only THIS tenant sells.

    The hardcoded list above is Homebase's product range. A tenant who also
    tiles, fits gutters or sinks pumps has those on their OWN price sheet, and
    with no intent to return for them the model fell back to combined_pricing —
    so a tiling question was answered with the bathroom package (prod, barmak,
    2026-08-28). Empty for a tenant with no such rows, leaving the prompt
    byte-identical to before.
    """
    try:
        from .tenant_config import get_config
        from .pricing_copy import tenant_custom_items
        items = tenant_custom_items(get_config(getattr(appointment, 'tenant', None)))
    except Exception:
        return ""
    if not items:
        return ""
    lines = [
        "",
        "This business ALSO sells the services below. They are its own work —",
        "never out of scope. Return the key on the left as product_intent when",
        "the customer asks about one, including misspelt, plural or Shona",
        'wording (Shona often prefixes "ma-": "matiles" = tiles):',
    ]
    for key, item in sorted(items.items()):
        label = (item.label or item.short_label or item.family or '').strip()
        lines.append(f"{key}   {label}")
    return "\n".join(lines)


def unified_turn(
    message: str,
    appointment=None,
    conversation_history=None,
    today_date: str = "",
    next_question: str = "",
) -> dict | None:
    """
    Make one DeepSeek call and return comprehension + extraction + a PLAN.

    The classification half is unchanged and load-bearing; the planning half
    (`reasoning`, `next_move`, `confidence`, `state_update.want_level`) is
    additive and, in Phase 0, drives nothing — see bot/controller.py.

    Returns None only when the body could not be parsed at all, in which case
    callers fall back to their individual classifiers exactly as before. A
    malformed PLAN never produces None: the classification is returned with the
    planning keys stripped instead.
    """
    client = _get_client()
    if not client:
        return None

    # ── Appointment state summary ─────────────────────────────────────────────
    state_parts = []
    if appointment:
        if getattr(appointment, "project_type", None):
            state_parts.append(f"service={appointment.project_type}")
        if getattr(appointment, "customer_area", None):
            state_parts.append(f"area={appointment.customer_area}")
        if getattr(appointment, "scheduled_datetime", None):
            state_parts.append("datetime=set")
        if getattr(appointment, "status", None):
            state_parts.append(f"status={appointment.status}")
    apt_state = ", ".join(state_parts) if state_parts else "new lead"

    if next_question:
        apt_state += f" | next_question={next_question}"

    # ── Recent conversation (last 6 turns, 80 chars each) ────────────────────
    history = conversation_history or []
    lines = []
    for turn in history[-6:]:
        role    = "Customer" if turn.get("role") == "user" else "Bot"
        content = (turn.get("content") or "").strip()[:80]
        if content and not content.startswith("["):
            lines.append(f"{role}: {content}")
    context = "\n".join(lines) if lines else "(start of conversation)"

    user_content = (
        f"Appointment: {apt_state}\n"
        f"Conversation:\n{context}\n\n"
        f"Customer message: \"{message}\""
    )

    messages = [
        {
            "role": "system",
            # The lead's OWN business name. This prompt used to name
            # HomeBase for every tenant, priming the model with the
            # wrong company on every classification.
            "content": (
                _SYSTEM
                .replace("{today}", today_date)
                .replace("{business}", business_name_for(appointment))
                # The out-of-scope list is Homebase's. Naming a service
                # THIS tenant advertises would have the model decline
                # their own work (prod: barmak sells boreholes).
                .replace("{out_of_scope_services}",
                         _out_of_scope_services(appointment))
                # The tenant's OWN extra services, so the model has a
                # key to return for work Homebase's product list never
                # names (barmak: tiling, gutters, pumps, filters).
                .replace("{tenant_services}",
                         _tenant_product_intents(appointment))
            ),
        },
        {"role": "user",   "content": user_content},
    ]

    first = _call_once(messages)
    if first is None:
        # Unparseable on the first go. One retry, which is the entire failure
        # tail (spec §4) — beyond that the caller's own classifiers are cheaper
        # than a third round-trip.
        logger.info("unified_turn: unparseable body, retrying once")
        second = _call_once(messages)
        if second is None:
            return None
        first = second

    from bot.controller import validate_turn, planning_attempted
    errors = validate_turn(first)
    if not errors:
        logger.debug("unified_turn result: %s", first)
        return first

    # No plan at all: the planning block is not landing (stale deploy, a model
    # ignoring it, a truncated body). A retry would not fix that and WOULD
    # double the call count on the bot's busiest path, so take the
    # classification and move on. Loud at info level because a permanently
    # planless controller is a thing to notice, not to absorb.
    if not planning_attempted(first):
        logger.info("unified_turn: no plan in the payload (%s) — "
                    "classification kept, no retry", '; '.join(errors[:2]))
        return _without_planning(first)

    # The body parsed and the model DID try to plan, but got it wrong. That is
    # a one-off slip worth one retry — never at the cost of the classification,
    # which is what every downstream handler actually depends on today. If the
    # retry is no better we return the ORIGINAL classification with the
    # planning keys stripped, so a planning regression degrades the planner and
    # nothing else.
    logger.info("unified_turn: planning invalid (%s), retrying once",
                '; '.join(errors[:3]))
    retry = _call_once(messages)
    if retry is not None and not validate_turn(retry):
        return retry
    return _without_planning(first)


def _call_once(messages) -> dict | None:
    """One DeepSeek round-trip. Returns the parsed object, or None."""
    raw = None
    try:
        from bot.services.clients import deepseek_call
        raw = deepseek_call(
            messages=messages,
            temperature=0.0,
            # Classification (500) plus the planning half. `reasoning` is
            # capped at 40 words in the prompt, but the cap is advisory and a
            # body truncated mid-JSON is unparseable — the exact failure that
            # broke every classifier when thinking mode was left on (see
            # bot/services/clients.py). Headroom is cheaper than a retry.
            max_tokens=800,
            json_response=True,
        )
        return json.loads(raw)
    except Exception as exc:
        # Log the raw body on failure so a malformed/truncated JSON is visible
        # (distinguishes "DeepSeek returned junk" from "DeepSeek returned nothing").
        logger.warning(
            "unified_turn call failed: %s | raw=%r",
            exc, (raw[:400] if raw else raw),
        )
        return None


def _without_planning(result: dict) -> dict:
    """The classification half alone, with the planning keys removed.

    Returning a half-valid plan would be worse than returning none: the
    accessors would read it as a real decision, and shadow mode would score a
    malformed move as a disagreement rather than as a planning failure.
    """
    stripped = {k: v for k, v in (result or {}).items()
                if k not in ('reasoning', 'next_move', 'move_confidence',
                             'comprehension', 'response')}
    state = stripped.get('state_update')
    if isinstance(state, dict):
        stripped.pop('state_update', None)
    return stripped


# The controller IS this call (spec §1) — same round-trip, now carrying the
# plan. The old name stays as an alias because ~15 call sites import it and a
# rename is not what Phase 0 is for.
unified_classify = unified_turn


# ── Accessor helpers (safe — return sensible defaults when result is None) ────

def uc_intent(r: dict | None) -> str:
    return (r or {}).get("intent", "in_scope")

def uc_confidence(r: dict | None) -> str:
    return (r or {}).get("confidence", "HIGH")

def uc_service_type(r: dict | None) -> str | None:
    return (r or {}).get("service_type") or None

def uc_product_intent(r: dict | None) -> str:
    return (r or {}).get("product_intent") or "none"

def uc_is_photo_request(r: dict | None) -> bool:
    return bool((r or {}).get("is_photo_request", False))

def uc_is_plan_later(r: dict | None) -> bool:
    return bool((r or {}).get("is_plan_later", False))

def uc_is_repeat(r: dict | None) -> bool:
    return bool((r or {}).get("is_repeat_question", False))

def uc_english(r: dict | None) -> str:
    """The literal English rendering of the customer's message ('' when it was
    already English or the call failed). RULE ENGINE ONLY — never put this in
    front of a customer or into a reply prompt; the bot answers in the language
    the lead used. See bot/message_normalizer.py."""
    value = (r or {}).get("english")
    return value.strip() if isinstance(value, str) else ""


def uc_extracted(r: dict | None) -> dict:
    return (r or {}).get("extracted") or {}

# ── Qualification signals (Phase 1: date-stage dispatch) ─────────────────────

def uc_answered_current_question(r: dict | None) -> bool:
    return bool((r or {}).get("answered_current_question", False))

def uc_pivoted_to_timeline(r: dict | None) -> bool:
    return bool((r or {}).get("pivoted_to_timeline", False))

def uc_offered_date(r: dict | None) -> str | None:
    v = (r or {}).get("offered_date")
    return v or None

def uc_offered_timeframe(r: dict | None) -> str | None:
    v = (r or {}).get("offered_timeframe")
    return v or None


def uc_speech_act(r: dict | None) -> str | None:
    """What KIND of message this is — see the speech_act block in the prompt.

    Returns None when the classifier did not run or gave nothing usable, which
    is the signal for the caller to fall back to its keyword resolver. Never
    guesses a default: "no answer" and "other" must stay distinguishable, or a
    failed call would silently read as a definite classification.
    """
    v = (r or {}).get("speech_act")
    if not isinstance(v, str):
        return None
    v = v.strip().lower()
    return v or None


# Nouns we are willing to say back in "a new ___". The model is asked for the
# customer's own word, and this keeps a surprising one out of customer-facing
# copy — an unlisted noun still COUNTS as a new build, it just gets confirmed
# as "a new house" rather than pasting whatever came back into the reply.
_NEW_BUILD_NOUNS = {
    'house', 'home', 'building', 'property', 'structure',
    'place', 'flat', 'cottage', 'stand', 'build',
}


def uc_new_build(r: dict | None) -> str | None:
    """The customer's own noun for a structure that is new or still going up
    ('house', 'building', ...), or None when they named none.

    None also means "the classifier did not run" — callers treat that as no
    answer and fall back to their keyword resolver, never as a definite "no".
    """
    v = (r or {}).get("new_build")
    if not isinstance(v, str):
        return None
    v = v.strip().lower().strip('.,!?"\'')
    if not v:
        return None
    # Tolerate "a new house" / "4 bedroom house" coming back instead of a bare
    # noun: take the listed noun out of it rather than discarding the signal.
    for word in reversed(v.split()):
        if word in _NEW_BUILD_NOUNS:
            return word
    return 'house'

def uc_as_service_inquiry(r: dict | None) -> dict:
    """Format the result as the dict that detect_service_inquiry() would return."""
    return {
        "intent":     uc_product_intent(r),
        "confidence": uc_confidence(r),
    }

def uc_as_oos_classification(r: dict | None) -> dict:
    """Format the result as the dict that classify_message() would return."""
    intent = uc_intent(r)
    # Map unified intent names to OOS handler category names
    cat_map = {
        "in_scope":     "in_scope",
        "out_of_scope": "out_of_scope",
        "delay_signal": "delay_signal",
        "complaint":    "complaint",
        "ack":          "in_scope",   # acks should fall through normally
    }
    return {
        "category":   cat_map.get(intent, "in_scope"),
        "confidence": uc_confidence(r),
        "detail":     "",
    }


# ── Planning accessors (spec §1) ─────────────────────────────────────────────
# The planning half lives in bot/controller.py, which is pure and offline. These
# are thin re-exports so a caller that already imports from this module has one
# import surface for the whole turn, classification and plan alike.

def uc_next_move(r: dict | None) -> str | None:
    """The move the model chose, or None when it gave none we recognise."""
    from bot.controller import plan_next_move
    return plan_next_move(r)


def uc_plan_confidence(r: dict | None) -> float:
    """0.0-1.0 confidence in next_move; 0.0 when absent (= do not act on it)."""
    from bot.controller import plan_confidence
    return plan_confidence(r)


def uc_want_level(r: dict | None) -> str | None:
    """'cold' | 'interested' | 'wants_it' — the fee gate. None when unstated."""
    from bot.controller import plan_want_level
    return plan_want_level(r)


def uc_reasoning(r: dict | None) -> str:
    """The model's internal plan. Logs and diagnostics ONLY — never customer copy."""
    from bot.controller import plan_reasoning
    return plan_reasoning(r)


def uc_sentiment(r: dict | None) -> str | None:
    from bot.controller import plan_sentiment
    return plan_sentiment(r)
