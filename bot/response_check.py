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
    ask, an acknowledgement of something they never said, an answer that
    ignores the message the customer actually highlighted.
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

# What a refinement may not add (a figure, a promise word, an outright
# budget ask) is decided by bot/copy_fence.py, shared with the availability
# ask and the follow-up rewrite. The vocabulary used to be copied here and in
# availability_ask.py and had drifted: this copy matched promise words as bare
# substrings, so "freestanding" read as the promise "free".

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
- the customer HIGHLIGHTED one of our earlier messages (marked in the
  transcript as [highlighting our earlier message: "..."]) and the draft
  answers something else. A short reply like "this one?" or "how much?"
  is about the message they highlighted, not the last thing either of
  you said.

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
    """The recent turns, each carrying whatever it was a reply TO.

    WhatsApp's reply-to (the "highlighted message") is the one piece of
    context a plain transcript loses: "this one, how much?" is meaningless
    on its own and perfectly clear against the photo it points at. The
    quote is already stored on the entry by add_conversation_message
    (quoted=...), so it is read back from there rather than threaded in
    through every caller - which also makes it right by construction when
    the debounce batches several rapid messages into one turn and only ONE
    of them carried a quote.
    """
    rows = [e for e in (getattr(appointment, 'conversation_history', None) or [])
            if isinstance(e, dict) and e.get('content')]
    out = []
    for e in rows[-limit:]:
        who = 'CUSTOMER' if e.get('role') == 'user' else 'US'
        quoted = str(e.get('quoted') or '').strip()
        if quoted:
            who += f' [highlighting our earlier message: "{quoted[:140]}"]'
        out.append(f"{who}: {str(e.get('content'))[:300]}")
    return '\n'.join(out)


def _fences_hold(draft: str, refined: str) -> tuple:
    """Is this refinement allowed to go out? Returns (ok, why_not).

    Held to the DRAFT: nothing may appear that the draft did not carry (a new
    figure, a new promise word, a new outright budget ask), and it may not grow
    past MAX_GROWTH. Days and times are NOT fenced here, unlike the other two
    users of bot/copy_fence: the reader works from the whole conversation and is
    there precisely to fix a day the draft got wrong ("tomorrow or this Tuesday?"
    to a lead away until December).
    """
    from bot.copy_fence import fence_holds
    ok, why_not = fence_holds(refined, draft, check_slots=False, max_growth=MAX_GROWTH)
    if not ok:
        return ok, why_not
    return _priced_reply_kept(draft, refined)


def _last_question(text: str) -> str:
    """The draft's closing question ("…willing to invest in for a new tub?"), or ''."""
    # Lines break sentences too: a price block's last bullet has no full stop,
    # so without the newline split the close read as part of the list.
    sentences = re.split(r'(?<=[.!?])\s+|\n+', (text or '').strip())
    return next((s.strip() for s in reversed(sentences) if s.strip().endswith('?')), '')


def _priced_reply_kept(draft: str, refined: str) -> tuple:
    """A priced draft may be corrected, never cut down. Returns (ok, why_not).

    WHY: the fence above stops a refinement ADDING a figure; nothing stopped it
    REMOVING one. Lead 1161 highlighted our photo of a built-in bath and a
    walk-in shower and asked "this one how much"; the draft priced both, the
    reader decided the lead meant the tub, kept the tub's two figures, dropped
    the shower, and replaced the owner's price close with "Want to book a
    visit?", a yes/no close the house rules forbid (2026-09-22).
    HOW: when the draft carries prices, every one of them must survive, and so
    must its closing question (the price close comes from
    _price_tiedown/_get_pricing_followup_prompt, never from the model).
    Unpriced drafts are untouched: correcting a question the lead already
    answered is still the reader's job. Pinned in TEST 0 ("reply check:").
    """
    from bot.copy_fence import figures
    priced = figures(draft)
    if not priced:
        return True, ''
    dropped = priced - figures(refined)
    if dropped:
        return False, 'dropped a price the draft gave: %s' % ', '.join(sorted(dropped))
    close = _last_question(draft)
    if close and close.lower() not in (refined or '').lower():
        return False, 'replaced the price close'
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
