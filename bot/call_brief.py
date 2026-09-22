"""
bot/call_brief.py
=================
The 24-hour reminder for a "please call this lead" email to the plumber.

WHAT: a call email ("Please call today: ...") asks the plumber to phone a lead
and log the call through the "Log the call" button, which opens that lead's
phone-quote form. When the form is still unanswered 24 hours later, the
plumber gets the same email once more, opening with "we didn't hear back
after yesterday's email", with the day choices in the script moved forward.
One reminder, never a second one (owner, 2026-09-22).

WHY: the bot cannot chase these leads itself; their free WhatsApp window has
closed and they have no email, so the plumber's call is the only way to reach
them. A call email that gets buried means the lead is lost silently.

HOW, with no new state: the sent-emails log (`SentEmail`) already holds the
call email, its HTML and its text. A call email that went out 24 to 72 hours
ago, with no reminder logged for that lead since, is due, unless nothing is
left to chase: the form was answered, the lead wrote in after it, or the lead
is booked, closed or suppressed (`phone_quote.lead_is_done`). The reminder is
built FROM the stored email, so it repeats exactly the script the owner
approved, rather than a second copy of the script living in code. It runs
from `send_scheduled_followups` (the Email_Follow_Ups cron, every 5 minutes)
inside the contact hours, and the reminder's own log row is the gate that
makes every tick after the first a no-op. Pinned by `bot/test_call_brief.py`.
"""

import logging
import re
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Subject prefixes: the call email, and the reminder built from it. The
# reminder's subject is how the next tick knows it has already gone.
CALL_SUBJECT_PREFIX = 'Please call today: '
REMINDER_SUBJECT_PREFIX = 'Reminder: please call '

REMIND_AFTER = timedelta(hours=24)
# A call email older than this is stale: after three days a reminder asks the
# plumber to open with "on Saturday you mentioned Monday" about a lead who
# has long moved on, so it is dropped instead (a cron back from an outage
# must not fire a pile of week-old reminders).
REMIND_BEFORE = timedelta(hours=72)

_WENT_OUT = ('sent', 'delivered', 'opened')
_DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
# The two-day close in the script ("Would Wednesday or Thursday suit you
# better?", or "... suit you better for us to come and have a quick look?").
# By the reminder those days are a day stale, so they move on. No "?" in the
# pattern: the contact-card script carries on after "better".
_DAY_PAIR = re.compile(r'(Would )(%s) or (%s)( suit you better)' % ('|'.join(_DAYS), '|'.join(_DAYS)))


def _next_two_working_days(appointment, now):
    """The next two days after today that the lead's tenant works.

    Closed weekdays come from the tenant (`TenantConfig.closed_weekdays`), so
    a Saturday-closed tenant never has a Saturday offered; none closed means
    simply tomorrow and the day after.
    """
    from bot.tenant_config import get_config
    from bot.management.commands.send_followups import SA_TIMEZONE
    try:
        closed = get_config(getattr(appointment, 'tenant', None)).closed_weekdays() or frozenset()
    except Exception:
        closed = frozenset()
    day = now.astimezone(SA_TIMEZONE).date()
    found = []
    while len(found) < 2:
        day += timedelta(days=1)
        if day.weekday() not in closed:
            found.append(_DAYS[day.weekday()])
    return found


def _reminder_intro(original_text, sent_at, now):
    """The reminder's opening line, greeting the plumber the way the call
    email did ("Hi Kudakwashe,"). "Yesterday's email" only when it was
    yesterday; otherwise the day it went, so it is never untrue."""
    from bot.management.commands.send_followups import SA_TIMEZONE
    found = re.match(r'\s*(Hi [^,\n]+),', original_text or '')
    hi = found.group(1) if found else 'Hi'
    sent_day = sent_at.astimezone(SA_TIMEZONE).date()
    today = now.astimezone(SA_TIMEZONE).date()
    when = ("yesterday's email" if (today - sent_day).days == 1
            else "our email on %s" % _DAYS[sent_day.weekday()])
    return ("%s, we didn't hear back after %s, so we're assuming the lead didn't "
            "pick up or the call didn't happen. Here's the script again. After the "
            "call, tap the button at the bottom." % (hi, when))


