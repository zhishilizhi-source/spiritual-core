"""How much a memory weighs right now.

Weight only decides ordering: what wake surfaces and what search puts first.
It never hides, compresses or deletes anything.

  importance  = her rating  >  hindsight review  >  score given at write time  >  kind default
  recency     = halves every HALF_LIFE days, never below FLOOR; some kinds never fade
  use         = read often, read lately, linked to other records
"""

from __future__ import annotations

import math
from datetime import date

KIND_DEFAULT = {
    "anchor": 5, "excerpt": 4, "letter": 4, "intimate": 4,
    "diary": 3, "memory": 3, "handoff": 3, "image": 3, "plan": 3,
    "raw": 2, "daily": 2, "work": 2, "note": 1,
}

# Days for recency to halve. None = never fades: what we agreed to keep does not sink with time.
HALF_LIFE = {
    "anchor": None, "excerpt": None, "letter": None, "intimate": None,
    "diary": 180, "memory": 180, "image": 180, "raw": 90, "daily": 60, "work": 45,
    "plan": 30, "note": 21, "handoff": 14,
}

FLOOR = 0.15
FIVES_PER_WEEK = 2   # at most this many 5s per ISO week, so "important" stays scarce
HOT_DAYS = 7
WARM_SCORE = 1.5
WARM_READ_DAYS = 30


def effective(r) -> int:
    return r.human_rating or r.reviewed or r.importance or KIND_DEFAULT.get(r.kind, 2)


def _days_between(earlier: str, today: date) -> int:
    try:
        d = date.fromisoformat(earlier[:10])
    except ValueError:
        return 0
    return max(0, (today - d).days)


def recency(r, today: date) -> float:
    half = HALF_LIFE.get(r.kind, 60)
    if half is None:
        return 1.0
    return max(FLOOR, 0.5 ** (_days_between(r.event_date, today) / half))


def use(r, today: date, links: int = 0) -> float:
    # Gentle on purpose: one look-up read is ~+35%, it takes repeated returns to really climb.
    boost = 1.0 + 0.25 * math.log1p(r.read_count) + 0.1 * min(links, 5)
    if r.last_read_at:
        boost += 0.2 * 0.5 ** (_days_between(r.last_read_at, today) / WARM_READ_DAYS)
    return boost


def score(r, today: date, links: int = 0) -> float:
    s = effective(r) * recency(r, today) * use(r, today, links)
    if r.pinned:
        s += 10
    return round(s, 3)


def layer(r, today: date, s: float) -> str:
    """热 / 温 / 冷 — derived on the fly, never stored."""
    if r.pinned or r.status == "open" or _days_between(r.event_date, today) < HOT_DAYS:
        return "热"
    recently_read = r.last_read_at and _days_between(r.last_read_at, today) <= WARM_READ_DAYS
    if s >= WARM_SCORE or recently_read:
        return "温"
    return "冷"


def iso_week(day: str) -> tuple[str, str]:
    d = date.fromisoformat(day)
    monday = date.fromordinal(d.toordinal() - d.weekday())
    sunday = date.fromordinal(monday.toordinal() + 6)
    return monday.isoformat(), sunday.isoformat()


def month(day: str) -> tuple[str, str]:
    d = date.fromisoformat(day)
    first = d.replace(day=1)
    nxt = (first.replace(year=first.year + 1, month=1) if first.month == 12
           else first.replace(month=first.month + 1))
    return first.isoformat(), date.fromordinal(nxt.toordinal() - 1).isoformat()
