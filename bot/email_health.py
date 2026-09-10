"""
bot/email_health.py
===================
Can the bot send email, read email, and answer it? And prove it, on demand.

Three capabilities, because they fail independently and for different reasons:

* **SENDING** needs an outbound transport (Brevo, else SendGrid, else SMTP) and
  a from-identity. It ALSO needs the per-tenant switch, which defaults OFF for
  every tenant but the homebase seed -- so a perfectly configured transport can
  still send nothing, and that is the single most likely reason a tenant's email
  is silently absent. The switch is reported on its own line rather than folded
  into the transport verdict, because a green transport and a closed switch are
  two different problems with two different fixes.
* **RECEIVING** needs the IMAP inbox credentials the 5-minute cron polls with.
* **RESPONDING** needs DeepSeek (which writes the reply) AND receiving, because
  a reply is only ever an answer to mail we managed to read. It is also
  reply-only by rule: the bot answers people already in the CRM with a WhatsApp
  record, and no email ever creates a lead.

CONFIGURATION IS NOT PROOF, and this module is careful never to imply otherwise.
Every capability reports what is configured; the tests are what actually exercise
the path. Nothing here can tell a dead cron from a quiet one -- that needs the
Railway deployment metadata (see `_cronScheduleTrap` in railway.json) -- so the
copy says so instead of showing a green tick that means less than it looks.

THE RECEIVE TEST MUST NOT READ ANYTHING. The polled inbox is the operator's own
Gmail, and the whole inbound pipeline is built on peek-then-gate so that merely
looking never marks their mail read. A test that fetched a body would break that
rule from a different direction, so it logs in, selects the mailbox READONLY,
counts what is unread, and stops.
"""

import imaplib
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# What a test email says. Recognisable in an inbox as a test rather than a real
# message from the business, because it lands in a plumber's mailbox too.
TEST_SUBJECT = 'Plumbot email test'

READY = 'ready'
BLOCKED = 'blocked'
PARTIAL = 'partial'


_UNSET = object()


def _env(name):
    """A setting if Django DEFINES one, else the environment, else ''.

    Presence, not truthiness: a name settings.py declares (every transport key
    does, via os.environ.get) is answered from settings even when the value is
    EMPTY, so an explicit `@override_settings(BREVO_API_KEY='')` means empty
    rather than "go and look at the real environment" - which is what made a
    blocked-transport case read as ready on a machine that happened to have a
    key exported. Names settings.py never declares (the IMAP_* group, read
    straight from the environment by the cron) still fall through.
    """
    import os

    value = getattr(settings, name, _UNSET)
    if value is _UNSET:
        value = os.environ.get(name, '')
    return (str(value) if value is not None else '').strip()


# ── What is configured ───────────────────────────────────────────────────────

def outbound_transport():
    """(name, detail) for the transport that would carry an email, or (None, why).

    The order is the real one: Brevo is primary and SendGrid is the fallback kept
    for the accounts still on it. SMTP is last and effectively dead on Railway,
    which blocks outbound SMTP entirely -- so it is reported as configured but
    unusable rather than as a working transport.
    """
    if _env('BREVO_API_KEY'):
        return 'Brevo', 'Brevo HTTP API on port 443.'
    if _env('SENDGRID_API_KEY'):
        return 'SendGrid', 'SendGrid v3 HTTP API on port 443 (the fallback transport).'
    host = _env('EMAIL_HOST')
    if host:
        return None, (
            f'Only SMTP is configured ({host}), and Railway blocks all outbound '
            f'SMTP. Set BREVO_API_KEY.'
        )
    return None, 'No email transport configured. Set BREVO_API_KEY.'


def _from_domain(from_email) -> str:
    """The domain out of a From value, which may be 'Name <addr@domain>'."""
    raw = (from_email or '').strip()
    if '<' in raw and '>' in raw:
        raw = raw[raw.index('<') + 1:raw.index('>')]
    return raw.rpartition('@')[2].strip().lower()


def imap_target():
    """(address, host, port) for the polled inbox, address '' when unset."""
    return (_env('IMAP_EMAIL'),
            _env('IMAP_HOST') or 'imap.gmail.com',
            _env('IMAP_PORT') or '993')


