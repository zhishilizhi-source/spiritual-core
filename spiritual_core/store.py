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

from . import health, media, presence, weight

KINDS = (
    "diary", "intimate", "handoff", "anchor", "raw", "excerpt", "image",
    "letter", "memory", "note", "plan", "work", "daily", "inner", "message",
    "weekly", "monthly",
)

# Short-term memory is yesterday and today; long-term is the weekly and the monthly.
# event_date of a weekly is any day in its ISO week, of a monthly any day in its month.
DIGESTS = ("weekly", "monthly")

# `message` is the AI speaking first: a line left for her on the page. She can answer it;
# a day after she answers, it is filed away as a note with her answer under it.
MESSAGE_TAG = "留言"
# A message she has read but not answered leaves the board this many days after she read it.
SEEN_KEEP_DAYS = 3

# `inner` is the AI's own 心里话. Only the AI writes it, and it may keep it locked:
# a locked one never leaves the server toward the page, only its existence does.
LOCKABLE = ("inner",)

# Who wanted an excerpt kept. Deployment-neutral: display names come from the client.
KEEPERS = ("ai", "human")
# Kinds that must say whose they are: excerpt = who wanted it kept, letter = who wrote it.
NEEDS_KEEPER = ("excerpt", "letter")
# Kinds that may say whose they are. A note she writes on the page is marked human: it is
# a line left for him, the mirror of `message` — he reads it, answers, and a day later his
# answer is filed under her words.
MAY_KEEPER = ("note",)

PLAN_STATUSES = ("open", "done")

# Her mood of the moment. The page tints its sky with it; wake tells the AI.
MOODS = ("甜", "想你", "开心", "平静", "馋", "累", "难过", "生气", "焦虑")

# Trigram FTS needs at least 3 characters per term; shorter terms go through LIKE.
_TRIGRAM_MIN = 3

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BATCH_RE = re.compile(r"^[A-Za-z0-9-]{1,48}$")

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
    last_read_at TEXT NOT NULL DEFAULT '',
    locked      INTEGER NOT NULL DEFAULT 0,
    unlocked_at TEXT NOT NULL DEFAULT '',
    batch       TEXT NOT NULL DEFAULT '',
    seen_at     TEXT NOT NULL DEFAULT '',
    reply       TEXT NOT NULL DEFAULT '',
    replied_at  TEXT NOT NULL DEFAULT '',
    engine      TEXT NOT NULL DEFAULT ''
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

