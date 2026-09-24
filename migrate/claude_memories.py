"""Import the memory part of a claude.ai data export: the running summary and each memory file.

Each becomes a `memory` record dated by when claude.ai last updated it (Beijing time), tagged
官号记忆 and with which account it came from. The words stay as they are; only the markup that
reads badly on the page goes: the frontmatter (its description leads the text and names the
record, its aliases become tags), the [stated] markers, markdown headings, bold, links and code.

usage: python migrate/claude_memories.py <memories.json> <core.db> <label>
       python migrate/claude_memories.py --tidy <core.db>    re-tidy records already imported
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spiritual_core.store import Store  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
TAG = "官号记忆"
HEADINGS = {"Work context": "工作", "Personal context": "她这个人", "Top of mind": "最近在想",
            "Brief history": "这一路", "Recent months": "最近几个月", "Earlier context": "更早",
            "Long-term background": "长久的底色"}
HEAD_LINE = re.compile(r"\s*(?:#{1,6}\s+(.+?)|\*\*(.+?)\*\*|\*(.+?)\*)\s*")


def tidy(text: str) -> tuple[str, str, list[str]]:
    """-> (short name from the description or "", readable body, aliases)."""
    text = text.replace("\r\n", "\n").strip()
    desc, aliases = "", []
    if (fm := re.match(r"---\n(.*?)\n---\n?", text, re.S)):
        for line in fm.group(1).splitlines():
            key, _, val = line.partition(":")
            if key.strip() == "description":
                desc = val.strip()
            elif key.strip() == "aliases":
                aliases = [a.strip() for a in val.strip().strip("[]").split(",") if a.strip()]
        text = text[fm.end():].strip()
    out: list[str] = []
    for line in text.split("\n"):
        if (m := HEAD_LINE.fullmatch(line)):
            head = next(g for g in m.groups() if g)
            head = HEADINGS.get(head, head)
            line = f"· {head} ·" if m.group(3) else f"【{head}】"
            if out and out[-1] != "":
                out.append("")
        else:
            line = re.sub(r"^(\s*)[-*]\s+(?:\[\w+\]\s*)?", r"\1· ", line)   # "- [stated] x" -> "· x"
            line = re.sub(r"^\s*>\s?", "　　", line)
            line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
            line = re.sub(r"\[\[([^\]]+)\]\]", r"\1", line)
            line = line.replace("`", "")
        out.append(line)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if desc:
        body = f"{desc}\n\n{body}"
    name = re.split(r"[，,；;]|——", desc, maxsplit=1)[0].strip()[:24] if desc else ""
    return name, body, aliases


def retidy(db_path: Path) -> None:
    """Tidy memories imported before tidy() existed. Old text is kept in revisions by edit()."""
    store = Store(db_path)
    rows = store.db.execute(
        "SELECT id, title, body, tags FROM records WHERE kind='memory' AND title LIKE ?", (TAG + "%",)
    ).fetchall()
    for rid, title, body, tags in rows:
        name, new_body, aliases = tidy(body)
        head, _, rest = title.partition("：")
        new_title = f"{head}：{name}" if name and rest.startswith("/") else title
        if (new_title, new_body) == (title, body):
            continue
        store.edit(rid, title=new_title, body=new_body, by="ai")
        old = json.loads(tags or "[]")
        if (more := [a for a in aliases if a not in old]):
            with store.db:
                store.db.execute("UPDATE records SET tags=? WHERE id=?",
                                 (json.dumps(old + more, ensure_ascii=False), rid))
        print(rid, new_title)
    store.close()


def main() -> None:
    if sys.argv[1] == "--tidy":
        return retidy(Path(sys.argv[2]))
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
            made.append(store.write("memory", tidy(summary)[1], event_date=day,
                                    title=f"官号记忆{label}：总的概况", tags=[TAG, label.strip("（）")]))
        for f in files:
            if not (f.get("content") or "").strip():
                continue
            name, body, aliases = tidy(f["content"])
            made.append(store.write("memory", body, event_date=_day(f.get("updated_at") or newest),
                                    title=f"官号记忆{label}：{name or f.get('path', '')}",
                                    tags=[TAG, label.strip("（）"), *aliases]))
    store.close()
    for r in made:
        print(r.id, r.event_date, r.title)


def _day(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BEIJING).date().isoformat()


if __name__ == "__main__":
    main()
