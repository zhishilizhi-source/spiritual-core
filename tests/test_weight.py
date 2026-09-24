from datetime import date, timedelta

import pytest

from spiritual_core import weight
from spiritual_core.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "core.db")
    yield s
    s.close()


def days_ago(store, n):
    return (store.today() - timedelta(days=n)).isoformat()


def test_whose_score_wins(store):
    r = store.write("note", "便签")
    assert weight.effective(r) == weight.KIND_DEFAULT["note"]
    r = store.write("note", "便签", importance=3)
    assert weight.effective(r) == 3
    r = store.rate(r.id, 2)                    # hindsight beats write-time
    assert weight.effective(r) == 2
    r = store.rate(r.id, 5, by="human")        # hers beats everything
    assert weight.effective(r) == 5
    r = store.rate(r.id, 1)                    # my later review can't override hers
    assert weight.effective(r) == 5
    r = store.rate(r.id, 0, by="human")        # she hands it back
    assert weight.effective(r) == 1


def test_fives_are_scarce_per_week(store):
    day = "2026-09-23"                         # a Wednesday
    store.write("diary", "a", event_date=day, importance=5)
    store.write("diary", "b", event_date="2026-09-21", importance=5)
    with pytest.raises(StoreError, match="5 分已经给满"):
        store.write("diary", "c", event_date="2026-09-27", importance=5)
    # next week has its own quota
    store.write("diary", "d", event_date="2026-09-28", importance=5)
    # her ratings are never capped
    c = store.write("diary", "c", event_date=day, importance=4)
    store.rate(c.id, 5, by="human")


def test_review_cannot_sneak_past_write_time_fives(store):
    a = store.write("diary", "a", event_date="2026-09-15", importance=5)
    store.write("diary", "b", event_date="2026-09-16", importance=5)
    c = store.write("diary", "c", event_date="2026-09-17", importance=4)
    with pytest.raises(StoreError):
        store.rate(c.id, 5)
    store.rate(a.id, 3)                        # hindsight demotes a write-time 5
    store.rate(c.id, 5)


def test_one_read_is_a_nudge_not_a_jump(store):
    r = store.write("diary", "x", event_date=days_ago(store, 30))
    before = store.weigh([r])[r.id][0]
    store.touch(r.id)
    after = store.weigh([store.get(r.id)])[r.id][0]
    assert 1.2 < after / before < 1.5


def test_review_fives_rerate_and_demote(store):
    ids = [store.write("diary", str(i), event_date="2026-09-22").id for i in range(3)]
    store.rate(ids[0], 5)
    store.rate(ids[1], 5)
    store.rate(ids[1], 5)                      # re-rating the same one is fine
    with pytest.raises(StoreError):
        store.rate(ids[2], 5)
    store.rate(ids[0], 3)                      # demote one to make room
    store.rate(ids[2], 5)


def test_what_we_keep_never_fades(store):
    today = store.today()
    old = (today - timedelta(days=400)).isoformat()
    ex = store.write("excerpt", "她：……", event_date=old, kept_by="human")
    note = store.write("note", "便签", event_date=old)
    assert weight.recency(ex, today) == 1.0
    assert weight.recency(note, today) == weight.FLOOR


def test_reading_lifts(store):
    a = store.write("diary", "a", event_date=days_ago(store, 60))
    b = store.write("diary", "b", event_date=days_ago(store, 60))
    for _ in range(4):
        store.touch(b.id)
    w = store.weigh([store.get(a.id), store.get(b.id)])
    assert w[b.id][0] > w[a.id][0]


def test_layers(store):
    fresh = store.write("note", "今天", event_date=days_ago(store, 1))
    heavy = store.write("letter", "信", event_date=days_ago(store, 100), kept_by="human")
    faded = store.write("note", "旧便签", event_date=days_ago(store, 200))
    w = store.weigh([fresh, heavy, faded])
    assert [w[x.id][1] for x in (fresh, heavy, faded)] == ["热", "温", "冷"]
    store.touch(faded.id)                      # read lately -> warm again
    assert store.weigh([store.get(faded.id)])[faded.id][1] == "温"


