"""
bot/near_date_call.py
=====================
The plumber's call for a lead who named a day within the week.

WHAT (owner, 2026-09-23), two kinds of lead, tagged by the delay flow
(out_of_scope_handler):

  [NEAR_AWAIT] <day> <set at>  "I'll contact you on Monday". We waited. The
      MORNING AFTER the day, if they have not written since, have no quote sent
      and no visit booked, the plumber gets a call email: what they said, and
      a script ready to read.
  [NEAR_CALL]  <day> <set at>  "Contact me on Monday". They were asked for an
      email. The plumber gets the call email on the MORNING OF the day either
      way (they asked to be contacted, so no permission question). With no
      email the lead was told we would call; with one, the dated check-back
      email goes too and the call is on top of it (owner, 2026-09-23: "yes,
      plumber should call as well"; it used to be email only).

WHY: a lead who names a day inside the week is usually out of the free
WhatsApp window by then (24h from their last message), so with no email the
bot cannot reach them at all; a person on the phone is the only contact left.

HOW: `run_tick(now)` is called every Follow_Ups cron tick (send_followups),
BEFORE its contact-hours cutoff because this only emails the plumber; it has
its own morning gate (CALL_FROM_HOUR). Each lead is sent at most once
([NEAR_CALL_SENT]); a lead who wrote to us after the tag was set, booked, got
a quote or is suppressed is marked done without an email. A day more than
STALE_AFTER_DAYS gone is dropped, so a cron back from an outage never asks the
plumber to call about last week. The email is the owner's call-email layout
(call_brief.build_call_email, subject CALL_SUBJECT_PREFIX, so the 24-hour call
reminder covers it) with the "Log the call" button (the lead's phone-quote
form). Pinned by NearDateContactTests.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

SENT_TAG = '[NEAR_CALL_SENT]'
CALL_FROM_HOUR = 8          # local (SAST) hour from which the email may go
STALE_AFTER_DAYS = 3

_TAG_RE = re.compile(r'^\[NEAR_(AWAIT|CALL)\] (\d{4}-\d{2}-\d{2}) (\S+)', re.MULTILINE)


def _local(now):
    import pytz
    return now.astimezone(pytz.timezone('Africa/Johannesburg'))


def read_tag(appointment):
    """(kind, day, set_at) from the lead's near-date tag, or None."""
    m = _TAG_RE.search(getattr(appointment, 'internal_notes', '') or '')
    if not m:
        return None
    try:
        day = date.fromisoformat(m.group(2))
        set_at = datetime.fromisoformat(m.group(3))
    except ValueError:
        return None
    return ('await' if m.group(1) == 'AWAIT' else 'call'), day, set_at


def due_day(kind, day):
    """The day the plumber is emailed: the day after for type 1 (we waited
    for them), the day itself for type 2 (they asked us to call then)."""
    return day + timedelta(days=1) if kind == 'await' else day


def _why_not(appointment, kind, set_at):
    """Why this lead needs no call, or ''."""
    from bot.job_date_ladder import quote_given
    from bot.post_visit import lead_is_suppressed
    if lead_is_suppressed(appointment):
        return 'the lead is suppressed'
    if (getattr(appointment, 'status', '') == 'confirmed'
            or getattr(appointment, 'scheduled_datetime', None)):
        return 'a visit is booked'
    if quote_given(appointment):
        return 'a quote has gone out'
    last_in = (getattr(appointment, 'last_customer_response', None)
               or getattr(appointment, 'last_inbound_at', None))
    if last_in and set_at and last_in > set_at:
        return 'the lead wrote to us since'
    # A type 2 lead who gave an email is still called (owner, 2026-09-23).
    # This line used to skip them ("the check-back goes by email").
    return ''


def _mark_done(appointment, note):
    appointment.internal_notes = (
        f"{appointment.internal_notes or ''}\n{SENT_TAG} {note}".strip())
    appointment.save(update_fields=['internal_notes'])


