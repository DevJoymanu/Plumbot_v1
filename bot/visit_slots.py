"""
bot/visit_slots.py
==================
Which days we offer for a site visit, and how we say them (spec §6).

The model never names a day. It sets a move; this module picks the real dates
and the words for them. That is the whole point of the split: a model that
names days will sooner or later name a Thursday that does not exist, or one the
tenant is closed on.

Two jobs:

- `next_two_slots()` — the close. Always tomorrow and the day after, rolled
  forward past any day the tenant does not work.
- `proposed_visit_date()` — the future-dated lead. A real day a few working
  days before the date they gave us.

Nothing here calls an API and nothing here reads the database. Pass in the
tenant config and, in tests, the day to treat as today.
"""

from __future__ import annotations

import logging
from datetime import date as _date, timedelta

logger = logging.getLogger(__name__)

# How far ahead we will roll looking for a working day before giving up. A
# tenant closed every day of the week would otherwise spin here.
_MAX_ROLL_DAYS = 14

# For a lead with a date in mind, how many working days before it we suggest
# coming out. Far enough ahead that the quote is ready, close enough that the
# price still means something.
DEFAULT_LEAD_WORKING_DAYS = 5

_WEEKDAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                  'Saturday', 'Sunday')

# Same order, Monday first. Taken from the mapping the classifier prompt
# already uses, so the day the lead is offered and the day the extractor reads
# back out of their reply cannot drift apart.
_SHONA_WEEKDAYS = ('Muvhuro', 'Chipiri', 'Chitatu', 'China', 'Chishanu',
                   'Mugovera', 'Svondo')


class VisitSlot:
    """A real date plus the words we use for it.

    The label is not decoration. "tomorrow" is only true when the date really
    is tomorrow, so when a closed day pushes the slot out, the label becomes
    the weekday name instead. Saying "tomorrow" about Wednesday is how a lead
    turns up on the wrong day.
    """

    __slots__ = ('date', 'label')

    def __init__(self, day, label):
        self.date = day
        self.label = label

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'VisitSlot({self.date}, {self.label!r})'

    def __eq__(self, other):
        return (isinstance(other, VisitSlot)
                and self.date == other.date and self.label == other.label)


def _today(today=None) -> _date:
    if today is not None:
        return today
    from django.utils import timezone
    import pytz
    return timezone.now().astimezone(pytz.timezone('Africa/Johannesburg')).date()


def _is_working_day(day, tenant_cfg) -> bool:
    if tenant_cfg is None:
        return True
    try:
        return bool(tenant_cfg.is_open_on(day.weekday()))
    except Exception:
        # A tenant with no hours on file should not lose their slots. Better to
        # offer a day they may be closed than to offer none at all.
        logger.warning("Could not read the tenant's working days", exc_info=True)
        return True


def _roll_to_working_day(day, tenant_cfg):
    """The first working day on or after `day`, or None if there is none."""
    for _ in range(_MAX_ROLL_DAYS):
        if _is_working_day(day, tenant_cfg):
            return day
        day = day + timedelta(days=1)
    logger.warning("No working day found within %d days", _MAX_ROLL_DAYS)
    return None


def _label_for(day, today, is_shona: bool = False,
               allow_day_after: bool = True):
    """What to call a date in a message.

    Relative words only when they are literally true; a weekday name otherwise.
    A date more than a week out gets the day and month, because "Tuesday" on
    its own is ambiguous once it is not this coming Tuesday.

    `allow_day_after` exists because "the day after" only means anything when
    "tomorrow" was said first. Offered on its own it makes the reader ask "the
    day after what?", which happens whenever tomorrow is a day the tenant is
    closed and the first slot rolls to the day after that.
    """
    gap = (day - today).days
    names = _SHONA_WEEKDAYS if is_shona else _WEEKDAY_NAMES
    if gap == 1:
        return 'mangwana' if is_shona else 'tomorrow'
    # Shona never takes "the day after": there is no short natural equivalent,
    # and naming the day is clearer than reaching for one.
    if gap == 2 and allow_day_after and not is_shona:
        return 'the day after'
    if 0 < gap <= 7:
        return names[day.weekday()]
    if is_shona:
        return f'{names[day.weekday()]} musi wa{day.day}'
    return f'{names[day.weekday()]} the {day.day}'


def next_two_slots(tenant_cfg=None, today=None, is_shona: bool = False):
    """The two days we offer at the close: tomorrow and the day after.

    Rolled forward past days the tenant does not work, and never the same day
    twice. Returns a list of up to two VisitSlot. An empty list means we could
    not find working days at all, and the caller must not offer any.

    The second slot may be called "the day after" only when the first really is
    tomorrow. On a Friday with Saturday closed the first slot is Sunday, and
    "the day after or Monday" would be asking the lead to work out what it is
    the day after.
    """
    today = _today(today)
    days = []
    candidate = today + timedelta(days=1)
    while len(days) < 2:
        day = _roll_to_working_day(candidate, tenant_cfg)
        if day is None:
            break
        days.append(day)
        candidate = day + timedelta(days=1)

    slots = []
    leads_with_tomorrow = bool(days) and (days[0] - today).days == 1
    for index, day in enumerate(days):
        slots.append(VisitSlot(day, _label_for(
            day, today, is_shona=is_shona,
            allow_day_after=(index == 1 and leads_with_tomorrow),
        )))
    return slots


def slot_offer(tenant_cfg=None, today=None, is_shona: bool = False) -> str:
    """The closing question, or '' when we have no days to offer.

    Short on purpose. This is the last line of a message, and the lead only has
    to pick one of two words.
    """
    slots = next_two_slots(tenant_cfg, today, is_shona=is_shona)
    if not slots:
        return ''
    if len(slots) == 1:
        if is_shona:
            return f'Tingauya {slots[0].label} here?'
        return f'Can we come {slots[0].label}?'
    first, second = slots[0].label, slots[1].label
    if is_shona:
        return f'{first.capitalize()} kana {second}?'
    return f'{first.capitalize()} or {second}?'


def proposed_visit_date(target, tenant_cfg=None, today=None,
                        lead_working_days: int = DEFAULT_LEAD_WORKING_DAYS,
                        is_shona: bool = False):
    """A real day to suggest for a lead who named a date in the future.

    Counts back `lead_working_days` working days from their target, then rolls
    forward to a working day. Never returns a day in the past, and never the
    target itself when we can help it: the visit has to happen before the job.

    Returns a VisitSlot, or None when the target is too close to fit a visit
    in front of it.
    """
    if target is None:
        return None
    today = _today(today)
    if target <= today:
        return None

    day = target
    counted = 0
    for _ in range(_MAX_ROLL_DAYS * 3):
        if counted >= lead_working_days:
            break
        day = day - timedelta(days=1)
        if day <= today:
            break
        if _is_working_day(day, tenant_cfg):
            counted += 1

    # Counting back can land on a closed day or run into today. Roll forward to
    # something real, but never past the target.
    day = max(day, today + timedelta(days=1))
    day = _roll_to_working_day(day, tenant_cfg)
    if day is None or day >= target:
        return None
    return VisitSlot(day, _label_for(day, today, is_shona=is_shona))
