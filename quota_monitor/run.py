"""一轮完整巡检：读旧快照 -> 抓新 -> 校验 -> diff -> 落盘。CI 每 5 分钟调用一次。

产物（均在 data/）：
- quota.json      最新快照（看板数据源，内容确定性，不含抓取时间戳）
- quota_prev.json 上一轮快照（diff 基准）
- events.json     本轮变化事件（通知模块的输入）
- meta.json       抓取时间/新鲜度元信息（看板显示用）

CI 集成：
- 频率护栏：GITHUB_ACTIONS 下距上轮检查 < 4 分钟直接跳过
  （schedule 与外部 dispatch 触发碰撞时保住「≥5 分钟/次」的接口友好承诺）
- 提交决策：内容有变 / 首轮 / 心跳超时(20min) 才让 CI 提交，
  结果写 GITHUB_OUTPUT 的 commit 变量，避免纯时间戳提交灌爆仓库
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import fetch as fetch_mod
from .diff import diff_snapshots

HKT = timezone(timedelta(hours=8))
DATA = Path("data")
MIN_INTERVAL_MIN = 4
HEARTBEAT_MIN = 20


def _read_json(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _set_output(commit: bool) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"commit={'true' if commit else 'false'}\n")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    DATA.mkdir(exist_ok=True)
    now = datetime.now(HKT)

    prev_meta = _read_json(DATA / "meta.json") or {}
    if os.environ.get("GITHUB_ACTIONS") and prev_meta.get("last_check"):
        elapsed = (now - datetime.fromisoformat(prev_meta["last_check"])).total_seconds() / 60
        if 0 <= elapsed < MIN_INTERVAL_MIN:
            print(f"skip: last check {elapsed:.1f}min ago (< {MIN_INTERVAL_MIN}min)")
            _set_output(False)
            return

    quota_path = DATA / "quota.json"
    old = _read_json(quota_path)

    new = fetch_mod.normalize(fetch_mod.fetch_raw())
    fetch_mod.validate_snapshot(new)  # 残缺数据在此抛异常，绝不落盘毒化快照链
    events = diff_snapshots(old, new)

    content_changed = old is None or (
        old.get("quota") != new["quota"] or old.get("dates") != new["dates"])
    if content_changed:
        if old is not None:
            shutil.copyfile(quota_path, DATA / "quota_prev.json")
        quota_path.write_text(
            json.dumps(new, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")

    now_iso = now.isoformat(timespec="seconds")
    (DATA / "events.json").write_text(
        json.dumps({"generated_at": now_iso, "events": events},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")

    open_cells = sum(1 for off in new["quota"].values()
                     for d in off.values() if d["R"] in "gy" or d["K"] in "gy")
    meta = {
        "last_check": now_iso,
        "source_update_time": new.get("source_update_time"),
        "open_cells": open_cells,
        "events_this_run": len(events),
        "notify_worthy": sum(1 for e in events if e["type"] in ("quota_open", "new_date")),
    }
    (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                    encoding="utf-8")

    heartbeat_due = True
    if prev_meta.get("last_check"):
        heartbeat_due = (now - datetime.fromisoformat(prev_meta["last_check"])
                         ).total_seconds() / 60 >= HEARTBEAT_MIN
    _set_output(content_changed or heartbeat_due)
    print(f"OK open_cells={open_cells} events={len(events)} "
          f"notify_worthy={meta['notify_worthy']} changed={content_changed}")


if __name__ == "__main__":
    main()
