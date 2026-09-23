"""
bot/photo_ask.py
================
The photo or plan ask that rides in front of the area question.

WHAT (owner, 2026-09-23, decisions 7, 7a, 7b): once the lead has said what
needs doing, the reply that asks their area first asks for a picture of what
is there, named from their OWN words:

    replacing or fixing  "If you have one, send us a picture of the old tub so
                          we can see what's there. What area are you in?"
    adding something     "...a picture of the spot in the bathroom where it's
                          going. What area are you in?"
    a new build          "If you have a plan or drawings, send them through, or
                          a picture of the site. What area are you in?"
    can't tell           "If you have a picture of what's there now, or a plan,
                          send it through. What area are you in?"

WHY: a picture of what is there proves we looked, makes the visit better, and
a plan puts them on the plan path (a quote with no visit). It is a REQUEST,
not a question, so the area stays the one question in the message (the house
one-question rule, decision 7a), and the area is the answerable ask, so a lead
with no picture just answers it and carries on. Asked ONCE, never chased
(decision 8B): chasing a photo reads as neediness.

HOW: `add_photo_ask(reply, appointment, message_body)` runs in
`whatsapp_webhook.finalise_outbound`, the choke point every reply passes, so
every path that asks the area gets it (there are several: the scripted first
ask, the "All good, what area are you in?" after a description, the retry
paraphrase). It changes nothing unless ALL of these hold: the reply ENDS on the
area question; the lead has told us the job; they have sent no photo, file or
plan yet and have not said a plan is coming; we have never asked before (read
from the transcript, so no state is written); the lead is not writing in Shona
(the Shona wording waits for the owner); the lead is not booked. Deterministic:
the kind of job is read from their description with keyword lists, English
only. Pinned by the "photo ask" cases in TEST 0 and PhotoAskTests.
"""

from __future__ import annotations

import re

from . import copy_catalog

# The reply's closing area question, with whatever ack clause precedes it on
# the same sentence ("All good, what area are you in?").
_AREA_Q_RE = re.compile(
    r"(?P<pre>^|.*?[,.!]\s*|.*?\s)"
    r"(?P<q>(?:what area are you in|whereabouts are you(?: based)?|which area are you in"
    r"|what suburb are you in|where are you based|whereabouts is the site"
    r"|where is the site)\??)\s*$",
    re.IGNORECASE | re.DOTALL)

_NEW_BUILD_RE = re.compile(
    r"\b(?:new build|new house|new home|building (?:a|our|my) (?:new )?(?:house|home)"
    r"|build(?:ing)? a house|construction|from scratch|foundation|slab"
    r"|nothing there yet|empty stand|new stand)\b", re.IGNORECASE)
_ADD_RE = re.compile(
    r"\b(?:add|adding|put in|putting in|fit a new|install a new|installing a new"
    r"|extra|another|second)\b", re.IGNORECASE)
_EXISTING_RE = re.compile(
    r"\b(?:replac\w*|old|broken|leak\w*|fix\w*|repair\w*|redo\w*|re-do|renovat\w*"
    r"|upgrad\w*|remodel\w*|refurb\w*|chang\w*|swap\w*|damaged|cracked|blocked"
    r"|not working|needs work|redone)\b", re.IGNORECASE)

_REPLACING_RE = re.compile(
    r"\b(?:replac\w*|old|redo\w*|re-do|renovat\w*|upgrad\w*|remodel\w*|refurb\w*"
    r"|chang\w*|swap\w*|redone)\b", re.IGNORECASE)

# Fixture words to the thing the picture should show, most specific first.
_FIXTURES = (
    (r"\b(?:bath ?tubs?|tubs?|baths?)\b", 'tub'),
    (r"\btoilets?\b", 'toilet'),
    (r"\bshower cubicles?\b", 'shower cubicle'),
    (r"\bshowers?\b", 'shower'),
    (r"\bgeysers?\b", 'geyser'),
    (r"\bvanit(?:y|ies)\b", 'vanity'),
    (r"\bsinks?\b", 'sink'),
    (r"\bbasins?\b", 'basin'),
    (r"\btaps?\b", 'tap'),
    (r"\bpipes?\b", 'pipes'),
)
_ROOMS = (('bathroom', 'bathroom'), ('en-?suite', 'ensuite'), ('kitchen', 'kitchen'),
          ('toilet room', 'toilet'))

# A photo, file or plan already in the conversation. The media path logs every
# customer file as "[Sent image] ..." / "[Sent document] ...".
_MEDIA_TURN_RE = re.compile(r"^\[Sent (?:image|photo|document|video|file)", re.IGNORECASE)

# Our own earlier ask, whichever variant it was. The distinctive openings of
# the four catalogue lines, so a rewording there updates this for free.
_ASKED_MARKERS = tuple(
    line.split(',')[0] for line in (
        copy_catalog.PHOTO_ASK_EXISTING, copy_catalog.PHOTO_ASK_NEW_SPOT,
        copy_catalog.PHOTO_ASK_NEW_BUILD, copy_catalog.PHOTO_ASK_UNCLEAR))


