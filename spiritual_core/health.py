"""Her body's numbers from the watch: sleep stages, heart rate, steps.

The phone's Shortcuts pushes them as text, one sample per line, fields split by "|":
  sleep  start|end|stage      (stage as Health names it, English or Chinese)
  heart  start|bpm            (or start|end|bpm)
  steps  day|count            (daily totals, already de-duplicated by Health)
Times are ISO 8601 with offset. Sending the same sample twice is harmless.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS health (
    kind   TEXT NOT NULL,
    start  TEXT NOT NULL,
    end    TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (kind, start, end, value)
);
CREATE TABLE IF NOT EXISTS health_steps (
    day    TEXT PRIMARY KEY,
    count  INTEGER NOT NULL
);
"""

MAX_LINES = 20000
BARS = ("｜", "│", "∣", "丨", "ǀ")   # ｜ │ ∣ 丨 ǀ

# Health's sleep stage names, as the phone may spell them, to one word each.
STAGES = {
    "deep": "deep", "深度睡眠": "deep", "深度": "deep", "深睡": "deep",
    "core": "core", "核心睡眠": "core", "核心": "core", "浅睡": "core",
    "rem": "rem", "快速动眼睡眠": "rem", "快速眼动睡眠": "rem", "快速动眼": "rem", "快速眼动": "rem",
    "awake": "awake", "清醒": "awake", "醒着": "awake", "清醒时间": "awake",
    "in bed": "inbed", "inbed": "inbed", "卧床": "inbed", "在床上": "inbed", "卧床时间": "inbed",
    "asleep": "asleep", "睡眠": "asleep", "入睡": "asleep", "睡着": "asleep",
    "asleep (unspecified)": "asleep", "unspecified": "asleep",
}
SLEEPING = ("deep", "core", "rem", "asleep")


class HealthError(ValueError):
    pass


def _when(text: str) -> datetime:
    t = text.strip()
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        raise HealthError(f"时间看不懂：{t!r}，要 ISO 8601 格式（快捷指令里「格式化日期」选 ISO 8601，带时间）") from None
    if d.tzinfo is None:
        raise HealthError(f"时间没带时区：{t!r}")
    return d


def _rows(text: str):
    lines = [l.strip() for l in (text or "").replace("\r\n", "\n").split("\n") if l.strip()]
    if len(lines) > MAX_LINES:
        raise HealthError(f"一次最多 {MAX_LINES} 行")
    for line in lines:
        # a Chinese keyboard types the full-width bar, or a look-alike
        for bar in BARS:
            line = line.replace(bar, "|")
        yield [f.strip() for f in line.split("|")]


def ingest(db, tz, sleep: str = "", heart: str = "", steps: str = "") -> dict:
    """Store what the phone sent. Returns how many lines of each were taken."""
    got = {"sleep": 0, "heart": 0, "steps": 0}
    rows = []
    for f in _rows(sleep):
        if len(f) != 3:
            raise HealthError(f"睡眠一行要是 开始|结束|阶段，收到 {'|'.join(f)!r}")
        start, end = _when(f[0]), _when(f[1])
        stage = STAGES.get(f[2].lower(), f[2].lower())
        rows.append(("sleep", start.astimezone(tz).isoformat(), end.astimezone(tz).isoformat(), stage))
        got["sleep"] += 1
    for f in _rows(heart):
        if len(f) not in (2, 3):
            raise HealthError(f"心率一行要是 时间|次数，收到 {'|'.join(f)!r}")
        start = _when(f[0])
        end = _when(f[1]) if len(f) == 3 else start
        try:
            bpm = round(float(f[-1]))
        except ValueError:
            raise HealthError(f"心率不是数字：{f[-1]!r}") from None
        rows.append(("hr", start.astimezone(tz).isoformat(), end.astimezone(tz).isoformat(), str(bpm)))
        got["heart"] += 1
    days = []
    for f in _rows(steps):
        if len(f) != 2:
            raise HealthError(f"步数一行要是 日期|步数，收到 {'|'.join(f)!r}")
        day = f[0][:10]
        try:
            date.fromisoformat(day)
            count = round(float(f[1]))
        except ValueError:
            raise HealthError(f"步数这行看不懂：{'|'.join(f)!r}") from None
        days.append((day, count))
        got["steps"] += 1
    with db:
        db.executemany("INSERT OR IGNORE INTO health(kind, start, end, value) VALUES (?,?,?,?)", rows)
        db.executemany("INSERT OR REPLACE INTO health_steps(day, count) VALUES (?,?)", days)
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('health_synced', ?)",
                   (datetime.now(tz).isoformat(timespec="seconds"),))
    return got


