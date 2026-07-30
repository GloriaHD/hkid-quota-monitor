"""放号历史积累的写入侧冷却（防接口抖动灌噪声）。"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.run import history_fresh

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 30, 15, 0, tzinfo=HKT)


def ev(etype="quota_open", office="FTO", date="2026-09-08"):
    return {"type": etype, "office": office, "date": date, "session": "R",
            "from": "r", "to": "y", "detected_at": NOW.isoformat()}


def line(office, date, hours_ago):
    return json.dumps({"office": office, "date": date, "session": "R",
                       "detected_at": (NOW - timedelta(hours=hours_ago)).isoformat()})


def test_first_open_recorded_gone_ignored():
    out = history_fresh([ev(), ev(etype="quota_gone")], [], NOW)
    assert [e["type"] for e in out] == ["quota_open"]


def test_flapping_suppressed_within_6h():
    existing = [line("FTO", "2026-09-08", hours_ago=1)]
    assert history_fresh([ev()], existing, NOW) == []


def test_recorded_again_after_6h_and_other_cells_pass():
    existing = [line("FTO", "2026-09-08", hours_ago=7)]
    out = history_fresh([ev(), ev(office="RHK", date="2026-09-02")], existing, NOW)
    assert len(out) == 2


def test_corrupt_lines_skipped():
    out = history_fresh([ev()], ["{broken", '{"no":"keys"}'], NOW)
    assert len(out) == 1


def test_bad_detected_at_does_not_crash():
    """回归：一行脏历史曾能每轮炸掉监控（自我延续的中毒态）。
    naive 时间串与非字符串都必须被容忍，而不是抛 TypeError。"""
    bad = [
        json.dumps({"office": "FTO", "date": "2026-09-08", "session": "R",
                    "detected_at": "2026-07-30T15:00:00"}),      # 无时区
        json.dumps({"office": "FTO", "date": "2026-09-08", "session": "R",
                    "detected_at": 1234567890}),                  # 非字符串
        json.dumps({"office": "FTO", "date": "2026-09-08", "session": "R",
                    "detected_at": "not-a-date"}),
    ]
    for line in bad:
        out = history_fresh([ev()], [line], NOW)   # 不抛异常即通过
        assert isinstance(out, list)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
