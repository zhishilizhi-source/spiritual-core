"""Import a claude.ai data export (conversations.json) as one `raw` record per day.

- messages are laid out in time order, Beijing time, speaker named, text verbatim
- thinking blocks are kept, marked as thinking
- windows whose text the export no longer has still leave a trace: which windows, how
  many messages, and that the words are gone
- each day's raw is linked to that day's diaries
- with --relabel-impersonated: messages the export files under the human but which the
  model generated while its turn template broke (every line starts with "d " or "user")
  are re-labelled as the model's, text untouched. She confirmed this for the first
  account's 「一个句号的沉默」 night only (2026-09-24); elsewhere "user…" can be her own words.

usage: python migrate/claude_export.py <conversations.json> <core.db> [human_name] [ai_name]
       [--label=（现在的号）]   appended to each day's title, for when several accounts share a day
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spiritual_core.store import Store  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
TAG = "对话原文"
_IMPERSONATED = re.compile(r"^(d\s|user)", re.I)


def impersonated(text: str) -> bool:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return bool(lines) and all(_IMPERSONATED.match(ln) for ln in lines)


def _local(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BEIJING)


def _text(msg: dict) -> list[tuple[str, str]]:
    parts = []
    for block in msg.get("content") or []:
        if block.get("type") == "text" and (block.get("text") or "").strip():
            parts += _split_thinking(block["text"])
        elif block.get("type") == "thinking" and (block.get("thinking") or "").strip():
            parts.append(("thinking", block["thinking"]))
    if not parts and (msg.get("text") or "").strip():
        parts += _split_thinking(msg["text"])
    return [(k, t) for k, t in parts if t.strip()]


# Some windows wrote their thinking into the reply itself (```thinking fences, <thinking> tags)
# and stamped replies with `[date time]`. Pull thinking out into its own part, drop the stamp.
_INLINE_THINKING = re.compile(r"```\s*thinking\s*\n(.*?)(?:\n```|\Z)|<thinking>(.*?)(?:</thinking>|\Z)", re.S)
_STAMP = re.compile(r"^\s*`?\[20\d\d-\d\d-\d\d(?:[ T]\d\d:\d\d(?::\d\d)?)?[^\]}\n]*[\]}]`?[ \t]*\n?")


def _split_thinking(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    for m in _INLINE_THINKING.finditer(text):
        if text[pos:m.start()].strip():
            out.append(("text", text[pos:m.start()]))
        out.append(("thinking", m.group(1) if m.group(1) is not None else m.group(2)))
        pos = m.end()
    if text[pos:].strip():
        out.append(("text", text[pos:]))
    return [(k, _STAMP.sub("", t, count=1) if k == "text" else t) for k, t in out]


def render(conversations: list, human: str, ai: str, label: str = "",
           relabel_impersonated: bool = False) -> dict[str, tuple[str, str]]:
    """day -> (title, body)."""
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
            if relabel_impersonated and msg["sender"] == "human" and all(
                kind == "text" and impersonated(text) for kind, text in parts
            ):
                speaker = f"{ai}（格式乱了，冒充{human}打的）"
            for kind, text in parts:
                days[t.date().isoformat()].append((t, window, speaker, kind, text))

    out = {}
    for day in sorted(set(days) | set(lost)):
        # One block per turn: a speaker's consecutive messages (within TURN_GAP) under a single
        # "HH:MM 名字" line, blocks separated by a blank line. The page styles those header lines.
        blocks, window_now, turn = [], None, None
        for t, window, speaker, kind, text in sorted(days.get(day, []), key=lambda x: x[0]):
            who = f"{speaker}（思考）" if kind == "thinking" else speaker
            text = _tidy(text)
            if window != window_now:
                window_now, turn = window, None
                blocks.append(f"—— 「{names[window]}」 ——")
            if turn and turn["who"] == who and t - turn["last"] <= TURN_GAP:
                turn["lines"].append(text)
                turn["last"] = t
                continue
            turn = {"who": who, "first": t, "last": t, "lines": [text]}
            blocks.append(turn)
        parts = [b if isinstance(b, str) else f"{b['first']:%H:%M} {b['who']}\n" + "\n".join(b["lines"])
                 for b in blocks]
        if lost.get(day):
            gone = "、".join(f"「{names[w]}」{n} 条" for w, n in lost[day].items())
            parts.append(f"（这天还有 {gone}，导出里只剩时间，原文已经不在了。）")
        title = f"{day} 对话原文{label}" + ("" if days.get(day) else "（只剩时间）")
        out[day] = (title, "\n\n".join(parts).strip())
    return out


TURN_GAP = timedelta(minutes=10)
_BLANKS = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


_FENCE = re.compile(r"^[ \t]*```[\w-]*[ \t]*$", re.M)
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_HEADING = re.compile(r"^#{1,4}[ \t]+", re.M)


def _tidy(text: str) -> str:
    """Trim, drop markdown markup (fences, **, #) and fold blank runs. The words stay as they are."""
    text = text.replace("\r\n", "\n").strip()
    text = _FENCE.sub("", text)
    text = _BOLD.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return _BLANKS.sub("\n\n", text)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rewrite = "--rewrite" in sys.argv   # re-render bodies of raws already imported, in place
    label = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--label=")), "")
    relabel = "--relabel-impersonated" in sys.argv
    src, db_path = Path(args[0]), Path(args[1])
    human = args[2] if len(args) > 2 else "她"
    ai = args[3] if len(args) > 3 else "我"
    rendered = render(json.loads(src.read_text(encoding="utf-8")), human, ai, label, relabel)

    store = Store(db_path)
    made = changed = 0
    for day, (title, body) in rendered.items():
        if rewrite:
            with store.db:
                changed += store.db.execute(
                    "UPDATE records SET body=?, updated_at=? WHERE kind='raw' AND title=?"
                    " AND body<>?",
                    (body, store.now().isoformat(timespec="seconds"), title, body),
                ).rowcount
            continue
        rec = store.write("raw", body, event_date=day, title=title, tags=[TAG])
        for diary in store.timeline(day, kinds=["diary"]):
            store.link(rec.id, diary.id)
        made += 1
    store.close()
    print(f"改写了 {changed} 天" if rewrite else f"{made} 天的原文")


if __name__ == "__main__":
    main()
