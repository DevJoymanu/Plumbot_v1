import logging
import os
from datetime import timedelta

import pytz
from django.core.management.base import BaseCommand
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from openai import OpenAI

from bot.models import Appointment, LeadInteraction, LeadActivityType
from bot.whatsapp_cloud_api import whatsapp_api

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Notify plumber daily about leads with score >= 20 that have not responded "
        "for 26+ hours, including AI conversation summary."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be sent without sending WhatsApp messages.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Optional max number of leads to notify in this run (0 = no limit).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]

        local_tz = pytz.timezone("Africa/Harare")
        now_local = timezone.now().astimezone(local_tz)
        today_local = now_local.date()
        cutoff = timezone.now() - timedelta(hours=26)

        leads = self._stale_priority_leads_queryset(cutoff)
        if limit and limit > 0:
            leads = leads[:limit]

        self.stdout.write(
            self.style.SUCCESS(
                f"Checking stale priority leads at {now_local.strftime('%Y-%m-%d %H:%M %Z')}..."
            )
        )

        sent = 0
        skipped = 0
        errors = 0

        for lead in leads:
            marker = f"[STALE_PRIORITY_ALERT {today_local.isoformat()}]"
            already_notified = LeadInteraction.objects.filter(
                appointment=lead,
                activity_type=LeadActivityType.NOTE,
                created_at__date=today_local,
                note__startswith=marker,
            ).exists()
            if already_notified:
                skipped += 1
                continue

            summary = self._generate_conversation_summary(lead)
            inactivity_hours = self._hours_since_response(lead)
            plumber_number = (lead.plumber_contact_number or "+263610318200").replace(
                "whatsapp:", ""
            )

            message = (
                f"Priority stale lead alert\n"
                f"Score: {lead.lead_score} | Status: {lead.get_lead_status_display()}\n"
                f"No response for: {inactivity_hours:.1f} hours\n"
                f"Customer: {lead.customer_name or 'Unknown Customer'}\n"
                f"Phone: {lead.phone_number}\n"
                f"Service: {lead.project_type or 'Not specified'}\n"
                f"Area: {lead.customer_area or 'Not specified'}\n"
                f"Timeline: {lead.timeline or 'Not specified'}\n"
                f"Follow-up: {lead.get_follow_up_status_display()}\n"
                f"Next follow-up: {lead.next_follow_up_at or 'Not set'}\n\n"
                f"AI Summary:\n{summary}\n\n"
                f"Lead: https://plumbotv1-production.up.railway.app/appointments/{lead.id}/"
            )

            try:
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[DRY RUN] Would notify {plumber_number} for lead {lead.id}"
                        )
                    )
                else:
                    whatsapp_api.send_text_message(plumber_number, message)

                lead.last_priority_alert_summary = message
                lead.last_priority_alert_sent_at = timezone.now()
                lead.save(update_fields=["last_priority_alert_summary", "last_priority_alert_sent_at"])

                LeadInteraction.objects.create(
                    appointment=lead,
                    activity_type=LeadActivityType.NOTE,
                    note=f"{marker}\nSent to {plumber_number}\n\n{message}",
                )
                sent += 1
            except Exception as exc:
                errors += 1
                logger.exception(
                    "Failed to send stale-priority alert for appointment %s", lead.id
                )
                self.stdout.write(
                    self.style.ERROR(f"Failed for lead {lead.id}: {exc}")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Stale-priority notifications complete | sent={sent} skipped={skipped} errors={errors}"
            )
        )

    def _stale_priority_leads_queryset(self, cutoff):
        has_project_type = Case(
            When(Q(project_type__isnull=False) & ~Q(project_type=""), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_property_type = Case(
            When(Q(property_type__isnull=False) & ~Q(property_type=""), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_area = Case(
            When(Q(customer_area__isnull=False) & ~Q(customer_area=""), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_timeline = Case(
            When(Q(timeline__isnull=False) & ~Q(timeline=""), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        has_site_visit = Case(
            When(scheduled_datetime__isnull=False, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )

        qs = (
            Appointment.objects.annotate(
                completed_fields=has_project_type
                + has_property_type
                + has_area
                + has_timeline
                + has_site_visit,
                computed_score=Case(
                    When(scheduled_datetime__isnull=False, then=Value(100)),
                    default=(
                        has_project_type
                        + has_property_type
                        + has_area
                        + has_timeline
                        + has_site_visit
                    )
                    * Value(20),
                    output_field=IntegerField(),
                ),
                last_response_at=Coalesce("last_customer_response", "created_at"),
            )
            .filter(computed_score__gte=20, is_lead_active=True)
            .filter(last_response_at__lte=cutoff)
            .order_by("-computed_score", "last_response_at")
        )
        return qs

    def _hours_since_response(self, lead):
        reference = lead.last_customer_response or lead.created_at
        return (timezone.now() - reference).total_seconds() / 3600

    def _generate_conversation_summary(self, lead):
        history = lead.conversation_history or []
        transcript_lines = []
        for msg in history[-20:]:
            role = "Customer" if msg.get("role") == "user" else "Bot"
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            if content.startswith("[AUTOMATIC FOLLOW-UP]"):
                content = content.replace("[AUTOMATIC FOLLOW-UP]", "").strip()
            if content.startswith("[MANUAL FOLLOW-UP]"):
                content = content.replace("[MANUAL FOLLOW-UP]", "").strip()
            transcript_lines.append(f"{role}: {content[:300]}")

        if not transcript_lines:
            return "No conversation history available."

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            return "DeepSeek summary unavailable: DEEPSEEK_API_KEY not configured."

        try:
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize WhatsApp lead conversations for a plumber. "
                            "Return concise actionable bullets."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Summarize in 3-5 bullets: customer need, details collected, "
                            "objections, and best next step.\n\n"
                            + "\n".join(transcript_lines)
                        ),
                    },
                ],
                temperature=0.2,
                max_tokens=250,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("DeepSeek summary failed for appointment %s: %s", lead.id, exc)
            # Deterministic fallback for operational continuity.
            return " | ".join(transcript_lines[-3:])[:500]
