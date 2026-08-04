"""本机尖兵：秒级放号监听（云端 2 分钟轮询的加速补充，不是替代）。

云端链路的延迟下限 = cron 间隔 + Actions 排队启动（10~30s）+ 邮件投递。
尖兵跑在你自己电脑上，绕过整段 CI：每 20 秒直连官方接口，检测到放号
立即推飞书群 + （紧急档）自动打开官方预约页。

用法（电脑开着就行，蹲守时段挂后台）：
    cd hkid-quota-monitor
    python sentinel.py            # webhook 从 ~/.hkid-quota-keys/feishu_webhook.txt 读
    SENTINEL_INTERVAL=30 python sentinel.py   # 自定义间隔（秒，下限 15）

边界：
- 只推飞书，不发邮件（邮件仍由云端发，SMTP 凭据不出 GitHub）
- 状态存 sentinel_data/（.gitignore 内），与仓库 data/ 互不干扰
- 与云端共享 config.json 的监测窗口/分级阈值
- 云端照常运行，同一次放号飞书里会先后出现尖兵（⚡前缀）和云端两条——
  这是双保险不是 bug；15 秒下限是对官方接口的礼貌底线，别调低
"""

from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

from quota_monitor import fetch as F
from quota_monitor import notify as N
from quota_monitor.diff import diff_snapshots
from quota_monitor.run import should_accept

HKT = timezone(timedelta(hours=8))
SDIR = Path("sentinel_data")
KEY_DIR = Path.home() / ".hkid-quota-keys"
MIN_INTERVAL = 15


def _load_webhook() -> str:
    hook = os.environ.get("FEISHU_WEBHOOK", "")
    f = KEY_DIR / "feishu_webhook.txt"
    if not hook and f.exists():
        hook = f.read_text(encoding="utf-8").strip()
    return hook


def one_round(state: dict, cfg: dict, dry: bool) -> None:
    quota_path = SDIR / "quota.json"
    old = None
    try:
        old = json.loads(quota_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pass
    prev_ts = F.source_ts(old) if old else 0.0

    # samples=2：抓到比现有新的立即收手（1 次请求）；打到旧节点才多试一次
    new = F.normalize(F.fetch_raw(samples=2, gap_sec=2.0, newer_than=prev_ts))
    F.validate_snapshot(new)

    now = datetime.now(HKT)
    now_src = F.ts_of(now.strftime("%m/%d/%Y %H:%M:%S"))
    frozen = (now_src - prev_ts) / 60 if prev_ts and now_src else 0.0
    accept, why = should_accept(prev_ts, F.source_ts(new), frozen)
    stamp = now.strftime("%H:%M:%S")
    if not accept:
        print(f"[{stamp}] 数据未推进（{why}）")
        return
    realign_only = why == "stale-valve"
    events = [] if realign_only else diff_snapshots(old, new)

    quota_path.write_text(json.dumps(new, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")

    fresh = [e for e in events if N.in_monitor_window(e["date"], cfg)]
    fresh = N.filter_events(fresh, state, cooldown_min=360)
    if not fresh:
        n_open = sum(1 for off in new["quota"].values()
                     for d in off.values() if d["R"] in "gy" or d["K"] in "gy")
        print(f"[{stamp}] 源 {new.get('source_update_time')} 无新放号（开放格 {n_open}）")
        return

    N.prune_state(state)
    (SDIR / "state.json").write_text(json.dumps(state, ensure_ascii=False),
                                     encoding="utf-8")
    tiers = [N.tier_of(e["date"], cfg) for e in fresh]
    tier = "urgent" if "urgent" in tiers else "notice" if "notice" in tiers else "info"
    lines = N.summarize(fresh, md=True)
    # ⚡ 前缀标明来源是尖兵，与稍后到的云端消息区分
    lines.insert(0, "⚡ 本机尖兵秒级检出（云端确认稍后到）")
    print(f"[{stamp}] 🎫 放号 {len(fresh)} 格 -> 推飞书")
    try:
        N.send_feishu(lines, len(fresh), dry, tier, cfg, tiers.count(tier), fresh)
    except Exception as e:  # noqa: BLE001 - 推送失败不中断监听
        print(f"WARN feishu failed: {e}")
    if tier == "urgent" and os.environ.get("SENTINEL_OPEN", "1") == "1":
        webbrowser.open(N.BOOKING)   # 紧急档直接把官方页拍到脸上，省下点链接的几秒


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    SDIR.mkdir(exist_ok=True)
    interval = max(MIN_INTERVAL, int(os.environ.get("SENTINEL_INTERVAL", "20")))
    dry = os.environ.get("DRY_RUN") == "1"

    hook = _load_webhook()
    if hook:
        os.environ["FEISHU_WEBHOOK"] = hook
    else:
        print("WARN 没找到飞书 webhook（环境变量或 ~/.hkid-quota-keys/feishu_webhook.txt），"
              "本轮尖兵将只打日志不推送")

    try:
        state = json.loads((SDIR / "state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        state = {}
    cfg = N.load_alert_cfg()
    print(f"尖兵启动：每 {interval}s 直连官方接口，窗口 {cfg.get('monitor_before', '不限')}，"
          f"Ctrl+C 停止")

    while True:
        try:
            one_round(state, cfg, dry)
        except KeyboardInterrupt:
            print("\n尖兵停止")
            return
        except Exception as e:  # noqa: BLE001 - 单轮失败照常下一轮，监听不许断
            print(f"WARN round failed: {e}")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n尖兵停止")
            return


if __name__ == "__main__":
    main()
