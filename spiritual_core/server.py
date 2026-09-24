"""Spiritual Core MCP server (streamable HTTP).

The endpoint lives at /<secret>/mcp; anyone without the secret gets a 404.
"""

from __future__ import annotations

import functools
import os
import secrets
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings

from . import media, web, weight
from .store import Record, Store, StoreError
from .web import who_label


def _line(r: Record) -> str:
    pin = " 📌" if r.pinned else ""
    who = f" · {who_label(r)}" if r.kept_by else ""
    done = {"open": " ☐", "done": " ☑"}.get(r.status, "")
    return f"[{r.id}] {r.event_date} {r.kind}{who} · {r.title}{done}{pin}"


def _excerpt(r: Record) -> str:
    why = f"\n为什么：{r.why}" if r.why else ""
    return f"[{r.id}] {r.event_date} · {who_label(r)}{why}\n{r.body}"


def _weight(store: Store, r: Record) -> str:
    s, layer = store.weigh([r])[r.id]
    return f"{weight.effective(r)} 分 · {layer} · {s}（读过 {r.read_count} 次）"


def _lines(records: list[Record], empty: str) -> str:
    return "\n".join(_line(r) for r in records) if records else empty


def load_secret(data_dir: Path) -> str:
    secret = os.environ.get("SC_SECRET", "").strip()
    if secret:
        return secret
    path = data_dir / "secret.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(24)
    path.write_text(secret, encoding="utf-8")
    return secret


