"""订阅链路纯逻辑的可执行验收（spec phase-5 验收标准 1）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor.subscribe import apply_change, classify

NOW = "2026-07-30T13:00:00+08:00"


def test_classify():
    assert classify("订阅香港ID放号提醒", "") == "subscribe"
    assert classify("SUBSCRIBE", "") == "subscribe"
    assert classify("你好", "帮我订阅一下") == "subscribe"
    assert classify("退订", "") == "unsubscribe"
    assert classify("取消订阅", "") == "unsubscribe"   # 同含两类词，退订优先
    assert classify("Re: 通知", "收到了") is None


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
