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

# After this many attempts with zero response, lead goes cold/inactive
GHOSTED_THRESHOLD = 4


# ─────────────────────────────────────────────────────────────────────────────
class Command(BaseCommand):
    help = 'High-converting follow-ups: Hormozi timing, value-first messaging, pattern interrupts'

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

    # ─── Eligibility ─────────────────────────────────────────────────────────

    def _get_eligible_leads(self, now_local, force):
        from django.db.models import Q

        # Don't interrupt a customer who engaged in the last 24h
        response_window = now_local - timedelta(hours=2/60)

        leads = (
            Appointment.objects
            .filter(is_lead_active=True, status='pending')
            .exclude(followup_stage='completed')
            .exclude(last_customer_response__gte=response_window)
            # Don't interfere with plan-upload flows
            .exclude(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book'])
#            .exclude(plan_status='pending_upload')
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

    # ─── Per-lead processing ──────────────────────────────────────────────────

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
        attempt_index = min(lead.followup_count, 3)   # 0-3, maps to tuple index
        intervals     = TIER_INTERVALS.get(lead.lead_status, TIER_INTERVALS[LeadStatus.COLD])
        base_hours    = intervals[attempt_index]

        # Temporary test mode: keep follow-up cadence fixed at 2 minutes.
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
        """
        How many follow-ups have been sent since the customer last replied?
        Each one doubles the wait. Cap at 4× so we don't disappear entirely.
        """
        if (lead.last_customer_response and lead.last_followup_sent
                and lead.last_customer_response > lead.last_followup_sent):
            return 1   # They replied after last message — no backoff

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

        # Pull the template for this question/attempt as the required base
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
        """
        Hand-crafted fallback templates. Each attempt uses a different angle
        so the customer doesn't receive the same message twice.
        """
        service = self._service_label(lead)
        area    = f' in {lead.customer_area}' if lead.customer_area else ''

        # Attempt 1 — value-led
        # Attempt 2 — social proof + casual
        # Attempt 3 — soft urgency
        # Attempt 4+ — nine-word style
    
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
        # Pick by attempt number, cycling back to last option if past the list
        idx = min(attempt - 1, len(options) - 1)
        message = options[idx]

        return {'message': message, 'ai_generated': False, 'template_fallback': True}

    # ─── Utility ──────────────────────────────────────────────────────────────

    def _clean_phone(self, phone):
        return phone.replace('whatsapp:', '').replace('+', '').strip()
