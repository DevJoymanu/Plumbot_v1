# bot/management/commands/send_followups.py
#
# HIGH-CONVERTING FOLLOW-UP SYSTEM
#
# THE CADENCE (owner rule). Every lead gets FOUR follow-ups, placed in three
# bands measured from the moment the lead last messaged us:
#
#       0-24h  →  2 follow-ups
#      24-48h  →  1 follow-up
#      48-72h  →  1 follow-up
#
# FOLLOWUP_BAND_OFFSETS holds that shape as absolute hours-from-window-open, one
# tuple per lead temperature: hotter leads are chased sooner INSIDE each band,
# never moved into a different one. Because the offsets are absolute, a touch
# delayed by the nightly contact-window pause never pushes the later ones out,
# they self-correct.
#
# THE RESET. A reply from the lead puts the counter back to zero
# (Appointment.reset_followup_sequence, called from mark_customer_response), so
# the four touches are four touches SINCE THEY LAST SPOKE, not four in the
# lifetime of the lead. Someone who answers every time is never retired; someone
# who goes quiet gets four and stops.
#
# THE WINDOW IS THE HARD DEADLINE. Once the WhatsApp free-form window shuts, a
# send bounces with 131047 and we don't pay for templates, so a touch scheduled
# past the close is a touch the lead never gets:
#
#   CTWA ad lead  → 72h window (from the ad click), the bands land literally
#   Standard lead → 24h window (from the lead's last message)
#
# **FOUR TOUCHES FOR EVERY LEAD. The BANDS are what needs the 72h window.**
# A 24h lead cannot be reached at 33h or 60h at all - free-form sending is dead
# once their window shuts and we don't pay for templates - so the three-day band
# placement is simply not theirs to use. They still get four touches; they get
# them spread across the window they actually have (SHORT_WINDOW_FRACTIONS,
# positions as a share of the usable span, so a half-spent ad window is handled
# by the same rule).
#
#   window carries the full cadence  -> FOLLOWUP_BAND_OFFSETS, at their literal
#                                       hours (2 / 1 / 1 across three days)
#   anything shorter                 -> four touches spread over what is left
#
# Either way it is four, and max_followups_for is len(followup_offsets_for(lead))
# so the cron's retirement, the UI chip, the dashboard due-list and the LLM
# prompt read the same number off the same schedule.

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import datetime, timedelta
from bot.models import Appointment, LeadStatus
from bot.whatsapp_window import paid_sends_allowed
from bot.utils import business_name_for
from bot.whatsapp_cloud_api import get_client_for_tenant, whatsapp_api
from bot.views.plumbot.response_mixin import dequalify_free_visit
import os
import re
import logging
import pytz
from urllib.parse import unquote

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')

SA_TIMEZONE = pytz.timezone('Africa/Johannesburg')

# ─── Contact windows (local time, half-open) ─────────────────────────────────
# Each entry is (open_hour, open_minute, close_hour, close_minute) in CAT.
# ONE window a day, 08:03-20:33 (owner rule, 2026-09-21, replacing the two
# blocks 12:33-14:33 and 16:02-19:33). Half-open, so the last possible send is
# 20:32; the off-minute edges keep sends off obvious bot times.
#
# Why one long window: the 4h floor (FOLLOWUP_MIN_GAP_HOURS) allowed at most
# two sends a day inside the old ~5.5 sendable hours, so a standard 24h lead
# got 1 to 3 touches instead of four. Twelve and a half hours fits three a day.
# Everything below that walks the windows still takes a LIST, so a second
# block can come back without touching the helpers.
#
# This is the ONE definition. Anything else that needs to know when we may
# message a lead reads it from here — _next_window_open, _window_moment_before
# and the plan-path follow-up all do — because two copies of a sending window
# drift within a month and the second one is always the one nobody updates.
CONTACT_WINDOWS = [
    (8, 3, 20, 33),
]

# ─── How many follow-ups ──────────────────────────────────────────────────────
# Four touches per run, for every lead. What a lead's window changes is where
# those four SIT (see followup_offsets_for), never how many there are. The delay
# and parked nudge loops, which have their own fraction lists, use it as their
# target count too.
FOLLOWUP_MIN_COUNT = 4

# The CEILING on what a lead may receive between one message of theirs and the
# next: four, counted across every loop that messages them (owner rule,
# 2026-09-09). Each loop already caps its own run at four and the loops are
# mutually exclusive per inbound, but "four from the lead" is a fact about the
# LEAD, not about whichever code path happens to own them this week - a lead
# chased four times and then parked could be nudged four more times off the same
# silence.
FOLLOWUP_CAP_PER_REPLY = 4

# What a proactive touch looks like in the transcript. Every loop stamps its own
# prefix as it sends, so the transcript is the one place that knows the total
# regardless of which counter each loop keeps.
# '[JOB DATE FOLLOW-UP]' is job_date_ladder.TRANSCRIPT_MARKER, the -7/-3 touches.
PROACTIVE_MARKERS = (
    '[AUTO FOLLOW-UP]', '[AUTOMATIC FOLLOW-UP]',
    '[DELAY NUDGE', '[PARKED NUDGE', '[DELAY REACTIVATION]',
    '[JOB DATE FOLLOW-UP]',
)


def touches_since_last_reply(lead) -> int:
    """Proactive messages sent since the lead last said anything.

    THE single reader for the cap, so no loop can answer it differently. Counted
    off the transcript rather than a column because each loop keeps its own
    counter (followup_count, and the two nudge states in internal_notes) and
    none of them can see the others; the transcript sees all three.

    A manual takeover by a human is deliberately NOT counted. The cap exists to
    stop the machine talking over itself, and a person who has read the thread
    and decided to write is the opposite of that.
    """
    history = getattr(lead, 'conversation_history', None) or []
    since = getattr(lead, 'last_customer_response', None) or getattr(
        lead, 'last_inbound_at', None)

    count = 0
    for message in history:
        if (message or {}).get('role') != 'assistant':
            continue
        content = (message.get('content') or '').lstrip()
        if not content.startswith(PROACTIVE_MARKERS):
            continue
        # No timestamp is treated as "before their reply": an entry we cannot
        # place must not be allowed to spend the lead's allowance.
        stamp = _parse_history_stamp(message.get('timestamp'))
        if since is not None and (stamp is None or stamp <= since):
            continue
        count += 1
    return count


def _greet(hi, body):
    """'Hi there, happy to hold…': the greeting and a nudge body joined as one
    sentence. The bodies are written capitalised, so joining them after a
    comma sent "Hi there, Happy to hold the quote" to every delayed lead. "I"
    keeps its capital."""
    body = (body or '').strip()
    if body[:1].isupper() and not re.match(r"I\b", body):
        body = body[0].lower() + body[1:]
    return f'{hi}, {body}'


def handoff_sent_since_last_reply(lead) -> bool:
    """Has a PROACTIVE touch carried the plumber's link since the lead last spoke?

    WHAT: True once the second follow-up (the plumber handoff) has gone out in
    the current silence, from any of the three loops.
    WHY (owner rule, 2026-09-21): the handoff is the LAST automatic touch. After
    it, every loop stops until the lead writes again; their reply resets the
    count and the cycle (contextual touch, then handoff) starts over.
    HOW: the same transcript read as touches_since_last_reply: only entries
    that start with a PROACTIVE_MARKERS prefix and are stamped after the reply.
    A link the bot put in a conversational reply (the delay flow's portfolio
    answer) is not proactive, so it does not stop anything. What is looked for
    is `plumber_link.is_handoff_text`: the handoff copy or any of its link
    forms, so no extra tag has to be kept in step.
    """
    history = getattr(lead, 'conversation_history', None) or []
    since = getattr(lead, 'last_customer_response', None) or getattr(
        lead, 'last_inbound_at', None)
    for message in history:
        if (message or {}).get('role') != 'assistant':
            continue
        content = (message.get('content') or '').lstrip()
        if not content.startswith(PROACTIVE_MARKERS):
            continue
        # Any link form the handoff has used, or its copy (plumber_link): the
        # business-domain short link carries no wa.me, and checking only for
        # wa.me would let a handed-off lead be chased again.
        from bot.plumber_link import is_handoff_text
        if not is_handoff_text(content):
            continue
        stamp = _parse_history_stamp(message.get('timestamp'))
        if since is not None and (stamp is None or stamp <= since):
            continue
        return True
    return False


def _parse_history_stamp(raw):
    """A conversation_history timestamp as an aware datetime, or None.

    The field is schemaless (CLAUDE.md: transcript metadata never gets a
    migration), so it holds whatever the writer put there across several years
    of writers. Anything unreadable is None and the caller decides what that
    means - here, that it does not count against the lead.
    """
    if raw in (None, ''):
        return None
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    if timezone.is_naive(parsed):
        try:
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        except Exception:
            return None
    return parsed


def followup_window_start(lead):
    """When this lead's messaging window opened — their last message to us,
    which is also what WhatsApp measures the free-form window from."""
    return (
        getattr(lead, 'last_customer_response', None)
        or getattr(lead, 'last_inbound_at', None)
        or getattr(lead, 'last_followup_sent', None)
        or getattr(lead, 'created_at', None)
    )


def is_ctwa_lead(lead) -> bool:
    """True for a lead that arrived by tapping a Facebook/Instagram
    click-to-WhatsApp ad.

    Those get a 72h FREE ENTRY POINT, which makes sends free. It does NOT give
    us longer to send: permission is 24h from their last message like everyone
    else. This is kept because cost and prioritisation still care who came from
    an ad; the schedule no longer does.
    """
    return bool(getattr(lead, 'ctwa_entry_at', None))


def messaging_window_hours(lead) -> float:
    """Hours we actually have to work with: from the window opening to the
    moment free-form sending shuts off.

    24h for every lead. The ad window is a price and never bought us extra
    hours to send in — see Appointment.messaging_window_closes_at for the
    traffic that settled it.
    """
    default = DEFAULT_WINDOW_HOURS
    start = followup_window_start(lead)
    closes = getattr(lead, 'messaging_window_closes_at', None)
    if start is None or closes is None:
        return default
    hours = (closes - start).total_seconds() / 3600
    # A window already (nearly) spent still needs a positive span to divide.
    return hours if hours > 1 else default


def usable_window_hours(lead) -> float:
    """The span a schedule may occupy: the lead's messaging window less the
    safety margin, floored at half the window so a freak value can't collapse
    it to nothing."""
    window_hours = messaging_window_hours(lead)
    return max(window_hours - FOLLOWUP_WINDOW_MARGIN_HOURS, window_hours * 0.5)


def space_offsets(offsets, usable) -> tuple:
    """Offsets pushed apart to the minimum gap, and truncated at the window.

    THE SINGLE RESOLVER for "how far apart are these", used by the follow-up
    schedule and both nudge loops. Two jobs, in this order:

      * push each touch to at least FOLLOWUP_MIN_GAP_HOURS after the one before
        it, so no written cadence can produce a pair closer than the floor
        (VERY_HOT on a 24h window was 3.8h apart);
      * DROP any touch that no longer fits inside the usable window rather than
        squeezing it back in. When the gap and the count cannot both hold, the
        COUNT gives: four is a ceiling, not a quota, and a bounced or bunched
        message costs more than a missing one.

    Truncating here is what keeps the count honest, because every reader of the
    count reads it off the schedule (max_followups_for). A lead the window can
    only carry two touches for is a lead the UI chip, the dashboard and the LLM
    prompt all describe as having two.
    """
    spaced = []
    for offset in offsets:
        if spaced:
            offset = max(offset, spaced[-1] + FOLLOWUP_MIN_GAP_HOURS)
        if offset > usable:
            break
        spaced.append(offset)
    # Never nothing: the first touch stands even on a window too short to hold
    # it properly, because a lead we never chase at all is the worse failure.
    return tuple(spaced) or (min(offsets[0], usable),)


def followup_offsets_for(lead):
    """The four touches this lead gets, as absolute hours from the moment their
    messaging window opened (their last message to us).

    ALWAYS FOUR. What the window changes is WHERE they sit:

        72h of window  -> FOLLOWUP_BAND_OFFSETS, at their literal hours: 2
                          touches on day one, 1 on day two, 1 on day three
        anything less  -> SHORT_WINDOW_FRACTIONS, four touches spread across
                          the span the lead actually has

    The band placement is not something a 24h lead can be given: a touch written
    for 33h or 60h could only ever bounce with 131047, since free-form sending
    is dead once their window shuts and we don't pay for templates. Nor is it
    something to squeeze - scaling the three-day shape into one day is just four
    messages in a day wearing the cadence's clothes. So a short window gets its
    own placement, tuned for a single day, and keeps the full four attempts.

    Fractions (rather than a second hour table) mean the same branch covers
    everything in between: an ad lead who replied late, with 40h of window left,
    gets four touches spread across those 40 hours.
    """
    tier = getattr(lead, 'lead_status', None)
    bands = FOLLOWUP_BAND_OFFSETS.get(tier, FOLLOWUP_BAND_OFFSETS[LeadStatus.COLD])
    usable = usable_window_hours(lead)
    if bands[-1] <= usable:
        offsets = space_offsets(bands, usable)
    else:
        fractions = SHORT_WINDOW_FRACTIONS.get(tier, SHORT_WINDOW_FRACTIONS[LeadStatus.COLD])
        offsets = space_offsets([f * usable for f in fractions], usable)
    # TWO for a lead who gets the plumber handoff (owner rule, 2026-09-21):
    # follow-up 1 is the contextual touch, follow-up 2 the handoff, and nothing
    # after it. Cut HERE, where the schedule is decided, so the cron, the UI
    # chip, the dashboard due-list and retirement all read the same two.
    if handoff_eligible(lead):
        offsets = offsets[:HANDOFF_TOUCHES]
    return offsets


# The run length for a lead who gets the handoff: the contextual touch, then
# the handoff (owner rule, 2026-09-21, down from four).
HANDOFF_TOUCHES = 2


def handoff_eligible(lead) -> bool:
    """Will this lead's second follow-up be the plumber handoff?

    The three fields (a real service type, a description, an area) AND a
    plumber number for the lead's own tenant: without the number there is no
    handoff to make, and the lead keeps the ordinary four-touch run. The same
    test `Command._handoff_touch` makes before it builds the message, so the
    schedule and the message can never disagree about who is handed off.
    """
    try:
        from bot.lead_handoff import service_label
        from bot.plumber_link import plumber_number
        return bool(service_label(lead)
                    and str(getattr(lead, 'project_description', '') or '').strip()
                    and str(getattr(lead, 'customer_area', '') or '').strip()
                    and plumber_number(lead))
    except Exception:
        return False


