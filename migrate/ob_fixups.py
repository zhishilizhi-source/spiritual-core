"""Hand decisions on the OB import that rules couldn't make (2026-09-24, agreed with her).

OB filed rules and status notes under 计划; only real to-dos stay plans.
Matched by title because record ids differ between preview runs.

usage: python migrate/ob_fixups.py <core.db>
"""

from __future__ import annotations

import sqlite3
import sys

AS_MEMORY = ["存推理不存结论", "动线自己拉不等她推", "时间戳习惯规则", "OB定位与三层架构"]
AS_WORK = ["日记桶索引维护", "OB备份仓库配置", "自主链条状态位", "写入接口未解决",
           "记忆瘦身方案", "本地文件管理方案", "共享日志2026-09-2109-23"]
AS_HANDOFF = ["换窗交接状态"]
DONE = ["OB高优条目清理", "明日会员充值手表权限", "推送需同步对话记录"]


def main() -> None:
    db = sqlite3.connect(sys.argv[1])
    changed = 0
    with db:
        for kind, titles in (("memory", AS_MEMORY), ("work", AS_WORK), ("handoff", AS_HANDOFF)):
            for t in titles:
                changed += db.execute(
                    "UPDATE records SET kind=?, status='' WHERE kind='plan' AND title=?", (kind, t)
                ).rowcount
        for t in DONE:
            changed += db.execute(
                "UPDATE records SET status='done' WHERE kind='plan' AND title=?", (t,)
            ).rowcount
    expected = len(AS_MEMORY) + len(AS_WORK) + len(AS_HANDOFF) + len(DONE)
    print(f"改了 {changed}/{expected} 条")
    if changed != expected:
        sys.exit("有标题没对上，停下来查。")


if __name__ == "__main__":
    main()
