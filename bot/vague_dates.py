"""
bot/vague_dates.py
==================
A vague timeframe is an ANSWER. Assume a date inside it; never ask again.

WHAT: ``resolve(message)`` turns "month end", "mid next week", "early next
month", "in a few weeks", "after payday" into a ``Frame``: the range the lead
named (``start``..``end``) and the one date we assume inside it (``anchor``).

WHY (owner rule, 2026-09-21, repeated because it kept surfacing): a lead who
said "month end" or "mid next week" was asked "do you mean towards the end of
the week or the beginning?" or "roughly when?". That is a second question
about a range they had just given us, and it is what made conversations stall.
Two paths did it: the delay flow re-asked when neither DeepSeek nor the
keyword parser produced a date, and the booking flow's retry paraphrase let
the model ask which part of the range. Both now read this first.

HOW: deterministic, a closed vocabulary (the CLAUDE.md rule for short fuzzy
strings). It stands aside (returns None) whenever the message names something
more precise, a weekday, "tomorrow", an ordinal day or a digit count ("in 2
weeks"), so the existing exact parsers keep those. Where we land inside a
range:
  week:  early -> Monday, mid -> Wednesday, later/end -> Thursday/Friday,
         a bare "this/next week" -> Wednesday
  month: early -> the 3rd, mid -> the 15th, end -> the 28th (or the last day),
         a bare "next month" -> the 15th
  year:  end of the year -> 1 December, early next year -> 10 January
  spans: a few days -> +3, a couple of weeks -> +14, a few weeks -> +21,
         a couple of months -> +60, a few months -> +90, soon -> +7
A range already behind us this week/month rolls to the next one, and an
anchor that has passed is pulled up to tomorrow while still inside the range.

Pinned by the "vague date" cases in TEST 0.
"""

import calendar
import re
from datetime import date, timedelta

import pytz

_TZ = pytz.timezone('Africa/Johannesburg')


class Frame:
    """The range the lead named, and the date we assume inside it."""

    __slots__ = ('start', 'end', 'anchor', 'phrase')

    def __init__(self, start, end, anchor, phrase):
        self.start, self.end, self.anchor, self.phrase = start, end, anchor, phrase

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'Frame({self.start}..{self.end}, anchor={self.anchor}, {self.phrase!r})'


# ── Week parts ───────────────────────────────────────────────────────────────
# ONE table, read by this module and by the booking flow's `week_part_of`
# (availability_mixin), so "midweek" cannot mean two things in two places.
# Each entry: pattern, the weekdays it covers (0=Monday), the phrase said back.
WEEK_PARTS = (
    (re.compile(r"\bmid[\s-]?(?:(?:this|next|the)\s+)?week\b"
                r"|\bmiddle\s+of\s+(?:the\s+|this\s+|next\s+)?week\b"
                r"|\bpakati\s+pe?(?:ne)?vhiki\b", re.IGNORECASE),
     (1, 2, 3), 'midweek'),
    (re.compile(r"\b(?:early|beginning|start)\s+(?:in\s+|of\s+)?"
                r"(?:the\s+|this\s+|next\s+)?week\b", re.IGNORECASE),
     (0, 1), 'early in the week'),
    (re.compile(r"\b(?:end|later|late)\s+(?:in\s+|of\s+)?"
                r"(?:the\s+|this\s+|next\s+)?week\b", re.IGNORECASE),
     (3, 4), 'later in the week'),
)

_NEXT_WEEK_LABELS = {'midweek': 'mid next week', 'early in the week': 'early next week',
                     'later in the week': 'the end of next week', 'this week': 'next week'}

# Something more precise than a range: the exact parsers own these.
_PRECISE_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|tmrw|mangwana"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day"
    r"|\d{1,2}(?:st|nd|rd|th)"
    r"|\d+\s*(?:days?|weeks?|months?|years?))\b",
    re.IGNORECASE,
)

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS['sept'] = 9
_MONTH_RE = re.compile(r"\b(" + '|'.join(sorted(_MONTHS, key=len, reverse=True)) + r")\b",
                       re.IGNORECASE)

# Month parts. "payday" is month end: that is when salaries land here, and it
# is what a lead waiting on pay means.
_MONTH_EARLY = re.compile(r"\b(?:early|beginning|start)\s+(?:of\s+)?(?:the\s+|this\s+|next\s+)?"
                          r"(?:month|" + '|'.join(_MONTHS) + r")\b", re.IGNORECASE)
_MONTH_MID = re.compile(r"\b(?:mid|middle\s+of)[\s-]*(?:the\s+|this\s+|next\s+)?"
                        r"(?:month|" + '|'.join(_MONTHS) + r")\b", re.IGNORECASE)
