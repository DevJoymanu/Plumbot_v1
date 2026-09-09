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
* The ASK is everything still outstanding, in the flow's own order, each item
  carrying the reason it is being asked (see the owner decision at ``NEEDS``).
* With nothing outstanding there is nothing to ask, so the message CLOSES
  instead: a day already pencilled in gets confirmed, otherwise two days to
  pick from. A qualified lead who is never asked for a day never books.

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


def job_phrase(appointment) -> str:
    """The job as a bare noun phrase, in the customer's OWN words where we have
    any, else the service label, else empty.

    THE shared resolver for "what is this lead's job called" in copy that has to
    prove we read them. The recap in this module and the follow-up cron's
    fallback templates both read it, so a lead is never "a bathroom renovation"
    in one message and "your project" in the next. Their own words come first
    for the same reason the recap prefers them: "Bathroom Renovation" is the
    category we filed them under, and "full ensuite refit, new tub" is the job.

    No article, because every caller sits it after one of their own ("about the
    ...", "your ..."). `_recap` keeps its own article handling, which has to
    decide between "a bathroom renovation" and a description that takes none.
    """
    notes = _tidy(_value(appointment, 'project_description'))
    if _names_a_thing(notes):
        return notes
    return service_label(appointment).lower()


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


# How many things we are asking for, said out loud. Naming the number is the
# cheapest thing in the message and it does the most work: "a few more things"
# could be three or seven, and a lead who cannot see the end of a request puts
# it off. "Two things" is a job you can finish on the spot.
_COUNTS = {2: 'two things', 3: 'three things', 4: 'four things'}


def _count_phrase(n: int) -> str:
    return _COUNTS.get(n, 'a few things')


def _ask_payoff(appointment, single: bool = False) -> str:
    """What happens once they answer.

    A list of requests with no destination is extraction: the best case is a
    lead who answers and then waits. This is the line that makes the list worth
    working through, and it is the honest next step for the path they are on,
    never a promise on top of it.

    A plan on file means the drawing carries the measurements, so the answers
    are the last thing between them and a price. With no plan there is nothing
    to price off yet, so the next step is the look, described the way the owner
    describes it. It carries NO cost either way: what a visit costs is the
    tenant's own business and some of them charge for it.
    """
    # "one more thing" followed by "once I have those" reads as though nobody
    # proof-read it.
    it = 'that' if single else 'those'
    if has_plan(appointment):
        return f'Once I have {it} I can get the price over to you.'
    return (f'Once I have {it} we can come and see the place and get you '
            f'an exact price.')


def build_ask(appointment) -> str:
    """Everything outstanding, numbered, each with the reason we want it, and
    the thing that happens once they answer.

    A numbered list because these are things to go and find out, and a list
    someone can work down and tick off gets answered; a paragraph of four
    requests gets one answer and a vague apology. One item is not a list
    though, so a single gap stays a sentence.

    THE LIST IS THE ONLY ASK. The payoff line that closes it is a statement on
    purpose: a question after a list is the question they answer instead of the
    list.
    """
    gaps = _gaps(appointment)
    if not gaps:
        return ''

    # A name prices nothing, so "so I can get this priced properly" is the wrong
    # reason to give for it. On its own it is the booking question the owner
    # actually asks, in the words they ask it in.
    if gaps == ['customer_name']:
        return 'One last thing, what name should I put it under?'

    items = [_need(appointment, f) for f in gaps]
    # The opener names THEIR outcome, not our paperwork. "To give you an
    # accurate quote, I'll need" makes the list a favour to us; the same
    # request framed as the price they are waiting on is a step towards
    # something they already want. The plan is the better opener still, because
    # it says they have already done the hard part.
    opener = ('I can price it off the plan you sent. I just need'
              if has_plan(appointment)
              else 'So I can get this priced properly for you, I just need')

    payoff = _BREAK + _ask_payoff(appointment, single=len(items) == 1)

    if len(items) == 1:
        return f'{opener} one more thing: {items[0]}.{payoff}'

    # Each line starts as a sentence would. A numbered list whose items are
    # lowercase reads as a fragment of something else.
    numbered = chr(10).join(
        f'{n}. {item[0].upper()}{item[1:]}'
        for n, item in enumerate(items, 1))
    return (f'{opener} {_count_phrase(len(items))}:'
            f'{chr(10)}{chr(10)}{numbered}{payoff}')


