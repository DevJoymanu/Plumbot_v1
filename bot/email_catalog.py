"""
bot/email_catalog.py
====================
A single catalogue of the customer-facing emails, mirroring the follow-up test
suite but aimed at a specific lead. Each entry exposes:

    key   -> {
        'label':       short human title,
        'description': what the email is / when it's normally sent,
        'build':       callable(apt) -> (subject, html)   # for preview/edit
        'send':        callable(apt) -> bool              # real send to the lead
    }

The build/send callables delegate to bot.customer_emails so previews use the
exact templates that go out — one source of truth.
"""

from collections import OrderedDict

from bot.customer_emails import (
    build_booking_confirmation_email, send_booking_confirmation_email,
    build_delay_followup_email,      send_delay_followup_email,
    build_delay_last_check_email,    send_delay_last_check_email,
    build_customer_reminder_email,   send_customer_reminder_email,
    send_delay_quote_email,
)


def _reminder(rtype):
    return (
        lambda apt: build_customer_reminder_email(apt, rtype),
        lambda apt: send_customer_reminder_email(apt, rtype),
    )


_two_days_b, _two_days_s         = _reminder('two_days')
_one_day_b, _one_day_s           = _reminder('one_day')
_morning_b, _morning_s           = _reminder('morning')
_two_hours_b, _two_hours_s       = _reminder('two_hours')
_thirty_mins_b, _thirty_mins_s   = _reminder('thirty_mins')


EMAIL_CATALOG = OrderedDict([
    ('booking_confirmation', {
        'label': 'Booking confirmation',
        'description': 'Sent right after a booking is confirmed — appointment details and what to expect.',
        'build': build_booking_confirmation_email,
        'send':  send_booking_confirmation_email,
    }),
    ('delay_followup', {
        'label': 'Delay re-engagement',
        'description': 'Warm re-engagement for a lead who went quiet — circles back on the agreed date.',
        'build': build_delay_followup_email,
        'send':  send_delay_followup_email,
    }),
    ('delay_last_check', {
        'label': 'Delay last-check',
        'description': 'The final, short re-engagement a few days after the first — leaves the door open.',
        'build': build_delay_last_check_email,
        'send':  send_delay_last_check_email,
    }),
    ('delay_quote', {
        'label': 'Quote + portfolio',
        'description': 'Portfolio and pricing guide, with the portfolio PDF attached on send.',
        'build': lambda apt: send_delay_quote_email(apt, preview_only=True),
        'send':  lambda apt: send_delay_quote_email(apt),
    }),
    ('reminder_two_days', {
        'label': 'Reminder — 2 days before',
        'description': 'Appointment reminder two days ahead (needs a scheduled date/time).',
        'build': _two_days_b, 'send': _two_days_s,
    }),
    ('reminder_one_day', {
        'label': 'Reminder — 1 day before',
        'description': 'Appointment reminder the day before (needs a scheduled date/time).',
        'build': _one_day_b, 'send': _one_day_s,
    }),
    ('reminder_morning', {
        'label': 'Reminder — morning of',
        'description': 'Reminder on the morning of the appointment (needs a scheduled date/time).',
        'build': _morning_b, 'send': _morning_s,
    }),
    ('reminder_two_hours', {
        'label': 'Reminder — 2 hours before',
        'description': 'Reminder two hours before arrival (needs a scheduled date/time).',
        'build': _two_hours_b, 'send': _two_hours_s,
    }),
    ('reminder_thirty_mins', {
        'label': 'Reminder — 30 mins before',
        'description': 'Reminder 30 minutes before arrival (needs a scheduled date/time).',
        'build': _thirty_mins_b, 'send': _thirty_mins_s,
    }),
])


def catalog_for_template():
    """List of (key, label, description) for rendering the picker."""
    return [(k, v['label'], v['description']) for k, v in EMAIL_CATALOG.items()]
