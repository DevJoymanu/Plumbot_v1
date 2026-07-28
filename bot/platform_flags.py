"""Per-tenant runtime switches (superuser console -> bot behaviour).

Two timers shape how the bot answers on WhatsApp:

* the **batch window** — wait N seconds after the customer's LAST message
  before generating anything, so a burst of rapid-fire texts gets one combined
  reply instead of three disjointed ones;
* the **reply delay** — hold the finished reply 1-5 minutes so it lands like a
  person typing rather than an instant bot.

Switching either off makes that stage immediate for THAT tenant only; nothing
else in the pipeline changes. Off is a demo/testing convenience (you see
answers straight away) — both should stay ON for live traffic.

Storage is `TenantSetting` (tenant + key/value), so adding a switch here is a
row, never a migration. Reads are fail-open: any DB trouble returns the
default, and an unknown tenant falls back to the homebase seed.
"""

from .models import TenantSetting

REPLY_DELAY_KEY = 'reply_delay_enabled'
BATCH_WINDOW_KEY = 'batch_window_enabled'

# Rendered by the tenant config page (label + the info-button copy), so the
# explanation of what a switch does lives next to the switch itself.
TIMER_FLAGS = [
    {
        'key': BATCH_WINDOW_KEY,
        'label': 'Message batch timer',
        'summary': 'Waits 45s after the last message, then answers everything at once.',
        'info': (
            'ON (recommended): after a customer texts, the bot waits 45 seconds. '
            'Every message that arrives inside that window resets the timer, and '
            'when it finally expires the bot writes ONE reply covering all of them '
            '- so a customer who sends "hi", "how much for a geyser?", "im in '
            'Hatfield" gets a single joined-up answer instead of three.\n\n'
            'OFF: every message is answered on its own, immediately. Faster for '
            'testing and demos, but a customer typing in bursts will get several '
            'part-answers and the bot can look like it is talking over itself.'
        ),
    },
    {
        'key': REPLY_DELAY_KEY,
        'label': 'Human reply delay',
        'summary': 'Holds each finished reply 1-5 minutes before sending.',
        'info': (
            'ON (recommended): once a reply is written it is held for a random '
            '1-5 minutes before sending, so replies read as a busy plumber '
            'getting back to you rather than a bot answering in half a second. '
            'A new customer message during the wait cancels the pending send and '
            'the reply is rewritten with the newer context.\n\n'
            'OFF: replies send the moment they are ready. Useful when you are '
            'testing or demoing and do not want to wait minutes for each turn.'
        ),
    },
]


def reply_delay_enabled(tenant=None) -> bool:
    """False = send this tenant's replies as soon as they are generated."""
    return TenantSetting.get_flag(REPLY_DELAY_KEY, tenant, default=True)


def batch_window_enabled(tenant=None) -> bool:
    """False = answer every inbound message on its own, no debounce wait."""
    return TenantSetting.get_flag(BATCH_WINDOW_KEY, tenant, default=True)


def timer_flag_rows(tenant):
    """The timing switches with this tenant's current state — for the console."""
    return [dict(flag, enabled=TenantSetting.get_flag(flag['key'], tenant, True))
            for flag in TIMER_FLAGS]
