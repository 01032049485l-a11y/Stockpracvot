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
import ai_judge

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


def is_morning_buy_window() -> bool:
    """장중 매수 알림은 오전(9:00~12:00)에만 보낸다.
    오후 확정 신호는 당일 매수 실익이 적어 다음날 후보로 넘긴다."""
    now = now_kst()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 900 <= hm <= 1200


MIN_CONFIDENCE = 0.70


def main():
    if not is_morning_buy_window():
        print("오전 매수 알림 시간대(09:00~12:00)가 아니므로 건너뜁니다.")
        return

    data = common.load_json(CANDIDATES_FILE, None)
    if not data or not data.get("candidates"):
        print("후보 목록이 없습니다. daily_scan.py를 먼저 실행하세요.")
        return
    market_sentiment = data.get("market_sentiment")

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

        if ev["verdict"] != "BUY" or ev["confidence"] < MIN_CONFIDENCE:
            continue

        key = f"{code}_BUY"
        if key in alerted:
            continue

        tp = common.price_targets(ev["close"], ev["atr14"], "BUY")
        if not common.meets_min_return(tp):
            continue  # 채권형 ETF 등 변동폭이 미미한 종목 제외

        news = ai_judge.fetch_news(name)
        fundamentals = ai_judge.fetch_fundamentals(code)
        earnings_news = ai_judge.fetch_earnings_news(name)
        ai = ai_judge.ai_analyze(code, name, ev, tp, news,
                                  fundamentals, earnings_news, market_sentiment)

        if ai is not None and ai["decision"] != "BUY":
            print(f"  [AI 반려] {name}({code}) - {ai.get('summary','')[:60]}")
            alerted[key] = now_kst().isoformat()  # 같은 신호 반복 재검토 방지
            continue

        if ai is not None:
            ai_tp_check = {"entry": ev["close"], "target": ai["target_price"]}
            if ai["target_price"] <= ev["close"] or not common.meets_min_return(ai_tp_check):
                print(f"  [제외] {name}({code}) - AI 목표가가 비정상이거나 기대수익 기준 미달")
                alerted[key] = now_kst().isoformat()
                continue
            rs = common.rank_score(ai["confidence"], ev["close"], ai["target_price"])
            msg = ai_judge.format_ai_alert(code, name, ev, ai, news, rs)
        else:
            rs = common.rank_score(ev["confidence"] * 100, tp["entry"], tp["target"])
            msg = common.format_alert(code, name, ev, tp, rs)  # AI 판단 불가 시 규칙기반으로 대체

        common.send_telegram(msg)
        alerted[key] = now_kst().isoformat()
        n_alerted += 1
        print(f"  [알림 발송] {name}({code}) BUY (기술적 신뢰도 {ev['confidence']*100:.0f}%)")

    common.save_json(ALERTED_FILE, alerted)
    print(f"\n체크 완료: {n_checked}개 후보 확인, {n_alerted}건 신규 알림 발송")


if __name__ == "__main__":
    main()
