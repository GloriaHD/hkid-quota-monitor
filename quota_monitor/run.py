"""一轮完整巡检：读旧快照 -> 抓新 -> 校验 -> diff -> 落盘。CI 每 2 分钟调用一次。

产物（均在 data/）：
- quota.json      最新快照（看板数据源，内容确定性，不含抓取时间戳）
- quota_prev.json 上一轮快照（diff 基准）
- events.json     本轮变化事件（通知模块的输入）
- meta.json       抓取时间/新鲜度元信息（看板显示用）

CI 集成：
- 频率护栏：GITHUB_ACTIONS 下距上轮检查 < 0.75 分钟直接跳过
  （1 分钟 cron 与 schedule 兜底碰撞时兜底；0.75 而非 1.0 是为吸收调度抖动）
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
MIN_INTERVAL_MIN = 0.75  # 频率护栏：外部触发器抖动/重叠时的下限（支持 1 分钟 cron）
HEARTBEAT_MIN = 20
STALE_ACCEPT_MIN = 30    # 数据冻结超过此分钟数则放弃单调约束，避免永久停摆


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


def _append_history(events: list[dict], now: datetime,
                    src: str | None = None) -> None:
    hp = DATA / "history.jsonl"
    existing = hp.read_text(encoding="utf-8").splitlines() if hp.exists() else []
    fresh = history_fresh(events, existing, now)
    if fresh:
        with open(hp, "a", encoding="utf-8") as f:
            for e in fresh:
                # src=官方 lastUpdateTime：detected_at 是我们的轮询时刻，96% 落在
                # 偶数分钟（2min 网格量化），做分钟级放号规律分析必须用官方自己的时间
                f.write(json.dumps(dict(e, src=src) if src else e,
                                   ensure_ascii=False) + "\n")
        print(f"history +{len(fresh)} open events")


def _write_meta(now: datetime, snap: dict, stale: bool = False,
                events: int = 0, notify_worthy: int = 0) -> None:
    """写新鲜度元信息。stale=True 表示本轮抓到旧节点、数据未推进，但检查
    确实发生了；该轮是否真的落库由心跳决定（见 main 里的 heartbeat_due），
    这样「本站检查」不会停在上次数据推进的时刻而误报滞后。"""
    # 用 .get 而非硬下标：skip 分支传进来的是未经 validate_snapshot 的旧快照
    open_cells = sum(1 for off in snap.get("quota", {}).values()
                     for d in off.values()
                     if d.get("R") in ("g", "y") or d.get("K") in ("g", "y"))
    (DATA / "meta.json").write_text(json.dumps({
        "last_check": now.isoformat(timespec="seconds"),
        "source_update_time": snap.get("source_update_time"),
        "open_cells": open_cells,
        "events_this_run": events,
        "notify_worthy": notify_worthy,
        "stale_node_skipped": stale,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


COMMIT_SPACING_MIN = 3.0


def should_commit(content_changed: bool, notify_worthy: int,
                  since_commit_min: float, heartbeat_due: bool,
                  first_run: bool) -> bool:
    """本轮要不要让 CI 提交数据。1 分钟 cron 后不能每轮都提交：
    每轮提交 = 每天 1440 commit + 每分钟一次 Pages 构建（官方软限 10 次/小时），
    还会以每分钟一次的频率触发 jsDelivr 缓存清理。

    普通内容变化按 COMMIT_SPACING_MIN 间隔攒批提交（看板显示最多晚 3 分钟）。
    但有放号事件（notify_worthy>0）必须立即提交——通知一旦发出，冷却状态
    notify_state.json 必须随之落库，否则下一轮重新 diff 出同一批事件、
    冷却又不在，会对订阅者重复轰炸。"""
    if first_run or heartbeat_due:
        return True
    if not content_changed:
        return False
    return notify_worthy > 0 or since_commit_min >= COMMIT_SPACING_MIN


def should_accept(prev_ts: float, new_ts: float,
                  frozen_min: float) -> tuple[bool, str]:
    """本轮抓到的快照要不要接受？返回 (是否接受, 原因码)。

    抽成独立函数是为了能被测试直接调用——判定逻辑内联在 main 里时，
    测试只能抄一遍条件，那种影子实现改坏了也不会红。
    原因码：advanced=数据推进 / stale-valve=冻结太久放行对齐 /
    no-baseline=无基线 / older=旧节点 / same=数据未推进
    """
    if not prev_ts or not new_ts:
        return True, "no-baseline"
    if new_ts > prev_ts:
        return True, "advanced"
    if frozen_min >= STALE_ACCEPT_MIN:
        return True, "stale-valve"
    return False, "same" if new_ts == prev_ts else "older"


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
    prev_ts = fetch_mod.source_ts(old) if old else 0.0

    new = fetch_mod.normalize(fetch_mod.fetch_raw(newer_than=prev_ts))
    fetch_mod.validate_snapshot(new)  # 残缺数据在此抛异常，绝不落盘毒化快照链

    # 数据只进不退：官方多节点缓存进度不同（实测两节点相位差 6/9 分钟交替），
    # 打到旧节点时若照单全收，看板会在两个时间点的状态间反复横跳，还会产出
    # 「时光倒流」的虚假放号/关闭事件。不比现有更新的一律丢弃。
    new_ts = fetch_mod.source_ts(new)
    now_src = fetch_mod.ts_of(now.strftime("%m/%d/%Y %H:%M:%S"))
    frozen_min = (now_src - prev_ts) / 60 if prev_ts and now_src else 0.0
    accept, why = should_accept(prev_ts, new_ts, frozen_min)

    # CI 里 prev_meta 来自上次提交的 checkout，故 last_check 距今 ≈ 距上次提交
    since_commit = float("inf")
    if prev_meta.get("last_check"):
        try:
            since_commit = (now - datetime.fromisoformat(prev_meta["last_check"])
                            ).total_seconds() / 60
        except (TypeError, ValueError):
            pass
    # 心跳同样适用于「本轮不更新」：否则看板的「本站检查」会停在上次数据推进
    # 的时刻，官方冻结时会挂出「检查滞后」的假警报（其实每分钟都在检查）
    heartbeat_due = since_commit >= HEARTBEAT_MIN

    if not accept:
        label = "数据未推进" if why == "same" else "抓到较旧节点"
        print(f"skip: {label}（{new.get('source_update_time')} "
              f"<= 现有 {old.get('source_update_time')}），本轮不更新")
        _write_meta(now, old, stale=True)
        _set_output(heartbeat_due)
        return

    # 安全阀放行的是「比现有更旧」的数据，只用于重新对齐节点，
    # 绝不能拿去 diff——那正是本要消灭的假放号事件，还会真发通知、污染历史统计
    realign_only = why == "stale-valve"
    if realign_only:
        print(f"WARN 现有数据已冻结 {frozen_min:.0f} 分钟，放行本次抓取以重新对齐节点"
              f"（本轮不产出事件、不通知）")
    events = [] if realign_only else diff_snapshots(old, new)

    content_changed = old is None or (
        old.get("quota") != new["quota"] or old.get("dates") != new["dates"])
    if content_changed:
        if old is not None:
            shutil.copyfile(quota_path, DATA / "quota_prev.json")
        quota_path.write_text(
            json.dumps(new, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")

    _append_history(events, now, new.get("source_update_time"))

    now_iso = now.isoformat(timespec="seconds")
    (DATA / "events.json").write_text(
        json.dumps({"generated_at": now_iso, "events": events},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")

    open_cells = sum(1 for off in new["quota"].values()
                     for d in off.values() if d["R"] in "gy" or d["K"] in "gy")
    notify_worthy = sum(1 for e in events if e["type"] in ("quota_open", "new_date"))
    _write_meta(now, new, stale=False, events=len(events),
                notify_worthy=notify_worthy)

    commit = should_commit(content_changed, notify_worthy, since_commit,
                           heartbeat_due, first_run=old is None)
    _set_output(commit)
    print(f"OK open_cells={open_cells} events={len(events)} "
          f"notify_worthy={notify_worthy} changed={content_changed} commit={commit}")


if __name__ == "__main__":
    main()
