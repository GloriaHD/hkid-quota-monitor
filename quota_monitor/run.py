"""一轮完整巡检：读旧快照 -> 抓新 -> diff -> 落盘。CI 每 5 分钟调用一次。

产物（均在 data/）：
- quota.json      最新快照（看板数据源）
- quota_prev.json 上一轮快照（diff 基准）
- events.json     本轮变化事件（Phase 4 通知模块的输入）
- meta.json       轻量元信息（最后检查时间/是否有变化），看板显示新鲜度用
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import fetch as fetch_mod
from .diff import diff_snapshots

HKT = timezone(timedelta(hours=8))
DATA = Path("data")


def main() -> None:
    DATA.mkdir(exist_ok=True)
    quota_path = DATA / "quota.json"
    prev_path = DATA / "quota_prev.json"

    old = None
    if quota_path.exists():
        old = json.loads(quota_path.read_text(encoding="utf-8"))

    new = fetch_mod.normalize(fetch_mod.fetch_raw())
    events = diff_snapshots(old, new)

    if old is not None:
        shutil.copyfile(quota_path, prev_path)
    quota_path.write_text(json.dumps(new, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")

    now = datetime.now(HKT).isoformat(timespec="seconds")
    (DATA / "events.json").write_text(
        json.dumps({"generated_at": now, "events": events}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    open_cells = sum(1 for off in new["quota"].values()
                     for d in off.values() if d["R"] in "gy" or d["K"] in "gy")
    meta = {
        "last_check": now,
        "source_update_time": new.get("source_update_time"),
        "open_cells": open_cells,
        "events_this_run": len(events),
        "notify_worthy": sum(1 for e in events if e["type"] in ("quota_open", "new_date")),
    }
    (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    print(f"OK open_cells={open_cells} events={len(events)} "
          f"notify_worthy={meta['notify_worthy']}")


if __name__ == "__main__":
    main()
