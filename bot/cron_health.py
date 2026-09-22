"""
bot/cron_health.py
==================
Is the follow-up cron actually running? A heartbeat, a check, and one email.

WHAT: `send_followups` records when it ran (`beat`). The Email_Follow_Ups cron,
a separate Railway service, checks that record every 5 minutes
(`alert_if_stale`), and the Follow-ups dashboard page shows it (`last_run`).
If the follow-up cron has not run for STALE_AFTER inside the sending hours,
the operator gets ONE email for that outage.

WHY: a Railway deployment built while a cron service has no schedule comes out
`buildOnly=false`, `cronSchedule=null`: it runs once, shows SUCCESS, and never
ticks again. It happened to Follow_Ups on 2026-08-25 (three days) and again on
2026-09-21 12:48 (about 20 hours, until the next deploy restored it). Both
times it was noticed only because follow-ups stopped and the owner pressed
"Run check" by hand. See `_cronScheduleTrap` in railway.json.

HOW: the heartbeat is a `TenantSetting` row on the platform's seed tenant
(key/value, so no migration), holding {'at': iso, 'alerted_for': iso}.
Outside the sending hours the follow-up command returns before doing anything,
so the check only runs inside them, and the first run after the window opens
has a grace period before a quiet cron counts as down. The alert remembers
which heartbeat it was about, so one outage is one email, and the next beat
arms it again. Pinned by `bot/test_cron_health.py`.
"""

import logging
from datetime import datetime, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

KEY = 'cron_heartbeat:send_followups'
# The cron ticks every 7 minutes; four missed ticks is not a slow run.
STALE_AFTER = timedelta(minutes=30)


def _row():
    from bot.models import TenantSetting
    return TenantSetting.objects.filter(
        tenant_id=TenantSetting._tenant_id(None), key=KEY).first()


def beat(now=None):
    """Record that send_followups ran. Never raises: a heartbeat must not stop
    the follow-ups it reports on."""
    try:
        from bot.models import TenantSetting
        now = now or timezone.now()
        row = _row()
        value = dict(row.value) if row and isinstance(row.value, dict) else {}
        value['at'] = now.isoformat()
        TenantSetting.objects.update_or_create(
            tenant_id=TenantSetting._tenant_id(None), key=KEY,
            defaults={'value': value})
    except Exception:
        logger.warning('Could not record the follow-up heartbeat', exc_info=True)


def last_run():
    """When send_followups last ran, or None when it never has (or on error)."""
    try:
        row = _row()
        at = (row.value or {}).get('at') if row else None
        return datetime.fromisoformat(at) if at else None
    except Exception:
        return None


def alert_if_stale(now=None, send=None):
    """Email the operator once when the follow-up cron has gone quiet.

    Returns True when an alert was sent. `send` is injectable for tests; it
    defaults to the platform mail path (no tenant, so no tenant's switch can
    silence it).
    """
    from bot.management.commands.send_followups import CONTACT_WINDOWS, SA_TIMEZONE
    now = now or timezone.now()
    local = now.astimezone(SA_TIMEZONE)
    minutes = local.hour * 60 + local.minute
    # Inside the sending hours, and past the grace period after they open:
    # before the window opens the cron returns early, so a quiet cron at
    # 08:05 is not down.
    inside = any((oh * 60 + om) + STALE_AFTER.total_seconds() / 60 <= minutes < (ch * 60 + cm)
                 for oh, om, ch, cm in CONTACT_WINDOWS)
    if not inside:
        return False
    ran = last_run()
    if ran and now - ran < STALE_AFTER:
        return False
    row = _row()
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    about = value.get('at') or 'never'
    if value.get('alerted_for') == about:
        return False            # this outage was already reported
    if send is None:
        from bot.plumber_notifications import (PLATFORM_NOTIFICATION_EMAIL,
                                               send_email_to_recipients)

        def send(subject, body):
            return send_email_to_recipients(
                [PLATFORM_NOTIFICATION_EMAIL], subject, body, to_role='operator')
    since = ran.astimezone(SA_TIMEZONE).strftime('%a %d %b %H:%M') if ran else 'never'
    ok = send(
        'Automatic follow-ups have stopped running',
        'The follow-up job (Railway service Follow_Ups) has not run since %s.\n\n'
        'Follow-ups will not go out until it runs again. Until then, press "Run '
        'check" on the Follow-ups page to send the ones that are due.\n\n'
        'The usual cause is a deployment that lost its cron schedule. Check the '
        'latest Follow_Ups deployment in Railway: it needs a cron schedule of '
        '*/7 * * * *. If it has none, re-apply the schedule on the service and '
        'deploy again (a plain redeploy repeats the problem).' % since)
    if ok:
        from bot.models import TenantSetting
        value['alerted_for'] = about
        TenantSetting.objects.update_or_create(
            tenant_id=TenantSetting._tenant_id(None), key=KEY,
            defaults={'value': value})
    return bool(ok)
