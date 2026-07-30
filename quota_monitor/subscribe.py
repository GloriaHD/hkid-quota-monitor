"""发邮件即订阅：IMAP 拉收件箱，登记/注销订阅者，回发确认邮件。

内地直连方案：订阅者给监控 QQ 邮箱发一封主题含「订阅」的邮件即完成订阅，
退订同理（主题或正文含「退订」）。全链路走国内邮件网络，无被墙依赖。

环境变量：
- QQ_SMTP_USER / QQ_SMTP_PASS : 收件 QQ 邮箱与授权码（IMAP/SMTP 同一授权码）
- SUBSCRIBER_KEY              : Fernet 密钥（生成：python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"）
- DRY_RUN=1                   : 不发确认邮件、不标已读
"""

from __future__ import annotations

import email
import email.header
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from pathlib import Path

HKT = timezone(timedelta(hours=8))
DATA = Path("data")
ENC_PATH = DATA / "subscribers.json.enc"
MAX_SUBSCRIBERS = 2000

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SUB_WORDS = ("订阅", "SUBSCRIBE", "subscribe")
UNSUB_WORDS = ("退订", "取消订阅", "UNSUBSCRIBE", "unsubscribe")

DASHBOARD = "https://cdn.jsdelivr.net/gh/chen1111-a/hkid-quota-monitor@main/index.html"


def classify(subject: str, body: str) -> str | None:
    """'subscribe' / 'unsubscribe' / None。退订词优先（\"取消订阅\"同时含两类词）。"""
    text = f"{subject}\n{body}"
    if any(w in text for w in UNSUB_WORDS):
        return "unsubscribe"
    if any(w in text for w in SUB_WORDS):
        return "subscribe"
    return None


def apply_change(subs: list[dict], addr: str, action: str,
                 now_iso: str) -> tuple[list[dict], bool]:
    """幂等更新名册。返回 (新名册, 是否发生变化)。"""
    addr = addr.strip().lower()
    if not EMAIL_RE.match(addr):
        return subs, False
    existing = next((s for s in subs if s.get("email") == addr), None)
    if action == "subscribe":
        if existing:
            if existing.get("active", True):
                return subs, False
            existing["active"] = True
            existing["updated"] = now_iso
            return subs, True
        if len([s for s in subs if s.get("active", True)]) >= MAX_SUBSCRIBERS:
            print(f"WARN roster full ({MAX_SUBSCRIBERS}), rejected {addr}")
            return subs, False
        subs.append({"email": addr, "active": True, "updated": now_iso})
        return subs, True
    if existing and existing.get("active", True):
        existing["active"] = False
        existing["updated"] = now_iso
        return subs, True
    return subs, False


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(os.environ["SUBSCRIBER_KEY"].encode())


def load_roster() -> list[dict]:
    if not ENC_PATH.exists():
        return []
    return json.loads(_fernet().decrypt(ENC_PATH.read_bytes()))


def save_roster(subs: list[dict]) -> None:
    ENC_PATH.write_bytes(_fernet().encrypt(
        json.dumps(subs, ensure_ascii=False).encode()))


def _decode_header(raw: str) -> str:
    parts = email.header.decode_header(raw or "")
    out = []
    for val, enc in parts:
        if isinstance(val, bytes):
            out.append(val.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(val)
    return "".join(out)


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          errors="replace")[:2000]
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8",
                              errors="replace")[:2000]
    return ""


def send_confirmation(user: str, pwd: str, addr: str, action: str, dry: bool) -> None:
    if action == "subscribe":
        subject = "✅ 订阅成功：香港ID预约放号提醒"
        html = (f"<p>订阅成功！之后任一办事处放出预约名额，会第一时间邮件通知你。</p>"
                f"<p>实时看板：<a href='{DASHBOARD}'>{DASHBOARD}</a></p>"
                f"<p style='color:#999;font-size:12px'>退订：回复本邮件，正文写「退订」。"
                f"第三方公益工具，非入境处官方服务。</p>")
    else:
        subject = "已退订：香港ID预约放号提醒"
        html = "<p>已为你退订，不会再收到放号提醒。想重新订阅随时再发「订阅」。</p>"
    if dry:
        print(f"[DRY] confirm({action}) -> {addr}")
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("香港ID配额监控", "utf-8")), user))
    msg["To"] = addr
    with smtplib.SMTP("smtp.qq.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pwd)
        s.sendmail(user, [addr], msg.as_string())
    print(f"confirm({action}) sent -> {addr}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    dry = os.environ.get("DRY_RUN") == "1"
    user = os.environ.get("QQ_SMTP_USER", "")
    pwd = os.environ.get("QQ_SMTP_PASS", "")
    if not user or not pwd or not os.environ.get("SUBSCRIBER_KEY"):
        print("skip subscribe: credentials/key not configured")
        return

    imap = imaplib.IMAP4_SSL("imap.qq.com", 993)
    try:
        imap.login(user, pwd)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        ids = data[0].split()
        if not ids:
            print("no new mail")
            return

        subs = load_roster()
        changed = False
        now_iso = datetime.now(HKT).isoformat(timespec="seconds")
        for mid in ids[:100]:
            _, msg_data = imap.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            addr = parseaddr(msg.get("From", ""))[1].lower()
            subject = _decode_header(msg.get("Subject", ""))
            action = classify(subject, _body_text(msg))
            if action and EMAIL_RE.match(addr) and addr != user.lower():
                subs, did = apply_change(subs, addr, action, now_iso)
                if did:
                    changed = True
                    try:
                        send_confirmation(user, pwd, addr, action, dry)
                    except Exception as e:  # noqa: BLE001 - 确认信失败不影响登记
                        print(f"WARN confirm mail failed for {addr}: {e}")
            if not dry:
                imap.store(mid, "+FLAGS", "\\Seen")

        if changed:
            save_roster(subs)
            n_active = sum(1 for s in subs if s.get("active", True))
            print(f"roster updated: {n_active} active subscribers")
        else:
            print("no roster change")
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
