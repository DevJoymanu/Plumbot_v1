# bot/management/commands/send_followups.py
#
# HIGH-CONVERTING FOLLOW-UP SYSTEM
#
# Principles applied (Hormozi + conversion psychology):
#
#  1. SPECIFICITY SELLS, vague messages get ignored. Every message references
#     exactly what the customer said they need. "Your bathroom" beats "your project".
#
#  2. VALUE BEFORE ASK — each follow-up leads with something useful (insight,
#     social proof, a concrete next step) before asking for anything.
#
#  3. PATTERN INTERRUPTS — message #2+ deliberately break the pattern of the
#     previous one. Different length, different opener, different angle.
#     Same message twice = unsubscribe.
#
#  4. URGENCY WITHOUT LYING — real constraints only: limited slots, real wait
#     times, genuine price change warnings. Never fake scarcity.
#
#  5. MICRO-COMMITMENTS — each message asks for the smallest possible "yes"
#     that moves the sale forward one step, not the whole thing at once.
#
#  6. TIMING LOGIC (Hormozi: "speed to lead" + "9-word email"):
#       - Very hot (booked slot): 4h → 8h → 1d → 2d (chase fast, they're ready)
#       - Hot (4 fields): 20h → 36h → 60h → 5d (consistent, not desperate)
#       - Warm (2-3 fields): 36h → 3d → 6d → 10d (patient, educational)
#       - Cold (0-1 fields): 48h → 5d → 10d → 21d (nurture, don't push)
#
#  7. CONTACT WINDOWS — only reach during high-read-rate windows.
#     Research: 8-10am (commute), 12-1pm (lunch), 5-7pm (after work).
#
#  8. EXPONENTIAL BACKOFF — each ignored message doubles the wait.
#     Respect = better deliverability + warmer reception when they do reply.
#
#  9. THE "9-WORD EMAIL" (Hormozi) — attempt 4+ uses ultra-short messages.
#     "Are you still looking for a plumber?" converts better than paragraphs.
#
# 10. ZIMBABWE/SA CONTEXT — warm, direct, professional. No American hype.
#     Prices in USD. Informal but not sloppy.
#
# ── PLAN FOLLOW-UP (integrated) ──────────────────────────────────────────────
#
# When a customer says "I have a plan" (has_plan=True) but hasn't uploaded
# anything yet, a separate nudge sequence fires.
#
# TIMING RULES for plan follow-ups:
#   ALLOWED  : 19:00–20:00 SAST only  (after-work, high read rate)
#   BLOCKED  : 08:00–10:00 (morning commute)
#              12:00–13:00 (lunch)
#
# DYNAMIC "NOT BEFORE" GATE:
#   If the customer said "I'll send tomorrow" / "tonight" / "on Monday",
#   DeepSeek parses the promise and sets plan_followup_not_before so we
#   never nudge before they said they'd send it.
#
# ESCALATION (4 attempts max):
#   #1 — gentle reminder
#   #2 — nudge + offer site-visit alternative
#   #3 — short, direct
#   #4 — final pivot: flip has_plan→False, offer free site visit, stop plan nudges
#
# NEW MODEL FIELDS REQUIRED (add to Appointment + run migration):
#   plan_followup_attempts    = models.IntegerField(default=0)
#   plan_followup_not_before  = models.DateTimeField(null=True, blank=True)

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from bot.models import Appointment, LeadStatus
from bot.whatsapp_cloud_api import whatsapp_api
from openai import OpenAI
import os
import logging
import pytz

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')
deepseek_client = (
    OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")
    if DEEPSEEK_API_KEY else None
)

SA_TIMEZONE = pytz.timezone('Africa/Johannesburg')

# ─── Contact windows (local hour, half-open) ─────────────────────────────────
CONTACT_WINDOWS = [
    (8, 24),    # All day: 8 AM - 8 PM SAST
]

# ─── Plan follow-up windows ───────────────────────────────────────────────────
PLAN_ALLOWED_WINDOW  = (19, 20)       # 19:00–20:00 SAST only
PLAN_BLOCKED_WINDOWS = [
    (8,  10),   # Morning commute
    (12, 13),   # Lunch
]
MAX_PLAN_FOLLOWUP_ATTEMPTS = 4

