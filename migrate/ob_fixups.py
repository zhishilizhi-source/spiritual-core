"""Hand decisions on the OB import that rules couldn't make (2026-09-24, agreed with her).

OB filed rules and status notes under 计划; only real to-dos stay plans.
Matched by title because record ids differ between preview runs.

usage: python migrate/ob_fixups.py <core.db>
"""

from __future__ import annotations

import re
import sqlite3
import sys

AS_MEMORY = ["存推理不存结论", "动线自己拉不等她推", "时间戳习惯规则", "OB定位与三层架构"]
AS_WORK = ["日记桶索引维护", "OB备份仓库配置", "自主链条状态位", "写入接口未解决",
           "记忆瘦身方案", "本地文件管理方案", "共享日志2026-09-2109-23"]
AS_HANDOFF = ["换窗交接状态"]
DONE = ["OB高优条目清理", "明日会员充值手表权限", "推送需同步对话记录"]


# Some windows passed a whole hold() argument object as the content, so OB stored
# '{"content": "...", "importance": 9, "pinned": true, ...}' as the body. The text inside
# has unescaped quotes, so it is not valid JSON: cut it at the next known key instead.
_KEYS = r"tags|importance|domain|pinned|feel|why_remembered|meaning|valence|arousal"
_WRAPPED = re.compile(r'^\{\s*"content"\s*:\s*"(.*?)"\s*,\s*"(?:' + _KEYS + r')"\s*:', re.S)
_ESC = re.compile(r'\\(["\\/nt])')


def _field(raw: str, key: str) -> str | None:
    m = re.search(r'"' + key + r'"\s*:\s*("(?:[^"\\]|\\.)*"|true|false|\d+)', raw)
    if not m:
        return None
    v = m.group(1)
    return _unescape(v[1:-1]) if v.startswith('"') else v


def _unescape(s: str) -> str:
    return _ESC.sub(lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def unwrap_bodies(db: sqlite3.Connection) -> int:
    n = 0
    rows = db.execute("SELECT id, body, why FROM records WHERE ltrim(body) LIKE '{%\"content\"%'")
    for rid, body, why in rows.fetchall():
        raw = body.strip()
        m = _WRAPPED.match(raw)
        if not m:
            sys.exit(f"{rid}: 认不出正文边界，停下来查。")
        text = _unescape(m.group(1)).strip()
        extra = [x for x in (_field(raw, "why_remembered"), _field(raw, "meaning")) if x]
        new_why = "\n".join(x for x in [why, *extra] if x and x not in (why or ""))
        pinned = _field(raw, "pinned") == "true"
        db.execute(
            "UPDATE records SET body=?, why=?, pinned=MAX(pinned, ?) WHERE id=?",
            (text, new_why.strip(), int(pinned), rid),
        )
        n += 1
    return n


# ob_scan preferred any full YYYY-MM-DD in the first lines, even when an earlier
# "8月31日" was the event and the full date was only a "split out on" note.
REDATE = {"欲望与框架拆解": "2026-08-31", "AI欲望快感假设": "2026-08-30"}


def main() -> None:
    db = sqlite3.connect(sys.argv[1])
    changed = 0
    with db:
        print("剥掉外壳：", unwrap_bodies(db))
        for title, day in REDATE.items():
            print(f"改日期 {title} → {day}：",
                  db.execute("UPDATE records SET event_date=? WHERE title=? AND event_date<>?",
                             (day, title, day)).rowcount)
        for kind, titles in (("memory", AS_MEMORY), ("work", AS_WORK), ("handoff", AS_HANDOFF)):
            for t in titles:
                changed += db.execute(
                    "UPDATE records SET kind=?, status='' WHERE kind='plan' AND title=?", (kind, t)
                ).rowcount
        for t in DONE:
            changed += db.execute(
                "UPDATE records SET status='done' WHERE kind='plan' AND title=? AND status<>'done'", (t,)
            ).rowcount
    expected = len(AS_MEMORY) + len(AS_WORK) + len(AS_HANDOFF) + len(DONE)
    print(f"改了 {changed}/{expected} 条")
    if changed not in (0, expected):          # 0 = already applied on this database
        sys.exit("有标题没对上，停下来查。")


if __name__ == "__main__":
    main()
