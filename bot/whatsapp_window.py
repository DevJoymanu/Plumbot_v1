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
    Return True if we are currently inside the 24-hour free-messaging window
    for this appointment (i.e. the customer messaged us within the last 24h).

    Returns False if:
    - The customer has never messaged us.
    - The last inbound message was more than 24 hours ago (minus safety buffer).
    """
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