"""Spiritual Core storage: one SQLite file per owner.

Every record carries two times:
  event_date  -- the day it happened (YYYY-MM-DD, owner's local calendar)
  created_at  -- the moment it was written (ISO 8601 with offset)
They are never derived from each other after write.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import media, weight

KINDS = (
    "diary", "intimate", "handoff", "anchor", "raw", "excerpt", "image",
    "letter", "memory", "note", "plan", "work", "daily",
)

# Who wanted an excerpt kept. Deployment-neutral: display names come from the client.
KEEPERS = ("ai", "human")
# Kinds that must say whose they are: excerpt = who wanted it kept, letter = who wrote it.
NEEDS_KEEPER = ("excerpt", "letter")

PLAN_STATUSES = ("open", "done")

# Trigram FTS needs at least 3 characters per term; shorter terms go through LIKE.
_TRIGRAM_MIN = 3

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    pinned      INTEGER NOT NULL DEFAULT 0,
    tags        TEXT NOT NULL DEFAULT '[]',
    kept_by     TEXT NOT NULL DEFAULT '',
    why         TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    importance  INTEGER NOT NULL DEFAULT 0,
    reviewed    INTEGER NOT NULL DEFAULT 0,
    reviewed_at TEXT NOT NULL DEFAULT '',
    human_rating INTEGER NOT NULL DEFAULT 0,
    read_count  INTEGER NOT NULL DEFAULT 0,
    last_read_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS records_by_keeper ON records(kind, kept_by, event_date);
CREATE INDEX IF NOT EXISTS records_by_date ON records(event_date, kind);
CREATE INDEX IF NOT EXISTS records_by_kind ON records(kind, created_at);

CREATE TABLE IF NOT EXISTS links (
    a TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    b TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    PRIMARY KEY (a, b)
);

CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    title, body, tags, why,
    content='records', content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, title, body, tags, why)
    VALUES (new.rowid, new.title, new.body, new.tags, new.why);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, title, body, tags, why)
    VALUES ('delete', old.rowid, old.title, old.body, old.tags, old.why);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, title, body, tags, why)
    VALUES ('delete', old.rowid, old.title, old.body, old.tags, old.why);
    INSERT INTO records_fts(rowid, title, body, tags, why)
    VALUES (new.rowid, new.title, new.body, new.tags, new.why);
END;

CREATE TABLE IF NOT EXISTS images (
    record_id  TEXT PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
    file       TEXT NOT NULL,
    mime       TEXT NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    bytes      INTEGER NOT NULL,
    sha256     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SCHEMA_VERSION = "3"

# Columns added after v1, in the order they arrived. _migrate adds whichever are missing.
_ADDED_COLUMNS = (
    ("status", "TEXT NOT NULL DEFAULT ''"),          # v2
    ("importance", "INTEGER NOT NULL DEFAULT 0"),    # v3
    ("reviewed", "INTEGER NOT NULL DEFAULT 0"),
    ("reviewed_at", "TEXT NOT NULL DEFAULT ''"),
    ("human_rating", "INTEGER NOT NULL DEFAULT 0"),
    ("read_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_read_at", "TEXT NOT NULL DEFAULT ''"),
)


@dataclass
class Record:
    id: str
    kind: str
    title: str
    body: str
    event_date: str
    created_at: str
    updated_at: str
    pinned: bool
    tags: list[str]
    kept_by: str = ""
    why: str = ""
    status: str = ""
    importance: int = 0      # given at write time (provisional)
    reviewed: int = 0        # given in hindsight, overrides importance
    reviewed_at: str = ""
    human_rating: int = 0    # hers, overrides everything
    read_count: int = 0
    last_read_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Record":
        return cls(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            event_date=row["event_date"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            pinned=bool(row["pinned"]),
            tags=json.loads(row["tags"]),
            kept_by=row["kept_by"],
            why=row["why"],
            status=row["status"],
            importance=row["importance"],
            reviewed=row["reviewed"],
            reviewed_at=row["reviewed_at"],
            human_rating=row["human_rating"],
            read_count=row["read_count"],
            last_read_at=row["last_read_at"],
        )


class StoreError(ValueError):
    """Bad input from a caller; the message is safe to show to the model."""


class Store:
    def __init__(self, path: str | Path, tz: str = "Asia/Shanghai"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.images_dir = self.path.parent / "images"
        self.images_dir.mkdir(exist_ok=True)
        self.tz = ZoneInfo(tz)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        self.db.commit()

    def _migrate(self) -> None:
        """Bring databases created by older versions up to the current columns."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(records)")}
        for name, ddl in _ADDED_COLUMNS:
            if name not in have:
                self.db.execute(f"ALTER TABLE records ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        self.db.close()

    # --- time ---

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    @staticmethod
    def _check_date(value: str, field: str = "event_date") -> str:
        if not _DATE_RE.match(value or ""):
            raise StoreError(f"{field} 要写成 YYYY-MM-DD，收到的是 {value!r}")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise StoreError(f"{field} 不是真实存在的日期：{value!r}") from None
        return value

    # --- write ---

    def write(
        self,
        kind: str,
        body: str,
        event_date: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        pinned: bool = False,
        link_to: list[str] | None = None,
        created_at: str | None = None,
        kept_by: str = "",
        why: str = "",
        importance: int = 0,
    ) -> Record:
        """Always creates a new record. Nothing is ever merged into an existing one.

        Excerpts are stored verbatim: the body is not stripped or reflowed.
        """
        if kind not in KINDS:
            raise StoreError(f"kind 只能是 {', '.join(KINDS)} 之一，收到 {kind!r}")
        kept_by = (kept_by or "").strip()
        # Browsers submit textarea newlines as CRLF; line endings are not content.
        body = (body or "").replace("\r\n", "\n")
        why = (why or "").replace("\r\n", "\n").strip()
        if kind in NEEDS_KEEPER:
            if kept_by not in KEEPERS:
                what = "谁想留的" if kind == "excerpt" else "谁写的"
                raise StoreError(f"{kind} 要写明是{what}：填 ai 或 human")
        elif kept_by:
            raise StoreError("kept_by 只用在 excerpt 和 letter 上")
        status = "open" if kind == "plan" else ""
        if kind != "excerpt":
            body = (body or "").strip()
        if not (body or "").strip():
            raise StoreError("body 不能为空")
        event_date = self._check_date(event_date or self.today().isoformat())
        importance = _check_score(importance, allow_zero=True)
        if importance == 5:
            self._check_fives(event_date)
        title = (title or "").strip() or _default_title(kind, event_date, body)
        tags = [t.strip() for t in (tags or []) if t and t.strip()]
        stamp = created_at or self.now().isoformat(timespec="seconds")
        rid = secrets.token_hex(6)

        with self.db:
            self.db.execute(
                "INSERT INTO records(id, kind, title, body, event_date, created_at,"
                " updated_at, pinned, tags, kept_by, why, status, importance)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, kind, title, body, event_date, stamp, stamp, int(pinned),
                 json.dumps(tags, ensure_ascii=False), kept_by, why, status, importance),
            )
            for other in link_to or []:
                self._link(rid, other)
        return self.get(rid)

    def link(self, a: str, b: str) -> None:
        with self.db:
            self._link(a, b)

    def _link(self, a: str, b: str) -> None:
        if a == b:
            raise StoreError("不能把一条记录挂到它自己身上")
        for rid in (a, b):
            if not self.db.execute("SELECT 1 FROM records WHERE id=?", (rid,)).fetchone():
                raise StoreError(f"找不到记录 {rid}")
        lo, hi = sorted((a, b))
        self.db.execute("INSERT OR IGNORE INTO links(a, b) VALUES (?, ?)", (lo, hi))

    def set_status(self, rid: str, status: str) -> Record:
        if status not in PLAN_STATUSES:
            raise StoreError("status 只能是 open 或 done")
        if self.get(rid).kind != "plan":
            raise StoreError(f"{rid} 不是计划")
        with self.db:
            self.db.execute(
                "UPDATE records SET status=?, updated_at=? WHERE id=?",
                (status, self.now().isoformat(timespec="seconds"), rid),
            )
        return self.get(rid)

    def open_plans(self) -> list[Record]:
        rows = self.db.execute(
            "SELECT * FROM records WHERE kind='plan' AND status='open'"
            " ORDER BY event_date, created_at, rowid"
        ).fetchall()
        return [Record.from_row(r) for r in rows]

    # --- weight ---

    def _check_fives(self, event_date: str, exclude: str = "") -> None:
        """Keep my 5s scarce: at most FIVES_PER_WEEK per ISO week.

        Counts my current score for each record — the hindsight one if reviewed, otherwise
        the write-time one — so a review can't sneak a third 5 past two unreviewed ones.
        Her ratings are hers and never counted.
        """
        start, end = weight.iso_week(event_date)
        rows = self.db.execute(
            "SELECT id, title FROM records WHERE event_date BETWEEN ? AND ? AND id<>?"
            " AND (reviewed=5 OR (reviewed=0 AND importance=5)) ORDER BY event_date",
            (start, end, exclude),
        ).fetchall()
        if len(rows) >= weight.FIVES_PER_WEEK:
            names = "、".join(f"[{r['id']}] {r['title']}" for r in rows)
            raise StoreError(
                f"{start} 这一周的 5 分已经给满 {weight.FIVES_PER_WEEK} 条：{names}。"
                "要给这条 5 分，先把其中一条降下来。"
            )

    def rate(self, rid: str, value: int, by: str = "ai") -> Record:
        """ai = hindsight review (overrides the write-time score); human = hers, final. 0 clears."""
        rec = self.get(rid)
        value = _check_score(value, allow_zero=True)
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            if by == "human":
                self.db.execute("UPDATE records SET human_rating=? WHERE id=?", (value, rid))
            elif by == "ai":
                if value == 5:
                    self._check_fives(rec.event_date, exclude=rid)
                self.db.execute(
                    "UPDATE records SET reviewed=?, reviewed_at=? WHERE id=?",
                    (value, now if value else "", rid),
                )
            else:
                raise StoreError("by 只能是 ai 或 human")
        return self.get(rid)

    def touch(self, rid: str) -> None:
        """Somebody read this. Reading is evidence that it still matters."""
        with self.db:
            self.db.execute(
                "UPDATE records SET read_count=read_count+1, last_read_at=? WHERE id=?",
                (self.now().isoformat(timespec="seconds"), rid),
            )

    def link_counts(self, ids: list[str]) -> dict[str, int]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT id, COUNT(*) n FROM (SELECT a id FROM links WHERE a IN ({marks})"
            f" UNION ALL SELECT b FROM links WHERE b IN ({marks})) GROUP BY id",
            (*ids, *ids),
        ).fetchall()
        return {r["id"]: r["n"] for r in rows}

    def weigh(self, records: list[Record]) -> dict[str, tuple[float, str]]:
        """id -> (score, layer)."""
        today = self.today()
        links = self.link_counts([r.id for r in records])
        out = {}
        for r in records:
            s = weight.score(r, today, links.get(r.id, 0))
            out[r.id] = (s, weight.layer(r, today, s))
        return out

    def review(self, period: str = "week", day: str | None = None) -> tuple[str, str, list[Record]]:
        """Records of one ISO week or month, for re-rating with hindsight.

        Without a day: the previous week / month, since hindsight needs distance.
        """
        if period not in ("week", "month"):
            raise StoreError("period 只能是 week 或 month")
        if day:
            day = self._check_date(day, "day")
        else:
            today = self.today()
            back = today - timedelta(days=7 if period == "week" else today.day)
            day = back.isoformat()
        start, end = (weight.iso_week if period == "week" else weight.month)(day)
        return start, end, self.timeline(start, end, limit=1000)

    def surface(self, n: int = 5, exclude: set[str] | None = None) -> list[Record]:
        """Heaviest records outside the hot set: what should float up on its own."""
        exclude = exclude or set()
        cutoff = (self.today() - timedelta(days=weight.HOT_DAYS)).isoformat()
        # Anchors never fade and start at 5; left in, they would hold every slot forever.
        rows = self.db.execute(
            "SELECT * FROM records WHERE event_date < ? AND pinned=0 AND status<>'open'"
            " AND kind<>'anchor'",
            (cutoff,),
        ).fetchall()
        records = [Record.from_row(r) for r in rows if r["id"] not in exclude]
        scores = self.weigh(records)
        records.sort(key=lambda r: scores[r.id][0], reverse=True)
        return records[:n]

    def set_pinned(self, rid: str, pinned: bool) -> Record:
        with self.db:
            cur = self.db.execute(
                "UPDATE records SET pinned=?, updated_at=? WHERE id=?",
                (int(pinned), self.now().isoformat(timespec="seconds"), rid),
            )
        if not cur.rowcount:
            raise StoreError(f"找不到记录 {rid}")
        return self.get(rid)

    # --- read ---

    def get(self, rid: str) -> Record:
        row = self.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        if not row:
            raise StoreError(f"找不到记录 {rid}")
        return Record.from_row(row)

    def linked(self, rid: str) -> list[Record]:
        rows = self.db.execute(
            "SELECT r.* FROM links l JOIN records r"
            " ON r.id = CASE WHEN l.a = ? THEN l.b ELSE l.a END"
            " WHERE l.a = ? OR l.b = ? ORDER BY r.event_date, r.created_at, r.rowid",
            (rid, rid, rid),
        ).fetchall()
        return [Record.from_row(r) for r in rows]

    def same_day(self, rec: Record, kinds: tuple[str, ...] | None = None) -> list[Record]:
        sql = "SELECT * FROM records WHERE event_date=? AND id<>?"
        args: list = [rec.event_date, rec.id]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        rows = self.db.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [Record.from_row(r) for r in rows]

    def timeline(
        self,
        start: str,
        end: str | None = None,
        kinds: list[str] | None = None,
        limit: int = 200,
    ) -> list[Record]:
        start = self._check_date(start, "start")
        end = self._check_date(end or start, "end")
        if end < start:
            start, end = end, start
        sql = "SELECT * FROM records WHERE event_date BETWEEN ? AND ?"
        args: list = [start, end]
        if kinds:
            _check_kinds(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        sql += " ORDER BY event_date, created_at, rowid LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        return [Record.from_row(r) for r in self.db.execute(sql, args).fetchall()]

    def add_image(
        self,
        data: bytes,
        caption: str = "",
        event_date: str | None = None,
        tags: list[str] | None = None,
    ) -> Record:
        """Keep the original bytes. event_date falls back to the photo's EXIF date, then today."""
        try:
            info = media.probe(data)
        except media.MediaError as e:
            raise StoreError(str(e)) from None
        caption = (caption or "").replace("\r\n", "\n").strip()
        event_date = event_date or info.taken_on
        body = caption or "（没写说明）"
        title = caption.splitlines()[0][:40] if caption else "照片"
        rec = self.write("image", body, event_date, title, tags)
        name = f"{rec.id}.{info.ext}"
        (self.images_dir / name).write_bytes(data)
        with self.db:
            self.db.execute(
                "INSERT INTO images(record_id, file, mime, width, height, bytes, sha256)"
                " VALUES (?,?,?,?,?,?,?)",
                (rec.id, name, info.mime, info.width, info.height, len(data),
                 hashlib.sha256(data).hexdigest()),
            )
        return rec

    def image(self, rid: str) -> tuple[Record, dict, bytes]:
        rec = self.get(rid)
        row = self.db.execute("SELECT * FROM images WHERE record_id=?", (rid,)).fetchone()
        if not row:
            raise StoreError(f"{rid} 不是图片记录")
        return rec, dict(row), (self.images_dir / row["file"]).read_bytes()

    def earliest_date(self) -> str | None:
        row = self.db.execute("SELECT MIN(event_date) FROM records").fetchone()
        return row[0] if row else None

    def recent(self, kinds: list[str], limit: int = 12) -> list[Record]:
        _check_kinds(kinds)
        rows = self.db.execute(
            f"SELECT * FROM records WHERE kind IN ({','.join('?' * len(kinds))})"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*kinds, max(1, min(limit, 100))),
        ).fetchall()
        return [Record.from_row(r) for r in rows]

    def excerpts(
        self,
        kept_by: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 50,
    ) -> list[Record]:
        """The excerpt shelf, newest day first."""
        sql = "SELECT * FROM records WHERE kind='excerpt'"
        args: list = []
        if kept_by:
            if kept_by not in KEEPERS:
                raise StoreError("kept_by 填 ai 或 human")
            sql += " AND kept_by=?"
            args.append(kept_by)
        if start:
            sql += " AND event_date >= ?"
            args.append(self._check_date(start, "start"))
        if end:
            sql += " AND event_date <= ?"
            args.append(self._check_date(end, "end"))
        sql += " ORDER BY event_date DESC, created_at DESC, rowid DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        return [Record.from_row(r) for r in self.db.execute(sql, args).fetchall()]

    def latest(self, kind: str, n: int = 1) -> list[Record]:
        _check_kinds([kind])
        rows = self.db.execute(
            "SELECT * FROM records WHERE kind=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (kind, n),
        ).fetchall()
        return [Record.from_row(r) for r in rows]

    def pinned(self) -> list[Record]:
        rows = self.db.execute(
            "SELECT * FROM records WHERE pinned=1 ORDER BY event_date, created_at, rowid"
        ).fetchall()
        return [Record.from_row(r) for r in rows]

    def search(
        self,
        query: str,
        kinds: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 20,
    ) -> list[Record]:
        # Space = AND between groups; "|" inside a group = OR ("难过|哭|崩 那天").
        groups = [[a for a in t.split("|") if a] for t in (query or "").split()]
        groups = [g for g in groups if g]
        if not groups:
            raise StoreError("query 不能为空")
        where: list[str] = []
        args: list = []

        if all(len(a) >= _TRIGRAM_MIN for g in groups for a in g):
            quote = lambda a: '"' + a.replace('"', '""') + '"'  # noqa: E731
            match = " AND ".join("(" + " OR ".join(quote(a) for a in g) + ")" for g in groups)
            base = (
                "SELECT r.* FROM records_fts f JOIN records r ON r.rowid = f.rowid"
                " WHERE records_fts MATCH ?"
            )
            args.append(match)
            order = " ORDER BY bm25(records_fts), r.event_date DESC"
        else:
            base = "SELECT r.* FROM records r WHERE 1=1"
            one = ("(r.title LIKE ? ESCAPE '\\' OR r.body LIKE ? ESCAPE '\\'"
                   " OR r.tags LIKE ? ESCAPE '\\' OR r.why LIKE ? ESCAPE '\\')")
            for g in groups:
                where.append("(" + " OR ".join([one] * len(g)) + ")")
                for a in g:
                    like = "%" + a.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    args += [like] * 4
            order = " ORDER BY r.event_date DESC, r.created_at DESC"

        if kinds:
            _check_kinds(kinds)
            where.append(f"r.kind IN ({','.join('?' * len(kinds))})")
            args += kinds
        if start:
            where.append("r.event_date >= ?")
            args.append(self._check_date(start, "start"))
        if end:
            where.append("r.event_date <= ?")
            args.append(self._check_date(end, "end"))

        limit = max(1, min(limit, 100))
        sql = base + "".join(" AND " + w for w in where) + order + " LIMIT ?"
        args.append(limit * 5)
        found = [Record.from_row(r) for r in self.db.execute(sql, args).fetchall()]
        # Relevance first, weight second: a heavy memory can climb a few places, not jump the list.
        scores = self.weigh(found)
        ranked = sorted(
            enumerate(found),
            key=lambda p: scores[p[1].id][0] / (1 + 0.15 * p[0]),
            reverse=True,
        )
        return [r for _, r in ranked[:limit]]

    def latest_diary(self) -> Record | None:
        row = self.db.execute(
            "SELECT * FROM records WHERE kind='diary' AND event_date <= ?"
            " ORDER BY event_date DESC, created_at DESC, rowid DESC LIMIT 1",
            (self.today().isoformat(),),
        ).fetchone()
        return Record.from_row(row) if row else None

    def review_due(self) -> bool:
        """Last week has records and nothing has been re-rated in the past 7 days."""
        today = self.today()
        start, end = weight.iso_week((today - timedelta(days=7)).isoformat())
        has_last_week = self.db.execute(
            "SELECT 1 FROM records WHERE event_date BETWEEN ? AND ? LIMIT 1", (start, end)
        ).fetchone()
        reviewed_lately = self.db.execute(
            "SELECT 1 FROM records WHERE reviewed_at >= ? LIMIT 1",
            ((today - timedelta(days=7)).isoformat(),),
        ).fetchone()
        return bool(has_last_week) and not reviewed_lately

    # --- anniversaries ---

    def anniversaries(self) -> list[dict]:
        row = self.db.execute("SELECT value FROM meta WHERE key='anniversaries'").fetchone()
        return json.loads(row[0]) if row else []

    def set_anniversary(self, name: str, day: str) -> list[dict]:
        name = (name or "").strip()
        if not name:
            raise StoreError("纪念日要有名字")
        day = self._check_date(day, "day")
        items = [a for a in self.anniversaries() if a["name"] != name]
        items.append({"name": name, "date": day})
        items.sort(key=lambda a: a["date"])
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('anniversaries', ?)",
                (json.dumps(items, ensure_ascii=False),),
            )
        return items

    def anniversary_lines(self) -> list[str]:
        today = self.today()
        out = []
        for a in self.anniversaries():
            d = date.fromisoformat(a["date"])
            if d > today:
                out.append(f"离{a['name']}还有 {(d - today).days} 天（{a['date']}）")
                continue
            n = (today - d).days + 1                      # the day itself is day 1
            line = f"{a['name']}第 {n} 天（{a['date']}）"
            months = (today.year - d.year) * 12 + today.month - d.month
            if today.day == d.day and months > 0:
                line += f"，今天满 {months} 个月" if months % 12 else f"，今天满 {months // 12} 年"
            elif n % 100 == 0:
                line += "，整百"
            out.append(line)
        return out

    def wake(self) -> dict:
        """What a new window needs first. Titles only; bodies come through read."""
        today = self.today()
        yesterday = today - timedelta(days=1)
        handoff = self.latest("handoff", 1)
        handoff_ids = {r.id for r in handoff}
        hot = {
            "days": [r for r in self.timeline(yesterday.isoformat(), today.isoformat())
                     if r.id not in handoff_ids],
            "handoff": handoff,
            "pinned": self.pinned(),
            "plans": self.open_plans(),
        }
        # No diary yesterday or today: point at the newest one so the thread isn't lost.
        hot["last_diary"] = []
        if not any(r.kind == "diary" for r in hot["days"]):
            if (d := self.latest_diary()) is not None:
                hot["last_diary"] = [d]
        month_ago = _months_back(today, 1)
        hot["month_ago"] = self.timeline(month_ago.isoformat()) if month_ago else []
        shown = {r.id for group in hot.values() for r in group}
        hot["surfaced"] = self.surface(5, shown)
        hot["anniversaries"] = self.anniversary_lines()
        hot["review_due"] = self.review_due()
        return hot


def _months_back(d: date, n: int) -> date | None:
    """Same day n months earlier, or None when that month has no such day (e.g. 31st)."""
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    try:
        return date(y, m + 1, d.day)
    except ValueError:
        return None


def _check_kinds(kinds: list[str]) -> None:
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise StoreError(f"不认识的 kind：{', '.join(bad)}；可选 {', '.join(KINDS)}")


def _check_score(value, allow_zero: bool = False) -> int:
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        raise StoreError("分数要是 1 到 5 的整数") from None
    if not (1 <= v <= 5 or (allow_zero and v == 0)):
        raise StoreError("分数要是 1 到 5 的整数")
    return v


def _default_title(kind: str, event_date: str, body: str) -> str:
    if kind == "diary":
        return f"{event_date} 日记"
    if kind == "handoff":
        return f"{event_date} 交接"
    first = body.strip().splitlines()[0].strip()
    return first[:40] + ("…" if len(first) > 40 else "")
