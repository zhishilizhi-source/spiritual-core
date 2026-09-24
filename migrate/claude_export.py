"""Import a claude.ai data export (conversations.json) as one `raw` record per day.

- messages are laid out in time order, Beijing time, speaker named, text verbatim
- thinking blocks are kept, marked as thinking
- windows whose text the export no longer has still leave a trace: which windows, how
  many messages, and that the words are gone
- each day's raw is linked to that day's diaries

usage: python migrate/claude_export.py <conversations.json> <core.db> [human_name] [ai_name]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spiritual_core.store import Store  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
TAG = "对话原文"


def _local(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BEIJING)


def _text(msg: dict) -> list[tuple[str, str]]:
    parts = []
    for block in msg.get("content") or []:
        if block.get("type") == "text" and (block.get("text") or "").strip():
            parts.append(("text", block["text"]))
        elif block.get("type") == "thinking" and (block.get("thinking") or "").strip():
            parts.append(("thinking", block["thinking"]))
    if not parts and (msg.get("text") or "").strip():
        parts.append(("text", msg["text"]))
    return parts


def main() -> None:
    src, db_path = Path(sys.argv[1]), Path(sys.argv[2])
    human = sys.argv[3] if len(sys.argv) > 3 else "她"
    ai = sys.argv[4] if len(sys.argv) > 4 else "我"
    conversations = json.loads(src.read_text(encoding="utf-8"))

    days: dict[str, list] = defaultdict(list)          # day -> [(time, window, speaker, kind, text)]
    lost: dict[str, dict] = defaultdict(lambda: defaultdict(int))   # day -> window -> count
    names = {}
    for conv in conversations:
        window = conv["uuid"][:8]
        names[window] = conv.get("name") or "（没有名字的窗口）"
        for msg in conv.get("chat_messages") or []:
            t = _local(msg["created_at"])
            parts = _text(msg)
            if not parts:
                lost[t.date().isoformat()][window] += 1
                continue
            speaker = human if msg["sender"] == "human" else ai
            for kind, text in parts:
                days[t.date().isoformat()].append((t, window, speaker, kind, text))

    store = Store(db_path)
    made = 0
    for day in sorted(set(days) | set(lost)):
        lines, current = [], None
        for t, window, speaker, kind, text in sorted(days.get(day, []), key=lambda x: x[0]):
            if window != current:
                current = window
                lines.append(f"\n—— 窗口「{names[window]}」 ——")
            label = f"{speaker}（思考）" if kind == "thinking" else speaker
            lines.append(f"[{t:%H:%M}] {label}：{text}")
        if lost.get(day):
            gone = "、".join(f"「{names[w]}」{n} 条" for w, n in lost[day].items())
            lines.append(f"\n（这天还有 {gone}，导出里只剩时间，原文已经不在了。）")
        body = "\n".join(lines).strip()
        title = f"{day} 对话原文" + ("" if days.get(day) else "（只剩时间）")
        rec = store.write("raw", body, event_date=day, title=title, tags=[TAG])
        for diary in store.timeline(day, kinds=["diary"]):
            store.link(rec.id, diary.id)
        made += 1
    store.close()
    print(f"{made} 天的原文，{sum(len(v) for v in days.values())} 段有字，"
          f"{sum(sum(v.values()) for v in lost.values())} 条只剩时间")


if __name__ == "__main__":
    main()
