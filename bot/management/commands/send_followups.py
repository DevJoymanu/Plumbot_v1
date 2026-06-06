# bot/management/commands/send_followups.py
#
# HIGH-CONVERTING FOLLOW-UP SYSTEM
#
# 4 follow-ups within 24 hours:
#   Attempt 1 → 0h  (first message, sent as soon as lead goes cold)
#   Attempt 2 → 6h  after attempt 1
#   Attempt 3 → 12h after attempt 1
#   Attempt 4 → 18h after attempt 1
#
# Total spread: 18 hours, all 4 messages land within a single day.

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from bot.models import Appointment, LeadStatus
from bot.whatsapp_cloud_api import whatsapp_api
import os
import re
import logging
import pytz
from urllib.parse import unquote

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')

SA_TIMEZONE = pytz.timezone('Africa/Johannesburg')

# ─── Contact windows (local hour, half-open) ─────────────────────────────────
CONTACT_WINDOWS = [
    (8, 22),    # 8 AM – 9 PM CAT (hour < 22 allows sends up to 21:59)
]

# ─── Intervals (hours since last customer response OR last follow-up) ─────────
# 4 follow-ups spread across ~18 hours:
#   Attempt 1: 2h  after going silent
#   Attempt 2: 6h  after attempt 1
#   Attempt 3: 6h  after attempt 2  (12h total)
#   Attempt 4: 6h  after attempt 3  (18h total)
TIER_INTERVALS = {
    LeadStatus.VERY_HOT: (2, 4, 4, 6),
    LeadStatus.HOT:      (2, 6, 6, 6),
    LeadStatus.WARM:     (3, 6, 6, 6),
    LeadStatus.COLD:     (4, 6, 6, 6),
}

MAX_FOLLOWUPS_PER_STATUS = {
    LeadStatus.VERY_HOT: 4,
    LeadStatus.HOT:      4,
    LeadStatus.WARM:     4,
    LeadStatus.COLD:     4,
}

# Hours between the first delay re-engagement email (sent on the agreed
# follow-up date) and the second/final "last check" email. Keep this on the
# longer side so we never feel pushy on a cold-but-polite lead.
DELAY_SECOND_TOUCH_HOURS = 96  # 4 days