def missing(appointment) -> list:
    """[(label, need clause), ...] the draft is actually going to ask for.

    Derived from ``_gaps``, never recomputed from the fields. Asked separately
    the two drifted, and every screen that shows "still missing" showed a
    different list from the message underneath it: a lead whose plan is on
    file was listed as still owing us a description that ``_gaps``
    deliberately does not ask for, and the name was listed on every card while
    ``_gaps`` drops it whenever anything else is outstanding. A panel headed
    "still missing" that names things nobody is going to ask for is worse than
    no panel, because the plumber then chases them by hand.
    """
    labels = dict(LEAD_FIELDS)
    return [(labels[field], NEEDS[field][0]) for field in _gaps(appointment)]


# ── The close ────────────────────────────────────────────────────────────────

def _when(slot) -> str:
    """A slot as the customer should read it: their clock, not the database's.

    Stored aware and in UTC, so ``strftime`` straight off the field tells a
    Harare customer to expect us two hours before we turn up.
    """
    try:
        from django.utils import timezone
        slot = timezone.localtime(slot)
    except Exception:
        logger.warning("Could not localise the visit slot", exc_info=True)
    return slot.strftime('%A %d %B at %H:%M')


def booking_close(appointment) -> str:
    """Nothing left to find out, so the only thing left is the day.

    Two shapes, and which one runs is decided by whether a day is already on
    the row:

    * A slot pencilled in gets CONFIRMED. Offering fresh days to somebody who
      already has one re-pitches a visit they have effectively agreed to,
      which is the repeat-pitch bug in a new channel. The priority board only
      excludes ``confirmed`` leads, so a pending lead holding a slot is
      exactly the lead this draft gets opened on.
    * Otherwise, two days to pick from. Not "shall I book you in?": a yes/no
      hands a lead who is already qualified a way to say no to a question they
      were never really being asked, and the Close stage has one shape, which
      is to offer a choice. The days come from ``visit_slots`` against the
      lead's OWN tenant, so we never name a day that tenant is shut.

    The look is described casually, and it carries NO price. What a visit
    costs is the tenant's own business and some of them charge for it, so a
    draft that called it free would be making that promise on their behalf.
    """
    slot = getattr(appointment, 'scheduled_datetime', None)
    if slot:
        # Deliberately NOT "I've got you down for ...": that is the recap's
        # own opening, and a message that used it twice read as though two
        # people had written it.
        return (f"Just confirming we're coming out on {_when(slot)}. "
                f'Does that still work for you?')

    from .tenant_config import get_config
    from .visit_slots import slot_offer

    try:
        offer = slot_offer(get_config(getattr(appointment, 'tenant', None)))
    except Exception:
        logger.warning("Could not work out which days to offer", exc_info=True)
        offer = ''

    look = 'I can come out and take a quick look at the space.'
    # No working day to offer means we do not invent one. An open question is
    # the honest version of the same message, and it is still a question about
    # WHEN rather than whether.
    return f'{look} {offer}' if offer else f'{look} When suits you best?'


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
    """The timeline as a clause that can follow the job and the area.

    Only the FIRST character is lowered, and only when the opening word is not
    an acronym. Lowercasing the whole string turned "Back in Zimbabwe on the
    22nd of December" into "back in zimbabwe on the 22nd of december": this is
    the customer's own wording and it routinely carries proper nouns, so
    flattening the case reads as though nobody proof-read it.
    """
    raw = timeline.strip().rstrip('.')
    if raw.lower() in ('asap', 'urgent', 'immediately', 'now',
                       'as soon as possible'):
        return 'looking to get it done as soon as possible'
    # Lower the first letter only when the opening word is an ordinary one.
    # Left alone: anything carrying internal capitals ("ASAP", "NOW-ish"), and
    # month names, which are the single most common way a timeline opens on a
    # proper noun ("December 22nd").
    head = raw.split(' ', 1)[0].strip(',.')
    _internal_caps = any(c.isupper() for c in head[1:])
    try:
        from .out_of_scope_handler import _MONTHS
        _is_month = head.lower() in _MONTHS
    except Exception:
        _is_month = False
    if head and not _internal_caps and not _is_month:
        raw = raw[0].lower() + raw[1:]
    return 'looking to get it done ' + raw


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

    # An acknowledgement that asks for nothing is a receipt. This invites the
    # cheapest possible agreement - "yeah that's right" - and a lead who has
    # just agreed with us once answers the ask underneath it more readily than
    # one who has only been read a list of their own details back.
    #
    # A STATEMENT, not a question: the ask below is the message's one question,
    # and two of them means only the last gets answered. It also earns its place
    # honestly, because the job in the recap is often the customer's own words
    # and we may well have read them wrong.
    if head or timeline:
        lines.append('Let me know if I have anything wrong.')

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

    booked = (getattr(appointment, 'status', '') == 'confirmed'
              and getattr(appointment, 'scheduled_datetime', None))
    if booked:
        # Booked. Re-qualifying somebody who has already agreed a time is the
        # repeat-pitch bug in a new channel, so this one skips the recap and
        # the ask entirely and says the only thing left worth saying.
        return _clean(greeting + _BREAK + booking_close(appointment)
                      + sign_off)

    # The greeting keeps its own line. Joined with a comma the next word keeps
    # its capital ("Hi there, What exactly...") and lowercasing it is not safe,
    # because half of these continuations start with "I".
    blocks = [greeting]

    recap = _recap(appointment)
    if recap:
        blocks.append(' '.join(recap))

    blocks.append(build_ask(appointment) or booking_close(appointment))

    return _clean(_BREAK.join(blocks) + sign_off)


