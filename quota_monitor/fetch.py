"""抓取入境处配额查询接口，输出规范化快照 data/quota.json。

接口：GET https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation?svcId=579
svcId=579 = 智能身份证预约（quota-enquiry-client 前端同款，官方页面自身每 15 分钟刷新）。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

API_URL = "https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation"
SVC_ID = "579"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 quota-monitor/0.1 (+non-commercial watch tool)"
)
TIMEOUT = 30
MAX_RETRIES = 3

# quota-g 充足 / quota-y 少量 / quota-r 已满 / no-quotaX 该时段不开放
_STATUS_MAP = {"quota-g": "g", "quota-y": "y", "quota-r": "r"}


def _fetch_once(timeout: int = TIMEOUT, retries: int = MAX_RETRIES) -> dict:
    """单次拉取，带重试与指数退避。"""
    url = f"{API_URL}?svcId={SVC_ID}&t={int(time.time() * 1000)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": "https://eservices.es2.immd.gov.hk/es/quota-enquiry-client/",
        "Accept": "application/json",
    })
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 网络层任何错误都走同一退避
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)  # 2s, 4s
    raise RuntimeError(f"配额接口连续 {retries} 次失败: {last_err}")


def _update_ts(raw: dict) -> float:
    """把 lastUpdateTime 解析成可比较的时间戳，解析不了当作最旧。"""
    try:
        return datetime.strptime(raw["lastUpdateTime"], "%m/%d/%Y %H:%M:%S").timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def fetch_raw(samples: int = 3, gap_sec: float = 3.0) -> dict:
    """取样多次，采用 lastUpdateTime 最新的一份。

    官方接口是多节点负载均衡，各节点缓存进度不同——实测同一时刻不同节点
    的数据可差 2 分钟以上（表现为配额格反复横跳）。单次取样有概率打到旧节点，
    导致我们比别人晚发现放号；取两次挑最新的一份能显著削掉这段落后。"""
    best = _fetch_once()
    parsed_any = _update_ts(best) > 0
    for _ in range(max(0, samples - 1)):
        time.sleep(gap_sec)
        try:
            # 补采是锦上添花：单独用短超时/不重试，否则一次节点抽风
            # 就能把本轮拖过 2 分钟触发周期，形成监控空洞
            cand = _fetch_once(timeout=8, retries=1)
        except RuntimeError:
            break  # 已有一份可用数据，不因补采失败拖垮本轮
        parsed_any = parsed_any or _update_ts(cand) > 0
        if _update_ts(cand) > _update_ts(best):
            best = cand
    if samples > 1 and not parsed_any:
        print("WARN lastUpdateTime 全部无法解析，多取样已退化为单取样（官方可能改了格式）")
    return best


def _parse_status(cls: str) -> str:
    """CSS class -> 单字符状态：g/y/r，或 'x'（该时段不开放）。"""
    return _STATUS_MAP.get(cls, "x")


def _iso_date(mdy: str) -> str:
    """'07/31/2026' -> '2026-07-31'。"""
    return datetime.strptime(mdy, "%m/%d/%Y").strftime("%Y-%m-%d")


def normalize(raw: dict) -> dict:
    """把官方响应压成看板/diff 共用的快照结构。"""
    offices = []
    for o in raw.get("office", []):
        offices.append({
            "id": o["officeId"],
            "name": {
                "chs": o["chs"]["officeName"],
                "cht": o["cht"]["officeName"],
                "en": o["eng"]["officeName"],
            },
            "region_chs": o["chs"]["region"],
            "district_chs": o["chs"]["district"],
            "address_chs": o["chs"]["officeAddress"],
            "hint_chs": o["chs"]["officeHint"],
            "tel": o.get("telNum"),
        })

    quota: dict[str, dict[str, dict[str, str]]] = {}
    dates: set[str] = set()
    for row in raw.get("data", []):
        date = _iso_date(row["date"])
        dates.add(date)
        cell = quota.setdefault(row["officeId"], {})
        cell[date] = {
            "R": _parse_status(row.get("quotaR", "")),
            "K": _parse_status(row.get("quotaK", "")),
        }

    # 注意：快照内容保持确定性（不含抓取时间戳），抓取时间记在 meta.json；
    # 这样"内容没变就不提交"的判断才成立，仓库不会被纯时间戳提交灌爆
    return {
        "schema": 1,
        "source_update_time": raw.get("lastUpdateTime"),
        "offices": offices,
        "dates": sorted(dates),
        "quota": quota,
    }


def validate_snapshot(snap: dict) -> None:
    """接口偶发返回合法但空/残缺的 JSON（维护页、限流）。空快照一旦落盘，
    下一轮恢复正常时所有日期都会被判成 new_date，触发对全体订阅者的
    假放号群发——所以残缺快照必须让本轮直接失败，绝不入链。"""
    offices = snap.get("offices", [])
    quota = snap.get("quota", {})
    dates = snap.get("dates", [])
    if len(offices) < 6 or not quota or len(dates) < 30:
        raise RuntimeError(
            f"接口返回残缺数据（offices={len(offices)} dates={len(dates)} "
            f"quota_offices={len(quota)}），拒绝落盘")


def main(out_path: str = "data/quota.json") -> None:
    snapshot = normalize(fetch_raw())
    validate_snapshot(snapshot)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    n_open = sum(1 for off in snapshot["quota"].values()
                 for d in off.values() if d["R"] in "gy" or d["K"] in "gy")
    print(f"OK offices={len(snapshot['offices'])} dates={len(snapshot['dates'])} "
          f"open_slots={n_open} source_time={snapshot['source_update_time']}")


if __name__ == "__main__":
    main(*sys.argv[1:])