def max_followups_for(lead) -> int:
    """Attempts this lead gets before the run is retired: four, for everybody.

    Read off the schedule rather than declared, so the count and the timing can
    never disagree - whichever placement a lead's window earns them, it is four
    touches. The counter resets on every reply (reset_followup_sequence), so a
    lead who is actually talking to us keeps earning another run.
    """
    return len(followup_offsets_for(lead))


# ─── Spacing: the 2 / 1 / 1 cadence, in hours from the window opening ─────
# Absolute positions, measured from the moment the lead last messaged us. Every
# tuple obeys the same band contract (two touches inside the first day, one on
# the second, one on the third) and temperature only moves a touch WITHIN its
# band: hotter is chased sooner, colder gets more room to breathe.
#
# FOLLOWUP_BANDS is that contract in data, so a refactor that quietly drops a
# touch out of its day fails the test instead of the lead.
FOLLOWUP_BANDS = ((0, 24, 2), (24, 48, 1), (48, 72, 1))

FOLLOWUP_BAND_OFFSETS = {
    LeadStatus.VERY_HOT: (4.0, 10.0, 27.0, 51.0),
    LeadStatus.HOT:      (4.5, 11.0, 29.0, 54.0),
    LeadStatus.WARM:     (5.0, 12.0, 31.0, 57.0),
    LeadStatus.COLD:     (6.0, 13.0, 33.0, 60.0),
}

# The same four touches for a lead whose window cannot carry the three-day
# bands - a standard 24h lead, or an ad lead who replied with most of their 72h
# already spent. Positions as a SHARE of the usable span, so one table covers
# every window length; the last stays well under 1.0 so the final touch clears
# the close even after jitter and a contact-window roll. On a 24h window this is
# roughly COLD 3.6 / 8.6 / 13.5 / 18.9h.
SHORT_WINDOW_FRACTIONS = {
    LeadStatus.VERY_HOT: (0.08, 0.25, 0.45, 0.70),
    LeadStatus.HOT:      (0.10, 0.30, 0.52, 0.76),
    LeadStatus.WARM:     (0.13, 0.34, 0.56, 0.80),
    LeadStatus.COLD:     (0.16, 0.38, 0.60, 0.84),
}

# Reserved at the end of the window: the last follow-up must land before the
# free-form window shuts, not on its doorstep.
FOLLOWUP_WINDOW_MARGIN_HOURS = 1.5

# FOUR HOURS between touches, minimum, and it is a floor rather than a target
# (owner rule, 2026-09-09). Every path that sends a proactive message asks
# `_min_gap_hours` for it, and `space_offsets` bakes it into the schedule so the
# runtime guard rarely has to intervene.
#
# It was 1.5h, which the schedule itself almost never needed - the written
# cadences are 4 to 27 hours apart. What 1.5h really licensed was the COLLAPSE:
# absolute offsets rolled forward into the same contact window arrive at the
# same minute, and a cron catching up after an outage fires whatever is due.
#
# THE CONSEQUENCE CAN BE FEWER TOUCHES ON A SHORT WINDOW, and that is the trade
# the rule makes. The sendable hours are one 12.5h block a day (CONTACT_WINDOWS),
# so a 4h floor allows at most three sends per day and a standard 24h lead can
# lose a touch to the night. The cap of four is a ceiling, not a quota, and
# the count is read off the schedule everywhere (max_followups_for), so the UI
# chip, the dashboard due-list, the LLM prompt and cron retirement all follow.
FOLLOWUP_MIN_GAP_HOURS = 4.0

# We just spoke to this lead (a reply, a nudge, anything) — hold off, whatever
# the schedule says. Without this a follow-up can land minutes after our own
# message and read as if nobody is reading the conversation.
FOLLOWUP_QUIET_AFTER_OUTBOUND_HOURS = 1.5

# The lead is typing to us right now: their message is the live conversation and
# the bot's own reply is the touch. A follow-up on top of it is noise.
FOLLOWUP_LIVE_CONVERSATION_MINUTES = 20

# How close to the last sendable moment counts as "last call" — a pending touch
# inside this stretch goes out now rather than waiting for a tomorrow that the
# messaging window will not survive.
LAST_CALL_GRACE_MINUTES = 30

# On a last call the spacing USED to yield, down to 45 minutes: a touch that
# must go now or never was worth a tighter gap than one with a whole day ahead.
# A minimum of four hours is a minimum, so the relaxation is gone and this is
# kept equal to the floor rather than deleted, because several call sites read
# it and the last-call branch is still the right place to reason about.
#
# What gives instead is the TOUCH. A fourth message that cannot clear four
# hours before the window shuts is not sent at all - which is the same trade the
# schedule makes, and the honest one: a lead who hears from us twice in ninety
# minutes has learned something about us that no fourth touch recovers.
LAST_CALL_MIN_GAP_HOURS = FOLLOWUP_MIN_GAP_HOURS

# Assumed window length when the lead has no usable inbound timestamp yet.
DEFAULT_WINDOW_HOURS = 24.0

# ─── CTWA (Click-to-WhatsApp / Facebook ad) window ────────────────────────
# A lead who taps a Facebook or Instagram "Send message" ad opens a 72-hour
# free-form window instead of the standard 24. That is the ONLY thing the ad
# entry changes here: it is what lets the 2 / 1 / 1 cadence land at its written
# hours (day one, day two, day three) instead of being scaled into a single day.
# The touch COUNT is the same four for everyone.
CTWA_WINDOW_HOURS = 72.0

# Hours between the first delay re-engagement email (sent on the agreed
# follow-up date) and the second/final "last check" email. Keep this on the
# longer side so we never feel pushy on a cold-but-polite lead.
DELAY_SECOND_TOUCH_HOURS = 96  # 4 days


# ─── Context the follow-up must read (audit of live sends, 2026-09-19) ──────
# Every helper below exists because a real follow-up went out without it.

_FOLLOWUP_PREFIXES = ('[AUTO FOLLOW-UP] ', '[AUTOMATIC FOLLOW-UP] ',
                      '[MANUAL FOLLOW-UP] ', '[BULK MANUAL FOLLOW-UP] ')


def not_a_lead_reason(lead) -> str:
    """Why this conversation is not a lead to chase, or '' when it is.

    Two kinds went out on 2026-09-18/19 and should never have:
      - a vendor pitching US (a marketing agency's "20 dollar package", barmak
        1159) was asked twice whether it wanted a bathroom or a kitchen;
      - a message the bot itself judged out of scope ("Ndasiya grease pamota",
        homebase 1163, tagged [OOS_PENDING] category=out_of_scope) was told
        "you got in touch about some work".
    A lead whose job we already know is always chaseable, whatever else they
    said along the way.
    """
    if (getattr(lead, 'project_type', '') or getattr(lead, 'project_description', '')):
        return ''
    user_turns = [str(m.get('content') or '') for m in (getattr(lead, 'conversation_history', None) or [])
                  if isinstance(m, dict) and m.get('role') == 'user']
    try:
        from bot.out_of_scope_handler import is_inbound_sales_pitch
        if user_turns and any(is_inbound_sales_pitch(t) for t in user_turns):
            return 'a vendor pitching us, not a customer'
    except Exception:
        logger.warning('Sales-pitch check failed', exc_info=True)
    if '[OOS_PENDING] category=out_of_scope' in (getattr(lead, 'internal_notes', '') or ''):
        return 'their message was out of scope and nothing plumbing has come since'
    return ''


# What the ad they clicked was about. All eleven barmak ad leads of 2026-09-18
# came from one BATHROOM ad and were asked "is it a bathroom, a kitchen, or a
# new installation?"; one answered "I thought you were selling tubs, looks
# like it's not" and left (1169).
_AD_SUBJECTS = (('kitchen', ('kitchen',)),
                ('bathroom', ('bathroom', 'bath', 'tub', 'shower', 'toilet')))


def ad_subject(lead):
    """'bathroom' / 'kitchen' when the lead came from an ad about one, else None."""
    ref = getattr(lead, 'ctwa_referral', None) or {}
    if not isinstance(ref, dict):
        return None
    text = ' '.join(str(ref.get(k) or '') for k in ('headline', 'body')).lower()
    for subject, words in _AD_SUBJECTS:
        if any(re.search(r'\b' + w, text) for w in words):
            return subject
    return None


def pending_price_tiedown(lead):
    """The subject noun ('a new tub') of a price tie-down still waiting on the
    lead, '' when it names none, or None when no tie-down is pending.

    Pending means: one of OUR messages since the lead last spoke carries the
    tie-down. Our own follow-ups count (they re-ask it), so the run stays on
    the same question across attempts. Without this the tie-down was answered
    for them: "which suburb are you in? Also, does the starting price range sit
    alright with your budget?" (barmak 1158), two questions in one touch.
    """
    from bot.views.plumbot.response_mixin import ResponseMixin
    sigs = ResponseMixin._price_tiedown_signatures()
    for msg in reversed(getattr(lead, 'conversation_history', None) or []):
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'user':
            return None
        text = str(msg.get('content') or '')
        for prefix in _FOLLOWUP_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
        low = text.lower()
        if any(sig in low for sig in sigs):
            m = re.search(r'invest in (?:for )?((?:a new|the) [a-z]+)', low)
            if m:
                return m.group(1)
            entry = ResponseMixin._invest_subject(text)
            return entry[2] if entry else ''
    return None


def lead_writes_shona(lead) -> bool:
    """Did the lead write to us in Shona? Follow-ups answer in their language."""
    text = ' '.join(str(m.get('content') or '') for m in (lead.conversation_history or [])
                    if isinstance(m, dict) and m.get('role') == 'user')
    if not text.strip():
        return False
    # AI-primary, like the chat path: the keyword detector needs two markers
    # and read "Ndoda kuchinja tub nemusinki mubathroom yangu" as English.
    # Only asked on the AI path, which is already spending a call.
    try:
        from bot.repeated_question_detector import detect_language
        return detect_language(text[-600:]) == 'shona'
    except Exception:
        return False


def fit_to_template(ai_text: str, template: str) -> str:
    """Hold the model's version to the shape of the owner's script.

    The model was told to stay close to the template and still added a second
    question ("Which suburb are you in? Also, does the starting price range sit
    alright with your budget?", barmak 1158) and a closing line nobody needs
    ("Once we know that we can tell you how we will sort it."). The owner's
    scripts themselves are sent whole: some carry two questions on purpose
    ("what made you reach out? ... Is it a bathroom, kitchen...?") and some end
    on a statement, so the rule is relative to the script, never absolute:
      - more questions than the script asks -> the script goes out instead;
      - the script ends on its question -> anything after the model's last
        question is cut.
    """
    if not ai_text:
        return template
    allowed = max(template.count('?'), 1)
    if ai_text.count('?') > allowed:
        return template
    if template.rstrip().endswith('?') and '?' in ai_text:
        return ai_text[:ai_text.rindex('?') + 1].rstrip()
    return ai_text


class _Subjectless:
    """A stand-in `self` for ResponseMixin._price_tiedown: the cron has no
    Plumbot, and the subject is always passed explicitly here."""
    _PRICE_TIEDOWN = None
    appointment = None

    def __init__(self):
        from bot.views.plumbot.response_mixin import ResponseMixin
        self._PRICE_TIEDOWN = ResponseMixin._PRICE_TIEDOWN
        self._lang_key = ResponseMixin._lang_key


