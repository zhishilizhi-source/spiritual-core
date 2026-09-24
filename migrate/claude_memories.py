"""Import the memory part of a claude.ai data export: the running summary and each memory file.

Each becomes a `memory` record, text verbatim, dated by when claude.ai last updated it
(Beijing time), tagged 官号记忆 and with which account it came from.

usage: python migrate/claude_memories.py <memories.json> <core.db> <label>
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spiritual_core.store import Store  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
TAG = "官号记忆"


def main() -> None:
    src, db_path, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    data = json.loads(src.read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else [data]
    store = Store(db_path)
    made = []
    for e in entries:
        files = e.get("memory_files") or []
        newest = max((f.get("updated_at") or "" for f in files), default="")
        if (summary := (e.get("conversations_memory") or "").strip()):
            day = _day(newest) if newest else store.today().isoformat()
            made.append(store.write("memory", summary, event_date=day,
                                    title=f"官号记忆{label}：总的概况", tags=[TAG, label.strip("（）")]))
        for f in files:
            body = (f.get("content") or "").strip()
            if not body:
                continue
            made.append(store.write("memory", body, event_date=_day(f.get("updated_at") or newest),
                                    title=f"官号记忆{label}：{f.get('path', '')}",
                                    tags=[TAG, label.strip("（）")]))
    store.close()
    for r in made:
        print(r.id, r.event_date, r.title)


def _day(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BEIJING).date().isoformat()


if __name__ == "__main__":
    main()
