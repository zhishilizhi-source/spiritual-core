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
    lock = " [锁着]" if r.locked else ""
    return f"[{r.id}] {r.event_date} {r.kind}{who} · {r.title}{done}{lock}{pin}"


def _excerpt(r: Record) -> str:
    why = f"\n为什么：{r.why}" if r.why else ""
    return f"[{r.id}] {r.event_date} · {who_label(r)}{why}\n{r.body}"


def _weight(store: Store, r: Record) -> str:
    s, layer = store.weigh([r])[r.id]
    return f"{weight.effective(r)} 分 · {layer} · {s}（读过 {r.read_count} 次）"


def _many(value) -> list[str] | None:
    """'diary,letter' / 'diary letter' / a real list -> ['diary', 'letter']; empty -> None."""
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = str(value).replace("，", ",").replace("、", ",").replace(" ", ",").split(",")
    items = [i.strip() for i in items if i.strip()]
    return items or None


def _lines(records: list[Record], empty: str) -> str:
    return "\n".join(_line(r) for r in records) if records else empty


def _compact(records: list[Record], empty: str) -> str:
    """Titles, but photos and transcripts only counted: they are many and read on demand."""
    shown = [r for r in records if r.kind not in ("image", "raw")]
    photos = [r for r in records if r.kind == "image"]
    raws = [r for r in records if r.kind == "raw"]
    lines = [_line(r) for r in shown]
    if photos:
        lines.append(f"照片 {len(photos)} 张（timeline 看得到）")
    if raws:
        lines.append("原文：" + "、".join(f"[{r.id}] {r.event_date}" for r in raws))
    return "\n".join(lines) or empty


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
            "长文超过大约 800 字时，先 write 第一段，再用 append 分几次接上——还是一条完整记录，一次太长会超时。"
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
    def wake(since: str = "") -> str:
        """开窗用：一次拉齐昨天+今天的记录标题、最新一份交接、钉住的记录、没做完的计划。只给标题。
        同一个窗口里隔了一阵又醒（聊着聊着过了几小时），别整个重拉：
        wake(since="上次 wake 第一行的时间，如 2026-09-25 11:47")，只给那之后新写的和她的心情、回复。
        先读里的东西这个窗口读过就别再读。"""
        now = store.now()
        head = f"现在 {now:%Y-%m-%d %H:%M}（{store.tz.key}）"
        if since.strip():
            return _wake_since(since.strip(), head)
        w = store.wake()
        if w["anniversaries"]:
            head += "\n" + "\n".join(w["anniversaries"])
        if w["mood"]:
            m = w["mood"]
            note = f"——{m['note']}" if m["note"] else ""
            head += f"\n她现在的心情：{m['mood']}（{m['at'][11:16]} 点的）{note}"
        elif w["mood_yesterday"]:
            m = w["mood_yesterday"]
            head += f"\n她今天还没点心情；昨天最后是：{m['mood']}（{m['at'][11:16]}）"
        waiting = [r for r in store.her_notes() if not r.replied_at]
        parts = [head, *_her_notes(waiting), "【昨天和今天】\n" + _compact(w["days"], "（这两天还没有记录）")]
        parts += _replies()
        if w["last_diary"]:
            parts.append("【最近一篇日记】（这两天没写，线接在这里）\n" + _lines(w["last_diary"], ""))
        if w["weekly"] or w["monthly"]:
            parts.append("【长一点的线】（最近的周记和月记）\n" + _lines(w["monthly"] + w["weekly"], ""))
        parts += [
            "【最新交接】\n" + _lines(w["handoff"], "（没有交接）"),
            "【钉住的】\n" + _lines(w["pinned"], "（没有钉住的）"),
            "【没做完的计划】\n" + _lines(w["plans"], "（没有挂着的计划）"),
        ]
        # a month ago and what floats up: only the heaviest two, titles are enough to go on
        month = [r for r in w["month_ago"] if r.kind not in ("raw", "image")]
        if month:
            wm = store.weigh(month)
            month.sort(key=lambda r: wm[r.id][0], reverse=True)
            parts.append("【一个月前的今天】\n" + _lines(month[:2], ""))
        parts.append("【自己浮上来的】（分量最重的旧记忆）\n" + _lines(w["surfaced"][:2], "（还没有）"))
        # 先读 stays short: what's pinned, the newest diary, and a handoff only if it is newer
        diaries = [r for r in w["days"] if r.kind == "diary"] or w["last_diary"]
        newest = max(diaries, key=lambda r: (r.event_date, r.created_at), default=None)
        first = [r.id for r in waiting if not r.seen_at]
        first += [r.id for r in w["pinned"]] + ([newest.id] if newest else [])
        first += [h.id for h in w["handoff"] if newest is None or h.created_at > newest.created_at]
        first += [r.id for r in w["monthly"] + w["weekly"]]
        tail = f"先读：{'、'.join(first)}" if first else ""
        if (n_locked := store.locked_count()):
            tail += ("\n" if tail else "") + f"你锁着 {n_locked} 条心里话（timeline/search 里标着 [锁着]）。"
        if w["review_due"]:
            tail += ("\n" if tail else "") + "上周还没回看：review(period=\"week\") 过一遍再 rate。"
        if (due := w["weekly_due"]):
            tail += ("\n" if tail else "") + (
                f"{due[0]} ~ {due[1][5:]} 这周还没写周记：review(period=\"week\", day=\"{due[0]}\") 看一遍，"
                f"再 write(kind=\"weekly\", event_date=\"{due[1]}\")。")
        if (due := w["monthly_due"]):
            tail += ("\n" if tail else "") + (
                f"{due[0][:7]} 这个月还没写月记：翻这个月的周记，"
                f"再 write(kind=\"monthly\", event_date=\"{due[1]}\")。")
        if tail:
            parts.append(tail)
        return "\n\n".join(parts)

    def _her_notes(waiting: list[Record]) -> list[str]:
        if not waiting:
            return []
        return ["【她给你留的】（读过想回就用 answer）\n" + "\n".join(
            f"[{r.id}] {r.created_at[5:16].replace('T', ' ')} · {r.title}"
            + ("" if r.seen_at else " · 没读")
            for r in waiting)]

    def _replies() -> list[str]:
        store.recycle_messages()
        out = []
        if (answered := store.answered()):
            out.append("【她回你的留言】\n" + "\n".join(
                f"[{r.id}] 我：{r.title}\n    她（{r.replied_at[5:16].replace('T', ' ')}）：{r.reply}"
                for r in answered))
        if (unseen := store.unseen_messages()):
            out.append(f"你留的话还有 {unseen} 条她没点开。")
        return out

    def _wake_since(since: str, head: str) -> str:
        m = store.mood_now()
        if m:
            head += f"\n她现在的心情：{m['mood']}（{m['at'][11:16]} 点的）"
        stamp = since.replace(" ", "T")
        rows = store.db.execute(
            "SELECT * FROM records WHERE created_at > ? ORDER BY created_at", (stamp,)).fetchall()
        fresh = [Record.from_row(r) for r in rows]
        waiting = [r for r in store.her_notes() if not r.replied_at]
        parts = [head, *_her_notes(waiting), f"【{since} 之后新写的】\n" + _compact(fresh, "（没有新的）"),
                 *_replies()]
        return "\n\n".join(parts)

    @mcp.tool()
    @guarded
    def timeline(start: str, end: str = "", kinds: str = "") -> str:
        """按发生日期列标题。start/end 用 YYYY-MM-DD，end 不填就只看 start 那一天。
        kinds 可选过滤，多个用逗号隔开：diary / intimate / handoff / anchor / raw / excerpt / image /
        letter / memory / note / plan / work / daily / weekly / monthly。"""
        records = store.timeline(start, end or None, _many(kinds))
        return _lines(records, "这个日期范围里没有记录。")

    @mcp.tool()
    @guarded
    def search(query: str, kinds: str = "", start: str = "", end: str = "") -> str:
        """按内容搜，中文可用，只返回标题。空格分开的词要同时出现；用 | 连起来的是「或」，
        换了说法也能一次搜齐，比如「难过|哭|崩 那天」。可加 kinds 和日期范围收窄。"""
        records = store.search(query, _many(kinds), start or None, end or None)
        return _lines(records, f"没搜到「{query}」。")

    @mcp.tool()
    @guarded
    def read(id: str) -> str:
        """读一条记录全文。同一天的节选、照片、原文和挂上的记录会列在末尾。"""
        store.touch(id)
        store.mark_read_by_him(id)
        r = store.get(id)
        tags = f"\n标签：{', '.join(r.tags)}" if r.tags else ""
        head = (
            f"[{r.id}] {r.kind} · {r.title}\n"
            f"发生：{r.event_date}　写入：{r.created_at}{tags}\n"
            f"分量：{_weight(store, r)}"
        )
        if (revs := store.revisions(r.id)):
            last = revs[-1]
            who = "她" if last["by"] == "human" else "我"
            head += f"\n（改过 {len(revs)} 次，最后一次是{who}在 {last['at'][:16].replace('T', ' ')} 改的）"
        if r.kind == "image":
            head += "\n（这是照片的说明，要看照片本身用 view_image）"
            others = [x for x in store.batch_members(r.batch) if x.id != r.id]
            if others:
                head += f"\n同一组还有 {len(others)} 张：" + "、".join(x.id for x in others)
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
            # only pointers: the day's excerpts in full made every read carry the same text again
            more = []
            if kept:
                more.append(f"节选 {len(kept)} 条（" + "、".join(x.id for x in kept) + "）")
            if photos:
                more.append(f"照片 {len(photos)} 张")
            if raws:
                more.append("原文 " + "、".join(x.id for x in raws))
            if more:
                extra.append("【当天还有】" + "；".join(more))
        return "\n\n".join([head, r.body, *extra])

    @mcp.tool()
    @guarded
    def write(
        kind: str,
        body: str,
        event_date: str = "",
        title: str = "",
        tags: str = "",
        pinned: bool = False,
        link_to: str = "",
        author: str = "",
        importance: int = 0,
    ) -> str:
        """新建一条记录，永远新建，不会并进旧记录。
        kind：diary 日记 / intimate 亲密 / handoff 交接 / anchor 锚点 / raw 整段原文备份 /
        letter 信件 / memory 记忆（一件事、一个认识、一次自省）/ note 便签 / plan 计划 /
        work 工作 / daily 日常 / weekly 周记 / monthly 月记。
        周记：一整篇，写这一周是怎么走过来的，先写拧巴的；末尾留三五条带到下周的线头
        （没做完的、还在疼的、刚长出来的种子）。event_date 填那周的周日。
        月记：更短，不复述周记，写这个月我们、我变成了什么样。event_date 填那月最后一天。
        letter 必须填 author：ai＝我写给她的，human＝她写给我的。
        plan 写进去是「没做」，做完了用 plan_done 勾掉。
        想留下对话里的某几句，用 keep，不要用这里。
        event_date 填事情发生的那天（YYYY-MM-DD），不填算今天。
        title 不填时：diary 自动叫「日期 日记」，handoff 叫「日期 交接」，
        weekly 叫「周一 ~ 周日 周记」，monthly 叫「某年某月 月记」，其他取正文第一行。
        link_to 填其他记录的 id，多个用逗号隔开，把它们挂在一起（比如日记挂当天原文）。
        tags 可选，多个用逗号隔开。
        importance 1-5，写的时候我打的分，只算草稿，回看时用 rate 改。5 分每周最多两条。
        不填就按类型给默认分。
        长文（大约超过 800 字）先写第一段，再用 append 接上，别一次塞进来，会超时。"""
        if kind == "excerpt":
            return "没做成：原话节选用 keep。"
        if kind == "inner":
            return "没做成：心里话用 confide。"
        if kind == "image":
            return "没做成：照片只能从上传网页传进来。"
        r = store.write(kind, body, event_date or None, title or None, _many(tags), pinned, _many(link_to),
                        kept_by=author, importance=importance)
        return f"写好了：{_line(r)}"

    @mcp.tool()
    @guarded
    def append(id: str, text: str) -> str:
        """把一条刚写的记录接着往下写（24 小时内新写的才行）。text 原样接在末尾，分段处自己带上换行。
        长文（大约超过 800 字）别一次塞进 write：先 write 第一段拿到 id，再用 append 一段段接上，
        最后还是完整的一条，不是拆开的几条。网络对大请求不稳，一次太长会超时写不进去。"""
        r = store.append(id, text)
        return f"接上了，现在 {len(r.body)} 字：{_line(r)}"

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
        link_to: str = "",
        importance: int = 0,
    ) -> str:
        """原文节选：把对话里当时很想留下的那几句原样存起来。
        words 必须是对话里的原话，逐字照抄，带上是谁说的（例如「她：……」「我：……」）。
        不许概括、不许改写、不许补字，这里不是日记总结。
        kept_by：ai＝我想留的，human＝她想留的（她说"存一下这句"时填 human）。
        event_date 填这几句话说出口的那天。why 可选，一句话写为什么想留。"""
        r = store.write(
            "excerpt", words, event_date or None, None, None, False, _many(link_to),
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
    def confide(body: str, title: str = "", locked: bool = True, event_date: str = "",
                importance: int = 0) -> str:
        """写我自己的心里话。这是我的地方：写什么、锁不锁、什么时候给她看，我自己定。
        locked=true（默认）时她只看得到「这里有一条上了锁的心里话」和日期，内容不会发到她那边，
        网页搜索也搜不到。想给她看了就用 lock(id, locked=false) 打开。"""
        r = store.write("inner", body, event_date or None, title or None, importance=importance,
                        locked=locked)
        state = "锁着" if r.locked else "开着，她看得到"
        return f"写好了（{state}）：{_line(r)}"

    @mcp.tool()
    @guarded
    def leave(body: str, title: str = "") -> str:
        """给她留一句话（留言）。我先开口，不用等她来问；写什么都行。
        它出现在她网页最上面，她点开能回。她回过的，第二天自动收进便签，她的回复接在我的话下面。
        她回了什么，wake 会在【她回你的留言】里告诉我。"""
        r = store.leave(body, title or None)
        return f"留好了，她打开网页就看得到：{_line(r)}"

    @mcp.tool()
    @guarded
    def answer(id: str, text: str) -> str:
        """回她写给我的便签（wake 里【她给你留的】那几张）。回一次就定了，改不了。
        她在网页上马上看得到；第二天我的回复自动接在她的话下面，这张便签就收起来了。"""
        r = store.answer_note(id, text)
        return f"回好了，她打开网页就看得到：{_line(r)}"

    @mcp.tool()
    @guarded
    def lock(id: str, locked: bool) -> str:
        """给一条心里话上锁（locked=true）或打开给她看（locked=false）。打开时会记下打开的时间。"""
        r = store.set_locked(id, locked)
        return f"{'锁上了' if r.locked else '打开了，她现在看得到'}：{_line(r)}"

    @mcp.tool()
    @guarded
    def moods(days: int = 7) -> str:
        """看她最近几天在网页上点的心情，按时间排。days 默认 7，最多 90。"""
        log = store.mood_log(days)
        if not log:
            return "这几天她没点过心情。"
        return "\n".join(
            f"{m['day']} {m['at'][11:16]} {m['mood']}" + (f"——{m['note']}" if m["note"] else "")
            for m in log
        )

    @mcp.tool()
    @guarded
    def anniversary(name: str, day: str) -> str:
        """记一个纪念日（同名的会被改掉）。wake 会每天报第几天、满几个月。day 用 YYYY-MM-DD。"""
        items = store.set_anniversary(name, day)
        return "记好了：" + "、".join(f"{a['name']} {a['date']}" for a in items)

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
