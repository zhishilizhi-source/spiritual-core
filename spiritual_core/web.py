"""Phone-facing page: upload photos and keep words. Everything lives under /<secret>/."""

from __future__ import annotations

from datetime import date, timedelta
from importlib import resources

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import media, weight
from .store import Store, StoreError

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

    @mcp.custom_route(p + "/", methods=["GET"])
    async def index(request: Request):
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

    @mcp.custom_route(p + "/api/rate/{rid}", methods=["POST"])
    async def rate(request: Request):
        """Her score. Final; 0 hands it back to mine."""
        try:
            body = await request.json()
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
            store.get(rid)
        except StoreError:
            return Response(status_code=404)
        store.touch(rid)
        return JSONResponse({"ok": True})

    @mcp.custom_route(p + "/api/recent", methods=["GET"])
    async def recent(request: Request):
        kinds = ["image", "excerpt", *PAGE_KINDS]
        return JSONResponse({"items": [_item(r) for r in store.recent(kinds, 30)]})

    @mcp.custom_route(p + "/api/browse", methods=["GET"])
    async def browse(request: Request):
        """Whole library, newest day first. Either a search (q) or a date window."""
        q = request.query_params
        kinds = [k for k in q.get("kind", "").split(",") if k] or None
        try:
            if q.get("q", "").strip():
                records = store.search(q["q"], kinds, limit=100)
                w = store.weigh(records)
                return JSONResponse({"items": [_item(r, w) for r in records]})
            end = q.get("end") or store.today().isoformat()
            start = q.get("start") or (date.fromisoformat(end) - timedelta(days=29)).isoformat()
            records = store.timeline(start, end, kinds, limit=1000)
        except (StoreError, ValueError) as e:
            return _err(str(e))
        records.sort(key=lambda r: (r.event_date, r.created_at), reverse=True)
        w = store.weigh(records)
        return JSONResponse({
            "items": [_item(r, w) for r in records],
            "start": start, "end": end, "earliest": store.earliest_date(),
        })

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
    item = {
        "id": r.id, "kind": r.kind, "event_date": r.event_date, "created_at": r.created_at,
        "title": r.title, "body": r.body, "why": r.why, "status": r.status,
        "kept_by": who_label(r),
        "weight": weight.effective(r), "hers": r.human_rating, "reads": r.read_count,
    }
    if weighed and r.id in weighed:
        item["score"], item["layer"] = weighed[r.id]
    return item


def _err(msg: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=400)
