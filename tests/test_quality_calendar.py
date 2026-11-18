from datetime import date

import pytest

from mrm.quality.calendar import (
    easter,
    federal_holidays,
    is_nyse_business_day,
    last_nyse_business_day,
    market_closed_days,
    nyse_business_days,
    nyse_holidays,
)

# Published NYSE holiday schedules.
NYSE_2021 = {
    date(2021, 1, 1), date(2021, 1, 18), date(2021, 2, 15), date(2021, 4, 2), date(2021, 5, 31),
    date(2021, 7, 5), date(2021, 9, 6), date(2021, 11, 25), date(2021, 12, 24),
}  # fmt: skip
NYSE_2025 = {
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18), date(2025, 5, 26),
    date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
}  # fmt: skip
NYSE_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
}  # fmt: skip
# OPM federal holiday schedule 2021 (Juneteenth observed Fri Jun 18; New Year 2022 on Dec 31).
FEDERAL_2021 = {
    date(2021, 1, 1), date(2021, 1, 18), date(2021, 2, 15), date(2021, 5, 31), date(2021, 6, 18),
    date(2021, 7, 5), date(2021, 9, 6), date(2021, 10, 11), date(2021, 11, 11),
    date(2021, 11, 25), date(2021, 12, 24), date(2021, 12, 31),
}  # fmt: skip


@pytest.mark.parametrize(
    ("year", "expected"), [(2021, NYSE_2021), (2025, NYSE_2025), (2026, NYSE_2026)]
)
def test_nyse_holidays_match_published_schedule(year: int, expected: set[date]) -> None:
    assert nyse_holidays(year) == expected


def test_federal_holidays_2021() -> None:
    assert federal_holidays(2021) == FEDERAL_2021


@pytest.mark.parametrize(
    "easter_sunday",
    [date(2008, 3, 23), date(2019, 4, 21), date(2024, 3, 31), date(2038, 4, 25)],
)
def test_easter(easter_sunday: date) -> None:
    assert easter(easter_sunday.year) == easter_sunday


def test_juneteenth_nyse_only_from_2022() -> None:
    assert date(2021, 6, 18) not in nyse_holidays(2021)
    assert date(2022, 6, 20) in nyse_holidays(2022)  # Sunday -> Monday
    assert date(2021, 6, 18) in federal_holidays(2021)
    assert not any(d.month == 6 and d.day >= 18 for d in federal_holidays(2020))


def test_new_year_on_saturday_not_observed_by_nyse() -> None:
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert date(2021, 12, 31) in federal_holidays(2021)


def test_veterans_day_1971_1977_rule() -> None:
    assert date(1975, 10, 27) in federal_holidays(1975)
    assert date(1975, 11, 11) not in federal_holidays(1975)
    assert date(1978, 11, 10) in federal_holidays(1978)  # Saturday -> Friday


def test_special_closures_and_business_days() -> None:
    closures = {date(2025, 1, 9)}
    assert is_nyse_business_day(date(2025, 1, 9))
    assert not is_nyse_business_day(date(2025, 1, 9), closures)
    week = nyse_business_days(date(2025, 1, 6), date(2025, 1, 12), closures)
    assert week == [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 10)]


def test_last_business_day() -> None:
    assert last_nyse_business_day(date(2026, 11, 18)) == date(2026, 11, 18)
    assert last_nyse_business_day(date(2026, 11, 22)) == date(2026, 11, 20)  # Sunday
    assert last_nyse_business_day(date(2026, 11, 26)) == date(2026, 11, 25)  # Thanksgiving


def test_market_closed_days_reasons() -> None:
    reasons = market_closed_days(
        [
            date(2024, 3, 29),  # Good Friday
            date(2024, 11, 11),  # Veterans Day: NYSE open, bond market closed
            date(2025, 1, 9),  # Carter
            date(2024, 11, 9),  # Saturday
            date(2024, 11, 12),  # ordinary Tuesday
        ],
        special_closures={date(2025, 1, 9)},
    )
    assert reasons == {
        date(2024, 3, 29): "NYSE holiday",
        date(2024, 11, 11): "federal holiday",
        date(2025, 1, 9): "special closure",
        date(2024, 11, 9): "weekend",
    }
