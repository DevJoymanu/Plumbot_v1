"""
bot/lead_handoff.py
===================
The plumber messaging a lead from their OWN WhatsApp, with the message written.

Same handoff as the quote (``quotation_whatsapp_handoff``) and for the same two
reasons: it comes from the number the customer already knows, and a ``wa.me``
link is not bound by the 24h messaging window that stops the bot's own sends.
What is added here is the drafting. The plumber should not have to scroll the
transcript to work out what we already know and what is still missing.

The message has two halves and they do different jobs:

* The RECAP says back everything we hold — the job, the area, the timeline, and
  the plan if one arrived. That is proof we read them, and it is why the
  customer does not have to repeat themselves to a second person.
* The ASK is ONE question, the next gap in the flow's own order, carrying the
  reason it is being asked. People answer the last question they are given, so
  a message asking three things collects one answer and wastes two.

The order is the flow's own: what the job is, then the area, then the timeline,
then the email. Email comes last on purpose. Asked cold it reads as data
collection; asked once we know what they want and when, it is obviously just
the thing the quote gets sent to.

The draft is a starting point, never a send. Nothing here contacts anybody: the
view hands the text to the plumber's own WhatsApp and they decide what goes.
"""

import logging

from .utils import business_name_for, strip_dashes, strip_emojis

logger = logging.getLogger(__name__)

# A blank line between blocks. Written this way because the escape does not
# survive every editing route this file has been through.
_BREAK = chr(10) + chr(10)

# What the person signing the message is to the customer. Stated because a
# name on its own is just a name: "Kudakwashe Marange" could be anybody at the
# company, and "lead plumber" is the difference between a message from the
# business and a message from the person who will be doing the work.
PLUMBER_ROLE = 'lead plumber'

# What the flow collects, in the order it asks for it. The label is what the
# page shows; the ORDER is what decides which single question the draft asks.
LEAD_FIELDS = (
    ('project_description', 'The job'),
    ('customer_area', 'Area'),
    ('timeline', 'Timeline'),
    ('customer_email', 'Email'),
    ('customer_name', 'Name'),
)


def _value(appointment, field):
    return str(getattr(appointment, field, '') or '').strip()


def service_label(appointment) -> str:
    """The lead's service type, worded the way it was offered to them.

    'other' is not a service. It is what the flow stores when it does not know,
    so reading it back as though it were a fact makes the recap claim something
    the customer never said.
    """
    raw = getattr(appointment, 'project_type', None)
    if not raw or str(raw).strip().lower() == 'other':
        return ''
    try:
        label = appointment.get_project_type_display()
    except Exception:
        label = str(raw).replace('_', ' ').strip()
    # Spoken copy, not a dropdown: nobody says "and-sign" out loud.
    return label.replace('&', 'and')


def has_plan(appointment) -> bool:
    """A real plan on file, not one that was merely promised.

    Reads ``plan_status``, never ``has_plan``: that flag goes true the moment a
    lead SAYS a drawing is coming, and a promised plan is nothing to quote from.
    """
    return str(getattr(appointment, 'plan_status', '') or '') == 'plan_uploaded'


def collected(appointment) -> list:
    """[(label, value), ...] for everything we actually hold."""
    out = []
    service = service_label(appointment)
    if service:
        out.append(('Service', service))
    for field, label in LEAD_FIELDS:
        value = _value(appointment, field)
        if value:
            out.append((label, value))
    if has_plan(appointment):
        out.append(('Plan', 'Received'))
    return out


# ── The ask ─────────────────────────────────────────────────────────────────
# ONE message that collects everything. The plumber hits send once and gets
# every outstanding answer back, rather than working a lead across four
# messages over four days.
#
# This is a deliberate departure from the bot's own one-question rule (owner
# decision, 2026-09-07), and the two are not in conflict: the bot asks one
# thing at a time because a machine reciting a checklist reads as an
# interrogation, while a named human asking for a few details so they can price
# a job is just how the trade already works. What IS kept is the single
# question mark. The list arrives as one request, not as a stack of separate
# questions, because that is the difference between "could you let me know X, Y
# and Z?" and an inbox form.

# Each gap as the thing WE NEED plus WHY we need it. The reason is the whole
# point of the format: "what area you're in" is a form field, "what area you're
# in, so I know if we cover you" is a person explaining themselves. Wording
# follows the owner's own takeovers in
# .claude/skills/plumbot-sales-flow/references/house-voice.md — plain words,
# contractions, no wind-up.
NEEDS = {
    'project_description': ('what exactly you want done',
                            'so I know what to price'),
    'customer_area': ("what area you're in",
                      'so I know if we cover you'),
    'timeline': ('when you were hoping to get it done',
                 "so I can check we're free"),
    'customer_email': ('the best email to reach you on',
                       'so I can send the quote over'),
    'customer_name': ('what name to put it under', 'for the booking'),
}


