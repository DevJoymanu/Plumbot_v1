import logging
import os
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from openai import OpenAI

from bot.models import Appointment

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Generate and store DeepSeek conversation summaries for unconfirmed leads "
        "that are older than 24 hours."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be summarized without writing changes.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Optional max number of leads to process (0 = no limit).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        cutoff = timezone.now() - timedelta(hours=24)

        leads = (
            Appointment.objects.filter(status__in=["pending", "in_progress"])
            .filter(created_at__lte=cutoff)
            .exclude(conversation_history=[])
            .order_by("created_at")
        )
        if limit and limit > 0:
            leads = leads[:limit]

        processed = 0
        skipped = 0
        errors = 0

        for lead in leads:
            already_summarized = lead.last_unconfirmed_summary_at is not None
            if already_summarized:
                skipped += 1
                continue

            summary = self._generate_conversation_summary(lead)
            timestamp_text = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M')
            note_text = f"[UNCONFIRMED_24H_SUMMARY {timestamp_text}] {summary}"

            try:
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[DRY RUN] Would summarize unconfirmed lead {lead.id}"
                        )
                    )
                else:
                    lead.last_unconfirmed_summary_text = summary
                    lead.last_unconfirmed_summary_at = timezone.now()
                    existing_notes = lead.admin_notes or ""
                    lead.admin_notes = f"{note_text}\n{existing_notes}".strip()
                    lead.save(
                        update_fields=[
                            "last_unconfirmed_summary_text",
                            "last_unconfirmed_summary_at",
                            "admin_notes",
                        ]
                    )
                processed += 1
            except Exception as exc:
                errors += 1
                logger.exception(
                    "Failed to summarize unconfirmed appointment %s", lead.id
                )
                self.stdout.write(self.style.ERROR(f"Failed for lead {lead.id}: {exc}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Unconfirmed 24h summaries complete | processed={processed} skipped={skipped} errors={errors}"
            )
        )

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
            logger.warning(
                "DeepSeek summary failed for appointment %s: %s", lead.id, exc
            )
            return " | ".join(transcript_lines[-3:])[:500]
