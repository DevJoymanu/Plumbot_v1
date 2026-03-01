# bot/management/commands/send_followups.py
#
# FOLLOW-UP SYSTEM — 4-ATTEMPT PARAPHRASE STRATEGY
#
# Core principle (Hormozi-aligned, but simplified):
#
#  Every lead gets exactly 4 follow-up attempts.
#  Attempt 1: Original message — warm, specific, value-first.
#  Attempt 2: Paraphrase of attempt 1 — same core message, different wording.
#  Attempt 3: Another paraphrase — keeps the warmth, shifts the angle slightly.
#  Attempt 4: Final paraphrase — last chance, softest tone, no pressure.
#
#  After attempt 4 with no response → lead is marked inactive (dead).
#
#  Why 4 attempts max?
#  - More than 4 messages without a reply = harassment, not sales.
#  - Quality > quantity. A well-crafted paraphrase beats a new angle.
#  - Clean pipeline: dead leads are removed, not just deprioritised.
#
# TIMING (same for all tiers — simple, predictable):
#   Attempt 1: 24h after last customer response (or creation)
#   Attempt 2: 48h after attempt 1
#   Attempt 3: 72h after attempt 2
#   Attempt 4: 96h after attempt 3
#
#  Total follow-up window: ~10 days. After that, they're gone.
#
# CONTACT WINDOWS — only reach during high-read-rate times:
#   8-10am (commute), 12-1pm (lunch), 5-7pm (after work).
#
# TONE PROGRESSION:
#   1 → Warm & specific (full context, value-first)
#   2 → Slightly shorter, reworded, same warmth
#   3 → More casual, empathetic, low pressure
#   4 → Ultra-short, direct, human — the "9-word" style

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
    (8, 10),    # morning commute
    (12, 13),   # lunch break
    (17, 23),   # after work / evening
]

# ─── Fixed intervals per attempt (hours) — same for all lead temperatures ────
# attempt_index: 0=first, 1=second, 2=third, 3=fourth+
ATTEMPT_INTERVALS = (5/60, 5/60, 5/60, 5/60)   # cumulative wait from previous message

# All leads get exactly 4 attempts. No exceptions.
MAX_FOLLOWUPS = 4