def _first_match(pairs, text):
    for pattern, value in pairs:
        if re.search(pattern, text, re.IGNORECASE):
            return value
    return None


def photo_line(description: str, project_type: str = '') -> str:
    """The request line for this job, from the lead's own description.

    New build first (a plan is what they have), then adding something (show
    the spot), then replacing or fixing (show the old one), else the either-or
    line. A fixture they named beats the room; neither falls back to the
    either-or line rather than guess.
    """
    text = ' '.join(str(description or '').split())
    kind = (project_type or '').lower()
    if _NEW_BUILD_RE.search(text) or 'new_build' in kind or 'new_construction' in kind:
        return copy_catalog.PHOTO_ASK_NEW_BUILD
    # "New installation(s)" can be a new room or new fixtures in an existing
    # one, so it gets the either-or line (a picture of what is there, or a
    # plan) rather than a guess. scenarios/new_install_flow.txt.
    if (re.search(r"\binstallations?\b", text, re.IGNORECASE)
            and not _EXISTING_RE.search(text) and not _ADD_RE.search(text)):
        return copy_catalog.PHOTO_ASK_UNCLEAR
    fixture = _first_match(_FIXTURES, text)
    room = _first_match(_ROOMS, text)
    if _ADD_RE.search(text) and not _EXISTING_RE.search(text):
        spot = f'the spot in the {room}' if room else 'the space'
        return copy_catalog.PHOTO_ASK_NEW_SPOT.format(spot=spot)
    # A leak is the thing to see, wherever it is ("leaking pipe under the
    # sink" asked for "the old basin" before this).
    if re.search(r"\bleak\w*", text, re.IGNORECASE):
        return copy_catalog.PHOTO_ASK_EXISTING.format(thing='the leak')
    if fixture:
        # "old" only when it is being replaced or redone: a blocked sink is
        # "the sink", a replaced tub is "the old tub".
        thing = 'the pipes' if fixture == 'pipes' else (
            f'the old {fixture}' if _REPLACING_RE.search(text) else f'the {fixture}')
        return copy_catalog.PHOTO_ASK_EXISTING.format(thing=thing)
    if room:
        return copy_catalog.PHOTO_ASK_EXISTING.format(thing=f'the {room}')
    return copy_catalog.PHOTO_ASK_UNCLEAR


def _already_has_media_or_plan(appointment) -> bool:
    if getattr(appointment, 'has_plan', None) is True:
        return True
    if (getattr(appointment, 'plan_status', '') or '') in (
            'plan_uploaded', 'plan_reviewed', 'ready_to_book', 'pending_upload'):
        return True
    for turn in getattr(appointment, 'conversation_history', None) or []:
        if turn.get('role') == 'user' and _MEDIA_TURN_RE.match(str(turn.get('content', ''))):
            return True
    return False


def _asked_before(appointment) -> bool:
    for turn in getattr(appointment, 'conversation_history', None) or []:
        if turn.get('role') == 'assistant':
            content = str(turn.get('content', ''))
            if any(marker in content for marker in _ASKED_MARKERS):
                return True
    return False


def add_photo_ask(reply: str, appointment, message_body: str = None):
    """(reply, added). Puts the photo/plan request before a closing area
    question when every condition in the module docstring holds."""
    from .views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER
    if not reply or appointment is None:
        return reply, False
    parts = reply.split(MESSAGE_SPLIT_MARKER)
    last = parts[-1]
    m = _AREA_Q_RE.match(last.strip())
    if not m:
        return reply, False
    description = str(getattr(appointment, 'project_description', '') or '').strip()
    if not description:
        return reply, False
    if getattr(appointment, 'status', '') == 'confirmed':
        return reply, False
    if _already_has_media_or_plan(appointment) or _asked_before(appointment):
        return reply, False
    # English only until the owner approves the Shona wording. The shared
    # detector calls most real Shona sentences "mixed" ("Ndiri kuda kuchinja
    # bhavhu rekare" is mixed, not shona), so anything but 'english' holds it.
    try:
        from .repeated_question_detector import detect_language_simple
        if detect_language_simple(message_body or '') != 'english':
            return reply, False
    except Exception:
        pass
    line = photo_line(description, getattr(appointment, 'project_type', '') or '')
    pre = m.group('pre').rstrip()
    question = m.group('q')
    question = question[:1].upper() + question[1:]
    if not question.endswith('?'):
        question += '?'
    if pre:
        # "All good, what area are you in?" -> "All good. <line> What area...?"
        pre = pre.rstrip(',;:')
        if not pre.endswith(('.', '!', '?')):
            pre += '.'
        new_last = f'{pre} {line} {question}'
    else:
        new_last = f'{line} {question}'
    parts[-1] = new_last
    return MESSAGE_SPLIT_MARKER.join(parts), True
