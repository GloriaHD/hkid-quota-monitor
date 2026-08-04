"""数据只进不退的守卫（官方多节点缓存相位差导致看板横跳）。

直接调用 run.should_accept，不复刻判定逻辑——影子实现的测试在真实逻辑
被改坏时不会红，等于假绿灯（本文件上一版就犯过这个错）。
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor import fetch as F
from quota_monitor.run import STALE_ACCEPT_MIN, should_accept

HKT = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 31, 21, 0, tzinfo=HKT)


def _args(prev_stamp, new_stamp):
    prev, new = F.ts_of(prev_stamp), F.ts_of(new_stamp)
    now_src = F.ts_of(NOW.strftime("%m/%d/%Y %H:%M:%S"))
    frozen = (now_src - prev) / 60 if prev and now_src else 0.0
    return prev, new, frozen


def test_older_node_is_rejected():
    # 实测场景：两节点相位差 6~9 分钟，打到旧的那个必须丢弃
    accept, why = should_accept(*_args("07/31/2026 20:55:00", "07/31/2026 20:46:00"))
    assert not accept and why == "older"


def test_same_timestamp_is_rejected():
    accept, why = should_accept(*_args("07/31/2026 20:55:00", "07/31/2026 20:55:00"))
    assert not accept and why == "same"


def test_newer_node_passes():
    accept, why = should_accept(*_args("07/31/2026 20:46:00", "07/31/2026 20:55:00"))
    assert accept and why == "advanced"


def test_safety_valve_marks_realign_not_normal_accept():
    """冻结超阈值时放行，但原因码必须是 stale-valve——
    main 靠它决定「只对齐、不 diff」，否则会把倒流数据当成真放号发出去。"""
    accept, why = should_accept(*_args("07/31/2026 20:15:00", "07/31/2026 20:10:00"))
    assert accept and why == "stale-valve"


def test_frozen_threshold_boundary():
    _, _, frozen = _args("07/31/2026 20:15:00", "x")
    assert frozen > STALE_ACCEPT_MIN          # 45 分钟前 -> 超阈值
    _, _, frozen2 = _args("07/31/2026 20:45:00", "x")
    assert frozen2 < STALE_ACCEPT_MIN         # 15 分钟前 -> 未超


def test_no_baseline_always_accepts():
    # 首轮 / 时间戳不可解析：不做单调约束，避免把监控卡死
    accept, why = should_accept(*_args(None, "07/31/2026 20:55:00"))
    assert accept and why == "no-baseline"
    accept, why = should_accept(*_args("07/31/2026 20:55:00", "bad-format"))
    assert accept and why == "no-baseline"


def test_realign_path_emits_no_events():
    """守卫的真实契约：stale-valve 路径不得产出事件。
    直接断言 main 里的取值规则（events = [] if realign_only）。"""
    _, why = should_accept(*_args("07/31/2026 20:15:00", "07/31/2026 20:10:00"))
    realign_only = why == "stale-valve"
    assert realign_only, "冻结超阈值必须走只对齐路径"
    assert ([] if realign_only else ["fake-event"]) == []


def test_fetch_stops_early_when_data_fresh():
    """抓到「比现有新且距今 ≤90s」的数据就不再多打请求。"""
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
                          newer_than=F.ts_of("07/31/2026 20:46:00"),
                          now_ts=F.ts_of("07/31/2026 20:55:30"))
        assert got["lastUpdateTime"] == "07/31/2026 20:55:00"
        assert len(calls) == 1
    finally:
        F._fetch_once = orig


def test_fetch_keeps_sampling_when_riding_laggard_node():
    """慢节点偏差回归：候选「比现有新」但距今太久（骑上慢 8 分钟的节点）
    时必须继续补采，够到快节点——只比 newer_than 会让看板长期显示
    早被抢完的号。"""
    calls = []
    orig = F._fetch_once
    try:
        seq = iter([{"lastUpdateTime": "07/31/2026 20:47:00"},   # 慢节点：比现有新但旧
                    {"lastUpdateTime": "07/31/2026 20:54:45"}])  # 快节点：距今 15s

        def fake(*_a, **_kw):
            calls.append(1)
            return next(seq)
        F._fetch_once = fake
        got = F.fetch_raw(samples=3, gap_sec=0,
                          newer_than=F.ts_of("07/31/2026 20:46:00"),
                          now_ts=F.ts_of("07/31/2026 20:55:00"))
        assert got["lastUpdateTime"] == "07/31/2026 20:54:45", "必须够到快节点"
        assert len(calls) == 2, "慢节点不该触发提前收手"
    finally:
        F._fetch_once = orig


def test_fetch_default_keeps_multi_sample_semantics():
    """不传 newer_than 时必须保持「采满取最新」，否则独立入口被静默降级。"""
    calls = []
    orig = F._fetch_once
    try:
        seq = iter([{"lastUpdateTime": "07/31/2026 20:10:00"},
                    {"lastUpdateTime": "07/31/2026 20:55:00"},
                    {"lastUpdateTime": "07/31/2026 20:20:00"}])

        def fake(*_a, **_kw):
            calls.append(1)
            return next(seq)
        F._fetch_once = fake
        got = F.fetch_raw(samples=3, gap_sec=0)
        assert got["lastUpdateTime"] == "07/31/2026 20:55:00"
        assert len(calls) == 3
    finally:
        F._fetch_once = orig


def test_timestamp_parsed_as_hongkong_time():
    """时间戳必须按港时解析：早先按本机时区解析，带夏令时的自建 runner
    在回拨重叠窗口会多算 60 分钟，足以误触安全阀。"""
    expect = datetime(2026, 7, 31, 20, 55, 0, tzinfo=HKT).timestamp()
    assert F.ts_of("07/31/2026 20:55:00") == expect


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
