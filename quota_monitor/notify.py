"""放号通知：events.json -> 防抖过滤 -> 聚合 -> QQ SMTP 邮件 + 飞书 webhook。

环境变量（CI secrets 注入；全部缺省时静默跳过，不让 CI 失败）：
- QQ_SMTP_USER / QQ_SMTP_PASS : 发件 QQ 邮箱与 SMTP 授权码
- ADMIN_EMAIL                 : 管理员收件邮箱（必收）
- FEISHU_WEBHOOK              : 飞书群自定义机器人 webhook URL
- SUBSCRIBER_KEY              : 订阅者文件 Fernet 解密钥（Phase 5）
- NOTIFY_COOLDOWN_MIN         : 单格冷却分钟数，默认 360
- DRY_RUN=1                   : 打印代替真实发送
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

HKT = timezone(timedelta(hours=8))
DATA = Path("data")
STATE_PATH = DATA / "notify_state.json"

OFFICE_NAMES = {"FTO": "火炭", "RHK": "港岛(湾仔)", "RKO": "九龙", "RTK": "将军澳",
                "TMO": "屯门", "YLO": "元朗"}
STATUS_TEXT = {"g": "充足", "y": "少量"}
# fork 自部署时链接自动指向自己的仓库（CI 注入 GITHUB_REPOSITORY）
REPO = os.environ.get("GITHUB_REPOSITORY", "chen1111-a/hkid-quota-monitor")
_OWNER, _NAME = REPO.split("/", 1)
DASHBOARD = f"https://{_OWNER}.github.io/{_NAME}/"
BOOKING = "https://www.gov.hk/tc/residents/immigration/idcard/hkic/bookregidcard.htm"


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load_alert_cfg(path: str = "config.json") -> dict:
    """读分级提醒阈值。config 是用户网页直编的不可信输入：
    文件缺失/坏 JSON/非字符串/非 ISO 格式一律丢弃该键（回退为无分级），
    绝不让一次手滑编辑炸掉通知链路；两阈值填反时自动对调。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    cfg = {k: v for k, v in raw.items()
           if k in ("urgent_before", "notice_before")
           and isinstance(v, str) and _ISO_DATE.fullmatch(v)}
    u, n = cfg.get("urgent_before"), cfg.get("notice_before")
    if u and n and u > n:
        cfg["urgent_before"], cfg["notice_before"] = n, u
    return cfg


def tier_of(date: str, cfg: dict) -> str:
    """'urgent' / 'notice' / 'info'。ISO 日期字符串可直接比较；等于阈值日不算。"""
    if cfg.get("urgent_before") and date < cfg["urgent_before"]:
        return "urgent"
    if cfg.get("notice_before") and date < cfg["notice_before"]:
        return "notice"
    return "info"


def _now() -> datetime:
    return datetime.now(HKT)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"cell_last_notified": {}}


def prune_state(state: dict, today: str | None = None) -> None:
    """剪掉已过期日期的冷却键，防止 state 文件无界增长。"""
    today = today or _now().strftime("%Y-%m-%d")
    cells = state.get("cell_last_notified", {})
    for key in [k for k in cells if k.split("|")[1] < today]:
        del cells[key]


def filter_events(events: list[dict], state: dict,
                  cooldown_min: int, now: datetime | None = None) -> list[dict]:
    """只留通知级事件，且冷却期外；就地更新 state 的最近通知时间。"""
    now = now or _now()
    out = []
    cells = state.setdefault("cell_last_notified", {})
    for e in events:
        if e["type"] not in ("quota_open", "new_date"):
            continue
        key = f'{e["office"]}|{e["date"]}|{e["session"]}'
        last = cells.get(key)
        if last:
            elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
            if elapsed < cooldown_min:
                continue
        cells[key] = now.isoformat(timespec="seconds")
        out.append(e)
    return out


def summarize(events: list[dict]) -> list[str]:
    """按办事处聚合成人话行：港岛(湾仔)：09/02、09/03（少量）"""
    by_office: dict[str, list[dict]] = {}
    for e in events:
        by_office.setdefault(e["office"], []).append(e)
    lines = []
    for off in sorted(by_office):
        evs = sorted(by_office[off], key=lambda x: x["date"])
        parts = []
        for e in evs:
            d = e["date"][5:].replace("-", "/")
            tag = STATUS_TEXT.get(e["to"], e["to"])
            sess = "延长时段" if e["session"] == "K" else ""
            parts.append(f"{d}{sess}({tag})")
        lines.append(f"{OFFICE_NAMES.get(off, off)}：{'、'.join(parts)}")
    return lines


