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

    def fake():
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    F._fetch_once = fake


def test_picks_freshest_node():
    orig = F._fetch_once
    try:
        _patch([_raw("07/30/2026 17:18:52"),
                _raw("07/30/2026 17:22:43"),
                _raw("07/30/2026 17:19:00")])
        assert F.fetch_raw(samples=3, gap_sec=0)["lastUpdateTime"] == "07/30/2026 17:22:43"
    finally:
        F._fetch_once = orig


def test_survives_later_sample_failure():
    orig = F._fetch_once
    try:
        _patch([_raw("07/30/2026 17:18:52"), RuntimeError("boom")])
        assert F.fetch_raw(samples=3, gap_sec=0)["lastUpdateTime"] == "07/30/2026 17:18:52"
    finally:
        F._fetch_once = orig


def test_unparsable_timestamp_treated_as_oldest():
    orig = F._fetch_once
    try:
        _patch([_raw(None), _raw("07/30/2026 10:00:00")])
        assert F.fetch_raw(samples=2, gap_sec=0)["lastUpdateTime"] == "07/30/2026 10:00:00"
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
