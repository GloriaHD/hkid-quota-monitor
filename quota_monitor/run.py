"""一轮完整巡检：读旧快照 -> 抓新 -> 校验 -> diff -> 落盘。CI 每 2 分钟调用一次。

产物（均在 data/）：
- quota.json      最新快照（看板数据源，内容确定性，不含抓取时间戳）
- quota_prev.json 上一轮快照（diff 基准）
- events.json     本轮变化事件（通知模块的输入）
- meta.json       抓取时间/新鲜度元信息（看板显示用）

CI 集成：
- 频率护栏：GITHUB_ACTIONS 下距上轮检查 < 1.5 分钟直接跳过
  （2 分钟 cron 与 schedule 兜底碰撞时兜底；1.5 而非 2.0 是为吸收调度抖动）
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
MIN_INTERVAL_MIN = 1.5   # 频率护栏：外部触发器抖动/重叠时的下限
HEARTBEAT_MIN = 20


def _read_json(path: Path) -> dict | None:
    """读不出来一律当作「没有上一轮」。这些文件会被并发轮次提交、也可能被
    人工编辑；若在此抛异常，崩溃点在写新 meta.json 之前，文件又被提交回仓库，
    会形成每轮必崩的自我延续中毒态（只能人工修仓库）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        print(f"WARN {path} 读取失败({e})，按无历史处理")
        return None


HISTORY_COOLDOWN_H = 6


def history_fresh(events: list[dict], existing_lines: list[str],
                  now: datetime) -> list[dict]:
    """放号规律统计的数据积累：只沉淀放号事件（quota_open/new_date），
    且同一格子 6 小时内只记一次——官方接口负载均衡抖动会让同一格
    反复 满↔有 横跳，裸记录一天能灌上万行噪声。"""
    # 只回看冷却窗口内的行：原来按固定行数截断，隐式耦合「办事处×日期」总数，
    # 窗口一变大冷却判定就会静默失效并反过来加速文件膨胀
    cutoff = (now - timedelta(hours=HISTORY_COOLDOWN_H * 2)).isoformat()
    recent: dict[str, str] = {}
    for ln in existing_lines[-20000:]:
        try:
            r = json.loads(ln)
            if str(r["detected_at"]) >= cutoff:
                recent[f'{r["office"]}|{r["date"]}|{r["session"]}'] = r["detected_at"]
        except (ValueError, KeyError, TypeError):
            continue
    out = []
    for e in events:
        if e["type"] not in ("quota_open", "new_date"):
            continue
        last = recent.get(f'{e["office"]}|{e["date"]}|{e["session"]}')
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() \
                        < HISTORY_COOLDOWN_H * 3600:
                    continue
            except (ValueError, TypeError):
                pass  # 坏时间戳当作无冷却；绝不能让一行脏历史每轮炸掉监控
        out.append(e)
    return out


def _append_history(events: list[dict], now: datetime) -> None:
    hp = DATA / "history.jsonl"
    existing = hp.read_text(encoding="utf-8").splitlines() if hp.exists() else []
    fresh = history_fresh(events, existing, now)
    if fresh:
        with open(hp, "a", encoding="utf-8") as f:
            for e in fresh:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"history +{len(fresh)} open events")


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
        try:
            elapsed = (now - datetime.fromisoformat(
                prev_meta["last_check"])).total_seconds() / 60
        except (TypeError, ValueError):
            elapsed = float("inf")
        if 0 <= elapsed < MIN_INTERVAL_MIN:
            print(f"skip: last check {elapsed:.1f}min ago (< {MIN_INTERVAL_MIN}min)")  # noqa: E501
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

    _append_history(events, now)

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
        try:
            heartbeat_due = (now - datetime.fromisoformat(prev_meta["last_check"])
                             ).total_seconds() / 60 >= HEARTBEAT_MIN
        except (TypeError, ValueError):
            heartbeat_due = True
    _set_output(content_changed or heartbeat_due)
    print(f"OK open_cells={open_cells} events={len(events)} "
          f"notify_worthy={meta['notify_worthy']} changed={content_changed}")


if __name__ == "__main__":
    main()
