"""
bot/response_check.py
=====================
DeepSeek reads the reply we are about to send, in context, and corrects it.

The controller already lets the model choose the MOVE. This is the other half:
once a move has produced a reply — usually from a deterministic template — the
model checks that the words actually answer the person in front of them, and
rewrites them when they do not.

It exists because of a real conversation (barmak lead 1005, 2026-09-06). The
lead said "Currently im out of the country" and, four minutes later, "My flight
back to Zim..is on the 22nd of Dec". The very next thing the bot said was:

    What works better for you, tomorrow or this Tuesday?

Every gate passed. The template was the right template for the branch, the copy
was correct, no rule was broken. It was simply, obviously wrong to anybody who
had read the conversation. Deterministic code cannot catch that class of fault,
because the fault is not in any rule — it is in the fit between the reply and
what was just said. A reader can catch it, and now one does.

WHAT IT MAY AND MAY NOT CHANGE
------------------------------
The house architecture is "the model picks the move; deterministic code writes
anything the customer must be able to trust". Letting a model rewrite outbound
copy runs straight at that, so the refinement is fenced:

  * It may fix what the reply SAYS to the lead: a question already answered, a
    slot offered to somebody who is away, an answer to a question they did not
    ask, an acknowledgement of something they never said.
  * It may NOT introduce a figure. Any currency amount in the refined text must
    already appear in the draft — prices come from the tenant's own tables and
    a model that invents one is quoting a business it does not work for.
  * It may NOT add a promise. "free", "no charge", "discount", "guarantee" and
    their Shona equivalents cannot appear unless the draft had them.
  * It may NOT change language. A Shona reply stays Shona.
  * It may NOT lengthen the message much: this is a correction, not a rewrite.

Anything it returns then goes back through the SAME strippers the draft passed
(fee, repeat-free-visit, emoji, dash, one-question), because a refinement is
just another draft and gets no exemption.

FAILS OPEN, ALWAYS. A timeout, a malformed body, a refusal, a refinement that
breaks one of the fences above: the original draft is sent unchanged. A checker
that can stop a reply going out is worse than no checker.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# ON by owner decision (2026-09-07): the model should read what we are about to
# say and correct it. PLUMBOT_REPLY_CHECK=0 turns it off without a deploy.
REPLY_CHECK_ENABLED = os.environ.get('PLUMBOT_REPLY_CHECK', '1') != '0'

# How much of the conversation the checker sees. Enough to catch "they told us
# that two turns ago", short enough to keep the prompt cheap.
HISTORY_TURNS = 8

# A refinement is a correction. Past this multiple of the draft it is a
# rewrite, and a rewrite is not what was asked for.
MAX_GROWTH = 1.6

# Words that promise something. None may APPEAR in a refinement unless the
# draft already carried it.
_PROMISE_WORDS = (
    'free', 'no charge', 'no cost', 'discount', 'guarantee', 'guaranteed',
    'refund', 'mahara', 'complimentary', 'waive', 'waived',
)

# The amount only. A trailing comma or full stop is punctuation, not part
# of the figure: absorbing it made "US$10," and "US$10." read as two
# different amounts, so re-punctuating a sentence looked like inventing one.
_MONEY = re.compile(
    r'(?:US\$|USD|\$|R)\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?', re.IGNORECASE)

_SYSTEM = """You check a plumbing company's outgoing WhatsApp reply before it is sent.

You are given the recent conversation, what the company already knows about
this customer, and the DRAFT reply. Decide whether the draft actually answers
the person in front of you.

Return ONLY JSON:
{"verdict":"ok"|"refine","reason":"<12 words max>","reply":"<corrected reply, or empty when ok>"}

Say "refine" ONLY when the draft is wrong in context. The clearest cases:
- it asks something the customer has already answered
- it offers a time to somebody who has said they are away or busy then
- it answers a question the customer did not ask, or ignores the one they did
- it acknowledges something the customer never said
- it contradicts a fact in the conversation

Say "ok" for everything else. Wording you would merely have phrased differently
is "ok". Being terse is "ok". This is a safety net, not an editor.

When you refine, you MUST:
- keep every figure exactly as it is, and add none
- add no offer, discount, guarantee or anything free that is not already there
- reply in the SAME language as the draft (English or Shona)
- keep it about the same length, or shorter
- no emojis, no dash punctuation
- keep one question at most

