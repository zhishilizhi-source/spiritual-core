"""Import Ombre Brain buckets into a Spiritual Core database, following ob_scan's plan.

- deleted (tombstoned) buckets are skipped
- fragments of one grow batch are joined back, in order, into a single diary; text unchanged
- bodies are otherwise copied as-is; OB's UTC timestamps become Beijing time
- write-time scores come from OB importance (1-10 -> 1-4); 5s are left for hindsight review
- every OB id -> new id is written to a map file; it only ever imports into a new file

usage: python migrate/ob_import.py <buckets_dir> <plan.csv> <core.db> <map.csv>
"""

from __future__ import annotations

import csv
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrate.ob_scan import EARLY_HOURS, _AUTO_NAME, split  # noqa: E402
from spiritual_core.store import Store  # noqa: E402

TAG = "OB旧桶"
_STAMP = re.compile(r"^20\d\d-\d\d-\d\d \d\d-\d\d-\d\d\s*")


def score(ob) -> int:
    try:
        return min(4, max(1, math.ceil(int(ob) / 2)))
    except (TypeError, ValueError):
        return 0


def title_of(name: str) -> str | None:
    rest = _STAMP.sub("", name).strip() if _AUTO_NAME.match(name) else name.strip()
    return rest or None


def why_of(meta: dict) -> str:
    parts = [str(meta.get("why_remembered") or "").strip()]
    meaning = meta.get("meaning") or []
    parts += [str(m).strip() for m in (meaning if isinstance(meaning, list) else [meaning])]
    return "\n".join(p for p in parts if p)


def tags_of(meta: dict) -> list[str]:
    seen, out = set(), []
    for t in [*(meta.get("domain") or []), *(meta.get("tags") or []), TAG]:
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def event_date_of(row: dict) -> str:
    """The scan's date, except where the scan itself said not to trust it."""
    if not row["confidence"].startswith("低"):
        return row["event_date"]
    local = datetime.fromisoformat(row["created_local"])
    day = local.date() - timedelta(days=1) if local.hour < EARLY_HOURS else local.date()
    return day.isoformat()


_TEST = re.compile(r"测试信|测试写入|测试条目|功能测试|测试.{0,6}功能")


def is_test(meta: dict, body: str) -> bool:
    """Scaffolding from setting OB up. Kept, but it must not float up as a memory."""
    return bool(_TEST.search(str(meta.get("name") or "") + " " + body[:120]))


def plan_done(meta: dict) -> bool:
    return meta.get("resolved") is True or meta.get("status") in ("resolved", "abandoned")


def main() -> None:
    root, plan_csv, db_path, map_csv = (Path(a) for a in sys.argv[1:5])
    rows = [r for r in csv.DictReader(plan_csv.open(encoding="utf-8-sig")) if not r["deleted"]]
    # Store.write commits per record, so the import can't be one transaction.
    # Instead it only ever targets a brand-new file, which is thrown away if anything fails.
    if db_path.exists():
        sys.exit(f"{db_path} 已经存在。只往新文件里搬，搬好了再换上去。")
    store = Store(db_path)
    try:
        mapping, n_single, n_batch = run(root, rows, store)
    except BaseException:
        store.close()
        for f in db_path.parent.glob(db_path.name + "*"):
            f.unlink()
        raise
    store.close()
    with map_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ob_id", "new_id", "kind", "event_date", "title"])
        w.writerows(mapping)
    print(f"{n_single} 个单桶 + {n_batch} 组拼回 → {len(set(m[1] for m in mapping))} 条记录")


def run(root: Path, rows: list[dict], store: Store):
    loaded = {}
    for r in rows:
        meta, body = split((root / r["path"]).read_text(encoding="utf-8"))
        loaded[r["id"]] = (r, meta, body)

    batches: dict[str, list] = defaultdict(list)
    singles = []
    for r, meta, body in loaded.values():
        (batches[r["grow_batch"]] if r["grow_batch"] else singles).append((r, meta, body))

    mapping = []
    for r, meta, body in singles:
        kind = r["kind"]
        rec = store.write(
            kind, body,
            event_date=event_date_of(r),
            title=title_of(r["name"]),
            tags=tags_of(meta),
            pinned=bool(meta.get("pinned")),
            created_at=r["created_local"] + "+08:00",
            kept_by="ai" if kind == "letter" else "",
            why=why_of(meta),
            importance=1 if is_test(meta, body) else score(meta.get("importance")),
        )
        if kind == "plan" and (plan_done(meta) or is_test(meta, body)):
            store.db.execute("UPDATE records SET status='done' WHERE id=?", (rec.id,))
        mapping.append((r["id"], rec.id, kind, rec.event_date, rec.title))

    for batch, parts in batches.items():
        parts.sort(key=lambda p: (p[0]["created_utc"], p[0]["id"]))
        days = [event_date_of(p[0]) for p in parts]
        day = max(set(days), key=days.count)
        tags, whys = [], []
        for _, meta, _ in parts:
            tags += [t for t in tags_of(meta) if t not in tags]
            if w := why_of(meta):
                whys.append(w)
        rec = store.write(
            "diary", "\n\n".join(body for _, _, body in parts),
            event_date=day,
            title=f"{day} 日记",
            tags=tags,
            created_at=parts[0][0]["created_local"] + "+08:00",
            why="\n".join(whys),
            importance=max(score(m.get("importance")) for _, m, _ in parts),
        )
        for p in parts:
            mapping.append((p[0]["id"], rec.id, "diary", day, f"{rec.title}（grow {batch} 拼回）"))

    store.db.execute(
        "INSERT INTO meta(key, value) VALUES ('ob_imported', ?)",
        (datetime.now().isoformat(timespec="seconds"),),
    )
    store.db.commit()
    return mapping, len(singles), len(batches)


if __name__ == "__main__":
    main()