def load_subscribers() -> list[dict]:
    """解密订阅者列表（含个性化偏好）。文件或密钥缺失返回空表。
    旧记录无 offices/before 字段 = 全量订阅（向后兼容）。"""
    key = os.environ.get("SUBSCRIBER_KEY", "")
    enc = DATA / "subscribers.json.enc"
    if not key or not enc.exists():
        return []
    try:
        from cryptography.fernet import Fernet
        raw = Fernet(key.encode()).decrypt(enc.read_bytes())
        subs = json.loads(raw)
        return [{"email": s["email"], "offices": s.get("offices"),
                 "before": s.get("before")}
                for s in subs if s.get("email") and s.get("active", True)]
    except Exception as e:  # noqa: BLE001 - 订阅表坏了不该阻塞管理员通知
        print(f"WARN subscribers decrypt failed: {e}")
        return []


def event_matches(sub: dict, e: dict) -> bool:
    """个性化过滤：订阅者未设的维度不过滤。"""
    if sub.get("offices") and e["office"] not in sub["offices"]:
        return False
    if sub.get("before") and e["date"] >= sub["before"]:
        return False
    return True


def compose(events: list[dict], cfg: dict) -> tuple[str, str]:
    """一组事件 -> (邮件主题, 邮件 HTML)。分级取组内最高档。"""
    lines = summarize(events)
    tiers = [tier_of(e["date"], cfg) for e in events]
    tier = "urgent" if "urgent" in tiers else "notice" if "notice" in tiers else "info"
    n_top = tiers.count(tier)
    subject = {
        "urgent": f"🚨 紧急放号：{cfg.get('urgent_before', '近期')} 前有名额！（{n_top} 个）",
        "notice": f"🔔 香港ID放号：{len(events)} 个名额（含 {cfg.get('notice_before', '近期')} 前）",
        "info": f"🎫 香港ID预约放号：{len(events)} 个名额",
    }[tier]
    return subject, build_email_html(lines, len(events), tier, cfg)


def build_email_html(lines: list[str], n: int, tier: str = "info",
                     cfg: dict | None = None) -> str:
    items = "".join(f"<li style='margin:4px 0'>{ln}</li>" for ln in lines)
    cfg = cfg or {}
    if tier == "urgent":
        head_color, head = "#d03b3b", (f"🚨 {cfg.get('urgent_before', '近期')} 前有名额！"
                                       f"共 {n} 个放出，手慢无")
    elif tier == "notice":
        head_color, head = "#b8860b", (f"🔔 {cfg.get('notice_before', '近期')} 前有名额，"
                                       f"共 {n} 个放出")
    else:
        head_color, head = "#0b57d0", f"🎫 检测到 {n} 个预约名额放出"
    return f"""<div style="font-family:system-ui,'PingFang SC','Microsoft YaHei';max-width:560px">
<h2 style="color:{head_color};margin:0 0 6px">{head}</h2>
<p style="color:#666;margin:0 0 12px">香港入境处智能身份证预约（检测时间 {_now().strftime('%m-%d %H:%M')} 港时）</p>
<ul style="padding-left:18px">{items}</ul>
<p style="margin:16px 0">
<a href="{BOOKING}" style="background:#0b57d0;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">立即去官方预约/改期</a>
&nbsp;<a href="{DASHBOARD}" style="color:#0b57d0">查看实时看板</a></p>
<p style="color:#999;font-size:12px;line-height:1.6">名额变动很快，以官方预约页实际为准。<br>
第三方公益工具，非入境处官方服务。想停止提醒：给本邮箱另发一封主题为「退订」的新邮件即可。</p></div>"""


