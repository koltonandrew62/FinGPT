"""NYSE trading calendar, used to print exact buy/sell dates and times."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

ORDER_TIME = dt.time(9, 35)   # 5 minutes after the open, past the opening auction
ET = "America/New_York"


def _easter(year: int) -> dt.date:
    """Gregorian Easter Sunday (Anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year, month, weekday, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=1) if d.weekday() == 5 else d + dt.timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset:
    days = {
        _nth_weekday(year, 1, 0, 3),             # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),             # Presidents' Day
        _easter(year) - dt.timedelta(days=2),    # Good Friday
        _last_weekday(year, 5, 0),               # Memorial Day
        _observed(dt.date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),             # Labor Day
        _nth_weekday(year, 11, 3, 4),            # Thanksgiving
        _observed(dt.date(year, 12, 25)),        # Christmas
    }
    if year >= 2022:
        days.add(_observed(dt.date(year, 6, 19)))   # Juneteenth
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() != 5:                  # NYSE does not observe it on Fri Dec 31
        days.add(_observed(new_year))
    return frozenset(days)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def next_trading_day(after: dt.date) -> dt.date:
    d = after + dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def first_trading_day_of_week(on_or_after: dt.date) -> dt.date:
    """First session of the week that starts on the Monday on/after `on_or_after`."""
    monday = on_or_after + dt.timedelta(days=(7 - on_or_after.weekday()) % 7)
    d = monday
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def order_time(d: dt.date) -> str:
    """'Mon Nov 02, 2026 9:35 AM ET'."""
    return f"{d:%a %b %d, %Y} {ORDER_TIME.hour}:{ORDER_TIME.minute:02d} AM ET"
