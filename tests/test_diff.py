"""diff_snapshots 的可执行验收（spec phase-1 验收标准 3）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.diff import diff_snapshots


def snap(dates, quota):
    return {"schema": 1, "dates": dates, "quota": quota}


def test_first_run_no_events():
    new = snap(["2026-08-01"], {"FTO": {"2026-08-01": {"R": "g", "K": "x"}}})
    assert diff_snapshots(None, new) == []
    assert diff_snapshots({}, new) == []


def test_quota_open_r_to_g():
    old = snap(["2026-08-01"], {"FTO": {"2026-08-01": {"R": "r", "K": "x"}}})
    new = snap(["2026-08-01"], {"FTO": {"2026-08-01": {"R": "g", "K": "x"}}})
    ev = diff_snapshots(old, new)
    assert len(ev) == 1
    e = ev[0]
    assert (e["type"], e["office"], e["date"], e["session"], e["from"], e["to"]) == \
        ("quota_open", "FTO", "2026-08-01", "R", "r", "g")


def test_quota_open_x_to_y_extended_session():
    old = snap(["2026-08-01"], {"RHK": {"2026-08-01": {"R": "r", "K": "x"}}})
    new = snap(["2026-08-01"], {"RHK": {"2026-08-01": {"R": "r", "K": "y"}}})
    ev = diff_snapshots(old, new)
    assert [e["type"] for e in ev] == ["quota_open"]
    assert ev[0]["session"] == "K"


def test_new_date_only_reports_open_slots():
    old = snap(["2026-08-01"], {"FTO": {"2026-08-01": {"R": "r", "K": "x"}}})
    new = snap(["2026-08-01", "2026-08-02"],
               {"FTO": {"2026-08-01": {"R": "r", "K": "x"},
                        "2026-08-02": {"R": "g", "K": "x"}}})
    ev = diff_snapshots(old, new)
    assert [e["type"] for e in ev] == ["new_date"]
    assert ev[0]["date"] == "2026-08-02"

    # 新日期但已满 -> 不报，避免每日滚动窗口刷屏
    new_full = snap(["2026-08-01", "2026-08-02"],
                    {"FTO": {"2026-08-01": {"R": "r", "K": "x"},
                             "2026-08-02": {"R": "r", "K": "x"}}})
    assert diff_snapshots(old, new_full) == []


def test_quota_gone():
    old = snap(["2026-08-01"], {"TMO": {"2026-08-01": {"R": "y", "K": "x"}}})
    new = snap(["2026-08-01"], {"TMO": {"2026-08-01": {"R": "r", "K": "x"}}})
    ev = diff_snapshots(old, new)
    assert [e["type"] for e in ev] == ["quota_gone"]


def test_g_to_y_is_not_an_event():
    old = snap(["2026-08-01"], {"YLO": {"2026-08-01": {"R": "g", "K": "x"}}})
    new = snap(["2026-08-01"], {"YLO": {"2026-08-01": {"R": "y", "K": "x"}}})
    assert diff_snapshots(old, new) == []


def test_multi_office_sorted_output():
    old = snap(["2026-08-01"],
               {"RKO": {"2026-08-01": {"R": "r", "K": "x"}},
                "FTO": {"2026-08-01": {"R": "r", "K": "x"}}})
    new = snap(["2026-08-01"],
               {"RKO": {"2026-08-01": {"R": "g", "K": "x"}},
                "FTO": {"2026-08-01": {"R": "y", "K": "x"}}})
    ev = diff_snapshots(old, new)
    assert [e["office"] for e in ev] == ["FTO", "RKO"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
