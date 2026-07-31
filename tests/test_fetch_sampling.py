"""多取样选最新节点的逻辑（官方多节点缓存新鲜度差异达 4 分钟）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor import fetch as F


def _raw(ts):
    return {"lastUpdateTime": ts, "office": [], "data": []}


def _patch(seq):
    """让 _fetch_once 依次返回 seq 中的响应（异常则抛出）。"""
    it = iter(seq)

    def fake(*_a, **_kw):   # 补采会传 timeout/retries，签名要能吃掉
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    F._fetch_once = fake


def _ts(s):
    return F._update_ts({"lastUpdateTime": s})


def test_picks_freshest_node():
    """打到旧节点时继续补采，取最新的一份。"""
    orig = F._fetch_once
    try:
        _patch([_raw("07/30/2026 17:18:52"),
                _raw("07/30/2026 17:22:43"),
                _raw("07/30/2026 17:19:00")])
        got = F.fetch_raw(samples=3, gap_sec=0, newer_than=_ts("07/30/2026 23:00:00"))
        assert got["lastUpdateTime"] == "07/30/2026 17:22:43"
    finally:
        F._fetch_once = orig


def test_early_exit_when_first_sample_is_new_enough():
    """首次就抓到比现有更新的数据 -> 只打 1 个请求，不做无谓补采。"""
    orig = F._fetch_once
    calls = []
    try:
        seq = [_raw("07/30/2026 17:22:43"), _raw("07/30/2026 17:30:00")]
        it = iter(seq)

        def fake(*_a, **_kw):
            calls.append(1)
            return next(it)
        F._fetch_once = fake
        got = F.fetch_raw(samples=3, gap_sec=0, newer_than=_ts("07/30/2026 17:00:00"))
        assert got["lastUpdateTime"] == "07/30/2026 17:22:43"
        assert len(calls) == 1, f"应只请求 1 次，实际 {len(calls)} 次"
    finally:
        F._fetch_once = orig


def test_stops_once_beats_stored_timestamp():
    """补采到比现有更新的一份就收手，不必把 samples 用满。"""
    orig = F._fetch_once
    calls = []
    try:
        seq = [_raw("07/30/2026 17:10:00"), _raw("07/30/2026 17:25:00"),
               _raw("07/30/2026 17:40:00")]
        it = iter(seq)

        def fake(*_a, **_kw):
            calls.append(1)
            return next(it)
        F._fetch_once = fake
        got = F.fetch_raw(samples=3, gap_sec=0, newer_than=_ts("07/30/2026 17:20:00"))
        assert got["lastUpdateTime"] == "07/30/2026 17:25:00"
        assert len(calls) == 2
    finally:
        F._fetch_once = orig


def test_survives_later_sample_failure():
    orig = F._fetch_once
    try:
        _patch([_raw("07/30/2026 17:18:52"), RuntimeError("boom")])
        assert F.fetch_raw(samples=3, gap_sec=0, newer_than=1e18)["lastUpdateTime"] == "07/30/2026 17:18:52"
    finally:
        F._fetch_once = orig


def test_unparsable_timestamp_treated_as_oldest():
    orig = F._fetch_once
    try:
        _patch([_raw(None), _raw("07/30/2026 10:00:00")])
        assert F.fetch_raw(samples=2, gap_sec=0, newer_than=1e18)["lastUpdateTime"] == "07/30/2026 10:00:00"
    finally:
        F._fetch_once = orig


def test_single_sample_mode():
    orig = F._fetch_once
    try:
        _patch([_raw("07/30/2026 12:00:00")])
        assert F.fetch_raw(samples=1)["lastUpdateTime"] == "07/30/2026 12:00:00"
    finally:
        F._fetch_once = orig


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
