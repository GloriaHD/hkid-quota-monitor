"""发邮件即订阅：IMAP 拉收件箱，登记/注销订阅者，回发确认邮件。

内地直连方案：订阅者给监控 QQ 邮箱发一封主题含「订阅」的邮件即完成订阅，
退订同理（主题或正文含「退订」）。全链路走国内邮件网络，无被墙依赖。

环境变量：
- QQ_SMTP_USER / QQ_SMTP_PASS : 收件 QQ 邮箱与授权码（IMAP/SMTP 同一授权码）
- SUBSCRIBER_KEY              : Fernet 密钥（生成：python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"）
- DRY_RUN=1                   : 不发确认邮件、不标已读（用 BODY.PEEK 读取，不影响未读态）
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

from .util import mask_email

HKT = timezone(timedelta(hours=8))
DATA = Path("data")
ENC_PATH = DATA / "subscribers.json.enc"
MAX_SUBSCRIBERS = 2000

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# 个性化订阅：中文别名（含繁体）→ 办事处局号
OFFICE_ALIASES = {
    "RHK": ("湾仔", "灣仔", "港岛", "港島"),
    "RKO": ("长沙湾", "長沙灣", "九龙", "九龍"),
    "RTK": ("将军澳", "將軍澳"),
    "FTO": ("火炭",),
    "TMO": ("屯门", "屯門"),
    "YLO": ("元朗",),
}
OFFICE_CN = {"RHK": "湾仔", "RKO": "长沙湾", "RTK": "将军澳",
             "FTO": "火炭", "TMO": "屯门", "YLO": "元朗"}
_DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")


def parse_prefs(text: str) -> dict:
    """从订阅邮件文本解析个性化偏好。看不懂的一律回退全量订阅——
    绝不因用户写法而丢订阅。

    - offices: 文本中提到的办事处（全部 6 个都提到 = 不过滤，兼容模板默认全清单）
    - before : 截止日期，只要这天之前的名额（支持 2026-10-15 / 2026/10/15 / 2026年10月15日）
    """
    prefs: dict = {}
    hits = [oid for oid, names in OFFICE_ALIASES.items()
            if any(n in text for n in names)]
    if hits and len(hits) < len(OFFICE_ALIASES):
        prefs["offices"] = sorted(hits)
    m = _DATE_RE.search(text)
    if m:
        try:
            from datetime import date
            prefs["before"] = date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            pass
    return prefs


def describe_prefs(prefs: dict) -> str:
    """把偏好翻译成人话，确认信里回显给用户核对。"""
    parts = []
    if prefs.get("offices"):
        parts.append("只看：" + "、".join(OFFICE_CN.get(o, o) for o in prefs["offices"]))
    if prefs.get("before"):
        parts.append(f"只要 {prefs['before']} 之前的名额")
    return "；".join(parts) if parts else "全部办事处、全部日期"

# fork 自部署时链接自动指向自己的仓库（CI 注入 GITHUB_REPOSITORY）
_REPO = os.environ.get("GITHUB_REPOSITORY", "chen1111-a/hkid-quota-monitor")
_OWNER, _NAME = _REPO.split("/", 1)
DASHBOARD = f"https://{_OWNER}.github.io/{_NAME}/"


_QUOTE_MARKERS = re.compile(
    r"^(>|On .+wrote:|在.+写道|-{2,}\s*(原始邮件|Original Message)|发件人[:：]|From[:：])")


def _strip_quoted(body: str) -> str:
    """去掉回复中的引用部分：遇到引用标记行即截断，> 开头行直接丢弃。

    没有这一步，通知/确认邮件页脚里的「退订」二字会随引文回流，
    把用户的任意回复（如"谢谢"）误判成退订请求。
    """
    kept = []
    for line in body.splitlines():
        s = line.strip()
        if _QUOTE_MARKERS.match(s):
            break
        if s.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


_NOREPLY_LOCALS = {"noreply", "no-reply", "donotreply", "do-not-reply", "bounce",
                   "mailer-daemon", "postmaster", "notify", "notifications",
                   "news", "newsletter", "hello", "info", "support"}

# 回复/转发前缀：用户回复本系统邮件时，客户端会把原主题带回来
_REPLY_PREFIX = re.compile(r"^\s*((re|fwd|fw|答复|回覆|回复|转发|轉發)\s*[:：]\s*)+", re.I)
_EN_SUB = re.compile(r"(un)?subscribe(\s+me)?", re.I)
# 本系统自己发出的邮件主题特征——被回复带回时不代表用户意图，必须忽略。
# 必须收窄到系统主题独有的形态：用户自己的订阅主题是「订阅香港ID放号提醒」，
# 若标记写成宽泛的「香港ID放号提醒」会把正常订阅也误杀。
_OUR_SUBJECT_MARKS = (
    "已开启香港ID放号提醒", "已停止香港ID放号提醒",   # 确认信
    "订阅成功：", "已退订：",                        # 旧版确认信（用户邮箱里可能还有）
    "紧急放号：", "香港ID放号：", "香港ID预约放号：",  # 放号通知信
)


def _strip_reply_prefix(subject: str) -> str:
    return _REPLY_PREFIX.sub("", subject or "").strip()


def is_machine_sender(addr: str) -> bool:
    """营销/系统邮件的典型发件地址——不当订阅者处理。
    实测收件箱会收到正文含 subscribe/unsubscribe 的英文机器邮件，
    不挡会把 noreply@ 之类登记进名册（确认信还会被退回）。"""
    return addr.split("@", 1)[0].lower() in _NOREPLY_LOCALS


def classify(subject: str, body: str) -> str | None:
    """'subscribe' / 'unsubscribe' / None。

    主题先剥掉 Re:/Fwd: 前缀；**若剥完是本系统自己的主题，则整个主题不代表
    用户意图，只信正文**——否则用户回复我们的确认信说「退订」，会被主题里的
    「订阅」二字反向判成订阅，退订链路直接失效（曾真实存在）。
    中文：主题优先于正文（正文只看非引用部分），同层级内退订词优先。
    英文：只接受主题整体匹配——英文营销邮件正文几乎必含 unsubscribe 字样。"""
    subj = _strip_reply_prefix(subject)
    m = _EN_SUB.fullmatch(subj)
    if m:
        return "unsubscribe" if m.group(1) else "subscribe"
    if any(mark in subj for mark in _OUR_SUBJECT_MARKS):
        subj = ""  # 是本系统发出的主题被带回，丢弃
    for text in (subj, _strip_quoted(body)):
        if any(w in text for w in ("退订", "取消订阅")):
            return "unsubscribe"
        if "订阅" in text:
            return "subscribe"
    return None


def apply_change(subs: list[dict], addr: str, action: str,
                 now_iso: str, prefs: dict | None = None) -> tuple[list[dict], bool]:
    """幂等更新名册。返回 (新名册, 是否发生变化)。
    重发订阅邮件 = 更新个性化偏好（prefs 总是覆盖为本次邮件解析结果）。"""
    addr = addr.strip().lower()
    prefs = prefs or {}
    if not EMAIL_RE.match(addr):
        return subs, False
    existing = next((s for s in subs if s.get("email") == addr), None)

    def _set_prefs(rec: dict) -> None:
        for k in ("offices", "before"):
            if prefs.get(k):
                rec[k] = prefs[k]
            else:
                rec.pop(k, None)

    if action == "subscribe":
        if existing:
            same_prefs = (existing.get("offices") == prefs.get("offices")
                          and existing.get("before") == prefs.get("before"))
            if existing.get("active", True) and same_prefs:
                return subs, False
            existing["active"] = True
            _set_prefs(existing)
            existing["updated"] = now_iso
            return subs, True
        if len([s for s in subs if s.get("active", True)]) >= MAX_SUBSCRIBERS:
            print(f"WARN roster full ({MAX_SUBSCRIBERS}), rejected {mask_email(addr)}")
            return subs, False
        rec = {"email": addr, "active": True, "updated": now_iso}
        _set_prefs(rec)
        subs.append(rec)
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
    return json.loads(_fernet().decrypt(ENC_PATH.read_bytes()).decode().rstrip())


_PAD_BLOCK = 4096


def save_roster(subs: list[dict]) -> None:
    """加密前填充到固定块大小。Fernet 不做长度填充，密文长度与条数呈线性
    关系（实测 1 条=204B），公开仓库里任何人都能据此推算订阅人数、并从
    提交时间轴看出谁在何时订阅——填充后只剩块级粒度。"""
    raw = json.dumps(subs, ensure_ascii=False).encode()
    pad = (-len(raw)) % _PAD_BLOCK
    ENC_PATH.write_bytes(_fernet().encrypt(raw + b" " * pad))


def _decode_header(raw: str) -> str:
    """RFC2047 头解码。字符集名是攻击者可控字段，非法名会抛 LookupError，
    必须兜住，否则一封毒邮件能永久卡死整条订阅链路。"""
    try:
        parts = email.header.decode_header(raw or "")
    except Exception:  # noqa: BLE001
        return raw or ""
    out = []
    for val, enc in parts:
        if isinstance(val, bytes):
            try:
                out.append(val.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(val.decode("utf-8", errors="replace"))
        else:
            out.append(val)
    return "".join(out)


def _body_text(msg: email.message.Message) -> str:
    def _safe_decode(payload: bytes, charset: str | None) -> str:
        try:
            return payload.decode(charset or "utf-8", errors="replace")[:2000]
        except LookupError:  # 非法字符集名同样是攻击者可控字段
            return payload.decode("utf-8", errors="replace")[:2000]

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return _safe_decode(payload, part.get_content_charset())
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        return _safe_decode(payload, msg.get_content_charset())
    return ""


def send_confirmation(user: str, pwd: str, addr: str, action: str, dry: bool,
                      prefs_desc: str = "") -> None:
    # 主题刻意不含「订阅/退订」二字：用户回复本信时客户端会带回原主题，
    # 含关键词会让 classify 误判成与用户意图相反的动作
    if action == "subscribe":
        subject = "✅ 已开启香港ID放号提醒"
        scope = prefs_desc or "全部办事处、全部日期"
        html = (f"<p>订阅成功！你的订阅范围：<b>{scope}</b>。"
                f"范围内一有名额放出，会第一时间邮件通知你。</p>"
                f"<p>想调整范围：重发一封订阅邮件写上新需求即可（如「订阅 只看湾仔 长沙湾 2026-10-15之前」）。</p>"
                f"<p>实时看板：<a href='{DASHBOARD}'>{DASHBOARD}</a></p>"
                f"<p style='color:#999;font-size:12px'>想停止提醒：给本邮箱另发一封主题为「退订」的新邮件即可。"
                f"第三方公益工具，非入境处官方服务。</p>")
    else:
        subject = "已停止香港ID放号提醒"
        html = ("<p>已为你停止提醒，不会再收到放号通知。"
                "想重新开启：给本邮箱另发一封主题为「订阅」的新邮件即可。</p>")
    if dry:
        print(f"[DRY] confirm({action}) -> {mask_email(addr)}")
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("香港ID配额监控", "utf-8")), user))
    msg["To"] = addr
    with smtplib.SMTP("smtp.qq.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pwd)
        s.sendmail(user, [addr], msg.as_string())
    print(f"confirm({action}) sent -> {mask_email(addr)}")


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
        n_changed = 0
        now_iso = datetime.now(HKT).isoformat(timespec="seconds")
        for mid in ids[:100]:
            # 单封邮件的任何解析/处理异常都不许炸穿批次：
            # 1) 已处理邮件的名册变更已即时落盘，不会因后续崩溃丢失
            # 2) finally 保证标已读，毒邮件不会每轮重复卡死链路
            try:
                # BODY.PEEK 才不会隐式置 \Seen——标已读的权力只留给下面的 finally，
                # 否则解析中途崩溃的邮件已被标已读，永不重试 = 静默丢订阅
                _, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
                msg = email.message_from_bytes(msg_data[0][1])
                addr = parseaddr(msg.get("From", ""))[1].lower()
                subject = _decode_header(msg.get("Subject", ""))
                body = _body_text(msg)
                action = classify(subject, body)
                if (action and EMAIL_RE.match(addr) and addr != user.lower()
                        and not is_machine_sender(addr)):
                    prefs = (parse_prefs(subject + "\n" + _strip_quoted(body))
                             if action == "subscribe" else {})
                    subs, did = apply_change(subs, addr, action, now_iso, prefs)
                    if did:
                        save_roster(subs)  # 即时保存，每封独立生效
                        n_changed += 1
                        try:
                            send_confirmation(user, pwd, addr, action, dry,
                                              describe_prefs(prefs))
                        except Exception as e:  # noqa: BLE001 - 确认信失败不影响登记
                            print(f"WARN confirm mail failed for {mask_email(addr)}: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"WARN skip malformed mail uid={mid!r}: {e}")
            finally:
                if not dry:
                    try:
                        imap.store(mid, "+FLAGS", "\\Seen")
                    except Exception as e:  # noqa: BLE001
                        print(f"WARN mark seen failed uid={mid!r}: {e}")

        if n_changed:
            n_active = sum(1 for s in subs if s.get("active", True))
            print(f"roster updated: {n_changed} changes, {n_active} active subscribers")
        else:
            print("no roster change")
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
