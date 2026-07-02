"""
Local Plumbot chat — converse with the bot from your terminal, or replay a
scripted scenario, WITHOUT WhatsApp, Railway, or a phone.

Runs the EXACT production inbound pipeline (`handle_text_message` → batch →
router → reply paths) with a reserved 999-prefixed test number, the same
mechanism as the web test console: replies are generated synchronously, the
WhatsApp client hard-stops any real send for 999 numbers, and plumber alerts
are muted. Uses the real DeepSeek key from your .env, so classifier behaviour
matches production.

Usage:
    # Interactive REPL (Ctrl+C or /quit to exit; /reset starts a fresh lead)
    python manage.py chat

    # Replay a scenario file (one customer message per line; # = comment)
    python manage.py chat --script scenarios/new_install.txt

    # Fresh lead first, custom test line (multiple parallel scenarios)
    python manage.py chat --reset --number 999000000007

Windows note: set PYTHONIOENCODING=utf-8 first — the handlers print emoji.
"""
import os

from django.core.management.base import BaseCommand

from bot.scenario_runner import history as _history, reset_lead, send_message
from bot.test_console import DEFAULT_TEST_NUMBER, is_test_sender


def _send(sender, message):
    """Feed one customer message through the production pipeline; print replies."""
    replies = send_message(sender, message)
    if not replies:
        print("  (no reply — conversation complete or message suppressed)")
    for r in replies:
        print(f"\nPLUMBOT:\n{r}\n")
    return replies


def _reset(sender):
    reset_lead(sender)
    print(f"[reset] fresh lead for +{sender}")


class Command(BaseCommand):
    help = "Chat with Plumbot locally (interactive REPL or scripted scenario replay)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--number", default=DEFAULT_TEST_NUMBER,
            help="Test line to use (999-prefixed; default %(default)s). "
                 "Use different numbers to keep parallel scenarios separate.",
        )
        parser.add_argument(
            "--script", default=None,
            help="Path to a scenario file: one customer message per line, "
                 "blank lines and lines starting with # are skipped.",
        )
        parser.add_argument(
            "--reset", action="store_true",
            help="Delete the test lead first so the conversation starts fresh.",
        )

    def handle(self, *args, **opts):
        sender = "".join(ch for ch in opts["number"] if ch.isdigit())
        if not is_test_sender(sender):
            self.stderr.write(
                f"Refusing to run with non-test number '{opts['number']}' — "
                "test lines must start with 999 so a real subscriber can never "
                "be messaged."
            )
            return

        if opts["reset"]:
            _reset(sender)

        if opts["script"]:
            self._run_script(sender, opts["script"])
        else:
            self._run_repl(sender)

    # ── Scenario replay ─────────────────────────────────────────────────────
    def _run_script(self, sender, path):
        if not os.path.exists(path):
            self.stderr.write(f"Scenario file not found: {path}")
            return
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
        messages = [ln for ln in lines if ln and not ln.startswith("#")]
        if not messages:
            self.stderr.write("Scenario file has no messages.")
            return
        print(f"[script] {len(messages)} message(s) -> +{sender}\n")
        for msg in messages:
            print(f"CUSTOMER:\n{msg}")
            _send(sender, msg)
        print("[script] done — transcript above is exactly what WhatsApp would show.")

    # ── Interactive REPL ────────────────────────────────────────────────────
    def _run_repl(self, sender):
        print(
            f"Plumbot local chat on +{sender} (production pipeline, real DeepSeek).\n"
            "Commands: /reset = fresh lead, /quit = exit.\n"
        )
        # Show existing context so resuming a conversation isn't confusing.
        existing = _history(sender)
        if existing:
            print(f"[resuming — {len(existing)} prior turns; /reset for a fresh lead]\n")
        while True:
            try:
                msg = input("CUSTOMER> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nbye")
                return
            if not msg:
                continue
            if msg.lower() in ("/quit", "/q", "/exit"):
                return
            if msg.lower() == "/reset":
                _reset(sender)
                continue
            _send(sender, msg)
