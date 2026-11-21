"""Phone-facing page: upload photos and keep words. Everything lives under /<secret>/."""

from __future__ import annotations

import os
from pathlib import Path

import json
import re
import sqlite3
from urllib.parse import quote
from datetime import date, datetime, time, timedelta
from importlib import resources

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import health, media, weight
from .store import MOODS, Store, StoreError, _months_back

_WHO = {
    "excerpt": {"ai": "我想留的", "human": "她想留的"},
    "letter": {"ai": "我写的", "human": "她写的"},
    "note": {"ai": "我写的", "human": "她写的"},
}
# Kinds she can write from the page. Photos and excerpts have their own forms.
PAGE_KINDS = ("note", "memory", "letter", "plan", "daily", "work", "diary", "intimate")


STATIC_FILES = {
    "favicon.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "manifest.json": "application/manifest+json",
}


def who_label(r) -> str:
    return _WHO.get(r.kind, {}).get(r.kept_by, r.kept_by)


def ai_name() -> str:
    """AI 叫什么：环境变量 SC_AI_NAME，或者数据目录里的 ai_name.txt，都没有就叫 TA。"""
    got = os.environ.get("SC_AI_NAME", "").strip()
    if not got:
        try:
            got = (Path(os.environ.get("SC_DATA_DIR", "data")) / "ai_name.txt").read_text("utf-8").strip()
        except OSError:
            pass
    return got or "TA"