def _need(appointment, field) -> str:
    """One numbered item: the thing, then why we are asking for it.

    Only the job detail changes with context. Where we already named the
    service in the recap, asking flatly what they want done reads as though we
    had not just said we knew: what is missing is what that job involves.
    """
    thing, why = NEEDS[field]
    if field == 'project_description':
        service = service_label(appointment)
        if service:
            thing = f'what the {service.lower()} involves'
            why = 'so nothing gets left off the price'
    return f'{thing}, {why}'


def _gaps(appointment) -> list:
    """The fields still outstanding, in the order we ask for them."""
    plan = has_plan(appointment)
    out = []
    for field, _label in LEAD_FIELDS:
        if field not in NEEDS or _value(appointment, field):
            continue
        # A plan on file IS the detail of what needs doing, so asking for it
        # again reads as though nobody opened the drawing they sent.
        if plan and field == 'project_description':
            continue
        out.append(field)

    # The name is a BOOKING question, not a quote question. In the owner's own
    # takeovers it comes last and on its own: "what name should we put on the
    # booking?". Nothing about a quote needs it, so it only rides along when it
    # is the last thing outstanding.
    if len(out) > 1 and 'customer_name' in out:
        out.remove('customer_name')
    return out


def build_ask(appointment) -> str:
    """Everything outstanding, numbered, each with the reason we want it.

    A numbered list because these are things to go and find out, and a list
    someone can work down and tick off gets answered; a paragraph of four
    requests gets one answer and a vague apology. One item is not a list
    though, so a single gap stays a sentence.
    """
    gaps = _gaps(appointment)
    if not gaps:
        return ''

    # A name prices nothing, so "to give you an accurate quote" is the wrong
    # reason to give for it. On its own it is the booking question the owner
    # actually asks, in the words they ask it in.
    if gaps == ['customer_name']:
        return 'One last thing, what name should I put it under?'

    items = [_need(appointment, f) for f in gaps]
    # The plan is what makes a real price possible without a visit, so it is
    # the opener worth giving: they already did the hard part.
    opener = ('I can price it off the plan you sent. I just need'
              if has_plan(appointment)
              else "To give you an accurate quote, I'll need")

    if len(items) == 1:
        return f'{opener} one more thing: {items[0]}.'

    # Each line starts as a sentence would. A numbered list whose items are
    # lowercase reads as a fragment of something else.
    numbered = chr(10).join(
        f'{n}. {item[0].upper()}{item[1:]}'
        for n, item in enumerate(items, 1))
    return f'{opener} a few more things:{chr(10)}{chr(10)}{numbered}'


def missing(appointment) -> list:
    """[(label, need clause), ...] still outstanding, in the order we ask."""
    return [(label, NEEDS[field][0])
            for field, label in LEAD_FIELDS
            if not _value(appointment, field) and field in NEEDS]


# ── The recap ────────────────────────────────────────────────────────────────

def _subject(appointment) -> str:
    """What the job is, for the "I have you down for a ..." frame.

    The SERVICE LABEL only. A project description is the customer's own
    sentence, not a noun phrase, and dropping one into this frame produces
    "I have you down for we are redoing the whole upstairs bathroom and the
    downstairs guest toilet, plus moving.. in Ruwa". Truncating it does not
    help; the problem is the grammar, not the length. A description we hold
    but cannot phrase is acknowledged separately in _recap instead.
    """
    return service_label(appointment).lower()


def _timeline_phrase(timeline: str) -> str:
    """The timeline as a clause that can follow the job and the area."""
    low = timeline.strip().rstrip('.').lower()
    if low in ('asap', 'urgent', 'immediately', 'now', 'as soon as possible'):
        return 'looking to get it done as soon as possible'
    return f'looking to get it done {low}'


def _tidy(text: str) -> str:
    """A stored value as it should read mid-sentence."""
    # Spoken copy, not a dropdown: nobody says "and-sign" out loud.
    return text.strip().rstrip('.').strip().replace('&', 'and')


# A description that starts like a sentence cannot sit inside "I've got you
# down for ___". Measured against the 256 descriptions on file, that is 3 of
# them: the frame is right for 99% and the exception falls back rather than
# the other way round, which is how "I have you down for we are redoing the
# whole upstairs bathroom" got sent.
_SENTENCE_STARTERS = ('i ', "i'm ", 'im ', 'we ', "we're ", 'my ', 'our ',
                      'they ', 'it ', 'there ', 'you ', 'he ', 'she ',
                      'need ', 'want ', 'looking ', 'can ', 'do ', 'is ')


