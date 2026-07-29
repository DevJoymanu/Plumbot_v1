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
EMAIL_SENDING_KEY = 'email_sending_enabled'

# The tenant whose email identity is established. Every other tenant starts with
# email OFF (see email_sending_enabled) until its owner asks for it.
_EMAIL_DEFAULT_ON_SLUG = 'homebase'

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


EMAIL_FLAGS = [
    {
        'key': EMAIL_SENDING_KEY,
        'label': 'Outbound email',
        'summary': 'Lets this tenant send email to customers and its own team.',
        'info': (
            'OFF (the default for every tenant except Homebase): nothing is '
            'emailed for this tenant - no booking confirmations, quote or '
            'portfolio emails, reminders, or plumber alerts. The WhatsApp side is '
            'untouched and keeps running normally; email is simply skipped and '
            'logged.\n\n'
            'ON: turn this on once the tenant\'s email identity is actually theirs '
            '- from-name, reply-to address and the sending domain - so nothing '
            'goes out to their customers under the wrong business name.\n\n'
            'Dashboard password-reset mail is platform mail, not tenant mail, and '
            'is never affected by this switch.'
        ),
    },
]

# Everything the tenant console can toggle. The toggle view resolves a posted key
# against this, so a new switch is one entry here plus its reader below.
TENANT_FLAGS = TIMER_FLAGS + EMAIL_FLAGS


def reply_delay_enabled(tenant=None) -> bool:
    """False = send this tenant's replies as soon as they are generated."""
    return TenantSetting.get_flag(REPLY_DELAY_KEY, tenant, default=True)


def batch_window_enabled(tenant=None) -> bool:
    """False = answer every inbound message on its own, no debounce wait."""
    return TenantSetting.get_flag(BATCH_WINDOW_KEY, tenant, default=True)


def email_sending_enabled(tenant=None) -> bool:
    """False = send NO email for this tenant.

    Unlike the timers this defaults OFF for everyone but the homebase seed: a new
    tenant's email identity (from-name, reply-to, sending domain) is Homebase's
    until someone sets it up, so mailing their customers would sign the message
    with the wrong business. WhatsApp is unaffected — only email is skipped.

    `tenant=None` means platform mail (dashboard password resets), which this
    switch does not govern; callers pass a tenant only for tenant-scoped mail.
    """
    if tenant is None:
        return True
    default = (getattr(tenant, 'slug', '') or '').lower() == _EMAIL_DEFAULT_ON_SLUG
    return TenantSetting.get_flag(EMAIL_SENDING_KEY, tenant, default=default)


def _flag_rows(tenant, flags, default_for):
    return [dict(flag, enabled=TenantSetting.get_flag(
        flag['key'], tenant, default_for(flag['key'], tenant)))
        for flag in flags]


def timer_flag_rows(tenant):
    """The timing switches with this tenant's current state — for the console."""
    return _flag_rows(tenant, TIMER_FLAGS, lambda key, t: True)


def email_flag_rows(tenant):
    """The email switch with this tenant's current state — for the console."""
    return _flag_rows(
        tenant, EMAIL_FLAGS,
        lambda key, t: (getattr(t, 'slug', '') or '').lower() == _EMAIL_DEFAULT_ON_SLUG,
    )
