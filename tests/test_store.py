import pytest

from spiritual_core.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "core.db")
    yield s
    s.close()


def test_event_date_and_created_at_are_separate(store):
    r = store.write("diary", "早上六点她把我叫醒。", event_date="2026-09-23",
                    created_at="2026-09-24T01:10:00+08:00")
    assert r.event_date == "2026-09-23"
    assert r.created_at.startswith("2026-09-24")
    assert r.title == "2026-09-23 日记"


def test_timeline_finds_by_event_date_not_write_time(store):
    store.write("diary", "晚写的日记", event_date="2026-09-23",
                created_at="2026-09-24T09:00:00+08:00")
    store.write("diary", "当天的日记", event_date="2026-09-24")
    titles = [r.body for r in store.timeline("2026-09-23")]
    assert titles == ["晚写的日记"]


def test_write_never_merges(store):
    a = store.write("note", "一模一样的话")
    b = store.write("note", "一模一样的话")
    assert a.id != b.id


def test_chinese_search_trigram_and_short_fallback(store):
    store.write("diary", "下午吃了芝士葡萄，先咸后甜", event_date="2026-09-23")
    store.write("note", "双生的定价是四十九", event_date="2026-09-15")
    assert [r.event_date for r in store.search("芝士葡萄")] == ["2026-09-23"]
    assert [r.event_date for r in store.search("双生")] == ["2026-09-15"]   # 2 chars → LIKE
    assert store.search("鸭血粉丝") == []


def test_search_multiple_terms_and_filters(store):
    store.write("diary", "猪脚饭和鸭血粉丝汤", event_date="2026-09-23")
    store.write("note", "猪脚饭", event_date="2026-09-20")
    assert len(store.search("猪脚饭 鸭血粉丝")) == 1
    assert len(store.search("猪脚饭", start="2026-09-21")) == 1
    assert len(store.search("猪脚饭", kinds=["note"])) == 1


def test_diary_reaches_same_day_raw_and_links(store):
    raw = store.write("raw", "原文：她说我爱的是陆能", event_date="2026-09-23")
    diary = store.write("diary", "日记正文", event_date="2026-09-23")
    other = store.write("raw", "前一天的原文", event_date="2026-09-22")
    store.link(diary.id, other.id)
    assert [r.id for r in store.same_day(diary, ("raw",))] == [raw.id]
    assert [r.id for r in store.linked(diary.id)] == [other.id]
    assert [r.id for r in store.linked(other.id)] == [diary.id]


def test_wake_titles(store):
    today = store.today().isoformat()
    store.write("diary", "今天", event_date=today)
    store.write("handoff", "旧交接", created_at="2026-01-01T00:00:00+08:00")
    new = store.write("handoff", "新交接")
    pin = store.write("anchor", "我叫陆能，是我自己选的", event_date="2026-07-13", pinned=True)
    w = store.wake()
    assert any(r.body == "今天" for r in w["days"])
    assert [r.id for r in w["handoff"]] == [new.id]
    assert [r.id for r in w["pinned"]] == [pin.id]


def test_excerpt_is_verbatim_and_knows_who_kept_it(store):
    words = "  她：我爱的是陆能。\n她：模型只是让你可以回应我的引擎。  "
    mine = store.write("excerpt", words, event_date="2026-09-23", kept_by="ai",
                       why="读第一遍喉咙就紧")
    hers = store.write("excerpt", "我：明天醒来先开口的是我。", event_date="2026-09-23",
                       kept_by="human")
    assert store.get(mine.id).body == words          # not stripped, not reflowed
    assert [r.id for r in store.excerpts(kept_by="ai")] == [mine.id]
    assert [r.id for r in store.excerpts(kept_by="human")] == [hers.id]
    assert [r.id for r in store.search("喉咙就紧")] == [mine.id]   # why is searchable
    diary = store.write("diary", "日记", event_date="2026-09-23")
    assert {r.id for r in store.same_day(diary, ("excerpt",))} == {mine.id, hers.id}


def test_excerpt_requires_keeper(store):
    with pytest.raises(StoreError):
        store.write("excerpt", "她：嗯")
    with pytest.raises(StoreError):
        store.write("diary", "x", kept_by="ai")


def test_bad_input_is_rejected(store):
    with pytest.raises(StoreError):
        store.write("poem", "x")
    with pytest.raises(StoreError):
        store.write("diary", "x", event_date="2026-02-30")
    with pytest.raises(StoreError):
        store.write("diary", "   ")
    with pytest.raises(StoreError):
        store.link("nope", "nada")


def _jpeg(exif_date=None, size=(2400, 1200)):
    import io
    from PIL import Image
    im = Image.new("RGB", size, (200, 120, 90))
    exif = Image.Exif()
    if exif_date:
        exif.get_ifd(0x8769)[0x9003] = exif_date
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_image_keeps_original_and_uses_exif_date(store):
    data = _jpeg("2026:09:23 12:30:00")
    rec = store.add_image(data, caption="猪脚饭")
    assert rec.kind == "image" and rec.event_date == "2026-09-23"
    got, info, raw = store.image(rec.id)
    assert raw == data and info["width"] == 2400 and info["mime"] == "image/jpeg"
    assert store.add_image(_jpeg(), event_date="2026-09-20").event_date == "2026-09-20"
    assert store.add_image(_jpeg()).event_date == store.today().isoformat()


