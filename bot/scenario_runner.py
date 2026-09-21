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
import threading
import uuid
import zlib

from bot.models import Appointment, WhatsAppInboundEvent
from bot.test_console import is_test_sender


# ── Pipeline plumbing (single implementation for REPL, CLI and web) ──────────

def history(sender, tenant=None):
    """The test lead's transcript ON THIS TENANT. The same scenario run as two
    tenants uses the same 999 number, and an unscoped lookup read the OTHER
    tenant's lead, reporting a real reply as silence."""
    leads = Appointment.objects.filter(phone_number=f"whatsapp:+{sender}")
    if tenant is not None:
        leads = leads.filter(tenant=tenant)
    appt = leads.first()
    return (appt.conversation_history or []) if appt else []


def reset_lead(sender, tenant=None):
    """Wipe the test lead so the next message starts a fresh conversation."""
    leads = Appointment.objects.filter(phone_number=f"whatsapp:+{sender}")
    if tenant is not None:
        leads = leads.filter(tenant=tenant)
    leads.delete()
    WhatsAppInboundEvent.objects.filter(sender=sender).delete()


# How long one turn's background work may take before the next turn is sent.
# Test senders have no relay delay, so a reply thread finishes in well under a
# second; this only bounds a thread that is genuinely stuck.
SETTLE_TIMEOUT_SECONDS = 30.0


def _settle(threads_before):
    """Wait for every thread this turn started to finish.

    WHY: several reply paths answer from a daemon thread even for test senders
    (`delayed_response`, the portfolio/photo send). Without this, turn N's
    thread was still writing to the lead when turn N+1 arrived, and turn N+1
    either read a transcript missing turn N's reply or, on SQLite, failed its
    own write outright ("database table is locked") and came back SILENT. The
    replay then reported a bug that was only the harness racing itself, and
    reported it on some runs and not others, which is no use in a gate.

    HOW: joins the threads that did not exist before the turn, against one
    shared deadline, so a stuck thread costs at most SETTLE_TIMEOUT_SECONDS for
    the whole turn rather than per thread. Threads from earlier turns are left
    alone. The media_wait poll in send_message stays as the fallback for a
    reply that arrives by some route this cannot see.
    """
    import time

    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    # Re-scan until nothing new is alive: a reply thread can itself start one
    # (the photo send queues its follow-up), so a single snapshot misses it.
    while time.monotonic() < deadline:
        pending = [t for t in threading.enumerate()
                   if t not in threads_before
                   and t is not threading.current_thread() and t.is_alive()]
        if not pending:
            return
        for t in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            t.join(timeout=remaining)


def send_message(sender, message, media_wait: float = 60.0, tenant=None):
    """Feed one customer message through the production pipeline; return the
    assistant replies generated for this turn (synchronous for 999 senders).

    Media paths (portfolio gallery, catalogue PDF) reply from a background
    thread even for test senders — when the synchronous call produced nothing,
    poll history briefly so those turns aren't reported as silent."""
    import time

    from bot.whatsapp_webhook import handle_text_message

    def _new_replies(before_count):
        entries = history(sender, tenant=tenant)[before_count:]
        return [(e.get("content") or "") for e in entries
                if isinstance(e, dict) and e.get("role") == "assistant"]

    before = len(history(sender, tenant=tenant))
    threads_before = set(threading.enumerate())
    handle_text_message(
        sender, {"body": message},
        message_id=f"wamid.TESTIN{uuid.uuid4().hex}",
        tenant=tenant,
    )
    _settle(threads_before)
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

def run_scenario(name: str, text: str, progress=None, tenant=None,
                 media_wait: float = 60.0) -> dict:
    """Run one scenario end to end and return a structured result:

        {name, sender, passed, failed, turns: [
            {message, replies: [...], checks: [{kind, text, ok}, ...]},
        ]}

    `progress(turn_index, total_turns)` is called before each turn (optional) so
    a UI can show live progress.

    `media_wait` is how long a turn that produced no synchronous reply is polled
    for one, because the gallery/catalogue paths answer from a background thread
    even for test senders. The 60s default is right for a live run; the OFFLINE
    gate passes 0, since with a mocked DeepSeek there is no thread to wait for
    and 30 scenarios x 60s of polling would make the commit gate unusable.
    """
    turns = parse_scenario(text, origin=name)
    sender = scenario_number(name)
    reset_lead(sender, tenant=tenant)

    result = {"name": name, "sender": sender, "passed": 0, "failed": 0, "turns": []}
    for i, (msg, checks) in enumerate(turns):
        if progress:
            progress(i, len(turns))
        replies = send_message(sender, msg, tenant=tenant, media_wait=media_wait)
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
