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
from datetime import timedelta
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
# Opens at 8:21 and closes at 20:53 (not round o'clock times) so sends don't
# land at obvious bot times; half-open, so the last possible send is 20:52.
CONTACT_WINDOWS = [
    (8, 21, 20, 53),
]

# ─── How many follow-ups ──────────────────────────────────────────────────────
# Four touches per run, for every lead. What a lead's window changes is where
# those four SIT (see followup_offsets_for), never how many there are. The delay
# and parked nudge loops, which have their own fraction lists, use it as their
# target count too.
FOLLOWUP_MIN_COUNT = 4


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
    click-to-WhatsApp ad — those open the extended 72h free-form window."""
    return bool(getattr(lead, 'ctwa_entry_at', None))


def messaging_window_hours(lead) -> float:
    """Hours we actually have to work with: from the window opening to the
    moment free-form sending shuts off (24h standard, up to 72h for an ad
    lead). Falls back to a plain 24h when the lead has no usable timestamp."""
    default = (
        float(getattr(lead, 'CTWA_WINDOW_HOURS', CTWA_WINDOW_HOURS))
        if is_ctwa_lead(lead) else DEFAULT_WINDOW_HOURS
    )
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
        return bands
    fractions = SHORT_WINDOW_FRACTIONS.get(tier, SHORT_WINDOW_FRACTIONS[LeadStatus.COLD])
    return tuple(f * usable for f in fractions)


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

# No two follow-ups back-to-back, even if the cron is catching up after an
# outage or a long nightly pause.
FOLLOWUP_MIN_GAP_HOURS = 1.5

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

# On a last call the usual spacing yields: a touch that must go now or never is
# worth a tighter gap than one with a whole day of window ahead of it.
LAST_CALL_MIN_GAP_HOURS = 0.75

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
        """Absolute hours-from-last-inbound for each delay nudge."""
        usable = max(
            self._messaging_window_hours(lead) - FOLLOWUP_WINDOW_MARGIN_HOURS,
            self._messaging_window_hours(lead) * 0.5,
        )
        return tuple(f * usable for f in self._DELAY_NUDGE_FRACTIONS)

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
        # Widest window any lead can have (72h for a click-to-WhatsApp ad lead) —
        # a hardcoded 23h dropped ad leads out of the nudge flow on day two, half
        # their window unused. The per-lead messaging_window_open check below is
        # what actually decides; this is only a cheap prefilter.
        window_open_cutoff = now - timedelta(hours=Appointment.CTWA_WINDOW_HOURS - 1)
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

                # At least four nudges, bounded by the copy we actually have.
                max_nudges = min(
                    max(FOLLOWUP_MIN_COUNT, len(self._DELAY_NUDGE_FRACTIONS)),
                    len(self._DELAY_NUDGE_MESSAGES[step]),
                )
                if nudge_count >= max_nudges:
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

                # Build message
                name    = lead.customer_name or ''
                hi      = f'Hi {name}' if name else 'Hi there'
                template = self._DELAY_NUDGE_MESSAGES[step][nudge_count]
                if '{date}' in template and not date:
                    # Never render a missing date as the literal word "None" to a
                    # customer. Skip until the stored follow-up date is available.
                    logger.warning(
                        "Delay nudge skipped for lead %s: %s template needs a date "
                        "but none is stored", lead.id, step,
                    )
                    continue
                body     = template.format(date=date) if '{date}' in template else template
                message  = f'{hi}, {body}'
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
        """Absolute hours-from-last-inbound for each parked re-engagement nudge."""
        window = self._messaging_window_hours(lead)
        usable = max(window - FOLLOWUP_WINDOW_MARGIN_HOURS, window * 0.5)
        return tuple(f * usable for f in self._PARKED_NUDGE_FRACTIONS)

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
        )

        count = candidates.count()
        if count:
            self.stdout.write(f'🅿️ {count} parked lead(s) eligible for re-engagement nudge')

        for lead in candidates:
            try:
                notes = lead.internal_notes or ''
                nudge_count, last_nudge_at = self._read_parked_nudge_state(notes)

                # At least four touches, bounded by the copy we have.
                max_nudges = min(
                    max(FOLLOWUP_MIN_COUNT, len(self._PARKED_NUDGE_FRACTIONS)),
                    len(self._PARKED_NUDGE_MESSAGES),
                )
                if nudge_count >= max_nudges:
                    continue

                # A free-form send outside the window bounces with 131047 and
                # flags the lead's window closed, which would block every other
                # send path too. Never attempt one.
                if not lead.messaging_window_open:
                    continue
                # Nor one Meta would charge for — a nudge is optional.
                if not paid_sends_allowed() and not lead.messaging_is_free:
                    continue

                # The customer replied after our last nudge → they re-engaged;
                # let the live conversation take over and stop nudging.
                if last_nudge_at and lead.last_inbound_at and lead.last_inbound_at > last_nudge_at:
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

                name = lead.customer_name or ''
                hi   = f'Hi {name}' if name else 'Hi there'
                body = self._PARKED_NUDGE_MESSAGES[nudge_count]
                message = f'{hi}, {body}'
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
        )
        due = self._exclude_suppressed_states(due)

        count = due.count()
        if count:
            self.stdout.write(f'🔔 {count} delayed lead(s) due for re-engagement')

        for lead in due:
            try:
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
        result  = self._generate_message(lead, next_q, attempt)
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

        tag = '🤖 AI' if result['ai_generated'] else '📄 Template'
        self.stdout.write(
            self.style.SUCCESS(
                f'✅ {tag} → {lead.phone_number} '
                f'[{lead.get_lead_status_display()}] '
                f'attempt #{lead.followup_count}'
            )
        )
        return {'status': 'sent', **result}

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
        window that shuts at 06:00 waited for 08:21, by which time free-form
        sending was dead — so it sat there and went out only when the lead
        messaged again, arriving as a stale "just checking in" on top of their
        live message. Sending it at 20:52 the evening before is both timely and
        deliverable.

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
        if not lead.project_type:
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
        return mapping.get(lead.project_type or '', 'plumbing work')

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

        if next_question == 'complete':
            question_block = (
                'We have everything we need. Tell them we are ready to lock in their '
                'appointment the moment they confirm — make it feel effortless to say yes.'
            )
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

        prompt = f"""You are writing a WhatsApp follow-up message for {business_name_for(lead)} — a professional plumbing company in Zimbabwe.

