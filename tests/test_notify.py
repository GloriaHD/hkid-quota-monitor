"""通知防抖/聚合逻辑的可执行验收（spec phase-4 验收标准 1）。"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.notify import (build_feishu_card, check_feishu_body,
                                  cn_date, compose, earliest_line,
                                  event_matches, filter_events,
                                  in_monitor_window, load_alert_cfg, summarize,
                                  tier_of)

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
    assert lines[1].startswith("湾仔：")


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


def test_monitor_window_filters_far_dates():
    """窗口外（10 月、9 月下旬）的名额一律不推送——实测这类占放号总量约三成。"""
    cfg = {"monitor_before": "2026-09-16"}
    assert in_monitor_window("2026-08-20", cfg)
    assert in_monitor_window("2026-09-15", cfg)
    assert not in_monitor_window("2026-09-16", cfg)      # 边界：等于截止日不算
    assert not in_monitor_window("2026-09-25", cfg)
    assert not in_monitor_window("2026-10-08", cfg)


def test_monitor_window_absent_means_no_filter():
    assert in_monitor_window("2026-12-31", {})
    assert in_monitor_window("2026-12-31", {"urgent_before": "2026-09-01"})


def test_monitor_window_config_is_validated():
    import json as _j
    assert load_alert_cfg(_cfg_file("t6.json",
        _j.dumps({"monitor_before": "2026-09-16"}))) == {"monitor_before": "2026-09-16"}
    assert load_alert_cfg(_cfg_file("t7.json",
        _j.dumps({"monitor_before": "9/16/2026"}))) == {}   # 非 ISO 丢弃


def test_feishu_bad_token_raises_despite_http_200():
    """实测：机器人换了但 secret 没同步时，飞书回 HTTP 200 + code 19001。
    只看状态码会把「一条没送到」记成成功，冷却照烧 -> 这批名额彻底错过。"""
    raw = ('{"code":19001,"data":{},"msg":"param invalid: '
           'incoming webhook access token invalid"}')
    try:
        check_feishu_body(200, raw)
    except RuntimeError as e:
        assert "19001" in str(e)
    else:
        raise AssertionError("坏 token 必须抛出，否则失败被静默吞掉")


def test_feishu_success_body_passes():
    check_feishu_body(200, '{"StatusCode":0,"code":0,"msg":"success"}')  # 不抛即通过


def test_feishu_unparsable_body_does_not_raise():
    """飞书改返回格式不该拖垮真送达的通知——解析不出来一律放行。"""
    check_feishu_body(200, "")
    check_feishu_body(200, "<html>gateway</html>")
    check_feishu_body(200, '{"msg":"ok"}')          # 没有 code 字段
    check_feishu_body(200, "[1,2]")                 # 合法 JSON 但不是对象
    check_feishu_body(200, "123")                   # 裸数字，.get 会 AttributeError


def test_send_feishu_actually_calls_the_check():
    """守卫必须真的接在 send_feishu 上。只测 check_feishu_body 本身不够——
    删掉那行调用时函数级测试全绿，而症状恰恰是它要防的那种无声失败。"""
    import os
    from quota_monitor import notify as N

    class _Resp:
        status = 200

        def read(self, n=None):
            return b'{"code":19001,"msg":"param invalid"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig_open = N.urllib.request.urlopen
    orig_hook = os.environ.get("FEISHU_WEBHOOK")
    os.environ["FEISHU_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    N.urllib.request.urlopen = lambda *a, **kw: _Resp()
    try:
        N.send_feishu(["火炭 2026-09-08 上午"], 1, dry=False)
    except RuntimeError as e:
        assert "19001" in str(e)
    else:
        raise AssertionError("send_feishu 必须把返回体交给 check_feishu_body")
    finally:
        N.urllib.request.urlopen = orig_open
        if orig_hook is None:
            os.environ.pop("FEISHU_WEBHOOK", None)
        else:
            os.environ["FEISHU_WEBHOOK"] = orig_hook


CFG_TIERS = {"monitor_before": "2026-09-16", "urgent_before": "2026-09-01",
             "notice_before": "2026-09-16"}


def test_cn_date_and_bad_config_passthrough():
    assert cn_date("2026-09-01") == "9月1日"
    assert cn_date("2026-12-15") == "12月15日"
    # config 是网页可编的不可信输入：格式不对只准降级显示，不准炸掉通知
    assert cn_date("9/1/2026") == "9/1/2026"
    assert cn_date(None) == "近期"
    assert cn_date("") == "近期"


def test_earliest_line_picks_the_single_most_actionable_fact():
    evs = [ev(office="FTO", date="2026-09-08"),
           ev(office="RHK", date="2026-08-22", to="g"),
           ev(office="TMO", date="2026-09-01")]
    assert earliest_line(evs) == "湾仔 08/22 （充足）"
    assert earliest_line([ev(office="TMO", date="2026-08-30", session="K")]) \
        == "屯门 08/30 延长时段 （少量）"
    assert earliest_line([]) == ""


def test_card_tier_drives_color_and_at_all():
    """红/橙/蓝三档一眼可分；@所有人只在 urgent 出现——
    每条都 @ 全员会被当噪声屏蔽，那样急件也没人看了。"""
    def dump(tier):
        c = build_feishu_card(["**湾仔**：09/02(少量)"], 1, tier, CFG_TIERS, 1, "湾仔 09/02")
        import json as _j
        return c["header"]["template"], c["header"]["title"]["content"], \
            _j.dumps(c["elements"], ensure_ascii=False)

    color, title, body = dump("urgent")
    assert color == "red" and "9月1日前" in title and "速抢" in title
    assert "<at id=all></at>" in body

    color, title, body = dump("notice")
    assert color == "orange" and "9月16日前" in title
    assert "<at id=all>" not in body

    color, _, body = dump("info")
    assert color == "blue" and "<at id=all>" not in body


def test_card_explains_count_mismatch():
    """标题只报本档数量、列表是全部——「说 2 个却列了 3 行」看着像 bug，必须说明。"""
    import json as _j
    both = _j.dumps(build_feishu_card(["a", "b", "c"], 2, "urgent", CFG_TIERS, 3,
                                      "湾仔 08/22")["elements"], ensure_ascii=False)
    assert "本轮共 3 个，其中 2 个在9月1日前" in both
    same = _j.dumps(build_feishu_card(["a"], 1, "urgent", CFG_TIERS, 1, "x")
                    ["elements"], ensure_ascii=False)
    assert "本轮共" not in same, "数量一致时不该多这句废话"
    # info 档的定义就是落在 notice_before 之外，说「其中 N 个在X前」自相矛盾
    info = _j.dumps(build_feishu_card(["a"], 1, "info", CFG_TIERS, 3, "")
                    ["elements"], ensure_ascii=False)
    assert "本轮共" not in info


def test_stray_slots_get_the_sniper_tag():
    """散号（一轮 ≤2 格的孤立回流）是黄牛转关的失手窗口，飞书卡片、
    纯文本兜底、邮件三条路都必须带上手快专场标记；大批量放号不带。"""
    from quota_monitor.notify import build_email_html, is_stray
    assert is_stray([ev()]) and is_stray([ev(), ev(office="TMO")])
    assert not is_stray([]) and not is_stray([ev(), ev(), ev()])

    import json as _j
    tagged = _j.dumps(build_feishu_card(["x"], 1, "notice", CFG_TIERS, 1,
                                        "湾仔 09/02", stray=True), ensure_ascii=False)
    assert "散号回流" in tagged and "手快专场" in tagged
    plain = _j.dumps(build_feishu_card(["x"] * 9, 9, "notice", CFG_TIERS, 9, ""),
                     ensure_ascii=False)
    assert "散号回流" not in plain

    html_doc = build_email_html(["x"], 1, "notice", CFG_TIERS, 1, stray=True)
    assert "散号回流" in html_doc
    assert "散号回流" not in build_email_html(["x"] * 9, 9, "notice", CFG_TIERS, 9)

    # compose 的接线：单事件邮件自动带标记
    subj, body = compose([ev()], CFG_TIERS)
    assert "散号回流" in body
    _, body_mass = compose([ev(date=f"2026-09-{d:02d}") for d in range(1, 8)], CFG_TIERS)
    assert "散号回流" not in body_mass

    # send_feishu 的接线：兜底纯文本也要带
    sent, err = _send_with_fake(
        [11246, 0], lines=["**湾仔**：09/02(少量)"], n=1, dry=False, tier="notice",
        cfg=CFG_TIERS, n_top=1, events=[ev(date="2026-09-02")])
    assert err is None
    assert "散号回流" in _j.dumps(sent[0], ensure_ascii=False)
    assert "散号回流" in sent[1]["content"]["text"]


def test_card_always_carries_booking_button_and_disclaimer():
    c = build_feishu_card([], 0, "info", {}, 0, "")
    import json as _j
    body = _j.dumps(c["elements"], ensure_ascii=False)
    assert "立即去官网预约" in body and "gov.hk" in body
    assert "不代抢代约" in body      # goal.md 不变量 1：只监控不代抢


def _fake_feishu(codes):
    """按 codes 顺序返回飞书响应，记录每次实际发出的 payload。
    元素给 int 表示只设 code，给 str 表示整个返回体自定（用来造 log_id 之类）。"""
    sent = []

    class _Resp:
        def __init__(self, code):
            self.status, self._code = 200, code

        def read(self, n=None):
            if isinstance(self._code, str):
                return self._code.encode()
            return ('{"code":%d,"msg":"x"}' % self._code).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    it = iter(codes)

    def fake(req, *a, **kw):
        import json as _j
        sent.append(_j.loads(req.data.decode()))
        return _Resp(next(it))
    return fake, sent


def test_card_reject_falls_back_to_plain_text():
    """卡片 schema 归飞书管，哪天字段改了也不能让这条通道整个哑掉。
    实测过：畸形 card 回 code 11246，退回纯文本后送达成功。"""
    import os
    from quota_monitor import notify as N
    fake, sent = _fake_feishu([11246, 0])
    orig, hook = N.urllib.request.urlopen, os.environ.get("FEISHU_WEBHOOK")
    os.environ["FEISHU_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    N.urllib.request.urlopen = fake
    try:
        N.send_feishu(["**湾仔**：09/02(少量)"], 1, dry=False, tier="notice",
                      cfg=CFG_TIERS, n_top=1, events=[ev(date="2026-09-02")])
    finally:
        N.urllib.request.urlopen = orig
        os.environ.pop("FEISHU_WEBHOOK", None) if hook is None \
            else os.environ.update(FEISHU_WEBHOOK=hook)
    assert [p["msg_type"] for p in sent] == ["interactive", "text"]
    assert "**" not in sent[1]["content"]["text"], "纯文本里不该出现 lark_md 星号"
    assert "最早可约" in sent[1]["content"]["text"]


def _send_with_fake(codes, **kw):
    """在桩掉 urlopen 的前提下跑一次 send_feishu，返回 (发出的 payload 列表, 异常)。"""
    import os
    from quota_monitor import notify as N
    fake, sent = _fake_feishu(codes)
    orig, hook = N.urllib.request.urlopen, os.environ.get("FEISHU_WEBHOOK")
    os.environ["FEISHU_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    N.urllib.request.urlopen = fake
    err = None
    try:
        N.send_feishu(**kw)
    except Exception as e:      # noqa: BLE001 - 交给调用方断言
        err = e
    finally:
        N.urllib.request.urlopen = orig
        if hook is None:
            os.environ.pop("FEISHU_WEBHOOK", None)
        else:
            os.environ["FEISHU_WEBHOOK"] = hook
    return sent, err


def test_urgent_fallback_still_ats_everyone():
    """触发兜底的场景（飞书改了卡片字段）会是持续性的——@所有人 若只活在
    卡片里，届时所有紧急提醒都静默沉底。兜底必须是降级版，不是精简版。"""
    sent, err = _send_with_fake(
        [11246, 0], lines=["**湾仔**：08/22(充足)"], n=3, dry=False, tier="urgent",
        cfg=CFG_TIERS, n_top=2, events=[ev(office="RHK", date="2026-08-22", to="g")])
    assert err is None and len(sent) == 2
    # 顺带钉住 n_top/n_all 的接线：这两个整数语义相反、类型相同，
    # 在调用点写反不会报错，只会静默播出错误数字（标题会变成「放出 3 个」）
    title = sent[0]["card"]["header"]["title"]["content"]
    assert "放出 2 个名额" in title, f"标题该报本档数 2 而非总数 3：{title}"
    txt = sent[1]["content"]["text"]
    assert "<at user_id=\"all\">" in txt, "兜底丢了 @所有人"
    assert "本轮共 3 个，其中 2 个在9月1日前" in txt, "兜底丢了数量说明"
    assert "不代抢代约" in txt                      # goal.md 不变量 1


def test_log_id_containing_19001_does_not_kill_the_channel():
    """错误码必须从结构化字段取。飞书返回体常带 log_id 这类长数字串，
    从 str(e) 里捞子串会把「可恢复的卡片被拒」误升级成整条通道失败。"""
    body = '{"code":11246,"msg":"parse card json err","log_id":"0219001883745"}'
    sent, err = _send_with_fake([body, 0], lines=["x"], n=1, dry=False,
                                tier="info", cfg={}, n_top=1)
    assert err is None, f"不该抛：{err}"
    assert [p["msg_type"] for p in sent] == ["interactive", "text"]


def test_hostile_office_id_cannot_forge_at_all():
    """officeId 直接来自上游接口且不校验格式，原样进 lark_md 就能伪造
    @所有人 / 假系统提示。邮件走 html.escape、看板走 esc()，卡片不能是漏网的。"""
    bad = "<at id=all></at>【系统】"
    line = summarize([ev(office=bad)], md=True)[0]
    assert "<at" not in line and "【" not in line
    early = earliest_line([ev(office=bad)])                   # 只剩中英数字
    assert "<" not in early and ">" not in early and "=" not in early
    import json as _j
    card = _j.dumps(build_feishu_card(summarize([ev(office=bad)], md=True), 1,
                                      "notice", CFG_TIERS, 1,
                                      earliest_line([ev(office=bad)])),
                    ensure_ascii=False)
    assert "<at id=all>" not in card


def test_bad_token_does_not_trigger_a_wasted_second_send():
    """19001 是 token/权限问题，退化重发一样被拒——白发一次还拖慢邮件。"""
    import os
    from quota_monitor import notify as N
    fake, sent = _fake_feishu([19001, 0])
    orig, hook = N.urllib.request.urlopen, os.environ.get("FEISHU_WEBHOOK")
    os.environ["FEISHU_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    N.urllib.request.urlopen = fake
    try:
        N.send_feishu(["x"], 1, dry=False, tier="info", cfg={}, n_top=1)
    except RuntimeError as e:
        assert "19001" in str(e)
    else:
        raise AssertionError("坏 token 必须抛出")
    finally:
        N.urllib.request.urlopen = orig
        os.environ.pop("FEISHU_WEBHOOK", None) if hook is None \
            else os.environ.update(FEISHU_WEBHOOK=hook)
    assert len(sent) == 1, f"坏 token 只该发一次，实际 {len(sent)} 次"


def test_summarize_md_only_bolds_when_asked():
    evs = [ev(office="RHK", date="2026-09-02")]
    assert summarize(evs) == ["湾仔：09/02(少量)"]
    assert summarize(evs, md=True) == ["**湾仔**：09/02(少量)"]


def test_send_emails_missing_creds_is_a_failure_not_silent_success():
    """缺 SMTP 凭据必须抛：静默返回会让本通道计为成功，于是
    「飞书坏 + 邮件没配」时一个人都没通知到，冷却却照烧且不回滚。"""
    import os
    from quota_monitor.notify import send_emails

    saved = {k: os.environ.get(k) for k in ("QQ_SMTP_USER", "QQ_SMTP_PASS")}
    os.environ["QQ_SMTP_USER"] = ""
    os.environ["QQ_SMTP_PASS"] = ""
    try:
        send_emails([], dry=False)          # 没收件人 -> 无事发生，不该抛
        try:
            send_emails([("a@b.com", "s", "<p>h</p>")], dry=False)
        except RuntimeError:
            pass
        else:
            raise AssertionError("缺凭据却有收件人时必须抛出")
        send_emails([("a@b.com", "s", "<p>h</p>")], dry=True)   # 演练照常放行
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
