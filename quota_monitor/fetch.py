"""抓取入境处配额查询接口，输出规范化快照 data/quota.json。

接口：GET https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation?svcId=579
svcId=579 = 智能身份证预约（quota-enquiry-client 前端同款，官方页面自身每 15 分钟刷新）。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

API_URL = "https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation"
SVC_ID = "579"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 quota-monitor/0.1 (+non-commercial watch tool)"
)
TIMEOUT = 30
MAX_RETRIES = 3

HKT = timezone(timedelta(hours=8))

# quota-g 充足 / quota-y 少量 / quota-r 已满 / no-quotaX 该时段不开放
_STATUS_MAP = {"quota-g": "g", "quota-y": "y", "quota-r": "r"}


def fetch_raw() -> dict:
    """带重试与指数退避地拉取原始 JSON。"""
    url = f"{API_URL}?svcId={SVC_ID}&t={int(time.time() * 1000)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": "https://eservices.es2.immd.gov.hk/es/quota-enquiry-client/",
        "Accept": "application/json",
    })
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 网络层任何错误都走同一退避
            last_err = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 2)  # 2s, 4s
    raise RuntimeError(f"配额接口连续 {MAX_RETRIES} 次失败: {last_err}")


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

    return {
        "schema": 1,
        "fetched_at": datetime.now(HKT).isoformat(timespec="seconds"),
        "source_update_time": raw.get("lastUpdateTime"),
        "offices": offices,
        "dates": sorted(dates),
        "quota": quota,
    }


def main(out_path: str = "data/quota.json") -> None:
    snapshot = normalize(fetch_raw())
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