def _send_capability(tenant):
    transport, detail = outbound_transport()
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', '') or ''

    blockers = []
    if transport is None:
        blockers.append(detail)
    if not from_email:
        blockers.append('No from-address (DEFAULT_FROM_EMAIL is empty).')

    # The per-tenant switch. Reported whatever the transport says, because it is
    # the thing that most often makes a working transport send nothing.
    switch_on, switch_note = _tenant_switch(tenant)

    if blockers:
        state = BLOCKED
    elif not switch_on:
        state = PARTIAL
    else:
        state = READY

    facts = []
    if transport:
        facts.append(f'Transport: {transport}. {detail}')
    if from_email:
        facts.append(f'From: {from_email}')
    facts.append(switch_note)

    return {
        'key': 'send',
        'title': 'Sending email',
        'state': state,
        'summary': {
            READY: 'The bot can send email.',
            PARTIAL: 'The transport works, but this workspace has email switched off.',
            BLOCKED: 'The bot cannot send email.',
        }[state],
        'facts': facts,
        'blockers': blockers,
        'proves': ('Sends a real email to your own address through the '
                   'configured transport.'),
    }


def _tenant_switch(tenant):
    """(enabled, one line saying so) for the workspace's outbound-email switch."""
    if tenant is None:
        return True, ('No workspace selected, so only platform mail (password '
                      'resets) applies here. A tenant switch is read per '
                      'workspace.')
    name = getattr(tenant, 'name', None) or getattr(tenant, 'slug', 'this tenant')
    try:
        from .platform_flags import email_sending_enabled
        enabled = email_sending_enabled(tenant)
    except Exception:
        logger.warning('Could not read the email switch for %s', name, exc_info=True)
        return True, f'Could not read the outbound-email switch for {name}.'
    if enabled:
        return True, f'Outbound email is ON for {name}.'
    return False, (
        f'Outbound email is OFF for {name}, so nothing is sent to their '
        f'customers whatever the transport says. It defaults off until a '
        f'tenant\'s own sending identity is set up.'
    )


def _receive_capability():
    address, host, port = imap_target()
    password = _env('IMAP_PASSWORD')

    blockers = []
    if not address:
        blockers.append('No inbox address (IMAP_EMAIL is empty).')
    if not password:
        blockers.append('No inbox password (IMAP_PASSWORD is empty).')

    facts = []
    if address:
        facts.append(f'Polling {address} at {host}:{port}, every 5 minutes.')
    facts.append('Mail from anyone not already in the CRM is left unread for a '
                 'human. No email ever creates a lead.')

    return {
        'key': 'receive',
        'title': 'Receiving email',
        'state': BLOCKED if blockers else READY,
        'summary': ('The bot can read the inbox.' if not blockers
                    else 'The bot cannot read the inbox.'),
        'facts': facts,
        'blockers': blockers,
        'proves': ('Logs in and counts what is unread. It opens the mailbox '
                   'read-only and downloads nothing, so your own mail is not '
                   'marked as read.'),
    }


def _respond_capability(receive_state):
    key_set = bool(_env('DEEPSEEK_API_KEY'))

    blockers = []
    if not key_set:
        blockers.append('No DEEPSEEK_API_KEY, so no reply can be written.')
    if receive_state == BLOCKED:
        blockers.append('The inbox cannot be read, and a reply is only ever an '
                        'answer to mail we read.')

    return {
        'key': 'respond',
        'title': 'Answering email',
        'state': BLOCKED if blockers else READY,
        'summary': ('The bot can answer email.' if not blockers
                    else 'The bot cannot answer email.'),
        'facts': [
            'Replies go only to people already in the CRM with a WhatsApp '
            'record, on a thread we started or matched by their address.',
            'Machine mail (receipts, auto-replies, bounces) and our own traffic '
            'are dropped before a reply is ever considered.',
        ],
        'blockers': blockers,
        'proves': 'Asks DeepSeek for a short reply. Sends nothing.',
    }


def email_capabilities(tenant=None):
    """The three capabilities, for the settings panel.

    THE single reader: the page renders what this returns and adds no logic of
    its own, so what the panel claims and what a test exercises cannot drift.
    """
    send = _send_capability(tenant)
    receive = _receive_capability()
    respond = _respond_capability(receive['state'])
    return [send, receive, respond]


def capability_keys():
    return ('send', 'receive', 'respond')


# ── Proving it ───────────────────────────────────────────────────────────────

def run_test(key, *, to=None, tenant=None):
    """Exercise one capability for real. Returns {'ok', 'summary', 'detail'}.

    Never raises: a settings page reporting "could not test" is useful, and a
    500 is not.
    """
    runners = {
        'send': _test_send,
        'receive': _test_receive,
        'respond': _test_respond,
    }
    runner = runners.get(key)
    if runner is None:
        return {'ok': False, 'summary': f'Unknown test: {key}', 'detail': ''}
    try:
        return runner(to=to, tenant=tenant)
    except Exception as exc:  # noqa: BLE001 - shown to the operator, not swallowed
        logger.exception('Email %s test failed', key)
        return {'ok': False, 'summary': f'The {key} test raised an error.',
                'detail': f'{type(exc).__name__}: {exc}'}


