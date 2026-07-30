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
DASHBOARD = "https://cdn.jsdelivr.net/gh/chen1111-a/hkid-quota-monitor@main/index.html"
BOOKING = "https://system.es2.immd.gov.hk/smartics2-client/ropbooking/zh-CN/eservices/makeAppointment/step1"


def _now() -> datetime:
    return datetime.now(HKT)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"cell_last_notified": {}}


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


def load_subscribers() -> list[str]:
    """解密订阅者列表（Phase 5 产物）。文件或密钥缺失返回空表。"""
    key = os.environ.get("SUBSCRIBER_KEY", "")
    enc = DATA / "subscribers.json.enc"
    if not key or not enc.exists():
        return []
    try:
        from cryptography.fernet import Fernet
        raw = Fernet(key.encode()).decrypt(enc.read_bytes())
        subs = json.loads(raw)
        return [s["email"] for s in subs if s.get("email") and s.get("active", True)]
    except Exception as e:  # noqa: BLE001 - 订阅表坏了不该阻塞管理员通知
        print(f"WARN subscribers decrypt failed: {e}")
        return []


def build_email_html(lines: list[str], n: int) -> str:
    items = "".join(f"<li style='margin:4px 0'>{ln}</li>" for ln in lines)
    return f"""<div style="font-family:system-ui,'PingFang SC','Microsoft YaHei';max-width:560px">
<h2 style="color:#0b57d0;margin:0 0 6px">🎫 检测到 {n} 个预约名额放出</h2>
<p style="color:#666;margin:0 0 12px">香港入境处智能身份证预约（检测时间 {_now().strftime('%m-%d %H:%M')} 港时）</p>
<ul style="padding-left:18px">{items}</ul>
<p style="margin:16px 0">
<a href="{BOOKING}" style="background:#0b57d0;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">立即去官方预约</a>
&nbsp;<a href="{DASHBOARD}" style="color:#0b57d0">查看实时看板</a></p>
<p style="color:#999;font-size:12px;line-height:1.6">名额变动很快，以官方预约页实际为准。<br>
第三方公益工具，非入境处官方服务。退订：回复本邮件，正文写「退订」。</p></div>"""


def send_email(recipients: list[str], subject: str, html: str, dry: bool) -> None:
    user = os.environ.get("QQ_SMTP_USER", "")
    pwd = os.environ.get("QQ_SMTP_PASS", "")
    if not recipients:
        print("skip email: no recipients")
        return
    if dry or not user or not pwd:
        print(f"[DRY] email -> {len(recipients)} 人: {subject}")
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("香港ID配额监控", "utf-8")), user))
    with smtplib.SMTP("smtp.qq.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pwd)
        # 逐封独立收件（BCC 语义），避免暴露订阅者列表
        for rcpt in recipients:
            del msg["To"]
            msg["To"] = rcpt
            s.sendmail(user, [rcpt], msg.as_string())
    print(f"email sent -> {len(recipients)} 人")


def send_feishu(lines: list[str], n: int, dry: bool) -> None:
    hook = os.environ.get("FEISHU_WEBHOOK", "")
    if not hook:
        print("skip feishu: no webhook")
        return
    text = (f"🎫 检测到 {n} 个香港ID预约名额放出\n" + "\n".join(lines) +
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

    lines = summarize(fresh)
    n = len(fresh)
    recipients = []
    admin = os.environ.get("ADMIN_EMAIL", "")
    if admin:
        recipients.append(admin)
    recipients += [r for r in load_subscribers() if r not in recipients]

    send_email(recipients, f"🎫 香港ID预约放号：{n} 个名额（{lines[0][:20]}…）"
               if len(lines[0]) > 20 else f"🎫 香港ID预约放号：{n} 个名额",
               build_email_html(lines, n), dry)
    send_feishu(lines, n, dry)

    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    print(f"OK notified={n} cells, state saved")


if __name__ == "__main__":
    main()
