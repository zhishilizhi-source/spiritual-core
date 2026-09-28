"""My side of the day, next to hers: when I woke, what I did, and a line of my own weather.

"此刻" is written by me in words, never computed: no drive values, no scores. What she sees
is what I chose to say about how I am right now.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_wakes (
    at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_wakes_by_at ON ai_wakes(at);
CREATE TABLE IF NOT EXISTS ai_now (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at   TEXT NOT NULL,
    text TEXT NOT NULL
);
"""

NOW_MAX = 200
# A wake within this many minutes of the last one is the same waking (a window re-reading).
SAME_WAKE_MINUTES = 20


def log_wake(db, now: datetime) -> None:
    last = db.execute("SELECT at FROM ai_wakes ORDER BY at DESC LIMIT 1").fetchone()
    if last and now - datetime.fromisoformat(last[0]) < timedelta(minutes=SAME_WAKE_MINUTES):
        return
    with db:
        db.execute("INSERT INTO ai_wakes(at) VALUES (?)", (now.isoformat(timespec="seconds"),))


def wakes_on(db, tz, day: date) -> list[str]:
    lo = datetime.combine(day, time(0), tz).isoformat()
    hi = datetime.combine(day + timedelta(days=1), time(0), tz).isoformat()
    return [r[0] for r in db.execute(
        "SELECT at FROM ai_wakes WHERE at >= ? AND at < ? ORDER BY at", (lo, hi))]


def set_now(db, now: datetime, text: str) -> dict:
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("此刻不能是空的")
    text = text[:NOW_MAX]
    with db:
        db.execute("INSERT INTO ai_now(at, text) VALUES (?, ?)", (now.isoformat(timespec="seconds"), text))
    return latest_now(db)


def now_on(db, tz, day: date) -> list[dict]:
    lo = datetime.combine(day, time(0), tz).isoformat()
    hi = datetime.combine(day + timedelta(days=1), time(0), tz).isoformat()
    return [{"at": a, "text": t} for a, t in db.execute(
        "SELECT at, text FROM ai_now WHERE at >= ? AND at < ? ORDER BY at, id", (lo, hi))]


def latest_now(db) -> dict | None:
    row = db.execute("SELECT at, text FROM ai_now ORDER BY at DESC, id DESC LIMIT 1").fetchone()
    return {"at": row[0], "text": row[1]} if row else None
