# bot/management/commands/send_followups.py
# SMART FOLLOW-UP SYSTEM
# - Tier-based intervals:    very_hot=4-8h, hot=24h, warm=48h, cold=72-168h
# - Exponential backoff:     each ignored follow-up doubles wait (capped 8×)
# - Contact window:          only sends 8-10am, 12-1pm, 5-7pm local time
# - AI messages:             warm, non-pushy, asks ONE next question naturally

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from bot.models import Appointment, LeadStatus
from bot.whatsapp_cloud_api import whatsapp_api
from openai import OpenAI
import os
import logging
import json
import pytz

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')
deepseek_client = (
    OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")
    if DEEPSEEK_API_KEY else None
)

SA_TIMEZONE = pytz.timezone('Africa/Johannesburg')

# ─── Contact windows (local time, hour ranges are half-open) ─────────────────
CONTACT_WINDOWS = [
    (8, 10),    # 08:00–09:59
    (12, 13),   # 12:00–12:59
    (17, 19),   # 17:00–18:59
]

# ─── Base intervals (hours) per lead status ───────────────────────────────────
BASE_INTERVALS = {
    LeadStatus.VERY_HOT: 6,    # ~6 hours
    LeadStatus.HOT:      24,   # 1 day
    LeadStatus.WARM:     48,   # 2 days
    LeadStatus.COLD:     96,   # 4 days
}

# Stage caps so we stop at a sensible point regardless of score
MAX_FOLLOWUPS_PER_STATUS = {
    LeadStatus.VERY_HOT: 8,
    LeadStatus.HOT:      6,
    LeadStatus.WARM:     5,
    LeadStatus.COLD:     4,
}

BACKOFF_MULTIPLIER = 2      # double the wait each time a follow-up is ignored
MAX_BACKOFF_FACTOR  = 8     # cap at 8× the base interval