# ── The quote ───────────────────────────────────────────────────────────────

def build_quote_message(appointment, name: str = '') -> str:
    """The message the QUOTE goes out with, from the plumber's own WhatsApp.

    It used to be "here is your quote for the bathroom renovation." and nothing
    more: a document handed over with no next step, so the best outcome was a
    lead who read it and did nothing. A quote IS the offer, which makes this
    message the CLOSE, and a close has a shape. Say what they are getting, then
    ask WHEN rather than WHETHER: a yes/no hands somebody a way to say no to a
    question they were never really being asked, while a choice between two days
    is answered by picking one.

    Deliberately carries NO figure. The total is in the document, and a number
    typed into a chat line that later disagrees with the PDF is worse than no
    number at all. Deliberately makes NO claim about what the price covers
    beyond what the document itself shows: "fixed", "all in" and "no extras on
    the day" are one tenant's USPs, and this text goes to another tenant's
    customer.

    `name` is the fallback for a standalone quote, whose lead is a stub with no
    customer name on it but whose sheet has one typed on it.
    """
    who = (_value(appointment, 'customer_name') if appointment is not None
           else '') or (name or '').strip()
    greeting = f'Hi {who},' if who else 'Hi there,'
    return _clean(greeting + _BREAK + quote_message_body(appointment))


def quote_message_body(appointment) -> str:
    """The quote message WITHOUT the greeting.

    Split out for the editors, which draft the same message in the browser and
    know a name the server does not: a standalone quote is typed on a blank
    sheet, so the client's name is in a form field rather than on any row. The
    server owns the copy, the page owns the greeting, and there is one wording
    of this message in the product rather than one per screen.
    """
    service = service_label(appointment).lower() if appointment is not None else ''

    return _clean(_BREAK.join([
        # Not "please find attached": the plumber attaches the PDF themselves in
        # their own app, and copy that promises otherwise reads as a lie when
        # they forget. What it does say is why the document is worth opening.
        f'Here is your quote for {"the " + service if service else "the work"}. '
        f'The full breakdown is in there so you can see exactly what you are '
        f'getting.',
        # The close. One question, and it is about the diary.
        'If you are happy with it I can get you booked in. Which suits you '
        'better, earlier in the week or later on?',
    ]))


def _clean(text: str) -> str:
    """The same outbound rules the bot's own copy obeys.

    Run here rather than trusted to whoever writes the literals above: this
    text goes to a customer, so it gets the emoji and dash strippers exactly
    like every other customer-facing string.
    """
    return strip_dashes(strip_emojis(text or '')).strip()
