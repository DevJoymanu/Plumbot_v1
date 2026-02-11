# Save this as: yourapp/management/commands/send_followups.py
# Create the directory structure: yourapp/management/commands/

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from bot.models import Appointment  # Replace 'yourapp' with your actual app name
from bot.whatsapp_cloud_api import whatsapp_api
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Check and send follow-up messages to non-responsive leads'

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
        
        self.stdout.write(self.style.SUCCESS('🔍 Starting follow-up check...'))
        
        if dry_run:
            self.stdout.write(self.style.WARNING('🧪 DRY RUN MODE - No messages will be sent'))
        
        # Get leads that need follow-up
        leads = self.get_leads_needing_followup(force)
        
        self.stdout.write(f'📊 Found {leads.count()} leads needing follow-up')
        
        results = {
            'sent': 0,
            'skipped': 0,
            'errors': 0,
            'completed': 0,
        }
        
        for lead in leads:
            try:
                result = self.process_lead_followup(lead, dry_run)
                results[result] += 1
                
            except Exception as e:
                logger.error(f"Error processing lead {lead.id}: {str(e)}")
                results['errors'] += 1
                self.stdout.write(
                    self.style.ERROR(f'❌ Error with lead {lead.id}: {str(e)}')
                )
        
        # Print summary
        self.stdout.write(self.style.SUCCESS('\n📊 FOLLOW-UP SUMMARY:'))
        self.stdout.write(f"✅ Sent: {results['sent']}")
        self.stdout.write(f"⏭️  Skipped: {results['skipped']}")
        self.stdout.write(f"✔️  Completed: {results['completed']}")
        self.stdout.write(f"❌ Errors: {results['errors']}")
        
        if dry_run:
            self.stdout.write(self.style.WARNING('\n🧪 This was a dry run - no actual messages sent'))

    def get_leads_needing_followup(self, force=False):
        """Get all leads that need a follow-up message"""
        now = timezone.now()
        
        # Base criteria: incomplete appointments that are still active leads
        leads = Appointment.objects.filter(
            is_lead_active=True,
            status='pending',  # Not yet confirmed
        ).exclude(
            followup_stage='completed'
        ).exclude(
            followup_stage='responded'
        )
        
        # Exclude leads that have responded recently (within last 12 hours)
        # Exclude leads that responded in last 2 minutes
        recent_response_cutoff = now - timedelta(minutes=7)

        leads = leads.exclude(
            last_customer_response__gte=recent_response_cutoff
        )
        
        # If not forcing, exclude leads we already followed up with today
        if not force:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            leads = leads.exclude(
                last_followup_sent__gte=today_start
            )
        
        return leads.order_by('last_customer_response', 'created_at')

    def process_lead_followup(self, lead, dry_run=False):
        """Process a single lead for follow-up"""
        now = timezone.now()
        
        # Determine which follow-up stage this lead should be at
        next_stage = self.calculate_followup_stage(lead, now)
        
        if next_stage is None:
            # Not ready for follow-up yet
            return 'skipped'
        
        if next_stage == 'completed':
            # Mark as completed
            if not dry_run:
                lead.followup_stage = 'completed'
                lead.is_lead_active = False
                lead.lead_marked_inactive_at = now
                lead.save()
            
            self.stdout.write(
                self.style.WARNING(f'✔️  Lead {lead.id} marked as completed (no more follow-ups)')
            )
            return 'completed'
        
        # Generate and send the follow-up message
        message = self.generate_followup_message(lead, next_stage)
        
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(f'🧪 Would send {next_stage} follow-up to {lead.phone_number}:')
            )
            self.stdout.write(f'   "{message[:100]}..."')
        else:
            # Send the actual message
            clean_phone = self.clean_phone_number(lead.phone_number)
            whatsapp_api.send_text_message(clean_phone, message)
            
            # Update lead record
            lead.last_followup_sent = now
            lead.followup_count += 1
            lead.followup_stage = next_stage
            lead.save()
            
            # Add to conversation history
            lead.add_conversation_message('assistant', message)
            
            self.stdout.write(
                self.style.SUCCESS(f'✅ Sent {next_stage} follow-up to {lead.phone_number}')
            )
        
        return 'sent'
    
    def calculate_followup_stage(self, lead, now):
        """Calculate which follow-up stage the lead should be at"""

        # Determine time since last interaction
        if lead.last_customer_response:
            time_since = now - lead.last_customer_response
        elif lead.last_followup_sent:
            time_since = now - lead.last_followup_sent
        else:
            time_since = now - lead.created_at

        minutes_since = time_since.total_seconds() / 60
        current_stage = lead.followup_stage

        # 🔥 NEW: 2-minute immediate follow-up
        if current_stage in ['none', 'responded'] and minutes_since >= 7:
            return 'day_1'
        
        elif current_stage == 'day_1':
            # Second follow-up: 3 days after first follow-up
            if lead.last_followup_sent:
                days_since_followup = (now - lead.last_followup_sent).total_seconds() / (3600 * 24)
                if days_since_followup >= 3:
                    return 'day_3'
        
        elif current_stage == 'day_3':
            # Third follow-up: 1 week after second follow-up
            if lead.last_followup_sent:
                days_since_followup = (now - lead.last_followup_sent).total_seconds() / (3600 * 24)
                if days_since_followup >= 7:
                    return 'week_1'
        
        elif current_stage == 'week_1':
            # Fourth follow-up: 2 weeks after third follow-up
            if lead.last_followup_sent:
                days_since_followup = (now - lead.last_followup_sent).total_seconds() / (3600 * 24)
                if days_since_followup >= 14:
                    return 'week_2'
        
        elif current_stage == 'week_2':
            # Final follow-up: 1 month after fourth follow-up
            if lead.last_followup_sent:
                days_since_followup = (now - lead.last_followup_sent).total_seconds() / (3600 * 24)
                if days_since_followup >= 30:
                    return 'month_1'
        
        elif current_stage == 'month_1':
            # No more follow-ups after the 1-month follow-up
            return 'completed'
        
        # Not ready for next follow-up yet
        return None

    def generate_followup_message(self, lead, stage):
        """Generate appropriate follow-up message based on stage"""
        customer_name = lead.customer_name or "there"
        
        # Get context about what we know so far
        context = self.get_lead_context(lead)
        
        messages = {
            'day_1': f"""Hi {customer_name},

I noticed we started discussing your {context['service'] or 'plumbing needs'} yesterday, but I haven't heard back from you.

{context['progress']}

Are you still interested? Just reply with "YES" to continue, or let me know if now isn't a good time.

- Sarah & team""",

            'day_3': f"""Hi {customer_name},

Just following up on your {context['service'] or 'plumbing project'}.

I understand you might be busy, but I wanted to check if you're still interested in getting this done?

{context['progress']}

Reply "YES" to continue or "LATER" if you'd prefer I check back in a few weeks.

- Sarah & team""",

            'week_1': f"""Hi {customer_name},

It's been a week since we last spoke about your {context['service'] or 'plumbing needs'}.

I wanted to reach out one more time to see if you'd like to move forward.

{context['offer']}

Let me know if you're interested - just reply "YES" 👍

- Sarah & team""",

            'week_2': f"""Hi {customer_name},

I hope all is well! 

I'm checking in about your {context['service'] or 'plumbing project'} one last time.

If you're still interested, we'd love to help. If timing isn't right, no worries - just let us know and we can follow up later.

Reply "YES" to continue or "NOT NOW" and I'll reach out in a month.

- Sarah & team""",

            'month_1': f"""Hi {customer_name},

It's been a while! I wanted to see if you're still considering your {context['service'] or 'plumbing project'}.

We're here whenever you're ready.

{context['offer']}

Just reply if you'd like to discuss or book an appointment.

Thanks!
- Sarah & team"""
        }
        
        return messages.get(stage, messages['day_1'])

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