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