def _test_send(*, to=None, tenant=None):
    address = (to or '').strip()
    if not address:
        return {'ok': False, 'summary': 'No address to send the test to.',
                'detail': 'Set an email address on your own login, or set '
                          'PLATFORM_NOTIFICATION_EMAIL.'}

    transport, detail = outbound_transport()
    if transport is None:
        return {'ok': False, 'summary': 'No usable transport is configured.',
                'detail': detail}

    from .plumber_notifications import send_email_to_recipients

    stamp = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S')
    # tenant=None ON PURPOSE: this is platform mail to the operator, so it is not
    # governed by any tenant's outbound switch. The panel reports that switch on
    # its own line, so a green result here can never be read as overriding it.
    sent = send_email_to_recipients(
        [address], TEST_SUBJECT,
        f'This is a test from the Plumbot settings page, sent at {stamp}.\n\n'
        f'It proves the {transport} transport accepted an email. It does not '
        f'prove a tenant\'s outbound switch is on, and it does not prove the '
        f'inbound cron is running.',
        tenant=None,
    )
    if sent:
        # NAME the domain. "The sending domain is probably not authenticated" is
        # useless advice without saying which one, and the answer is not obvious:
        # each tenant's customer mail leaves on their OWN domain, and a tenant
        # with none falls back to the platform subdomain. Verified 2026-09-10:
        # homebaseplumbers.co.zw carries Brevo DKIM (s1/s2), while
        # barmakplumbing.co.zw and notifications.homexmedia.com carry only the
        # brevo-code verification TXT with no DKIM at all, so their mail is
        # accepted and then filtered.
        domain = (_from_domain(getattr(settings, 'DEFAULT_FROM_EMAIL', ''))
                  or 'the sending domain')
        return {'ok': True, 'summary': f'{transport} accepted a test email to {address}.',
                'detail': (f'Check that inbox. Acceptance is not delivery: it has '
                           f'to arrive, and that needs {domain} authenticated in '
                           f'{transport} with SPF and DKIM published in DNS. Each '
                           f'tenant sends on their own domain, so authenticating '
                           f'one does not authenticate the others.')}
    return {'ok': False, 'summary': f'{transport} refused the test email.',
            'detail': 'The transport returned a failure. The server log for this '
                      'request carries the reason.'}


def _test_receive(*, to=None, tenant=None):
    address, host, port = imap_target()
    password = _env('IMAP_PASSWORD')
    if not address or not password:
        return {'ok': False, 'summary': 'The inbox credentials are not set.',
                'detail': 'IMAP_EMAIL and IMAP_PASSWORD are both required.'}

    imap = None
    try:
        imap = imaplib.IMAP4_SSL(host, int(port))
        imap.login(address, password)
        # READONLY, and no FETCH at all. Merely looking must never mark the
        # operator's own mail as read -- the same rule the inbound cron follows
        # with BODY.PEEK.
        status, _ = imap.select('INBOX', readonly=True)
        if status != 'OK':
            return {'ok': False, 'summary': 'Logged in, but could not open INBOX.',
                    'detail': f'SELECT returned {status}.'}
        status, data = imap.search(None, 'UNSEEN')
        unread = len((data[0] or b'').split()) if status == 'OK' and data else 0
        return {
            'ok': True,
            'summary': f'Logged in to {address} and found {unread} unread.',
            'detail': ('Opened read-only and nothing was downloaded, so no mail '
                       'was marked as read. This proves the credentials work; it '
                       'cannot tell you whether the 5-minute cron is running.'),
        }
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def _test_respond(*, to=None, tenant=None):
    if not _env('DEEPSEEK_API_KEY'):
        return {'ok': False, 'summary': 'No DEEPSEEK_API_KEY is set.',
                'detail': 'The reply text is written by DeepSeek, so without a '
                          'key there is nothing to answer with.'}

    from .services.clients import deepseek_call

    reply = deepseek_call(
        [{'role': 'system',
          'content': 'You are a plumbing assistant. Reply in one short sentence.'},
         {'role': 'user',
          'content': 'Hi, is someone able to come and look at a leaking geyser?'}],
        max_tokens=60, timeout=15, retries=1,
    )
    text = (reply or '').strip()
    if not text:
        return {'ok': False, 'summary': 'DeepSeek answered with nothing.',
                'detail': 'The call succeeded but the reply was empty, which is '
                          'the signature of thinking mode eating the token '
                          'budget. Check DEEPSEEK_THINKING.'}
    return {'ok': True, 'summary': 'DeepSeek wrote a reply.',
            'detail': f'It said: "{text}" Nothing was sent to anybody.'}
