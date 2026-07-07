# -*- coding: utf-8 -*-
"""
daily_scan.py - 장 시작 전 1회 실행
코스피 + 코스닥 전체 종목의 과거 데이터를 모아 지표를 계산하고,
엄격 기준(추세필터+다중지표확인+거래량검증)을 통과했거나 근접한 종목만
'후보'로 뽑아서 candidates.json 에 저장합니다.
(전체 종목을 매번 개별 조회하면 매우 느리므로, '일자별 전체 스냅샷'을
 과거 ~140 캘린더데이 반복 조회하는 방식으로 효율화했습니다.)
"""
import sys
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from pykrx import stock as krx

sys.path.insert(0, ".")
import common

KST = ZoneInfo("Asia/Seoul")

CALENDAR_DAYS_BACK = 160   # 최소 65 거래일 확보용 여유
CANDIDATE_MAX = 80         # 후보 종목 최대 개수 (실시간 체크 부담 제한)
CANDIDATES_FILE = "candidates.json"


def collect_full_market_history() -> dict:
    """지난 CALENDAR_DAYS_BACK일간 코스피+코스닥 전종목 일별 스냅샷을 모아
    ticker -> DataFrame(OHLCV) 형태로 반환"""
    end = datetime.now(KST)
    dates = [(end - timedelta(days=i)).strftime("%Y%m%d") for i in range(CALENDAR_DAYS_BACK)]
    dates.reverse()

    per_ticker_rows = {}   # code -> list of dict rows
    name_map = {}

    for d in dates:
        try:
            df = krx.get_market_ohlcv_by_ticker(d, market="ALL")
        except Exception:
            continue
        if df is None or df.empty:
            continue  # 휴장일

        df = df.rename(columns={
            "시가": "open", "고가": "high", "저가": "low",
            "종가": "close", "거래량": "volume",
        })
        for code, row in df.iterrows():
            per_ticker_rows.setdefault(code, []).append({
                "date": d, "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
        time.sleep(0.05)  # 과도한 요청 방지

    return per_ticker_rows


def load_ticker_names() -> dict:
    names = {}
    for market in ("KOSPI", "KOSDAQ"):
        for code in krx.get_market_ticker_list(market=market):
            try:
                names[code] = krx.get_market_ticker_name(code)
            except Exception:
                names[code] = code
    return names


def main():
    print("[1/3] 전체 시장 과거 데이터 수집 중... (수 분 소요)")
    history = collect_full_market_history()
    print(f"  -> {len(history)}개 종목 데이터 확보")

    print("[2/3] 종목명 매핑 로딩 중...")
    names = load_ticker_names()

    print("[3/3] 지표 계산 및 후보 선정 중...")
    candidates = []
    for code, rows in history.items():
        if len(rows) < 65:
            continue
        df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
        ind = common.add_indicators(df)
        ev = common.evaluate(ind)

        is_confirmed = ev["verdict"] in ("BUY", "SELL")
        is_near = (not is_confirmed) and abs(ev["score"]) >= 2.0 and ev["trend"] != "횡보"
        if not (is_confirmed or is_near):
            continue

        candidates.append({
            "code": code,
            "name": names.get(code, code),
            "verdict": ev["verdict"],
            "score": ev["score"],
            "trend": ev["trend"],
            # 실시간 체크에서 오늘자 데이터만 이어붙이면 되도록 과거 데이터 보관
            "history": rows[-100:],
        })

    candidates.sort(key=lambda c: abs(c["score"]), reverse=True)
    candidates = candidates[:CANDIDATE_MAX]

    out = {
        "generated_at": datetime.now(KST).isoformat(),
        "count": len(candidates),
        "candidates": candidates,
    }
    common.save_json(CANDIDATES_FILE, out)

    print(f"\n[완료] 후보 {len(candidates)}개 선정 -> {CANDIDATES_FILE}")
    buy_n = sum(1 for c in candidates if c["verdict"] == "BUY")
    sell_n = sum(1 for c in candidates if c["verdict"] == "SELL")
    print(f"  확정 매수신호: {buy_n}개 / 확정 매도신호: {sell_n}개 / 근접 관찰: {len(candidates)-buy_n-sell_n}개")

    # 확정 신호는 장 시작 전에도 바로 1회 알림
    for c in candidates:
        if c["verdict"] in ("BUY", "SELL"):
            df = pd.DataFrame(c["history"]).sort_values("date").reset_index(drop=True)
            ind = common.add_indicators(df)
            ev = common.evaluate(ind)
            tp = common.price_targets(ev["close"], ev["atr14"], ev["verdict"])
            msg = "[전일 종가 기준 사전 신호]\n" + common.format_alert(c["code"], c["name"], ev, tp)
            common.send_telegram(msg)


if __name__ == "__main__":
    main()