def build_brief(appointment, kind, day, set_at, now=None):
    """(subject, text, html) for the plumber, script first."""
    from bot.call_brief import CALL_SUBJECT_PREFIX, _next_two_working_days, build_call_email
    from bot.job_date_ladder import _visit_offer
    from bot.lead_handoff import service_label
    from bot.phone_quote import ensure_request, form_url
    from bot.utils import business_name_for

    now = now or timezone.now()
    name = (getattr(appointment, 'customer_name', '') or '').strip()
    first = name.split()[0] if name else 'there'
    phone = ''.join(c for c in str(getattr(appointment, 'phone_number', '') or '') if c.isdigit())
    service = service_label(appointment).lower() or 'plumbing work'
    area = getattr(appointment, 'customer_area', '') or 'not given'
    description = ' '.join(str(getattr(appointment, 'project_description', '') or '').split())
    try:
        plumber = (appointment.plumber_display_name() or '').strip()
    except Exception:
        plumber = ''
    plumber = '' if plumber == 'the plumber' else plumber
    pf = plumber.split()[0] if plumber else ''
    company = business_name_for(appointment, default='').strip()
    who = f"it's {pf}" + (f' from {company}' if company else '') if pf else f"it's {company or 'us'}"
    day_name = day.strftime('%A %d %B')
    told_on = _local(set_at).strftime('%A %d %B') if set_at else 'earlier'
    d1, d2 = _next_two_working_days(appointment, now)

    if kind == 'await':
        intro = (f'Hi {pf or "there"}, please call {name or "this lead"} today and read the '
                 f'script below. On {told_on} they said they would contact us on '
                 f'{day_name} about the {service}. That day has passed with no word from '
                 'them, no quote sent and no visit booked.')
        opening = (f"Hi {first}, {who}. You mentioned you'd get back to us on "
                   f"{day.strftime('%A')} about the {service}, so I thought I'd give you "
                   "a quick call. Are you still keen to get it sorted, or has the timing moved?")
        subject = f"{CALL_SUBJECT_PREFIX}{name or '+' + phone}, said they'd contact us {day_name}"
    else:
        # The why-this-call line depends on the email: without one the call
        # is our only way to reach them; with one it follows our email.
        has_email = bool((getattr(appointment, 'customer_email', '') or '').strip())
        reach = ('They also have our check-back email today, so this call follows it up.'
                 if has_email else
                 'They did not give an email, so this call is how we reach them, and they '
                 'were told to expect it.')
        intro = (f'Hi {pf or "there"}, please call {name or "this lead"} today and read the '
                 f'script below. On {told_on} they asked us to contact them today about '
                 f'the {service}. {reach}')
        opening = (f"Hi {first}, {who}. You asked us to get in touch today about the "
                   f"{service}. Are you still keen to get it sorted, or has the timing moved?")
        subject = f"{CALL_SUBJECT_PREFIX}{name or '+' + phone}, asked us to call them today"

    branches = [
        ("If they still want it done", [
            f'"Perfect, easiest thing is I pop round, {_visit_offer(appointment)}. '
            f'Would {d1} or {d2} suit you better?"',
            'If they pick a day: "Morning or afternoon?"']),
        ("Only if they're not keen on a visit", [
            '"That\'s okay. If you have measurements, or a few photos, or a plan, just '
            'send those over with what you need done and I\'ll give you a price over '
            'the phone."']),
        ("If the timing has moved", [
            '"No problem at all. Roughly when are you thinking now?" Note the new date.']),
        ("If they went with someone else, or no longer need it", [
            '"No worries at all. If it doesn\'t work out, you\'ve got my number."']),
        ("If there's no answer", ['Try once more later today, then leave it.']),
    ]
    details = [
        ('Call', f'{name or "Name not given"}, +{phone}' if phone else (name or 'not given')),
        ('Job', description or service),
        ('Area', area),
        ('What they said', (f'they would contact us on {day_name}' if kind == 'await'
                            else f'contact them on {day_name}')),
        ('Told us on', told_on),
    ]
    url = (form_url(ensure_request(appointment))
           if getattr(appointment, 'pk', None) else '#log-the-call')
    text, html = build_call_email(intro, opening, branches, details, url)
    return subject, text, html


def run_tick(now=None, dry_run=False, log=None):
    """One pass. Returns {'sent', 'skipped', 'failed'}."""
    from bot.models import Appointment
    from bot.plumber_notifications import (send_email_to_recipients,
                                           split_notification_recipients)
    now = now or timezone.now()
    emit = log or (lambda _m: None)
    stats = {'sent': 0, 'skipped': 0, 'failed': 0}
    local = _local(now)
    if local.hour < CALL_FROM_HOUR:
        return stats
    today = local.date()
    leads = (Appointment.objects.real()
             .filter(internal_notes__regex=r'\[NEAR_(AWAIT|CALL)\]')
             .exclude(internal_notes__contains=SENT_TAG))
    for apt in leads:
        try:
            tag = read_tag(apt)
            if tag is None:
                continue
            kind, day, set_at = tag
            due = due_day(kind, day)
            if today < due:
                continue
            if today > due + timedelta(days=STALE_AFTER_DAYS):
                if not dry_run:
                    _mark_done(apt, 'stale')
                stats['skipped'] += 1
                continue
            why = _why_not(apt, kind, set_at)
            if why:
                if not dry_run:
                    _mark_done(apt, f'skipped: {why}')
                stats['skipped'] += 1
                emit(f'  near-date call skipped for lead {apt.pk}: {why}')
                continue
            if dry_run:
                emit(f'  would email the plumber a near-date call brief for lead {apt.pk}')
                stats['sent'] += 1
                continue
            subject, text, html = build_brief(apt, kind, day, set_at, now)
            tenant = getattr(apt, 'tenant', None)
            to, hidden = split_notification_recipients(tenant)
            ok = send_email_to_recipients(
                to, subject, text, html_message=html, tenant=tenant, appointment=apt,
                bcc=hidden, category='plumber_alert', to_role='plumber')
            if ok:
                _mark_done(apt, f'{kind} {day.isoformat()} at {now.isoformat()}')
                stats['sent'] += 1
                emit(f'  near-date call brief sent for lead {apt.pk} ({kind})')
            else:
                stats['failed'] += 1       # retried next tick; not marked
        except Exception:
            logger.exception('Near-date call failed for lead %s', getattr(apt, 'pk', None))
            stats['failed'] += 1
    return stats
