"""Phone-facing page: upload photos and keep words. Everything lives under /<secret>/."""

from __future__ import annotations

import json
from datetime import date, timedelta
from importlib import resources

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import media, weight
from .store import MOODS, Store, StoreError

_WHO = {
    "excerpt": {"ai": "我想留的", "human": "她想留的"},
    "letter": {"ai": "我写的", "human": "她写的"},
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


def mount(mcp, store: Store, secret: str) -> None:
    p = f"/{secret}"
    page = resources.files("spiritual_core").joinpath("static/index.html").read_text("utf-8")

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
        size = int(request.headers.get("content-length") or 0)
        if size > media.MAX_UPLOAD_BYTES + 1024 * 1024:
            return _err("图片太大了")
        form = await request.form(max_part_size=media.MAX_UPLOAD_BYTES + 1)
        file = form.get("file")
        if file is None or isinstance(file, str):
            return _err("没收到图片")
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
                kept_by="human" if kind == "letter" else "",
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

    @mcp.custom_route(p + "/api/recent", methods=["GET"])
    async def recent(request: Request):
        kinds = ["image", "excerpt", "inner", *PAGE_KINDS]
        items = _group([_item(r) for r in store.recent(kinds, 100)], store)
        return JSONResponse({"items": items[:30]})

    @mcp.custom_route(p + "/api/inner", methods=["GET"])
    async def inner(request: Request):
        """His 心里话 shelf: newest first; locked ones arrive sealed."""
        return JSONResponse({"items": [_item(r) for r in store.recent(["inner"], 50)]})

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
        if request.query_params.get("size") == "thumb":
            return Response(media.render(data, media.THUMB_MAX_SIDE), media_type="image/jpeg",
                            headers=headers)
        return Response(data, media_type=info["mime"], headers=headers)


def _item(r, weighed: dict | None = None) -> dict:
    """The only way a record leaves toward the page. A locked one leaves as a sealed shell."""
    if r.locked:
        return {
            "id": r.id, "kind": r.kind, "event_date": r.event_date,
            "created_at": r.created_at, "sealed": True,
            "title": "上了锁的心里话", "body": "", "why": "", "status": "", "kept_by": "",
            "weight": 0, "hers": 0, "reads": 0,
        }
    item = {
        "id": r.id, "kind": r.kind, "event_date": r.event_date, "created_at": r.created_at,
        "title": r.title, "body": r.body, "why": r.why, "status": r.status,
        "kept_by": who_label(r),
        "weight": weight.effective(r), "hers": r.human_rating, "reads": r.read_count,
        "unlocked_at": r.unlocked_at, "batch": r.batch,
    }
    if weighed and r.id in weighed:
        item["score"], item["layer"] = weighed[r.id]
    return item


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


def _err(msg: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=400)
