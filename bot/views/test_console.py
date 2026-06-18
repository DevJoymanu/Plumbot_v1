"""
Web test console — chat with Plumbot from the browser instead of a real phone.

Messages typed here are fed through the EXACT production inbound pipeline
(`handle_text_message`), using a reserved 999-prefixed "test" number that the
WhatsApp client refuses to deliver to (see `bot/test_console.py`). The bot's
replies are read back from the appointment's `conversation_history`, which every
reply path writes to before sending.
"""
import json
import os
import uuid

from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from ..decorators import staff_required
from ..models import Appointment, WhatsAppInboundEvent
from ..test_console import DEFAULT_TEST_NUMBER, is_test_sender

# Extra password gate on top of staff login (the console is no longer linked in
# the nav — reachable by URL only, then unlocked with this password per session).
TEST_CONSOLE_PASSWORD = os.environ.get('TEST_CONSOLE_PASSWORD', 'jones007**')
_CONSOLE_SESSION_KEY = 'test_console_unlocked'


def _console_unlocked(request) -> bool:
    return request.session.get(_CONSOLE_SESSION_KEY) is True


def _sanitize_test_number(raw) -> str:
    """Return a safe test number (digits only, 999-prefixed) or the default.

    Forcing the 999 prefix server-side guarantees the console can never be
    pointed at a real subscriber, no matter what the client posts.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits and is_test_sender(digits):
        return digits
    return DEFAULT_TEST_NUMBER


def _serialize_history(appointment):
    """Flatten conversation_history into render-ready message dicts."""
    out = []
    for entry in (appointment.conversation_history or []):
        if not isinstance(entry, dict):
            continue
        out.append({
            "role": entry.get("role", "assistant"),
            "content": entry.get("content", ""),
            "timestamp": entry.get("timestamp", ""),
            "is_media": bool(entry.get("media_index")),
        })
    return out


@staff_required
def test_console_view(request):
    """Render the chat console page, behind a per-session password gate."""
    # Password submission.
    if request.method == "POST" and "console_password" in request.POST:
        if request.POST.get("console_password") == TEST_CONSOLE_PASSWORD:
            request.session[_CONSOLE_SESSION_KEY] = True
            qs = request.GET.urlencode()
            return redirect(f"{reverse('test_console')}?{qs}" if qs else reverse('test_console'))
        return render(request, "bot/pages/test_console_gate.html",
                      {"error": "Incorrect password. Try again."})

    # Locked until the password is entered this session.
    if not _console_unlocked(request):
        return render(request, "bot/pages/test_console_gate.html", {})

    sender = _sanitize_test_number(request.GET.get("number"))
    appointment = Appointment.objects.filter(phone_number=f"whatsapp:+{sender}").first()
    history = _serialize_history(appointment) if appointment else []
    return render(request, "bot/pages/test_console.html", {
        "active_nav": "test_console",
        "test_number": sender,
        "history": history,
    })


@staff_required
@require_http_methods(["POST"])
def test_console_send(request):
    """Feed one customer message into the production pipeline and reply."""
    if not _console_unlocked(request):
        return JsonResponse({"ok": False, "error": "Console locked"}, status=403)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    sender = _sanitize_test_number(payload.get("number"))
    message = (payload.get("message") or "").strip()
    quoted_id = (payload.get("quoted_id") or "").strip() or None
    if not message:
        return JsonResponse({"ok": False, "error": "Empty message"}, status=400)

    # Imported lazily: whatsapp_webhook pulls in the heavy bot stack.
    from ..whatsapp_webhook import handle_text_message

    message_id = f"wamid.TESTIN{uuid.uuid4().hex}"
    # For a test sender, handle_text_message flushes the batch and generates the
    # reply synchronously, so by the time this returns the reply is in history.
    handle_text_message(
        sender, {"body": message}, message_id=message_id, quoted_id=quoted_id,
    )

    appointment = Appointment.objects.filter(phone_number=f"whatsapp:+{sender}").first()
    history = _serialize_history(appointment) if appointment else []
    return JsonResponse({"ok": True, "history": history, "count": len(history)})


@staff_required
@require_http_methods(["GET"])
def test_console_poll(request):
    """Return conversation history (optionally only entries after an index)."""
    if not _console_unlocked(request):
        return JsonResponse({"ok": False, "error": "Console locked"}, status=403)
    sender = _sanitize_test_number(request.GET.get("number"))
    try:
        after = max(0, int(request.GET.get("after", 0)))
    except (TypeError, ValueError):
        after = 0

    appointment = Appointment.objects.filter(phone_number=f"whatsapp:+{sender}").first()
    history = _serialize_history(appointment) if appointment else []
    return JsonResponse({
        "ok": True,
        "messages": history[after:],
        "count": len(history),
    })


@staff_required
@require_http_methods(["POST"])
def test_console_reset(request):
    """Wipe the test conversation so the next message starts a fresh lead."""
    if not _console_unlocked(request):
        return JsonResponse({"ok": False, "error": "Console locked"}, status=403)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = {}
    sender = _sanitize_test_number(payload.get("number"))

    Appointment.objects.filter(phone_number=f"whatsapp:+{sender}").delete()
    WhatsAppInboundEvent.objects.filter(sender=sender).delete()
    return JsonResponse({"ok": True, "number": sender})
