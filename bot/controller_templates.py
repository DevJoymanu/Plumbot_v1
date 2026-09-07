"""
bot/controller_templates.py
===========================
The deterministic copy behind each controller move (spec §5).

The model picks the move. This module writes the words. Everything the customer
must be able to trust lives here: the days we can actually come, the fee and
the refund beside it, what we do and do not do.

Voice: see `.claude/skills/plumbot-sales-flow/references/house-voice.md`. Short,
plain words, one question, no emojis and no dashes. That is not a style
preference. It is how the owner writes when they take over from the bot, and
those takeovers book at more than twice the rate the bot does.

Phase 1 ships `slow_lead_nudge`. Phase 2 adds show_examples, paid_visit_close
and fee_objection here beside it.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def slow_lead_nudge(appointment=None, subtype: str = 'unknown',
                    is_shona: bool = False, tenant_cfg=None,
                    today=None) -> str:
    """The lead has gone quiet or put us off, but is still in the window.

    Lower the ask, keep the destination. Two shapes, picked by what is actually
    in the way:

    - They WANT it and something practical is blocking (they are at work, they
      need to sort access). Nothing to discover, so offer the two days.
    - They are away, or we have no idea when. Asking for a day would be asking
      them to guess, so ask for a rough timeframe instead. "Even next week or
      month end helps" is there to make a vague answer feel allowed, because a
      vague answer is still an answer we can work with.

    Never the portfolio and never an email ask. That is the exit this replaces.
    A lead who is ready now and gets offered a catalogue takes the catalogue.
    The portfolio still belongs to the genuinely future dated lead, where it is
    the reason we can ask for an email at all (spec §11).
    """
    from bot.visit_slots import slot_offer

    offer = slot_offer(tenant_cfg, today=today, is_shona=is_shona)

    if subtype == 'busy':
        if is_shona:
            body = ('Vazhinji vevatengi vedu vanoshanda masikati, saka '
                    'tinouyawo manheru nemaweekend.')
        else:
            body = ('Plenty of our clients work during the day, so we do '
                    'evenings and weekends too.')
        return f'{body} {offer}'.strip() if offer else _timeframe_ask(is_shona)

    if subtype == 'access':
        if is_shona:
            body = ('Hapana dambudziko. Gadzirisai kuti tikwanise kupinda, '
                    'isu tinotevedzera nguva yenyu.')
        else:
            body = ('No problem. Sort the access on your side and we will '
                    'work around you.')
        return f'{body} {offer}'.strip() if offer else _timeframe_ask(is_shona)

    # Travelling, or we simply do not know. Asking for a day here asks them to
    # guess, and a guess is what we then have to unpick later.
    if subtype == 'travelling':
        if is_shona:
            return 'Hapana dambudziko. Munodzoka rini zvakadaro?'
        return 'No problem. Roughly when are you back?'

    return _timeframe_ask(is_shona)


def paid_visit_close(tenant_cfg, is_shona: bool = False, today=None) -> str:
    """The close. What the visit buys, what it costs, and two days to pick from.

    The fee and the refund are ONE sentence, never two. Split across sentences
    the fee lands on its own and the lead has a beat to react to it before the
    refund arrives. Together it reads as one offer. A test asserts they share a
    sentence, because this is the rule most likely to be lost in a later edit.

    The figure comes from the tenant, never from here, and a tenant with no fee
    on file says the visit is free rather than borrowing someone else's number.
    The days come from visit_slots, never from a model.
    """
    from bot.visit_slots import slot_offer

    fee = getattr(tenant_cfg, 'consultation_fee', None)
    currency = getattr(tenant_cfg, 'currency', 'US$') or 'US$'
    waived = False
    try:
        waived = bool(tenant_cfg.visit_fee_waived_on_job())
    except Exception:
        logger.warning("Could not read the visit fee waiver", exc_info=True)

    offer = slot_offer(tenant_cfg, today=today, is_shona=is_shona)

    if is_shona:
        why = ('Nzira iri nani yekukupai mutengo chaiwo ndeyekuuya '
               'kuzoona nzvimbo. Tinoyera toona zvese, mozowana mutengo '
               'wakagadzikana, pasina zvinozomuka gare gare.')
        if not fee:
            cost = 'Kuuya kwedu kumahara.'
        elif waived:
            cost = (f'Kuuya kunoita {currency}{fee}, uye tinoibvisa '
                    f'pamutengo webasa kana masarudza kuenderera mberi.')
        else:
            cost = f'Kuuya kunoita {currency}{fee}.'
    else:
        why = ('The best way to give you an exact price is a quick visit. '
               'We measure up and you get a fixed price, no surprises later.')
        if not fee:
            cost = 'The visit is free.'
        elif waived:
            # One sentence. The refund never gets its own.
            cost = (f'The visit is {currency}{fee} and it comes off the job '
                    f'when you go ahead.')
        else:
            cost = f'The visit is {currency}{fee}.'

    return ' '.join(p for p in (why, cost, offer) if p)


def fee_objection(tenant_cfg, is_shona: bool = False, today=None) -> str:
    """They hesitated on the fee. Reframe once, then ask again.

    One reframe, not an argument. The fee buys certainty instead of a guess,
    and it comes back, so a lead who goes ahead pays nothing for it. Then a
    day, because a close that ends without an ask is just a speech.

    Never restates the figure for a tenant with no fee: a lead told the visit
    is free has no fee to object to, and answering one they never raised
    invents it for them.
    """
    from bot.visit_slots import next_two_slots

    fee = getattr(tenant_cfg, 'consultation_fee', None)
    currency = getattr(tenant_cfg, 'currency', 'US$') or 'US$'
    slots = next_two_slots(tenant_cfg, today=today, is_shona=is_shona)
    first = slots[0].label if slots else ''

    if is_shona:
        body = ('Tinouya kuti mutengo wenyu uve chaiwo, pasina kufungidzira.')
        if fee:
            body += (f' Munodzoserwa {currency}{fee} kana masarudza '
                     f'kuenderera mberi, saka basa rikaitwa hamubhadhari '
                     f'chinhu pakuuya kwedu.')
        ask = f'Ndokubhukira {first} here?' if first else 'Ndokubhukira nguva here?'
    else:
        body = ('It is just so your price is exact instead of a guess.')
        if fee:
            body += (f' You get the {currency}{fee} back when you go ahead, '
                     f'so if you do the job it costs you nothing.')
        ask = f'Shall I lock in {first}?' if first else 'Shall I lock in a time?'

    return f'{body} {ask}'


def show_examples(appointment=None, is_shona: bool = False,
                  next_question: str = '') -> str:
    """The line that rides out with the photos.

    Short, because the photos are the message. Whatever question comes next
    travels with them or it never gets asked: the photo path returns outright,
    so a question left behind is a lead left waiting.
    """
    lead_in = ('Heano mamwe emabasa atakapedza.' if is_shona
               else 'Here are a couple we just finished.')
    question = (next_question or '').strip()
    # No question is a real answer here, not a missing one: on the proof step
    # the question rides out BEHIND the images, so putting it on the intro too
    # would ask it twice, once before they have seen anything.
    if not question:
        return lead_in
    return f'{lead_in} {question}'


def photo_followup(appointment=None, is_shona: bool = False) -> str:
    """The line that follows the gallery. Deterministic, no API call.

    This used to be a DeepSeek call per photo send, and what it produced was a
    nudge toward the "free on-site visit" — which `strip_repeat_free_visit`
    then had to take back out, because by that point the visit has usually
    been pitched already. So we paid for a call to generate a sentence another
    part of the system existed to delete.

    What should follow photos is the next thing we actually need to know. The
    order matches the close (spec §2): the description, then the area. A lead
    who has given us both gets a light open question instead of a fourth ask.
    """
    description = str(getattr(appointment, 'project_description', '') or '').strip()
    area = str(getattr(appointment, 'customer_area', '') or '').strip()

    if not description:
        return ('Chii chaicho chamunoda kuitwa?' if is_shona
                else 'What exactly are you looking to get done?')
    if not area:
        return 'Munogara kupi?' if is_shona else 'Whereabouts are you?'
    return ('Pane chamaona ipapo chamunofarira here?' if is_shona
            else 'Anything there you liked the look of?')


def _timeframe_ask(is_shona: bool = False) -> str:
    """The lowered ask: a rough when, not a day.

    Lifted almost word for word from what the owner sends. "When were you
    hoping to get this done?" is in the export three times, once as a
    correction after the bot asked something worse.
    """
    if is_shona:
        return ('Hapana dambudziko. Mairi kuda kuti zviitwe rini? '
                'Kunyange svondo rinouya kana kupera kwemwedzi zvinobatsira.')
    return ('No problem. When were you hoping to get this done? '
            'Even next week or month end helps.')


# Scripted questions carry a warm opener ("All good,", "Great,", "Got it!")
# because they are usually a reply to something the customer just said. Riding
# behind a statement of ours they acknowledge nothing, and two openers in a row
# read as two canned messages rather than one person talking.
_LEADING_ACK = re.compile(
    r'^(great|nice one|nice|perfect|awesome|all good|got it|sure|right|lovely)'
    r'[,!.]?\s+',
    re.IGNORECASE,
)


def question_without_ack(question: str) -> str:
    """A scripted question with its opening acknowledgement removed.

    For a question that FOLLOWS our own copy rather than answering the
    customer: after "Here are a couple we just finished", "All good, what area
    are you in?" is acknowledging nothing.

    Leaves anything that is not a known opener alone, so a question that begins
    with a real word survives untouched.
    """
    text = (question or '').strip()
    if not text:
        return text
    stripped = _LEADING_ACK.sub('', text, count=1).strip()
    if not stripped:
        return text
    return stripped[0].upper() + stripped[1:]