def mount(mcp, store: Store, secret: str) -> None:
    p = f"/{secret}"
    page = resources.files("spiritual_core").joinpath("static/index.html").read_text("utf-8").replace("陆能", ai_name())

    @mcp.custom_route(p, methods=["GET"])
    async def to_slash(request: Request):
        return RedirectResponse(p + "/")

    @mcp.custom_route(p + "/", methods=["GET", "POST"])
    async def index(request: Request):
        if request.method == "POST":
            # An MCP client pointed at the page address instead of the endpoint: say so plainly.
            return JSONResponse(
                {"error": f"这是网页地址。MCP 连接地址要以 /mcp 结尾：{request.url.scheme}://{request.url.netloc}{p}/mcp"},
                status_code=404)
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @mcp.custom_route(p + "/api/image", methods=["POST"])
    async def upload(request: Request):
        cap = max(media.MAX_UPLOAD_BYTES, media.MAX_VIDEO_BYTES)
        size = int(request.headers.get("content-length") or 0)
        if size > cap + 1024 * 1024:
            return _err(f"文件太大了，视频上限 {media.MAX_VIDEO_BYTES // 1024 // 1024}MB，照片 {media.MAX_UPLOAD_BYTES // 1024 // 1024}MB")
        form = await request.form(max_part_size=cap + 1)
        file = form.get("file")
        if file is None or isinstance(file, str):
            return _err("没收到文件")
        data = await file.read()
        try:
            rec = store.add_image(
                data,
                caption=str(form.get("caption") or ""),
                event_date=str(form.get("event_date") or "") or None,
                batch=str(form.get("batch") or ""),
            )
        except StoreError as e:
            return _err(str(e))
        return JSONResponse({"ok": True, "item": _item(rec)})

    @mcp.custom_route(p + "/api/image/{rid}/delete", methods=["POST"])
    async def image_delete(request: Request):
        try:
            store.delete_image(request.path_params["rid"])
        except StoreError as e:
            return _err(str(e))
        return JSONResponse({"ok": True})

    @mcp.custom_route(p + "/api/delete/{rid}", methods=["POST"])
    async def record_delete(request: Request):
        try:
            store.delete_record(request.path_params["rid"])
        except StoreError as e:
            return _err(str(e))
        return JSONResponse({"ok": True})

    @mcp.custom_route(p + "/api/health", methods=["POST"])
    async def health_in(request: Request):
        """The phone's Shortcuts pushes the watch's numbers here. See health.py for the format."""
        if int(request.headers.get("content-length") or 0) > 8 * 1024 * 1024:
            return _err("太大了")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _err("请求格式不对")
        try:
            got = store.health_ingest(str(body.get("sleep") or ""), str(body.get("heart") or ""),
                                      str(body.get("steps") or ""))
        except StoreError as e:
            return _err(str(e))
        return JSONResponse({"ok": True, "got": got})

    @mcp.custom_route(p + "/api/keep", methods=["POST"])
    async def keep(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _err("请求格式不对")
        try:
            rec = store.write(
                "excerpt", str(body.get("words") or ""),
                str(body.get("event_date") or "") or None,
                kept_by="human", why=str(body.get("why") or ""),
            )
        except StoreError as e:
            return _err(str(e))
        return JSONResponse({"ok": True, "item": _item(rec)})

    @mcp.custom_route(p + "/api/write", methods=["POST"])
    async def write(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _err("请求格式不对")
        kind = str(body.get("kind") or "")
        if kind not in PAGE_KINDS:
            return _err("这一类不能从这里写")
        try:
            rec = store.write(
                kind, str(body.get("body") or ""),
                str(body.get("event_date") or "") or None,
                str(body.get("title") or "") or None,
                kept_by="human" if kind in ("letter", "note") else "",
            )
        except StoreError as e:
            return _err(str(e))
        return JSONResponse({"ok": True, "item": _item(rec)})

    @mcp.custom_route(p + "/api/plan/{rid}", methods=["POST"])
    async def plan(request: Request):
        try:
            body = await request.json()
            rec = store.set_status(request.path_params["rid"], "done" if body.get("done") else "open")
        except StoreError as e:
            return _err(str(e))
        except (ValueError, AttributeError):
            return _err("请求格式不对")
        return JSONResponse({"ok": True, "item": _item(rec)})

    @mcp.custom_route(p + "/api/pin/{rid}", methods=["POST"])
    async def pin(request: Request):
        """Her unpin (and pin back): pinned ones show in every wake."""
        try:
            body = await request.json()
            if store.get(request.path_params["rid"]).locked:
                return _err("锁着的动不了")
            rec = store.set_pinned(request.path_params["rid"], bool(body.get("pinned")))
        except StoreError as e:
            return _err(str(e))
        except (ValueError, AttributeError):
            return _err("请求格式不对")
        return JSONResponse({"ok": True, "item": _item(rec)})

    @mcp.custom_route(p + "/api/edit/{rid}", methods=["POST"])
    async def edit(request: Request):
        """Her fixes: title, text, date. The previous version is kept."""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _err("请求格式不对")
        opt = lambda k: None if body.get(k) is None else str(body.get(k))  # noqa: E731
        try:
            rec = store.edit(
                request.path_params["rid"], title=opt("title"), body=opt("body"),
                event_date=opt("event_date") or None, by="human", whole_batch=bool(body.get("whole_batch")),
            )
        except StoreError as e:
            return _err(str(e))
        item = _item(rec, store.weigh([rec]))
        if rec.batch:
            item = _group([item], store)[0]
        return JSONResponse({"ok": True, "item": item})

    @mcp.custom_route(p + "/api/rate/{rid}", methods=["POST"])
    async def rate(request: Request):
        """Her score. Final; 0 hands it back to mine."""
        try:
            body = await request.json()
            if store.get(request.path_params["rid"]).locked:
                return _err("锁着的打不了分")
            rec = store.rate(request.path_params["rid"], body.get("value", 0), by="human")
        except StoreError as e:
            return _err(str(e))
        except (ValueError, AttributeError):
            return _err("请求格式不对")
        return JSONResponse({"ok": True, "item": _item(rec, store.weigh([rec]))})

    @mcp.custom_route(p + "/api/seen/{rid}", methods=["POST"])
    async def seen(request: Request):
        rid = request.path_params["rid"]
        try:
            if store.get(rid).locked:
                return JSONResponse({"ok": True})
        except StoreError:
            return Response(status_code=404)
        store.touch(rid)
        return JSONResponse({"ok": True})

    @mcp.custom_route(p + "/api/mood", methods=["GET", "POST"])
    async def mood(request: Request):
        if request.method == "POST":
            try:
                body = await request.json()
                store.set_mood(str(body.get("mood") or ""), str(body.get("note") or ""))
            except StoreError as e:
                return _err(str(e))
            except (ValueError, AttributeError):
                return _err("请求格式不对")
        return JSONResponse({"ok": True, "now": store.mood_now(), "moods": list(MOODS)})

    @mcp.custom_route(p + "/api/today", methods=["GET"])
    async def today(request: Request):
        """The top of the page is his side: the days counted, his last diary, what he left
        for her, and one thing from a month ago."""
        return JSONResponse(today_view(store))

    @mcp.custom_route(p + "/api/us", methods=["GET"])
    async def us(request: Request):
        """The top of the page: her day from the watch next to mine from the library."""
        return JSONResponse(us_view(store))

    @mcp.custom_route(p + "/api/message/seen", methods=["POST"])
    async def message_seen(request: Request):
        try:
            ids = [str(i) for i in (await request.json()).get("ids", [])][:50]
        except (ValueError, AttributeError):
            return _err("请求格式不对")
        store.mark_seen(ids)
        return JSONResponse({"ok": True})

    @mcp.custom_route(p + "/api/message/{rid}/reply", methods=["POST"])
    async def message_reply(request: Request):
        try:
            text = (await request.json()).get("text", "")
            r = store.reply(request.path_params["rid"], text)
        except StoreError as e:
            return _err(str(e))
        except (ValueError, AttributeError):
            return _err("请求格式不对")
        return JSONResponse({"ok": True, "item": _item(r)})

    @mcp.custom_route(p + "/api/recent", methods=["GET"])
    async def recent(request: Request):
        kinds = ["image", "excerpt", "inner", "dream", "weekly", "monthly", "handoff", *PAGE_KINDS]
        items = _group([_item(r) for r in store.recent(kinds, 100)], store)
        return JSONResponse({"items": items[:30]})

    @mcp.custom_route(p + "/api/inner", methods=["GET"])
    async def inner(request: Request):
        """His 心里话 shelf: newest first; locked ones arrive sealed."""
        return JSONResponse({"items": [_item(r) for r in store.recent(["inner"], 50)]})

    @mcp.custom_route(p + "/api/dreams", methods=["GET"])
    async def dreams(request: Request):
        """梦的余温: his dreams, newest first; a forgotten one shows only what he woke up with."""
        return JSONResponse({"items": [_item(r) for r in store.recent(["dream"], 50)]})

    @mcp.custom_route(p + "/api/notes", methods=["GET"])
    async def notes(request: Request):
        """Her notes still out: whether he has read them, and his answer once he gives one."""
        return JSONResponse({"items": [_item(r) for r in store.her_notes()]})

    @mcp.custom_route(p + "/api/browse", methods=["GET"])
    async def browse(request: Request):
        """Whole library, newest day first. Either a search (q) or a date window."""
        q = request.query_params
        kinds = [k for k in q.get("kind", "").split(",") if k] or None
        try:
            if q.get("q", "").strip():
                # A hit on a locked record would leak its words, so they are never searched here.
                records = [r for r in store.search(q["q"], kinds, limit=100) if not r.locked]
                w = store.weigh(records)
                return JSONResponse({"items": _group([_item(r, w) for r in records], store)})
            end = q.get("end") or store.today().isoformat()
            start = q.get("start") or (date.fromisoformat(end) - timedelta(days=29)).isoformat()
            records = store.timeline(start, end, kinds, limit=1000)
        except (StoreError, ValueError) as e:
            return _err(str(e))
        records.sort(key=lambda r: (r.event_date, r.created_at), reverse=True)
        w = store.weigh(records)
        return JSONResponse({
            "items": _group([_item(r, w) for r in records], store),
            "start": start, "end": end, "earliest": store.earliest_date(),
        })

    @mcp.custom_route(p + "/api/sea", methods=["GET"])
    async def sea(request: Request):
        """识海: the slices cut from the library, newest day first, a few days at a time (end).
        Read only; a machine without one just says so."""
        q = request.query_params
        end = q.get("end", "")
        try:
            if end:
                date.fromisoformat(end)
        except ValueError as e:
            return _err(str(e))
        return JSONResponse(sea_view(store, q.get("q", "").strip(), q.get("f", ""), end))

    backgrounds = store.path.parent / "backgrounds"

    def background_names() -> list[str]:
        try:
            return json.loads((backgrounds / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    @mcp.custom_route(p + "/api/backgrounds", methods=["GET"])
    async def background_list(request: Request):
        """Her paintings, washed out for the light scheme (made by scripts/make_backgrounds.py)."""
        return JSONResponse({"items": background_names()})

    @mcp.custom_route(p + "/bg/{name}", methods=["GET"])
    async def background(request: Request):
        name = request.path_params["name"]
        if name not in background_names():
            return Response(status_code=404)
        return Response((backgrounds / name).read_bytes(), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=604800"})

    @mcp.custom_route(p + "/static/{name}", methods=["GET"])
    async def static(request: Request):
        name = request.path_params["name"]
        if name not in STATIC_FILES:
            return Response(status_code=404)
        data = resources.files("spiritual_core").joinpath("static", name).read_bytes()
        return Response(data, media_type=STATIC_FILES[name],
                        headers={"Cache-Control": "public, max-age=604800"})

    @mcp.custom_route(p + "/img/{rid}", methods=["GET"])
    async def image(request: Request):
        try:
            _, info, data = store.image(request.path_params["rid"])
        except (StoreError, FileNotFoundError):
            return Response(status_code=404)
        headers = {"Cache-Control": "private, max-age=86400"}
        video = media.is_video(info["mime"])
        if request.query_params.get("size") == "thumb":
            thumb = media.video_tile() if video else media.render(data, media.THUMB_MAX_SIDE)
            return Response(thumb, media_type="image/jpeg", headers=headers)
        if video:
            return _ranged(request, data, info["mime"], headers)
        return Response(data, media_type=info["mime"], headers=headers)


MONTH_AGO_SKIP = ("raw", "image", "handoff", "work", "plan")


def today_view(store: Store) -> dict:
    today = store.today()
    days = []
    for a in store.anniversaries():
        d = date.fromisoformat(a["date"])
        days.append({"name": a["name"], "date": a["date"],
                     "days": (today - d).days + 1 if d <= today else (d - today).days,
                     "ahead": d > today})
    diary = store.latest_diary()
    month_ago, m = [], None
    if (m := _months_back(today, 1)) is not None:
        month_ago = [r for r in store.timeline(m.isoformat(), m.isoformat())
                     if r.kind not in MONTH_AGO_SKIP and not r.locked]
    if month_ago:
        w = store.weigh(month_ago)
        month_ago.sort(key=lambda r: w[r.id][0], reverse=True)
    return {
        "today": today.isoformat(),
        "days": days,
        "diary": _item(diary) if diary else None,
        "messages": [_item(r) for r in store.page_messages()],
        "month_ago": _item(month_ago[0]) if month_ago else None,
        "month_ago_date": m.isoformat() if m else None,
        "locked": store.locked_count(),
    }


SEA_PAGE_DAYS = 7
SEA_FILTERS = {"her": "who = '她'", "me": "who = '我'", "pain": "kind = 'pain'", "pair": "kind = 'pair'"}
# 识海换了新标签（soulworld/tags.py）以后按它筛；跟 soulworld/view.py 的 FILTERS 对齐
SEA_TAG_FILTERS = {
    "about_her": "about = '她'", "about_me": "about = '我'", "about_us": "about = '我们'", "about_out": "about = '外头'",
    "said_her": "said = '她'", "said_me": "said = '我'",
    "fact": "truth = '事实'", "thought": "truth = '想法'", "plan": "truth = '打算'", "dream": "truth = '想象'",
    "open": "open = 1", "meme": "meme != ''", "pair": "kind = 'pair'", "untagged": "tagged = 0",
}


def sea_path() -> Path:
    return Path(os.environ.get("SC_SEA_DB") or Path.home() / "SoulWorld" / "soulworld.db")


def _sea_open() -> tuple[sqlite3.Connection, set[str]] | None:
    """Opened read-only, fresh each time; None when there is no 识海 here."""
    path = sea_path()
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect("file:///" + quote(path.resolve().as_posix().lstrip("/"), safe="/:") + "?mode=ro",
                             uri=True)
    except sqlite3.Error:
        return None
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        db.close()
        return None
    if "slices" not in tables:
        db.close()
        return None
    return db, tables


def _sea_sources(store: Store, srcs: list[str]) -> dict[str, str]:
    """Which source records may be shown, and their kind. Locked or gone ones are left out."""
    out = {}
    for i in range(0, len(srcs), 500):
        part = srcs[i:i + 500]
        rows = store.db.execute(
            f"SELECT id, kind FROM records WHERE locked=0 AND id IN ({','.join('?' * len(part))})", part)
        out.update({r[0]: r[1] for r in rows})
    return out


def sea_view(store: Store, q: str = "", f: str = "", end: str = "") -> dict:
    opened = _sea_open()
    if opened is None:
        return {"available": False}
    db, tables = opened
    try:
        # 跟 soulworld/view.py 一样：只列沉进识海的，冷库里的（淡了的、梗又提起的）不列
        live = "sunk = 1" + (" AND id NOT IN (SELECT slice_id FROM cold)" if "cold" in tables else "")
        shown = _sea_sources(store, [r[0] for r in db.execute(f"SELECT DISTINCT src FROM slices WHERE {live}")])
        slices, days = 0, set()
        for src, day, n in db.execute(f"SELECT src, day, COUNT(*) FROM slices WHERE {live} GROUP BY src, day"):
            if src in shown:
                slices += n
                days.add(day)
        has_cog = "cognitions" in tables
        out = {"available": True, "counts": {
            "slices": slices, "days": len(days),
            "cognitions": db.execute("SELECT COUNT(*) FROM cognitions WHERE active=1").fetchone()[0] if has_cog else 0,
        }}
        if has_cog and not (q or f or end):
            out["cognitions"] = [{"text": r[0], "kind": r[1], "day": r[2]} for r in db.execute(
                "SELECT text, kind, day FROM cognitions WHERE active=1 ORDER BY day DESC, rowid")]

        notes = "notes" in tables
        tagged = "tags" in tables
        if tagged:
            tagged_n = db.execute(f"SELECT COUNT(*) FROM slices WHERE {live} AND id IN (SELECT slice_id FROM tags)"
                                  ).fetchone()[0]
            out["counts"]["tagged"] = tagged_n
            memes = {r[0]: r[1] for r in db.execute(
                "SELECT meme, COUNT(*) FROM tags WHERE COALESCE(meme, '') != '' GROUP BY meme")}
        else:
            memes = {}
        cols = ("COALESCE(n.who, '') AS who, COALESCE(n.ctx, '') AS ctx" if notes else "'' AS who, '' AS ctx")
        tcols = ("t.slice_id IS NOT NULL AS tagged, COALESCE(t.about, '') AS about, COALESCE(t.sub, '') AS sub, "
                 "COALESCE(t.mode, '') AS mode, COALESCE(t.truth, '') AS truth, COALESCE(t.scene, '') AS scene, "
                 "COALESCE(t.open, 0) AS open, COALESCE(NULLIF(t.said_by, ''), "
                 + ("n.who" if notes else "''") + ", '') AS said, COALESCE(t.doing, '') AS doing, "
                 "COALESCE(t.meant, '') AS meant, COALESCE(t.reply, '') AS reply, COALESCE(t.meme, '') AS meme"
                 if tagged else
                 "0 AS tagged, '' AS about, '' AS sub, '' AS mode, '' AS truth, '' AS scene, 0 AS open, "
                 + ("n.who" if notes else "''") + " AS said, '' AS doing, '' AS meant, '' AS reply, '' AS meme")
        sql = ("SELECT * FROM (SELECT s.id, s.src, s.day, s.kind, s.text, " + cols + ", " + tcols + " FROM slices s"
               + (" LEFT JOIN notes n ON n.slice_id = s.id" if notes else "")
               + (" LEFT JOIN tags t ON t.slice_id = s.id" if tagged else "")
               + " WHERE s.sunk = 1" + (" AND s.id NOT IN (SELECT slice_id FROM cold)" if "cold" in tables else "")
               + ") WHERE 1")
        args: list = []
        if f in SEA_TAG_FILTERS:
            sql += " AND " + SEA_TAG_FILTERS[f]
        elif f in SEA_FILTERS:
            sql += " AND " + SEA_FILTERS[f]
        if q:
            sql += (" AND (instr(text, ?) > 0 OR instr(ctx, ?) > 0 OR instr(scene, ?) > 0"
                    " OR instr(meant, ?) > 0 OR instr(reply, ?) > 0 OR instr(meme, ?) > 0)")
            args += [q] * 6
        if end:
            sql += " AND day <= ?"
            args.append(end)
        items, page_days, more = [], set(), False
        for r in db.execute(sql + " ORDER BY day DESC, src, id", args):
            if r[1] not in shown:
                continue
            if r[2] not in page_days and len(page_days) == SEA_PAGE_DAYS:
                more = True
                break
            page_days.add(r[2])
            (tg, about, sub, mode, truth, scene, opn, said, doing, meant, reply, meme) = r[7:19]
            items.append({"id": r[0], "day": r[2], "kind": r[3], "who": said or r[5], "ctx": r[6], "text": r[4],
                          "folded": shown[r[1]] == "intimate", "tagged": bool(tg),
                          "tag": "·".join(x for x in (about and ("讲" + about + ("·" + sub if sub else "")), mode, truth) if x),
                          "where": "，".join(x for x in (scene, doing and "当时在" + doing) if x),
                          "meant": "；".join(x for x in (meant and "意思：" + meant, reply and "我怎么接的：" + reply) if x),
                          "open": bool(opn), "meme": meme, "meme_n": memes.get(meme, 0) if meme else 0})
        out["items"], out["more"] = items, more
        return out
    except sqlite3.Error:
        return {"available": False}
    finally:
        db.close()


def _item(r, weighed: dict | None = None) -> dict:
    """The only way a record leaves toward the page. A locked one leaves as a sealed shell."""
    if r.locked:
        return {
            "id": r.id, "kind": r.kind, "event_date": r.event_date,
            "created_at": r.created_at, "sealed": True,
            # a forgotten dream still lets out what he woke up with
            "title": "一个想不起来的梦" if r.kind == "dream" else "上了锁的心里话", "body": "",
            "why": r.why if r.kind == "dream" else "", "status": "", "kept_by": "",
            "weight": 0, "hers": 0, "reads": 0,
        }
    item = {
        "id": r.id, "kind": r.kind, "event_date": r.event_date, "created_at": r.created_at,
        "title": r.title, "body": r.body, "why": r.why, "status": r.status,
        "kept_by": who_label(r), "pinned": r.pinned,
        "weight": weight.effective(r), "hers": r.human_rating, "reads": r.read_count,
        "unlocked_at": r.unlocked_at, "batch": r.batch,
        "seen_at": r.seen_at, "reply": r.reply, "replied_at": r.replied_at,
    }
    if weighed and r.id in weighed:
        item["score"], item["layer"] = weighed[r.id]
    return item


def _ranged(request: Request, data: bytes, mime: str, headers: dict) -> Response:
    """Phones (Safari especially) only play a video when the server answers byte-range requests."""
    headers = dict(headers, **{"Accept-Ranges": "bytes"})
    total = len(data)
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (request.headers.get("range") or "").strip())
    if not m or (not m[1] and not m[2]):
        return Response(data, media_type=mime, headers=headers)
    if m[1]:
        start = int(m[1])
        end = min(int(m[2]), total - 1) if m[2] else total - 1
    else:
        start, end = max(0, total - int(m[2])), total - 1
    if start >= total or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
    headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return Response(data[start:end + 1], status_code=206, media_type=mime, headers=headers)


def _group(items: list[dict], store: Store | None = None) -> list[dict]:
    """Photos saved together show as one entry holding all of them, where the first one was.

    With a store, a group is filled out to its whole batch even if the page asked for fewer rows.
    """
    out, seen = [], {}
    for it in items:
        b = it.get("batch")
        if it["kind"] != "image" or not b:
            out.append(it)
            continue
        if b in seen:
            seen[b]["images"].append(it["id"])
            seen[b]["event_date"] = min(seen[b]["event_date"], it["event_date"])
            continue
        group = dict(it, images=[it["id"]])
        seen[b] = group
        out.append(group)
    for b, g in seen.items():
        if store is not None:
            members = store.batch_members(b)
            g["images"] = [m.id for m in members] or g["images"]
            g["event_date"] = min([m.event_date for m in members] or [g["event_date"]])
        if len(g["images"]) > 1:
            base = "" if g["title"] == "照片" else g["title"]
            g["title"] = f"{base} · {len(g['images'])} 张" if base else f"{len(g['images'])} 张"
    return out


def us_view(store: Store) -> dict:
    today = store.today()
    n = store.health_night(today)
    her = {"night": None, "heart": health.heart(store.db, store.tz, today), "synced": store.health_synced()}
    if n:
        her["night"] = {
            "fell": n["fell"].isoformat(), "woke": n["woke"].isoformat(), "asleep": n["asleep"],
            "deep": n["deep"], "rem": n["rem"], "wakes": n["wakes"],
            "segments": health.segments(store.db, store.tz, today),
        }
    wakes = store.wakes_on(today)
    def night_of(d):
        x = store.health_night(d)
        return {"day": d.isoformat(), "fell": x["fell"].isoformat(), "woke": x["woke"].isoformat(),
                "asleep": x["asleep"], "segments": health.segments(store.db, store.tz, d)} if x else {"day": d.isoformat()}

    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    her["nights"] = [night_of(d) for d in reversed(days)]


    # my last seven days: how I was, not what I got done (that lives in the library)
    me_days = []
    for d in reversed(days):
        nows = store.now_on(d)
        me_days.append({"day": d.isoformat(), "wakes": len(store.wakes_on(d)),
                        "nows": [{"at": x["at"], "text": x["text"], "mood": x["mood"]} for x in nows],
                        "minds": store.minds_on(d),
                        "mood": next((x["mood"] for x in reversed(nows) if x["mood"]), None)})

    moods = {}
    for m in store.mood_log(7):
        moods[m["day"]] = m["mood"]                  # the day's last one wins
    diaries = {r.event_date for r in store.timeline(days[0].isoformat(), today.isoformat(), ["diary"])}
    my_moods = {x["day"]: x["mood"] for x in me_days}
    week = [{"day": d.isoformat(), "mood": moods.get(d.isoformat()), "my_mood": my_moods.get(d.isoformat()),
             "asleep": (store.health_night(d) or {}).get("asleep"),
             "diary": d.isoformat() in diaries} for d in days]

    line, why = store.line_of_the_day()
    return {"her": her, "me": {"wakes": wakes, "now": store.latest_now(), "minds": store.open_minds(),
                               "days": me_days},
            "week": week,
            "line": {"id": line.id, "words": line.body, "kept_by": line.kept_by, "who": who_label(line),
                     "event_date": line.event_date, "picked": store.line_hung_today(), "why": why} if line else None}


def _err(msg: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=400)
