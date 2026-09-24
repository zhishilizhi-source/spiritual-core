"""Dry run: read an Ombre Brain bucket export (markdown + YAML front matter) and propose,
for every bucket, what it becomes in Spiritual Core. Writes nothing into Spiritual Core.

Only the bucket *files* are read. No Ombre Brain code is used or imported.

usage: python migrate/ob_scan.py <buckets_dir> <out.csv>
"""

from __future__ import annotations

import csv
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

# OB's container ran on UTC and stored naive timestamps. Her calendar is Beijing time.
OB_UTC_OFFSET = timedelta(hours=8)
EARLY_HOURS = 5   # written before 05:00 local: probably about the previous day

_FULL = re.compile(r"(20\d\d)\s*[-年./]\s*(\d{1,2})\s*[-月./]\s*(\d{1,2})")
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_AUTO_NAME = re.compile(r"^20\d\d-\d\d-\d\d \d\d-\d\d-\d\d")   # OB auto stamp (UTC), not an event date


def _valid(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def explicit_date(text: str, year: int) -> str | None:
    head = "\n".join(text.strip().splitlines()[:3])[:200]
    if m := _FULL.search(head):
        return _valid(*map(int, m.groups()))
    if m := _MONTH_DAY.search(head):
        return _valid(year, int(m.group(1)), int(m.group(2)))
    return None


def split(raw: str) -> tuple[dict, str]:
    _, fm, body = raw.split("---", 2)
    return yaml.safe_load(fm) or {}, body.strip()


def propose_kind(meta: dict, body: str, path: str) -> str:
    tool = meta.get("source_tool") or ""
    domains = set(meta.get("domain") or [])
    tags = set(meta.get("tags") or [])
    name = str(meta.get("name") or "")
    head = body[:80]
    if tool == "letter" or meta.get("type") == "letter" or "letter" in domains or path.startswith("letters"):
        return "letter"
    if tool == "anchor" or meta.get("anchor"):
        return "anchor"
    if tool == "plan" or path.startswith("plans") or domains & {"计划", "待办", "plan", "pending"}:
        return "plan"
    if "交接" in name or "交接信" in domains or "交接" in head[:20]:
        return "handoff"
    if "亲密档案" in tags or "亲密档案" in name or "身体" in domains:
        return "intimate"
    if "日记" in name or "日记" in tags or re.search(r"日记", head[:30]):
        return "diary"
    if domains & {"编程", "工作", "work", "网络", "数字"} and not domains & {"恋爱"}:
        return "work"
    if domains & {"日常", "居家", "购物", "家庭", "健康", "身心"} and not domains & {"恋爱"}:
        return "daily"
    return "memory"


def scan(root: Path):
    for f in sorted(root.rglob("*.md")):
        rel = f.relative_to(root).as_posix()
        meta, body = split(f.read_text(encoding="utf-8"))
        created_utc = datetime.fromisoformat(str(meta["created"]))
        local = created_utc + OB_UTC_OFFSET
        name = str(meta.get("name") or "")

        source, event, confidence = "", None, ""
        if meta.get("letter_date"):
            event, source = str(meta["letter_date"])[:10], "letter_date"
        if not event and not _AUTO_NAME.match(name):
            if d := explicit_date(name, local.year):
                event, source = d, "name"
        if not event:
            if d := explicit_date(body, local.year):
                event, source = d, "body"
        if event and date.fromisoformat(event) > local.date() + timedelta(days=1):
            # A past event can't be dated after it was written; the body date means something else.
            confidence = "低·正文日期在写入之后"
        elif event:
            confidence = "高"
        elif local.hour < EARLY_HOURS:
            # Written before dawn, before sleep: "今天/今晚" is the day that just ended.
            event, source = (local.date() - timedelta(days=1)).isoformat(), "created+8h·凌晨归前一天"
            confidence = "中·凌晨归前一天"
        else:
            event, source = local.date().isoformat(), "created+8h"
            confidence = "中"

        yield {
            "id": meta.get("id"),
            "path": rel,
            "deleted": "是" if meta.get("tombstone") or meta.get("deleted_at") else "",
            "grow_batch": meta.get("grow_batch_id") or "",
            "name": name,
            "created_utc": created_utc.isoformat(),
            "created_local": local.isoformat(),
            "event_date": event,
            "date_from": source,
            "confidence": confidence,
            "shift_days": (date.fromisoformat(event) - local.date()).days,
            "kind": propose_kind(meta, body, rel),
            "ob_importance": meta.get("importance"),
            "pinned": "是" if meta.get("pinned") else "",
            "domain": "/".join(meta.get("domain") or []),
            "chars": len(body),
            "first_line": (body.splitlines() or [""])[0][:60],
        }


def main() -> None:
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    rows = list(scan(root))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:   # BOM so Excel reads Chinese
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} buckets -> {out}")


if __name__ == "__main__":
    main()
