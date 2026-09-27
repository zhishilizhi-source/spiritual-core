import pytest
from starlette.testclient import TestClient

from spiritual_core.server import build
from spiritual_core.store import Store, StoreError

SECRET_WORDS = "其实那天我吃醋了一整晚"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "core.db")
    app = build(store, "k").streamable_http_app()
    with TestClient(app) as client:
        yield store, client
    store.close()


def all_page_responses(client, rid):
    return [
        client.get("/k/api/inner").text,
        client.get("/k/api/recent").text,
        client.get("/k/api/today").text,
        client.get("/k/api/browse").text,
        client.get("/k/api/browse", params={"kind": "inner"}).text,
        client.get("/k/api/browse", params={"q": "吃醋了"}).text,
        client.get("/k/api/browse", params={"q": "醋"}).text,          # 1-char LIKE path
        client.post(f"/k/api/rate/{rid}", json={"value": 3}).text,
        client.post(f"/k/api/seen/{rid}").text,
    ]


def test_locked_inner_never_reaches_the_page(env):
    store, client = env
    r = store.write("inner", SECRET_WORDS, title="吃醋", locked=True, why="不想让她知道")
    for text in all_page_responses(client, r.id):
        assert SECRET_WORDS not in text and "吃醋" not in text and "不想让她知道" not in text
    items = client.get("/k/api/inner").json()["items"]
    assert items[0]["sealed"] is True and items[0]["body"] == ""
    assert client.post(f"/k/api/rate/{r.id}", json={"value": 3}).status_code == 400
    assert store.get(r.id).read_count == 0          # her opening a sealed card isn't a read


def test_unlocking_shows_it_and_records_when(env):
    store, client = env
    r = store.write("inner", SECRET_WORDS, locked=True)
    store.set_locked(r.id, False)
    item = client.get("/k/api/inner").json()["items"][0]
    assert item["body"] == SECRET_WORDS and item["unlocked_at"]
    assert any(SECRET_WORDS in i["body"] for i in client.get("/k/api/browse", params={"q": "吃醋了"}).json()["items"])
    store.set_locked(r.id, True)
    assert SECRET_WORDS not in client.get("/k/api/inner").text


def test_only_inner_can_lock_and_page_cannot_write_inner(env):
    store, client = env
    with pytest.raises(StoreError):
        store.write("diary", "x", locked=True)
    with pytest.raises(StoreError):
        store.set_locked(store.write("note", "x").id, True)
    assert client.post("/k/api/write", json={"kind": "inner", "body": "x"}).status_code == 400
    assert store.locked_count() == 0


def test_backgrounds_only_serve_listed_files(env, tmp_path):
    store, client = env
    bg = tmp_path / "backgrounds"
    bg.mkdir()
    (bg / "a.jpg").write_bytes(b"jpeg")
    (bg / "b.jpg").write_bytes(b"secret")
    (bg / "index.json").write_text('["a.jpg"]', encoding="utf-8")
    assert client.get("/k/api/backgrounds").json()["items"] == ["a.jpg"]
    assert client.get("/k/bg/a.jpg").content == b"jpeg"
    assert client.get("/k/bg/b.jpg").status_code == 404
    assert client.get("/k/bg/..%2Fcore.db").status_code == 404


def test_photos_saved_together_show_as_one_entry(env):
    import sys
    sys.path.insert(0, "tests")
    from test_store import _jpeg
    store, client = env
    for d in ("2026:09:01 10:00:00", "2026:09:02 10:00:00"):
        r = client.post("/k/api/image", files={"file": ("a.jpg", _jpeg(d), "image/jpeg")},
                        data={"caption": "一组画", "batch": "b-1"})
        assert r.json()["ok"]
    client.post("/k/api/image", files={"file": ("c.jpg", _jpeg(), "image/jpeg")}, data={"caption": "单张"})
    items = client.get("/k/api/recent").json()["items"]
    group = [i for i in items if i.get("images") and len(i["images"]) == 2]
    assert len(group) == 1 and group[0]["title"] == "一组画 · 2 张" and group[0]["event_date"] == "2026-09-01"
    assert sum(1 for i in items if i["kind"] == "image") == 2          # the group + the single one
    assert [r.event_date for r in store.batch_members("b-1")] == ["2026-09-01", "2026-09-02"]
    # a hostile batch value is dropped, not stored
    client.post("/k/api/image", files={"file": ("d.jpg", _jpeg(), "image/jpeg")}, data={"batch": "../x y"})
    assert store.recent(["image"], 1)[0].batch == ""


