# -*- coding: utf-8 -*-
"""
realtime_check.py - 장중 5~10분마다 실행 (GitHub Actions cron)
candidates.json 의 후보 종목들에 대해, '오늘' 하루치 전체시장 스냅샷을
딱 1번의 API 호출로 받아온 뒤(효율성), 각 후보의 과거 데이터에 이어붙여
지표를 재계산합니다. 확정 신호(BUY/SELL)가 새로 뜨면 텔레그램으로 알림을
보내고, 같은 신호를 하루에 중복으로 보내지 않도록 alerted.json 에 기록합니다.

주의: pykrx가 제공하는 당일 데이터는 KRX 공식 집계 기준이라
     증권사 HTS 대비 몇 분 정도 지연이 있을 수 있습니다.
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from pykrx import stock as krx

sys.path.insert(0, ".")
import common

CANDIDATES_FILE = "candidates.json"
ALERTED_FILE = "alerted.json"
KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


def is_market_hours() -> bool:
    now = now_kst()
    if now.weekday() >= 5:  # 토,일
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
    alerted = common.load_json(ALERTED_FILE, {})
    if alerted.get("_date") != today:
        alerted = {"_date": today}  # 날짜 바뀌면 초기화

    print("오늘자 전체시장 스냅샷 조회 중...")
    try:
        today_snapshot = krx.get_market_ohlcv_by_ticker(today, market="ALL")
        today_snapshot = today_snapshot.rename(columns={
            "시가": "open", "고가": "high", "저가": "low",
            "종가": "close", "거래량": "volume",
        })
    except Exception as e:
        print(f"스냅샷 조회 실패: {e}")
        return

    if today_snapshot is None or today_snapshot.empty:
        print("오늘 데이터가 없습니다 (휴장일이거나 아직 집계 전).")
        return

    n_checked, n_alerted = 0, 0
    for c in data["candidates"]:
        code, name = c["code"], c["name"]
        if code not in today_snapshot.index:
            continue
        n_checked += 1

        hist = pd.DataFrame(c["history"])
        row = today_snapshot.loc[code]
        today_row = pd.DataFrame([{
            "date": today, "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row["volume"]),
        }])
        # 과거 데이터에 오늘자 진행 중인 데이터를 이어붙여 재계산
        df = pd.concat([hist[hist["date"] != today], today_row], ignore_index=True)
        df = df.sort_values("date").reset_index(drop=True)

        ind = common.add_indicators(df)
        ev = common.evaluate(ind)

        if ev["verdict"] not in ("BUY", "SELL"):
            continue

        key = f"{code}_{ev['verdict']}"
        if key in alerted:
            continue  # 오늘 이미 같은 방향으로 알림 보냄

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
