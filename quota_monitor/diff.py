"""对比前后两份配额快照，产出变化事件。

事件类型：
- quota_open : 某办事处某日期某时段 已满/不开放 -> 有名额（r/x -> g/y）——通知的核心触发
- new_date   : 预约窗口滚动，新日期首次出现且有名额
- quota_gone : 有名额 -> 已满（看板展示用，不触发通知）
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HKT = timezone(timedelta(hours=8))

OPEN = ("g", "y")


def diff_snapshots(old: dict | None, new: dict) -> list[dict]:
    """返回事件列表。old 为 None（首次运行）时不产生事件。"""
    if not old:
        return []

    events: list[dict] = []
    detected_at = datetime.now(HKT).isoformat(timespec="seconds")
    old_quota = old.get("quota", {})
    old_dates = set(old.get("dates", []))

    def emit(etype: str, office: str, date: str, session: str,
             frm: str | None, to: str) -> None:
        events.append({
            "type": etype,
            "office": office,
            "date": date,
            "session": session,  # R=一般服务时段 K=延长服务时段
            "from": frm,
            "to": to,
            "detected_at": detected_at,
        })

    for office_id, by_date in new.get("quota", {}).items():
        old_by_date = old_quota.get(office_id, {})
        for date, cell in by_date.items():
            old_cell = old_by_date.get(date)
            for session in ("R", "K"):
                to = cell.get(session, "x")
                if old_cell is None:
                    # 新日期进入窗口：只报有名额的，避免每天滚动窗口刷屏
                    if date not in old_dates and to in OPEN:
                        emit("new_date", office_id, date, session, None, to)
                    continue
                frm = old_cell.get(session, "x")
                if frm == to:
                    continue
                if frm not in OPEN and to in OPEN:
                    emit("quota_open", office_id, date, session, frm, to)
                elif frm in OPEN and to not in OPEN:
                    emit("quota_gone", office_id, date, session, frm, to)
    events.sort(key=lambda e: (e["office"], e["date"], e["session"]))
    return events


def main(old_path: str = "data/quota_prev.json",
         new_path: str = "data/quota.json",
         out_path: str = "data/events.json") -> None:
    old = None
    if Path(old_path).exists():
        old = json.loads(Path(old_path).read_text(encoding="utf-8"))
    new = json.loads(Path(new_path).read_text(encoding="utf-8"))

    events = diff_snapshots(old, new)
    Path(out_path).write_text(
        json.dumps({"generated_at": datetime.now(HKT).isoformat(timespec="seconds"),
                    "events": events}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    n_open = sum(1 for e in events if e["type"] in ("quota_open", "new_date"))
    print(f"OK events={len(events)} notify_worthy={n_open}")


if __name__ == "__main__":
    main(*sys.argv[1:])