def _names_a_thing(text: str) -> bool:
    """Can this go straight after "you down for"?"""
    return bool(text) and not text.lower().startswith(_SENTENCE_STARTERS)


def _recap(appointment) -> list:
    """The sentences saying back what we already hold. May be empty.

    One sentence carrying the job, the area and the timeline, in whatever
    combination we have, then the plan on its own. Absent means omit, never a
    placeholder.

    THE JOB IS THEIR OWN WORDS where they gave us any. "Bathroom Renovation"
    is the category we filed them under; "full ensuite refit, new tub and
    shower" is the job, and it is the thing they can confirm or correct. The
    service label is the fallback, not the first choice.
    """
    service = service_label(appointment)
    notes = _tidy(_value(appointment, 'project_description'))
    area = _value(appointment, 'customer_area')
    timeline = _value(appointment, 'timeline')
    lines = []

    # No article on a description: "a bathroom and toilet maintenance" is
    # wrong and there is no safe rule for when to add one. Service labels are
    # known-countable, so they keep theirs.
    if _names_a_thing(notes):
        subject = notes
    elif service:
        subject = f'a {service.lower()}'
    else:
        subject = ''

    if subject and area:
        head = f"I've got you down for {subject} in {area}"
    elif subject:
        head = f"I've got you down for {subject}"
    elif area:
        head = f"I've got you down as being in {area}"
    else:
        head = ''

    if head and timeline:
        lines.append(f'{head}, {_timeline_phrase(timeline)}.')
    elif head:
        lines.append(f'{head}.')
    elif timeline:
        lines.append(f"I've got you down as {_timeline_phrase(timeline)}.")

    # Say thank you for what they DID give us, before asking for more. It
    # costs one short sentence and it is the difference between a recap and a
    # demand. The plan carries its own thanks, so we never say it twice; and
    # with nothing on file there is nothing to thank them for, so it is
    # omitted rather than faked.
    if has_plan(appointment):
        lines.append('Thanks for sending the plan through.')
    elif lines:
        lines.append('Thanks for that.')

    return lines


# ── Who it is from ───────────────────────────────────────────────────────────

def _signature(appointment) -> str:
    """Who this is from, using the lead's OWN tenant.

    Absent means omit, never borrow. A tenant with no plumber name on file gets
    the business name; one with neither gets no signature line at all, which is
    better than signing another business's name to their message.
    """
    try:
        name = (appointment.plumber_display_name() or '').strip()
    except Exception:
        name = ''
    business = business_name_for(appointment, default='').strip()

    if name and name != 'the plumber' and business:
        return f'{name}, {PLUMBER_ROLE} at {business}'
    if name and name != 'the plumber':
        return f'{name}, {PLUMBER_ROLE}'
    # A business with no name on file signs as the business. We will not call
    # an unnamed person the lead plumber: the role is only worth stating
    # because it says WHO the customer is dealing with.
    return business


# ── The draft ────────────────────────────────────────────────────────────────

def build_message(appointment) -> str:
    """The draft the plumber sends. Recap everything, ask one thing."""
    name = _value(appointment, 'customer_name')
    greeting = f'Hi {name},' if name else 'Hi there,'

    signature = _signature(appointment)
    sign_off = _BREAK + signature if signature else ''

    slot = getattr(appointment, 'scheduled_datetime', None)
    if getattr(appointment, 'status', '') == 'confirmed' and slot:
        # Booked. Re-qualifying somebody who has already agreed a time is the
        # repeat-pitch bug in a new channel.
        when = slot.strftime('%A %d %B at %H:%M')
        body = (f"Just confirming we're coming out on {when}. "
                f'Does that still work for you?')
        return _clean(greeting + _BREAK + body + sign_off)

    # The greeting keeps its own line. Joined with a comma the next word keeps
    # its capital ("Hi there, What exactly...") and lowercasing it is not safe,
    # because half of these continuations start with "I".
    blocks = [greeting]

    recap = _recap(appointment)
    if recap:
        blocks.append(' '.join(recap))

    blocks.append(build_ask(appointment)
                  or 'Shall I get you booked in for a visit?')

    return _clean(_BREAK.join(blocks) + sign_off)


def _clean(text: str) -> str:
    """The same outbound rules the bot's own copy obeys.

    Run here rather than trusted to whoever writes the literals above: this
    text goes to a customer, so it gets the emoji and dash strippers exactly
    like every other customer-facing string.
    """
    return strip_dashes(strip_emojis(text or '')).strip()