# ─────────────────────────────────────────────────────────────────────────────
class Command(BaseCommand):
    help = 'At least 4 follow-ups, spread across the lead messaging window — Hormozi timing, value-first messaging'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without sending')
        parser.add_argument('--force', action='store_true',
                            help='Ignore contact windows and cooldown rules')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force   = options['force']

        self.stdout.write(self.style.SUCCESS('🔍 Follow-up check starting…'))
        if dry_run:
            self.stdout.write(self.style.WARNING('🧪 DRY-RUN — no messages will be sent'))

        now_local = timezone.now().astimezone(SA_TIMEZONE)

        # Dispatch any staff-scheduled follow-ups that are now due. These run
        # regardless of the contact-window gate below — staff chose these exact
        # times deliberately — so do it before the early return.
        try:
            from bot.management.commands.send_scheduled_followups import (
                dispatch_due_scheduled_followups,
            )
            sres = dispatch_due_scheduled_followups(
                dry_run=dry_run, log=lambda m: self.stdout.write(m)
            )
            if sres['sent'] or sres['failed']:
                self.stdout.write(self.style.SUCCESS(
                    f"📅 Scheduled follow-ups → sent={sres['sent']} failed={sres['failed']}"
                ))
        except Exception as exc:  # noqa: BLE001 — never let this block normal follow-ups
            logger.warning('Scheduled follow-up dispatch failed: %s', exc)

        if not force and not self._in_contact_window(now_local):
            self.stdout.write(
                self.style.WARNING(
                    f'⏰ Outside contact windows ({now_local.strftime("%H:%M")} SAST). '
                    'Pass --force to override.'
                )
            )
            return

        self._nudge_delay_flow_ghosts(now_local, dry_run)
        self._nudge_parked_leads(now_local, dry_run)
        self._process_delayed_reactivations(now_local, dry_run)
        self._process_job_date_ladder(now_local, dry_run)

        self._print_eligibility_breakdown(now_local, force)
        leads = self._get_eligible_leads(now_local, force)
        self.stdout.write(f'📊 {leads.count()} leads eligible for follow-up')

        totals = dict(sent=0, skipped=0, errors=0, completed=0, ai=0, template=0)

        for lead in leads:
            try:
                result = self._process_lead(lead, now_local, dry_run, force)
                totals[result['status']] = totals.get(result['status'], 0) + 1
                if result.get('ai_generated'):
                    totals['ai'] += 1
                if result.get('template_fallback'):
                    totals['template'] += 1
            except Exception as exc:
                logger.error(f'Error processing lead {lead.id}: {exc}')
                totals['errors'] += 1
                self.stdout.write(self.style.ERROR(f'❌ Lead {lead.id}: {exc}'))

        self.stdout.write(self.style.SUCCESS('\n📊 SUMMARY'))
        for k, v in totals.items():
            self.stdout.write(f'  {k}: {v}')

    # ─── Within-window follow-ups for delay flow ghosts ──────────────────────

    # Messages per step per attempt (0-indexed).
    _DELAY_NUDGE_MESSAGES = {
        'delay_timeframe': [
            "Just checking in. Roughly when do you think you'll be back? Even a ballpark works.",
            "No rush at all. Just need a rough idea so we can set a reminder for you.",
            "Last check-in from us. When would work best to reconnect?",
            "We will leave this with you. Just send us a message whenever you are ready and we will pick up right where we left off.",
        ],
        'delay_confirm': [
            "Just checking, is it okay if we reach out to you on {date}? A quick yes or no is all we need.",
            "Should we put {date} in the diary to follow up with you?",
            "Last one from us. Would {date} work for us to check in?",
            "We will leave this with you. Whenever you are ready, just send us a message.",
        ],
        # The first nudge carries the reason the lead is better off on email —
        # the same three benefits as the in-conversation ask (it keeps, it
        # travels, it compares). A bare "what is your email?" is an extraction
        # with nothing in it for them, and it got ignored.
        'delay_email': [
            "One thing before we go. The quote goes over as a PDF you can keep, "
            "pass on to whoever else is in on the decision, and hold up against "
            "any other quotes. What email should we send it to?",
            "Happy to hold the quote until you are ready. What email works best?",
            "Last ask on the email. What address should we use?",
            "No worries if you would rather not share. We will follow up on WhatsApp on the agreed date.",
        ],
    }

    # Where each nudge sits in the lead's messaging window, as a fraction of the
    # usable span — on a 24h window that's roughly 2h, 7.5h, 13.5h and 19h.
    # Fractions rather than fixed steps for the same reason as the main cadence:
    # a nudge deferred overnight by the contact window used to push the last one
    # past the window close, so a ghost quietly got three nudges instead of four.
    _DELAY_NUDGE_FRACTIONS = (0.09, 0.34, 0.60, 0.85)

    def _delay_nudge_offsets(self, lead):
        """Absolute hours-from-last-inbound for each delay nudge.

        Through `space_offsets` like the main schedule: the four-hour floor is a
        rule about what a LEAD receives, so it cannot be something only one of
        the three loops that message them obeys.
        """
        usable = max(
            self._messaging_window_hours(lead) - FOLLOWUP_WINDOW_MARGIN_HOURS,
            self._messaging_window_hours(lead) * 0.5,
        )
        return space_offsets(
            [f * usable for f in self._DELAY_NUDGE_FRACTIONS], usable)

    def _nudge_delay_flow_ghosts(self, now_local, dry_run):
        """
        Sends at least 4 contextual WhatsApp follow-ups inside the lead's own
        messaging window to leads that ghosted at any step of the delay flow:
          - Step 1 (delay_timeframe): asked "roughly when will you be back?"
          - Step 2 (delay_confirm):   asked "is it okay if we reach out on {date}?"
          - Step 3 (delay_email):     asked "what email should we send your quote to?"

        Nudge count and last-sent time are stored in internal_notes so the
        cron can resume correctly across multiple runs.
        """
        now = timezone.now()
        # The widest window any lead can have, less an hour of slack. This was
        # the ad window's 72h, on the belief that an ad tap bought three days of
        # sending; it did not, and every lead selected between 24h and 72h was
        # rejected later by messaging_window_open anyway. Tied to the constant
        # now so it tracks the real rule. The per-lead check still decides; this
        # is only a cheap prefilter.
        window_open_cutoff = now - timedelta(
            hours=Appointment.CUSTOMER_SERVICE_WINDOW_HOURS - 1)
        min_wait_cutoff    = now - timedelta(hours=1)

        candidates = (
            Appointment.objects.real()
            .filter(
                is_lead_active=True,
                last_inbound_at__gte=window_open_cutoff,
                last_inbound_at__lte=min_wait_cutoff,
                internal_notes__contains='[OOS_PENDING] category=delay_',
            )
            # Access check-ins already have a scheduled reactivation at the agreed
            # time — don't also nudge them here (would double-message).
            .exclude(internal_notes__contains='category=delay_checkin')
            .exclude(chatbot_paused=True)
            .exclude(status='confirmed')
            # A declined area or an explicit "stop messaging me" outranks a
            # pending delay nudge — see _exclude_suppressed_states (lead 872).
            .exclude(internal_notes__contains='[EXCLUDED_AREA')
            .exclude(internal_notes__contains='[STOP_REQUESTED]')
            .exclude(internal_notes__contains='[OOS_DECLINED]')
            .exclude(internal_notes__contains='[FOLLOWUPS_OFF]')
        )

        count = candidates.count()
        if count:
            self.stdout.write(f'💬 {count} delay-flow ghost(s) eligible for in-window nudge')

        for lead in candidates:
            try:
                notes      = lead.internal_notes or ''
                step, date = self._parse_delay_step(notes)
                if not step:
                    continue

                nudge_count, last_nudge_at = self._read_delay_nudge_state(notes)
                # A reply puts the count back to zero (owner rule, 2026-09-21),
                # as it does for the main follow-ups. Read here rather than
                # written on the reply: a nudge stamped before their latest
                # message belongs to the previous silence. It used to carry
                # over, so a lead who replied mid-flow resumed at nudge #3.
                if (last_nudge_at and lead.last_inbound_at
                        and lead.last_inbound_at > last_nudge_at):
                    nudge_count, last_nudge_at = 0, None

                # Read off THIS LEAD's schedule, bounded by the copy we have.
                # It used to be max(FOLLOWUP_MIN_COUNT, len(fractions)) - "at
                # least four" - which is the opposite of a ceiling, and which no
                # longer matches a schedule the four-hour floor can truncate.
                max_nudges = min(
                    len(self._delay_nudge_offsets(lead)),
                    len(self._DELAY_NUDGE_MESSAGES[step]),
                )
                # Two for a delay-signal lead who gets the handoff (owner rule,
                # 2026-09-21): the contextual nudge, then the handoff, then
                # nothing. No field requirement for this group, only a plumber
                # number to hand them to.
                from bot.plumber_link import plumber_number
                if plumber_number(lead):
                    max_nudges = min(max_nudges, HANDOFF_TOUCHES)
                if nudge_count >= max_nudges:
                    continue

                # The cross-loop ceiling: this loop's own counter cannot see the
                # touches the other loops sent off the same silence.
                if touches_since_last_reply(lead) >= FOLLOWUP_CAP_PER_REPLY:
                    continue
                # The handoff was this silence's last touch: stop until they
                # reply (owner rule, 2026-09-21).
                if handoff_sent_since_last_reply(lead):
                    continue

                # A free-form send outside the window bounces with 131047 and
                # flags the lead's window closed for every other send path.
                if not lead.messaging_window_open:
                    continue
                # Nor one Meta would charge for — a nudge is optional.
                if not paid_sends_allowed() and not lead.messaging_is_free:
                    continue

                # Absolute offset into the messaging window, measured from the
                # lead's last message — never from the previous nudge, so a
                # delayed nudge can't push the rest out of the window.
                reference = lead.last_inbound_at
                if not reference:
                    continue
                offset_hours = self._delay_nudge_offsets(lead)[nudge_count]
                elapsed = (now - reference).total_seconds() / 3600
                if elapsed < offset_hours and not self._is_last_call(lead, now):
                    continue
                # ...but still never two nudges back to back.
                if last_nudge_at:
                    since_last = (now - last_nudge_at).total_seconds() / 3600
                    if since_last < self._min_gap_hours(lead, now):
                        continue

                # Build message. The SECOND nudge of a silence is the plumber
                # handoff (owner rule, 2026-09-21), whatever step they stopped
                # at: no email, no date and missing fields do not matter for a
                # lead who gave a delay signal. The first stays the contextual
                # nudge for the step they are on. No plumber number for the
                # lead's tenant means the ordinary nudge goes instead.
                name    = lead.customer_name or ''
                hi      = f'Hi {name}' if name else 'Hi there'
                message = ''
                if nudge_count + 1 == self.HANDOFF_ATTEMPT:
                    from bot.plumber_link import handoff_message
                    message = handoff_message(lead)
                if not message:
                    template = self._DELAY_NUDGE_MESSAGES[step][nudge_count]
                    if '{date}' in template and not date:
                        # Never render a missing date as the literal word "None"
                        # to a customer. Skip until the stored date is available.
                        logger.warning(
                            "Delay nudge skipped for lead %s: %s template needs a date "
                            "but none is stored", lead.id, step,
                        )
                        continue
                    body = template.format(date=date) if '{date}' in template else template
                    message = _greet(hi, body)
                # The visit is free ONCE, at the start. A nudge is never the
                # place to say it again — see strip_repeat_free_visit.
                message  = dequalify_free_visit(lead, message)

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(
                        f'🧪 Would send delay nudge #{nudge_count + 1} to lead {lead.id} '
                        f'[{step}]: "{message[:80]}…"'
                    ))
                    continue

                clean = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
                get_client_for_tenant(lead.tenant).send_text_message(clean, message)

                self._write_delay_nudge_state(lead, nudge_count + 1, now)
                lead.add_conversation_message(
                    'assistant', f'[DELAY NUDGE {nudge_count + 1}] {message}'
                )

                self.stdout.write(self.style.SUCCESS(
                    f'✅ Delay nudge #{nudge_count + 1}/4 → lead {lead.id} [{step}]'
                ))

            except Exception as exc:
                logger.error(f'Delay flow nudge failed for lead {lead.id}: {exc}')
                self.stdout.write(self.style.ERROR(f'❌ Delay nudge lead {lead.id}: {exc}'))

    def _parse_delay_step(self, notes):
        """Return (step_name, friendly_date_or_None) from internal_notes."""
        m = re.search(r'\[OOS_PENDING\] category=(delay_\w+) original=([^\n]*)', notes)
        if not m:
            return None, None
        step     = m.group(1)
        # _write_pending url-encodes the original (the "|" separator becomes %7C),
        # so decode it before splitting — matches how _read_pending reads it back.
        original = unquote(m.group(2).strip())
        date_str = None
        if step == 'delay_confirm':
            parts = original.split('|')
            iso   = parts[-1].strip() if len(parts) > 1 else None
            if iso:
                try:
                    from datetime import date as _d
                    date_str = _d.fromisoformat(iso).strftime('%A %d %B')
                except Exception:
                    pass
        return step, date_str

    def _read_delay_nudge_state(self, notes):
        """Return (count, last_sent_datetime_or_None) from internal_notes."""
        count_m = re.search(r'\[DELAY_NUDGE_COUNT\] (\d+)', notes)
        last_m  = re.search(r'\[DELAY_NUDGE_LAST\] ([^\n]+)', notes)
        count   = int(count_m.group(1)) if count_m else 0
        last    = None
        if last_m:
            try:
                from datetime import datetime as _dt
                last = _dt.fromisoformat(last_m.group(1).strip())
                if last.tzinfo is None:
                    import pytz as _pytz
                    last = _pytz.utc.localize(last)
            except Exception:
                pass
        return count, last

    def _write_delay_nudge_state(self, lead, new_count, sent_at):
        """
        Persist nudge count and timestamp to internal_notes.
        When all 4 nudges are exhausted at steps 1 or 2 (before is_delayed is set),
        clear the stale [OOS_PENDING] state so the lead re-enters normal follow-ups.
        """
        notes = lead.internal_notes or ''
        notes = re.sub(r'\[DELAY_NUDGE_COUNT\] \d+\n?', '', notes)
        notes = re.sub(r'\[DELAY_NUDGE_LAST\] [^\n]+\n?', '', notes)

        if new_count >= 4 and not lead.is_delayed:
            # Nudges exhausted — customer never confirmed a return date.
            # Clear the pending state so the lead can enter regular follow-ups.
            notes = re.sub(r'\[OOS_PENDING\][^\n]*\n?', '', notes)

        notes = notes.strip()
        notes = f'{notes}\n[DELAY_NUDGE_COUNT] {new_count}\n[DELAY_NUDGE_LAST] {sent_at.isoformat()}'.strip()
        lead.internal_notes = notes
        lead.save(update_fields=['internal_notes'])

    # ─── Re-engagement for parked (soft brush-off) leads ─────────────────────

    # Gentle re-engagement messages for leads who soft-exited ("I'll get back
    # to you") and were parked. The greeting is prepended separately (like the
    # delay nudge), so these are bodies only. The first re-offers the portfolio
    # (safe whether or not they already received it); the last leaves the door
    # open and stops.
    _PARKED_NUDGE_MESSAGES = [
        "just checking in — no pressure at all. If it helps while you decide, I can "
        "send over our portfolio of past projects and full pricing. Or whenever you "
        "are ready, a free on-site visit and fixed quote is one message away.",
        "one thing worth knowing while you think it over: the price we put on "
        "paper is the price you pay, with nothing added on the day.",
        "if it is easier, we can put the quote in an email so you have it on hand "
        "for whenever you are ready. Just send us the address and we will do the rest.",
        "we will leave this with you. Whenever the time is right, just send us a "
        "message and we will pick up right where we left off.",
    ]

    # Parked leads asked for space, so their touches sit in the BACK half of the
    # messaging window — but inside it. They used to be spaced 3 and 7 DAYS out,
    # which is past the 24h free-form window: every one of those sends bounced
    # with 131047 (and the first bounce flags the lead's window closed, blocking
    # everything else). Four gentle touches that actually arrive beat two that
    # cannot. Fractions of the usable window, absolute from the last inbound.
    _PARKED_NUDGE_FRACTIONS = (0.38, 0.56, 0.72, 0.88)

    def _parked_nudge_offsets(self, lead):
        """Absolute hours-from-last-inbound for each parked re-engagement nudge.

        Same spacing resolver as the main schedule and the delay loop.
        """
        window = self._messaging_window_hours(lead)
        usable = max(window - FOLLOWUP_WINDOW_MARGIN_HOURS, window * 0.5)
        return space_offsets(
            [f * usable for f in self._PARKED_NUDGE_FRACTIONS], usable)

    # Don't re-engage leads who have been cold for more than this — at that point
    # they are genuinely dormant and a nudge is just spam.
    _PARKED_NUDGE_MAX_AGE_DAYS = 30

    def _nudge_parked_leads(self, now_local, dry_run):
        """
        Gently re-engage leads who soft brushed off ("I'll get back to you") and
        were parked via mark_parked() ([PARKED] tag). Sends at least four spaced
        WhatsApp nudges across the back half of the lead's messaging window,
        then leaves them fully alone.

        Count and last-sent time live in internal_notes so the cron resumes
        across runs. Leads still mid delay-flow ([OOS_PENDING] category=delay_)
        are left to _nudge_delay_flow_ghosts; this only handles parked leads not
        in that flow. Respects the contact window (gated by the caller in
        handle()).
        """
        now = timezone.now()
        window_open_cutoff = now - timedelta(days=self._PARKED_NUDGE_MAX_AGE_DAYS)

        candidates = (
            Appointment.objects.real()
            .filter(
                is_lead_active=True,
                internal_notes__contains='[PARKED]',
                last_inbound_at__gte=window_open_cutoff,
            )
            .exclude(status='confirmed')
            .exclude(chatbot_paused=True)
            .exclude(internal_notes__contains='[HANDED_OFF]')
            .exclude(internal_notes__contains='[OOS_PENDING] category=delay_')
            # Parked is why this lead is here, but a declined area or an
            # explicit stop request still outranks the nudge (lead 872).
            .exclude(internal_notes__contains='[EXCLUDED_AREA')
            .exclude(internal_notes__contains='[STOP_REQUESTED]')
            .exclude(internal_notes__contains='[OOS_DECLINED]')
            .exclude(internal_notes__contains='[FOLLOWUPS_OFF]')
        )

        count = candidates.count()
        if count:
            self.stdout.write(f'🅿️ {count} parked lead(s) eligible for re-engagement nudge')

        for lead in candidates:
            try:
                notes = lead.internal_notes or ''
                nudge_count, last_nudge_at = self._read_parked_nudge_state(notes)
                # A reply resets the count (owner rule, 2026-09-21): a nudge
                # stamped before their latest message belongs to the previous
                # silence. This loop used to STOP for good once they replied
                # after a nudge; now the new silence gets its own cycle.
                if (last_nudge_at and lead.last_inbound_at
                        and lead.last_inbound_at > last_nudge_at):
                    nudge_count, last_nudge_at = 0, None

                # Read off THIS LEAD's schedule, bounded by the copy we have.
                # See the delay loop: "at least four" was the wrong shape once
                # four became a ceiling.
                max_nudges = min(
                    len(self._parked_nudge_offsets(lead)),
                    len(self._PARKED_NUDGE_MESSAGES),
                )
                # Two when the handoff can be made, as in the delay loop.
                from bot.plumber_link import plumber_number
                if plumber_number(lead):
                    max_nudges = min(max_nudges, HANDOFF_TOUCHES)
                if nudge_count >= max_nudges:
                    continue

                # The cross-loop ceiling: this loop's own counter cannot see the
                # touches the other loops sent off the same silence.
                if touches_since_last_reply(lead) >= FOLLOWUP_CAP_PER_REPLY:
                    continue

                # A free-form send outside the window bounces with 131047 and
                # flags the lead's window closed, which would block every other
                # send path too. Never attempt one.
                if not lead.messaging_window_open:
                    continue
                # Nor one Meta would charge for — a nudge is optional.
                if not paid_sends_allowed() and not lead.messaging_is_free:
                    continue

                # The handoff was this silence's last touch: stop until they
                # reply (owner rule, 2026-09-21).
                if handoff_sent_since_last_reply(lead):
                    continue

                # Absolute offset into the window from the lead's last message,
                # so a nudge deferred overnight doesn't push the rest past the
                # close — the reference never shifts to the previous nudge.
                reference = lead.last_inbound_at
                if not reference:
                    continue
                offset_hours = self._parked_nudge_offsets(lead)[nudge_count]
                elapsed_hours = (now - reference).total_seconds() / 3600
                if elapsed_hours < offset_hours and not self._is_last_call(lead, now):
                    continue
                if last_nudge_at:
                    since_last = (now - last_nudge_at).total_seconds() / 3600
                    if since_last < self._min_gap_hours(lead, now):
                        continue

                # A parked lead gave a delay signal (a soft brush-off), so the
                # SECOND nudge is the plumber handoff, as in the delay loop.
                name = lead.customer_name or ''
                hi   = f'Hi {name}' if name else 'Hi there'
                message = ''
                if nudge_count + 1 == self.HANDOFF_ATTEMPT:
                    from bot.plumber_link import handoff_message
                    message = handoff_message(lead)
                if not message:
                    message = _greet(hi, self._PARKED_NUDGE_MESSAGES[nudge_count])
                message = dequalify_free_visit(lead, message)

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(
                        f'🧪 Would send parked nudge #{nudge_count + 1} to lead {lead.id}: '
                        f'"{message[:80]}…"'
                    ))
                    continue

                clean = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
                get_client_for_tenant(lead.tenant).send_text_message(clean, message)

                self._write_parked_nudge_state(lead, nudge_count + 1, now)
                lead.add_conversation_message(
                    'assistant', f'[PARKED NUDGE {nudge_count + 1}] {message}'
                )

                self.stdout.write(self.style.SUCCESS(
                    f'✅ Parked nudge #{nudge_count + 1}/'
                    f'{max_nudges} → lead {lead.id}'
                ))

            except Exception as exc:
                logger.error(f'Parked nudge failed for lead {lead.id}: {exc}')
                self.stdout.write(self.style.ERROR(f'❌ Parked nudge lead {lead.id}: {exc}'))

    def _read_parked_nudge_state(self, notes):
        """Return (count, last_sent_datetime_or_None) from internal_notes."""
        count_m = re.search(r'\[PARKED_NUDGE_COUNT\] (\d+)', notes or '')
        last_m  = re.search(r'\[PARKED_NUDGE_LAST\] ([^\n]+)', notes or '')
        count   = int(count_m.group(1)) if count_m else 0
        last    = None
        if last_m:
            try:
                from datetime import datetime as _dt
                last = _dt.fromisoformat(last_m.group(1).strip())
                if last.tzinfo is None:
                    import pytz as _pytz
                    last = _pytz.utc.localize(last)
            except Exception:
                pass
        return count, last

    def _write_parked_nudge_state(self, lead, new_count, sent_at):
        """Persist parked-nudge count and timestamp to internal_notes."""
        notes = lead.internal_notes or ''
        notes = re.sub(r'\[PARKED_NUDGE_COUNT\] \d+\n?', '', notes)
        notes = re.sub(r'\[PARKED_NUDGE_LAST\] [^\n]+\n?', '', notes).strip()
        notes = f'{notes}\n[PARKED_NUDGE_COUNT] {new_count}\n[PARKED_NUDGE_LAST] {sent_at.isoformat()}'.strip()
        lead.internal_notes = notes
        lead.save(update_fields=['internal_notes'])

    # ─── Delayed lead re-engagement ──────────────────────────────────────────

    def _delay_wa_allowed(self, lead):
        """(allowed, reason) for a WhatsApp check-back to a delayed lead.

        The reactivation path used to fire WhatsApp blind, unlike every other
        send in this command. Outside the free-form window that bounces 131047,
        and the first bounce flags the lead's window closed — so a check-back
        the customer agreed to could burn the lead's window and still not
        arrive. When the agreed moment lands INSIDE the free window (the common
        case for a check-back a day or two out) WhatsApp is the right channel
        and costs nothing; outside it, the email touch carries the follow-up.
        """
        if not lead.messaging_window_open:
            return False, 'free-form window closed'
        # Permission is not price. A check-back is optional by definition, so it
        # waits for a free window rather than buying one (owner rule: keep
        # everything about Meta messaging free).
        if not paid_sends_allowed() and not lead.messaging_is_free:
            return False, f'would be billable ({lead.messaging_cost_reason})'
        return True, 'free window open'

    def _process_delayed_reactivations(self, now_local, dry_run):
        """
        Finds delayed leads whose follow-up date has arrived and contacts them.

        Two-touch email sequence (per lead, per delay cycle):
          • Touch 1 — sent immediately when delay_followup_due_at arrives.
                      WhatsApp goes out alongside touch 1 (single shot).
                      [DELAY_EMAIL_COUNT] is bumped to 1.
                      delay_followup_due_at is pushed forward by
                      DELAY_SECOND_TOUCH_HOURS so the cron returns for touch 2.
          • Touch 2 — sent ~4 days after touch 1 via send_delay_last_check_email.
                      Short, copy-different, explicit exit ("reply 'later'").
                      [DELAY_EMAIL_COUNT] is bumped to 2.
                      is_delayed and [DELAY_SIGNAL] are cleared — lead is fully
                      retired from the delay queue at this point.

        Leads without an email skip the 2-touch path entirely: one WhatsApp
        shot and we clear is_delayed (preserves the original single-shot
        behaviour for SMS-only leads).

        If a touch fails to send through either channel,
        delay_followup_due_at is pushed forward 24 hours so the cron retries
        tomorrow without spamming the same lead.
        """
        import re as _re
        from django.db.models import Q
        from bot.customer_emails import (
            send_delay_followup_email,
            send_delay_last_check_email,
        )

        due = (
            Appointment.objects.real()
            .filter(
                is_lead_active=True,
                is_delayed=True,
                delay_followup_due_at__lte=timezone.now(),
            )
            .exclude(chatbot_paused=True)
            # A lead on the job-date ladder is followed up by
            # _process_job_date_ladder at job -7 / -3, and its stored check-back
            # IS job -7, so this loop would fire the same day with the "back
            # and settled in?" copy, then a "last check" email four days later.
            # The near-term check-ins ([DELAY_KIND] pdf/access) are not ladder
            # touches and still go from here.
            .exclude(Q(internal_notes__contains='[JOB_DATE]')
                     & ~Q(internal_notes__contains='[DELAY_KIND]'))
        )
        due = self._exclude_suppressed_states(due)

        count = due.count()
        if count:
            self.stdout.write(f'🔔 {count} delayed lead(s) due for re-engagement')

        for lead in due:
            try:
                # Handed off to the plumber: that was the last text until they
                # reply (owner rule, 2026-09-21). No check-back after it.
                if handoff_sent_since_last_reply(lead):
                    continue
                name    = lead.customer_name or ''
                hi      = f'Hi {name}' if name else 'Hi there'
                service = self._service_label(lead)
                area    = lead.customer_area or ''
                desc    = (lead.project_description or '').strip()

                if desc:
                    detail = desc[:80]
                elif area:
                    detail = f'{service} in {area}'
                else:
                    detail = service

                has_email   = bool(getattr(lead, 'customer_email', None))
                email_count = self._read_delay_email_count(lead.internal_notes or '')
                # touch == 2 only if we've already sent touch 1 AND we have an email
                is_second_touch = has_email and email_count >= 1

                # Access check-in: the lead deferred to arrange access (no one
                # home / tenant / keys), not to travel. Use an access-appropriate
                # message and treat it as a single WhatsApp shot (no quote-email
                # 2-touch sequence).
                is_access_checkin = '[DELAY_KIND] access_checkin' in (lead.internal_notes or '')

                # Portfolio check-in: we sent the catalog/pricing PDF on WhatsApp and
                # scheduled this touch to land in the last stretch of the lead's
                # free-form window (24h organic / 72h ad).
                is_pdf_checkin = '[DELAY_KIND] pdf_checkin' in (lead.internal_notes or '')

                # ── Build the WhatsApp message (touch 1 only) ───────────────────
                if is_access_checkin:
                    message = (
                        f"{hi}, just checking in. Were you able to sort out access "
                        f"on your side?\n\n"
                        f"Happy to lock in a time to come through whenever suits you."
                    )
                elif is_pdf_checkin:
                    # Contextual: reference the job THEY described plus the lead
                    # magnet we sent. Soft micro-yes close only — this lead gave a
                    # delay signal, so no booking push. Paragraph breaks on purpose:
                    # one block of text is hard to read on WhatsApp.
                    want = ' '.join((lead.project_description or '').split())
                    if not want and lead.project_type:
                        try:
                            want = lead.get_project_type_display() or lead.project_type
                        except Exception:
                            want = lead.project_type
                    if want and len(want) > 140:
                        want = want[:140].rsplit(' ', 1)[0]
                    if want:
                        message = (
                            f"{hi}, hope you got a chance to look through the "
                            f"portfolio and pricing guide we sent.\n\n"
                            f"About the job you mentioned, {want}. The plumber can "
                            f"put an exact, all-in figure on it with a quick "
                            f"20-minute look at the space, free of charge.\n\n"
                            f"Is that the kind of work you had in mind?"
                        )
                    else:
                        message = (
                            f"{hi}, did you get a chance to look through the "
                            f"portfolio and pricing guide we sent?\n\n"
                            f"See anything you like, or any questions I can help with?"
                        )
                else:
                    message = (
                        f"{hi}, hope you're back and settled in. "
                        f'You were looking at {detail}. Still keen to move forward? '
                        f"We're ready when you are."
                    )

                # The visit is free ONCE, at the start. Every leg below sends
                # this same body, so de-qualify here — before the dry-run
                # preview, so what it prints is what goes out.
                message = dequalify_free_visit(lead, message)

                if dry_run:
                    label = ('access check-in' if is_access_checkin
                             else 'portfolio check-in' if is_pdf_checkin
                             else 'last-check email' if is_second_touch
                             else 'reactivation')
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'🧪 Would send {label} to lead {lead.id} '
                            f'(email_count={email_count}, has_email={has_email})'
                        )
                    )
                    continue

                clean    = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
                wa_ok    = False
                email_ok = False

                if is_access_checkin or is_pdf_checkin:
                    # ── Near-term check-in: single WhatsApp shot ────────────
                    kind = 'portfolio' if is_pdf_checkin else 'access'
                    wa_allowed, wa_reason = self._delay_wa_allowed(lead)
                    if not wa_allowed:
                        # No email leg on this path, so hold the lead and retry
                        # rather than bouncing 131047 — the first bounce flags
                        # the window closed and would block the retry too.
                        logger.info('%s check-in held for lead %s: %s',
                                    kind, lead.id, wa_reason)
                    else:
                        try:
                            get_client_for_tenant(lead.tenant).send_text_message(clean, message)
                            wa_ok = True
                        except Exception as wa_exc:
                            logger.warning(
                                '%s check-in WhatsApp failed for lead %s: %s',
                                kind, lead.id, wa_exc,
                            )
                    if wa_ok:
                        notes = lead.internal_notes or ''
                        notes = _re.sub(r'\[DELAY_SIGNAL\][^\n]*\n?', '', notes)
                        notes = _re.sub(r'\[DELAY_KIND\] (?:access_checkin|pdf_checkin)\n?', '', notes)
                        notes = _re.sub(r'\[OOS_PENDING\][^\n]*\n?', '', notes).strip()
                        lead.is_delayed     = False
                        lead.internal_notes = notes
                        lead.save(update_fields=['is_delayed', 'internal_notes'])
                        lead.add_conversation_message(
                            'assistant', f'[DELAY {kind.upper()} CHECK-IN] {message}'
                        )
                        self.stdout.write(self.style.SUCCESS(
                            f'✅ {kind.title()} check-in sent for lead {lead.id} — delay queue cleared'
                        ))
                    else:
                        # Retry tomorrow rather than spamming.
                        lead.delay_followup_due_at = timezone.now() + timedelta(hours=24)
                        lead.save(update_fields=['delay_followup_due_at'])
                        self.stdout.write(self.style.WARNING(
                            f'  ⚠️  {kind.title()} check-in failed for lead {lead.id} — retry in 24h'
                        ))
                    continue

                if is_second_touch:
                    # ── Touch 2: email only ─────────────────────────────────
                    try:
                        send_delay_last_check_email(lead)
                        email_ok = True
                    except Exception as email_exc:
                        logger.warning(
                            'Delay last-check email failed for lead %s: %s',
                            lead.id, email_exc,
                        )
                else:
                    # ── Touch 1: WhatsApp (when it will reach them free) + email
                    wa_allowed, wa_reason = self._delay_wa_allowed(lead)
                    if wa_allowed:
                        try:
                            get_client_for_tenant(lead.tenant).send_text_message(clean, message)
                            wa_ok = True
                        except Exception as wa_exc:
                            logger.warning(
                                'Delay reactivation WhatsApp failed for lead %s: %s',
                                lead.id, wa_exc,
                            )
                            self.stdout.write(
                                self.style.WARNING(
                                    f'  ⚠️  WhatsApp failed for lead {lead.id} — trying email fallback'
                                )
                            )
                    else:
                        logger.info(
                            'Delay reactivation WhatsApp skipped for lead %s: %s',
                            lead.id, wa_reason,
                        )
                        self.stdout.write(
                            f'  ↩️  WhatsApp skipped for lead {lead.id} ({wa_reason}) — email only'
                        )

                    if has_email:
                        try:
                            send_delay_followup_email(lead)
                            email_ok = True
                        except Exception as email_exc:
                            logger.warning(
                                'Delay reactivation email failed for lead %s: %s',
                                lead.id, email_exc,
                            )

                # ── Outcome handling ─────────────────────────────────────────
                if wa_ok or email_ok:
                    notes = lead.internal_notes or ''

                    if is_second_touch:
                        # Final touch fired — retire from delay queue
                        lead.is_delayed = False
                        notes = _re.sub(r'\[DELAY_SIGNAL\][^\n]*\n?', '', notes).strip()
                        if email_ok:
                            notes = self._set_delay_email_count(notes, 2)
                        lead.internal_notes = notes
                        lead.save(update_fields=['is_delayed', 'internal_notes'])
                        lead.add_conversation_message(
                            'assistant', '[DELAY LAST CHECK] last-check email sent'
                        )
                        self.stdout.write(self.style.SUCCESS(
                            f'✅ Last-check email sent for lead {lead.id} — delay queue cleared'
                        ))
                    elif email_ok:
                        # Touch 1 went out on email — keep the lead in the delay
                        # queue so we can fire touch 2 in DELAY_SECOND_TOUCH_HOURS.
                        notes = self._set_delay_email_count(notes, 1)
                        lead.internal_notes        = notes
                        lead.delay_followup_due_at = (
                            timezone.now() + timedelta(hours=DELAY_SECOND_TOUCH_HOURS)
                        )
                        lead.save(update_fields=[
                            'internal_notes', 'delay_followup_due_at',
                        ])
                        lead.add_conversation_message('assistant', f'[DELAY REACTIVATION] {message}')

                        channels = []
                        if wa_ok:    channels.append('WhatsApp')
                        if email_ok: channels.append('email')
                        self.stdout.write(self.style.SUCCESS(
                            f'✅ Reactivated lead {lead.id} via {" + ".join(channels)} '
                            f'— last-check email queued in {DELAY_SECOND_TOUCH_HOURS}h'
                        ))
                    else:
                        # WhatsApp succeeded but no email available — single-shot path.
                        # Hand the plumber the lead too: with no email the automated
                        # sequence ends here, so a human should follow up on the
                        # agreed date.
                        try:
                            from bot.plumber_notifications import send_plumber_followup_alert
                            send_plumber_followup_alert(
                                lead, reason='no_email_followup',
                                follow_up_date_str=now_local.strftime('%A %d %B'),
                            )
                        except Exception:
                            logger.exception(
                                'Plumber follow-up alert failed for lead %s', lead.id
                            )
                        lead.is_delayed = False
                        notes = _re.sub(r'\[DELAY_SIGNAL\][^\n]*\n?', '', notes).strip()
                        lead.internal_notes = notes
                        lead.save(update_fields=['is_delayed', 'internal_notes'])
                        lead.add_conversation_message('assistant', f'[DELAY REACTIVATION] {message}')
                        self.stdout.write(self.style.SUCCESS(
                            f'✅ Reactivated lead {lead.id} via WhatsApp (no email on file) '
                            f'— plumber alerted to follow up'
                        ))
                else:
                    # All channels failed — retry tomorrow without spamming
                    lead.delay_followup_due_at = timezone.now() + timedelta(hours=24)
                    lead.save(update_fields=['delay_followup_due_at'])
                    self.stdout.write(self.style.ERROR(
                        f'❌ Lead {lead.id} — all channels failed, rescheduled for tomorrow'
                    ))

            except Exception as exc:
                logger.error(f'Error reactivating delayed lead {lead.id}: {exc}')
                self.stdout.write(self.style.ERROR(f'❌ Delayed lead {lead.id}: {exc}'))

    # ─── Job-date ladder (handoff brief Rule 1, owner 2026-09-21) ───────────

    def _process_job_date_ladder(self, now_local, dry_run):
        """Walk every lead armed with a job date more than a week out.

        WHAT: at job - 7 and job - 3 a touch to the lead (email when we have
        one, else WhatsApp when the free window is open), and at job - 2, if
        they have not answered, the plumber's phone-call brief.
        WHY here: it is the cron that already owns delayed leads and their
        channel rules; `bot/job_date_ladder.py` holds the dates, state and copy.
        HOW: one lead per try, so one bad row never stops the run. The state
        guard (`_exclude_suppressed_states`) runs at the query and
        `lead_is_suppressed` again right before each send, because a lead can be
        booked or switched off between two ticks.
        """
        from bot import job_date_ladder as ladder

        leads = (
            Appointment.objects.real()
            .filter(is_lead_active=True,
                    internal_notes__contains=ladder.JOB_DATE_TAG)
            .exclude(chatbot_paused=True)
        )
        leads = self._exclude_suppressed_states(leads)
        for lead in leads:
            try:
                self._tick_job_ladder(lead, now_local, dry_run, ladder)
            except Exception as exc:
                logger.exception('Job-date ladder failed for lead %s', lead.id)
                self.stdout.write(self.style.ERROR(f'❌ Job ladder lead {lead.id}: {exc}'))

    def _ladder_due_at(self, lead, day):
        """The moment a ladder step on `day` goes out: the time the lead named
        for check-backs if they named one, else 09:00, rolled into the contact
        window like every other touch."""
        from bot.out_of_scope_handler import _stored_followup_time
        hour, minute = _stored_followup_time(lead) or (9, 0)
        local = SA_TIMEZONE.localize(datetime(day.year, day.month, day.day, hour, minute))
        return self._next_window_open(local)

    def _tick_job_ladder(self, lead, now_local, dry_run, ladder):
        """One lead, one tick: at most ONE step fires.

        Stop conditions come first, in this order: nothing armed; the lead is
        booked or otherwise suppressed (a booked lead is taken off the ladder,
        a switched-off one is only held, so Resume brings it back); the lead
        has answered since the first touch (the conversation owns them now, so
        no second touch and no call). A cron that was down across two steps
        fires only the LATEST due one, never a burst.
        """
        from bot.post_visit import lead_is_suppressed

        job_day = ladder.job_date(lead)
        at = ladder.step(lead)
        if job_day is None or at >= ladder.STEP_DONE:
            return
        if lead.status == 'confirmed' or lead_is_suppressed(lead):
            if lead.status == 'confirmed' or getattr(lead, 'job_scheduled_datetime', None):
                if not dry_run:
                    ladder.disarm(lead)
            return
        if at > ladder.STEP_FIRST and ladder.replied_since_first_touch(lead):
            if not dry_run:
                ladder.disarm(lead)
            self.stdout.write(f'  ↩️  Job ladder lead {lead.id}: replied, ladder closed')
            return

        today = now_local.date()
        while at < ladder.STEP_CALL and ladder.step_day(job_day, at + 1) <= today:
            at += 1
        if timezone.now() < self._ladder_due_at(lead, ladder.step_day(job_day, at)):
            return

        if at == ladder.STEP_CALL:
            self._ladder_call(lead, job_day, today, dry_run, ladder)
            return
        self._ladder_touch(lead, at, dry_run, ladder)

    def _ladder_touch(self, lead, at, dry_run, ladder):
        """A -7 or -3 touch. Email first (brief: it sidesteps the 24h window and
        any blocking risk), WhatsApp only when there is no email AND the free
        window is open. Held by the four-touch cap like every other loop. The
        step advances even when no channel could carry it, so the plumber's
        call still comes: for a lead with no email and a shut window that call
        is the only way left to reach them."""
        from bot.post_visit import lead_is_suppressed

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'🧪 Would send job-ladder touch {at + 1} to lead {lead.id}'))
            return

        sent_via = ''
        capped = touches_since_last_reply(lead) >= FOLLOWUP_CAP_PER_REPLY
        # The plumber handoff is the LAST text a lead gets until they reply
        # (owner rule, 2026-09-21), so a ladder touch after it is held. The
        # step still advances, so the plumber's call brief (internal, to him)
        # still comes at job - 2.
        handed_off = handoff_sent_since_last_reply(lead)
        if handed_off:
            capped = True
        if not capped and not lead_is_suppressed(lead):
            if getattr(lead, 'customer_email', None):
                from bot.customer_emails import _send
                subject, html = ladder.touch_email(lead, at)
                if _send(lead, subject, html, category='delay'):
                    sent_via = 'email'
                    lead.add_conversation_message(
                        'assistant', f'{ladder.TRANSCRIPT_MARKER} (email) {subject}')
            else:
                allowed, why = self._delay_wa_allowed(lead)
                if allowed:
                    message = dequalify_free_visit(lead, ladder.touch_message(lead, at))
                    clean = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
                    result = get_client_for_tenant(lead.tenant).send_text_message(clean, message)
                    logged = f'{ladder.TRANSCRIPT_MARKER} {message}'
                    lead.add_conversation_message('assistant', logged)
                    # Stamp the WAMID so a quoted reply to this touch resolves.
                    try:
                        wamid = (result or {}).get('messages', [{}])[0].get('id')
                    except Exception:
                        wamid = None
                    lead.attach_message_id('assistant', logged, wamid)
                    sent_via = 'WhatsApp'
                else:
                    logger.info('Job ladder touch for lead %s has no channel: %s',
                                lead.id, why)
        ladder.advance(lead, at + 1, timezone.now())
        self.stdout.write(self.style.SUCCESS(
            f'✅ Job ladder touch {at + 1} for lead {lead.id} '
            f'({sent_via or ("handed off" if handed_off else "capped" if capped else "no channel")})'))

    def _ladder_call(self, lead, job_day, today, dry_run, ladder):
        """job - 2 with no reply: the plumber phones. Skipped once the job date
        itself has passed (a cron back from an outage should not ask him to
        call about a day that is gone). Either way the ladder is finished and
        the lead leaves the delay queue: from here a person owns it."""
        import re as _re

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'🧪 Would send the plumber a call brief for lead {lead.id}'))
            return
        if job_day >= today:
            ok = ladder.send_call_brief(lead)
            self.stdout.write(self.style.SUCCESS(
                f'📞 Call brief for lead {lead.id} '
                f'{"sent to the plumber" if ok else "FAILED to send"}'))
            if not ok:
                return      # retried next tick; the step is not advanced
        ladder.advance(lead, ladder.STEP_DONE, timezone.now())
        lead.is_delayed = False
        lead.internal_notes = _re.sub(r'\[DELAY_SIGNAL\][^\n]*\n?', '',
                                      lead.internal_notes or '').strip()
        lead.save(update_fields=['is_delayed', 'internal_notes'])

    # ─── Delay-email state helpers (internal_notes-backed, no migration) ─────

    def _read_delay_email_count(self, notes: str) -> int:
        """Return the number of delay re-engagement emails sent so far (0, 1, or 2)."""
        m = re.search(r'\[DELAY_EMAIL_COUNT\] (\d+)', notes or '')
        return int(m.group(1)) if m else 0

    def _set_delay_email_count(self, notes: str, new_count: int) -> str:
        """Write/replace [DELAY_EMAIL_COUNT] and [DELAY_EMAIL_LAST] in internal_notes."""
        notes = notes or ''
        notes = re.sub(r'\[DELAY_EMAIL_COUNT\] \d+\n?', '', notes)
        notes = re.sub(r'\[DELAY_EMAIL_LAST\] [^\n]+\n?', '', notes).strip()
        stamp = timezone.now().isoformat()
        return (
            f'{notes}\n[DELAY_EMAIL_COUNT] {new_count}\n[DELAY_EMAIL_LAST] {stamp}'
        ).strip()

    # ─── Eligibility ─────────────────────────────────────────────────────────

    def _exclude_suppressed_states(self, qs):
        """State guard — never proactively message a lead we have already
        decided not to chase. Mirrors the prior pending_upload over-firing fix.

        EXCLUDED_AREA and STOP_REQUESTED were added after prod lead 872: the bot
        correctly declined the job ("Bulawayo is a bit far for our team"), and
        the very next outbound was an AUTO FOLLOW-UP asking which suburb in
        Bulawayo the property was in — the cron re-opening a decision the
        conversation had already closed. The same lead later wrote "Ok send hear
        and please dont say anything more" and received three further pitches.

        The decision has to live on the LEAD, not inside the handler that made
        it, or every other send path re-litigates it.
        """
        return (
            qs.exclude(internal_notes__contains='[HANDED_OFF]')
              .exclude(internal_notes__contains='[PARKED]')
              .exclude(internal_notes__contains='[EXCLUDED_AREA')
              .exclude(internal_notes__contains='[STOP_REQUESTED]')
              .exclude(internal_notes__contains='[OOS_DECLINED]')
              # Staff switched this lead's follow-ups off from the dashboard.
              .exclude(internal_notes__contains='[FOLLOWUPS_OFF]')
        )

    def _get_eligible_leads(self, now_local, force):
        from django.db.models import Q

        # Don't interrupt a live conversation. Two minutes was nowhere near
        # enough — a lead mid-exchange kept getting an auto follow-up dropped on
        # top of the bot's own reply.
        response_window = now_local - timedelta(minutes=FOLLOWUP_LIVE_CONVERSATION_MINUTES)
        #
        leads = (
            Appointment.objects.real()
            .filter(is_lead_active=True, status='pending', is_delayed=False)
            # Quotation-only stubs have a synthetic phone_number (see
            # quotation_templates.py) and no real WhatsApp number, so any send
            # to them 400s. Never proactively message them. Mirrors the same
            # exclusion already applied in quotation_templates.py.
            .exclude(phone_number__startswith='quotation_only_')
            # Email-only leads (process_inbound_emails) have the same kind of
            # synthetic key and no WhatsApp number at all — they are followed up
            # by email, never by a WhatsApp send that would 400.
            .exclude(phone_number__startswith='email_')
            .exclude(followup_stage='completed')
            .exclude(last_customer_response__gte=response_window)
            .exclude(internal_notes__contains='[DELAY_SIGNAL]')
            .exclude(internal_notes__contains='[OOS_PENDING] category=delay_')
            .exclude(chatbot_paused=True)
            # Already-confirmed: an agreed future re-contact date is set, so this
            # lead is parked until then and owned by the delayed-reactivation path.
            # (clear_delayed leaves delay_followup_due_at set — this stops the lead
            # leaking back into normal follow-ups, e.g. conv 378.)
            .exclude(delay_followup_due_at__gt=timezone.now())
        )
        leads = self._exclude_suppressed_states(leads)
        return leads.order_by('last_customer_response', 'created_at')
        
    def _print_eligibility_breakdown(self, now_local, force):
        from django.db.models import Q

        response_window = now_local - timedelta(minutes=FOLLOWUP_LIVE_CONVERSATION_MINUTES)
        plan_block_q = Q(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book'])

        q0 = Appointment.objects.real().filter(is_lead_active=True, status='pending')
        c0 = q0.count()

        q1 = q0.exclude(followup_stage='completed')
        c1 = q1.count()

        q2 = q1.exclude(last_customer_response__gte=response_window)
        c2 = q2.count()

        q3 = q2.exclude(plan_block_q)
        c3 = q3.count()

        self.stdout.write(self.style.WARNING('🔎 Eligibility breakdown'))
        self.stdout.write(f'  active_pending: {c0}')
        self.stdout.write(f'  excluded_completed_stage: {c0 - c1}')
        self.stdout.write(f'  excluded_live_conversation: {c1 - c2}')
        self.stdout.write(f'  excluded_plan_flow: {c2 - c3}')
        self.stdout.write(f'  eligible_after_filters: {c3}')

    # ─── Per-lead processing ──────────────────────────────────────────────────

    def _process_lead(self, lead, now_local, dry_run, force):
        ready, reason = self._is_ready_for_followup(lead, now_local, force)
        if not ready:
            logger.debug(f'Lead {lead.id} skipped: {reason}')
            return {'status': 'skipped'}

        # Don't fire a free-form send when the WhatsApp window is closed — it would
        # bounce with 131047 and we don't use paid templates. This honours both our
        # computed 24h/72h window AND Meta's authoritative verdict (a prior 131047
        # sets the closed flag; it reopens when the customer replies). CTWA leads
        # stay open for 72h here, so FU3/FU4 still go out as intended.
        if not force and not lead.messaging_window_open:
            logger.debug(f'Lead {lead.id} skipped: WhatsApp free-form window closed')
            return {'status': 'skipped', 'reason': 'window_closed'}

        # ...and never a send Meta would CHARGE for. The window being open is
        # permission, not price: once service messages are chargeable, a lead
        # whose free entry point has expired is billable to nudge even though
        # the send is allowed. A follow-up is optional by definition, so it
        # waits for a free window rather than buying one (owner rule,
        # 2026-08-29: keep everything about Meta messaging free).
        if not force and not paid_sends_allowed() and not lead.messaging_is_free:
            logger.info('Lead %s skipped: send would be billable (%s)',
                        lead.id, lead.messaging_cost_reason)
            return {'status': 'skipped', 'reason': 'would_be_billable'}

        max_followups = max_followups_for(lead)
        if lead.followup_count >= max_followups:
            if not dry_run:
                lead.followup_stage = 'completed'
                lead.is_lead_active = False
                lead.lead_marked_inactive_at = timezone.now()
                lead.save()
            self.stdout.write(
                self.style.WARNING(
                    f'✔️  Lead {lead.id} retired after {lead.followup_count} follow-ups'
                )
            )
            return {'status': 'completed'}

        next_q  = self._get_next_question(lead)
        attempt = lead.followup_count + 1   # 1-based attempt number
        # The second touch to a qualified lead who went quiet is the plumber
        # handoff (brief Rule 2), deterministic and never model-written: no
        # model call at the handoff. Everything else takes the owner's script.
        handoff = self._handoff_touch(lead, attempt)
        if handoff:
            next_q = 'plumber_handoff'
            result = {'message': handoff, 'ai_generated': False,
                      'template_fallback': False}
        else:
            result = self._generate_message(lead, next_q, attempt)
        message = dequalify_free_visit(lead, result['message'])

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f'🧪 Would send to {lead.phone_number} '
                    f'[{lead.get_lead_status_display()}] '
                    f'attempt #{attempt}, q={next_q}\n'
                    f'   "{message[:140]}…"'
                )
            )
            return {'status': 'sent', **result}

        clean_phone = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
        if not clean_phone.isdigit():
            # Not a real WhatsApp number (e.g. quotation-only stub) — sending
            # would 400 against the Cloud API. Skip instead of erroring forever.
            logger.debug(f'Lead {lead.id} skipped: non-numeric phone {clean_phone!r}')
            return {'status': 'skipped'}
        get_client_for_tenant(lead.tenant).send_text_message(clean_phone, message)

        lead.last_followup_sent = timezone.now()
        lead.followup_count    += 1
        lead.followup_stage     = self._stage_label(lead)
        lead.save()

        lead.add_conversation_message('assistant', f'[AUTO FOLLOW-UP] {message}')
        if handoff:
            from bot.plumber_link import LINK_SENT_TAG
            if LINK_SENT_TAG not in (lead.internal_notes or ''):
                lead.internal_notes = f'{lead.internal_notes or ""}\n{LINK_SENT_TAG}'.strip()
                lead.save(update_fields=['internal_notes'])

        tag = '🤖 AI' if result['ai_generated'] else '📄 Template'
        self.stdout.write(
            self.style.SUCCESS(
                f'✅ {tag} → {lead.phone_number} '
                f'[{lead.get_lead_status_display()}] '
                f'attempt #{lead.followup_count}'
            )
        )
        return {'status': 'sent', **result}

    # The follow-up number that hands a quiet, qualified lead to the plumber's
    # own line (brief Rule 2: "on the second follow-up"). Counted since their
    # last reply, like every attempt number here.
    # The last touch of a handoff lead's run (module HANDOFF_TOUCHES).
    HANDOFF_ATTEMPT = HANDOFF_TOUCHES

    def _handoff_touch(self, lead, attempt):
        """The plumber-handoff follow-up for a qualified lead gone quiet, or ''.

        WHAT: the second follow-up, for a lead who has given all three fields
        (service type, description, area) and was asked to book, is the
        plumber's wa.me link with the free-online-quote and formal-PDF offer.
        WHY: in the ghosted path (no delay signal) the handoff is the LAST
        resort, so the first touch still gets the owner's script and only the
        second hands over.
        HOW: '' (so the owner's script goes instead) when this is not the
        second attempt, a field is missing ('other' is not a service type), or
        the tenant has no plumber number. It goes out on the second follow-up
        of EVERY silence, even to a lead who has had the link before (owner
        rule, 2026-09-21: a reply resets the count, and the cycle is contextual
        touch then handoff); `_is_ready_for_followup` then stops the run until
        they reply. Pinned by "plumber link: ghosted" in TEST 0.
        """
        if attempt != self.HANDOFF_ATTEMPT:
            return ''
        from bot.plumber_link import handoff_message
        # The same test that cuts this lead's schedule to two (handoff_eligible),
        # so the run length and the message cannot disagree.
        if not handoff_eligible(lead):
            return ''
        return handoff_message(lead)

    # ─── Timing ───────────────────────────────────────────────────────────────

    def _is_ready_for_followup(self, lead, now_local, force):
        """
        Determine whether this lead is due for its next follow-up.

        Every attempt sits at an absolute offset from the moment the lead's
        messaging window opened (their last message), then that moment is moved
        into a time we can actually send: forward past the nightly quiet hours,
        or — when the messaging window would shut before the next opening —
        BACK to the last sendable moment, so the touch goes out this evening
        instead of being stranded until the lead writes again.
        """
        # The ceiling FIRST, counted across every loop that messages this lead
        # rather than off this loop's own counter: a lead who has had their four
        # is not "not due yet", they are finished until they say something, and
        # there is no point working out when a touch we will not send is due.
        spent = touches_since_last_reply(lead)
        if spent >= FOLLOWUP_CAP_PER_REPLY:
            return False, (
                f'{spent} touches since they last messaged '
                f'(cap {FOLLOWUP_CAP_PER_REPLY})'
            )
        why_not = not_a_lead_reason(lead)
        if why_not:
            return False, why_not
        # The plumber handoff is the last automatic touch of a silence (owner
        # rule, 2026-09-21). The follow-ups that used to come after it (a
        # booking question at 12:11 right after the link) stop until the lead
        # replies, which resets the count and starts a new cycle.
        if handoff_sent_since_last_reply(lead):
            return False, 'handed off to the plumber, waiting for their reply'

        due_at = self._scheduled_due_at(lead)
        if due_at is None:
            return False, 'no reference time'

        now = timezone.now()
        if now < due_at:
            return False, f'due at {due_at.astimezone(SA_TIMEZONE):%Y-%m-%d %H:%M} SAST'

        # We spoke to this lead very recently — a follow-up on top of our own
        # message (or on top of theirs, mid-conversation) reads as if nobody is
        # watching the thread. The live conversation IS the follow-up.
        last_out = getattr(lead, 'last_outbound_at', None)
        if last_out:
            # Relaxed on a last call: the window is minutes from shutting, so a
            # slightly closer touch beats no touch at all.
            quiet_hours = (
                LAST_CALL_MIN_GAP_HOURS if self._is_last_call(lead, now)
                else FOLLOWUP_QUIET_AFTER_OUTBOUND_HOURS
            )
            since_out = (now - last_out).total_seconds() / 3600
            if since_out < quiet_hours:
                return False, (
                    f'{since_out:.1f}h since we last messaged them, '
                    f'need {quiet_hours:.1f}h'
                )
        last_in = getattr(lead, 'last_customer_response', None)
        if last_in:
            since_in = (now - last_in).total_seconds() / 60
            if since_in < FOLLOWUP_LIVE_CONVERSATION_MINUTES:
                return False, f'lead messaged {since_in:.0f} min ago — conversation is live'

        # Absolute offsets mean a lead that went quiet mid-schedule (or a cron
        # catching up after an outage) can have two attempts due at once. Keep a
        # minimum gap so we never fire them back to back — tighter on a last
        # call, where the alternative is not sending at all.
        last_sent = getattr(lead, 'last_followup_sent', None)
        if (lead.followup_count or 0) > 0 and last_sent:
            required = self._min_gap_hours(lead, now)
            since_last = (now - last_sent).total_seconds() / 3600
            if since_last < required:
                return False, (
                    f'{since_last:.1f}h since the last follow-up, '
                    f'need {required:.1f}h'
                )
        return True, ''

    def _min_gap_hours(self, lead, now=None):
        """Spacing required before the next touch — relaxed in the final
        sendable stretch, where waiting the full gap means never sending."""
        return (
            LAST_CALL_MIN_GAP_HOURS if self._is_last_call(lead, now)
            else FOLLOWUP_MIN_GAP_HOURS
        )

    # Thin wrappers so the cron, the UI helper and the module-level functions
    # can never drift apart on what a lead's window is.
    def _followup_window_start(self, lead):
        return followup_window_start(lead)

    def _messaging_window_hours(self, lead):
        return messaging_window_hours(lead)

    def _followup_offsets(self, lead):
        """Absolute hours-from-window-open for each attempt this lead will get.

        Thin wrapper: the resolver is module-level so the cron, the UI chip and
        max_followups_for can never drift apart on a lead's schedule.
        """
        return followup_offsets_for(lead)

    def _followup_wait_and_reference(self, lead):
        """Shared timing core — returns (attempt_index, wait_hours, reference) for
        the next follow-up. Single source of truth for both the cron's readiness
        check and the UI's "next follow-up" display, so they can never disagree.

        wait_hours already includes the deterministic jitter; reference is the
        datetime the wait is measured from (None only if the lead has no usable
        timestamps).
        """
        offsets = self._followup_offsets(lead)
        attempt_index = min(lead.followup_count or 0, len(offsets) - 1)
        wait_hours = offsets[attempt_index]
        reference = self._followup_window_start(lead)

        # Human-timing jitter: shift the due moment by a stable per-lead,
        # per-attempt offset (3–57 min) so follow-ups land at natural minutes
        # (e.g. 8:03, 12:48) instead of clustering on the hour, and so leads
        # sharing a reference time don't all fire together. Deterministic, so a
        # lead's due moment doesn't jump around between minute-by-minute checks.
        jitter_hours = self._send_jitter_minutes(lead, attempt_index) / 60.0
        return attempt_index, wait_hours + jitter_hours, reference

    def _scheduled_due_at(self, lead):
        """When the next follow-up should actually GO OUT — the schedule after
        it has been reconciled with the hours we are allowed to send in.

        Three moves, in order:
          1. the raw position in the messaging window (_followup_wait_and_reference)
          2. rolled FORWARD out of the nightly quiet hours, and
          3. if that roll would land after the messaging window has closed,
             pulled BACK to the last moment we can still send.

        Step 3 is the one that matters: without it a touch due at 02:00 on a
        window that shuts at 06:00 waited for the next opening, by which time free-form
        sending was dead — so it sat there and went out only when the lead
        messaged again, arriving as a stale "just checking in" on top of their
        live message. Sending it in the last contact window before the close is
        both timely and deliverable.

        Returns an aware datetime, or None when the lead has no usable
        timestamps to schedule from.
        """
        attempt_index, wait_hours, reference = self._followup_wait_and_reference(lead)
        if reference is None:
            return None

        raw_due = reference + timedelta(hours=wait_hours)
        due = self._next_window_open(raw_due)

        deadline = self._last_sendable_moment(lead)
        if deadline is not None and due > deadline:
            # Last call. Leave the cron room to catch it: the deadline is the
            # very last sendable minute, and the job only runs every few
            # minutes, so aim a grace period earlier.
            due = deadline - timedelta(minutes=LAST_CALL_GRACE_MINUTES)

        # Never before the previous touch — the pull-back must not create a
        # back-to-back pair (the readiness check enforces this too).
        last_sent = getattr(lead, 'last_followup_sent', None)
        if attempt_index > 0 and last_sent:
            floor = last_sent + timedelta(hours=self._min_gap_hours(lead))
            if due < floor:
                due = self._next_window_open(floor)

            # ...but the spacing must not push the touch out of the window
            # entirely. Rolling the floor forward can land AFTER the messaging
            # window shuts, which silently undoes the pull-back above: the
            # touch is scheduled into a contact window the lead will not
            # survive to, so it simply never goes.
            #
            # A lead who wrote in the morning lost their fourth touch this way
            # every time. They have one evening window left before their 24h is
            # up, the full gap does not fit inside it, and the floor pushed the
            # touch to the next midday, hours after they became unreachable.
            #
            # In that stretch the SPACING is what gives, not the touch. That is
            # the trade _min_gap_hours already makes, via LAST_CALL_MIN_GAP_HOURS
            # — it just could not see it from here, because _is_last_call asks
            # whether we are in the final stretch NOW and this is scheduling
            # ahead. So the relaxed gap is applied explicitly.
            if deadline is not None and due > deadline:
                target = deadline - timedelta(minutes=LAST_CALL_GRACE_MINUTES)
                relaxed = last_sent + timedelta(hours=LAST_CALL_MIN_GAP_HOURS)
                # `target` is the only moment we may move it to. The deadline
                # is by definition the last sendable minute, so it sits inside
                # a contact window and the grace period keeps it there.
                #
                # Deliberately NOT max(target, relaxed): a `relaxed` later than
                # `target` is a moment outside the contact hours, and taking it
                # put touches at 19:47 and 20:32 with the evening window shut
                # at 19:33. Three messages in ninety minutes, two of them out
                # of hours, is worse than one touch missed.
                #
                # So the touch moves only if it still clears the relaxed gap.
                # Otherwise `due` is left where it is, past the deadline, and
                # the readiness check declines it: a missed touch, never a
                # bounced one and never an out-of-hours one.
                if target >= relaxed:
                    due = target
        return due

    def _is_last_call(self, lead, now=None):
        """True when we are in the final stretch of sendable time before this
        lead's messaging window shuts. A touch that is merely 'not due yet' but
        cannot survive the night is better sent now than never — after the
        window closes it can only reach them once they message again, arriving
        as a stale nudge on top of their live message.
        """
        deadline = self._last_sendable_moment(lead)
        if deadline is None:
            return False
        now = now or timezone.now()
        if not getattr(lead, 'messaging_window_open', True):
            return False
        return now >= deadline - timedelta(minutes=LAST_CALL_GRACE_MINUTES)

    def _last_sendable_moment(self, lead):
        """The last instant we could still send this lead a free-form message:
        inside the contact hours AND before their messaging window shuts (with
        the safety margin). None when the window is unknown."""
        closes = getattr(lead, 'messaging_window_closes_at', None)
        if closes is None:
            return None
        return self._window_moment_before(
            closes - timedelta(hours=FOLLOWUP_WINDOW_MARGIN_HOURS)
        )

    def _window_moment_before(self, dt):
        """Latest moment <= dt that falls inside a contact window (SAST).

        The mirror of _next_window_open: where that rolls a due time forward to
        the next opening, this walks backwards to the last minute we were still
        allowed to send. Returns None if no contact window precedes dt.
        """
        if not CONTACT_WINDOWS:
            return dt
        local = dt.astimezone(SA_TIMEZONE)
        for _ in range(8):  # safety bound: at most a week of day rolls
            mins = local.hour * 60 + local.minute
            closes_today = []
            for oh, om, ch, cm in CONTACT_WINDOWS:
                if (oh * 60 + om) <= mins < (ch * 60 + cm):
                    return local  # already inside a window
                if mins >= (ch * 60 + cm):
                    # Window shut earlier today — its last sendable minute.
                    closes_today.append(
                        local.replace(hour=ch, minute=cm, second=0, microsecond=0)
                        - timedelta(minutes=1)
                    )
            if closes_today:
                return max(closes_today)
            # Before every window today → step back to the end of yesterday.
            local = (local - timedelta(days=1)).replace(
                hour=23, minute=59, second=0, microsecond=0
            )
        return None

    def next_followup_due_at(self, lead):
        """Schedule info for the NEXT automatic follow-up, for UI display.

        Returns a dict {attempt, max, due_at, is_ctwa} or None when the lead is
        not in the auto follow-up flow (retired, completed, booked, or paused).
        Uses the same timing core as the cron, so the displayed time matches what
        will actually be sent.
        """
        if not lead.is_lead_active or lead.status != 'pending':
            return None
        if lead.followup_stage == 'completed':
            return None
        # Handed off: nothing more is due until they reply (the cron's own
        # rule in _is_ready_for_followup), so the UI must not show a next send.
        if handoff_sent_since_last_reply(lead):
            return None
        max_fu = max_followups_for(lead)
        if (lead.followup_count or 0) >= max_fu:
            return None

        attempt_index, _wait_hours, reference = self._followup_wait_and_reference(lead)
        due_at = self._scheduled_due_at(lead)
        if reference is None or due_at is None:
            return None

        now = timezone.now()
        # The cron sends at _scheduled_due_at (already reconciled with the
        # contact hours and the messaging-window close), so show exactly that —
        # clamped forward only so an overdue lead doesn't display a past time.
        send_at = due_at if due_at > now else self._next_window_open(now)
        return {
            'attempt': attempt_index + 1,
            'max': max_fu,
            'due_at': send_at,
            'overdue': due_at <= now,  # due already — sends on the next in-window cycle
            'is_ctwa': self._is_ctwa_lead(lead),
        }

    def _next_window_open(self, dt):
        """Earliest moment >= dt that falls inside a contact window (SAST).

        The UI's "next send" time must reflect that follow-ups only go out during
        CONTACT_WINDOWS — a due moment outside the window rolls forward to the next
        opening.
        """
        if not CONTACT_WINDOWS:
            return dt
        local = dt.astimezone(SA_TIMEZONE)
        for _ in range(8):  # safety bound: at most a week of day rolls
            mins = local.hour * 60 + local.minute
            opens_today = []
            for oh, om, ch, cm in CONTACT_WINDOWS:
                if (oh * 60 + om) <= mins < (ch * 60 + cm):
                    return local  # already inside a window
                if mins < (oh * 60 + om):
                    opens_today.append(
                        local.replace(hour=oh, minute=om, second=0, microsecond=0)
                    )
            if opens_today:
                return min(opens_today)
            # Past every window today → jump to the start of the next day and retry.
            local = (local + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        return local

    @staticmethod
    def _is_ctwa_lead(lead):
        """True if the lead originated from a Click-to-WhatsApp ad (has a referral
        entry time). These get the longer 72h CTWA follow-up cadence."""
        return is_ctwa_lead(lead)

    @staticmethod
    def _send_jitter_minutes(lead, attempt_index):
        """Deterministic 3–57 minute offset for a given lead+attempt.

        Stable across cron runs (no salted hash) so the computed due time is
        identical every minute the cron checks — the lead simply crosses the
        threshold once, at a natural-looking minute.
        """
        seed = (lead.id * 2654435761 + attempt_index * 40503) & 0xFFFFFFFF
        return 3 + (seed % 55)

    def _stage_label(self, lead):
        labels = ['day_1', 'day_3', 'week_1', 'week_2', 'month_1', 'completed']
        idx    = min(lead.followup_count, len(labels) - 1)
        return labels[idx]

    def _in_contact_window(self, now_local):
        mins = now_local.hour * 60 + now_local.minute
        return any(
            (oh * 60 + om) <= mins < (ch * 60 + cm)
            for oh, om, ch, cm in CONTACT_WINDOWS
        )

    # ─── Next question ────────────────────────────────────────────────────────

    def _get_next_question(self, lead):
        # An unanswered price tie-down is the open question, whatever fields
        # are missing: jumping to the area left the tie-down hanging and the
        # model asked both.
        if pending_price_tiedown(lead) is not None:
            return 'price_tiedown'
        # A description answers the service question, exactly as it does in
        # the chat (`_job_is_known`): a lead who said "tub" is not asked which
        # room it is.
        if not lead.project_type and not lead.project_description:
            return 'service_type'
        if not lead.project_description:
            return 'project_description'
        if not lead.customer_area:
            return 'area'
        if not lead.scheduled_datetime:
            return 'availability'
        return 'complete'

    # ─── Conversation context helpers ────────────────────────────────────────

    def _last_bot_question(self, lead):
        history = lead.conversation_history or []
        skip_prefixes = (
            '[AUTO FOLLOW-UP]', '[AUTOMATIC FOLLOW-UP]',
            '[MANUAL FOLLOW-UP]', '[BULK MANUAL FOLLOW-UP]',
            'APPOINTMENT CONFIRMED', 'NEW APPOINTMENT BOOKED',
            'PLAN RECEIVED', '📋', '🚨',
        )
        for msg in reversed(history):
            if msg.get('role') != 'assistant':
                continue
            content = (msg.get('content') or '').strip()
            for prefix in ('[AUTO FOLLOW-UP] ', '[AUTOMATIC FOLLOW-UP] ',
                           '[MANUAL FOLLOW-UP] ', '[BULK MANUAL FOLLOW-UP] '):
                if content.startswith(prefix):
                    content = content[len(prefix):]
            if any(content.startswith(p) for p in skip_prefixes):
                continue
            if '[Sent ' in content or '[MEDIA]' in content:
                continue
            if '?' not in content:
                continue
            return content[:600]
        return None

    def _elapsed_description(self, lead):
        reference = lead.last_customer_response or lead.created_at
        h = (timezone.now() - reference).total_seconds() / 3600
        if h < 30:   return 'earlier today'
        if h < 54:   return 'yesterday'
        if h < 120:  return 'a couple of days ago'
        if h < 240:  return 'a few days ago'
        if h < 500:  return 'last week'
        return 'a while back'

    def _service_label(self, lead):
        mapping = {
            'bathroom_renovation':       'bathroom renovation',
            'kitchen_renovation':        'kitchen renovation',
            'new_plumbing_installation': 'new plumbing installation',
        }
        # Rows written before the extraction fix hold the display label
        # ("Bathroom Renovation"), so normalise before the lookup.
        key = (lead.project_type or '').strip().lower().replace('&', 'and').replace(' ', '_')
        return mapping.get(key, 'plumbing work')

    # ─── Message generation ───────────────────────────────────────────────────

    def _generate_message(self, lead, next_question, attempt):
        last_question = self._last_bot_question(lead)
        if DEEPSEEK_API_KEY:
            try:
                return self._ai_message(lead, next_question, attempt, last_question)
            except Exception as exc:
                logger.warning(f'AI generation failed for lead {lead.id}: {exc}')
        return self._template_message(lead, next_question, attempt)

    # ─── AI message ──────────────────────────────────────────────────────────

    def _already_collected_summary(self, lead) -> str:
        """Return a bullet list of fields already saved so the AI doesn't re-ask them."""
        lines = []
        if lead.project_type:
            lines.append(f"- Service type: {self._service_label(lead)}")
        if lead.project_description:
            lines.append(f"- Project description: {lead.project_description[:120]}")
        if lead.customer_area:
            lines.append(f"- Area: {lead.customer_area}")
        if lead.scheduled_datetime:
            lines.append(f"- Appointment date/time: already set")
        return "\n".join(lines) if lines else "Nothing collected yet"

    def _recent_conversation_snippet(self, lead, max_turns: int = 4) -> str:
        """Return the last N non-system conversation turns as a readable string."""
        history = lead.conversation_history or []
        skip_prefixes = (
            '[AUTO FOLLOW-UP]', '[AUTOMATIC FOLLOW-UP]',
            '[MANUAL FOLLOW-UP]', '[BULK MANUAL FOLLOW-UP]',
            '[FILE UPLOADED]', '[VIDEO UPLOADED]', '[Sent ',
            'APPOINTMENT CONFIRMED', 'NEW APPOINTMENT BOOKED',
        )
        turns = []
        for msg in reversed(history):
            content = (msg.get('content') or '').strip()
            if any(content.startswith(p) for p in skip_prefixes):
                continue
            role = 'Customer' if msg.get('role') == 'user' else 'Bot'
            turns.append(f"{role}: {content[:200]}")
            if len(turns) >= max_turns * 2:
                break
        if not turns:
            return 'No prior conversation'
        return '\n'.join(reversed(turns))

    def _ai_message(self, lead, next_question, attempt, last_question):
        service  = self._service_label(lead)
        time_ref = self._elapsed_description(lead)
        area     = lead.customer_area or ''

        template_result = self._template_message(lead, next_question, attempt)
        template_text   = template_result['message']

        already_collected = self._already_collected_summary(lead)
        recent_convo      = self._recent_conversation_snippet(lead)

        if next_question == 'price_tiedown':
            # The tie-down IS the question. Handing the model the whole price
            # reply to "rephrase" is how prices, the disclaimer and a second
            # question ended up in one touch (barmak 1158, 1161).
            question_block = (
                'Our price message is still waiting on them. Ask ONLY whether '
                'the price sounds worth it to them, in the words of the base '
                'template. Do NOT repeat any price, figure or disclaimer.'
            )
        elif next_question == 'complete':
            question_block = (
                'We have everything we need. Tell them we are ready to lock in their '
                'appointment the moment they confirm — make it feel effortless to say yes.'
            )
        elif next_question == 'service_type' and ad_subject(lead):
            # The template names the ad they clicked. Rephrasing our last
            # question instead handed the model "a bathroom, a kitchen, or a
            # new installation?" to reword, and it asked that again (1165).
            question_block = ''
        elif last_question and attempt <= 3:
            question_block = (
                f'The last question we asked (unanswered) was:\n"""\n{last_question}\n"""\n\n'
                f'Rephrase it with completely different wording. '
                f'Same information needed, fresh phrasing. '
                f'Never hint that you already asked this.'
            )
        else:
            question_block = ''

        length_instruction = (
            '2 to 4 sentences total.' if attempt <= 3
            else '1 to 2 sentences only — keep it short and human.'
        )

        ad = ad_subject(lead)
        shona = lead_writes_shona(lead)

        prompt = f"""You are writing a WhatsApp follow-up message for {business_name_for(lead)} — a professional plumbing company in Zimbabwe.

LEAD CONTEXT:
- Interest: {service}
- How they found us: {f'they tapped our Facebook ad about a {ad}, so the {ad} is what they came for' if ad else 'not an ad we can read'}
- Language: {'they wrote in SHONA, so write the whole message in Shona' if shona else 'English'}
- Area: {area or 'not yet shared'}
- Last heard from them: {time_ref}
- This is follow-up attempt #{attempt} of {max_followups_for(lead)} (spread across {'three days — they came from a Facebook ad, so the window is 72 hours' if is_ctwa_lead(lead) else 'the 24 hours since they last messaged'})

ALREADY COLLECTED (do NOT ask for any of these again):
{already_collected}

RECENT CONVERSATION (last few turns — use this to avoid repeating questions already answered):
{recent_convo}

BASE TEMPLATE (your starting point — do not stray far from this):
\"\"\"
{template_text}
\"\"\"

{"QUESTION TO EMBED (rephrase naturally into the message):" + chr(10) + question_block if question_block else "Use the base template's question as-is or rephrase it very lightly."}

RULES — every single one must be followed:
1. Stay close to the base template — same intent, same question, same tone
2. You may lightly rephrase for naturalness but do not invent new angles or content
3. Open with {'"Mhoro,"' if shona else '"Hi there,"'} — we do not have their name, never use one
4. NEVER ask for the customer's name
5. Ask the base template's question(s) and NO others. Never restate prices, figures or disclaimers from the conversation, and never add a closing line the template does not have. NEVER ask for something already listed under ALREADY COLLECTED
6. {length_instruction}
7. {'Shona, as the customer wrote; keep plumbing words they would use in English (tub, shower, geyser) in English' if shona else 'Zimbabwean English (e.g. "sorted" not "handled", "keen" not "excited")'}
8. Zero markdown, zero bold, zero bullet points
9. No emojis, not one, on any attempt
10. Never use a dash as punctuation: no em dashes, no en dashes, no ' - ' between clauses. Use a comma, a full stop or a new sentence. Speak as the business: always 'we' ('we will come and have a look', 'once we see the space'), never 'the plumber' or 'our plumber'. Hyphens inside words are fine (on-site, all-in, wall-hung).
10. Never say: "just checking in", "following up", "I noticed you haven't replied", "hope you're well", "touching base"
11. Sound like a real person texting, not a marketing email

Output ONLY the message text. No labels, no quotes around it, no explanation."""

        from bot.services.clients import deepseek_call
        raw = deepseek_call(
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You write short WhatsApp messages based on provided templates. '
                        'Stay faithful to the template. Sound like a real person. '
                        'Never use or ask for the customer name — open with "Hi there,".'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            # Temperature stays low here, unlike the other customer-facing
            # generators: rule 1 above is "stay close to the base template", and
            # this is the one path where drifting off the approved copy matters
            # more than sounding fresh. top_p / frequency_penalty still apply —
            # one lead gets four to six of these, so reusing the same phrasing
            # across the sequence is the failure mode worth spending on.
            temperature=0.4,
            top_p=0.9,
            frequency_penalty=0.3,
            max_tokens=300,
        )

        from bot.utils import strip_emojis, strip_dashes
        message = fit_to_template(
            strip_dashes(strip_emojis(raw.strip().replace('**', '').replace('__', ''))),
            template_text)

        # Guard: if DeepSeek returned something too short to be a real follow-up,
        # fall back to the template so we never send a bare "Hi" or empty string.
        if len(message) < 20:
            logger.warning(
                f'AI follow-up too short ({len(message)} chars) for lead {lead.id} '
                f'— falling back to template'
            )
            return self._template_message(lead, next_question, attempt)

        # The fence (bot/copy_fence.py). The model REWORDS the owner's script; it
        # may not ADD to it. Until this, the rewrite was held to the script's
        # question count (fit_to_template) and a 20-character floor, and nothing
        # else, on a path with sampled drift of about 1 in 5: it could name a
        # price, promise something free or discounted, or offer a day and time
        # nobody had checked the diary for. Anything in the rewrite that is not
        # in the script (a figure, a promise word, a day word, a clock time), or
        # a rewrite 60% longer than the script, sends the owner's script instead,
        # which is always correct on its own. A day or price in a follow-up must
        # come from code, never from the model's reading of the conversation.
        from bot.copy_fence import fence_holds
        fence_ok, fence_why = fence_holds(message, template_text,
                                          check_slots=True, max_growth=1.6)
        if not fence_ok:
            logger.warning(
                f'AI follow-up rejected by the fence ({fence_why}) for lead '
                f'{lead.id} — sending the owner script instead'
            )
            return self._template_message(lead, next_question, attempt)

        logger.info(
            f'AI follow-up | lead={lead.id} attempt={attempt} '
            f'q={next_question} '
            f'rephrase={"yes" if last_question and attempt <= 3 else "no"}'
        )
        return {'message': message, 'ai_generated': True, 'template_fallback': False}

    # ─── Template fallback ────────────────────────────────────────────────────

    def _template_message(self, lead, next_question, attempt):
        """The offline fallback, and it has to be as CONTEXTUAL as the AI path.

        Every touch names the lead and names their job in their own words
        (`lead_handoff.job_phrase`, the same resolver the plumber's draft reads),
        because a follow-up that could have been sent to anybody tells the lead
        exactly how closely we are reading. "Still looking for a plumber?" was
        the fourth and last thing some leads ever heard from us.

        Four attempts, getting shorter, never emptier:
          1 - the job, said back, and the one thing outstanding
          2 - the same ask from a different angle
          3 - short
          4 - shortest, and STILL names the job

        Two things are deliberately gone from this bank:

        * FABRICATED SCARCITY. "We're getting booked up this week" and "we're
          getting tight on slots this week" appeared in four of these and were
          true of nothing: the cron has no idea what the diary looks like. The
          sales rules forbid invented urgency outright, and it is the opposite of
          contextual - a line that would be identical for every lead on earth.
        * TENANT CLAIMS. "We price the job upfront", "the price is fixed once we
          confirm" and "locking in a slot costs nothing" are one business's USPs
          and one business's visit policy, asserted into every tenant's copy.
        """
        from bot.lead_handoff import job_phrase

        # Never empty: _service_label falls back to 'plumbing work', so the copy
        # below can always finish its sentence.
        job = job_phrase(lead) or self._service_label(lead)
        # Their own words, unless their words are a paragraph: "we can come and
        # see the place for the renovation of small bathroom on a farm in
        # Madziva; install sink, toilet, freestanding bath, shower and give you
        # an exact price" is what the whole description does to a sentence.
        if len(job.split()) > 6:
            job = self._service_label(lead)
        name = (getattr(lead, 'customer_name', '') or '').strip()
        hi = f'Hi {name}, ' if name else 'Hi there, '
        area = f' in {lead.customer_area}' if lead.customer_area else ''

        # The price tie-down still waiting on them (see pending_price_tiedown):
        # the same easy yes, named for the thing we priced, never a new ask.
        noun = pending_price_tiedown(lead) or ''
        from bot.views.plumbot.response_mixin import ResponseMixin
        tiedown = ResponseMixin._price_tiedown(
            _Subjectless(), 'english', subject=noun or None)
        keen_on = noun or 'going ahead'

        # {service} in the owner's script: the service TYPE in words, as it was
        # in April ("your bathroom renovation slot"), never the lead's free text.
        from bot.models import Appointment as _Appt
        _type_key = ((lead.project_type or '').strip().lower()
                     .replace('&', 'and').replace(' ', '_'))
        service = ((dict(_Appt.PROJECT_TYPE_CHOICES).get(_type_key, '')
                    if _type_key not in ('', 'other') else '').lower()
                   or 'plumbing project')
        # "around Harare" is the TENANT's own city, and nothing when it has
        # none on file (absent means omit, never borrow Homebase's).
        try:
            from bot.tenant_config import get_config
            _city = get_config(getattr(lead, 'tenant', None)).location_city
        except Exception:
            _city = ''
        recent_where = area or (f' around {_city}' if _city else '')

        # They clicked an ad about a bathroom (or a kitchen): that IS the
        # service, so ask which part of it, never which room.
        ad = ad_subject(lead)
        ad_parts = {'bathroom': 'like the tub or the shower',
                    'kitchen': 'like the sink'}.get(ad, '')

        templates = {
            'price_tiedown': [
                f"{hi}just on the prices we sent{' for ' + noun if noun else ''}. {tiedown}",
                f"{hi}happy to go through the prices if anything was unclear. {tiedown}",
                f"{hi}still keen on {keen_on}?",
                f"Still keen on {keen_on}?",
            ],
            # -- THE OWNER'S SCRIPT (restored 2026-09-19) ----------------------
            # service_type, area, availability and complete are the owner's own
            # April 2026 copy, word for word apart from two mechanical changes:
            # " // " became a blank line, and each dash became the full stop or
            # comma it stood for (dashes are banned in customer copy, and the
            # automated pass of 2026-09-02 mangled exactly these lines). The
            # owner chose this over the 2026-09-09 contextual rewrite knowingly,
            # the "booking up" lines and "we price the job upfront" included.
            # Do not reword it; change it only when the owner asks. Pinned by
            # the `owner script` cases in TEST 0.
            'service_type': [
                (
                    f"{hi}what made you reach out? Most people don't message "
                    f"unless something's actually bothering them about their "
                    f"space.\n\nIs it a bathroom, kitchen, or new installation "
                    f"you're after?"
                ),
                (
                    "Hey! Just so I can point you in the right direction, are "
                    "you looking at a bathroom renovation, kitchen reno, or a "
                    "new installation?\n\nWe price the job upfront so you know "
                    "exactly what you're paying before anything starts."
                ),
                (
                    "We're getting booked up this week. If you're still keen, "
                    "which service were you after? Bathroom, kitchen, or new "
                    "plumbing installation?"
                ),
                (
                    "Still looking for a plumber?"
                ),
            ],
            'ad_service': [
                (
                    f"{hi}you got in touch from our {ad} ad. Is it the whole "
                    f"{ad} you want redone, or one thing in it, {ad_parts}?"
                ),
                (
                    f"{hi}for your {ad}, are you after a full redo or just one "
                    f"or two things changed?"
                ),
                (
                    f"{hi}still keen to get the {ad} sorted?"
                ),
                (
                    f"Still keen on the {ad}?"
                ),
            ],
            'project_description': [
                (
                    f"{hi}about the {job}{area}. What exactly needs doing, so I "
                    f"can get it priced properly for you?"
                ),
                (
                    f"{hi}the more you can tell me about the {job}, the closer "
                    f"the price will be. What needs doing?"
                ),
                (
                    f"{hi}what is the main thing you need sorted with the {job}?"
                ),
                (
                    f"What needs doing with the {job}?"
                ),
            ],
            'area': [
                (
                    f"{hi}I just need your area to finish the booking. Which "
                    f"suburb are you based in?"
                ),
                (
                    f"{hi}we've done a number of renovations{recent_where} "
                    f"recently, just need your suburb to match you with the "
                    f"right team."
                ),
                (
                    "Almost done. We're booking up this week. Which suburb are "
                    "you in so we can lock in your slot?"
                ),
                (
                    "Which area are you in?"
                ),
            ],
            'availability': [
                (
                    f"{hi}what day works best for the free site visit? We have "
                    f"slots this week and next."
                ),
                (
                    f"{hi}locking in a slot costs nothing and you can always "
                    f"reschedule. Would tomorrow or later this week work for "
                    f"the visit?"
                ),
                (
                    "We're getting tight on slots this week. Which day works "
                    "for the site visit?"
                ),
                (
                    "Want to lock in a time?"
                ),
            ],
            'complete': [
                (
                    f"{hi}everything's set on our end for your {service}. Just "
                    f"say the word and I'll confirm your slot."
                ),
                (
                    f"{hi}your {service} slot is ready. The price is fixed once "
                    f"we confirm. What's the best time to lock it in?"
                ),
                (
                    f"We're booking up. Shall I lock in your {service} slot?"
                ),
                (
                    f"Still want to get the {service} sorted?"
                ),
            ],
        }

        if next_question == 'service_type' and ad:
            next_question = 'ad_service'
        options = templates.get(next_question, templates['complete'])
        idx = min(attempt - 1, len(options) - 1)
        message = options[idx]

        return {'message': message, 'ai_generated': False, 'template_fallback': True}

    # ─── Utility ──────────────────────────────────────────────────────────────

    def _clean_phone(self, phone):
        return phone.replace('whatsapp:', '').replace('+', '').strip()