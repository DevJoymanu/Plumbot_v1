"""
bot/shared_contact.py
=====================
A lead sends us a contact card ("speak to my husband").

WHAT (owner, 2026-09-22):
  * A card AND an instruction to contact that person ("call him", "contact
    them", "speak to my husband"; Shona "mufonerei") emails the plumber AT ONCE
    with a call script, and the lead hears "Thanks, we'll give Tendai a call."
  * A card on its own: we are unsure what they want, so we ask ("Thanks for
    Tendai's number. Should we give them a call about the job?"). A yes, or an
    instruction, sends the email; a no keeps the number on file.

WHY: until 2026-09-22 a contact card got nothing at all (the handler crashed
silently). A card is usually the person who decides or who is home, and the
plumber's call is the fastest way to them: the bot has no free window to
message someone who has never written to us.

HOW: the webhook turns the card into a transcript turn, "[Sent contact] Tendai
Moyo +263771234567", and queues it through the same 45-second batching
window as text, so "call my husband" and the card are read as ONE turn
whichever arrived first (`handle_turn`, early in `_generate_and_schedule_reply`).
The contact is kept on the lead as `[SHARED_CONTACT] name|number` in
internal_notes (no migration); an open question is `[CONTACT_ASK_PENDING]`,
cleared on the next turn whatever it says (the customer's own words win); a
sent brief is `[CONTACT_BRIEF_SENT] number`, so a contact is briefed once.
The email is a call email (bot/call_brief.build_call_email, subject "Please
call today: …"), so the 24-hour reminder covers it too. Deterministic, English
and common Shona. Pinned by `bot/test_shared_contact.py`.
"""

import logging
import re

from django.utils import timezone

logger = logging.getLogger(__name__)

MARK = '[Sent contact]'
CONTACT_TAG = '[SHARED_CONTACT]'
ASK_TAG = '[CONTACT_ASK_PENDING]'
BRIEFED_TAG = '[CONTACT_BRIEF_SENT]'

# "Call him", "contact them", "please speak to my husband", "phone this number",
# "he will explain", Shona "mufonerei" / "taurai naye".
_INSTRUCTION = re.compile(
    r"\b(call|contact|phone|ring|speak\s+(?:to|with)|talk\s+(?:to|with)|reach|"
    r"get\s+(?:hold\s+of|in\s+touch\s+with)|chat\s+(?:to|with)|whatsapp|message|text)\b"
    r".{0,40}\b(him|her|them|this\s+(?:number|person|guy|lady|one|contact)|"
    r"my\s+\w+|the\s+(?:owner|landlord|builder|contractor|foreman))\b"
    r"|\b(?:he|she|they)\s+(?:will|can|would)\s+(?:explain|tell\s+you|help|show\s+you)\b"
    r"|\b(?:mu|va)?fonerei\b|\bfonerai\b|\btaurai\s+naye\b|\bmutaurirei\b|\bmubatei\b",
    re.IGNORECASE)
_YES = re.compile(
    r"^\s*(yes|yeah|yep|yah|ya|sure|ok(?:ay)?|please|pls|go\s+ahead|do\s+so|correct|"
    r"ehe|hongu|zvakanaka)\b", re.IGNORECASE)
_NO = re.compile(r"^\s*(no|nope|nah|not\s+now|kwete|aiwa|don'?t|do\s+not)\b", re.IGNORECASE)


def card_lines(contacts):
    """[(name, number)] from WhatsApp's `contacts` payload, cards without a number skipped."""
    out = []
    for card in contacts or []:
        name = ((card.get('name') or {}).get('formatted_name')
                or (card.get('name') or {}).get('first_name') or '').strip()
        phones = card.get('phones') or []
        number = next((str(p.get('phone') or p.get('wa_id') or '').strip()
                       for p in phones if p.get('phone') or p.get('wa_id')), '')
        if number:
            out.append((name or 'your contact', number))
    return out


def marker_text(contacts):
    """The transcript turn for a card: "[Sent contact] Tendai Moyo +263771234567"."""
    return '\n'.join('%s %s %s' % (MARK, name, number)
                     for name, number in card_lines(contacts))


def _parse_marker(message):
    """(name, number) from the first "[Sent contact] …" line in a turn, or None."""
    found = re.search(r'\[Sent contact\]\s+(.*?)\s+(\+?[\d\s()-]{7,})\s*$',
                      message or '', re.MULTILINE)
    if not found:
        return None
    return found.group(1).strip(), re.sub(r'[^\d+]', '', found.group(2))


def _said(message):
    """What they typed this turn, without the card line(s)."""
    return '\n'.join(line for line in (message or '').splitlines()
                     if not line.strip().startswith(MARK)).strip()


def _first(name):
    return (name or 'them').split()[0]


def _tag_value(notes, tag):
    found = re.findall(re.escape(tag) + r' ([^\n]*)', notes or '')
    return found[-1].strip() if found else ''


def _set_tag(appointment, tag, value):
    notes = re.sub(r'\n?' + re.escape(tag) + r'[^\n]*', '', appointment.internal_notes or '').strip()
    appointment.internal_notes = (notes + ('\n%s %s' % (tag, value) if value else '')).strip()
    appointment.save(update_fields=['internal_notes'])


def asks_us_to_contact(text) -> bool:
    return bool(_INSTRUCTION.search(text or ''))


