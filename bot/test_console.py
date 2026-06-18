"""
Web test-console support — lets staff converse with Plumbot from the browser
instead of a real WhatsApp device.

Design goals (and how they're met):

* **Never message a real person.** A "test sender" is identified purely by its
  phone-number PREFIX (``999...``). The ITU reserves country code 999 / the 99x
  range for future global services, so no real subscriber number begins with
  these digits. The WhatsApp client hard-stops any outbound to such a number.
* **Stateless detection.** Identification is pattern-based, not a registry, so it
  behaves identically across gunicorn workers and the background reply threads
  that actually perform the sends.
* **Faithful pipeline.** Messages typed into the console run the EXACT production
  inbound pipeline (``handle_text_message`` → batch → router → reply paths). The
  only differences for a test sender are: outbound is short-circuited instead of
  hitting Meta, the relay/batch delays are skipped, and plumber/team side-alerts
  are muted so testing never pages the real team.

The customer-facing transcript itself is read back from
``Appointment.conversation_history`` (every reply path logs the assistant turn
before sending), so the UI does not depend on the in-memory capture buffer below
— that buffer is best-effort, for logging/debugging only.
"""
import threading
import time

# Numbers in this range are console test lines, never real subscribers.
TEST_NUMBER_PREFIX = "999"
DEFAULT_TEST_NUMBER = "999000000001"

# Best-effort record of what would have gone to WhatsApp for a test recipient.
# Keyed by recipient digits. Not relied on for the UI transcript (that comes from
# conversation_history); kept for debugging and possible future media preview.
_captured_lock = threading.Lock()
_captured: dict = {}      # digits -> list[dict]
_seq = 0


def _digits(phone) -> str:
    """Normalise any phone format to bare digits."""
    return (phone or "").replace("whatsapp:", "").replace("+", "").strip()


def is_test_sender(phone) -> bool:
    """True when ``phone`` is a console test line (never a real subscriber)."""
    return _digits(phone).startswith(TEST_NUMBER_PREFIX)


def _fake_wamid() -> str:
    global _seq
    with _captured_lock:
        _seq += 1
        return f"wamid.TEST{int(time.time())}{_seq:04d}"


def record_outbound(to: str, kind: str, **fields) -> dict:
    """Capture an outbound message destined for a test recipient.

    Returns a WhatsApp-Cloud-API-shaped result dict (``{'messages': [{'id': ...}]}``)
    carrying a fake WAMID, so callers that stamp the returned id onto conversation
    history (``attach_message_id`` / ``record_sent_media``) keep working unchanged.
    """
    digits = _digits(to)
    wamid = _fake_wamid()
    entry = {"wamid": wamid, "kind": kind, "to": digits, "ts": time.time(), **fields}
    with _captured_lock:
        _captured.setdefault(digits, []).append(entry)
    preview = fields.get("text") or fields.get("caption") or fields.get("filename") or kind
    print(f"[test-console] captured {kind} -> +{digits}: {str(preview)[:80]}")
    return {"messages": [{"id": wamid}]}
