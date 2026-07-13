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
MIN_AI_ALERT_CONFIDENCE = 75  # AI 자신의 확신도가 이 이상일 때만 정식 매수신호로 알림
MAX_WORKERS = 15


MIDDAY_MARKER_FILE = "midday_marker.json"


def now_kst() -> datetime:
    return datetime.now(KST)


def is_weekday() -> bool:
    return now_kst().weekday() < 5


def already_ran_today() -> bool:
    marker = common.load_json(MIDDAY_MARKER_FILE, None)
    return bool(marker) and marker.get("date") == now_kst().strftime("%Y-%m-%d")


def main():
    if not is_weekday():
        print("주말이므로 건너뜁니다.")
        return
    if already_ran_today():
        print(f"오늘({now_kst().strftime('%Y-%m-%d')}) 이미 오후 재점검이 완료되어 건너뜁니다 (백업 트리거 중복실행 방지).")
        return

    data = common.load_json(CANDIDATES_FILE, None)
    if not data or not data.get("candidates"):
        print("후보 목록이 없습니다. daily_scan.py가 오늘 아침 실행되었는지 확인하세요.")
        return

    today = now_kst().strftime("%Y%m%d")
    fetch_start = (now_kst() - timedelta(days=10)).strftime("%Y-%m-%d")
    candidates = data["candidates"]
    market_sentiment = data.get("market_sentiment")

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
    all_reviewed = []
    ai_failures = 0
    for conf, c, ev, tp in picks:
        news = ai_judge.fetch_news(c["name"])
        fundamentals = ai_judge.fetch_fundamentals(c["code"])
        earnings_news = ai_judge.fetch_earnings_news(c["name"])
        investor_flow = ai_judge.fetch_investor_flow(c["code"])
        ai = ai_judge.ai_analyze(c["code"], c["name"], ev, tp, news,
                                  fundamentals, earnings_news, market_sentiment, investor_flow)
        if ai is None:
            ai_failures += 1
            continue
        print(f"  {c['name']}: AI={ai['decision']} (신뢰도 {ai['confidence']}%)")
        all_reviewed.append({"c": c, "ev": ev, "ai": ai, "news": news})
        if ai["decision"] == "BUY":
            if ai["confidence"] < MIN_AI_ALERT_CONFIDENCE:
                print(f"    -> AI 신뢰도 {ai['confidence']}%가 기준({MIN_AI_ALERT_CONFIDENCE}%) 미달로 제외")
                continue
            ai_tp_check = {"entry": ev["close"], "target": ai["target_price"]}
            if ai["target_price"] <= ev["close"] or not common.meets_min_return(ai_tp_check):
                print(f"    -> AI 목표가가 비정상이거나 기대수익 기준 미달로 제외 ({ai['target_price']:,}원)")
                continue
            rs = common.rank_score(ai["confidence"], ev["close"], ai["target_price"])
            ai_picks.append({"mode": "ai", "conf": ai["confidence"], "c": c, "ev": ev,
                              "tp": tp, "ai": ai, "news": news, "rank": rs})

    if ai_failures:
        print(f"\n[경고] AI 판단 실패 {ai_failures}/{len(picks)}건")
    if picks and ai_failures == len(picks):
        common.send_telegram(
            "⚠️ [시스템 경고] 오후 재점검에서 AI 판단이 전부 실패했습니다. "
            "ANTHROPIC_API_KEY/결제 상태를 확인해주세요."
        )
        print("\n[완료] AI 전체 실패로 종료")
        return
    elif picks and ai_failures / len(picks) >= 0.5:
        common.send_telegram(
            f"⚠️ [시스템 경고] 오후 재점검 AI 판단 중 {ai_failures}/{len(picks)}건 실패 (API 상태 확인 권장)."
        )

    ai_picks.sort(key=lambda x: x["rank"]["score"], reverse=True)
    final_picks = ai_picks  # 개수 상한 없음

    MIN_DAILY_PICKS = 3
    if len(final_picks) < MIN_DAILY_PICKS and all_reviewed:
        picked_codes = {p["c"]["code"] for p in final_picks}
        fillers = [w for w in all_reviewed if w["c"]["code"] not in picked_codes]
        fillers.sort(key=lambda x: x["ai"]["confidence"], reverse=True)
        needed = MIN_DAILY_PICKS - len(final_picks)
        for w in fillers[:needed]:
            ai = w["ai"]
            ev = w["ev"]
            rule_tp = common.price_targets(ev["close"], ev["atr14"], "BUY")
            tp_check = {"entry": ev["close"], "target": ai["target_price"]}
            ai_target_valid = ai["target_price"] > ev["close"] and common.meets_min_return(tp_check)
            target_price = ai["target_price"] if ai_target_valid else rule_tp["target"]
            promoted_tp = {"entry": ev["close"], "target": target_price,
                           "stop": rule_tp["stop"], "risk_reward": "1:2"}
            rs = common.rank_score(ai["confidence"], ev["close"], target_price)
            final_picks.append({"mode": "promoted", "conf": ai["confidence"], "c": w["c"], "ev": ev,
                                 "tp": promoted_tp, "ai": ai, "news": w["news"], "rank": rs})
        final_picks.sort(key=lambda x: x["rank"]["score"], reverse=True)

    if not final_picks:
        if all_reviewed:
            all_reviewed.sort(key=lambda x: x["ai"]["confidence"], reverse=True)
            common.send_telegram(
                "📋 [오후 재점검] 확신 있는 매수 신호는 없습니다.\n"
                "대신 오전장 기준 근접했던 종목을 참고용으로 보여드릴게요 (매수 추천 아님)."
            )
            for w in all_reviewed[:3]:
                body = ai_judge.format_watchlist_alert(w["c"]["code"], w["c"]["name"], w["ev"], w["ai"], w["news"])
                common.send_telegram(body)
        else:
            common.send_telegram(
                "📋 [오후 재점검] 오전장 흐름을 반영해도 1차 조건을 만족한 종목이 없습니다."
            )
        print("\n[완료] 확정 신호 없음")
        common.save_json(MIDDAY_MARKER_FILE, {"date": now_kst().strftime("%Y-%m-%d")})
        return

    for rank_no, p in enumerate(final_picks, 1):
        c, ev = p["c"], p["ev"]
        if p["mode"] == "ai":
            body = ai_judge.format_ai_alert(c["code"], c["name"], ev, p["ai"], p["news"], p["rank"])
        elif p["mode"] == "promoted":
            body = ai_judge.format_promoted_alert(c["code"], c["name"], ev, p["ai"], p["news"], p["tp"], p["rank"])
        else:
            body = common.format_alert(c["code"], c["name"], ev, p["tp"], p["rank"])
        msg = f"[오후 재점검 {rank_no}/{len(final_picks)} · 종합점수 {p['rank']['score']}점] (오전장 흐름 반영)\n" + body
        common.send_telegram(msg)

    print(f"\n[완료] {len(final_picks)}건 발송")
    common.save_json(MIDDAY_MARKER_FILE, {"date": now_kst().strftime("%Y-%m-%d")})


if __name__ == "__main__":
    main()
