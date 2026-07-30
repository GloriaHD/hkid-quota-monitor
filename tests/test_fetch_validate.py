"""快照校验的可执行验收——空/残缺响应绝不落盘（审计 🔴② 回归）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.fetch import normalize, validate_snapshot


def _raises(snap):
    try:
        validate_snapshot(snap)
        return False
    except RuntimeError:
        return True


def test_empty_response_rejected():
    assert _raises(normalize({"office": [], "data": []}))
    assert _raises(normalize({}))


def test_partial_response_rejected():
    raw = {"office": [], "data": [
        {"date": "08/01/2026", "officeId": "FTO", "quotaR": "quota-r", "quotaK": "no-quotaK"}]}
    assert _raises(normalize(raw))


def test_healthy_snapshot_passes():
    offices = ["FTO", "RHK", "RKO", "RTK", "TMO", "YLO"]
    raw = {
        "office": [{"officeId": o,
                    "chs": {"officeName": o, "region": "", "district": "",
                            "officeAddress": "", "officeHint": ""},
                    "cht": {"officeName": o}, "eng": {"officeName": o},
                    "telNum": ""} for o in offices],
        "data": [{"date": f"{m:02d}/{d:02d}/2026", "officeId": o,
                  "quotaR": "quota-r", "quotaK": "no-quotaK"}
                 for o in offices for m in (8, 9) for d in range(1, 29)],
        "lastUpdateTime": "07/30/2026 12:00:00",
    }
    snap = normalize(raw)
    validate_snapshot(snap)  # 不应抛异常
    assert len(snap["offices"]) == 6 and len(snap["dates"]) == 56


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