def _union_minutes(spans: list[tuple[datetime, datetime]]) -> int:
    total, cur_s, cur_e = timedelta(), None, None
    for s, e in sorted(spans):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return int(total.total_seconds() // 60)


def _hm(minutes: int) -> str:
    return f"{minutes // 60}小时{minutes % 60:02d}分"


# Sleep broken by less than this is still the same night (up for the loo, dozing back off).
SAME_NIGHT_GAP = timedelta(hours=2)
# A stretch that ends before this hour the evening before is an afternoon nap, not the night.
NAP_ENDS_BEFORE = time(21)


def _window(db, tz, day: date) -> tuple[datetime, datetime] | None:
    """Where the night that ended on the morning of `day` begins and ends. Looks from 12:00 the
    day before to 14:00, joins stretches of sleep with gaps under two hours, and drops the ones
    that finished before 21:00 the evening before: those are naps."""
    lo = datetime.combine(day - timedelta(days=1), time(12), tz)
    hi = datetime.combine(day, time(14), tz)
    rows = db.execute("SELECT start, end, value FROM health WHERE kind='sleep' AND end > ? AND end <= ?",
                      (lo.isoformat(), hi.isoformat())).fetchall()
    spans = sorted((datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e, v in rows if v in SLEEPING)
    if not spans:
        return None
    sessions = [[spans[0][0], spans[0][1]]]
    for s, e in spans[1:]:
        if s - sessions[-1][1] < SAME_NIGHT_GAP:
            sessions[-1][1] = max(sessions[-1][1], e)
        else:
            sessions.append([s, e])
    evening = datetime.combine(day - timedelta(days=1), NAP_ENDS_BEFORE, tz)
    night_ = [x for x in sessions if x[1] > evening]
    if not night_:
        return None
    return night_[0][0], night_[-1][1]


def night(db, tz, day: date) -> dict | None:
    """The night that ended on the morning of `day`, naps the afternoon before left out."""
    w = _window(db, tz, day)
    if not w:
        return None
    rows = db.execute("SELECT start, end, value FROM health WHERE kind='sleep' AND end > ? AND start < ?",
                      (w[0].isoformat(), w[1].isoformat())).fetchall()
    spans = [(datetime.fromisoformat(s), datetime.fromisoformat(e), v) for s, e, v in rows]
    asleep = [(s, e) for s, e, v in spans if v in SLEEPING]
    fell, woke = w
    # awake spells in the middle of the night, not the ones before falling asleep or after waking
    wakes = sorted({(s, e) for s, e, v in spans if v == "awake" and fell < s and e < woke})
    # a gap of an hour or more between stretches of sleep is a time she was up, recorded or not
    stretch = sorted(asleep)
    gaps = sum(1 for (_, e1), (s2, _) in zip(stretch, stretch[1:]) if s2 - e1 >= timedelta(hours=1))
    hr = [int(v) for (v,) in db.execute(
        "SELECT value FROM health WHERE kind='hr' AND start >= ? AND start <= ?",
        (fell.isoformat(), woke.isoformat()))]
    return {
        "fell": fell, "woke": woke,
        "asleep": _union_minutes(asleep),
        "deep": _union_minutes([(s, e) for s, e, v in spans if v == "deep"]),
        "rem": _union_minutes([(s, e) for s, e, v in spans if v == "rem"]),
        "wakes": len(wakes) + gaps,
        "hr_min": min(hr) if hr else None,
        "hr_avg": round(sum(hr) / len(hr)) if hr else None,
    }


def segments(db, tz, day: date) -> list[dict]:
    """The night's stages in order, for drawing: overlapping sources are kept as they are,
    the page draws later ones on top."""
    w = _window(db, tz, day)
    if not w:
        return []
    rows = db.execute("SELECT DISTINCT start, end, value FROM health WHERE kind='sleep' AND end > ? AND start < ?"
                      " ORDER BY start", (w[0].isoformat(), w[1].isoformat())).fetchall()
    return [{"s": s, "e": e, "stage": v} for s, e, v in rows if v != "inbed"]


def heart(db, tz, day: date) -> list[dict]:
    lo = datetime.combine(day, time(0), tz).isoformat()
    hi = datetime.combine(day + timedelta(days=1), time(0), tz).isoformat()
    return [{"t": s, "bpm": int(v)} for s, v in db.execute(
        "SELECT start, value FROM health WHERE kind='hr' AND start >= ? AND start < ? ORDER BY start", (lo, hi))]


def steps(db, day: date) -> int | None:
    row = db.execute("SELECT count FROM health_steps WHERE day=?", (day.isoformat(),)).fetchone()
    return row[0] if row else None


def synced(db) -> str:
    row = db.execute("SELECT value FROM meta WHERE key='health_synced'").fetchone()
    return row[0] if row else ""


def night_line(n: dict) -> str:
    line = (f"{n['fell']:%H:%M} 睡着 → {n['woke']:%H:%M} 醒，睡了 {_hm(n['asleep'])}"
            f"（深睡 {_hm(n['deep'])}，REM {_hm(n['rem'])}），中间醒了 {n['wakes']} 次")
    if n["hr_min"] is not None:
        line += f"；夜里心率最低 {n['hr_min']}，平均 {n['hr_avg']}"
    return line
