# bot/management/commands/send_followups.py
# AUTOMATIC FOLLOW-UP SYSTEM with AI-powered contextual messages
# ✅ FIXED VERSION - 'responded' stage only blocks for 24 hours, not forever

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from bot.models import Appointment
from bot.whatsapp_cloud_api import whatsapp_api
from openai import OpenAI
import os
import logging
import json

logger = logging.getLogger(__name__)

# Initialize DeepSeek client for AI-powered message generation
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')
deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1") if DEEPSEEK_API_KEY else None


class Command(BaseCommand):
    help = 'Check and send AI-powered automatic follow-up messages to non-responsive leads'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be sent without actually sending',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force send follow-ups even if already sent today',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force = options['force']
        
        self.stdout.write(self.style.SUCCESS('🔍 Starting automatic follow-up check...'))
        
        if dry_run:
            self.stdout.write(self.style.WARNING('🧪 DRY RUN MODE - No messages will be sent'))
        
        if not deepseek_client:
            self.stdout.write(self.style.ERROR('❌ DEEPSEEK_API_KEY not configured - using fallback templates'))
        
        # Get leads that need follow-up
        leads = self.get_leads_needing_followup(force)
        
        self.stdout.write(f'📊 Found {leads.count()} leads needing automatic follow-up')
        
        results = {
            'sent': 0,
            'skipped': 0,
            'errors': 0,
            'completed': 0,
            'ai_generated': 0,
            'template_fallback': 0
        }
        
        for lead in leads:
            try:
                result = self.process_lead_followup(lead, dry_run)
                results[result['status']] += 1
                if result.get('ai_generated'):
                    results['ai_generated'] += 1
                if result.get('template_fallback'):
                    results['template_fallback'] += 1
                
            except Exception as e:
                logger.error(f"Error processing lead {lead.id}: {str(e)}")
                results['errors'] += 1
                self.stdout.write(
                    self.style.ERROR(f'❌ Error with lead {lead.id}: {str(e)}')
                )
        
        # Print summary
        self.stdout.write(self.style.SUCCESS('\n📊 AUTOMATIC FOLLOW-UP SUMMARY:'))
        self.stdout.write(f"✅ Sent: {results['sent']}")
        self.stdout.write(f"🤖 AI Generated: {results['ai_generated']}")
        self.stdout.write(f"📄 Template Fallback: {results['template_fallback']}")
        self.stdout.write(f"⏭️  Skipped: {results['skipped']}")
        self.stdout.write(f"✔️  Completed: {results['completed']}")
        self.stdout.write(f"❌ Errors: {results['errors']}")
        
        if dry_run:
            self.stdout.write(self.style.WARNING('\n🧪 This was a dry run - no actual messages sent'))

    def get_leads_needing_followup(self, force=False):
        """
        Get all leads that need an automatic follow-up message
        
        ✅ FIXED: 'responded' stage only blocks for 24 hours, not forever
        """
        from django.db.models import Q
        now = timezone.now()
        
        # Base criteria: incomplete appointments that are still active leads
        leads = Appointment.objects.filter(
            is_lead_active=True,
            status='pending',  # Not yet confirmed
        ).exclude(
            followup_stage='completed'
        )
        
        # ✅ CRITICAL FIX: Only exclude 'responded' if they responded in last 24 hours
        # After 24 hours, they become eligible for follow-ups again
        response_window = now - timedelta(hours=24)
        leads = leads.exclude(
            Q(followup_stage='responded') & 
            Q(last_customer_response__gte=response_window)
        )
        
        # CRITICAL: Exclude leads who have sent plans (they're waiting for plumber review)
        leads = leads.exclude(
            plan_status__in=['plan_uploaded', 'plan_reviewed', 'ready_to_book']
        )
        
        # Also exclude leads actively in the plan upload flow
        leads = leads.exclude(
            plan_status='pending_upload'
        )
        
        # Exclude leads that responded in last 24 hours (general safety check)
        leads = leads.exclude(
            last_customer_response__gte=response_window
        )
        
        # If not forcing, exclude leads we already followed up with today
        if not force:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            leads = leads.exclude(
                last_followup_sent__gte=today_start
            )
        
        return leads.order_by('last_customer_response', 'created_at')

    def process_lead_followup(self, lead, dry_run=False):
        """Process a single lead for automatic follow-up with AI-generated message"""
        now = timezone.now()
        
        # Determine which follow-up stage this lead should be at
        next_stage = self.calculate_followup_stage(lead, now)
        
        if next_stage is None:
            return {'status': 'skipped'}
        
        if next_stage == 'completed':
            if not dry_run:
                lead.followup_stage = 'completed'
                lead.is_lead_active = False
                lead.lead_marked_inactive_at = now
                lead.save()
            
            self.stdout.write(
                self.style.WARNING(f'✔️  Lead {lead.id} marked as completed (no more automatic follow-ups)')
            )
            return {'status': 'completed'}
        
        # Generate the follow-up message using AI
        message_result = self.generate_followup_message(lead, next_stage)
        message = message_result['message']
        
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(f'🧪 Would send AUTOMATIC {next_stage} follow-up to {lead.phone_number}:')
            )
            self.stdout.write(f'   AI: {message_result["ai_generated"]}')
            self.stdout.write(f'   "{message[:100]}..."')
            return {
                'status': 'sent',
                'ai_generated': message_result['ai_generated'],
                'template_fallback': message_result['template_fallback']
            }
        else:
            # Send the actual message
            clean_phone = self.clean_phone_number(lead.phone_number)
            whatsapp_api.send_text_message(clean_phone, message)
            
            # Update lead record - mark as AUTOMATIC follow-up
            lead.last_followup_sent = now
            lead.followup_count += 1
            lead.followup_stage = next_stage
            lead.save()
            
            # Add to conversation history with AUTOMATIC tag
            lead.add_conversation_message('assistant', f"[AUTOMATIC FOLLOW-UP] {message}")
            
            ai_indicator = "🤖 AI" if message_result['ai_generated'] else "📄 Template"
            self.stdout.write(
                self.style.SUCCESS(f'✅ {ai_indicator} AUTOMATIC {next_stage} follow-up sent to {lead.phone_number}')
            )
            
            return {
                'status': 'sent',
                'ai_generated': message_result['ai_generated'],
                'template_fallback': message_result['template_fallback']
            }
    
    def calculate_followup_stage(self, lead, now):
        """Calculate which follow-up stage the lead should be at"""
        # Determine time since last interaction
        if lead.last_customer_response:
            time_since = now - lead.last_customer_response
        elif lead.last_followup_sent:
            time_since = now - lead.last_followup_sent
        else:
            time_since = now - lead.created_at

        current_stage = lead.followup_stage

        # Calculate last message time for day_1 check
        last_message_time = lead.last_customer_response or lead.created_at

        if current_stage in ['none', 'responded'] and now >= last_message_time + timedelta(days=1):
            return 'day_1'
                    
        elif current_stage == 'day_1':
            if lead.last_followup_sent:
                days_since_followup = (now - lead.last_followup_sent).total_seconds() / (3600 * 24)
                if days_since_followup >= 3:
                    return 'day_3'
        
        elif current_stage == 'day_3':
            if lead.last_followup_sent:
                days_since_followup = (now - lead.last_followup_sent).total_seconds() / (3600 * 24)
                if days_since_followup >= 7:
                    return 'week_1'
        
        elif current_stage == 'week_1':
            if lead.last_followup_sent:
                days_since_followup = (now - lead.last_followup_sent).total_seconds() / (3600 * 24)
                if days_since_followup >= 14:
                    return 'week_2'
        
        elif current_stage == 'week_2':
            if lead.last_followup_sent:
                days_since_followup = (now - lead.last_followup_sent).total_seconds() / (3600 * 24)
                if days_since_followup >= 30:
                    return 'month_1'
        
        elif current_stage == 'month_1':
            return 'completed'
        
        return None

    def generate_followup_message(self, lead, stage):
        """Generate AI-powered contextually appropriate automatic follow-up message"""
        try:
            # Try AI generation first if available
            if deepseek_client:
                return self.generate_ai_followup_message(lead, stage)
            else:
                # Fallback to templates if no AI
                return self.generate_template_followup_message(lead, stage)
        except Exception as e:
            logger.error(f"AI message generation failed: {str(e)}, falling back to template")
            return self.generate_template_followup_message(lead, stage)
    
    def generate_ai_followup_message(self, lead, stage):
        """Use DeepSeek AI to generate contextually appropriate automatic follow-up message"""
        customer_name = lead.customer_name or "there"
        context = self.get_lead_context(lead)
        
        # Get conversation history for context
        recent_messages = self.get_recent_conversation_summary(lead)
        
        # Define stage-specific guidance
        stage_guidance = {
            'day_1': {
                'tone': 'friendly and gentle',
                'goal': 'gentle check-in without being pushy',
                'time_reference': 'recently',
                'urgency': 'low',
            },
            'day_3': {
                'tone': 'understanding and patient',
                'goal': 'acknowledge they may be busy, offer flexibility',
                'time_reference': 'a few days ago',
                'urgency': 'low-medium',
            },
            'week_1': {
                'tone': 'professional with a helpful offer',
                'goal': 'create value, mention benefits or offer',
                'time_reference': 'last week',
                'urgency': 'medium',
            },
            'week_2': {
                'tone': 'warm and understanding',
                'goal': 'final soft attempt, offer to pause outreach',
                'time_reference': 'a couple weeks ago',
                'urgency': 'medium',
            },
            'month_1': {
                'tone': 'casual re-engagement',
                'goal': 'fresh start, no pressure',
                'time_reference': 'a while back',
                'urgency': 'low',
            }
        }
        
        guidance = stage_guidance.get(stage, stage_guidance['day_1'])
        
        prompt = f"""You are Sarah, a professional and friendly appointment assistant for a luxury plumbing company in South Africa.

CONTEXT:
- Customer name: {customer_name}
- Service interested in: {context['service'] or 'plumbing services'}
- Information collected so far: {context['progress']}
- Relevant offer: {context['offer']}
- Last contacted: {guidance['time_reference']}
- This is AUTOMATIC follow-up attempt: {stage.replace('_', ' ')}
- Recent conversation: {recent_messages}

TASK:
Write a WhatsApp AUTOMATIC follow-up message with these requirements:

TONE: {guidance['tone']}
GOAL: {guidance['goal']}
URGENCY LEVEL: {guidance['urgency']}

MESSAGE REQUIREMENTS:
1. Start with "Hi {customer_name}," (always use this exact format)
2. Acknowledge the gap in communication naturally based on: {guidance['time_reference']}
3. Reference their specific service interest: {context['service'] or 'plumbing needs'}
4. Include the next piece of information you still need if applicable: {context['progress']}
5. Make it conversational and warm, not salesy
6. Include a clear but gentle call-to-action
7. Keep it concise (2-3 short sentences max)
8. End with "- Homebase Plumbers"
9. Use South African English spelling and phrasing
10. Include ONE emoji maximum (optional, only if it feels natural)

AVOID:
- Being too formal or corporate
- Apologizing excessively  
- Being pushy or desperate
- Using multiple emojis
- Long paragraphs
- Complex language

Generate ONLY the message text, nothing else."""

        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system", 
                    "content": "You are Sarah, a professional appointment assistant. Write natural, warm WhatsApp messages for automatic follow-ups. Keep messages short and conversational."
                },
                {
                    "role": "user", 
                    "content": prompt
                }
            ],
            temperature=0.8,  # Higher temperature for more natural variation
            max_tokens=200
        )
        
        ai_message = response.choices[0].message.content.strip()
        
        # Log the AI-generated message for monitoring
        logger.info(f"AI generated AUTOMATIC {stage} follow-up for lead {lead.id}")
        
        return {
            'message': ai_message,
            'ai_generated': True,
            'template_fallback': False
        }
    
    def generate_template_followup_message(self, lead, stage):
        """Fallback template-based message generation for automatic follow-ups"""
        customer_name = lead.customer_name or "there"
        context = self.get_lead_context(lead)
        
        messages = {
            'day_1': f"""Hi {customer_name},

I noticed we started discussing your {context['service'] or 'plumbing needs'} recently, but I haven't heard back from you.

{context['progress']}

Are you still interested? Just reply with "YES" to continue, or let me know if now isn't a good time.

- Homebase Plumbers""",

            'day_3': f"""Hi {customer_name},

Just following up on your {context['service'] or 'plumbing project'}.

I understand you might be busy, but I wanted to check if you're still interested in getting this done?

{context['progress']}

Reply "YES" to continue or "LATER" if you'd prefer I check back in a few weeks.

- Homebase Plumbers""",

            'week_1': f"""Hi {customer_name},

It's been a week since we last spoke about your {context['service'] or 'plumbing needs'}.

I wanted to reach out one more time to see if you'd like to move forward.

{context['offer']}

Let me know if you're interested - just reply "YES" 👍

- Homebase Plumbers""",

            'week_2': f"""Hi {customer_name},

I hope all is well! 

I'm checking in about your {context['service'] or 'plumbing project'} one last time.

If you're still interested, we'd love to help. If timing isn't right, no worries - just let us know and we can follow up later.

Reply "YES" to continue or "NOT NOW" and I'll reach out in a month.

- Homebase Plumbers""",

            'month_1': f"""Hi {customer_name},

It's been a while! I wanted to see if you're still considering your {context['service'] or 'plumbing project'}.

We're here whenever you're ready.

{context['offer']}

Just reply if you'd like to discuss or book an appointment.

Thanks!
- Homebase Plumbers"""
        }
        
        return {
            'message': messages.get(stage, messages['day_1']),
            'ai_generated': False,
            'template_fallback': True
        }
    
    def get_recent_conversation_summary(self, lead):
        """Get a summary of recent conversation for AI context"""
        try:
            if not lead.conversation_history:
                return "No previous conversation"
            
            # Get last 3 messages
            recent = lead.conversation_history[-3:] if len(lead.conversation_history) > 3 else lead.conversation_history
            
            summary_parts = []
            for msg in recent:
                role = "Customer" if msg.get('role') == 'user' else "Sarah"
                content = msg.get('content', '')[:100]  # First 100 chars
                summary_parts.append(f"{role}: {content}")
            
            return " | ".join(summary_parts) if summary_parts else "No previous conversation"
            
        except Exception as e:
            logger.error(f"Error getting conversation summary: {str(e)}")
            return "Unable to load conversation history"

    def get_lead_context(self, lead):
        """Get context about what information we have for the lead"""
        context = {
            'service': None,
            'progress': '',
            'offer': ''
        }
        
        # Service type
        if lead.project_type:
            service_map = {
                'bathroom_renovation': 'bathroom renovation',
                'kitchen_renovation': 'kitchen renovation',
                'new_plumbing_installation': 'new plumbing installation'
            }
            context['service'] = service_map.get(lead.project_type, lead.project_type.replace('_', ' '))
        
        # Progress summary
        missing = []
        if not lead.customer_area:
            missing.append('your area')
        if not lead.property_type:
            missing.append('property type')
        if not lead.timeline:
            missing.append('when you want it done')
        
        if missing:
            context['progress'] = f"I still need to know: {', '.join(missing)}."
        else:
            context['progress'] = "We have most of your details - just need to schedule the appointment!"
        
        # Offer based on what we know
        if lead.has_plan:
            context['offer'] = "We're ready to review your plan and provide a quote."
        elif lead.customer_area:
            context['offer'] = f"We serve your area ({lead.customer_area}) and can schedule a site visit anytime."
        else:
            context['offer'] = "We offer free consultations and competitive pricing."
        
        return context

    def clean_phone_number(self, phone):
        """Clean phone number for WhatsApp Cloud API"""
        return phone.replace('whatsapp:', '').replace('+', '').strip()