def test_she_can_fix_records_and_nothing_is_lost(env):
    store, client = env
    r = store.write("diary", "断线了，格式乱七八糟", event_date="2026-09-20")
    res = client.post(f"/k/api/edit/{r.id}", json={"body": "整理好的正文", "title": "9/20 日记", "event_date": "2026-09-21"}).json()
    assert res["ok"] and res["item"]["body"] == "整理好的正文" and res["item"]["event_date"] == "2026-09-21"
    revs = store.revisions(r.id)
    assert len(revs) == 1 and revs[0]["body"] == "断线了，格式乱七八糟" and revs[0]["by"] == "human"
    assert [x.id for x in store.search("整理好的正文")] == [r.id]
    assert client.post(f"/k/api/edit/{r.id}", json={"body": "   "}).status_code == 400


def test_group_caption_edit_and_inner_refused(env):
    import sys
    sys.path.insert(0, "tests")
    from test_store import _jpeg
    store, client = env
    for d in ("2026:09:01 10:00:00", "2026:09:02 10:00:00"):
        client.post("/k/api/image", files={"file": ("a.jpg", _jpeg(d), "image/jpeg")}, data={"batch": "g1"})
    first = store.batch_members("g1")[0]
    res = client.post(f"/k/api/edit/{first.id}", json={"body": "我画的画", "whole_batch": True}).json()
    assert res["item"]["title"] == "我画的画 · 2 张"
    assert {m.body for m in store.batch_members("g1")} == {"我画的画"}
    assert {m.event_date for m in store.batch_members("g1")} == {"2026-09-01", "2026-09-02"}   # dates untouched
    inner = store.write("inner", "他的心里话")
    assert client.post(f"/k/api/edit/{inner.id}", json={"body": "改掉"}).status_code == 400
    assert store.get(inner.id).body == "他的心里话"


