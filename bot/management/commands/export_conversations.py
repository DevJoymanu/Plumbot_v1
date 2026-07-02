"""
Export every real lead conversation to JSONL for analysis / example mining.

One JSON object per lead: outcome metadata + the full transcript. Phone numbers
are masked by default (last 3 digits kept) — pass --unmasked to keep them.

Usage:
    python manage.py export_conversations                 # -> exports/conversations.jsonl
    python manage.py export_conversations --out my.jsonl --min-turns 3
"""
import json
import os

from django.core.management.base import BaseCommand

from bot.models import Appointment


def _mask(phone: str) -> str:
    digits = (phone or "").replace("whatsapp:", "").replace("+", "")
    return f"...{digits[-3:]}" if digits else ""


def _outcome(appt) -> str:
    """Coarse outcome bucket for funnel analysis."""
    if appt.status == "confirmed":
        return "booked"
    if appt.status == "cancelled":
        return "cancelled"
    if getattr(appt, "is_delayed", False):
        return "parked_delayed"
    history = appt.conversation_history or []
    last_role = next(
        (m.get("role") for m in reversed(history) if isinstance(m, dict)), None
    )
    # Bot spoke last and the lead never came back -> ghosted on that turn.
    return "ghosted_after_bot" if last_role == "assistant" else "open"


class Command(BaseCommand):
    help = "Export all real lead conversations to JSONL for analysis."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="exports/conversations.jsonl")
        parser.add_argument("--min-turns", type=int, default=1)
        parser.add_argument("--unmasked", action="store_true",
                            help="Keep full phone numbers (default: masked)")

    def handle(self, *args, **opts):
        os.makedirs(os.path.dirname(opts["out"]) or ".", exist_ok=True)
        qs = (Appointment.objects
              .exclude(phone_number__startswith="whatsapp:+999")
              .order_by("created_at"))
        written = 0
        with open(opts["out"], "w", encoding="utf-8") as fh:
            for appt in qs.iterator():
                history = [m for m in (appt.conversation_history or [])
                           if isinstance(m, dict) and (m.get("content") or "").strip()]
                if len(history) < opts["min_turns"]:
                    continue
                row = {
                    "id": appt.pk,
                    "phone": (appt.phone_number if opts["unmasked"]
                              else _mask(appt.phone_number)),
                    "status": appt.status,
                    "outcome": _outcome(appt),
                    "project_type": appt.project_type,
                    "project_description": appt.project_description,
                    "area": appt.customer_area,
                    "scheduled": (appt.scheduled_datetime.isoformat()
                                  if appt.scheduled_datetime else None),
                    "is_delayed": bool(getattr(appt, "is_delayed", False)),
                    "created_at": (appt.created_at.isoformat()
                                   if appt.created_at else None),
                    "turns": len(history),
                    "transcript": [
                        {"role": m.get("role"), "content": m.get("content"),
                         "ts": m.get("timestamp")}
                        for m in history
                    ],
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
        print(f"Exported {written} conversations -> {opts['out']}")