def handle_turn(appointment, message_body):
    """The lead-facing reply for a contact-card turn, or None to carry on.

    A card with an instruction: brief the plumber, "we'll give X a call".
    A card alone: ask. The answer to our ask: a yes or an instruction
    briefs, a no keeps the number. Anything else answers nothing here.
    """
    from bot import copy_catalog
    card = _parse_marker(message_body)
    said = _said(message_body)
    notes = appointment.internal_notes or ''

    if card:
        name, number = card
        _set_tag(appointment, CONTACT_TAG, '%s|%s|%s' % (name, number, timezone.now().isoformat()))
        if asks_us_to_contact(said):
            send_brief(appointment, name, number, said)
            return copy_catalog.CONTACT_WILL_CALL.format(name=_first(name))
        _set_tag(appointment, ASK_TAG, '%s|%s' % (name, number))
        return copy_catalog.CONTACT_ASK.format(name=_first(name))

    pending = _tag_value(notes, ASK_TAG)
    if pending:
        _set_tag(appointment, ASK_TAG, '')          # cleared whatever they say
        name, _, number = pending.partition('|')
        if _YES.search(said) or asks_us_to_contact(said):
            send_brief(appointment, name, number, said)
            return copy_catalog.CONTACT_WILL_CALL.format(name=_first(name))
        if _NO.search(said):
            return copy_catalog.CONTACT_KEPT.format(name=_first(name))
        return None

    # "Call my husband" arriving after the card's turn was already answered:
    # the latest shared contact, if it has not been briefed yet.
    shared = _tag_value(notes, CONTACT_TAG)
    if shared and asks_us_to_contact(said):
        name, number = (shared.split('|') + ['', ''])[:2]
        if number and _tag_value(notes, BRIEFED_TAG) != number:
            send_brief(appointment, name, number, said)
            return copy_catalog.CONTACT_WILL_CALL.format(name=_first(name))
    return None


def send_brief(appointment, name, number, said=''):
    """Email the plumber a call script for the shared contact. Returns True if sent.

    The owner's call-email format: script first with everything we know in it,
    visit first (online only if they are hesitant), details last, and the
    "Log the call" button (the lead's phone-quote form).
    """
    from bot.call_brief import CALL_SUBJECT_PREFIX, _next_two_working_days, build_call_email
    from bot.phone_quote import ensure_request, form_url
    from bot.plumber_notifications import (send_email_to_recipients,
                                           split_notification_recipients)
    from bot.utils import business_name_for

    tenant = getattr(appointment, 'tenant', None)
    try:
        plumber = (appointment.plumber_display_name() or '').strip()
    except Exception:
        plumber = ''
    plumber = '' if plumber == 'the plumber' else plumber
    plumber_first = plumber.split()[0] if plumber else ''
    business = business_name_for(appointment, default='').strip()
    job = ' '.join((appointment.project_description or '').split())[:120] or 'the plumbing work'
    area = (appointment.customer_area or '').strip()
    lead_name = (appointment.customer_name or '').strip()
    lead_phone = (appointment.phone_number or '').replace('whatsapp:', '')
    first = _first(name)
    days = _next_two_working_days(appointment, timezone.now())
    who = ("it's %s from %s. I'm the lead plumber and I handle our quotes" % (plumber_first, business)
           if plumber_first else "it's %s" % (business or 'the plumbing team'))
    passed_on = ('%s passed on your number' % lead_name) if lead_name else \
        'Someone we were chatting to on WhatsApp passed on your number'

    opening = ("Hi %s, %s. %s and asked us to give you a call about %s%s. "
               "Would %s or %s suit you better for us to come and have a quick look at the space?"
               % (first, who, passed_on, job, (' in %s' % area) if area else '', days[0], days[1]))
    branches = [
        ("If they pick a day", [
            '"Morning or afternoon?"',
            'Then: "Perfect. It only takes about 20 minutes, and I\'ll give you an exact quote."']),
        ("If they're not the right person", [
            '"No problem. Who should I speak to about it?" Note the name and number.']),
        ("Only if they're hesitant about a visit, or short on time", [
            '"No problem. If it\'s easier, you can send a few photos and measurements to me on '
            'this number on WhatsApp, and I\'ll price it from those."',
            'They message you first. Never message them cold from your business number.']),
        ("If they're no longer going ahead", ['"No problem at all, thanks for letting me know."']),
        ("If there's no answer", ['Try once more later today, then leave it.']),
    ]
    details = [
        ('Call', '%s, %s' % (name, number)),
        ('Passed on by', '%s%s' % ((lead_name + ', ') if lead_name else '', lead_phone)),
        ('Job', job),
    ]
    if area:
        details.append(('Area', area))
    if said:
        details.append(('What the lead said', '"%s"' % said[:200]))
    details.append(('When', timezone.localtime().strftime('%a %d %b, %H:%M')))
    intro = ('Hi %s, please call %s today and read the script below. %s shared their '
             'number on WhatsApp and asked us to contact them.'
             % (plumber_first or 'there', name, lead_name or 'A lead'))
    url = form_url(ensure_request(appointment))
    text, html = build_call_email(intro, opening, branches, details, url)
    to, hidden = split_notification_recipients(tenant)
    subject = '%s%s (%s), %s' % (CALL_SUBJECT_PREFIX, name, number, job[:60])
    ok = send_email_to_recipients(to, subject, text, html_message=html, tenant=tenant,
                                  appointment=appointment, bcc=hidden,
                                  category='plumber_alert', to_role='plumber')
    if ok:
        _set_tag(appointment, BRIEFED_TAG, number)
    else:
        logger.warning('Contact brief email failed for lead %s', appointment.pk)
    return bool(ok)