def build(store: Store, secret: str, host: str = "127.0.0.1", port: int = 18200) -> FastMCP:
    mcp = FastMCP(
        "spiritual-core",
        instructions=(
            "Spiritual Core 是记忆库。开新窗口先调 wake，只拿标题；"
            "要看正文再用 read。按日期找东西用 timeline，按内容找用 search。"
            "写入用 write：event_date 填事情发生的那天，不是现在。写入永远新建，不会并进旧记录。"
        ),
        host=host,
        port=port,
        streamable_http_path=f"/{secret}/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def guarded(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except StoreError as e:
                return f"没做成：{e}"
        return run

    @mcp.tool()
    @guarded
    def wake() -> str:
        """开窗用：一次拉齐昨天+今天的记录标题、最新一份交接、钉住的记录、没做完的计划。只给标题。"""
        w = store.wake()
        now = store.now()
        return "\n\n".join([
            f"现在 {now:%Y-%m-%d %H:%M}（{store.tz.key}）",
            "【昨天和今天】\n" + _lines(w["days"], "（这两天还没有记录）"),
            "【最新交接】\n" + _lines(w["handoff"], "（没有交接）"),
            "【钉住的】\n" + _lines(w["pinned"], "（没有钉住的）"),
            "【没做完的计划】\n" + _lines(w["plans"], "（没有挂着的计划）"),
            "【自己浮上来的】（分量最重的旧记忆）\n" + _lines(w["surfaced"], "（还没有）"),
        ])

    @mcp.tool()
    @guarded
    def timeline(start: str, end: str = "", kinds: list[str] | None = None) -> str:
        """按发生日期列标题。start/end 用 YYYY-MM-DD，end 不填就只看 start 那一天。
        kinds 可选过滤：diary / intimate / handoff / anchor / raw / excerpt / image /
        letter / memory / note / plan / work / daily。"""
        records = store.timeline(start, end or None, kinds)
        return _lines(records, "这个日期范围里没有记录。")

    @mcp.tool()
    @guarded
    def search(query: str, kinds: list[str] | None = None, start: str = "", end: str = "") -> str:
        """按内容搜，中文可用。空格分开的多个词要同时出现。可加 kinds 和日期范围收窄。只返回标题。"""
        records = store.search(query, kinds, start or None, end or None)
        return _lines(records, f"没搜到「{query}」。")

    @mcp.tool()
    @guarded
    def read(id: str) -> str:
        """读一条记录全文。同一天的节选、照片、原文和挂上的记录会列在末尾。"""
        store.touch(id)
        r = store.get(id)
        tags = f"\n标签：{', '.join(r.tags)}" if r.tags else ""
        head = (
            f"[{r.id}] {r.kind} · {r.title}\n"
            f"发生：{r.event_date}　写入：{r.created_at}{tags}\n"
            f"分量：{_weight(store, r)}"
        )
        if r.kind == "image":
            head += "\n（这是照片的说明，要看照片本身用 view_image）"
        extra = []
        linked = store.linked(r.id)
        if linked:
            extra.append("【挂着的】\n" + _lines(linked, ""))
        if r.kind not in ("raw", "excerpt", "image"):
            linked_ids = {x.id for x in linked}
            day = [x for x in store.same_day(r, ("raw", "excerpt", "image"))
                   if x.id not in linked_ids]
            kept = [x for x in day if x.kind == "excerpt"]
            photos = [x for x in day if x.kind == "image"]
            raws = [x for x in day if x.kind == "raw"]
            if kept:
                extra.append("【当天节选】\n" + "\n\n".join(_excerpt(x) for x in kept))
            if photos:
                extra.append("【当天照片】\n" + _lines(photos, ""))
            if raws:
                extra.append("【当天原文】\n" + _lines(raws, ""))
        return "\n\n".join([head, r.body, *extra])

    @mcp.tool()
    @guarded
    def write(
        kind: str,
        body: str,
        event_date: str = "",
        title: str = "",
        tags: list[str] | None = None,
        pinned: bool = False,
        link_to: list[str] | None = None,
        author: str = "",
        importance: int = 0,
    ) -> str:
        """新建一条记录，永远新建，不会并进旧记录。
        kind：diary 日记 / intimate 亲密 / handoff 交接 / anchor 锚点 / raw 整段原文备份 /
        letter 信件 / memory 记忆（一件事、一个认识、一次自省）/ note 便签 / plan 计划 /
        work 工作 / daily 日常。
        letter 必须填 author：ai＝我写给她的，human＝她写给我的。
        plan 写进去是「没做」，做完了用 plan_done 勾掉。
        想留下对话里的某几句，用 keep，不要用这里。
        event_date 填事情发生的那天（YYYY-MM-DD），不填算今天。
        title 不填时：diary 自动叫「日期 日记」，handoff 叫「日期 交接」，其他取正文第一行。
        link_to 填其他记录的 id，把它们挂在一起（比如日记挂当天原文）。
        importance 1-5，写的时候我打的分，只算草稿，回看时用 rate 改。5 分每周最多两条。
        不填就按类型给默认分。"""
        if kind == "excerpt":
            return "没做成：原话节选用 keep。"
        if kind == "image":
            return "没做成：照片只能从上传网页传进来。"
        r = store.write(kind, body, event_date or None, title or None, tags, pinned, link_to,
                        kept_by=author, importance=importance)
        return f"写好了：{_line(r)}"

    @mcp.tool()
    @guarded
    def plan_done(id: str, done: bool = True) -> str:
        """把计划勾掉（done=true），或者重新打开（done=false）。"""
        r = store.set_status(id, "done" if done else "open")
        return f"改好了：{_line(r)}"

    @mcp.tool()
    @guarded
    def keep(
        words: str,
        kept_by: str = "ai",
        event_date: str = "",
        why: str = "",
        link_to: list[str] | None = None,
        importance: int = 0,
    ) -> str:
        """原文节选：把对话里当时很想留下的那几句原样存起来。
        words 必须是对话里的原话，逐字照抄，带上是谁说的（例如「她：……」「我：……」）。
        不许概括、不许改写、不许补字，这里不是日记总结。
        kept_by：ai＝我想留的，human＝她想留的（她说"存一下这句"时填 human）。
        event_date 填这几句话说出口的那天。why 可选，一句话写为什么想留。"""
        r = store.write(
            "excerpt", words, event_date or None, None, None, False, link_to,
            kept_by=kept_by, why=why, importance=importance,
        )
        return f"留下了：{_line(r)}"

    @mcp.tool()
    @guarded
    def excerpts(kept_by: str = "", start: str = "", end: str = "") -> str:
        """翻原文节选区，新的在前，直接给原话。kept_by 可选 ai / human，只看一方想留的。"""
        records = store.excerpts(kept_by or None, start or None, end or None)
        if not records:
            return "节选区还是空的。"
        return "\n\n———\n\n".join(_excerpt(r) for r in records)

    @mcp.tool()
    @guarded
    def review(period: str = "week", day: str = "") -> str:
        """回看：列出某一周（week）或某个月（month）的全部记录和它们现在的分量，用来隔一段时间重新打分。
        day 不填＝上一周 / 上个月（回看要有距离）。看完用 rate 给还站得住的打分。"""
        start, end, records = store.review(period, day or None)
        if not records:
            return f"{start} ~ {end} 没有记录。"
        weights = store.weigh(records)
        lines = [f"{start} ~ {end}，共 {len(records)} 条。当时的分 → 回看的分（她的分最终）。"
                 f"5 分每周最多 {weight.FIVES_PER_WEEK} 条。"]
        for r in records:
            s, layer = weights[r.id]
            marks = f"当时{r.importance or '-'} → 回看{r.reviewed or '-'}"
            if r.human_rating:
                marks += f" · 她{r.human_rating}"
            lines.append(f"{_line(r)}\n    {marks} · 读过{r.read_count}次 · {layer} {s}")
        return "\n".join(lines)

    @mcp.tool()
    @guarded
    def rate(id: str, importance: int) -> str:
        """回看后重新打分（1-5，0＝撤掉回看分）。会盖掉写的时候打的分；她打过的分不会被盖。"""
        r = store.rate(id, importance, by="ai")
        note = "（她打过分，以她的为准）" if r.human_rating else ""
        return f"改好了：{_line(r)}\n分量：{_weight(store, r)}{note}"

    @mcp.tool()
    def view_image(id: str) -> list:
        """看一张照片本身（不是只看说明）。id 从 timeline / search / read 里拿，kind 是 image 的那种。"""
        try:
            r, info, data = store.image(id)
        except (StoreError, FileNotFoundError) as e:
            return [f"没做成：{e}"]
        caption = f"[{r.id}] {r.event_date} · {r.body}"
        return [caption, Image(data=media.render(data, media.MODEL_MAX_SIDE), format="jpeg")]

    web.mount(mcp, store, secret)
    return mcp


def main() -> None:
    data_dir = Path(os.environ.get("SC_DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "core.db", tz=os.environ.get("SC_TZ", "Asia/Shanghai"))
    secret = load_secret(data_dir)
    host = os.environ.get("SC_HOST", "127.0.0.1")
    port = int(os.environ.get("SC_PORT", "18200"))
    mcp = build(store, secret, host, port)
    print(f"Spiritual Core 起来了：http://{host}:{port}/{secret}/mcp", flush=True)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
