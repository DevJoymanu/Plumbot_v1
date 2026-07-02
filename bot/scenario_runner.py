"""
Shared scenario engine — parse + run conversation scenarios through the EXACT
production pipeline (real DeepSeek) on isolated 999 test numbers.

Used by:
  * `python manage.py chat` (interactive REPL / --script replay)
  * `python manage.py run_scenarios` (CLI suite with pass/fail report)
  * the Scenario Lab web page (run at the click of a button)

Scenario text format, line by line:
    # comment                    ignored
    > customer message           a turn the customer sends (bare lines work too)
    expect: some text            this turn's reply MUST contain the text (case-insensitive)
    reject: some text            this turn's reply must NOT contain the text
"""
import uuid
import zlib

from bot.models import Appointment, WhatsAppInboundEvent
from bot.test_console import is_test_sender


# ── Pipeline plumbing (single implementation for REPL, CLI and web) ──────────

def history(sender):
    appt = Appointment.objects.filter(phone_number=f"whatsapp:+{sender}").first()
    return (appt.conversation_history or []) if appt else []


def reset_lead(sender):
    """Wipe the test lead so the next message starts a fresh conversation."""
    Appointment.objects.filter(phone_number=f"whatsapp:+{sender}").delete()
    WhatsAppInboundEvent.objects.filter(sender=sender).delete()


def send_message(sender, message, media_wait: float = 60.0):
    """Feed one customer message through the production pipeline; return the
    assistant replies generated for this turn (synchronous for 999 senders).

    Media paths (portfolio gallery, catalogue PDF) reply from a background
    thread even for test senders — when the synchronous call produced nothing,
    poll history briefly so those turns aren't reported as silent."""
    import time

    from bot.whatsapp_webhook import handle_text_message

    def _new_replies(before_count):
        entries = history(sender)[before_count:]
        return [e.get("content", "") for e in entries
                if isinstance(e, dict) and e.get("role") == "assistant"]

    before = len(history(sender))
    handle_text_message(
        sender, {"body": message},
        message_id=f"wamid.TESTIN{uuid.uuid4().hex}",
    )
    replies = _new_replies(before)
    waited = 0.0
    while not replies and waited < media_wait:
        time.sleep(2)
        waited += 2
        replies = _new_replies(before)
    return replies


def scenario_number(name: str) -> str:
    """Deterministic per-scenario 999 test line so runs are isolated."""
    digest = zlib.crc32((name or "scenario").encode("utf-8")) % 10 ** 9
    number = f"999{digest:09d}"
    assert is_test_sender(number)
    return number


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_scenario(text: str, origin: str = "scenario"):
    """Parse scenario text into [(message, [(kind, check_text), ...]), ...].

    Raises ValueError on an expectation with no preceding customer message.
    """
    turns = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("expect:") or low.startswith("reject:"):
            kind, _, check_text = line.partition(":")
            if not turns:
                raise ValueError(
                    f"{origin}: '{line}' appears before any customer message"
                )
            turns[-1][1].append((kind.strip().lower(), check_text.strip()))
        else:
            msg = line[1:].strip() if line.startswith(">") else line
            if msg:
                turns.append((msg, []))
    return turns


# ── Execution ─────────────────────────────────────────────────────────────────

def run_scenario(name: str, text: str, progress=None) -> dict:
    """Run one scenario end to end and return a structured result:

        {name, sender, passed, failed, turns: [
            {message, replies: [...], checks: [{kind, text, ok}, ...]},
        ]}

    `progress(turn_index, total_turns)` is called before each turn (optional) so
    a UI can show live progress.
    """
    turns = parse_scenario(text, origin=name)
    sender = scenario_number(name)
    reset_lead(sender)

    result = {"name": name, "sender": sender, "passed": 0, "failed": 0, "turns": []}
    for i, (msg, checks) in enumerate(turns):
        if progress:
            progress(i, len(turns))
        replies = send_message(sender, msg)
        reply_text = "\n".join(replies)
        low = reply_text.lower()
        turn = {"message": msg, "replies": replies, "checks": []}
        for kind, check_text in checks:
            ok = (check_text.lower() in low) if kind == "expect" \
                 else (check_text.lower() not in low)
            turn["checks"].append({"kind": kind, "text": check_text, "ok": ok})
            result["passed" if ok else "failed"] += 1
        result["turns"].append(turn)
    return result
