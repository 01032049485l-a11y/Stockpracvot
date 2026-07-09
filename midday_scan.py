# -*- coding: utf-8 -*-
"""
midday_scan.py - 평일 낮 12:30경 1회 실행
아침 daily_scan.py가 뽑아둔 후보들(candidates.json)을 대상으로,
오전 장 시세까지 반영해서 기술적 지표를 재계산하고,
뉴스도 그 시점 기준으로 다시 가져와 AI가 한 번 더 종합 재판단한다.
아침 8:10 알림과는 별개로 "오늘 오후 매수 후보 재점검" 형태로 발송한다.
"""
import sys
import time
import concurrent.futures as cf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import FinanceDataReader as fdr

sys.path.insert(0, ".")
import common
import ai_judge

KST = ZoneInfo("Asia/Seoul")
CANDIDATES_FILE = "candidates.json"
MIN_CONFIDENCE = 0.70
AI_REVIEW_MAX = 25
TARGET_N = 10  # 목표 개수(가이드용) - 이보다 많으면 있는 만큼 다 보내고, 적으면 적은 대로 보냄
MAX_WORKERS = 15


def now_kst() -> datetime:
    return datetime.now(KST)


def is_weekday() -> bool:
    return now_kst().weekday() < 5


def main():
    if not is_weekday():
        print("주말이므로 건너뜁니다.")
        return

    data = common.load_json(CANDIDATES_FILE, None)
    if not data or not data.get("candidates"):
        print("후보 목록이 없습니다. daily_scan.py가 오늘 아침 실행되었는지 확인하세요.")
        return

    today = now_kst().strftime("%Y%m%d")
    fetch_start = (now_kst() - timedelta(days=10)).strftime("%Y-%m-%d")
    candidates = data["candidates"]

    print(f"[1/2] 오전장 시세 반영 재계산 중... (후보 {len(candidates)}개)")

    def fetch_and_eval(c):
        code, name = c["code"], c["name"]
        try:
            recent = fdr.DataReader(code, fetch_start)
        except Exception:
            return None
        if recent is None or recent.empty:
            return None
        recent = recent.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })[["open", "high", "low", "close", "volume"]].dropna()
        recent = recent.reset_index()
        date_col = recent.columns[0]
        recent["date"] = pd.to_datetime(recent[date_col]).dt.strftime("%Y%m%d")
        recent = recent[["date", "open", "high", "low", "close", "volume"]]
        if recent.empty or recent["date"].iloc[-1] != today:
            return None  # 오늘 데이터가 아직 없음

        hist = pd.DataFrame(c["history"])
        hist = hist[~hist["date"].isin(set(recent["date"]))]
        df = pd.concat([hist, recent], ignore_index=True).sort_values("date").reset_index(drop=True)

        ind = common.add_indicators(df)
        ev = common.evaluate(ind)
        if ev["verdict"] != "BUY" or ev["confidence"] < MIN_CONFIDENCE:
            return None
        tp = common.price_targets(ev["close"], ev["atr14"], "BUY")
        if not common.meets_min_return(tp):
            return None  # 채권형 ETF 등 변동폭이 미미한 종목 제외
        return (ev["confidence"], c, ev, tp)

    picks = []
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for result in ex.map(fetch_and_eval, candidates):
            if result:
                picks.append(result)

    picks.sort(key=lambda x: x[0], reverse=True)
    picks = picks[:AI_REVIEW_MAX]
    print(f"  -> 규칙기반 매수신호 {len(picks)}개 (오전 시세 기준)")

    print("[2/2] 뉴스 재수집 + AI 재판단 중...")
    ai_picks = []
    for conf, c, ev, tp in picks:
        news = ai_judge.fetch_news(c["name"])
        ai = ai_judge.ai_analyze(c["code"], c["name"], ev, tp, news)
        if ai is None:
            ai_picks.append({"mode": "rule", "conf": conf, "c": c, "ev": ev, "tp": tp})
            continue
        print(f"  {c['name']}: AI={ai['decision']} (신뢰도 {ai['confidence']}%)")
        if ai["decision"] == "BUY":
            ai_tp_check = {"entry": ev["close"], "target": ai["target_price"]}
            if not common.meets_min_return(ai_tp_check):
                print(f"    -> AI 목표가가 기대수익 기준 미달로 제외 ({ai['target_price']:,}원)")
                continue
            rs = common.rank_score(ai["confidence"], ev["close"], ai["target_price"])
            ai_picks.append({"mode": "ai", "conf": ai["confidence"], "c": c, "ev": ev,
                              "tp": tp, "ai": ai, "news": news, "rank": rs})

    for p in ai_picks:
        if p["mode"] == "rule" and "rank" not in p:
            p["rank"] = common.rank_score(p["ev"]["confidence"] * 100, p["tp"]["entry"], p["tp"]["target"])

    ai_picks.sort(key=lambda x: x["rank"]["score"], reverse=True)
    final_picks = ai_picks  # 개수 상한 없음

    if not final_picks:
        common.send_telegram(
            "📋 [오후 재점검] 오전장 흐름을 반영해도 AI 검토를 통과한 매수 신호가 없습니다."
        )
        print("\n[완료] 알릴 신호 없음")
        return

    for rank_no, p in enumerate(final_picks, 1):
        c, ev = p["c"], p["ev"]
        if p["mode"] == "ai":
            body = ai_judge.format_ai_alert(c["code"], c["name"], ev, p["ai"], p["news"], p["rank"])
        else:
            body = common.format_alert(c["code"], c["name"], ev, p["tp"], p["rank"])
        msg = f"[오후 재점검 {rank_no}/{len(final_picks)} · 종합점수 {p['rank']['score']}점] (오전장 흐름 반영)\n" + body
        common.send_telegram(msg)

    print(f"\n[완료] {len(final_picks)}건 발송")


if __name__ == "__main__":
    main()