Write plainly, like a busy tradesperson texting. Short words. No wind-up."""


def _facts(appointment) -> str:
    """The one-line state the checker judges the draft against."""
    def v(name):
        return str(getattr(appointment, name, '') or '').strip()

    bits = []
    for label, val in (
        ('job', v('project_description')),
        ('area', v('customer_area')),
        ('timeline', v('timeline')),
        ('name', v('customer_name')),
        ('email', v('customer_email')),
    ):
        if val:
            bits.append(f'{label}={val[:60]}')
    if str(getattr(appointment, 'plan_status', '') or '') == 'plan_uploaded':
        bits.append('they have sent a plan')
    slot = getattr(appointment, 'scheduled_datetime', None)
    if slot and getattr(appointment, 'status', '') == 'confirmed':
        bits.append('BOOKED for ' + slot.strftime('%a %d %b %H:%M'))
    return '; '.join(bits) or 'nothing on file yet'


def _transcript(appointment, limit=HISTORY_TURNS) -> str:
    rows = [e for e in (getattr(appointment, 'conversation_history', None) or [])
            if isinstance(e, dict) and e.get('content')]
    out = []
    for e in rows[-limit:]:
        who = 'CUSTOMER' if e.get('role') == 'user' else 'US'
        out.append(f"{who}: {str(e.get('content'))[:300]}")
    return '\n'.join(out)


def _fences_hold(draft: str, refined: str) -> tuple:
    """Is this refinement allowed to go out? Returns (ok, why_not)."""
    if not refined or not refined.strip():
        return False, 'empty'
    if len(refined) > max(120, int(len(draft) * MAX_GROWTH)):
        return False, 'too long'

    # No figure may appear that the draft did not already carry. Compared as a
    # set of normalised amounts, so reordering or re-wording is fine and a NEW
    # number is not.
    def money(text):
        return {m.group(0).upper().replace(' ', '') for m in _MONEY.finditer(text)}
    added = money(refined) - money(draft)
    if added:
        return False, 'invented a figure: %s' % ', '.join(sorted(added))

    low_d, low_r = draft.lower(), refined.lower()
    for word in _PROMISE_WORDS:
        if word in low_r and word not in low_d:
            return False, 'added a promise: %s' % word
    return True, ''


def verify_and_refine(reply: str, appointment, message_body=None):
    """Check the draft in context. Returns (text_to_send, note_or_None).

    `note` is the internal flag: a short line for the lead's admin notes when
    the reply was corrected, so the change is visible to a human afterwards
    rather than happening silently.
    """
    if not REPLY_CHECK_ENABLED or not (reply or '').strip():
        return reply, None
    if not appointment:
        return reply, None

    try:
        from bot.services.clients import deepseek_call

        payload = (
            f"WHAT WE KNOW: {_facts(appointment)}\n\n"
            f"CONVERSATION SO FAR:\n{_transcript(appointment)}\n\n"
            f"CUSTOMER JUST SAID: {(message_body or '(nothing new)')[:400]}\n\n"
            f"DRAFT REPLY:\n{reply}"
        )
        raw = deepseek_call(
            [{'role': 'system', 'content': _SYSTEM},
             {'role': 'user', 'content': payload}],
            temperature=0.2,
            max_tokens=400,
            json_response=True,
            retries=1,
            timeout=12,
        )
        if not raw:
            return reply, None
        data = json.loads(raw)
    except Exception:
        # Fails open: the draft goes as it is. See the module docstring.
        logger.warning('Reply check failed — sending the draft unchanged',
                       exc_info=True)
        return reply, None

    verdict = str(data.get('verdict') or '').strip().lower()
    if verdict != 'refine':
        return reply, None

    refined = str(data.get('reply') or '').strip()
    reason = str(data.get('reason') or '').strip()[:80]

    ok, why_not = _fences_hold(reply, refined)
    if not ok:
        # The checker wanted a change we will not make. That is worth knowing
        # about — it is either a bad refinement or a rule that needs looking
        # at — so it is logged, and the customer still gets the draft.
        logger.info('Reply check REJECTED its own refinement (%s): %s',
                    why_not, reason)
        return reply, None

    logger.info('Reply check refined the draft: %s', reason)
    return refined, f'Reply corrected before sending ({reason or "context"}).'