_MONTH_END = re.compile(
    r"\b(?:end|late|later|towards\s+the\s+end)\s+(?:of\s+)?(?:the\s+|this\s+|next\s+)?"
    r"(?:month|" + '|'.join(_MONTHS) + r")\b"
    r"|\bmonth(?:'s)?[\s-]*end\b|\bpay\s*day\b|\bwhen\s+i\s+get\s+paid\b|\bsalary\b"
    r"|\bkupera\s+kwe\s*mwedzi\b|\bkumagumo\s+kwe\s*mwedzi\b",
    re.IGNORECASE)
_BARE_MONTH_NAME = re.compile(
    r"\b(" + '|'.join(m.lower() for m in calendar.month_name if m and m != 'May')
    + r")\b|\bin\s+(may)\b", re.IGNORECASE)
_BARE_MONTH = re.compile(r"\b(?:this|next)\s+month\b|mwedzi\s+(?:unotevera|unouya)\b", re.IGNORECASE)

_YEAR_END = re.compile(r"\b(?:end\s+of\s+(?:the\s+|this\s+)?year|year[\s-]*end"
                       r"|later\s+this\s+year)\b", re.IGNORECASE)
_YEAR_START = re.compile(r"\b(?:early|beginning|start)\s+(?:of\s+)?next\s+year\b"
                         r"|\bnew\s+year\b|\bnext\s+year\b", re.IGNORECASE)

_WORD_NUM = {'a': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}
_SPANS = (
    (re.compile(r"\b(?:next|in\s+a|within\s+a)\s+few\s+days\b|\ba\s+day\s+or\s+two\b"
                r"|\bcouple\s+(?:of\s+)?days\b", re.IGNORECASE), 3, 'in the next few days'),
    (re.compile(r"\bcouple\s+(?:of\s+)?weeks\b", re.IGNORECASE), 14, 'in a couple of weeks'),
    (re.compile(r"\bfew\s+weeks\b", re.IGNORECASE), 21, 'in a few weeks'),
    (re.compile(r"\bcouple\s+(?:of\s+)?months\b", re.IGNORECASE), 60, 'in a couple of months'),
    (re.compile(r"\bfew\s+months\b", re.IGNORECASE), 90, 'in a few months'),
    # "as soon as possible" is urgency, not a delay, so it is not "soon".
    (re.compile(r"\b(?<!as )(?:soon|shortly|not\s+long)\b(?!\s+as\b)", re.IGNORECASE),
     7, 'soon'),
)
_WORD_COUNT = re.compile(r"\b(?:in|after)\s+(a|one|two|three|four|five|six)\s+(day|week|month)s?\b",
                         re.IGNORECASE)


def _today(today=None):
    if today is not None:
        return today
    from datetime import datetime
    return datetime.now(_TZ).date()


def _clamp(start, end, anchor, today):
    """Keep the anchor inside the range and after today, or None when the
    whole range is already behind us."""
    tomorrow = today + timedelta(days=1)
    if end < tomorrow:
        return None
    start = max(start, tomorrow)
    return start, end, min(max(anchor, start), end)


