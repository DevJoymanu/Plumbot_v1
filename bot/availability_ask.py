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
import re

logger = logging.getLogger(__name__)

# ON by default (owner request, 2026-09-17: "I want deepseek api to be able to
# handle these situations"). PLUMBOT_ASK_COMPOSER=0 turns it off without a
# deploy and every caller falls back to its deterministic sentence.
ASK_COMPOSER_ENABLED = os.environ.get('PLUMBOT_ASK_COMPOSER', '1') != '0'

# A WhatsApp question, not a paragraph. The scripted two-slot ask is ~140
# characters, so this leaves room for a longer day name without leaving room
# for an essay.
MAX_CHARS = 220

# Reused from the reply checker's rules - the same two things a model must
# never introduce into customer copy.
_PROMISE_WORDS = (
    'free', 'no charge', 'no cost', 'discount', 'guarantee', 'guaranteed',
    'refund', 'mahara', 'complimentary', 'waive', 'waived',
)
# The bare R is the rand prefix, and must not match the 'r' ending an
# ordinary word: "or 2pm" and "for 20 minutes" both matched R\s?\d under
# IGNORECASE, so a sound sentence read as naming a figure.
_MONEY = re.compile(
    r'(?:US\$|USD|\$|(?<![A-Za-z])R)\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?',
    re.IGNORECASE)

# "9am", "2:30pm". The same shape _clock_label writes, which is how a person
# types a time into WhatsApp.
_CLOCK = re.compile(r'\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b', re.IGNORECASE)

# Every word that could name a day, in both languages plus the relative ones.
# The fence asks "is this day word one we supplied?", so the list only has to
# be wide enough to CATCH a day the model invented, never to interpret it.
_DAY_WORDS = (
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
    'sunday', 'today', 'tomorrow', 'tonight', 'weekend',
    'muvhuro', 'chipiri', 'chitatu', 'china', 'chishanu', 'mugovera',
    'svondo', 'mangwana', 'nhasi', 'manheru', 'mangwanani', 'masikati',
)

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


def _slot_vocabulary(slots) -> tuple:
    """The day words and clock times we supplied. The fence's allow-list."""
    joined = ' '.join(slots).lower()
    days = {w for w in _DAY_WORDS if re.search(r'\b%s\b' % w, joined)}
    times = {m.group(0).lower().replace(' ', '') for m in _CLOCK.finditer(joined)}
    return days, times


def fences_hold(ask: str, slots) -> tuple:
    """May this composition go out? Returns (ok, why_not).

    Deterministic and public so TEST 0 can pin it: this is the part that makes
    an LLM safe on copy the customer has to be able to trust.
    """
    if not ask or not ask.strip():
        return False, 'empty'
    text = ask.strip()
    if len(text) > MAX_CHARS:
        return False, 'too long (%d chars)' % len(text)
    if text.count('?') != 1:
        return False, 'wants exactly one question, found %d' % text.count('?')

    if _MONEY.search(text):
        return False, 'named a figure'
    low = text.lower()
    for word in _PROMISE_WORDS:
        if re.search(r'\b%s\b' % re.escape(word), low):
            return False, 'added a promise: %s' % word

    allowed_days, allowed_times = _slot_vocabulary(slots)

    said_times = {m.group(0).lower().replace(' ', '')
                  for m in _CLOCK.finditer(low)}
    invented_times = said_times - allowed_times
    if invented_times:
        return False, 'invented a time: %s' % ', '.join(sorted(invented_times))

    said_days = {w for w in _DAY_WORDS if re.search(r'\b%s\b' % w, low)}
    invented_days = said_days - allowed_days
    if invented_days:
        return False, 'invented a day: %s' % ', '.join(sorted(invented_days))

    # Nothing free means nothing may be named. Checked explicitly as well as
    # by the allow-lists above, because an empty allow-list is exactly the
    # case where a bug would read as "everything is allowed".
    if not slots and (said_days or said_times):
        return False, 'named a slot when the diary had none'

    return True, ''


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