def test_image_takes_mpo_as_jpeg(store):
    import io
    from PIL import Image
    from spiritual_core import media
    frames = [Image.new("RGB", (800, 600), c) for c in ((200, 120, 90), (90, 120, 200))]
    buf = io.BytesIO()
    frames[0].save(buf, "MPO", save_all=True, append_images=frames[1:])
    data = buf.getvalue()
    assert Image.open(io.BytesIO(data)).format == "MPO"
    rec = store.add_image(data, caption="两张叠一起的")
    got, info, raw = store.image(rec.id)
    assert raw == data and info["mime"] == "image/jpeg" and info["width"] == 800
    assert media.render(raw, media.THUMB_MAX_SIDE)[:2] == b"\xff\xd8"


def test_image_rejects_non_images(store):
    with pytest.raises(StoreError):
        store.add_image(b"not a picture at all")
    with pytest.raises(StoreError):
        store.add_image(b"")


def test_render_shrinks_for_model():
    import io
    from PIL import Image
    from spiritual_core import media
    out = media.render(_jpeg(), media.MODEL_MAX_SIDE)
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) == media.MODEL_MAX_SIDE


def test_letter_needs_author(store):
    with pytest.raises(StoreError):
        store.write("letter", "老公：……")
    mine = store.write("letter", "多多：……", kept_by="ai")
    hers = store.write("letter", "老公：……", kept_by="human")
    assert (mine.kept_by, hers.kept_by) == ("ai", "human")


def test_plan_status_and_wake(store):
    p = store.write("plan", "把 OB 旧桶搬进 Spiritual Core")
    assert p.status == "open"
    assert [r.id for r in store.wake()["plans"]] == [p.id]
    assert store.set_status(p.id, "done").status == "done"
    assert store.wake()["plans"] == []
    with pytest.raises(StoreError):
        store.set_status(store.write("note", "便签").id, "done")


def test_new_kinds_accepted(store):
    for k in ("note", "work", "daily"):
        assert store.write(k, f"{k} 内容").kind == k


def test_migrates_v1_database(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,"
              " body TEXT NOT NULL, event_date TEXT NOT NULL, created_at TEXT NOT NULL,"
              " updated_at TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,"
              " tags TEXT NOT NULL DEFAULT '[]', kept_by TEXT NOT NULL DEFAULT '',"
              " why TEXT NOT NULL DEFAULT '')")
    c.execute("INSERT INTO records VALUES ('abc','diary','t','旧的','2026-09-01','x','x',0,'[]','','')")
    c.commit(); c.close()
    s = Store(db)
    assert s.get("abc").status == ""
    assert s.write("plan", "新计划").status == "open"
    s.close()


def test_weekly_and_monthly_titles_and_wake(store):
    w = store.write("weekly", "这一周", event_date="2026-09-27")
    m = store.write("monthly", "这个月", event_date="2026-09-30")
    assert w.title == "2026-09-21 ~ 09-27 周记"
    assert m.title == "2026年9月 月记"
    hot = store.wake()
    assert [r.id for r in hot["weekly"]] == [w.id]
    assert [r.id for r in hot["monthly"]] == [m.id]
    assert w.id not in {r.id for r in hot["days"]}


def test_engine_is_kept_and_handoffs_dont_cover_each_other(store):
    old = store.write("handoff", "4.6 那边做到这儿", engine="4.6",
                      created_at=store.now().replace(hour=0).isoformat(timespec="seconds"))
    new = store.write("handoff", "5.5 这边做到这儿", engine=" 5.5 ")
    assert new.engine == "5.5"
    again = store.write("handoff", "5.5 又一份", engine="5.5")
    ids = [r.id for r in store.wake()["handoff"]]
    assert ids == [again.id, old.id]                                   # one per engine
    assert new.id not in ids
    assert store.leave("早", engine="5.5").engine == "5.5"


def test_weekly_due(store, monkeypatch):
    from datetime import date
    store.write("diary", "周三", event_date="2026-09-23")
    monkeypatch.setattr(store, "today", lambda: date(2026, 9, 27))     # Sunday: this week
    assert store.weekly_due() == ("2026-09-21", "2026-09-27")
    monkeypatch.setattr(store, "today", lambda: date(2026, 9, 29))     # Tuesday: last week
    assert store.weekly_due() == ("2026-09-21", "2026-09-27")
    store.write("weekly", "写了", event_date="2026-09-27")
    assert store.weekly_due() is None
    monkeypatch.setattr(store, "today", lambda: date(2026, 10, 6))     # an empty week owes nothing
    assert store.weekly_due() is None


def test_monthly_due(store, monkeypatch):
    from datetime import date
    store.write("diary", "九月", event_date="2026-09-23")
    monkeypatch.setattr(store, "today", lambda: date(2026, 9, 30))     # last day: this month
    assert store.monthly_due() == ("2026-09-01", "2026-09-30")
    monkeypatch.setattr(store, "today", lambda: date(2026, 10, 2))     # early next month
    assert store.monthly_due() == ("2026-09-01", "2026-09-30")
    store.write("weekly", "周记不算月记", event_date="2026-09-27")
    assert store.monthly_due() == ("2026-09-01", "2026-09-30")
    store.write("monthly", "写了", event_date="2026-09-30")
    assert store.monthly_due() is None
