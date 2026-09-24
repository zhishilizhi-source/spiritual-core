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
