# -*- coding: utf-8 -*-
"""
realtime_check.py (v2) - 장중 10분마다 실행
데이터 출처: FinanceDataReader(네이버) - 해외(GitHub) 서버에서도 동작.
candidates.json 후보(최대 80개)만 오늘 시세를 다시 받아 재평가하고,
확정 신호가 새로 뜨면 텔레그램 알림. 하루 중복 알림 방지.
"""
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import FinanceDataReader as fdr

sys.path.insert(0, ".")
import common

KST = ZoneInfo("Asia/Seoul")
CANDIDATES_FILE = "candidates.json"
ALERTED_FILE = "alerted.json"
REQ_SLEEP = 0.05


def now_kst() -> datetime:
    return datetime.now(KST)


def is_market_hours() -> bool:
    now = now_kst()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 900 <= hm <= 1530


def main():
    if not is_market_hours():
        print("장 운영 시간이 아니므로 건너뜁니다.")
        return

    data = common.load_json(CANDIDATES_FILE, None)
    if not data or not data.get("candidates"):
        print("후보 목록이 없습니다. daily_scan.py를 먼저 실행하세요.")
        return

    today = now_kst().strftime("%Y%m%d")
    fetch_start = (now_kst() - timedelta(days=10)).strftime("%Y-%m-%d")

    alerted = common.load_json(ALERTED_FILE, {})
    if alerted.get("_date") != today:
        alerted = {"_date": today}

    n_checked, n_alerted = 0, 0
    for c in data["candidates"]:
        code, name = c["code"], c["name"]

        # 최근 며칠치(오늘 포함)만 새로 조회
        try:
            recent = fdr.DataReader(code, fetch_start)
        except Exception:
            continue
        time.sleep(REQ_SLEEP)
        if recent is None or recent.empty:
            continue

        recent = recent.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })[["open", "high", "low", "close", "volume"]].dropna()
        recent = recent.reset_index()
        date_col = recent.columns[0]
        recent["date"] = pd.to_datetime(recent[date_col]).dt.strftime("%Y%m%d")
        recent = recent[["date", "open", "high", "low", "close", "volume"]]

        if recent["date"].iloc[-1] != today:
            # 오늘 데이터가 아직 없음 (개장 직후 등)
            continue
        n_checked += 1

        hist = pd.DataFrame(c["history"])
        # 과거 저장분에서 최근 조회분과 겹치는 날짜 제거 후 병합
        hist = hist[~hist["date"].isin(set(recent["date"]))]
        df = pd.concat([hist, recent], ignore_index=True).sort_values("date").reset_index(drop=True)

        ind = common.add_indicators(df)
        ev = common.evaluate(ind)

        if ev["verdict"] not in ("BUY", "SELL"):
            continue

        key = f"{code}_{ev['verdict']}"
        if key in alerted:
            continue

        tp = common.price_targets(ev["close"], ev["atr14"], ev["verdict"])
        msg = common.format_alert(code, name, ev, tp)
        common.send_telegram(msg)
        alerted[key] = now_kst().isoformat()
        n_alerted += 1
        print(f"  [알림 발송] {name}({code}) {ev['verdict']}")

    common.save_json(ALERTED_FILE, alerted)
    print(f"\n체크 완료: {n_checked}개 후보 확인, {n_alerted}건 신규 알림 발송")


if __name__ == "__main__":
    main()