def build_reminder(call_email, appointment, now=None):
    """(subject, text, html) for the reminder of one call email.

    The stored email with two changes: its first paragraph (the "please call
    today" line) becomes the reminder intro, and the script's two-day choice
    moves to the next two working days. Everything else, the script, the
    button and the lead details, is exactly what the plumber got the first time.
    """
    now = now or timezone.now()
    intro = _reminder_intro(call_email.text_body, call_email.sent_at or call_email.created_at, now)
    days = _next_two_working_days(appointment, now)

    def shift(body):
        return _DAY_PAIR.sub(lambda m: '%s%s or %s%s' % (m.group(1), days[0], days[1], m.group(4)), body)

    from html import escape
    html = call_email.html_body or ''
    # The first paragraph is the intro: the builder writes nothing before it
    # but the charset tag and the wrapper div.
    # quote=False: apostrophes stay apostrophes ("didn't"), as in the script.
    html = re.sub(r'(<p[^>]*>).*?(</p>)', lambda m: m.group(1) + escape(intro, quote=False) + m.group(2),
                  html, count=1, flags=re.S)
    text = call_email.text_body or ''
    parts = text.split('\n\n', 1)
    text = intro + ('\n\n' + parts[1] if len(parts) > 1 else '')

    subject = call_email.subject[len(CALL_SUBJECT_PREFIX):] if call_email.subject.startswith(
        CALL_SUBJECT_PREFIX) else call_email.subject
    return REMINDER_SUBJECT_PREFIX + subject, shift(text), shift(html)


def build_call_email(intro, opening, branches, details, button_url):
    """(text, html) for a "please call this lead" email, in the owner's layout.

    WHAT: the layout the owner approved for Barmak lead 1162 (2026-09-22):
    one intro line to the plumber; the script FIRST, its opening line in a
    box; each branch as a bold "If …:" line with what to say under it; the
    "Log the call" button; the lead details LAST.
    WHY: the plumber reads the script out and nothing else, so it leads, and
    every detail the lead gave is worked into the script by the caller
    (plumber-call-brief-format). One builder, so every call email has the
    first-paragraph intro and "Would X or Y suit you better?" shape that
    build_reminder rewrites for the 24-hour reminder.
    HOW: `branches` is [(title, [lines])], `details` is [(label, value)].
    """
    from html import escape
    p = 'margin:0 0 12px;font:15px/1.5 Arial,sans-serif;color:#1a1a1a;'
    h = ('margin:20px 0 8px;font:bold 13px Arial,sans-serif;color:#555;'
         'text-transform:uppercase;letter-spacing:.04em;')
    html = ['<div style="max-width:620px;margin:0 auto;padding:16px;">',
            '<p style="%s">%s</p>' % (p, escape(intro, quote=False)),
            '<p style="%s">Script</p>' % h,
            '<div style="border-left:4px solid #1a73e8;background:#f3f7fe;padding:12px 16px;'
            'margin:0 0 12px;"><p style="%smargin:0;">"%s"</p></div>'
            % (p, escape(opening, quote=False))]
    text = [intro, '', 'SCRIPT', '"%s"' % opening, '']
    for title, lines in branches:
        html.append('<p style="%smargin-bottom:4px;"><b>%s:</b></p>' % (p, escape(title, quote=False)))
        html += ['<p style="%smargin-left:14px;">%s</p>' % (p, escape(line, quote=False))
                 for line in lines]
        text += [title + ':'] + ['  ' + line for line in lines] + ['']
    html.append('<p style="%smargin-top:20px;"><b>After the call, tap below and fill in '
                'how it went:</b></p>' % p)
    html.append('<p style="margin:0 0 24px;"><a href="%s" style="display:inline-block;'
                'background:#1a73e8;color:#fff;text-decoration:none;font:bold 15px Arial,'
                'sans-serif;padding:12px 22px;border-radius:6px;">Log the call</a></p>'
                % escape(button_url))
    html.append('<p style="%s">Lead details</p>' % h)
    html += ['<p style="%smargin-bottom:6px;"><b>%s:</b> %s</p>'
             % (p, escape(k, quote=False), escape(v, quote=False)) for k, v in details]
    html.append('</div>')
    text += ['After the call, fill in how it went: ' + button_url, '', 'LEAD DETAILS']
    text += ['%s: %s' % kv for kv in details]
    return '\n'.join(text), '<meta charset="utf-8">' + ''.join(html)


