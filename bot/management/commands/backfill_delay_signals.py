from django.core.management.base import BaseCommand

from bot.models import Appointment
from bot.out_of_scope_handler import detect_delay_signal_message, mark_delay_signal


TRIVIAL_ACKS = {
    "ok", "okay", "k", "kk", "sure", "sharp", "shap", "cool", "noted",
    "thanks", "thank you", "no problem", "no worries", "alright",
    "👍", "🙏", "yes", "yep", "ya",
}


def _is_trivial_ack(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in TRIVIAL_ACKS


def _history_shows_active_delay(appointment: Appointment):
    history = appointment.conversation_history or []
    active = False
    trigger_message = ""

    for msg in history:
        if msg.get("role") != "user":
            continue

        content = (msg.get("content") or "").strip()
        if not content or content.startswith("["):
            continue

        delay_check = detect_delay_signal_message(content, appointment)
        if delay_check.get("is_delay"):
            active = True
            trigger_message = content
            continue

        if active and not _is_trivial_ack(content):
            active = False
            trigger_message = ""

    return active, trigger_message


class Command(BaseCommand):
    help = "Scan conversation history and mark appointments with active delay signals."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="Only scan the newest N appointments.")
        parser.add_argument("--dry-run", action="store_true", help="Show matches without writing notes.")
        parser.add_argument(
            "--only-missing",
            action="store_true",
            help="Skip appointments that already have [DELAY_SIGNAL].",
        )

    def handle(self, *args, **options):
        qs = Appointment.objects.exclude(conversation_history=[]).order_by("-created_at")
        if options["only_missing"]:
            qs = qs.exclude(internal_notes__contains="[DELAY_SIGNAL]")
        if options["limit"]:
            qs = qs[: options["limit"]]

        scanned = 0
        marked = 0

        for appointment in qs:
            scanned += 1
            active, trigger_message = _history_shows_active_delay(appointment)
            if not active:
                continue

            self.stdout.write(
                f"delay-signal appointment={appointment.id} phone={appointment.phone_number} "
                f"message={trigger_message[:80]!r}"
            )

            if options["dry_run"]:
                continue

            if mark_delay_signal(appointment, trigger_message):
                marked += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Scanned {scanned} appointment(s); marked {marked} delay signal(s)."
            )
        )
