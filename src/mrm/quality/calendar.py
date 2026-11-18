"""US market and federal holiday calendars, rule-based, no dependencies.

Rules cover the modern era (Uniform Monday Holiday Act, 1971+). One-off closures (national
days of mourning, hurricanes) are not rules: they come from ``config/data_quality.yaml``
(``calendar.special_closures``) and are passed in by the caller.
"""

from collections.abc import Collection, Iterable
from datetime import date, timedelta
from functools import cache

MON, THU, SAT, SUN = 0, 3, 5, 6


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th ``weekday`` of the month (n >= 1), or the last one when n == -1."""
    if n == -1:
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        last = nxt - timedelta(days=1)
        return last - timedelta(days=(last.weekday() - weekday) % 7)
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date:
    """Federal/NYSE observance: Saturday -> Friday, Sunday -> Monday."""
    if day.weekday() == SAT:
        return day - timedelta(days=1)
    if day.weekday() == SUN:
        return day + timedelta(days=1)
    return day


@cache
def federal_holidays(year: int) -> frozenset[date]:
    """US federal holidays (observed dates) of ``year``, 5 U.S.C. 6103."""
    days = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 2, MON, 3),  # Washington's Birthday
        _nth_weekday(year, 5, MON, -1),  # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, MON, 1),  # Labor Day
        _nth_weekday(year, 10, MON, 2),  # Columbus Day
        _nth_weekday(year, 11, THU, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 1986:
        days.add(_nth_weekday(year, 1, MON, 3))  # Martin Luther King Jr. Day
    if year >= 2021:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    # Veterans Day moved to the 4th Monday of October in 1971-1977.
    days.add(
        _nth_weekday(year, 10, MON, 4) if 1971 <= year <= 1977 else _observed(date(year, 11, 11))
    )
    # Next New Year's Day falling on Saturday is observed on Dec 31 of this year.
    days.add(_observed(date(year + 1, 1, 1)))
    return frozenset(d for d in days if d.year == year)


@cache
def nyse_holidays(year: int) -> frozenset[date]:
    """NYSE full-day holidays of ``year`` by rule (NYSE Rule 7.2), excluding one-off closures.

    Differences from the federal calendar: Good Friday is a holiday; Columbus and Veterans
    Day are not; Juneteenth only from 2022; MLK Day only from 1998; a New Year's Day falling on
    Saturday is not observed on the preceding Friday.
    """
    new_year = date(year, 1, 1)
    days = {
        _observed(new_year) if new_year.weekday() != SAT else None,
        _nth_weekday(year, 2, MON, 3),
        easter(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, MON, -1),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, MON, 1),
        _nth_weekday(year, 11, THU, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 1998:
        days.add(_nth_weekday(year, 1, MON, 3))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    return frozenset(d for d in days if d is not None and d.year == year)


def holidays_between(start: date, end: date, calendar: str) -> frozenset[date]:
    """Rule-based holidays of ``calendar`` (``"nyse"`` or ``"federal"``) in [start, end]."""
    rule = {"nyse": nyse_holidays, "federal": federal_holidays}[calendar]
    found: set[date] = set()
    for year in range(start.year, end.year + 1):
        found.update(d for d in rule(year) if start <= d <= end)
    return frozenset(found)


def is_nyse_business_day(day: date, special_closures: Collection[date] = ()) -> bool:
    """True when the NYSE is open on ``day`` (weekday, not a holiday, not a special closure)."""
    return (
        day.weekday() < SAT and day not in nyse_holidays(day.year) and day not in special_closures
    )


def nyse_business_days(
    start: date, end: date, special_closures: Collection[date] = ()
) -> list[date]:
    """NYSE trading days in [start, end], ascending."""
    days: list[date] = []
    day = start
    while day <= end:
        if is_nyse_business_day(day, special_closures):
            days.append(day)
        day += timedelta(days=1)
    return days


def last_nyse_business_day(today: date, special_closures: Collection[date] = ()) -> date:
    """``today`` if the NYSE is open, else the closest earlier trading day."""
    day = today
    while not is_nyse_business_day(day, special_closures):
        day -= timedelta(days=1)
    return day


def market_closed_days(
    days: Iterable[date], special_closures: Collection[date] = ()
) -> dict[date, str]:
    """Explain why each date is a non-trading/non-settlement day; others are left out.

    Reasons: ``weekend``, ``federal holiday``, ``NYSE holiday`` or ``special closure``.
    Used to classify FRED's missing values ('.') of daily series.
    """
    reasons: dict[date, str] = {}
    for day in days:
        if day.weekday() >= SAT:
            reasons[day] = "weekend"
        elif day in special_closures:
            reasons[day] = "special closure"
        elif day in federal_holidays(day.year):
            reasons[day] = "federal holiday"
        elif day in nyse_holidays(day.year):
            reasons[day] = "NYSE holiday"
    return reasons