def _nothing_left_to_chase(appointment, sent_at):
    """Why this lead needs no reminder, or ''.

    The form answered (the plumber logged the call), the lead wrote to us
    after the call email (they are talking again, the bot has them), or the
    lead is booked, closed or suppressed, by the same resolver every quote
    path uses.
    """
    from bot.phone_quote import lead_is_done
    row = getattr(appointment, 'phone_quote_request', None)
    if row is not None and not row.is_open:
        return 'the call was logged'
    last_in = getattr(appointment, 'last_customer_response', None)
    if last_in and last_in > sent_at:
        return 'the lead wrote in since'
    if lead_is_done(appointment):
        return 'the lead is booked, closed or suppressed'
    return ''


def due_call_emails(now=None):
    """Call emails whose reminder is due now, newest first, one per lead."""
    from bot.models import SentEmail
    now = now or timezone.now()
    rows = (SentEmail.objects
            .filter(subject__startswith=CALL_SUBJECT_PREFIX, status__in=_WENT_OUT,
                    appointment__isnull=False,
                    created_at__lte=now - REMIND_AFTER,
                    created_at__gt=now - REMIND_BEFORE)
            .select_related('appointment', 'tenant')
            .order_by('-created_at'))
    seen, due = set(), []
    for row in rows:
        if row.appointment_id in seen:
            continue
        seen.add(row.appointment_id)
        # The gate: a reminder logged for this lead after this call email.
        if SentEmail.objects.filter(appointment_id=row.appointment_id,
                                    subject__startswith=REMINDER_SUBJECT_PREFIX,
                                    created_at__gt=row.created_at).exists():
            continue
        due.append(row)
    return due


def send_due_reminders(now=None, dry_run=False, log=None):
    """Send every due call-email reminder. Returns {'sent', 'skipped', 'failed'}.

    Only inside the contact hours (`plan_quote.in_contact_window`, the same
    hours the lead follow-ups use), so a reminder due at 02:00 waits for the
    morning. Each lead is re-checked just before its send, and one bad lead
    never stops the run.
    """
    from bot.plan_quote import in_contact_window
    from bot.plumber_notifications import send_email_to_recipients
    now = now or timezone.now()
    counts = {'sent': 0, 'skipped': 0, 'failed': 0}
    emit = log or (lambda _msg: None)
    if not in_contact_window(now):
        return counts
    for call_email in due_call_emails(now):
        appointment = call_email.appointment
        try:
            why_not = _nothing_left_to_chase(appointment, call_email.created_at)
            if why_not:
                emit('  call reminder skipped for lead %s: %s' % (appointment.pk, why_not))
                counts['skipped'] += 1
                continue
            subject, text, html = build_reminder(call_email, appointment, now)
            if dry_run:
                emit('  would send call reminder for lead %s: %s' % (appointment.pk, subject))
                counts['sent'] += 1
                continue
            # The same people the call email went to; the operator's hidden
            # copy comes from the tenant's notification settings, as it did.
            from bot.plumber_notifications import split_notification_recipients
            _to, hidden = split_notification_recipients(call_email.tenant)
            ok = send_email_to_recipients(
                list(call_email.recipients or []), subject, text, html_message=html,
                tenant=call_email.tenant, appointment=appointment, bcc=hidden,
                category='plumber_alert', to_role='plumber')
            counts['sent' if ok else 'failed'] += 1
            emit('  call reminder %s for lead %s' % ('sent' if ok else 'FAILED', appointment.pk))
        except Exception:
            logger.exception('Call reminder failed for lead %s', getattr(appointment, 'pk', None))
            counts['failed'] += 1
    return counts