def _week_frame(msg, today):
    next_week = bool(re.search(r"\bnext\s+week\b|\bsvondo\s+rinouya\b", msg, re.I))
    part = None
    for pattern, days, phrase in WEEK_PARTS:
        if pattern.search(msg):
            part = (days, phrase)
            break
    if part is None:
        if not re.search(r"\b(?:this|next)\s+week\b|\bsvondo\s+rinouya\b", msg, re.I):
            return None
        # A bare week runs to Sunday, so "this week" said on a Friday is still
        # this week (Saturday), not silently next week.
        part = ((0, 1, 2, 3, 4, 5, 6), 'next week' if next_week else 'this week')
    days, phrase = part
    # Wednesday for a whole week or midweek, Monday for early, Friday for the
    # end of the week.
    if len(days) >= 5:
        anchor_wd = 2
    elif days[0] == 0:
        anchor_wd = 0
    elif days[0] >= 3:
        anchor_wd = days[-1]
    else:
        anchor_wd = days[len(days) // 2]
    monday = today - timedelta(days=today.weekday())
    for week_offset in ((1,) if next_week else (0, 1)):
        base = monday + timedelta(weeks=week_offset)
        clamped = _clamp(base + timedelta(days=days[0]), base + timedelta(days=days[-1]),
                         base + timedelta(days=anchor_wd), today)
        if clamped:
            # Said back the way a person would: "mid next week", not
            # "midweek next week". A range that rolled over because this
            # week's part has passed is next week's too.
            if week_offset == 1 and phrase in _NEXT_WEEK_LABELS:
                phrase = _NEXT_WEEK_LABELS[phrase]
            return Frame(*clamped, phrase)
    return None


def _month_frame(msg, today):
    if _MONTH_END.search(msg):
        part, anchor_day, phrase = 'end', 28, 'month end'
    elif _MONTH_MID.search(msg):
        part, anchor_day, phrase = 'mid', 15, 'the middle of the month'
    elif _MONTH_EARLY.search(msg):
        part, anchor_day, phrase = 'early', 3, 'early in the month'
    elif _BARE_MONTH.search(msg) or _BARE_MONTH_NAME.search(msg):
        part, anchor_day, phrase = 'whole', 15, 'this month'
    else:
        return None

    # A month named WITH a part ("end of Oct") may be abbreviated; a bare one
    # must be spelled out, and "may" only counts as "in May", so "I may call
    # you" is not a date.
    named = _MONTH_RE.search(msg) if part != 'whole' else _BARE_MONTH_NAME.search(msg)
    if named:
        month = _MONTHS[(named.group(1) or named.group(2)).lower()]
        year = today.year if month >= today.month else today.year + 1
        candidates = [(year, month)]
    # Shona "next month": "mwedzi unotevera" (the following month) or "mwedzi
    # unouya" (the coming month), often after "kupera kwe" (the end of).
    # "Kupera kwemwedzi unouya" read as THIS month's end until 2026-09-21,
    # because only "unotevera" was listed.
    # No \b before "mwedzi": Shona glues the possessive on ("kwemwedzi").
    elif re.search(r"\bnext\s+month\b|mwedzi\s+(?:unotevera|unouya)\b", msg, re.I):
        nxt = today.replace(day=1) + timedelta(days=32)
        candidates = [(nxt.year, nxt.month)]
    else:
        nxt = today.replace(day=1) + timedelta(days=32)
        candidates = [(today.year, today.month), (nxt.year, nxt.month)]

    for year, month in candidates:
        last = calendar.monthrange(year, month)[1]
        ranges = {'early': (1, 7), 'mid': (10, 20), 'end': (last - 6, last), 'whole': (1, last)}
        lo, hi = ranges[part]
        clamped = _clamp(date(year, month, lo), date(year, month, hi),
                         date(year, month, min(anchor_day, last)), today)
        if clamped:
            label = phrase
            if named:
                name = calendar.month_name[month]
                label = {'end': f'the end of {name}', 'mid': f'mid {name}',
                         'early': f'early {name}', 'whole': name}[part]
            elif (year, month) != (today.year, today.month):
                label = {'end': 'the end of next month', 'mid': 'mid next month',
                         'early': 'early next month', 'whole': 'next month'}[part]
            return Frame(*clamped, label)
    return None


def _year_frame(msg, today):
    if _YEAR_END.search(msg):
        clamped = _clamp(date(today.year, 12, 1), date(today.year, 12, 31),
                         date(today.year, 12, 1), today)
        return Frame(*clamped, 'the end of the year') if clamped else None
    if _YEAR_START.search(msg):
        y = today.year + 1
        return Frame(date(y, 1, 5), date(y, 1, 31), date(y, 1, 10), 'early next year')
    return None


def _span_frame(msg, today):
    m = _WORD_COUNT.search(msg)
    if m:
        n = _WORD_NUM[m.group(1).lower()]
        unit = m.group(2).lower()
        days = n * {'day': 1, 'week': 7, 'month': 30}[unit]
        anchor = today + timedelta(days=days)
        return Frame(anchor, anchor, anchor, m.group(0).lower())
    for pattern, days, phrase in _SPANS:
        if pattern.search(msg):
            # The range sits AROUND the assumed date, so the booking flow's
            # offer for "in a few weeks" is in a few weeks, not tomorrow.
            anchor = today + timedelta(days=days)
            spread = timedelta(days=days // 3)
            return Frame(max(anchor - spread, today + timedelta(days=1)),
                         anchor + spread, anchor, phrase)
    return None


def resolve(message, today=None):
    """The Frame for a vague timeframe in `message`, or None.

    None when the message carries no timeframe at all, and when it carries a
    precise one (a weekday, "tomorrow", "the 26th", "in 2 weeks"): those
    belong to the exact parsers, and a range must never overrule a day the
    lead actually named. Order: week, month, year, then loose spans, so "mid
    next week" is a week part and not "next week" plus nothing.
    """
    msg = ' '.join(str(message or '').split())
    if not msg or _PRECISE_RE.search(msg):
        return None
    today = _today(today)
    for reader in (_week_frame, _month_frame, _year_frame, _span_frame):
        frame = reader(msg, today)
        if frame:
            return frame
    return None