# ─────────────────────────────────────────────────────────────────────────────
class Command(BaseCommand):
    help = '4 follow-ups within 24 hours — Hormozi timing, value-first messaging'

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
            "Just checking in — roughly when do you think you will be back? Even a ballpark works.",
            "No rush at all. Just need a rough idea so we can set a reminder for you.",
            "Last check-in from us — when would work best to reconnect?",
            "We will leave this with you. Just send us a message whenever you are ready and we will pick up right where we left off.",
        ],
        'delay_confirm': [
            "Just checking — is it okay if we reach out to you on {date}? A quick yes or no is all we need.",
            "Should we put {date} in the diary to follow up with you?",
            "Last one from us — would {date} work for us to check in?",
            "We will leave this with you. Whenever you are ready, just send us a message.",
        ],
        'delay_email': [
            "Just one thing before we go — what email should we send your quote to? Or just say skip.",
            "Happy to hold the quote until you are ready. What email works best? Just say skip if you would rather not.",
            "Last ask on the email — what address should we use? Say skip if you would prefer not to share.",
            "No worries if you would rather not share. We will follow up on WhatsApp on the agreed date.",
        ],
    }

    # Spacing between nudges: 2 h before first, then 6 h between each.
    _DELAY_NUDGE_INTERVALS = (2, 6, 6, 6)

    def _nudge_delay_flow_ghosts(self, now_local, dry_run):
        """
        Sends up to 4 contextual WhatsApp follow-ups within the 24-hour window
        to leads that ghosted at any step of the delay flow:
          - Step 1 (delay_timeframe): asked "roughly when will you be back?"
          - Step 2 (delay_confirm):   asked "is it okay if we reach out on {date}?"
          - Step 3 (delay_email):     asked "what email should we send your quote to?"

        Nudge count and last-sent time are stored in internal_notes so the
        cron can resume correctly across multiple runs.
        """
        now                = timezone.now()
        window_open_cutoff = now - timedelta(hours=23)
        min_wait_cutoff    = now - timedelta(hours=1)

        candidates = (
            Appointment.objects
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

                if nudge_count >= 4:
                    continue

                # Determine reference time and required wait
                interval_hours = self._DELAY_NUDGE_INTERVALS[nudge_count]
                reference      = last_nudge_at if last_nudge_at else lead.last_inbound_at
                if not reference:
                    continue
                elapsed = (now - reference).total_seconds() / 3600
                if elapsed < interval_hours:
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

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(
                        f'🧪 Would send delay nudge #{nudge_count + 1} to lead {lead.id} '
                        f'[{step}]: "{message[:80]}…"'
                    ))
                    continue

                clean = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
                whatsapp_api.send_text_message(clean, message)

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
        "we will leave this with you. Whenever the time is right, just send us a "
        "message and we will pick up right where we left off.",
    ]

    # Days to wait before each parked nudge: 3 days before the first, then 7 more
    # before the second. Spaced over days (not hours) — they asked to be left alone.
    _PARKED_NUDGE_INTERVALS_DAYS = (3, 7)

    # Don't re-engage leads who have been cold for more than this — at that point
    # they are genuinely dormant and a nudge is just spam.
    _PARKED_NUDGE_MAX_AGE_DAYS = 30

    def _nudge_parked_leads(self, now_local, dry_run):
        """
        Gently re-engage leads who soft brushed off ("I'll get back to you") and
        were parked via mark_parked() ([PARKED] tag). Sends up to 2 spaced
        WhatsApp nudges (3 days, then 7 days), then leaves them fully alone.

        Count and last-sent time live in internal_notes so the cron resumes
        across runs. Leads still mid delay-flow ([OOS_PENDING] category=delay_)
        are left to _nudge_delay_flow_ghosts; this only handles parked leads not
        in that flow. Respects the contact window (gated by the caller in
        handle()).
        """
        now = timezone.now()
        window_open_cutoff = now - timedelta(days=self._PARKED_NUDGE_MAX_AGE_DAYS)

        candidates = (
            Appointment.objects
            .filter(
                is_lead_active=True,
                internal_notes__contains='[PARKED]',
                last_inbound_at__gte=window_open_cutoff,
            )
            .exclude(status='confirmed')
            .exclude(chatbot_paused=True)
            .exclude(internal_notes__contains='[HANDED_OFF]')
            .exclude(internal_notes__contains='[OOS_PENDING] category=delay_')
        )

        count = candidates.count()
        if count:
            self.stdout.write(f'🅿️ {count} parked lead(s) eligible for re-engagement nudge')

        for lead in candidates:
            try:
                notes = lead.internal_notes or ''
                nudge_count, last_nudge_at = self._read_parked_nudge_state(notes)

                if nudge_count >= len(self._PARKED_NUDGE_INTERVALS_DAYS):
                    continue

                # The customer replied after our last nudge → they re-engaged;
                # let the live conversation take over and stop nudging.
                if last_nudge_at and lead.last_inbound_at and lead.last_inbound_at > last_nudge_at:
                    continue

                interval_days = self._PARKED_NUDGE_INTERVALS_DAYS[nudge_count]
                reference     = last_nudge_at if last_nudge_at else lead.last_inbound_at
                if not reference:
                    continue
                elapsed_days = (now - reference).total_seconds() / 86400
                if elapsed_days < interval_days:
                    continue

                name = lead.customer_name or ''
                hi   = f'Hi {name}' if name else 'Hi there'
                body = self._PARKED_NUDGE_MESSAGES[nudge_count]
                message = f'{hi}, {body}'

                if dry_run:
                    self.stdout.write(self.style.SUCCESS(
                        f'🧪 Would send parked nudge #{nudge_count + 1} to lead {lead.id}: '
                        f'"{message[:80]}…"'
                    ))
                    continue

                clean = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
                whatsapp_api.send_text_message(clean, message)

                self._write_parked_nudge_state(lead, nudge_count + 1, now)
                lead.add_conversation_message(
                    'assistant', f'[PARKED NUDGE {nudge_count + 1}] {message}'
                )

                self.stdout.write(self.style.SUCCESS(
                    f'✅ Parked nudge #{nudge_count + 1}/'
                    f'{len(self._PARKED_NUDGE_INTERVALS_DAYS)} → lead {lead.id}'
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
            Appointment.objects
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

                # ── Build the WhatsApp message (touch 1 only) ───────────────────
                if is_access_checkin:
                    message = (
                        f"{hi}, just checking in — were you able to sort out access "
                        f"on your side? Happy to lock in a time to come through "
                        f"whenever suits you."
                    )
                else:
                    message = (
                        f"{hi}, hope you're back and settled in. "
                        f'You were looking at {detail} — still keen to move forward? '
                        f"We're ready when you are."
                    )

                if dry_run:
                    label = ('access check-in' if is_access_checkin
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

                if is_access_checkin:
                    # ── Access check-in: single WhatsApp shot ───────────────
                    try:
                        whatsapp_api.send_text_message(clean, message)
                        wa_ok = True
                    except Exception as wa_exc:
                        logger.warning(
                            'Access check-in WhatsApp failed for lead %s: %s',
                            lead.id, wa_exc,
                        )
                    if wa_ok:
                        notes = lead.internal_notes or ''
                        notes = _re.sub(r'\[DELAY_SIGNAL\][^\n]*\n?', '', notes)
                        notes = _re.sub(r'\[DELAY_KIND\] access_checkin\n?', '', notes)
                        notes = _re.sub(r'\[OOS_PENDING\][^\n]*\n?', '', notes).strip()
                        lead.is_delayed     = False
                        lead.internal_notes = notes
                        lead.save(update_fields=['is_delayed', 'internal_notes'])
                        lead.add_conversation_message(
                            'assistant', f'[DELAY ACCESS CHECK-IN] {message}'
                        )
                        self.stdout.write(self.style.SUCCESS(
                            f'✅ Access check-in sent for lead {lead.id} — delay queue cleared'
                        ))
                    else:
                        # Retry tomorrow rather than spamming.
                        lead.delay_followup_due_at = timezone.now() + timedelta(hours=24)
                        lead.save(update_fields=['delay_followup_due_at'])
                        self.stdout.write(self.style.WARNING(
                            f'  ⚠️  Access check-in failed for lead {lead.id} — retry in 24h'
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
                    # ── Touch 1: WhatsApp + (optional) email ────────────────
                    try:
                        whatsapp_api.send_text_message(clean, message)
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
                        # WhatsApp succeeded but no email available — single-shot path
                        lead.is_delayed = False
                        notes = _re.sub(r'\[DELAY_SIGNAL\][^\n]*\n?', '', notes).strip()
                        lead.internal_notes = notes
                        lead.save(update_fields=['is_delayed', 'internal_notes'])
                        lead.add_conversation_message('assistant', f'[DELAY REACTIVATION] {message}')
                        self.stdout.write(self.style.SUCCESS(
                            f'✅ Reactivated lead {lead.id} via WhatsApp (no email on file)'
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
        """State guard — never proactively message a lead that has been handed
        to a human or parked. Mirrors the prior pending_upload over-firing fix."""
        return (
            qs.exclude(internal_notes__contains='[HANDED_OFF]')
              .exclude(internal_notes__contains='[PARKED]')
        )

    def _get_eligible_leads(self, now_local, force):
        from django.db.models import Q

        # Don't interrupt a customer who engaged very recently (2 minutes)
        response_window = now_local - timedelta(minutes=2)  # ← must be defined FIRST
        #
        leads = (
            Appointment.objects
            .filter(is_lead_active=True, status='pending', is_delayed=False)
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

        response_window = now_local - timedelta(minutes=2)
        plan_block_q = Q(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book'])

        q0 = Appointment.objects.filter(is_lead_active=True, status='pending')
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
        self.stdout.write(f'  excluded_recent_response_2min: {c1 - c2}')
        self.stdout.write(f'  excluded_plan_flow: {c2 - c3}')
        self.stdout.write(f'  eligible_after_filters: {c3}')

    # ─── Per-lead processing ──────────────────────────────────────────────────

    def _process_lead(self, lead, now_local, dry_run, force):
        ready, reason = self._is_ready_for_followup(lead, now_local, force)
        if not ready:
            logger.debug(f'Lead {lead.id} skipped: {reason}')
            return {'status': 'skipped'}

        max_followups = MAX_FOLLOWUPS_PER_STATUS.get(lead.lead_status, 4)
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
        message = result['message']

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
        whatsapp_api.send_text_message(clean_phone, message)

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

        Wait times (hours since last activity):
          Attempt 1: TIER_INTERVALS[status][0]
          Attempt 2: TIER_INTERVALS[status][1]  (measured from attempt 1 sent time)
          Attempt 3: TIER_INTERVALS[status][2]  (measured from attempt 2 sent time)
          Attempt 4: TIER_INTERVALS[status][3]  (measured from attempt 3 sent time)

        The reference point shifts after each follow-up:
          - Before any follow-up was sent: reference = last_customer_response or created_at
          - After a follow-up was sent:    reference = last_followup_sent
        """
        attempt_index = min(lead.followup_count, 3)
        intervals = TIER_INTERVALS.get(lead.lead_status, TIER_INTERVALS[LeadStatus.COLD])
        wait_hours = intervals[attempt_index]

        # After the first follow-up, measure from when the LAST follow-up was sent.
        # Before any follow-up, measure from when the customer last responded (or created_at).
        if lead.followup_count > 0 and lead.last_followup_sent:
            reference = lead.last_followup_sent
        else:
            reference = (
                lead.last_customer_response
                or lead.last_followup_sent
                or lead.created_at
            )

        # Human-timing jitter: shift the due moment by a stable per-lead,
        # per-attempt offset (3–57 min) so follow-ups land at natural minutes
        # (e.g. 8:03, 12:48) instead of clustering on the hour, and so leads
        # sharing a reference time don't all fire together. Deterministic, so a
        # lead's due moment doesn't jump around between minute-by-minute checks.
        jitter_hours = self._send_jitter_minutes(lead, attempt_index) / 60.0
        wait_hours += jitter_hours

        elapsed = (timezone.now() - reference).total_seconds() / 3600

        if elapsed < wait_hours:
            return False, f'{elapsed:.1f}h elapsed, need {wait_hours:.1f}h (attempt #{attempt_index + 1})'
        return True, ''

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
        hour = now_local.hour
        return any(s <= hour < e for s, e in CONTACT_WINDOWS)

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
            content = msg.get('content', '').strip()
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

        prompt = f"""You are writing a WhatsApp follow-up message for Homebase Plumbers — a professional plumbing company in Zimbabwe.

LEAD CONTEXT:
- Interest: {service}
- Area: {area or 'not yet shared'}
- Last heard from them: {time_ref}
- This is follow-up attempt #{attempt} of 4 (all within 24 hours)

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
9. At most one emoji — only if it fits naturally. Attempt 4 = no emoji
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
            temperature=0.4,
            max_tokens=300,
        )

        message = raw.strip().replace('**', '').replace('__', '')

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

        templates = {
            'service_type': [
                (
                    f"Hi there, what made you reach out? Most people don't message unless something's "
                    f"actually bothering them about their space.\n\n"
                    f"Is it a bathroom, kitchen, or new installation you're after?"
                ),
                (
                    f"Hey! Just so I can point you in the right direction — are you looking at a "
                    f"bathroom renovation, kitchen reno, or a new installation?\n\n"
                    f"We price the job upfront so you know exactly what you're paying before anything starts."
                ),
                (
                    f"We're getting booked up this week — if you're still keen, which service "
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
                    f"the more accurate we can be with the price — what exactly needs doing?"
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
                    f"Hi there, I just need your area to finish the booking — "
                    f"which suburb are you based in?"
                ),
                (
                    f"Hi there, we've done a number of renovations{area or ' around Harare'} recently — "
                    f"just need your suburb to match you with the right team."
                ),
                (
                    f"Almost done — we're booking up this week. "
                    f"Which suburb are you in so we can lock in your slot?"
                ),
                (
                    f"Which area are you in?"
                ),
            ],
            'availability': [
                (
                    f"Hi there, what day works best for the free site visit — "
                    f"we have slots this week and next."
                ),
                (
                    f"Hi there, locking in a slot costs nothing and you can always reschedule. "
                    f"Would tomorrow or later this week work for the visit?"
                ),
                (
                    f"We're getting tight on slots this week — "
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
                    f"Hi there, your {service} slot is ready — the price is fixed once we confirm. "
                    f"What's the best time to lock it in?"
                ),
                (
                    f"We're booking up — shall I lock in your {service} slot?"
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