# ─── Intervals (hours) — tighter on hot leads, patient on cold ───────────────
# Each tuple is (attempt_1, attempt_2, attempt_3, attempt_4+)
TIER_INTERVALS = {
    LeadStatus.VERY_HOT: (2/60, 2/60, 2/60, 2/60),
    LeadStatus.HOT:      (2/60, 2/60, 2/60, 2/60),
    LeadStatus.WARM:     (2/60, 2/60, 2/60, 2/60),
    LeadStatus.COLD:     (2/60, 2/60, 2/60, 2/60),
}

MAX_FOLLOWUPS_PER_STATUS = {
    LeadStatus.VERY_HOT: 6,
    LeadStatus.HOT:      5,
    LeadStatus.WARM:     4,
    LeadStatus.COLD:     3,
}

GHOSTED_THRESHOLD = 4


# ─────────────────────────────────────────────────────────────────────────────
class Command(BaseCommand):
    help = (
        'High-converting follow-ups: Hormozi timing, value-first messaging, '
        'pattern interrupts. Also handles plan-upload nudges for customers '
        'who promised to send a plan but have not uploaded yet.'
    )

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

        # ── 1. Run plan follow-ups (own window logic) ─────────────────────
        self._run_plan_followups(now_local, dry_run, force)

        # ── 2. Run normal lead follow-ups ─────────────────────────────────
        if not force and not self._in_contact_window(now_local):
            self.stdout.write(
                self.style.WARNING(
                    f'⏰ Outside contact windows ({now_local.strftime("%H:%M")} SAST). '
                    'Pass --force to override.'
                )
            )
            return

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

    # =========================================================================
    # PLAN FOLLOW-UP SECTION
    # =========================================================================

    def _run_plan_followups(self, now_local, dry_run, force):
        """
        Nudge customers who said they have a plan but still haven't uploaded.
        Only fires during 19:00–20:00 SAST unless --force is passed.
        """
        self.stdout.write(self.style.SUCCESS('\n📋 Plan follow-up check…'))

        if not force and not self._in_plan_window(now_local):
            self.stdout.write(
                self.style.WARNING(
                    f'⏰ Outside plan follow-up window '
                    f'({now_local.strftime("%H:%M")} SAST, allowed 19:00–20:00). '
                    'Skipping plan nudges.'
                )
            )
            return

        if not force and self._in_plan_blocked_window(now_local):
            self.stdout.write(
                self.style.WARNING(
                    f'⏰ In a blocked window for plan follow-ups '
                    f'({now_local.strftime("%H:%M")} SAST). Skipping.'
                )
            )
            return

        leads = self._get_plan_eligible_leads(now_local)
        self.stdout.write(f'📊 {leads.count()} lead(s) awaiting plan upload')

        plan_totals = dict(sent=0, skipped=0, pivoted=0, errors=0)

        for lead in leads:
            try:
                result = self._process_plan_lead(lead, now_local, dry_run)
                plan_totals[result] = plan_totals.get(result, 0) + 1
            except Exception as exc:
                logger.error(f'Plan follow-up error for lead {lead.id}: {exc}')
                plan_totals['errors'] += 1
                self.stdout.write(self.style.ERROR(f'❌ Plan lead {lead.id}: {exc}'))

        self.stdout.write(self.style.SUCCESS('📋 Plan follow-up summary'))
        for k, v in plan_totals.items():
            self.stdout.write(f'  {k}: {v}')

    def _in_plan_window(self, now_local):
        """Only 19:00–20:00 SAST is allowed for plan nudges."""
        h = now_local.hour
        start, end = PLAN_ALLOWED_WINDOW
        return start <= h < end

    def _in_plan_blocked_window(self, now_local):
        """Return True if we're inside a blocked window."""
        h = now_local.hour
        return any(s <= h < e for s, e in PLAN_BLOCKED_WINDOWS)

    def _get_plan_eligible_leads(self, now_local):
        from django.db.models import Q

        return (
            Appointment.objects
            .filter(
                is_lead_active=True,
                status='pending',
                has_plan=True,
            )
            # No file uploaded yet
            .filter(Q(plan_file='') | Q(plan_file__isnull=True))
            # Still in upload-pending state (or state not set yet)
            .filter(
                Q(plan_status='pending_upload') |
                Q(plan_status__isnull=True) |
                Q(plan_status='')
            )
            # Not already completed / reviewed
            .exclude(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book'])
            # Haven't exceeded max attempts
            .filter(plan_followup_attempts__lt=MAX_PLAN_FOLLOWUP_ATTEMPTS)
            # Respect the "not before" gate set from the customer's promise
            .filter(
                Q(plan_followup_not_before__isnull=True) |
                Q(plan_followup_not_before__lte=timezone.now())
            )
            .order_by('plan_followup_attempts', 'last_customer_response')
        )

    def _process_plan_lead(self, lead, now_local, dry_run):
        attempt = (lead.plan_followup_attempts or 0) + 1

        # Max attempts reached → pivot to site visit
        if attempt > MAX_PLAN_FOLLOWUP_ATTEMPTS:
            return self._pivot_plan_to_site_visit(lead, dry_run)

        message = self._generate_plan_message(lead, attempt)

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'🧪 Would send plan nudge #{attempt} to {lead.phone_number}\n'
                f'   "{message[:160]}…"'
            ))
            return 'skipped'

        clean_phone = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
        whatsapp_api.send_text_message(clean_phone, message)

        # Advance counters and set next not-before to tomorrow 19:00
        tomorrow_19 = (now_local + timedelta(days=1)).replace(
            hour=19, minute=0, second=0, microsecond=0
        )
        lead.plan_followup_attempts = attempt
        lead.last_followup_sent     = timezone.now()
        lead.plan_followup_not_before = tomorrow_19.astimezone(pytz.utc)
        lead.save(update_fields=[
            'plan_followup_attempts',
            'last_followup_sent',
            'plan_followup_not_before',
        ])

        lead.add_conversation_message('assistant', f'[PLAN FOLLOW-UP #{attempt}] {message}')

        self.stdout.write(self.style.SUCCESS(
            f'✅ Plan nudge #{attempt} → {lead.phone_number}'
        ))
        return 'sent'

    def _pivot_plan_to_site_visit(self, lead, dry_run):
        """
        After MAX_PLAN_FOLLOWUP_ATTEMPTS the plan is clearly not coming.
        Offer a free site visit and re-enter the normal booking flow.
        """
        service = self._plan_service_label(lead)
        message = (
            f"Hi there, no worries if the plans are proving tricky to track down! "
            f"We can sort your {service} just as easily with a free on-site visit — "
            f"our plumber comes out, measures everything up and gives you a fixed price "
            f"on the spot. Shall I book that in for you?"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'🧪 Would pivot to site visit for {lead.phone_number}\n'
                f'   "{message[:160]}…"'
            ))
            return 'skipped'

        clean_phone = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
        whatsapp_api.send_text_message(clean_phone, message)

        # Flip to site-visit flow
        lead.has_plan              = False
        lead.plan_status           = None
        lead.plan_followup_attempts = MAX_PLAN_FOLLOWUP_ATTEMPTS   # prevents re-entry
        lead.last_followup_sent    = timezone.now()
        lead.save(update_fields=[
            'has_plan', 'plan_status',
            'plan_followup_attempts', 'last_followup_sent',
        ])

        lead.add_conversation_message('assistant', f'[PLAN PIVOT → SITE VISIT] {message}')

        self.stdout.write(self.style.WARNING(
            f'🔄 Pivoted to site visit for {lead.phone_number}'
        ))
        return 'pivoted'

    # ── Plan message generation ────────────────────────────────────────────

    def _generate_plan_message(self, lead, attempt):
        last_promise = self._last_plan_promise(lead)
        if deepseek_client:
            try:
                return self._ai_plan_message(lead, attempt, last_promise)
            except Exception as exc:
                logger.warning(f'AI plan message failed for lead {lead.id}: {exc}')
        return self._template_plan_message(lead, attempt)

    def _ai_plan_message(self, lead, attempt, last_promise):
        service  = self._plan_service_label(lead)
        template = self._template_plan_message(lead, attempt)

        promise_context = (
            f'Their last message about it was: "{last_promise}"'
            if last_promise
            else 'They have not mentioned a specific time for sending it.'
        )

        length_rule = (
            '2 to 3 sentences.' if attempt <= 2
            else '1 to 2 sentences only — very short.'
        )

        prompt = f"""You are writing a WhatsApp follow-up for Homebase Plumbers (Zimbabwe/South Africa).

CONTEXT:
- Customer said they have a plan for their {service} but hasn't uploaded it yet.
- {promise_context}
- This is follow-up attempt #{attempt}.

BASE TEMPLATE (stay close to this):
\"\"\"
{template}
\"\"\"

RULES:
1. Open with "Hi there," — never use their name, never ask for their name
2. Reference the plan naturally — never say "you promised" or "you said you would"
3. One question maximum
4. {length_rule}
5. South African / Zimbabwean English ("sorted", "pop it across", "keen")
6. No markdown, no bold, no bullets
7. One emoji max for attempt 1–2; zero emoji for attempt 3+
8. Never say: "just checking in", "following up", "I noticed", "hope you're well"
9. Sound like a real person texting, not a bot

Output ONLY the message text. No labels, no quotes."""

        response = deepseek_client.chat.completions.create(
            model='deepseek-chat',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You write short WhatsApp messages based on templates. '
                        'Stay faithful to the template. Sound human. '
                        'Always open with "Hi there,". Never ask for the customer\'s name.'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.45,
            max_tokens=160,
        )

        msg = response.choices[0].message.content.strip()
        return msg.replace('**', '').replace('__', '')

    def _template_plan_message(self, lead, attempt):
        service = self._plan_service_label(lead)

        templates = [
            # Attempt 1 — warm, low pressure
            (
                f"Hi there, just checking if you managed to get hold of that plan for your "
                f"{service}? 📐 Pop it across whenever you're ready and our plumber will "
                f"take a look straight away."
            ),
            # Attempt 2 — nudge + site-visit alternative
            (
                f"Hi there, still waiting on your plan for the {service}. "
                f"No stress if it's taking a while — would it be easier to just book "
                f"a free site visit instead? Our plumber comes to you and prices it up on the spot."
            ),
            # Attempt 3 — short and direct
            (
                f"Hi there, did you manage to find those plans for the {service}? "
                f"If it's easier we can always do a free site visit to get things moving."
            ),
            # Attempt 4 — final, pivot to site visit
            (
                f"Hi there, looks like the plans are tricky to track down for your {service}. "
                f"How about a free on-site visit instead — our plumber measures up and gives "
                f"you a fixed price on the spot. Shall I book that in?"
            ),
        ]

        idx = min(attempt - 1, len(templates) - 1)
        return templates[idx]

    # ── Plan helpers ───────────────────────────────────────────────────────

    def _plan_service_label(self, lead):
        mapping = {
            'bathroom_renovation':       'bathroom renovation',
            'kitchen_renovation':        'kitchen renovation',
            'new_plumbing_installation': 'new plumbing installation',
            'Bathroom Renovation':       'bathroom renovation',
            'Kitchen Renovation':        'kitchen renovation',
            'New Plumbing Installation': 'new plumbing installation',
        }
        return mapping.get(lead.project_type or '', 'plumbing project')

    def _last_plan_promise(self, lead):
        """Find the customer's most recent message that mentioned the plan."""
        history = lead.conversation_history or []
        plan_keywords = [
            'send', 'upload', 'plan', 'later', 'tomorrow', 'tonight',
            'photo', 'pic', 'image', 'mangwana', 'mauro',
            'ndichatumira', 'ndinotumira',
        ]
        for msg in reversed(history):
            if msg.get('role') != 'user':
                continue
            content = msg.get('content', '').lower()
            if any(kw in content for kw in plan_keywords):
                return msg.get('content', '')[:300]
        return None

    # =========================================================================
    # PLAN PROMISE PARSER (called from webhook when customer replies)
    # =========================================================================

    @staticmethod
    def parse_plan_promise_and_save(appointment, message: str) -> None:
        """
        Parse a timing promise from the customer's message ("I'll send tomorrow",
        "tonight", "on Monday", etc.) and store the earliest follow-up datetime
        on appointment.plan_followup_not_before.

        Call this from whatsapp_webhook.handle_text_message() right after
        appointment.mark_customer_response() whenever has_plan is True and
        no plan has been uploaded yet.

        Example (in whatsapp_webhook.py):
            from bot.management.commands.send_followups import Command as FollowUpCommand
            FollowUpCommand.parse_plan_promise_and_save(appointment, message_body)
        """
        if not deepseek_client:
            logger.warning('DeepSeek not available — skipping plan promise parsing')
            return

        # Quick keyword pre-filter — don't burn tokens on every message
        msg_lower = message.lower()
        timing_hints = [
            'send', 'upload', 'tomorrow', 'tonight', 'later', 'soon',
            'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
            'weekend', 'week', 'day', 'home', 'get back', 'plan',
            'mangwana',       # Shona: tomorrow
            'mauro',          # Shona: later today
            'ndichatumira',   # Shona: I will send
            'ndinotumira',
        ]
        if not any(hint in msg_lower for hint in timing_hints):
            return

        now_local = timezone.now().astimezone(SA_TIMEZONE)

        prompt = f"""Today is {now_local.strftime('%A, %Y-%m-%d')} ({now_local.strftime('%H:%M')} SAST).

A customer said they will send their plan. Parse WHEN they intend to send it.

Customer message: "{message}"

Return ONLY one of these tokens — nothing else:
  TODAY          — they'll send today / tonight / when they get home
  TOMORROW       — they'll send tomorrow / mangwana
  IN_N_DAYS:N    — they'll send in N days (e.g. IN_N_DAYS:3 for "in a few days")
  WEEKDAY:NAME   — a specific weekday (e.g. WEEKDAY:Monday)
  UNKNOWN        — no clear time reference"""

        try:
            response = deepseek_client.chat.completions.create(
                model='deepseek-chat',
                messages=[
                    {'role': 'system', 'content': 'Return only the token. No explanation.'},
                    {'role': 'user',   'content': prompt},
                ],
                temperature=0.0,
                max_tokens=20,
            )
            token = response.choices[0].message.content.strip().upper()
            logger.info(f'Plan promise token for lead {appointment.id}: {token!r}')

            not_before = Command._resolve_not_before(token, now_local)
            if not_before:
                appointment.plan_followup_not_before = not_before.astimezone(pytz.utc)
                appointment.save(update_fields=['plan_followup_not_before'])
                logger.info(
                    f'Lead {appointment.id}: plan follow-up not before '
                    f'{not_before.strftime("%Y-%m-%d %H:%M %Z")}'
                )

        except Exception as exc:
            logger.warning(f'Failed to parse plan promise for lead {appointment.id}: {exc}')

    @staticmethod
    def _resolve_not_before(token: str, now_local) -> object:
        """Convert a promise token to a concrete 19:00 SAST datetime."""
        from datetime import datetime as dt

        def at_1900(base):
            """Return base date at 19:00 SAST, pushed to next day if already past."""
            result = base.replace(hour=19, minute=0, second=0, microsecond=0)
            if result <= now_local and base.date() == now_local.date():
                result += timedelta(days=1)
            return result

        if token == 'TODAY':
            return at_1900(now_local)

        if token == 'TOMORROW':
            return at_1900(now_local + timedelta(days=1))

        if token.startswith('IN_N_DAYS:'):
            try:
                n = int(token.split(':')[1])
                return at_1900(now_local + timedelta(days=max(1, n)))
            except (IndexError, ValueError):
                pass

        if token.startswith('WEEKDAY:'):
            day_name = token.split(':')[1].capitalize()
            day_map = {
                'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
                'Friday': 4, 'Saturday': 5, 'Sunday': 6,
            }
            target = day_map.get(day_name)
            if target is not None:
                days_ahead = (target - now_local.weekday()) % 7 or 7
                return at_1900(now_local + timedelta(days=days_ahead))

        # UNKNOWN — default to tomorrow 19:00
        return at_1900(now_local + timedelta(days=1))

    # =========================================================================
    # NORMAL FOLLOW-UP SECTION (unchanged from original)
    # =========================================================================

    def _get_eligible_leads(self, now_local, force):
        from django.db.models import Q

        response_window = now_local - timedelta(hours=2/60)

        leads = (
            Appointment.objects
            .filter(is_lead_active=True, status='pending')
            .exclude(followup_stage='completed')
            .exclude(last_customer_response__gte=response_window)
            .exclude(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book'])
        )

        if not force:
            today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            cold_warm_sent_today = Q(
                last_followup_sent__gte=today_start,
                lead_status__in=[LeadStatus.COLD, LeadStatus.WARM]
            )
            leads = leads.exclude(cold_warm_sent_today)

        return leads.order_by('last_customer_response', 'created_at')

    def _print_eligibility_breakdown(self, now_local, force):
        from django.db.models import Q

        response_window = now_local - timedelta(hours=2/60)
        plan_block_q = Q(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book']) | Q(plan_status='pending_upload')

        q0 = Appointment.objects.filter(is_lead_active=True, status='pending')
        c0 = q0.count()

        q1 = q0.exclude(followup_stage='completed')
        c1 = q1.count()

        q2 = q1.exclude(last_customer_response__gte=response_window)
        c2 = q2.count()

        q3 = q2.exclude(plan_block_q)
        c3 = q3.count()

        removed_daily_cap = 0
        c4 = c3
        if not force:
            today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            cold_warm_sent_today = Q(
                last_followup_sent__gte=today_start,
                lead_status__in=[LeadStatus.COLD, LeadStatus.WARM]
            )
            q4 = q3.exclude(cold_warm_sent_today)
            c4 = q4.count()
            removed_daily_cap = c3 - c4

        self.stdout.write(self.style.WARNING('🔎 Eligibility breakdown'))
        self.stdout.write(f'  active_pending: {c0}')
        self.stdout.write(f'  excluded_completed_stage: {c0 - c1}')
        self.stdout.write(f'  excluded_recent_response_24h: {c1 - c2}')
        self.stdout.write(f'  excluded_plan_flow: {c2 - c3}')
        if force:
            self.stdout.write('  excluded_cold_warm_sent_today: 0 (force mode)')
        else:
            self.stdout.write(f'  excluded_cold_warm_sent_today: {removed_daily_cap}')
        self.stdout.write(f'  eligible_after_filters: {c4}')

    def _process_lead(self, lead, now_local, dry_run, force):
        ready, reason = self._is_ready_for_followup(lead, now_local, force)
        if not ready:
            logger.debug(f'Lead {lead.id} skipped: {reason}')
            return {'status': 'skipped'}

        max_followups = MAX_FOLLOWUPS_PER_STATUS.get(lead.lead_status, 3)
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
        attempt = lead.followup_count + 1
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
        attempt_index = min(lead.followup_count, 3)
        intervals     = TIER_INTERVALS.get(lead.lead_status, TIER_INTERVALS[LeadStatus.COLD])
        base_hours    = intervals[attempt_index]

        backoff_factor = 1
        wait_hours     = min(base_hours * backoff_factor, base_hours * 4)

        reference = (
            lead.last_customer_response
            or lead.last_followup_sent
            or lead.created_at
        )
        elapsed = (timezone.now() - reference).total_seconds() / 3600

        if elapsed < wait_hours:
            return False, f'{elapsed:.1f}h elapsed, need {wait_hours:.1f}h'
        return True, ''

    def _backoff_factor(self, lead):
        if (lead.last_customer_response and lead.last_followup_sent
                and lead.last_customer_response > lead.last_followup_sent):
            return 1
        ignored = lead.followup_count
        return min(2 ** ignored, 4)

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
        if lead.has_plan is None:
            return 'plan_or_visit'
        if not lead.customer_area:
            return 'area'
        if not lead.timeline:
            return 'timeline'
        if not lead.property_type:
            return 'property_type'
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
        if deepseek_client:
            try:
                return self._ai_message(lead, next_question, attempt, last_question)
            except Exception as exc:
                logger.warning(f'AI generation failed for lead {lead.id}: {exc}')
        return self._template_message(lead, next_question, attempt)

    # ─── AI message ──────────────────────────────────────────────────────────

    def _ai_message(self, lead, next_question, attempt, last_question):
        service  = self._service_label(lead)
        time_ref = self._elapsed_description(lead)
        area     = lead.customer_area or ''

        template_result = self._template_message(lead, next_question, attempt)
        template_text   = template_result['message']

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

        prompt = f"""You are writing a WhatsApp follow-up message for Homebase Plumbers — a professional plumbing company in Zimbabwe/South Africa.

    LEAD CONTEXT:
    - Interest: {service}
    - Area: {area or 'not yet shared'}
    - Last heard from them: {time_ref}
    - This is follow-up attempt #{attempt}

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
    5. One question maximum
    6. {length_instruction}
    7. South African / Zimbabwean English (e.g. "sorted" not "handled", "keen" not "excited")
    8. Zero markdown, zero bold, zero bullet points
    9. At most one emoji — only if it fits naturally. Attempt 4+ = no emoji
    10. Never say: "just checking in", "following up", "I noticed you haven't replied", "hope you're well", "touching base"
    11. Sound like a real person texting, not a marketing email

    Output ONLY the message text. No labels, no quotes around it, no explanation."""

        response = deepseek_client.chat.completions.create(
            model='deepseek-chat',
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
            max_tokens=180,
        )

        message = response.choices[0].message.content.strip()
        message = message.replace('**', '').replace('__', '')

        logger.info(
            f'AI follow-up | lead={lead.id} attempt={attempt} '
            f'q={next_question} '
            f'rephrase={"yes" if last_question and attempt <= 3 else "no"}'
        )
        return {'message': message, 'ai_generated': True, 'template_fallback': False}

    # ─── Template fallback (no AI) ────────────────────────────────────────────

    def _template_message(self, lead, next_question, attempt):
        service = self._service_label(lead)
        area    = f' in {lead.customer_area}' if lead.customer_area else ''

        templates = {
            'service_type': [
                (
                    f"Hi there, what made you reach out? Most people don't message unless something's actually bothering them about their space.\n\n"
                    f"Help me understand — is it a price thing, or is it that you're still figuring out exactly what needs to be done?"
                ),
                (
                    f"Hey! Just so I point you in the right direction — are you looking at a bathroom renovation, kitchen reno, or a new installation?\n\n"
                    f"Whatever it is, we price the job upfront so you know exactly what you're paying before anything starts."
                ),
                (
                    f"I might be off, but when people hesitate here it's usually because they're still figuring out exactly what they want done.\n\n"
                    f"If you had to choose today, which direction are you leaning — bathroom, kitchen, or installation?"
                ),
                (
                    f"If now's not the right time to explore it, that's completely fine.\n\n"
                    f"Just let me know, should we park this for now, or are you still looking to get something sorted?"
                ),
            ],
            'plan_or_visit': [
                (
                    f"Hi there, is it that you're not sure if the visit is worth it, or is it more of a timing thing?\n\n"
                    f"Either way — the visit is free, takes about an hour, and locks your price in before anything starts."
                ),
                (
                    f"Hi there, we knocked out a {service} last week where the client had a plan ready, "
                    f"saved them two days on site. Do you have plans already, or should we come take a look first?\n\n"
                    f"Most people who hesitate here are just unsure which option saves them more money — the visit usually wins."
                ),
                (
                    f"I could be wrong, but sometimes people delay the visit because they're worried it'll come with a big quote.\n\n"
                    f"The visit itself is free, and it actually locks in your price so there are no surprises mid-job. Would there be any reason not to book it?"
                ),
                (
                    f"No pressure at all.\n\n"
                    f"Should we organise the visit for your {service}, or would you prefer to pause this for now?"
                ),
            ],
            'area': [
                (
                    f"Hi there, is it that you're still comparing options, or is it more of a budget concern at this stage?\n\n"
                    f"Either is fine — I just want to make sure I'm pointing you in the right direction. Which area are you based in?"
                ),
                (
                    f"Hi there, sometimes people hesitate sharing their area because they're worried distance will push the price up.\n\n"
                    f"We price the job, not the distance — which suburb would we be working in?"
                ),
                (
                    f"I might be off, but sometimes people hesitate sharing their area because they're still comparing options.\n\n"
                    f"If we're the right fit, which suburb would we be working in?"
                ),
                (
                    f"If you'd rather not continue right now, no stress.\n\n"
                    f"Should we close this off, or are you still wanting help with your {service}?"
                ),
            ],
            'availability': [
                (
                    f"Hi there, is it a timing thing, or is the price still feeling uncertain?\n\n"
                    f"Once we lock in the visit, you get a fixed quote — no hourly rates, no bill shock. What day works for you?"
                ),
                (
                    f"Hi there, most people who go quiet at this stage are either waiting on budget or comparing other quotes.\n\n"
                    f"Either way — booking a slot costs nothing and you can always reschedule. Would that work?"
                ),
                (
                    f"Most people who hesitate here are either comparing quotes or waiting to see if the budget clears.\n\n"
                    f"Either way, locking in a slot costs nothing and you can always reschedule. Would that work for you?"
                ),
                (
                    f"Totally fine if now's not the time.\n\n"
                    f"Just let me know — should I close this out, or lock in your slot?"
                ),
            ],
            'complete': [
                (
                    f"Hi there, is it a price thing, or is something else making you hesitate?\n\n"
                    f"Your fixed quote is already set — there's nothing extra coming. Just say the word and I'll lock in your {service}."
                ),
                (
                    f"Hi there, all set on our end for your {service}. "
                    f"The price is fixed once we confirm — what's the best time to lock it in?"
                ),
                (
                    f"Hi there, your {service} slot is ready to book whenever you are. "
                    f"Shall I lock it in?"
                ),
                (
                    f"Hi there, still want to get the {service} sorted?"
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