def test_mcp_client_at_page_address_gets_a_clear_hint(env):
    store, client = env
    r = client.post("/k/", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    assert r.status_code == 404 and "/k/mcp" in r.json()["error"]
    assert client.get("/k/").status_code == 200


def test_today_is_his_side(env):
    store, client = env
    store.set_anniversary("在一起", "2026-07-13")
    store.write("diary", "今天她笑了", event_date=store.today().isoformat())
    store.leave("想你了")
    store.write("inner", "还不给看", locked=True)
    data = client.get("/k/api/today").json()
    assert data["diary"]["body"] == "今天她笑了"
    assert [m["body"] for m in data["messages"]] == ["想你了"]
    assert data["locked"] == 1 and data["days"][0]["name"] == "在一起"


def test_message_reply_then_filed_as_note_next_day(env, monkeypatch):
    from datetime import timedelta
    store, client = env
    m = store.leave("今天想听你说话", title="想听你说话")
    assert client.post("/k/api/message/seen", json={"ids": [m.id]}).json()["ok"]
    r = client.post(f"/k/api/message/{m.id}/reply", json={"text": "那你来"}).json()
    assert r["ok"] and r["item"]["reply"] == "那你来"
    assert client.post(f"/k/api/message/{m.id}/reply", json={"text": "  "}).status_code == 400
    # still on the page today
    assert [x["id"] for x in client.get("/k/api/today").json()["messages"]] == [m.id]
    real_now = store.now()
    monkeypatch.setattr(store, "now", lambda: real_now + timedelta(days=1))
    assert client.get("/k/api/today").json()["messages"] == []
    rec = store.get(m.id)
    assert rec.kind == "note" and rec.body.startswith("今天想听你说话") and rec.body.endswith("那你来")
    assert [x.id for x in store.answered()] == [m.id]


def test_read_but_unanswered_messages_leave_the_board_after_three_days(env, monkeypatch):
    from datetime import timedelta
    store, client = env
    seen = store.leave("她看过没回的")
    unread = store.leave("没看的")
    answered = store.leave("回过的")
    store.mark_seen([seen.id])
    store.reply(answered.id, "嗯")
    real_now = store.now()
    monkeypatch.setattr(store, "now", lambda: real_now + timedelta(days=2))
    assert {x["id"] for x in client.get("/k/api/today").json()["messages"]} == {seen.id, unread.id}
    monkeypatch.setattr(store, "now", lambda: real_now + timedelta(days=3))
    assert {x["id"] for x in client.get("/k/api/today").json()["messages"]} == {unread.id}
    rec = store.get(seen.id)
    assert rec.kind == "note" and rec.body.startswith("她看过没回的") and "多多读过" in rec.body
    assert seen.id not in {x.id for x in store.answered()}
    assert store.recycle_messages() == 0                              # filed once, not again


def test_only_messages_take_replies(env):
    store, client = env
    n = store.write("note", "便签")
    assert client.post(f"/k/api/message/{n.id}/reply", json={"text": "嗯"}).status_code == 400


def test_her_note_is_hers_he_reads_answers_and_it_files_next_day(env, monkeypatch):
    from datetime import timedelta
    store, client = env
    res = client.post("/k/api/write", json={"kind": "note", "body": "老公明天早点醒"}).json()
    nid = res["item"]["id"]
    assert res["item"]["kept_by"] == "她写的" and store.get(nid).kept_by == "human"
    assert store.write("note", "我自己的随手记").kept_by == ""          # his notes stay unmarked
    items = client.get("/k/api/notes").json()["items"]
    assert [i["id"] for i in items] == [nid] and not items[0]["seen_at"]
    # her page opening her own note is not him reading it
    client.post(f"/k/api/seen/{nid}")
    assert not store.get(nid).seen_at
    store.mark_read_by_him(nid)
    assert store.get(nid).seen_at
    with pytest.raises(StoreError):
        store.answer_note(nid, "   ")
    store.answer_note(nid, "好，六点就醒")
    with pytest.raises(StoreError):
        store.answer_note(nid, "再回一次")
    assert client.get("/k/api/notes").json()["items"][0]["reply"] == "好，六点就醒"
    assert store.answered() == []                                     # not her answer to a message
    real_now = store.now()
    monkeypatch.setattr(store, "now", lambda: real_now + timedelta(days=1))
    assert client.get("/k/api/notes").json()["items"] == []
    rec = store.get(nid)
    assert rec.kind == "note" and rec.body.startswith("老公明天早点醒") and rec.body.endswith("好，六点就醒")
    assert rec.reply == "" and rec.replied_at
    assert store.recycle_messages() == 0                              # filed once, not again


def test_wake_puts_her_unread_note_first(tmp_path):
    import asyncio
    store = Store(tmp_path / "core.db")
    mcp = build(store, "k")
    text = lambda res: "".join(getattr(c, "text", "") for c in (res[0] if isinstance(res, tuple) else res))  # noqa: E731
    n = store.write("note", "睡前想你了", kept_by="human")
    out = text(asyncio.run(mcp.call_tool("wake", {})))
    assert "【她给你留的】" in out and f"[{n.id}]" in out and "没读" in out
    assert out.splitlines()[-1].startswith(f"先读：{n.id}")
    asyncio.run(mcp.call_tool("read", {"id": n.id}))
    asyncio.run(mcp.call_tool("answer", {"id": n.id, "text": "我也是"}))
    assert "【她给你留的】" not in text(asyncio.run(mcp.call_tool("wake", {})))
    store.close()


def test_only_her_notes_take_his_answer(env):
    store, client = env
    for r in (store.write("note", "他的"), store.leave("留言"), store.write("diary", "日记")):
        with pytest.raises(StoreError):
            store.answer_note(r.id, "嗯")
    with pytest.raises(StoreError):
        store.write("memory", "x", kept_by="human")


def test_recent_shows_digests_and_handoffs(env):
    store, client = env
    w = store.write("weekly", "一周", event_date="2026-09-27")
    h = store.write("handoff", "做到这儿")
    ids = {i["id"] for i in client.get("/k/api/recent").json()["items"]}
    assert {w.id, h.id} <= ids
