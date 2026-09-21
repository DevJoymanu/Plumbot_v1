"""
The fence every model-written customer sentence passes before it is sent.

THE RULE (house split): code resolves anything the customer must be able to
trust (a price, a day, a time, whether something is free) and hands it to the
model as FIXED TEXT. The model may reword; it may not add. So this asks one
question of a composition: does it contain a figure, a promise word, a day or a
clock time that was NOT in the text the model was given? If so it is rejected,
and the caller sends its deterministic sentence instead, which must be correct
on its own. Every failure fails CLOSED to that sentence, never open to the
model's.

WHY ONE MODULE: the vocabulary below existed twice, in `availability_ask.py`
and `response_check.py`, and had already drifted. The money regex had to be
fixed "in BOTH" places (the rand prefix `R` matched the r ending "or 2pm", so
a sound question read as naming a price), and the promise words were matched
two different ways: whole words in one (so "discounted" and "guarantees" got
past) and bare substrings in the other (so "freestanding" read as the promise
"free", which the notes say it never is). One definition, one matcher.

USERS
  * `availability_ask.fences_hold` - the composed availability ask.
  * `response_check._fences_hold` - the reader's refinement of a draft.
  * `send_followups.Command._ai_message` - the model's rewrite of the owner's
    follow-up script (sampled drift about 1 in 5, and until this it was held to
    the script's question count and nothing else).
A new path where a model writes customer copy calls `fence_holds` too.
"""
import re

# Words that promise something about cost or outcome. A leading word boundary
# and NO trailing one, so the inflected forms are caught ("discounted",
# "guaranteed", "refunds", "waived") while a word merely containing the letters
# is not ("carefree"). Matched by `_promise_words`.
PROMISE_WORDS = (
    'free', 'no charge', 'no cost', 'discount', 'guarantee', 'refund',
    'mahara', 'complimentary', 'waive',
)
# ...except the tub. A freestanding / free-standing tub is a product, not a
# price claim, and it is named constantly on the pricing path.
_NOT_A_PROMISE = re.compile(r'free[\s-]?standing', re.IGNORECASE)

# The amount only. A trailing comma or full stop is punctuation, not part of
# the figure: absorbing it made "US$10," and "US$10." read as two different
# amounts. The bare R is the rand prefix and must not match the r ending an
# ordinary word ("or 2pm", "for 20 minutes").
MONEY = re.compile(
    r'(?:US\$|USD|\$|(?<![A-Za-z])R)\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?',
    re.IGNORECASE)

# "9am", "2:30pm": the shape _clock_label writes, which is how a person types a
# time into WhatsApp.
CLOCK = re.compile(r'\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b', re.IGNORECASE)

# Every word that could name a day, in both languages plus the relative ones.
# The fence asks "is this day word one we supplied?", so the list only has to
# be wide enough to CATCH a day the model invented, never to interpret it.
DAY_WORDS = (
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
    'sunday', 'today', 'tomorrow', 'tonight', 'weekend',
    'muvhuro', 'chipiri', 'chitatu', 'china', 'chishanu', 'mugovera',
    'svondo', 'mangwana', 'nhasi', 'manheru', 'mangwanani', 'masikati',
)

# Our close is a soft value yes; the budget is only ever asked after a "no",
# by deterministic copy. "What is your budget?" went out in place of the
# tie-down (prod, 2026-09-19) and read as a demand.
_BUDGET_ASK = re.compile(r"\bwhat(?:'s|\s+is)\s+(?:your|the)\s+budget\b"
                         r"|\byour\s+budget\s*\?", re.IGNORECASE)


def figures(text):
    """The money amounts in `text`, normalised so rewording does not change them."""
    return {m.group(0).upper().replace(' ', '') for m in MONEY.finditer(text or '')}


def promise_words(text):
    """The promise words in `text` (as listed in PROMISE_WORDS)."""
    low = _NOT_A_PROMISE.sub(' ', (text or '').lower())
    return {w for w in PROMISE_WORDS if re.search(r'(?<![a-z])' + re.escape(w), low)}


def clock_times(text):
    return {m.group(0).lower().replace(' ', '') for m in CLOCK.finditer(text or '')}


def day_words(text):
    low = (text or '').lower()
    return {w for w in DAY_WORDS if re.search(r'\b%s\b' % w, low)}


def fence_holds(candidate, source='', *, check_slots=True, questions=None,
                max_chars=None, max_growth=None, min_chars=None):
    """May this model-written text go to a customer? Returns (ok, why_not).

    `source` is everything the model was GIVEN to say: the draft, the script,
    the slot strings, joined. A figure, promise word, day word or clock time in
    `candidate` that is not in `source` is an invention and fails.

      check_slots  also fence day words and clock times. Off for the reply
                   reader, which works from the whole conversation and may
                   rightly move a day the customer named; on everywhere a
                   slot comes from code.
      questions    the exact number of '?' allowed (None = any).
      max_chars    an absolute ceiling.
      max_growth   a ceiling relative to `source` (1.6 = 60% longer), floored
                   at 120 characters so a very short source leaves room to
                   write a sentence.
      min_chars    a floor, so a bare "Hi" never replaces a real message.

    Deterministic and public so TEST 0 can pin it. It never raises.
    """
    if not candidate or not candidate.strip():
        return False, 'empty'
    text = candidate.strip()

    if min_chars is not None and len(text) < min_chars:
        return False, 'too short (%d chars)' % len(text)
    if max_chars is not None and len(text) > max_chars:
        return False, 'too long (%d chars)' % len(text)
    if max_growth is not None and len(text) > max(120, int(len(source or '') * max_growth)):
        return False, 'too long for what it was given (%d chars)' % len(text)
    if questions is not None and text.count('?') != questions:
        return False, 'wants exactly %d question(s), found %d' % (questions, text.count('?'))

    added = figures(text) - figures(source)
    if added:
        return False, 'invented a figure: %s' % ', '.join(sorted(added))
    added = promise_words(text) - promise_words(source)
    if added:
        return False, 'added a promise: %s' % ', '.join(sorted(added))
    if _BUDGET_ASK.search(text) and not _BUDGET_ASK.search(source or ''):
        return False, 'asked their budget outright'

    if check_slots:
        added = clock_times(text) - clock_times(source)
        if added:
            return False, 'invented a time: %s' % ', '.join(sorted(added))
        added = day_words(text) - day_words(source)
        if added:
            return False, 'invented a day: %s' % ', '.join(sorted(added))

    return True, ''
