"""
bot/availability_ask.py
=======================
DeepSeek writes the availability ask when the diary gives us an awkward shape.

The normal case has an approved script and keeps it (script-first: the FIRST
ask of every early-flow question is hardcoded, because consistency on first
contact converts). This module exists for the shapes that have no script, and
which `_get_first_pass_question` used to paper over with two literals:

    slot_a = slots[0] if len(slots) > 0 else "tomorrow"
    slot_b = slots[1] if len(slots) > 1 else "the day after"

One free slot therefore went out as "tomorrow at 9am or the day after" - a real
slot ORed with a vague phrase carrying no time, so picking the second option
costs the extra turn the day+time ask exists to avoid. And with nothing
resolvable it went out as "tomorrow or the day after", two invented days that
ignore whether the tenant is even open then - the exact fault `visit_slots`
was written to prevent ("the day after or Monday" asks the lead to work out the
day after what). Two slots on one day read "tomorrow at 9am or tomorrow at
2pm", repeating the day no person would repeat.

WHAT THE MODEL MAY AND MAY NOT DO
---------------------------------
The house architecture is "the model picks the move; deterministic code writes
anything the customer must be able to trust". A day and a time we offer is the
most trusted thing in the conversation - a slot we do not have is a plumber who
does not arrive - so the model NEVER chooses one. Code resolves the slots
(`_get_two_visit_slots`, already rolled past the days this tenant is shut and
checked against the real diary) and hands them over as fixed strings. The model
only writes the sentence around them.

Then a deterministic fence reads what came back, and REJECTS it unless:
  * every clock time in it is one we supplied
  * every day word in it is one we supplied
  * with NO slots to offer, it names no day and no time at all - it must ask
    openly, which is the honest thing to say when we have nothing free
  * it carries no figure and no promise word (free / discount / guarantee)
  * it asks exactly one question
  * it is not longer than a WhatsApp message anybody reads

A rejected composition is logged and the deterministic sentence goes out
instead. FAILS OPEN, ALWAYS: a timeout, bad JSON or a refusal returns None and
the caller uses its own copy. The deterministic sentence is therefore still
correct for every shape on its own - this module makes it read better, it is
never what makes it safe.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# ON by default (owner request, 2026-09-17: "I want deepseek api to be able to
# handle these situations"). PLUMBOT_ASK_COMPOSER=0 turns it off without a
# deploy and every caller falls back to its deterministic sentence.
ASK_COMPOSER_ENABLED = os.environ.get('PLUMBOT_ASK_COMPOSER', '1') != '0'

# A WhatsApp question, not a paragraph. The scripted two-slot ask is ~140
# characters, so this leaves room for a longer day name without leaving room
# for an essay.
MAX_CHARS = 220

# The fence vocabulary (money, promise words, clock times, day words) lives
# in bot/copy_fence.py, shared with the reply checker and the follow-up
# rewrite. It used to be copied here and in response_check.py, and the two
# copies had drifted (the rand-prefix bug was fixed in both by hand).

_SYSTEM = """You write ONE WhatsApp message for a plumbing company: the message that asks a customer when we may come and look at their job.

You are given the exact appointment options that are actually free, already written the way we say them. Use them EXACTLY as given, word for word.

Return ONLY JSON:
{"ask":"<the message>"}

Hard rules:
- Never invent, adjust or add a day or a time. Only the options given are real.
- Given ONE option, offer that one option and ask if it works. Do not pad it out with a second vague option like "or the day after".
- Given TWO options on the SAME day, say the day once: "tomorrow at 9am or 2pm".
- Given NO options, do not name any day or any time at all, and do not state our opening hours. Just ask them when would suit them. Our hours are added to your message afterwards by us.
- Exactly ONE question mark. Never two questions.
- No prices, no discounts, nothing free, no guarantees.
- No emojis. No dashes.
- Reply in the language you are told to use.

Voice: a busy tradesperson texting. Short plain words, warm, no wind-up, no sales patter. Lead with the offer and close on the question."""


def fences_hold(ask: str, slots) -> tuple:
    """May this composition go out? Returns (ok, why_not).

    Deterministic and public so TEST 0 can pin it: this is the part that makes
    an LLM safe on copy the customer has to be able to trust. The model was
    given only the slot strings, so any figure, promise word, day word or clock
    time not in them is invented, which covers "nothing free means nothing may
    be named" too: with no slots, every day and time is one we did not supply.
    Exactly one question, and a WhatsApp length.
    """
    from bot.copy_fence import fence_holds
    return fence_holds(ask, ' '.join(slots or ()), check_slots=True,
                       questions=1, max_chars=MAX_CHARS)


def compose_availability_ask(slots, *, purpose, is_shona=False,
                             job_noun=''):
    """The availability ask for an awkward diary shape, or None.

    `slots` are the real options as strings ('tomorrow at 9am'), resolved by
    `_visit_slot_labels` - an empty list means we have nothing free and the
    message must ask openly. `purpose` is what the visit is FOR, in the lead's
    own terms ('have a quick look at the bathroom space').

    Deliberately NOT given the opening hours, even though the no-slots message
    reads better with them: that sentence carries day names and clock times
    ("Sunday to Friday, 8am to 6pm"), so letting the model write it would mean
    allowing those words through the fence - and then a composition that
    offered "Sunday" on an empty diary would pass. The caller appends the
    tenant's hours deterministically instead.

    None on any failure, including a composition that breaks a fence. The
    caller then sends its own deterministic sentence.
    """
    if not ASK_COMPOSER_ENABLED:
        return None

    options = list(slots or [])
    if options:
        offer = ('The options that are free, to be used exactly as written: '
                 + '; '.join('"%s"' % o for o in options))
    else:
        offer = ('We have NOTHING free to offer. Name no day and no time. Ask '
                 'when would suit them.')

    lines = [
        offer,
        'The visit is to: %s' % (purpose or 'have a quick look at the job'),
        'Language: %s' % ('Shona' if is_shona else 'English'),
    ]
    if job_noun:
        lines.append('Their job, in their words: %s' % job_noun)

    try:
        from bot.services.clients import deepseek_call
        raw = deepseek_call(
            messages=[
                {'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': '\n'.join(lines)},
            ],
            temperature=0.3,
            max_tokens=150,
            json_response=True,
        )
        ask = str(json.loads(
            (raw or '').replace('```json', '').replace('```', '').strip()
        ).get('ask', '')).strip()
    except Exception as exc:
        logger.warning('Availability ask composer failed (%s) - using the '
                       'scripted sentence', exc)
        return None

    ok, why = fences_hold(ask, options)
    if not ok:
        logger.warning('Availability ask REJECTED (%s): %r', why, ask)
        return None

    logger.info('Availability ask composed for %d option(s): %r',
                len(options), ask)
    return ask