def test_surface_skips_hot_and_prefers_heavy(store):
    store.write("note", "旧便签", event_date=days_ago(store, 90))
    letter = store.write("letter", "信", event_date=days_ago(store, 90), kept_by="human")
    store.write("diary", "昨天", event_date=days_ago(store, 1))
    w = store.wake()
    assert w["surfaced"][0].id == letter.id
    assert all(r.event_date < days_ago(store, weight.HOT_DAYS) for r in w["surfaced"])


def test_anchors_do_not_hog_surfacing(store):
    for i in range(6):
        store.write("anchor", f"锚点{i}", event_date=days_ago(store, 60))
    mem = store.write("memory", "一件事", event_date=days_ago(store, 60))
    assert [r.id for r in store.wake()["surfaced"]] == [mem.id]


def test_search_lets_weight_climb_but_not_jump(store):
    store.write("note", "猪脚饭", event_date=days_ago(store, 300))
    heavy = store.write("letter", "猪脚饭那天", event_date=days_ago(store, 300), kept_by="human")
    assert store.search("猪脚饭")[0].id == heavy.id


def test_review_defaults_to_last_week(store):
    today = store.today()
    last_week = today - timedelta(days=7)
    store.write("diary", "上周", event_date=last_week.isoformat())
    start, end, records = store.review("week")
    assert start <= last_week.isoformat() <= end
    assert [r.body for r in records] == ["上周"]
    start, end, _ = store.review("month", "2026-02-10")
    assert (start, end) == ("2026-02-01", "2026-02-28")


def test_bad_scores(store):
    with pytest.raises(StoreError):
        store.write("note", "x", importance=6)
    r = store.write("note", "x")
    with pytest.raises(StoreError):
        store.rate(r.id, "high")
    with pytest.raises(StoreError):
        store.rate(r.id, 3, by="someone")


def test_iso_week_and_month():
    assert weight.iso_week("2026-09-24") == ("2026-09-21", "2026-09-27")
    assert weight.month("2026-12-15") == ("2026-12-01", "2026-12-31")


def test_search_or_groups(store):
    store.write("diary", "那天她哭了很久", event_date="2026-09-01")
    store.write("diary", "心里难过但没说", event_date="2026-09-02")
    store.write("diary", "吃了猪脚饭", event_date="2026-09-03")
    assert len(store.search("难过|哭")) == 2                    # short terms -> LIKE path
    assert len(store.search("哭了很久|难过但没说")) == 2          # long terms -> FTS path
    assert len(store.search("她哭了|难过但 那天")) == 1           # OR group AND another term


def test_wake_dedupes_handoff_and_falls_back_to_last_diary(store):
    h = store.write("handoff", "交接")
    old = store.write("diary", "三天前", event_date=days_ago(store, 3))
    w = store.wake()
    assert h.id not in [r.id for r in w["days"]]
    assert [r.id for r in w["handoff"]] == [h.id]
    assert [r.id for r in w["last_diary"]] == [old.id]
    store.write("diary", "昨天", event_date=days_ago(store, 1))
    assert store.wake()["last_diary"] == []


def test_anniversaries(store):
    today = store.today()
    store.set_anniversary("在一起", (today - timedelta(days=9)).isoformat())
    store.set_anniversary("在一起", (today - timedelta(days=99)).isoformat())   # same name replaces
    lines = store.anniversary_lines()
    assert len(lines) == 1 and "第 100 天" in lines[0] and "整百" in lines[0]
    with pytest.raises(StoreError):
        store.set_anniversary("", today.isoformat())


def test_anniversary_month_mark():
    from spiritual_core.store import _months_back
    assert _months_back(date(2026, 9, 24), 1) == date(2026, 8, 24)
    assert _months_back(date(2026, 3, 31), 1) is None
    assert _months_back(date(2026, 1, 15), 1) == date(2025, 12, 15)


def test_review_due(store):
    store.write("diary", "上周", event_date=days_ago(store, 7))
    assert store.review_due()
    r = store.write("diary", "上周2", event_date=days_ago(store, 7))
    store.rate(r.id, 3)
    assert not store.review_due()


def test_moods(store):
    assert store.mood_now() is None
    store.set_mood("累")
    now = store.set_mood("想你", note="下班路上")
    assert now["mood"] == "想你" and now["note"] == "下班路上"
    assert [m["mood"] for m in store.mood_log(1)] == ["累", "想你"]
    assert store.wake()["mood"]["mood"] == "想你"
    with pytest.raises(StoreError):
        store.set_mood("emo")
