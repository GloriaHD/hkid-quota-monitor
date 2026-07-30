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
    p = parse_prefs("订阅 只看港岛 九龙 2026-10-15之前")
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
        "只看：港岛、九龙；只要 2026-10-01 之前的名额"
    assert describe_prefs({}) == "全部办事处、全部日期"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
