"""
bot/unanswered_sweep.py
=======================
The hourly check for a lead's message that never got a reply.

WHAT: once an hour, find leads whose latest message is theirs, with nothing
from us after it, and answer it through the normal reply pipeline, exactly as
if it had just arrived.

WHY (owner, 2026-09-22): a reply is prepared inside the running web server
(the 45-second batching window, then a delayed send). A message that lands
while a deploy is swapping servers, or while the server is restarting, gets
saved but its reply dies with the old server. Lead 1161's "How much" at 11:00
arrived 42 seconds before the new server was ready and was never answered.

HOW:
  * `unanswered_leads(now)` picks the leads: not paused (a paused lead is a
    human's conversation, and silence there is on purpose), still inside the
    free WhatsApp window (a reply outside it would bounce or cost), a real
    WhatsApp number, and a latest user message between 15 minutes and 23
    hours old. Under 15 minutes the live reply may still be on its way (batch
    window plus up to 5 minutes of delay).
  * Every trailing text message of theirs is answered together, joined the
    way the batching window joins them, through `_generate_and_schedule_reply`,
    so the silence rules (a bare "ok" / "thank you" gets no reply), the stop
    rule and every routing step apply as usual.
  * The last of those messages is stamped `sweep_checked` (an optional JSON key
    on the transcript turn) BEFORE answering, so a message the pipeline chose
    to leave unanswered is not tried again every hour.
  * The pipeline sends from a background thread after a delay; a cron process
    exits when its command ends and would kill that thread. So the sweep turns
    the delay off for its own process (`whatsapp_webhook.force_immediate_replies`)
    and waits for the send threads before returning.
Runs from the `send_scheduled_followups` command (the Email_Follow_Ups cron,
every 5 minutes) on the first tick of each hour only. Pinned by
`bot/test_unanswered_sweep.py`.
"""

import logging
import threading
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

MIN_AGE = timedelta(minutes=15)
MAX_AGE = timedelta(hours=23)
SWEEP_KEY = 'sweep_checked'
# How long to wait for the reply threads to send before the cron exits.
SEND_WAIT_SECONDS = 120


def _stamp(value):
    from bot.management.commands.send_followups import _parse_history_stamp
    return _parse_history_stamp(value)


def trailing_user_turns(history):
    """The lead's text messages after our last message, oldest first, or [].

    [] when our message is the latest, when there is none of theirs, or when a
    trailing turn is a bracketed media/system entry ("[Sent image] …"), which
    the text pipeline cannot answer.
    """
    turns = []
    for message in reversed(history or []):
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'user':
            break
        content = str(message.get('content') or '').strip()
        if not content or content.startswith('['):
            return []
        turns.append(message)
    return list(reversed(turns))


def unanswered_leads(now=None):
    """[(lead, trailing user turns)] for every lead owed a reply now."""
    from bot.models import Appointment
    now = now or timezone.now()
    found = []
    candidates = (Appointment.objects.real()
                  .filter(last_customer_response__gte=now - MAX_AGE,
                          last_customer_response__lte=now - MIN_AGE)
                  .exclude(chatbot_paused=True)
                  .exclude(status__in=('cancelled', 'completed'))
                  .exclude(phone_number__startswith='email_')
                  .exclude(phone_number__startswith='quotation_only_'))
    for lead in candidates:
        turns = trailing_user_turns(lead.conversation_history)
        if not turns or turns[-1].get(SWEEP_KEY):
            continue
        sent_at = _stamp(turns[-1].get('timestamp'))
        if sent_at is None or not (now - MAX_AGE <= sent_at <= now - MIN_AGE):
            continue
        try:
            if not lead.messaging_window_open:
                continue
        except Exception:
            continue
        found.append((lead, turns))
    return found


def _mark_checked(lead, last_turn):
    """Stamp the last unanswered turn so it is never swept twice."""
    for message in reversed(lead.conversation_history or []):
        if message is last_turn or (
                message.get('role') == 'user'
                and message.get('timestamp') == last_turn.get('timestamp')
                and message.get('content') == last_turn.get('content')):
            message[SWEEP_KEY] = timezone.now().isoformat()
            break
    lead.save(update_fields=['conversation_history'])


def answer_unanswered(now=None, dry_run=False, log=None):
    """Answer every lead owed a reply. Returns {'answered', 'failed'}."""
    from bot import whatsapp_webhook as wh
    emit = log or (lambda _msg: None)
    counts = {'answered': 0, 'failed': 0}
    owed = unanswered_leads(now)
    if not owed:
        return counts
    before = set(threading.enumerate())
    with wh.force_immediate_replies():
        for lead, turns in owed:
            text = '\n'.join(str(t.get('content') or '').strip() for t in turns)
            sender = lead.phone_number.replace('whatsapp:', '').replace('+', '').strip()
            if dry_run:
                emit('  would answer lead %s: %r' % (lead.pk, text[:80]))
                counts['answered'] += 1
                continue
            try:
                _mark_checked(lead, turns[-1])
                emit('  answering lead %s, unanswered since %s: %r' % (
                    lead.pk, turns[0].get('timestamp'), text[:80]))
                wh._generate_and_schedule_reply(
                    sender, text, turns[-1].get('message_id') or turns[-1].get('wamid'),
                    None, tenant=lead.tenant)
                counts['answered'] += 1
            except Exception:
                logger.exception('Unanswered sweep failed for lead %s', lead.pk)
                counts['failed'] += 1
        # The replies go out from threads; let them finish before the cron
        # process exits and takes them with it.
        for thread in set(threading.enumerate()) - before:
            thread.join(timeout=SEND_WAIT_SECONDS)
    return counts


def is_sweep_tick(now=None):
    """The first 5-minute tick of each hour (SAST), so a */5 cron sweeps hourly."""
    from bot.management.commands.send_followups import SA_TIMEZONE
    return (now or timezone.now()).astimezone(SA_TIMEZONE).minute < 5
