"""数据只进不退的守卫（官方多节点缓存差 9 分钟导致看板横跳）。"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor import fetch as F
from quota_monitor import run as R

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 31, 21, 0, tzinfo=HKT)


def _frozen_min(stamp):
    now_src = F.ts_of(NOW.strftime("%m/%d/%Y %H:%M:%S"))
    return (now_src - F.ts_of(stamp)) / 60


def _blocked(prev_stamp, new_stamp):
    """复刻 run.main 的判定：新数据不比现有新、且未冻结超阈值 -> 拦截。"""
    prev, new = F.ts_of(prev_stamp), F.ts_of(new_stamp)
    return bool(prev and new and new <= prev
                and _frozen_min(prev_stamp) < R.STALE_ACCEPT_MIN)


def test_older_node_is_rejected():
    # 实测场景：两节点相差 9 分钟，打到旧的那个必须丢弃
    assert _blocked("07/31/2026 20:55:00", "07/31/2026 20:46:00")


def test_same_timestamp_is_rejected():
    assert _blocked("07/31/2026 20:55:00", "07/31/2026 20:55:00")


def test_newer_node_passes():
    assert not _blocked("07/31/2026 20:46:00", "07/31/2026 20:55:00")


def test_safety_valve_releases_when_frozen_too_long():
    """现有数据已冻结超过阈值 -> 放弃单调约束，宁可抖动不可永久停摆
    （官方改时间戳格式 / 新节点下线时的兜底）。"""
    assert _frozen_min("07/31/2026 20:15:00") > R.STALE_ACCEPT_MIN
    assert not _blocked("07/31/2026 20:15:00", "07/31/2026 20:10:00")


def test_unparsable_timestamps_never_block():
    # 解析不了就不做单调约束，避免因格式变化把监控卡死
    assert not _blocked(None, "07/31/2026 20:55:00")
    assert not _blocked("07/31/2026 20:55:00", "bad-format")


def test_fetch_stops_early_when_data_advanced():
    """守卫依赖 fetch 的提前收手：抓到更新的数据就不再多打请求。"""
    calls = []
    orig = F._fetch_once
    try:
        seq = iter([{"lastUpdateTime": "07/31/2026 20:55:00"},
                    {"lastUpdateTime": "07/31/2026 21:00:00"}])

        def fake(*_a, **_kw):
            calls.append(1)
            return next(seq)
        F._fetch_once = fake
        got = F.fetch_raw(samples=3, gap_sec=0,
                          newer_than=F.ts_of("07/31/2026 20:46:00"))
        assert got["lastUpdateTime"] == "07/31/2026 20:55:00"
        assert len(calls) == 1
    finally:
        F._fetch_once = orig


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
