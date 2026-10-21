"""Month helpers for batch generation. A month is a view over weeks, never a unit of planning.

There are no monthly or cross-week rules: a month fill is a batch of independent
weekly plans, each solved and rule-checked on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
SEED_MODULUS = 2**31


def week_seed(week_start: date) -> int:
    """Deterministic solver seed for a week: int(YYYYMMDD of its Monday) mod 2**31.

    Pure function of the week, so refilling a fresh database yields the same plans
    and a week's plan does not depend on which month fill created it.
    """
    return int(week_start.strftime("%Y%m%d")) % SEED_MODULUS


def parse_month(month: str) -> tuple[int, int]:
    """Return (year, month) for 'YYYY-MM', or raise ValueError."""
    match = MONTH_PATTERN.fullmatch(month) if isinstance(month, str) else None
    if match is None or int(match.group(1)) < 1:
        raise ValueError("month must be YYYY-MM")
    return int(match.group(1)), int(match.group(2))


def weeks_of_month(month: str) -> list[date]:
    """Mondays of every Monday-Sunday week overlapping the month, in order.

    Includes the weeks crossing either month boundary (a month spans 4 to 6 weeks).
    """
    year, number = parse_month(month)
    first = date(year, number, 1)
    last = (date(year + 1, 1, 1) if number == 12 else date(year, number + 1, 1)) - timedelta(days=1)
    monday = first - timedelta(days=first.weekday())
    weeks: list[date] = []
    while monday <= last:
        weeks.append(monday)
        monday += timedelta(days=7)
    return weeks


@dataclass(frozen=True)
class WeekFillOutcome:
    week_start: date
    week_end: date
    status: str  # created | exists | skipped_past | failed
    plan_id: str | None = None
    revision: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class MonthFillResult:
    month: str
    weeks: tuple[WeekFillOutcome, ...]
