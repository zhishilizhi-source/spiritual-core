from datetime import date

import pytest

from spiritual_core.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "core.db")
    yield s
    s.close()


SLEEP = """
2026-09-27T23:20:00+08:00|2026-09-27T23:40:00+08:00|卧床
2026-09-27T23:40:00+08:00|2026-09-28T01:00:00+08:00|核心睡眠
2026-09-28T01:00:00+08:00|2026-09-28T02:10:00+08:00|深度睡眠
2026-09-28T02:10:00+08:00|2026-09-28T02:20:00+08:00|清醒
2026-09-28T02:20:00+08:00|2026-09-28T04:00:00+08:00|Core
2026-09-28T04:00:00+08:00|2026-09-28T05:30:00+08:00|REM
2026-09-28T05:30:00+08:00|2026-09-28T07:10:00+08:00|Core
2026-09-28T07:10:00+08:00|2026-09-28T07:30:00+08:00|Awake
"""
HEART = "2026-09-28T01:30:00+08:00|54\n2026-09-28T04:30:00+08:00|62\n2026-09-28T09:00:00+08:00|90"


def test_night_summary_from_what_the_phone_sends(store):
    got = store.health_ingest(SLEEP, HEART, "2026-09-27|6532")
    assert got == {"sleep": 8, "heart": 3, "steps": 1}
    n = store.health_night(date(2026, 9, 28))
    assert (n["fell"].strftime("%H:%M"), n["woke"].strftime("%H:%M")) == ("23:40", "07:10")
    assert n["asleep"] == 7 * 60 + 30 - 10                    # the awake spell in the middle is not sleep
    assert (n["deep"], n["rem"], n["wakes"]) == (70, 90, 1)   # the awake after waking doesn't count
    assert (n["hr_min"], n["hr_avg"]) == (54, 58)             # the 9:00 beat is after waking
    assert store.health_steps(date(2026, 9, 27)) == 6532
    assert store.health_synced()


def test_sending_twice_counts_once_and_steps_update(store):
    store.health_ingest(SLEEP, HEART, "2026-09-27|6000")
    store.health_ingest(SLEEP, HEART, "2026-09-27|6532")
    n = store.health_night(date(2026, 9, 28))
    assert n["asleep"] == 440 and n["hr_avg"] == 58
    assert store.health_steps(date(2026, 9, 27)) == 6532


def test_bad_lines_are_refused_plainly(store):
    with pytest.raises(StoreError, match="ISO 8601"):
        store.health_ingest(sleep="2026年9月28日 上午1:00|2026-09-28T02:00:00+08:00|Core")
    with pytest.raises(StoreError, match="时区"):
        store.health_ingest(heart="2026-09-28T01:30:00|54")
    assert store.health_night(date(2026, 9, 28)) is None



def test_full_width_bars_from_a_chinese_keyboard(store):
    got = store.health_ingest(sleep="2026-09-28T01:39:54+08:00 ｜ 2026-09-28T01:57:49+08:00 ｜ Core")
    assert got["sleep"] == 1


def test_an_afternoon_nap_is_not_the_night(store):
    store.health_ingest(sleep="""
2026-09-28T17:54:00+08:00|2026-09-28T18:50:00+08:00|Asleep
2026-09-29T00:33:00+08:00|2026-09-29T06:30:00+08:00|Core
2026-09-29T07:54:00+08:00|2026-09-29T09:41:00+08:00|Asleep
""")
    n = store.health_night(date(2026, 9, 29))
    assert (n["fell"].strftime("%H:%M"), n["woke"].strftime("%H:%M")) == ("00:33", "09:41")
    assert n["asleep"] == 357 + 107                 # the hour and a half up in between isn't sleep
    assert n["wakes"] == 1                          # that gap counts as waking once
