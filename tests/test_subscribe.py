"""订阅链路纯逻辑的可执行验收（spec phase-5 验收标准 1）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.subscribe import _decode_header, apply_change, classify

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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
