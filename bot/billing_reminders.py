"""
bot/billing_reminders.py
========================
The automatic side of platform billing: reminding a tenant before an invoice
is due, asking the operator on the due day whether they paid, and, once the
operator says "Not paid", counting the tenant down to their bot being
switched off (docs/current-state/billing.md).

The schedule (owner, 2026-09-24):

    before due   tenant reminder at each Billing details "days before"
                 (default 14 and 7) and on the due day itself
    due day      operator check: paid / more days / not paid
    not paid     switch-off date = the later of due and today, plus 8 days;
                 tenant notice the day after due (7 days to go), 3 days after
                 due (5 to go), the day before, and on the day; on the day the
                 operator is emailed to pause the account (a person presses
                 it, the cron never switches anyone off)

Everything stops the moment the invoice is paid (balance 0) or voided, or its
reminders are switched off. "More days" moves the due date and clears the
not-paid countdown, so the cycle starts again against the new date.

HOW, and why it is shaped this way:
- `plan_for(invoice, today)` is the pure answer "what is due for this invoice
  today", as (key, kind, detail) tuples. The run just sends each and logs it,
  so the schedule can be tested without sending anything.
- Keys carry the date they were scheduled against (`remind:<due>:<days>`,
  `off:<switch-off>:<days>`), so an extended invoice gets a fresh cycle and a
  second 5-minute tick the same day finds the key and sends nothing.
- CATCH-UP, ONE AT A TIME: a missed tick (a dead cron, a deploy) sends only
  the most recent step that has come due, never a burst of old ones, and
  never a step scheduled before the invoice was sent (an invoice issued with
  10 days to go does not get a "14 days" reminder on top of itself).
- A failed send is retried on later ticks, at most `MAX_TRIES` times, so a
  broken transport cannot mail the same thing every 5 minutes all day.
- Runs only at or after `PlatformBillingProfile.reminder_time` (07:00 local).
  Billing mail goes to a business, so the lead-email send windows do not
  apply; the owner asked for 7AM.
- Driven from the `send_scheduled_followups` command (the Email_Follow_Ups
  cron, every 5 minutes) in its own try, like the call-brief reminder, so no
  new Railway service or PLUMBOT_CRON entry is needed. `send_billing_reminders`
  runs it by hand, with --dry-run.
Pinned by `BillingReminderTests` in bot/test_views_actions.py.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_TRIES = 3
# Not paid: the switch-off date is this many days after the later of the due
# date and the day "Not paid" was pressed, so the first notice (the day after)
# can say "in the next 7 days".
SWITCH_OFF_AFTER_DAYS = 8
# Tenant switch-off notices, as days before the switch-off date: the day after
# due (8 - 1 = 7), 3 days after due (5), the day before (1), the day (0).
SWITCH_OFF_NOTICE_DAYS = (7, 5, 1, 0)


def switch_off_date_for(invoice, today):
    """The day a not-paid tenant's bot is switched off."""
    base = max(invoice.due_date, today)
    return base + timedelta(days=SWITCH_OFF_AFTER_DAYS)


def _latest_due_step(steps, target, today, not_before):
    """The most recent of `steps` (days before `target`) whose day has come,
    or None. Steps whose day is after today are not due yet; a step whose day
    fell before `not_before` (the invoice was not out yet) never fires."""
    due = [n for n in steps
           if target - timedelta(days=n) <= today
           and target - timedelta(days=n) >= not_before]
    return min(due) if due else None


def is_chaseable(invoice):
    """Sent, not void, money still owed, and reminders on."""
    return (invoice.status == invoice.Status.SENT
            and invoice.reminders_enabled
            and invoice.balance > 0)


def plan_for(invoice, today):
    """What the cron owes this invoice today: a list of (key, kind, detail).

    kind is one of 'remind' (tenant, before or on due), 'check' (operator,
    due day), 'switch_off' (tenant, not-paid countdown), 'pause' (operator,
    switch-off day). `detail` is the number of days left. Already-logged
    successes and exhausted keys are left out.
    """
    if not is_chaseable(invoice):
        return []
    sent_day = timezone.localtime(invoice.sent_at).date() if invoice.sent_at else invoice.issue_date
    plan = []

    if invoice.unpaid_confirmed_at and invoice.switch_off_on:
        off = invoice.switch_off_on
        days_to_off = (off - today).days
        if days_to_off >= 0:
            unpaid_day = timezone.localtime(invoice.unpaid_confirmed_at).date()
            step = _latest_due_step(SWITCH_OFF_NOTICE_DAYS, off, today, unpaid_day)
            if step is not None:
                plan.append((f'off:{off.isoformat()}:{step}', 'switch_off', days_to_off))
        if days_to_off <= 0:
            plan.append((f'pause:{off.isoformat()}', 'pause', days_to_off))
    else:
        due = invoice.due_date
        days_left = (due - today).days
        if days_left >= 0:
            step = _latest_due_step(invoice.reminder_offsets(), due, today, sent_day)
            if step is not None:
                plan.append((f'remind:{due.isoformat()}:{step}', 'remind', days_left))
        if days_left <= 0:
            plan.append((f'check:{due.isoformat()}', 'check', days_left))

    log = invoice.reminder_log or {}
    return [(key, kind, detail) for key, kind, detail in plan
            if not log.get(key, {}).get('ok')
            and log.get(key, {}).get('tries', 0) < MAX_TRIES]


def _record(invoice, key, ok):
    """Write one attempt into the invoice's log, re-read under a row lock so
    two overlapping ticks cannot overwrite each other's entries."""
    from .models import PlatformInvoice
    with transaction.atomic():
        row = PlatformInvoice.objects.select_for_update().get(pk=invoice.pk)
        log = dict(row.reminder_log or {})
        entry = dict(log.get(key) or {})
        entry['tries'] = entry.get('tries', 0) + 1
        entry['ok'] = bool(ok)
        entry['at'] = timezone.now().isoformat()
        log[key] = entry
        row.reminder_log = log
        row.save(update_fields=['reminder_log'])
        invoice.reminder_log = log


def run_billing_reminders(*, now=None, dry_run=False, log=None):
    """Send everything due across all invoices. Returns counts.

    Before `reminder_time` it does nothing, so the first tick after 7:00 is
    the one that sends. Each invoice is isolated: one failing never stops the
    rest.
    """
    from .billing_emails import send_billing_notice
    from .models import PlatformBillingProfile, PlatformInvoice

    say = log or (lambda message: None)
    counts = {'sent': 0, 'failed': 0, 'due': 0}
    now = timezone.localtime(now or timezone.now())
    profile = PlatformBillingProfile.current()
    if now.time() < profile.reminder_time:
        return counts
    today = now.date()

    invoices = (PlatformInvoice.objects
                .filter(status=PlatformInvoice.Status.SENT, reminders_enabled=True)
                .select_related('tenant').prefetch_related('items', 'payments'))
    for invoice in invoices:
        try:
            for key, kind, days in plan_for(invoice, today):
                counts['due'] += 1
                say(f'{invoice.number}: {kind} ({days} days) [{key}]')
                if dry_run:
                    continue
                ok, error = send_billing_notice(invoice, kind, days)
                _record(invoice, key, ok)
                if ok:
                    counts['sent'] += 1
                else:
                    counts['failed'] += 1
                    logger.warning('Billing %s for %s failed: %s', kind, invoice.number, error)
        except Exception:  # noqa: BLE001 - one bad invoice never stops the run
            counts['failed'] += 1
            logger.exception('Billing reminders failed for invoice %s', invoice.pk)
    return counts