# ─────────────────────────────────────────────────────────────────────────────
class Command(BaseCommand):
    help = 'Smart follow-up: tier-based intervals, exponential backoff, contact windows'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be sent without sending')
        parser.add_argument('--force', action='store_true',
                            help='Ignore contact windows and cooldown rules')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force   = options['force']

        self.stdout.write(self.style.SUCCESS('🔍 Smart follow-up check starting…'))
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

        if dry_run:
            self.stdout.write(self.style.WARNING('\n🧪 Dry-run complete — nothing sent'))

    # ─── Eligibility ─────────────────────────────────────────────────────────

    def _get_eligible_leads(self, now_local, force):
        from django.db.models import Q

        response_window = now_local - timedelta(hours=24)

        leads = (
            Appointment.objects
            .filter(is_lead_active=True, status='pending')
            .exclude(followup_stage='completed')
            # Never interrupt a customer who responded in the last 24 h
            .exclude(last_customer_response__gte=response_window)
            # Exclude plan-upload flows
            .exclude(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book'])
            .exclude(plan_status='pending_upload')
        )

        if not force:
            today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            # For hot/very_hot leads we allow multiple per day; for others skip if sent today
            cold_warm_sent_today = Q(
                last_followup_sent__gte=today_start,
                lead_status__in=[LeadStatus.COLD, LeadStatus.WARM]
            )
            leads = leads.exclude(cold_warm_sent_today)

        return leads.order_by('last_customer_response', 'created_at')

    # ─── Per-lead processing ──────────────────────────────────────────────────

    def _process_lead(self, lead, now_local, dry_run, force):
        # Decide if it's actually time
        ready, reason = self._is_ready_for_followup(lead, now_local, force)
        if not ready:
            logger.debug(f'Lead {lead.id} skipped: {reason}')
            return {'status': 'skipped'}

        # Check if we should retire this lead
        max_followups = MAX_FOLLOWUPS_PER_STATUS.get(lead.lead_status, 4)
        if lead.followup_count >= max_followups:
            if not dry_run:
                lead.followup_stage = 'completed'
                lead.is_lead_active  = False
                lead.lead_marked_inactive_at = timezone.now()
                lead.save()
            self.stdout.write(
                self.style.WARNING(f'✔️  Lead {lead.id} retired after {lead.followup_count} follow-ups')
            )
            return {'status': 'completed'}

        # Generate message
        next_q   = self._get_next_question(lead)
        result   = self._generate_message(lead, next_q)
        message  = result['message']

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f'🧪 Would send to {lead.phone_number} '
                    f'[{lead.get_lead_status_display()}] '
                    f'(backoff_factor={self._backoff_factor(lead)}×, '
                    f'next_q={next_q})\n'
                    f'   "{message[:120]}…"'
                )
            )
            return {'status': 'sent', **result}

        # Send
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
                f'follow-up #{lead.followup_count}'
            )
        )
        return {'status': 'sent', **result}

    # ─── Timing logic ─────────────────────────────────────────────────────────

    def _is_ready_for_followup(self, lead, now_local, force):
        """Return (True, '') if it's time to send, else (False, reason)."""
        base_hours   = BASE_INTERVALS.get(lead.lead_status, 96)
        factor       = self._backoff_factor(lead)
        wait_hours   = min(base_hours * factor, base_hours * MAX_BACKOFF_FACTOR)

        # Reference: last time the customer responded, or last follow-up, or creation
        reference = (
            lead.last_customer_response
            or lead.last_followup_sent
            or lead.created_at
        )
        hours_elapsed = (timezone.now() - reference).total_seconds() / 3600

        if hours_elapsed < wait_hours:
            return False, f'only {hours_elapsed:.1f}h elapsed, need {wait_hours:.1f}h'

        return True, ''

    def _backoff_factor(self, lead):
        """
        Exponential backoff: every follow-up that was sent but NOT followed by
        a customer response counts as an 'ignored' message.
        factor = 2^(ignored_count), capped at MAX_BACKOFF_FACTOR.
        """
        # Approximate 'ignored' count: total follow-ups sent minus responses received.
        # We track followup_count; each response resets the stage to 'responded'.
        # A simple proxy: if last_customer_response predates last_followup_sent,
        # the customer has not replied since our last message → +1 ignored.
        ignored = lead.followup_count  # starts at 0 for new leads

        # If they responded after the last follow-up, reset ignored to 0
        if (lead.last_customer_response and lead.last_followup_sent
                and lead.last_customer_response > lead.last_followup_sent):
            ignored = 0

        factor = BACKOFF_MULTIPLIER ** ignored
        return min(factor, MAX_BACKOFF_FACTOR)

    def _stage_label(self, lead):
        """Map follow-up count to a human-readable stage name."""
        labels = ['day_1', 'day_3', 'week_1', 'week_2', 'month_1', 'completed']
        idx = min(lead.followup_count, len(labels) - 1)
        return labels[idx]

    # ─── Contact window ───────────────────────────────────────────────────────

    def _in_contact_window(self, now_local):
        hour = now_local.hour
        return any(start <= hour < end for start, end in CONTACT_WINDOWS)

    # ─── Next question helper ─────────────────────────────────────────────────

    def _get_next_question(self, lead):
        """
        Return the single most important piece of missing information.
        NOTE: customer_name is intentionally excluded — it is only asked at the
        very end of the booking confirmation flow, never in follow-up messages.
        """
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

    # ─── Conversation history helpers ────────────────────────────────────────

    def _last_bot_question(self, lead):
        """
        Scan conversation_history in reverse to find the most recent message
        sent by the bot (role='assistant') that looks like a question.
        Strips auto-follow-up tags and skips confirmation/notification messages.
        Returns the raw question text, or None if nothing useful is found.
        """
        history = lead.conversation_history or []
        skip_prefixes = (
            '[AUTO FOLLOW-UP]',
            '[AUTOMATIC FOLLOW-UP]',
            '[MANUAL FOLLOW-UP]',
            '[BULK MANUAL FOLLOW-UP]',
            'APPOINTMENT CONFIRMED',
            'NEW APPOINTMENT BOOKED',
            'PLAN RECEIVED',
            '📋',
            '🚨',
        )

        for msg in reversed(history):
            if msg.get('role') != 'assistant':
                continue

            content = msg.get('content', '').strip()

            # Strip follow-up tags so we see the real text
            for prefix in ('[AUTO FOLLOW-UP] ', '[AUTOMATIC FOLLOW-UP] ',
                           '[MANUAL FOLLOW-UP] ', '[BULK MANUAL FOLLOW-UP] '):
                if content.startswith(prefix):
                    content = content[len(prefix):]

            # Skip if it's a system notification or doesn't contain a question
            if any(content.startswith(p) for p in skip_prefixes):
                continue
            if '[Sent ' in content or '[MEDIA]' in content:
                continue
            if '?' not in content:
                continue

            # Found a real bot question — return it (truncated if huge)
            return content[:600]

        return None

    def _time_reference(self, lead):
        """Human-friendly description of how long the lead has been silent."""
        reference = lead.last_customer_response or lead.created_at
        elapsed_h = (timezone.now() - reference).total_seconds() / 3600
        if elapsed_h < 30:
            return 'earlier today'
        if elapsed_h < 54:
            return 'yesterday'
        if elapsed_h < 120:
            return 'a couple of days ago'
        if elapsed_h < 240:
            return 'a few days ago'
        if elapsed_h < 500:
            return 'last week'
        return 'a while back'

    # ─── Message generation ───────────────────────────────────────────────────

    def _generate_message(self, lead, next_question):
        last_question = self._last_bot_question(lead)
        if deepseek_client:
            try:
                return self._ai_message(lead, next_question, last_question)
            except Exception as exc:
                logger.warning(f'AI generation failed for lead {lead.id}: {exc}')
        return self._template_message(lead, next_question)

    def _ai_message(self, lead, next_question, last_question):
        """
        Generate a follow-up by rephrasing the last unanswered bot question.
        If no prior question exists in history, fall back to asking next_question fresh.
        Customer name is NEVER asked in a follow-up message.
        """
        # Name is collected at the END of the flow, never during follow-ups.
        # Use a warm, nameless opener instead.
        service = (lead.project_type or '').replace('_', ' ')
        time_ref      = self._time_reference(lead)
        attempt_num   = lead.followup_count + 1  # 1-based for the prompt

        # ── What question to rephrase / ask ──────────────────────────────────
        # Default fallbacks per field (used when no prior question in history)
        fallback_question_map = {
            'service_type':  'which service they need — bathroom renovation, kitchen renovation, or new plumbing installation',
            'plan_or_visit': 'whether they already have a plan/blueprint or would prefer a site visit first',
            'area':          'which area or suburb they are located in',
            'timeline':      'roughly when they are hoping to get the work done',
            'property_type': 'whether the property is a house, an apartment, or a business',
            'availability':  'what day and time would suit them for an appointment',
            'complete':      None,
        }

        if next_question == 'complete':
            question_instruction = (
                'Let them know we have all the details we need and are ready to lock in their appointment '
                'whenever they give the go-ahead. Keep it light and easy.'
            )
            rephrase_block = ''
        elif last_question:
            rephrase_block = f'\nLAST QUESTION WE ASKED (that went unanswered):\n"""\n{last_question}\n"""\n'
            question_instruction = (
                'Rephrase that last unanswered question in a fresh, natural way — same information needed, '
                'completely different wording. Do NOT copy the original phrasing. '
                'Do NOT reveal that we already asked it before. Make it feel like a new, casual question.'
            )
        else:
            rephrase_block = ''
            question_instruction = (
                f'Ask ONE gentle question to find out: {fallback_question_map.get(next_question, "what they need")}. '
                'Keep it conversational and natural.'
            )

        prompt = f"""You are Sarah, a warm and professional appointment assistant for Homebase Plumbers — a luxury plumbing company in Zimbabwe/South Africa.

CUSTOMER CONTEXT:
- Service interest: {service or 'plumbing work'}
- Area: {lead.customer_area or 'not yet shared'}
- Last heard from them: {time_ref}
- This is follow-up attempt #{attempt_num}
{rephrase_block}
YOUR TASK:
{question_instruction}

STRICT RULES — follow every one:
1. Open with "Hi there," — we do not have the customer's name yet, do NOT use a name
2. Reference their specific interest ({service or 'plumbing project'}) to make it feel personal
3. Acknowledge the gap in time naturally without apologising or guilt-tripping
4. Ask AT MOST ONE question — embedded naturally in the message, not bolted on at the end
5. NEVER ask for the customer's name — that happens only at booking confirmation, not here
6. 2–4 short sentences maximum — WhatsApp messages, not emails
7. End with "– Homebase Plumbers"
8. South African / Zimbabwean English spelling
9. At most ONE emoji, only if it genuinely fits — do NOT force one in
10. No bullet points, no bold text, no markdown
11. Do NOT say "just checking in", "following up", or "I noticed you haven't replied"

Generate ONLY the message text — no labels, no quotes around it, no explanation."""

        response = deepseek_client.chat.completions.create(
            model='deepseek-chat',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You write short, warm WhatsApp messages for a plumbing company. '
                        'Sound like a real person, not a bot. '
                        'Never list questions. Never use or ask for the customer name — we do not have it yet. Always open with "Hi there,". '
                        'Always rephrase the last unanswered question — never copy its exact wording.'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.88,   # slightly higher = more varied rephrasing each time
            max_tokens=160,
        )

        message = response.choices[0].message.content.strip()
        logger.info(
            f'AI follow-up generated for lead {lead.id} '
            f'(attempt={attempt_num}, q={next_question}, '
            f'rephrase={"yes" if last_question else "no"})'
        )
        return {'message': message, 'ai_generated': True, 'template_fallback': False}

    def _template_message(self, lead, next_question):
        """
        Fallback templates used when DeepSeek is unavailable.
        One question each, non-pushy. Customer name is never asked here.
        """
        # Name is only collected at booking confirmation — never use it here.
        service = (lead.project_type or 'plumbing project').replace('_', ' ')

        templates = {
            'service_type': (
                f"Hi there, hope you're doing well! We'd love to help with your plumbing — "
                f"are you looking at a bathroom renovation, kitchen renovation, or a new installation?\n\n"
                f"– Homebase Plumbers"
            ),
            'plan_or_visit': (
                f"Hi there, still here whenever you're ready to move forward with your {service}. "
                f"Do you have existing plans or blueprints, or would a quick site visit work better for you?\n\n"
                f"– Homebase Plumbers"
            ),
            'area': (
                f"Hi there, we're keen to get your {service} sorted. "
                f"Which area are you based in so we can check our availability near you?\n\n"
                f"– Homebase Plumbers"
            ),
            'timeline': (
                f"Hi there, no rush at all — roughly when were you hoping to get your {service} moving?\n\n"
                f"– Homebase Plumbers"
            ),
            'property_type': (
                f"Hi there, one small thing we still need for your {service} — "
                f"is the property a house, an apartment, or a business?\n\n"
                f"– Homebase Plumbers"
            ),
            'availability': (
                f"Hi there, we have everything we need to get your {service} booked. "
                f"What day and time works best for you?\n\n"
                f"– Homebase Plumbers"
            ),
            'complete': (
                f"Hi there, we're all set on our end for your {service}. "
                f"Just say the word and we'll lock it in!\n\n"
                f"– Homebase Plumbers"
            ),
        }

        message = templates.get(next_question, templates['complete'])
        return {'message': message, 'ai_generated': False, 'template_fallback': True}

    # ─── Utility ──────────────────────────────────────────────────────────────

    def _clean_phone(self, phone):
        return phone.replace('whatsapp:', '').replace('+', '').strip()