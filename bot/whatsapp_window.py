"""
bot/whatsapp_window.py
======================
Central utility for enforcing WhatsApp's 24-hour free-messaging window.

WhatsApp only allows free-tier outbound messages within 24 hours of the
customer's last inbound message. Sending outside this window will either
fail or incur template-message charges.

Usage
-----
    from bot.whatsapp_window import (
        is_window_open,
        assert_window_open,
        WindowClosedError,
        WINDOW,
    )

    # Check before sending
    if not is_window_open(appointment):
        logger.info("Window closed for %s — skipping", appointment.id)
        continue

    # Or raise on violation (use in strict paths)
    assert_window_open(appointment)  # raises WindowClosedError if closed
"""

from datetime import timedelta
from django.utils import timezone

# WhatsApp free-messaging window
WINDOW: timedelta = timedelta(hours=24)

# Safety buffer: treat window as closed this many seconds before true expiry,
# to avoid sending right as the window closes and the message arriving outside.
SAFETY_BUFFER_SECONDS: int = 300  # 5 minutes


def may_send_proactively(appointment) -> bool:
    """The one gate for a bot-INITIATED send: is it allowed, and is it free?

    Use this for follow-ups, nudges and reminders — anything WE decide to send.
    Do NOT use it to gate a reply to a customer who just wrote in: answering is
    the product, and suppressing an answer to save a fraction of a cent would
    cost the job it was trying to protect.

    Non-Appointment objects have no cost model, so they fall back to the
    permission check alone and behave exactly as before.
    """
    if not is_window_open(appointment):
        return False
    if paid_sends_allowed():
        return True
    return bool(getattr(appointment, 'messaging_is_free', True))


def paid_sends_allowed() -> bool:
    """Whether a send Meta would CHARGE for may go out at all.

    Default False. The owner's rule (2026-08-29) is that nothing about Meta
    messaging costs money: the business runs on click-to-WhatsApp ads, whose
    free entry point covers 72h of every ad conversation, and no paid template
    has ever been sent. Once service messages become chargeable, a PROACTIVE
    send to a lead outside that free window is a purchase, so it waits for a
    free window instead of buying one.

    Set WHATSAPP_ALLOW_PAID_SENDS=true to let high-value proactive sends
    through — an appointment reminder is worth more than a fraction of a cent,
    and it is the one send where skipping can cost a booked job.

    This never gates a REPLY to a customer who just wrote in: answering is the
    product, and Appointment.messaging_is_free is there to make that cost
    visible, not to suppress it.
    """
    from django.conf import settings
    return bool(getattr(settings, 'WHATSAPP_ALLOW_PAID_SENDS', False))


class WindowClosedError(Exception):
    """Raised when an outbound message would fall outside the 24-hour window."""

    def __init__(self, appointment_id, last_inbound, window_expires):
        self.appointment_id = appointment_id
        self.last_inbound = last_inbound
        self.window_expires = window_expires
        super().__init__(
            f"24-hour window closed for appointment {appointment_id}. "
            f"Last inbound: {last_inbound}, window expired: {window_expires}"
        )


def _last_inbound(appointment) -> object:
    """
    Return the most recent timestamp at which the customer sent us a message.
    Checks both last_customer_response and last_inbound_at for compatibility
    with older records where only one field is populated.
    Returns None if the customer has never messaged.
    """
    candidates = [
        getattr(appointment, 'last_customer_response', None),
        getattr(appointment, 'last_inbound_at', None),
    ]
    valid = [ts for ts in candidates if ts is not None]
    return max(valid) if valid else None


def window_expires_at(appointment):
    """
    Return the datetime at which the 24-hour window closes, or None if the
    customer has never messaged (window never opened).
    """
    last = _last_inbound(appointment)
    if last is None:
        return None
    return last + WINDOW


def is_window_open(appointment) -> bool:
    """
    Return True if a FREE-FORM message may be sent right now.

    Defers to Appointment.messaging_window_open, which is the only place that
    knows all three rules: the standard 24h window, the extended 72h CTWA window
    for ad-originated leads, and Meta's authoritative 131047 verdict (a bounced
    send closes the window no matter what our own clock says). This helper used
    to do a bare 24h subtraction, which both under- and over-reported: a CTWA
    lead still inside its 72h window was treated as closed, and a lead Meta had
    already refused was treated as open.

    The 5-minute safety buffer is kept on top, so we never send into the last
    few minutes of a window and have it land outside.

    Returns False if the customer has never messaged us.
    """
    closes_at = getattr(appointment, 'messaging_window_closes_at', None)
    if closes_at is not None:
        # The model property covers the 131047 flag and the 24h/72h choice.
        if not getattr(appointment, 'messaging_window_open', False):
            return False
        return timezone.now() <= closes_at - timedelta(seconds=SAFETY_BUFFER_SECONDS)

    # Fallback for objects that aren't Appointments (or have never been messaged).
    last = _last_inbound(appointment)
    if last is None:
        return False

    effective_window = WINDOW - timedelta(seconds=SAFETY_BUFFER_SECONDS)
    elapsed = timezone.now() - last
    return elapsed <= effective_window


def assert_window_open(appointment) -> None:
    """
    Raise WindowClosedError if the 24-hour window is closed.
    Use this in code paths where sending outside the window is a hard error.
    """
    if not is_window_open(appointment):
        last = _last_inbound(appointment)
        expires = window_expires_at(appointment)
        raise WindowClosedError(
            appointment_id=getattr(appointment, 'id', '?'),
            last_inbound=last,
            window_expires=expires,
        )


def hours_remaining(appointment) -> float:
    """
    Return how many hours remain in the window, or 0.0 if the window is closed.
    Useful for logging / dashboard display.
    """
    last = _last_inbound(appointment)
    if last is None:
        return 0.0
    elapsed_seconds = (timezone.now() - last).total_seconds()
    window_seconds = WINDOW.total_seconds()
    remaining = window_seconds - elapsed_seconds
    return max(0.0, remaining / 3600)


def filter_queryset_by_window(qs):
    """
    Filter a Django queryset of Appointment objects to only those whose
    24-hour window is currently open.

    Uses a DB-level filter for efficiency — no Python loop required.
    This is the preferred way to pre-filter large querysets.

    Usage:
        leads = filter_queryset_by_window(Appointment.objects.filter(...))
    """
    from django.db.models import Q
    cutoff = timezone.now() - WINDOW
    return qs.filter(
        Q(last_customer_response__gte=cutoff) |
        Q(last_inbound_at__gte=cutoff)
    )