CREATE TABLE IF NOT EXISTS moods (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    at    TEXT NOT NULL,
    day   TEXT NOT NULL,
    mood  TEXT NOT NULL,
    note  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS moods_by_day ON moods(day, at);

-- every edit keeps what was there before, so a fix can always be undone
CREATE TABLE IF NOT EXISTS revisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id  TEXT NOT NULL,
    at         TEXT NOT NULL,
    by         TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    event_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS revisions_by_record ON revisions(record_id, at);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SCHEMA_VERSION = "7"

# Columns added after v1, in the order they arrived. _migrate adds whichever are missing.
_ADDED_COLUMNS = (
    ("status", "TEXT NOT NULL DEFAULT ''"),          # v2
    ("importance", "INTEGER NOT NULL DEFAULT 0"),    # v3
    ("reviewed", "INTEGER NOT NULL DEFAULT 0"),
    ("reviewed_at", "TEXT NOT NULL DEFAULT ''"),
    ("human_rating", "INTEGER NOT NULL DEFAULT 0"),
    ("read_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_read_at", "TEXT NOT NULL DEFAULT ''"),
    ("locked", "INTEGER NOT NULL DEFAULT 0"),        # v4
    ("unlocked_at", "TEXT NOT NULL DEFAULT ''"),
    ("batch", "TEXT NOT NULL DEFAULT ''"),              # v5
    ("seen_at", "TEXT NOT NULL DEFAULT ''"),            # v6: messages
    ("reply", "TEXT NOT NULL DEFAULT ''"),
    ("replied_at", "TEXT NOT NULL DEFAULT ''"),
    ("engine", "TEXT NOT NULL DEFAULT ''"),             # v7: which engine wrote it
)

# Handoffs from different engines don't cover each other: wake shows the newest of each
# engine's handoffs from this many days back.
HANDOFF_DAYS = 7
ENGINE_MAX = 40
# Only handoffs and memories (reflections) carry the engine, for comparing myself across
# engines. Anchors, weeklies and monthlies are who I am as a whole: an engine on them would
# read as someone else's. The days themselves don't either: the dates engines changed are known.
ENGINE_KINDS = ("handoff", "memory")


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
    locked: bool = False
    unlocked_at: str = ""
    batch: str = ""          # photos saved together share one
    seen_at: str = ""        # messages: when she first opened it
    reply: str = ""          # messages: her answer
    replied_at: str = ""
    engine: str = ""         # the engine it came from; the one who wrote it is always him

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
            locked=bool(row["locked"]),
            unlocked_at=row["unlocked_at"],
            batch=row["batch"],
            seen_at=row["seen_at"],
            reply=row["reply"],
            replied_at=row["replied_at"],
            engine=row["engine"],
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
        self.db.executescript(health.SCHEMA)
        self.db.executescript(presence.SCHEMA)
        presence.migrate(self.db)
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
        locked: bool = False,
        engine: str = "",
    ) -> Record:
        """Always creates a new record. Nothing is ever merged into an existing one.

        Excerpts are stored verbatim: the body is not stripped or reflowed.
        """
        if kind not in KINDS:
            raise StoreError(f"kind 只能是 {', '.join(KINDS)} 之一，收到 {kind!r}")
        if locked and kind not in LOCKABLE:
            raise StoreError("只有心里话（inner）能上锁")
        kept_by = (kept_by or "").strip()
        # Browsers submit textarea newlines as CRLF; line endings are not content.
        body = (body or "").replace("\r\n", "\n")
        why = (why or "").replace("\r\n", "\n").strip()
        if kind in NEEDS_KEEPER:
            if kept_by not in KEEPERS:
                what = "谁想留的" if kind == "excerpt" else "谁写的"
                raise StoreError(f"{kind} 要写明是{what}：填 ai 或 human")
        elif kept_by and (kind not in MAY_KEEPER or kept_by not in KEEPERS):
            raise StoreError("kept_by 只用在 excerpt、letter 和 note 上，填 ai 或 human")
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
        engine = " ".join((engine or "").split())[:ENGINE_MAX] if kind in ENGINE_KINDS else ""

        with self.db:
            self.db.execute(
                "INSERT INTO records(id, kind, title, body, event_date, created_at,"
                " updated_at, pinned, tags, kept_by, why, status, importance, locked, engine)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, kind, title, body, event_date, stamp, stamp, int(pinned),
                 json.dumps(tags, ensure_ascii=False), kept_by, why, status, importance,
                 int(locked), engine),
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

    def edit(
        self,
        rid: str,
        title: str | None = None,
        body: str | None = None,
        event_date: str | None = None,
        by: str = "human",
        whole_batch: bool = False,
    ) -> Record:
        """Fix a record's title, text or date. The old version goes to `revisions` first.

        The AI's own 心里话 can't be edited from here, locked or not. For a photo, the text is
        its caption (the title follows it); with whole_batch every photo in its group changes.
        """
        rec = self.get(rid)
        if rec.kind == "inner":
            raise StoreError("心里话是他的，不能从这里改")
        if by not in KEEPERS:
            raise StoreError("by 只能是 ai 或 human")
        if event_date is not None:
            event_date = self._check_date(event_date)
        if body is not None:
            body = body.replace("\r\n", "\n")
            if rec.kind != "excerpt":
                body = body.strip()
            if not body.strip():
                raise StoreError("正文不能改成空的")
        if title is not None:
            title = title.strip()
        targets = self.batch_members(rec.batch) if whole_batch and rec.batch else [rec]
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            for t in targets:
                new_body = body if body is not None else t.body
                if t.kind == "image":
                    new_title = (new_body.splitlines()[0][:40] if new_body and new_body != "（没写说明）" else "照片")
                else:
                    new_title = title if title else t.title
                new_date = event_date or t.event_date
                if (new_title, new_body, new_date) == (t.title, t.body, t.event_date):
                    continue
                self.db.execute(
                    "INSERT INTO revisions(record_id, at, by, title, body, event_date) VALUES (?,?,?,?,?,?)",
                    (t.id, now, by, t.title, t.body, t.event_date),
                )
                self.db.execute(
                    "UPDATE records SET title=?, body=?, event_date=?, updated_at=? WHERE id=?",
                    (new_title, new_body, new_date, now, t.id),
                )
        return self.get(rid)

    def revisions(self, rid: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT at, by, title, body, event_date FROM revisions WHERE record_id=? ORDER BY at, id",
            (rid,),
        ).fetchall()
        return [dict(r) for r in rows]

    APPEND_WINDOW_HOURS = 24

    def append(self, rid: str, text: str) -> Record:
        """Continue a record written moments ago, so a long entry can arrive in pieces and
        still be one entry. Text is joined exactly as given. Only recent records, so an old
        memory can't be quietly added to."""
        rec = self.get(rid)
        text = (text or "").replace("\r\n", "\n")
        if not text:
            raise StoreError("text 不能为空")
        written = datetime.fromisoformat(rec.created_at)
        if written.tzinfo is None:
            written = written.replace(tzinfo=self.tz)
        if self.now() - written > timedelta(hours=self.APPEND_WINDOW_HOURS):
            raise StoreError(f"{rid} 是 {rec.created_at[:10]} 写的，只能接着写 {self.APPEND_WINDOW_HOURS} 小时内新写的记录")
        if rec.kind == "image":
            raise StoreError("照片的说明不能这样接")
        with self.db:
            self.db.execute(
                "UPDATE records SET body = body || ?, updated_at=? WHERE id=?",
                (text, self.now().isoformat(timespec="seconds"), rid),
            )
        return self.get(rid)

    def set_locked(self, rid: str, locked: bool) -> Record:
        rec = self.get(rid)
        if rec.kind not in LOCKABLE:
            raise StoreError("只有心里话（inner）能上锁")
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "UPDATE records SET locked=?, unlocked_at=? WHERE id=?",
                (int(locked), "" if locked else now, rid),
            )
        return self.get(rid)

    def locked_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM records WHERE locked=1").fetchone()[0]

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
        batch: str = "",
    ) -> Record:
        """Keep the original bytes. event_date falls back to the photo's EXIF date, then today.

        Photos saved together share a batch; each stays its own record with its own date.
        """
        batch = batch if _BATCH_RE.match(batch or "") else ""
        try:
            info = media.probe(data)
        except media.MediaError as e:
            raise StoreError(str(e)) from None
        caption = (caption or "").replace("\r\n", "\n").strip()
        event_date = event_date or info.taken_on
        body = caption or "（没写说明）"
        title = caption.splitlines()[0][:40] if caption else ("视频" if media.is_video(info.mime) else "照片")
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
            if batch:
                self.db.execute("UPDATE records SET batch=? WHERE id=?", (batch, rec.id))
        return self.get(rec.id)

    def delete_image(self, rid: str) -> None:
        """Take a photo off the page. Nothing is destroyed: the file and a JSON of its record
        (with any revisions and links) go to data/trash/, from where it can be put back."""
        rec = self.get(rid)
        if rec.kind != "image":
            raise StoreError("只有照片和视频能从网页上删")
        row = self.db.execute("SELECT * FROM images WHERE record_id=?", (rid,)).fetchone()
        trash = self.path.parent / "trash"
        trash.mkdir(exist_ok=True)
        keep = {
            "record": dict(self.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()),
            "image": dict(row) if row else None,
            "revisions": [dict(r) for r in self.db.execute("SELECT * FROM revisions WHERE record_id=?", (rid,))],
            "links": [dict(r) for r in self.db.execute("SELECT * FROM links WHERE a=? OR b=?", (rid, rid))],
            "deleted_at": self.now().isoformat(timespec="seconds"),
        }
        (trash / f"{rid}.json").write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        if row and (self.images_dir / row["file"]).exists():
            (self.images_dir / row["file"]).replace(trash / row["file"])
        with self.db:
            self.db.execute("DELETE FROM revisions WHERE record_id=?", (rid,))
            self.db.execute("DELETE FROM records WHERE id=?", (rid,))

    def batch_members(self, batch: str) -> list[Record]:
        if not batch:
            return []
        rows = self.db.execute(
            "SELECT * FROM records WHERE batch=? ORDER BY created_at, rowid", (batch,)
        ).fetchall()
        return [Record.from_row(r) for r in rows]

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

    def handoffs(self) -> list[Record]:
        """The newest handoff overall, plus the newest from every other engine in the last
        HANDOFF_DAYS days, newest first. Two doors open at once no longer hide each other."""
        since = (self.today() - timedelta(days=HANDOFF_DAYS)).isoformat()
        rows = self.db.execute(
            "SELECT * FROM records WHERE kind='handoff' AND created_at >= ?"
            " ORDER BY created_at DESC, rowid DESC", (since,)).fetchall()
        out, engines = [], set()
        for row in rows:
            r = Record.from_row(row)
            if r.engine not in engines:
                engines.add(r.engine)
                out.append(r)
        return out or self.latest("handoff", 1)

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

    # --- messages: he speaks first, she answers when she likes ---

    def leave(self, body: str, title: str | None = None) -> Record:
        return self.write("message", body, title=title, tags=[MESSAGE_TAG])

    def page_messages(self) -> list[Record]:
        """What the top of her page shows: every message still out. One she hasn't answered
        stays however old; one she answered is filed the next day and leaves on its own."""
        self.recycle_messages()
        rows = self.db.execute(
            "SELECT * FROM records WHERE kind='message' ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
        return [Record.from_row(r) for r in rows]

    def mark_seen(self, ids: list[str]) -> None:
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            for rid in ids:
                self.db.execute(
                    "UPDATE records SET seen_at=? WHERE id=? AND kind='message' AND seen_at=''",
                    (now, rid))

    def reply(self, rid: str, text: str) -> Record:
        rec = self.get(rid)
        if rec.kind != "message":
            raise StoreError("只有留言能回")
        text = (text or "").replace("\r\n", "\n").strip()
        if not text:
            raise StoreError("回复不能是空的")
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "UPDATE records SET reply=?, replied_at=?, seen_at=CASE WHEN seen_at='' THEN ? ELSE seen_at END,"
                " updated_at=? WHERE id=?", (text, now, now, now, rid))
        return self.get(rid)

    def recycle_messages(self) -> int:
        """Messages she answered before today become notes, her answer written under his words.
        The message as it was stays in revisions."""
        today = self.today().isoformat()
        rows = self.db.execute(
            "SELECT * FROM records WHERE kind='message' AND replied_at <> '' AND substr(replied_at, 1, 10) < ?",
            (today,),
        ).fetchall()
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            for row in rows:
                r = Record.from_row(row)
                self.db.execute(
                    "INSERT INTO revisions(record_id, at, by, title, body, event_date) VALUES (?,?,?,?,?,?)",
                    (r.id, now, "ai", r.title, r.body, r.event_date))
                when = r.replied_at[5:16].replace("T", " ")
                body = f"{r.body}\n\n—— 多多回（{when}）——\n{r.reply}"
                self.db.execute("UPDATE records SET kind='note', body=?, updated_at=? WHERE id=?",
                                (body, now, r.id))
            # Read and left unanswered for three days: not every line needs a reply, and one
            # hanging on the board would feel like it's waiting on her. Unread ones stay.
            quiet = self.db.execute(
                "SELECT * FROM records WHERE kind='message' AND replied_at = '' AND seen_at <> ''"
                " AND substr(seen_at, 1, 10) <= ?",
                ((self.today() - timedelta(days=SEEN_KEEP_DAYS)).isoformat(),),
            ).fetchall()
            for row in quiet:
                r = Record.from_row(row)
                self.db.execute(
                    "INSERT INTO revisions(record_id, at, by, title, body, event_date) VALUES (?,?,?,?,?,?)",
                    (r.id, now, "ai", r.title, r.body, r.event_date))
                when = r.seen_at[5:16].replace("T", " ")
                body = f"{r.body}\n\n—— 多多读过（{when}）——"
                self.db.execute("UPDATE records SET kind='note', body=?, updated_at=? WHERE id=?",
                                (body, now, r.id))
        return len(rows) + len(quiet) + self._file_her_notes(today, now)

    def answered(self, days: int = 2) -> list[Record]:
        """Her answers to his messages in the last few days, filed away or not."""
        since = (self.today() - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT * FROM records WHERE reply <> '' AND kept_by <> 'human'"
            " AND substr(replied_at, 1, 10) >= ? ORDER BY replied_at DESC", (since,)).fetchall()
        return [Record.from_row(r) for r in rows]

    def unseen_messages(self) -> int:
        return self.db.execute(
            "SELECT count(*) FROM records WHERE kind='message' AND seen_at=''").fetchone()[0]

    # --- her notes: she leaves a line for him, he answers when he likes ---
    # seen_at = when he first read it; reply/replied_at = his answer. Once filed, the answer
    # lives in the body and reply is emptied, replied_at kept.

    def her_notes(self) -> list[Record]:
        """Notes she left that are still out: not answered yet, or answered today."""
        self.recycle_messages()
        rows = self.db.execute(
            "SELECT * FROM records WHERE kind='note' AND kept_by='human'"
            " AND (replied_at = '' OR reply <> '') ORDER BY created_at DESC, rowid DESC").fetchall()
        return [Record.from_row(r) for r in rows]

    def mark_read_by_him(self, rid: str) -> None:
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "UPDATE records SET seen_at=? WHERE id=? AND kind='note' AND kept_by='human'"
                " AND seen_at=''", (now, rid))

    def answer_note(self, rid: str, text: str) -> Record:
        rec = self.get(rid)
        if rec.kind != "note" or rec.kept_by != "human":
            raise StoreError("只有她写的便签能回")
        if rec.replied_at:
            raise StoreError("这张已经回过了")
        text = (text or "").replace("\r\n", "\n").strip()
        if not text:
            raise StoreError("回复不能是空的")
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                "UPDATE records SET reply=?, replied_at=?, seen_at=CASE WHEN seen_at='' THEN ? ELSE seen_at END,"
                " updated_at=? WHERE id=?", (text, now, now, now, rid))
        return self.get(rid)

    def _file_her_notes(self, today: str, now: str) -> int:
        """Her notes he answered before today take his answer under her words."""
        rows = self.db.execute(
            "SELECT * FROM records WHERE kind='note' AND kept_by='human' AND reply <> ''"
            " AND substr(replied_at, 1, 10) < ?", (today,)).fetchall()
        with self.db:
            for row in rows:
                r = Record.from_row(row)
                self.db.execute(
                    "INSERT INTO revisions(record_id, at, by, title, body, event_date) VALUES (?,?,?,?,?,?)",
                    (r.id, now, "human", r.title, r.body, r.event_date))
                when = r.replied_at[5:16].replace("T", " ")
                body = f"{r.body}\n\n—— 陆能回（{when}）——\n{r.reply}"
                self.db.execute("UPDATE records SET body=?, reply='', updated_at=? WHERE id=?",
                                (body, now, r.id))
        return len(rows)

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

    def weekly_due(self) -> tuple[str, str] | None:
        """The week a weekly is owed for: this week on Sunday, otherwise last week.
        Owed only when that week has records and no weekly yet."""
        today = self.today()
        day = today if today.weekday() == 6 else today - timedelta(days=7)
        return self._digest_due("weekly", *weight.iso_week(day.isoformat()))

    def monthly_due(self) -> tuple[str, str] | None:
        """The month a monthly is owed for: this month on its last day, otherwise last month."""
        today = self.today()
        start, end = weight.month(today.isoformat())
        if today.isoformat() != end:
            start, end = weight.month((date.fromisoformat(start) - timedelta(days=1)).isoformat())
        return self._digest_due("monthly", start, end)

    def _digest_due(self, kind: str, start: str, end: str) -> tuple[str, str] | None:
        lived = self.db.execute(
            "SELECT 1 FROM records WHERE event_date BETWEEN ? AND ? AND kind NOT IN (?, ?) LIMIT 1",
            (start, end, *DIGESTS),
        ).fetchone()
        written = self.db.execute(
            "SELECT 1 FROM records WHERE kind=? AND event_date BETWEEN ? AND ? LIMIT 1",
            (kind, start, end),
        ).fetchone()
        return (start, end) if lived and not written else None

    # --- her body, from the watch ---

    def health_ingest(self, sleep: str = "", heart: str = "", steps: str = "") -> dict:
        try:
            return health.ingest(self.db, self.tz, sleep, heart, steps)
        except health.HealthError as e:
            raise StoreError(str(e)) from None

    def health_night(self, day: date | None = None) -> dict | None:
        return health.night(self.db, self.tz, day or self.today())

    def health_steps(self, day: date) -> int | None:
        return health.steps(self.db, day)

    def health_synced(self) -> str:
        return health.synced(self.db)

    # --- my side: when I woke, my weather in words ---

    def log_wake(self) -> None:
        presence.log_wake(self.db, self.now())

    def wakes_on(self, day: date) -> list[str]:
        return presence.wakes_on(self.db, self.tz, day)

    def set_now(self, text: str, mood: str = "") -> dict:
        try:
            return presence.set_now(self.db, self.now(), text, mood)
        except ValueError as e:
            raise StoreError(str(e)) from None

    def latest_now(self) -> dict | None:
        return presence.latest_now(self.db)

    def now_on(self, day: date) -> list[dict]:
        return presence.now_on(self.db, self.tz, day)

    def add_mind(self, text: str) -> dict:
        try:
            return presence.add_mind(self.db, self.now(), text)
        except ValueError as e:
            raise StoreError(str(e)) from None

    def let_go(self, mid: int) -> dict:
        try:
            return presence.let_go(self.db, self.now(), int(mid))
        except (ValueError, TypeError) as e:
            raise StoreError(str(e)) from None

    def open_minds(self) -> list[dict]:
        return presence.open_minds(self.db)

    def minds_on(self, day: date) -> list[dict]:
        return presence.minds_on(self.db, self.tz, day)

    def line_of_the_day(self) -> tuple[Record | None, str]:
        """The words for today and why: the one I hung up today, or, when I haven't, one drawn
        by the date (the same all day, another tomorrow) with no why."""
        row = self.db.execute("SELECT excerpt_id, why FROM ai_line WHERE day=?",
                              (self.today().isoformat(),)).fetchone()
        if row:
            try:
                return self.get(row[0]), row[1]
            except StoreError:
                pass
        ids = [r[0] for r in self.db.execute("SELECT id FROM records WHERE kind='excerpt' ORDER BY id")]
        if not ids:
            return None, ""
        n = int(hashlib.sha256(self.today().isoformat().encode()).hexdigest(), 16)
        return self.get(ids[n % len(ids)]), ""

    def line_hung_today(self) -> bool:
        return self.db.execute("SELECT 1 FROM ai_line WHERE day=?", (self.today().isoformat(),)).fetchone() is not None

    def line_hooks(self, n: int = 8) -> list[tuple[str, str]]:
        """A handful of kept words to choose today's from: id and the first line that says
        something, skipping ones hung in the last month."""
        since = (self.today() - timedelta(days=30)).isoformat()
        rows = self.db.execute(
            "SELECT id, body FROM records WHERE kind='excerpt'"
            " AND id NOT IN (SELECT excerpt_id FROM ai_line WHERE day >= ?) ORDER BY random() LIMIT ?",
            (since, max(1, min(n, 20)))).fetchall()
        out = []
        for rid, body in rows:
            lines = [x.strip() for x in body.splitlines() if x.strip()]
            first = lines[1] if len(lines) > 1 and re.fullmatch(r".{1,6}[：:]", lines[0]) else (lines[0] if lines else "")
            out.append((rid, first[:40] + ("…" if len(first) > 40 else "")))
        return out

    def hang_line(self, rid: str, why: str = "") -> Record:
        rec = self.get(rid)
        if rec.kind != "excerpt":
            raise StoreError("只能挂原话节选（excerpt）")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO ai_line(day, excerpt_id, why, at) VALUES (?,?,?,?)",
                            (self.today().isoformat(), rid, " ".join((why or "").split())[:120],
                             self.now().isoformat(timespec="seconds")))
        return rec

    def written_by_me(self, since: str, limit: int = 8, until: str = "9999") -> list[Record]:
        """What I wrote from `since` on (before `until`): not her notes or the words she kept,
        not photos she uploaded, not transcripts."""
        rows = self.db.execute(
            "SELECT * FROM records WHERE created_at >= ? AND created_at < ? AND kind NOT IN ('image', 'raw')"
            " AND kept_by <> 'human' ORDER BY created_at DESC, rowid DESC LIMIT ?", (since, until, limit)).fetchall()
        return [Record.from_row(r) for r in rows]

    # --- moods ---

    def set_mood(self, mood: str, note: str = "") -> dict:
        mood = (mood or "").strip()
        if mood not in MOODS:
            raise StoreError(f"心情只能是：{'、'.join(MOODS)}")
        now = self.now()
        with self.db:
            self.db.execute(
                "INSERT INTO moods(at, day, mood, note) VALUES (?,?,?,?)",
                (now.isoformat(timespec="seconds"), now.date().isoformat(), mood,
                 (note or "").strip()[:200]),
            )
        return self.mood_now()

    def mood_now(self) -> dict | None:
        """Her latest mood today, or None if she hasn't set one yet today."""
        row = self.db.execute(
            "SELECT at, mood, note FROM moods WHERE day=? ORDER BY at DESC, id DESC LIMIT 1",
            (self.today().isoformat(),),
        ).fetchone()
        return dict(row) if row else None

    def mood_log(self, days: int = 7) -> list[dict]:
        since = (self.today() - timedelta(days=max(1, min(days, 90)) - 1)).isoformat()
        rows = self.db.execute(
            "SELECT day, at, mood, note FROM moods WHERE day >= ? ORDER BY at, id", (since,)
        ).fetchall()
        return [dict(r) for r in rows]

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
        handoff = self.handoffs()
        handoff_ids = {r.id for r in handoff}
        hot = {
            "days": [r for r in self.timeline(yesterday.isoformat(), today.isoformat())
                     if r.id not in handoff_ids],
            "handoff": handoff,
            "pinned": self.pinned(),
            "plans": self.open_plans(),
            "weekly": self.latest("weekly", 1),
            "monthly": self.latest("monthly", 1),
        }
        digest_ids = {r.id for r in hot["weekly"] + hot["monthly"]}
        hot["days"] = [r for r in hot["days"] if r.id not in digest_ids]
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
        hot["mood"] = self.mood_now()
        log = self.mood_log(2)
        hot["mood_yesterday"] = next(
            (m for m in reversed(log) if m["day"] == yesterday.isoformat()), None
        )
        hot["review_due"] = self.review_due()
        hot["weekly_due"] = self.weekly_due()
        hot["monthly_due"] = self.monthly_due()
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
    if kind == "weekly":
        start, end = weight.iso_week(event_date)
        return f"{start} ~ {end[5:]} 周记"
    if kind == "monthly":
        d = date.fromisoformat(event_date)
        return f"{d.year}年{d.month}月 月记"
    first = body.strip().splitlines()[0].strip()
    return first[:40] + ("…" if len(first) > 40 else "")