def send_emails(payloads: list[tuple[str, str, str]], dry: bool) -> None:
    """发送 (收件人, 主题, HTML) 列表——逐人独立内容（个性化）也逐人独立容错。"""
    user = os.environ.get("QQ_SMTP_USER", "")
    pwd = os.environ.get("QQ_SMTP_PASS", "")
    if not payloads:
        print("skip email: no recipients")
        return
    if dry or not user or not pwd:
        for rcpt, subject, _ in payloads:
            print(f"[DRY] email -> {rcpt}: {subject}")
        return
    sent = failed = 0
    with smtplib.SMTP("smtp.qq.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pwd)
        for rcpt, subject, html in payloads:
            msg = MIMEText(html, "html", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = formataddr((str(Header("香港ID配额监控", "utf-8")), user))
            msg["To"] = rcpt
            try:
                s.sendmail(user, [rcpt], msg.as_string())
                sent += 1
            except smtplib.SMTPException as e:
                failed += 1
                print(f"WARN send to {rcpt} failed: {e}")
    print(f"email sent -> {sent} ok, {failed} failed")


def send_feishu(lines: list[str], n: int, dry: bool, tier: str = "info",
                cfg: dict | None = None) -> None:
    hook = os.environ.get("FEISHU_WEBHOOK", "")
    if not hook:
        print("skip feishu: no webhook")
        return
    if not hook.startswith("https://"):
        print("skip feishu: webhook must be https")
        return
    cfg = cfg or {}
    if tier == "urgent":
        # @所有人需群机器人开启「允许 @ 所有人」，未开启时飞书按普通文本展示
        headline = (f'<at user_id="all">所有人</at> 🚨 '
                    f"{cfg.get('urgent_before', '近期')} 前有名额！{n} 个放出，速抢")
    elif tier == "notice":
        headline = f"🔔 {cfg.get('notice_before', '近期')} 前有名额，{n} 个放出"
    else:
        headline = f"🎫 检测到 {n} 个香港ID预约名额放出"
    text = (headline + "\n" + "\n".join(lines) +
            f"\n\n官方预约：{BOOKING}\n实时看板：{DASHBOARD}")
    if dry:
        print(f"[DRY] feishu:\n{text}")
        return
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(hook, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(f"feishu sent: HTTP {resp.status}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    dry = os.environ.get("DRY_RUN") == "1"
    ev_path = DATA / "events.json"
    if not ev_path.exists():
        print("skip: no events.json")
        return
    events = json.loads(ev_path.read_text(encoding="utf-8")).get("events", [])
    cooldown = int(os.environ.get("NOTIFY_COOLDOWN_MIN", "360"))

    state = load_state()
    fresh = filter_events(events, state, cooldown)
    if not fresh:
        print("skip: no notify-worthy events after cooldown filter")
        return

    n = len(fresh)
    # 冷却状态先落盘再发送：单通道抛异常时另一通道已发出的通知
    # 不会因 state 丢失而在下一轮重复轰炸（宁可漏一轮，不可炸订阅者）
    prune_state(state)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")

    cfg = load_alert_cfg()
    # 逐人个性化：管理员收全量；订阅者只收自己偏好范围内的事件，无匹配不打扰
    payloads: list[tuple[str, str, str]] = []
    admin = os.environ.get("ADMIN_EMAIL", "").lower()
    if admin:
        subject, html = compose(fresh, cfg)
        payloads.append((admin, subject, html))
    skipped = 0
    for sub in load_subscribers():
        if sub["email"] == admin:
            continue  # 管理员已收全量
        sub_ev = [e for e in fresh if event_matches(sub, e)]
        if not sub_ev:
            skipped += 1
            continue
        subject, html = compose(sub_ev, cfg)
        payloads.append((sub["email"], subject, html))
    if skipped:
        print(f"personalized: {skipped} subscribers had no matching events")

    full_subject_tier = [tier_of(e["date"], cfg) for e in fresh]
    tier = ("urgent" if "urgent" in full_subject_tier
            else "notice" if "notice" in full_subject_tier else "info")
    for send in (lambda: send_emails(payloads, dry),
                 lambda: send_feishu(summarize(fresh), n, dry, tier, cfg)):
        try:
            send()
        except Exception as e:  # noqa: BLE001 - 通道间互不拖累
            print(f"WARN notify channel failed: {e}")
    print(f"OK notified={n} cells, {len(payloads)} emails")


if __name__ == "__main__":
    main()