LEAD CONTEXT:
- Interest: {service}
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
3. Open with "Hi there," — we do not have their name, never use one
4. NEVER ask for the customer's name
5. One question maximum — and NEVER ask for something already listed under ALREADY COLLECTED
6. {length_instruction}
7. Zimbabwean English (e.g. "sorted" not "handled", "keen" not "excited")
8. Zero markdown, zero bold, zero bullet points
9. No emojis, not one, on any attempt
10. Never use a dash as punctuation: no em dashes, no en dashes, no ' - ' between clauses. Use a comma, a full stop or a new sentence. Hyphens inside words are fine (on-site, all-in, wall-hung).
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
        message = strip_dashes(
            strip_emojis(raw.strip().replace('**', '').replace('__', '')))

        # Guard: if DeepSeek returned something too short to be a real follow-up,
        # fall back to the template so we never send a bare "Hi" or empty string.
        if len(message) < 20:
            logger.warning(
                f'AI follow-up too short ({len(message)} chars) for lead {lead.id} '
                f'— falling back to template'
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
        """
        4 attempts, all within 24 hours.
        Attempt 1 — value-led, warm
        Attempt 2 — social proof + casual
        Attempt 3 — soft urgency (we're booking up)
        Attempt 4 — ultra-short 9-word style
        """
        service = self._service_label(lead)
        area    = f' in {lead.customer_area}' if lead.customer_area else ''
        # Where WE work, from this lead's own tenant — the fallback used to be
        # a hardcoded 'around Harare' (Homebase's city) in every tenant's copy.
        from bot.tenant_config import get_config
        _city = get_config(getattr(lead, 'tenant', None)).location_city
        area_or_ours = area or (f' around {_city}' if _city else '')

        templates = {
            'service_type': [
                (
                    f"Hi there, what made you reach out? Most people don't message unless something's "
                    f"actually bothering them about their space.\n\n"
                    f"Is it a bathroom, kitchen, or new installation you're after?"
                ),
                (
                    f"Hey! Just so I can point you in the right direction, are you looking at a "
                    f"bathroom renovation, kitchen reno, or a new installation?\n\n"
                    f"We price the job upfront so you know exactly what you're paying before anything starts."
                ),
                (
                    f"We're getting booked up this week. If you're still keen, which service "
                    f"were you after? Bathroom, kitchen, or new plumbing installation?"
                ),
                (
                    f"Still looking for a plumber?"
                ),
            ],
            'project_description': [
                (
                    f"Hi there, to give you the most accurate quote for your {service}, "
                    f"could you tell me a bit more about the specific work you need done?"
                ),
                (
                    f"Hi there, the more detail you can share about the {service} job, "
                    f"the more accurate we can be with the price. What exactly needs doing?"
                ),
                (
                    f"Hi there, we're booking up this week. "
                    f"What's the main thing you need sorted for the {service}?"
                ),
                (
                    f"What exactly needs doing?"
                ),
            ],
            'area': [
                (
                    f"Hi there, I just need your area to finish the booking. "
                    f"Which suburb are you based in?"
                ),
                (
                    f"Hi there, we've done a number of renovations{area_or_ours} recently — "
                    f"just need your suburb to match you with the right team."
                ),
                (
                    f"Almost done. We're booking up this week, "
                    f"Which suburb are you in so we can lock in your slot?"
                ),
                (
                    f"Which area are you in?"
                ),
            ],
            'availability': [
                (
                    f"Hi there, what day works best for the free site visit? "
                    f"we have slots this week and next."
                ),
                (
                    f"Hi there, locking in a slot costs nothing and you can always reschedule. "
                    f"Would tomorrow or later this week work for the visit?"
                ),
                (
                    f"We're getting tight on slots this week. "
                    f"which day works for the site visit?"
                ),
                (
                    f"Want to lock in a time?"
                ),
            ],
            'complete': [
                (
                    f"Hi there, everything's set on our end for your {service}. "
                    f"Just say the word and I'll confirm your slot."
                ),
                (
                    f"Hi there, your {service} slot is ready. The price is fixed once we confirm, "
                    f"What's the best time to lock it in?"
                ),
                (
                    f"We're booking up, shall I lock in your {service} slot?"
                ),
                (
                    f"Still want to get the {service} sorted?"
                ),
            ],
        }

        options = templates.get(next_question, templates['complete'])
        idx = min(attempt - 1, len(options) - 1)
        message = options[idx]

        return {'message': message, 'ai_generated': False, 'template_fallback': True}

    # ─── Utility ──────────────────────────────────────────────────────────────

    def _clean_phone(self, phone):
        return phone.replace('whatsapp:', '').replace('+', '').strip()