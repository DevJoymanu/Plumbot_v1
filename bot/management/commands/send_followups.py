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
    (8, 21),    # All day: 8 AM - 9 PM SAST
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

    # ─── Delayed lead re-engagement ──────────────────────────────────────────

    def _process_delayed_reactivations(self, now_local, dry_run):
        """
        Finds delayed leads whose follow-up date has arrived, re-activates them,
        sends a contextual WhatsApp message, and a follow-up email if on file.
        """
        import re as _re
        from bot.customer_emails import send_delay_followup_email

        due = (
            Appointment.objects
            .filter(
                is_lead_active=True,
                is_delayed=True,
                delay_followup_due_at__lte=timezone.now(),
            )
            .exclude(chatbot_paused=True)
        )

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

                # Build a specific project reference for the message
                if desc:
                    detail = desc[:80]
                elif area:
                    detail = f'{service} in {area}'
                else:
                    detail = service

                message = (
                    f'{hi}, hope you\'re back and settled in. '
                    f'You were looking at {detail} — still keen to move forward? '
                    f'We\'re ready when you are.'
                )

                if dry_run:
                    self.stdout.write(
                        self.style.SUCCESS(f'🧪 Would reactivate lead {lead.id}: "{message[:100]}…"')
                    )
                    continue

                # Re-activate — clear is_delayed and DELAY_SIGNAL tag
                lead.is_delayed = False
                notes = lead.internal_notes or ''
                notes = _re.sub(r'\[DELAY_SIGNAL\][^\n]*\n?', '', notes).strip()
                lead.internal_notes = notes
                lead.save(update_fields=['is_delayed', 'internal_notes'])

                # Send WhatsApp re-engagement
                clean = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
                whatsapp_api.send_text_message(clean, message)
                lead.add_conversation_message('assistant', f'[DELAY REACTIVATION] {message}')

                # Send contextual follow-up email if we have one
                has_email = bool(getattr(lead, 'customer_email', None))
                if has_email:
                    send_delay_followup_email(lead)

                self.stdout.write(self.style.SUCCESS(
                    f'✅ Reactivated lead {lead.id}'
                    + (' + email sent' if has_email else '')
                ))

            except Exception as exc:
                logger.error(f'Error reactivating delayed lead {lead.id}: {exc}')
                self.stdout.write(self.style.ERROR(f'❌ Delayed lead {lead.id}: {exc}'))

    # ─── Eligibility ─────────────────────────────────────────────────────────

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
#            .exclude(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book'])
            .exclude(internal_notes__contains='[DELAY_SIGNAL]')
            .exclude(chatbot_paused=True)
        )        
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

        elapsed = (timezone.now() - reference).total_seconds() / 3600

        if elapsed < wait_hours:
            return False, f'{elapsed:.1f}h elapsed, need {wait_hours:.1f}h (attempt #{attempt_index + 1})'
        return True, ''

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
        if deepseek_client:
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

        response = deepseek_client.chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
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

        message = response.choices[0].message.content.strip()
        message = message.replace('**', '').replace('__', '')

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