# ─────────────────────────────────────────────────────────────────────────────
class Command(BaseCommand):
    help = '4-attempt paraphrase follow-up: same message, progressively softer tone, dead after attempt 4'

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

        leads = self._get_eligible_leads(now_local, force)
        self.stdout.write(f'📊 {leads.count()} leads eligible for follow-up')

        totals = dict(sent=0, skipped=0, errors=0, retired=0, ai=0, template=0)

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
        response_window = now_local - timedelta(hours=24)

        leads = (
            Appointment.objects
            .filter(is_lead_active=True, status='pending')
            .exclude(followup_stage='completed')
            .exclude(last_customer_response__gte=response_window)
            # Don't interfere with plan-upload flows
            .exclude(plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book'])
            .exclude(plan_status='pending_upload')
        )

        return leads.order_by('last_customer_response', 'created_at')

    # ─── Per-lead processing ──────────────────────────────────────────────────

    def _process_lead(self, lead, now_local, dry_run, force):
        ready, reason = self._is_ready_for_followup(lead, now_local, force)
        if not ready:
            logger.debug(f'Lead {lead.id} skipped: {reason}')
            return {'status': 'skipped'}

        # Hard cap: 4 attempts. Retire after that.
        if lead.followup_count >= MAX_FOLLOWUPS:
            if not dry_run:
                lead.followup_stage = 'completed'
                lead.is_lead_active = False
                lead.lead_marked_inactive_at = timezone.now()
                lead.save()
            self.stdout.write(
                self.style.WARNING(
                    f'💀 Lead {lead.id} retired — no response after {lead.followup_count} follow-ups'
                )
            )
            return {'status': 'retired'}

        next_q  = self._get_next_question(lead)
        attempt = lead.followup_count + 1   # 1-based
        result  = self._generate_message(lead, next_q, attempt)
        message = result['message']

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f'🧪 Would send to {lead.phone_number} '
                    f'[{lead.get_lead_status_display()}] '
                    f'attempt #{attempt}/{MAX_FOLLOWUPS}, q={next_q}\n'
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

        # Retire immediately after attempt 4 is sent
        if lead.followup_count >= MAX_FOLLOWUPS:
            lead.followup_stage = 'completed'
            lead.is_lead_active = False
            lead.lead_marked_inactive_at = timezone.now()
            lead.save()
            self.stdout.write(
                self.style.WARNING(
                    f'💀 Lead {lead.id} marked dead — final follow-up sent'
                )
            )

        tag = '🤖 AI' if result['ai_generated'] else '📄 Template'
        self.stdout.write(
            self.style.SUCCESS(
                f'✅ {tag} → {lead.phone_number} '
                f'attempt #{lead.followup_count}/{MAX_FOLLOWUPS}'
            )
        )
        return {'status': 'sent', **result}

    # ─── Timing ───────────────────────────────────────────────────────────────

    def _is_ready_for_followup(self, lead, now_local, force):
        if force:
            return True, ''

        attempt_index = min(lead.followup_count, 3)   # 0→3
        wait_hours    = ATTEMPT_INTERVALS[attempt_index]

        reference = (
            lead.last_customer_response
            or lead.last_followup_sent
            or lead.created_at
        )
        elapsed = (timezone.now() - reference).total_seconds() / 3600

        if elapsed < wait_hours:
            return False, f'{elapsed:.1f}h elapsed, need {wait_hours}h'
        return True, ''

    def _stage_label(self, lead):
        """Map followup_count → human-readable stage label."""
        labels = ['attempt_1', 'attempt_2', 'attempt_3', 'attempt_4', 'completed']
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
        if not lead.scheduled_datetime:
            return 'availability'
        return 'complete'

    # ─── Conversation context helpers ────────────────────────────────────────

    def _last_bot_question(self, lead):
        """Return the last substantive question the bot asked (for paraphrasing)."""
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
            # Strip known prefixes
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

        # ── Tone progression — same core message, progressively softer ───────
        tone_guides = {
            1: (
                'TONE: Warm and specific. Reference their project directly. '
                'Lead with ONE genuinely useful insight — something that saves them '
                'time or money on this type of project. Then ask the one question '
                'we need. Full context, value-first. This is the freshest message '
                'they will receive from us.'
            ),
            2: (
                'TONE: Slightly softer than attempt 1. Same warmth, but a little '
                'shorter. Reword the previous message — same core point, different '
                'phrasing. No new angle, just a fresh way of saying the same thing. '
                'Acknowledge implicitly that life gets busy without saying it. '
                'Still ask the one question we need.'
            ),
            3: (
                'TONE: More casual and empathetic. No pressure at all. '
                'Keep it brief — 2 sentences max before the question. '
                'Sound like a real person checking in, not a business chasing a sale. '
                'Still ask the same question as before, but phrase it as gently as possible. '
                'Make it easy for them to say "not yet" without feeling guilty.'
            ),
            4: (
                'TONE: Ultra-short and human — the "9-word email" style. '
                'One sentence only. Direct, personal, zero pressure. '
                'Example: "Still thinking about the bathroom reno?" '
                'No preamble. No explanation.'
                'This is the last message they will ever get from us if they do not reply.'
            ),
        }

        tone_guide = tone_guides.get(attempt, tone_guides[4])

        # ── What to ask ───────────────────────────────────────────────────────
        field_context = {
            'service_type':  'which service they need — bathroom renovation, kitchen renovation, or new plumbing installation',
            'plan_or_visit': 'whether they have existing plans/blueprints, or prefer a site visit first',
            'area':          'which area or suburb they are in',
            'availability':  'what day and time suits them for an appointment',
            'complete':      None,
        }

        if next_question == 'complete':
            question_block = (
                'We have everything we need. Tell them we are ready to lock in their '
                'appointment the moment they confirm — make it feel effortless to say yes.'
            )
        elif last_question and attempt <= 3:
            question_block = (
                f'The last question we asked (unanswered) was:\n"""\n{last_question}\n"""\n\n'
                f'Rephrase it using a COMPLETELY different wording — same information needed, '
                f'totally fresh phrasing. Match the tone guide above. '
                f'Never hint that you already asked this.'
            )
        else:
            question_block = (
                f'Ask ONE question to find out: {field_context.get(next_question, "what they need")}.'
            )

        prompt = f"""You are writing a WhatsApp follow-up message for Homebase Plumbers — a professional, luxury plumbing company in Zimbabwe/South Africa.

LEAD CONTEXT:
- Interest: {service}
- Area: {area or 'not yet shared'}
- Last heard from them: {time_ref}
- This is follow-up attempt #{attempt} of 4

{tone_guide}

QUESTION TO EMBED (do this naturally, not bolted on):
{question_block}

RULES — every single one must be followed:
1. Open with "Hi there," — we do not have their name, never use one
2. NEVER ask for the customer's name — that only happens at booking confirmation
3. Be specific — use "{service}"{(' in "' + area + '"') if area else ''} not vague words like "your project"
4. One question maximum — embedded in the flow, not a standalone line at the end
5. Attempt 1-3: 2-4 sentences. Attempt 4: 1-2 sentences MAXIMUM
6. End with "– Homebase Plumbers" on its own line
7. South African / Zimbabwean English (e.g. "sorted" not "handled", "keen" not "excited")
8. Zero markdown, zero bold, zero bullet points
9. At most one emoji — only if it fits naturally. Attempt 4 = no emoji
10. Never say: "just checking in", "following up", "I noticed you haven't replied", "hope you're well", "touching base"
11. Sound like a real person texting, not a marketing email
12. DO NOT mention that this is a follow-up or that you've messaged before

Output ONLY the message text. No labels, no quotes around it, no explanation."""

        response = deepseek_client.chat.completions.create(
            model='deepseek-chat',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You write short, high-converting WhatsApp messages. '
                        'Sound like a real person, not a bot or marketer. '
                        'Each attempt should be a paraphrase of the previous one — '
                        'same core message, progressively softer tone. '
                        'Never use or ask for the customer name — open with "Hi there,".'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.85,
            max_tokens=180,
        )

        message = response.choices[0].message.content.strip()

        # Strip any accidental markdown
        message = message.replace('**', '').replace('__', '')

        logger.info(
            f'AI follow-up | lead={lead.id} attempt={attempt}/{MAX_FOLLOWUPS} '
            f'q={next_question} '
            f'rephrase={"yes" if last_question and attempt <= 3 else "no"}'
        )
        return {'message': message, 'ai_generated': True, 'template_fallback': False}

    # ─── Template fallback (no AI) ────────────────────────────────────────────

    def _template_message(self, lead, next_question, attempt):
        """
        Hand-crafted fallback templates.
        Each attempt is a progressively softer paraphrase of attempt 1.
        Attempt 4 is ultra-short — the last message they will ever receive.
        """
        service = self._service_label(lead)
        area    = f' in {lead.customer_area}' if lead.customer_area else ''

        templates = {
            # ── Attempt 1: warm, specific, value-first ────────────────────────
            # ── Attempt 2: reworded, slightly shorter ─────────────────────────
            # ── Attempt 3: casual, empathetic, low pressure ───────────────────
            # ── Attempt 4: ultra-short, human, final ─────────────────────────
            #

            # ── STAGE 1: Lead went silent after "which service?" ──────────────
            'service_type': [
                (
                    "Hi there,\n\n"
                    "What made you reach out? Most people don't message unless "
                    "something's actually bothering them about their space.\n\n"
                    "Help me understand — what's going on?"
                ),
                (
                    "Hi there,\n\n"
                    "I get the sense you might still be weighing things up. "
                    "That's completely fair — most people do before they see the quote.\n\n"
                    "Was it a bathroom renovation, kitchen reno, or a new plumbing installation?"
                ),
                (
                    "Hi there,\n\n"
                    "I recently helped someone who wasn't sure where to start either, "
                    "and they ended up loving how polished and complete the space felt afterwards.\n\n"
                    "Was it a bathroom, kitchen, or plumbing installation you had in mind?"
                ),
                (
                    "Hi there,\n\n"
                    "Still looking for a plumber?"
                ),
            ],

            # ── STAGE 2: Lead went silent after "plan or site visit?" ─────────
            'plan_or_visit': [
                (
                    f"Hi there,\n\n"
                    f"What made you reach out about your {service}? "
                    "Most people don't message unless something's genuinely bothering them.\n\n"
                    "Help me understand — what's going on?"
                ),
                (
                    f"Hi there,\n\n"
                    "I get the sense you might be wondering if a site visit is worth the time. "
                    "It genuinely catches things you'd miss and locks your pricing in properly.\n\n"
                    "But I could be wrong — what's actually making you hesitate?"
                ),
                (
                    f"Hi there,\n\n"
                    f"I recently worked with someone on their {service} who felt unsure at first, "
                    "and they ended up loving the result because everything was clear before work even started.\n\n"
                    "Do you already have plans, or should we come take a look?"
                ),
                (
                    f"Hi there,\n\n"
                    f"Do you have plans for the {service} yet?"
                ),
            ],

            # ── STAGE 3: Lead went silent after "which suburb?" ───────────────
            'area': [
                (
                    f"Hi there,\n\n"
                    f"What made you reach out about your {service}? "
                    "Most people don't message unless something's actually bothering them.\n\n"
                    "Help me understand — what's going on?"
                ),
                (
                    f"Hi there,\n\n"
                    "Sometimes people pause because they're unsure about logistics. "
                    "Once we know the area, we can give you clear pricing and timing.\n\n"
                    "Which suburb are you based in?"
                ),
                (
                    "Hi there,\n\n"
                    "I recently helped a client who thought their project would be complicated, "
                    "but once we saw the location, everything became straightforward.\n\n"
                    "Where are you based?"
                ),
                (
                    "Hi there,\n\n"
                    "Which area are you in?"
                ),
            ],

            # ── STAGE 4: Lead went silent after availability question ─────────
            'availability': [
                (
                    f"Hi there,\n\n"
                    f"We have everything we need for your {service}{area}. "
                    "The only thing left is locking in a time.\n\n"
                    "What day works for you?"
                ),
                (
                    f"Hi there,\n\n"
                    f"Once your {service} is booked, you'll have full clarity on timing and cost.\n\n"
                    "Which day would suit you best?"
                ),
                (
                    f"Hi there,\n\n"
                    f"Happy to work around your schedule for the {service}.\n\n"
                    "Is there a day that might work for you?"
                ),
                (
                    "Hi there,\n\n"
                    "Should I close this out for now?"
                ),
            ],

            'complete': [
                (
                    f"Hi there,\n\n"
                    f"We have everything we need for your {service}{area}.\n\n"
                    "Just say the word and we'll lock in the appointment."
                ),
                (
                    f"Hi there,\n\n"
                    f"All set on our end for your {service}.\n\n"
                    "What time works best to confirm?"
                ),
                (
                    f"Hi there,\n\n"
                    f"Your {service} slot is ready whenever you are.\n\n"
                    "Shall I lock it in?"
                ),
                (
                    f"Hi there,\n\n"
                    f"Still want to get the {service} sorted?"
                ),
            ],
        }
        options = templates.get(next_question, templates['complete'])
        # attempt is 1-based; list is 0-indexed; clamp to last option if past the list
        idx     = min(attempt - 1, len(options) - 1)
        message = options[idx]

        return {'message': message, 'ai_generated': False, 'template_fallback': True}

    # ─── Utility ──────────────────────────────────────────────────────────────

    def _clean_phone(self, phone):
        return phone.replace('whatsapp:', '').replace('+', '').strip()