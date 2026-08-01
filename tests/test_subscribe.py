"""订阅链路纯逻辑的可执行验收（spec phase-5 验收标准 1）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.subscribe import (_decode_header, apply_change, classify,
                                     describe_prefs, parse_prefs)

NOW = "2026-07-30T13:00:00+08:00"

# 看板 mailto 按钮预填的真实正文（与 index.html 保持同步）
MAILTO_BODY = "发送此邮件即完成订阅，无需修改内容。"


def test_classify():
    assert classify("订阅香港ID放号提醒", "") == "subscribe"
    assert classify("SUBSCRIBE", "") == "subscribe"
    assert classify("你好", "帮我订阅一下") == "subscribe"
    assert classify("退订", "") == "unsubscribe"
    assert classify("取消订阅", "") == "unsubscribe"   # 同含两类词，退订优先
    assert classify("Re: 通知", "收到了") is None


def test_classify_english_newsletter_not_registered():
    # 回归：英文营销邮件正文必含 subscribe/unsubscribe，曾把 noreply@ 误登记进名册
    assert classify("Weekly Digest", "Click here to unsubscribe from this list") is None
    assert classify("Welcome!", "Thanks for subscribing to our newsletter") is None


def test_machine_senders_blocked():
    from quota_monitor.subscribe import is_machine_sender
    assert is_machine_sender("noreply@xfx.life")
    assert is_machine_sender("hello@notify.railway.app")
    assert not is_machine_sender("13608174192@163.com")


def test_classify_real_mailto_is_subscribe():
    # 回归：曾因正文含「退订」示例文案被判成退订，旗舰功能整体不可用
    assert classify("订阅香港ID放号提醒", MAILTO_BODY) == "subscribe"


def test_classify_reply_quoting_footer_not_unsubscribe():
    # 回归：用户回复"谢谢"时邮件客户端引用原文，引文页脚含「退订」二字
    body = ("谢谢！\n\n"
            "在 2026年7月30日 写道：\n"
            "> 🎫 检测到 3 个预约名额放出\n"
            "> 想停止提醒：给本邮箱另发一封主题为「退订」的新邮件即可。\n")
    assert classify("Re: 🎫 香港ID预约放号：3 个名额", body) is None
    # 但用户自己正文写「退订」必须生效
    assert classify("Re: 🎫 香港ID预约放号：3 个名额", "退订\n\n> 引文…") == "unsubscribe"


def test_decode_header_bogus_charset_no_crash():
    # 回归：伪造非法字符集的毒邮件曾能永久卡死订阅链路
    assert isinstance(_decode_header("=?bogus-charset?B?5L2g5aW9?="), str)
    assert _decode_header("=?utf-8?B?6K6i6ZiF?=") == "订阅"


def test_subscribe_new_and_idempotent():
    subs, did = apply_change([], "A@Example.com", "subscribe", NOW)
    assert did and subs[0]["email"] == "a@example.com" and subs[0]["active"]
    subs2, did2 = apply_change(subs, "a@example.com", "subscribe", NOW)
    assert not did2 and len(subs2) == 1  # 重复订阅幂等


def test_unsubscribe_then_resubscribe():
    subs, _ = apply_change([], "a@example.com", "subscribe", NOW)
    subs, did = apply_change(subs, "a@example.com", "unsubscribe", NOW)
    assert did and subs[0]["active"] is False
    subs, did = apply_change(subs, "a@example.com", "unsubscribe", NOW)
    assert not did  # 重复退订幂等
    subs, did = apply_change(subs, "a@example.com", "subscribe", NOW)
    assert did and subs[0]["active"] is True and len(subs) == 1


def test_invalid_email_rejected():
    for bad in ("not-an-email", "a@b", "", "a b@c.com"):
        subs, did = apply_change([], bad, "subscribe", NOW)
        assert not did and subs == []


def test_unsubscribe_unknown_noop():
    subs, did = apply_change([], "x@y.com", "unsubscribe", NOW)
    assert not did and subs == []


DIY_BODY = ("订阅香港ID放号提醒（个性化）。\n"
            "只看办事处：港岛 九龙 将军澳 火炭 屯门 元朗 ←把不要的删掉\n"
            "只要这天之前的名额：2027-12-31 ←改成你的截止日期\n")


def test_parse_prefs_offices_and_date():
    p = parse_prefs("订阅 只看湾仔 长沙湾 2026-10-15之前")
    assert p == {"offices": ["RHK", "RKO"], "before": "2026-10-15"}
    assert parse_prefs("只要将军澳 2026年9月1日前")["before"] == "2026-09-01"
    assert parse_prefs("湾仔 2026/9/5")["offices"] == ["RHK"]  # 别名+斜杠日期


def test_parse_prefs_full_template_means_no_filter():
    # DIY 模板未编辑：6 局全在=不过滤；2027 远期日期无实际约束但保留
    p = parse_prefs(DIY_BODY)
    assert "offices" not in p and p["before"] == "2027-12-31"


def test_parse_prefs_garbage_falls_back_to_full():
    assert parse_prefs("订阅") == {}
    assert parse_prefs("2026-13-99 之前") == {}  # 非法日期丢弃


def test_apply_change_prefs_update_and_describe():
    subs, did = apply_change([], "a@b.com", "subscribe", NOW,
                             {"offices": ["RHK"], "before": "2026-10-01"})
    assert did and subs[0]["offices"] == ["RHK"] and subs[0]["before"] == "2026-10-01"
    # 同偏好重发=幂等
    subs, did = apply_change(subs, "a@b.com", "subscribe", NOW,
                             {"offices": ["RHK"], "before": "2026-10-01"})
    assert not did
    # 改偏好重发=更新
    subs, did = apply_change(subs, "a@b.com", "subscribe", NOW, {"offices": ["TMO"]})
    assert did and subs[0]["offices"] == ["TMO"] and "before" not in subs[0]
    assert describe_prefs({"offices": ["RHK", "RKO"], "before": "2026-10-01"}) == \
        "只看：湾仔、长沙湾；只要 2026-10-01 之前的名额"
    assert describe_prefs({}) == "全部办事处、全部日期"


def test_reply_to_our_confirmation_keeps_user_intent():
    """回归🔴：用户回复我们的确认信说「退订」，曾被主题里的「订阅」二字
    反向判成订阅，退订链路整体失效（三种场景本机复现过）。"""
    assert classify("Re: ✅ 已开启香港ID放号提醒", "退订") == "unsubscribe"
    assert classify("Re: ✅ 已开启香港ID放号提醒", "取消订阅") == "unsubscribe"
    assert classify("Re: 已停止香港ID放号提醒", "我要重新订阅") == "subscribe"
    # 即使旧版主题（含「订阅」二字）被带回，也必须只信正文
    assert classify("Re: ✅ 订阅成功：香港ID预约放号提醒", "退订") == "unsubscribe"


def test_english_unsubscribe_with_reply_prefix():
    """回归🔴：英文只认主题全等，任何客户端回复都带 Re: → 退订彻底不通。"""
    assert classify("Re: unsubscribe", "") == "unsubscribe"
    assert classify("Fwd: Unsubscribe", "") == "unsubscribe"
    assert classify("unsubscribe me", "") == "unsubscribe"
    assert classify("Re: subscribe", "") == "subscribe"
    # 英文营销邮件仍要挡住（正文匹配不放行）
    assert classify("Weekly Digest", "Click to unsubscribe from this list") is None


def test_roster_padding_hides_subscriber_count():
    """回归🟡：Fernet 无填充，密文长度与订阅人数呈线性关系，
    公开仓库里任何人都能反推有多少人订阅。"""
    import os
    import tempfile
    from pathlib import Path as P
    from cryptography.fernet import Fernet
    from quota_monitor import subscribe as S
    os.environ["SUBSCRIBER_KEY"] = Fernet.generate_key().decode()
    tmp = P(tempfile.mkdtemp())
    orig = S.ENC_PATH
    try:
        S.ENC_PATH = tmp / "r.enc"
        sizes = set()
        for n in (1, 3, 10, 30):
            S.save_roster([{"email": f"u{i}@x.com", "active": True} for i in range(n)])
            sizes.add(S.ENC_PATH.stat().st_size)
        assert len(sizes) == 1, f"密文长度仍泄露人数: {sizes}"
        S.save_roster([{"email": "a@b.com", "active": True}])
        assert S.load_roster() == [{"email": "a@b.com", "active": True}]
    finally:
        S.ENC_PATH = orig


def test_parse_prefs_single_day_lock():
    """滚轮 DIY 的「就这一天」：只有指定日期放号才通知。"""
    p = parse_prefs("订阅香港ID放号提醒（个性化）。\n就这一天放号才通知：2026-08-31\n")
    assert p.get("on") == ["2026-08-31"] and "before" not in p
    # 多个日期、多种写法、去重排序
    p = parse_prefs("指定日期：2026年8月31日 2026/09/02 2026-08-31")
    assert p.get("on") == ["2026-08-31", "2026-09-02"]


def test_parse_prefs_on_and_before_coexist():
    """同一封信里两种写法各归各：逐行判定，不能全文 search 只取第一个日期。"""
    p = parse_prefs("就这一天放号才通知：2026-08-31\n只要这天之前的名额：2026-09-15\n")
    assert p.get("on") == ["2026-08-31"]
    assert p.get("before") == "2026-09-15"


def test_parse_prefs_old_template_still_means_before():
    """老模板那行也含「这天」——判定顺序错了会把存量订阅者的截止日改成单日锁。"""
    p = parse_prefs("只看办事处：湾仔\n只要这天之前的名额：2026-10-15\n")
    assert p.get("before") == "2026-10-15" and "on" not in p
    # 裸日期（没有任何标记）沿用老语义
    assert parse_prefs("2026-10-15").get("before") == "2026-10-15"
    # 一行里两种标记都有（如「指定日期之前」）：之前 优先——说"之前"的人
    # 显然要的是范围，误判成单日锁会让他漏掉整个区间的推送
    p = parse_prefs("指定日期之前的名额都要：2026-10-15")
    assert p.get("before") == "2026-10-15" and "on" not in p


def test_parse_prefs_on_bad_date_falls_back():
    p = parse_prefs("就这一天：2026-02-30")     # 非法日期整个丢弃
    assert "on" not in p and "before" not in p
    p = parse_prefs("就这一天：2026-02-30 2026-08-31")   # 只丢非法的那个
    assert p.get("on") == ["2026-08-31"]


def test_event_matches_on_beats_before():
    """on 比 before 更严格，两者都设时以 on 为准。"""
    from quota_monitor.notify import event_matches
    sub = {"email": "a@b.com", "on": ["2026-08-31"], "before": "2026-09-15"}
    hit = {"office": "RHK", "date": "2026-08-31"}
    miss = {"office": "RHK", "date": "2026-09-01"}   # before 放行但 on 不放行
    assert event_matches(sub, hit)
    assert not event_matches(sub, miss)
    # 只设 before 的老订阅者行为不变
    old = {"email": "a@b.com", "before": "2026-09-15"}
    assert event_matches(old, miss)


def test_apply_change_updates_on_prefs():
    """重发订阅邮件=覆盖偏好，on 也要跟着增删，不能残留旧锁定日。"""
    subs, ch = apply_change([], "a@b.com", "subscribe", NOW,
                            {"on": ["2026-08-31"]})
    assert ch and subs[0]["on"] == ["2026-08-31"]
    subs, ch = apply_change(subs, "a@b.com", "subscribe", NOW,
                            {"before": "2026-09-15"})
    assert ch and "on" not in subs[0] and subs[0]["before"] == "2026-09-15"
    # 相同偏好重发 = 幂等
    subs, ch = apply_change(subs, "a@b.com", "subscribe", NOW,
                            {"before": "2026-09-15"})
    assert not ch


def test_describe_prefs_mentions_on():
    s = describe_prefs({"on": ["2026-08-31"], "offices": ["RHK"]})
    assert "2026-08-31" in s and "湾仔" in s and "这天放号" in s


def test_describe_prefs_matches_filter_semantics_when_both_set():
    """确认信是用户唯一核对机制，回显必须与 event_matches 实际行为一致：
    on 屏蔽 before 时不能把 before 说成还生效——那等于核对机制说谎。"""
    s = describe_prefs({"on": ["2026-08-31"], "before": "2026-09-15"})
    assert "以指定日为准" in s and "不生效" in s
    # 单独 before 不受影响
    assert describe_prefs({"before": "2026-09-15"}) == "只要 2026-09-15 之前的名额"


def test_on_pref_survives_roster_roundtrip():
    """接线测试：邮件解析 → 名册加密落盘 → notify 解密读取 → 过滤。
    load_subscribers 是白名单式取字段，漏了 on 键整条新功能会静默失效
    （测各环节的单元测试全绿，订阅者却照收他明确说不要的推送）。"""
    import os
    import tempfile
    from pathlib import Path as P
    from cryptography.fernet import Fernet
    from quota_monitor import notify as N
    from quota_monitor import subscribe as S
    os.environ["SUBSCRIBER_KEY"] = Fernet.generate_key().decode()
    tmp = P(tempfile.mkdtemp())
    orig_s, orig_n = S.ENC_PATH, N.DATA
    try:
        S.ENC_PATH = tmp / "subscribers.json.enc"
        N.DATA = tmp
        prefs = parse_prefs("就这一天放号才通知：2026-08-31")
        subs, _ = apply_change([], "a@b.com", "subscribe", NOW, prefs)
        S.save_roster(subs)
        loaded = N.load_subscribers()
        assert loaded and loaded[0]["on"] == ["2026-08-31"]
        assert N.event_matches(loaded[0], {"office": "RHK", "date": "2026-08-31"})
        assert not N.event_matches(loaded[0], {"office": "RHK", "date": "2026-09-01"})
    finally:
        S.ENC_PATH, N.DATA = orig_s, orig_n


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
