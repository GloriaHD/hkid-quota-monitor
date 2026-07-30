"""通知防抖/聚合逻辑的可执行验收（spec phase-4 验收标准 1）。"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.notify import (compose, event_matches, filter_events,
                                  load_alert_cfg, summarize, tier_of)

HKT = timezone(timedelta(hours=8))
T0 = datetime(2026, 7, 30, 12, 0, tzinfo=HKT)


def ev(office="FTO", date="2026-09-08", session="R", etype="quota_open", to="y"):
    return {"type": etype, "office": office, "date": date, "session": session,
            "from": "r", "to": to, "detected_at": T0.isoformat()}


def test_first_notification_passes_and_records():
    state = {}
    out = filter_events([ev()], state, 360, now=T0)
    assert len(out) == 1
    assert "FTO|2026-09-08|R" in state["cell_last_notified"]


def test_within_cooldown_suppressed():
    state = {}
    filter_events([ev()], state, 360, now=T0)
    out = filter_events([ev()], state, 360, now=T0 + timedelta(minutes=5))
    assert out == []  # 抖动场景：5 分钟后同格再放号 -> 抑制


def test_after_cooldown_passes_again():
    state = {}
    filter_events([ev()], state, 360, now=T0)
    out = filter_events([ev()], state, 360, now=T0 + timedelta(minutes=361))
    assert len(out) == 1


def test_quota_gone_never_notifies():
    out = filter_events([ev(etype="quota_gone")], {}, 360, now=T0)
    assert out == []


def test_different_cells_independent():
    state = {}
    filter_events([ev(date="2026-09-08")], state, 360, now=T0)
    out = filter_events([ev(date="2026-09-09")], state, 360,
                        now=T0 + timedelta(minutes=5))
    assert len(out) == 1  # 别的日期放号不受前一格冷却影响


def test_summarize_groups_by_office():
    lines = summarize([ev(date="2026-09-08"), ev(date="2026-09-09", to="g"),
                       ev(office="RHK", date="2026-09-02")])
    assert len(lines) == 2
    assert lines[0].startswith("火炭：") and "09/08(少量)" in lines[0] and "09/09(充足)" in lines[0]
    assert lines[1].startswith("港岛(湾仔)：")


CFG = {"urgent_before": "2026-09-01", "notice_before": "2026-09-15"}


def test_tier_boundaries():
    assert tier_of("2026-08-31", CFG) == "urgent"   # 阈值前一天
    assert tier_of("2026-09-01", CFG) == "notice"   # 等于 urgent 阈值日不算紧急
    assert tier_of("2026-09-14", CFG) == "notice"
    assert tier_of("2026-09-15", CFG) == "info"     # 等于 notice 阈值日不算
    assert tier_of("2026-11-20", CFG) == "info"


def test_tier_missing_config_all_info():
    assert tier_of("2026-08-01", {}) == "info"
    assert tier_of("2026-08-01", {"notice_before": "2026-09-15"}) == "notice"


def _cfg_file(tmp_name, content, text=True):
    import tempfile
    from pathlib import Path as P
    p = P(tempfile.gettempdir()) / tmp_name
    if text:
        p.write_text(content, encoding="utf-8")
    return str(p)


def test_load_alert_cfg_untrusted_input():
    # config 是用户网页直编的不可信输入：任何手滑都必须安全降级（审计 🔴 回归）
    assert load_alert_cfg("nonexistent_config.json") == {}                     # 文件缺失
    assert load_alert_cfg(_cfg_file("t1.json", "{broken")) == {}               # 坏 JSON
    assert load_alert_cfg(_cfg_file("t2.json",
        '{"urgent_before": 20260901}')) == {}                                  # 引号手滑成数字
    assert load_alert_cfg(_cfg_file("t3.json",
        '{"urgent_before": "9/1/2026"}')) == {}                                # 非 ISO 格式
    assert load_alert_cfg(_cfg_file("t4.json",
        '{"urgent_before": "2026-09-01", "notice_before": "2026-09-15"}')) == \
        {"urgent_before": "2026-09-01", "notice_before": "2026-09-15"}         # 正常
    assert load_alert_cfg(_cfg_file("t5.json",
        '{"urgent_before": "2026-09-15", "notice_before": "2026-09-01"}')) == \
        {"urgent_before": "2026-09-01", "notice_before": "2026-09-15"}         # 填反自动对调


def test_event_matches_personalization():
    e = ev(office="RHK", date="2026-09-08")
    assert event_matches({}, e)                                    # 旧记录=全量
    assert event_matches({"offices": ["RHK", "RKO"]}, e)
    assert not event_matches({"offices": ["TMO"]}, e)
    assert event_matches({"before": "2026-09-09"}, e)
    assert not event_matches({"before": "2026-09-08"}, e)          # 边界：等于截止日不算
    assert not event_matches({"offices": ["RHK"], "before": "2026-09-01"}, e)


def test_compose_counts_use_tier_scope():
    """回归🟡：主题/正文/飞书三处用了三种计数，@所有人的强提醒会报错数字。
    「X 前有 N 个」必须是该档内的个数，不是本批总数。"""
    events = [ev(date="2026-08-20"), ev(date="2026-09-10"), ev(date="2026-09-11")]
    subject, html_body = compose(events, CFG)          # 1 个 urgent + 2 个 notice
    assert "前有 1 个名额" in subject and "🚨" in subject
    assert "前有 1 个名额放出" in html_body
    assert "本批共检出 3 个" in html_body               # 总数另起一句，不混淆
    subject2, _ = compose([ev(date="2026-11-20")], CFG)
    assert subject2 == "🎫 香港ID预约放号：1 个名额"


def test_load_state_survives_corruption(tmp_path=None):
    """回归🟡：state 文件被写坏时曾整轮炸掉通知链路，且文件提交回仓库后每轮必死。"""
    import tempfile
    from pathlib import Path as P
    from quota_monitor import notify as N
    orig = N.STATE_PATH
    tmp = P(tempfile.mkdtemp())
    try:
        N.STATE_PATH = tmp / "s.json"
        assert N.load_state() == {"cell_last_notified": {}}       # 文件不存在
        N.STATE_PATH.write_text("{broken", encoding="utf-8")
        assert N.load_state() == {"cell_last_notified": {}}       # 坏 JSON
        N.STATE_PATH.write_text('["not","a","dict"]', encoding="utf-8")
        assert N.load_state() == {"cell_last_notified": {}}       # 结构异常
    finally:
        N.STATE_PATH = orig


def test_filter_events_tolerates_bad_timestamp():
    state = {"cell_last_notified": {"FTO|2026-09-08|R": "not-a-time"}}
    out = filter_events([ev()], state, 360, now=T0)               # 不抛异常
    assert len(out) == 1


def test_prune_state_drops_malformed_keys():
    from quota_monitor.notify import prune_state
    state = {"cell_last_notified": {"badkey": T0.isoformat(),
                                    "FTO|2026-09-08|R": T0.isoformat()}}
    prune_state(state, today="2026-07-30")
    assert list(state["cell_last_notified"]) == ["FTO|2026-09-08|R"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
