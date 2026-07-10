# -*- coding: utf-8 -*-
"""
daily_scan.py (v2) - 장 시작 전 1회 실행
데이터 출처를 KRX(pykrx) -> 네이버금융(FinanceDataReader)으로 변경.
(KRX는 해외 IP를 차단해서 GitHub Actions에서 동작하지 않음)

코스피+코스닥 전 종목의 과거 시세를 받아 지표 계산 후,
엄격 기준을 통과/근접한 종목만 candidates.json 으로 저장.
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
CANDIDATE_MAX = 80
CANDIDATES_FILE = "candidates.json"
MIN_ROWS = 65          # 지표 계산 최소 거래일
MAX_WORKERS = 20       # 동시 요청 수 (서버 차단 방지를 위해 20으로 제한)


def get_ticker_list() -> pd.DataFrame:
    """코스피+코스닥 종목 리스트 (Code, Name)
    1순위: 네이버금융 시가총액 페이지 (해외 서버에서도 접근 가능)
    2순위: FinanceDataReader StockListing (국내 실행 시)
    """
    import re
    import requests

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    pattern = re.compile(r'href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>')

    rows = []
    for sosok in (0, 1):  # 0=코스피, 1=코스닥
        empty_streak = 0
        for page in range(1, 60):
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                r.encoding = "euc-kr"
                found = pattern.findall(r.text)
            except Exception:
                found = []
            if not found:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue
            empty_streak = 0
            for code, name in found:
                rows.append({"Code": code, "Name": name.strip()})
            time.sleep(0.1)

    if rows:
        alldf = pd.DataFrame(rows)
    else:
        # 네이버 실패 시 FDR 시도 (국내 실행용)
        frames = []
        for market in ("KOSPI", "KOSDAQ"):
            try:
                df = fdr.StockListing(market)
                frames.append(df)
            except Exception as e:
                print(f"  [경고] {market} 리스트 조회 실패: {e}")
        if not frames:
            raise RuntimeError("종목 리스트를 가져오지 못했습니다.")
        alldf = pd.concat(frames, ignore_index=True)
        code_col = "Code" if "Code" in alldf.columns else "Symbol"
        name_col = "Name" if "Name" in alldf.columns else "name"
        alldf = alldf[[code_col, name_col]].rename(columns={code_col: "Code", name_col: "Name"})

    alldf = alldf.dropna(subset=["Code"])
    alldf["Code"] = alldf["Code"].astype(str).str.zfill(6)
    # 스팩, 채권형/머니마켓/인버스/레버리지 등 변동성이 사실상 없거나
    # 일반적인 상승 매매 전략과 안 맞는 상품은 제외
    EXCLUDE_KEYWORDS = [
        "스팩", "채권", "국채", "머니마켓", "MMF", "단기자금",
        "인버스", "레버리지", "TDF", "타겟데이트", "선물", "합성",
    ]
    pattern = "|".join(EXCLUDE_KEYWORDS)
    alldf = alldf[~alldf["Name"].astype(str).str.contains(pattern, na=False, regex=True)]
    return alldf.drop_duplicates(subset=["Code"]).reset_index(drop=True)


def fetch_history(code: str, start: str) -> pd.DataFrame | None:
    try:
        df = fdr.DataReader(code, start)
    except Exception:
        return None
    if df is None or df.empty or len(df) < MIN_ROWS:
        return None
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    if len(df) < MIN_ROWS:
        return None
    df = df.reset_index().rename(columns={df.index.name or "Date": "date", "index": "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    return df


def already_ran_today(now: datetime) -> bool:
    existing = common.load_json(CANDIDATES_FILE, None)
    picks_file = common.load_json("today_picks.json", None)
    if not existing or not picks_file:
        return False  # 둘 중 하나라도 없으면(과거 버전 실행분 등) 다시 완전히 실행
    gen_at = existing.get("generated_at", "")
    picks_date = picks_file.get("date", "")
    today_str = now.strftime("%Y-%m-%d")
    return gen_at[:10] == today_str and picks_date == today_str


def main():
    now = datetime.now(KST)
    if already_ran_today(now):
        print(f"오늘({now.strftime('%Y-%m-%d')}) 이미 스캔이 완료되어 건너뜁니다 (백업 트리거 중복실행 방지).")
        return
    start = (now - timedelta(days=200)).strftime("%Y-%m-%d")

    print("[1/2] 종목 리스트 조회 중...")
    tickers = get_ticker_list()
    print(f"  -> {len(tickers)}개 종목")

    print(f"[2/2] 종목별 시세 수집 + 지표 분석 중... (동시 {MAX_WORKERS}건, 5~15분 소요)")
    candidates = []
    n_ok = 0
    n_done = 0
    total = len(tickers)
    code_name = {row["Code"]: row["Name"] for _, row in tickers.iterrows()}

    # 시장 전체 공포/탐욕 심리 집계용 카운터 (스캔한 전종목 대상, 후보 여부와 무관)
    sentiment_total = 0
    sentiment_above_ma20 = 0
    sentiment_rsi_bullish = 0

    def task(code):
        return code, fetch_history(code, start)

    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(task, code) for code in code_name]
        for fut in cf.as_completed(futures):
            n_done += 1
            if n_done % 300 == 0:
                print(f"  진행: {n_done}/{total} (데이터 확보 {n_ok}, 후보 {len(candidates)})")
            try:
                code, df = fut.result()
            except Exception:
                continue
            if df is None:
                continue
            n_ok += 1
            name = code_name[code]

            ind = common.add_indicators(df)
            ev = common.evaluate(ind)

            # 시장심리 집계 (후보 채택 여부와 무관하게 전체 반영)
            if ev.get("ma60") is not None:
                sentiment_total += 1
                last_rsi = ind["rsi"].iloc[-1]
                if ev["close"] > ind["ma20"].iloc[-1]:
                    sentiment_above_ma20 += 1
                if last_rsi >= 50:
                    sentiment_rsi_bullish += 1

            is_confirmed = ev["verdict"] == "BUY"
            is_near = (not is_confirmed) and ev["score"] >= 2.0 and ev["trend"] == "상승추세"
            if not (is_confirmed or is_near):
                continue

            candidates.append({
                "code": code,
                "name": name,
                "verdict": ev["verdict"],
                "score": ev["score"],
                "trend": ev["trend"],
                "history": df.tail(100).to_dict("records"),
            })

    market_sentiment = common.compute_market_sentiment(
        sentiment_total, sentiment_above_ma20, sentiment_rsi_bullish
    )
    print(f"\n[시장심리] 공포/탐욕 지수: {market_sentiment['score']}/100 ({market_sentiment['label']}) "
          f"- 스캔 {sentiment_total}종목 기준")

    candidates.sort(key=lambda c: abs(c["score"]), reverse=True)
    candidates = candidates[:CANDIDATE_MAX]

    out = {
        "generated_at": now.isoformat(),
        "count": len(candidates),
        "candidates": candidates,
        "market_sentiment": market_sentiment,
    }
    common.save_json(CANDIDATES_FILE, out)

    buy_n = sum(1 for c in candidates if c["verdict"] == "BUY")
    print(f"\n[완료] 후보 {len(candidates)}개 (확정매수 {buy_n} / 근접 {len(candidates)-buy_n})")

    # 1단계: 규칙기반 매수 후보 (신뢰도 70% 이상 + 최소 기대수익률 충족)
    MIN_CONFIDENCE = 0.70
    AI_REVIEW_MAX = 25   # AI+뉴스 검토 대상 상한 (API 비용/시간 관리, 기존 15->25로 확대)
    TARGET_N = 10        # 목표 개수(가이드용) - 이보다 많으면 있는 만큼 다 보내고, 적으면 적은 대로 보냄
    picks = []
    for c in candidates:
        if c["verdict"] != "BUY":
            continue
        df = pd.DataFrame(c["history"])
        ind = common.add_indicators(df)
        ev = common.evaluate(ind)
        if ev["verdict"] != "BUY" or ev["confidence"] < MIN_CONFIDENCE:
            continue
        tp = common.price_targets(ev["close"], ev["atr14"], "BUY")
        if not common.meets_min_return(tp):
            continue  # 채권형 ETF 등 변동폭이 미미한 종목 제외
        picks.append((ev["confidence"], c, ev, tp))
    picks.sort(key=lambda x: x[0], reverse=True)
    picks = picks[:AI_REVIEW_MAX]

    # 2단계: 뉴스 + 재무지표 + 실적뉴스 수집 후 AI 종합 판단 (시장 전체 심리 포함)
    print(f"\n[AI 검토] 규칙기반 후보 {len(picks)}개에 대해 뉴스/재무/실적 수집 + AI 판단 중...")
    ai_picks = []
    all_reviewed = []  # PASS 포함 전체 AI 검토 결과 (확정 픽이 0개일 때 근접 관찰종목 후보로 사용)
    ai_failures = 0
    for conf, c, ev, tp in picks:
        news = ai_judge.fetch_news(c["name"])
        fundamentals = ai_judge.fetch_fundamentals(c["code"])
        earnings_news = ai_judge.fetch_earnings_news(c["name"])
        ai = ai_judge.ai_analyze(c["code"], c["name"], ev, tp, news,
                                  fundamentals, earnings_news, market_sentiment)
        if ai is None:
            # AI 판단 실패 -> 더 이상 규칙기반으로 자동승인하지 않음 (뉴스/재무 검증 없이
            # 기술지표만으로 신호를 내보내는 것은 정확도를 떨어뜨리므로 그냥 제외한다)
            ai_failures += 1
            continue
        print(f"  {c['name']}: AI={ai['decision']} (신뢰도 {ai['confidence']}%)")
        all_reviewed.append({"c": c, "ev": ev, "ai": ai, "news": news})
        if ai["decision"] == "BUY":
            ai_tp_check = {"entry": ev["close"], "target": ai["target_price"]}
            if ai["target_price"] <= ev["close"] or not common.meets_min_return(ai_tp_check):
                print(f"    -> AI 목표가가 비정상이거나 기대수익 기준 미달로 제외 ({ai['target_price']:,}원)")
                continue
            rs = common.rank_score(ai["confidence"], ev["close"], ai["target_price"])
            ai_picks.append({"mode": "ai", "conf": ai["confidence"], "c": c, "ev": ev,
                              "tp": tp, "ai": ai, "news": news, "rank": rs})

    if ai_failures:
        print(f"\n[경고] AI 판단 실패 {ai_failures}/{len(picks)}건 (API 키/결제/네트워크 확인 필요)")
    if picks and ai_failures == len(picks):
        common.send_telegram(
            "⚠️ [시스템 경고] 오늘 AI 판단이 전부 실패했습니다.\n"
            f"기술적 후보는 {len(picks)}개 있었지만 뉴스/재무 검증 없이는 신호를 보내지 않습니다.\n"
            "ANTHROPIC_API_KEY 또는 결제 상태를 확인해주세요."
        )
        common.save_json("today_picks.json", {"date": now.strftime("%Y-%m-%d"), "picks": []})
        return
    elif picks and ai_failures / len(picks) >= 0.5:
        common.send_telegram(
            f"⚠️ [시스템 경고] 오늘 AI 판단 중 {ai_failures}/{len(picks)}건이 실패했습니다. "
            "일부 신호가 누락됐을 수 있습니다 (API 상태 확인 권장)."
        )

    ai_picks.sort(key=lambda x: x["rank"]["score"], reverse=True)
    final_picks = ai_picks  # 개수 상한 없음: AI가 승인한 만큼(목표 약 10개, 많으면 더/적으면 덜)

    # 최소 보장: 확정 승인이 MIN_DAILY_PICKS(3)개 미만이면, PASS 판정을 받았더라도
    # 그중 AI 신뢰도가 가장 높았던 근접 종목을 '승격 신호'로 채워 넣는다.
    # (모의매매가 매일 최소한의 데이터를 쌓을 수 있도록. 단, 완전승인과는 명확히 구분 표시)
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
        print(f"[최소보장] 확정승인 부족으로 {min(needed, len(fillers))}개 근접종목을 승격신호로 추가")

    # 모의매매용: 오늘 승인된 픽을 저장 (paper_buy.py가 실제 시가로 가상매수할 때 사용)
    today_picks = [{
        "code": p["c"]["code"], "name": p["c"]["name"],
        "stop_price": p["tp"]["stop"],
        "target_price": (p["ai"]["target_price"] if p["mode"] == "ai" else p["tp"]["target"]),
        "target_days": (p["ai"]["target_days"] if p["mode"] in ("ai", "promoted") else 1),
        "rank_score": p["rank"]["score"],
        "promoted": p["mode"] == "promoted",
    } for p in final_picks]
    common.save_json("today_picks.json", {"date": now.strftime("%Y-%m-%d"), "picks": today_picks})

    if final_picks:
        for rank_no, p in enumerate(final_picks, 1):
            c, ev = p["c"], p["ev"]
            if p["mode"] == "ai":
                body = ai_judge.format_ai_alert(c["code"], c["name"], ev, p["ai"], p["news"], p["rank"])
            elif p["mode"] == "promoted":
                body = ai_judge.format_promoted_alert(c["code"], c["name"], ev, p["ai"], p["news"], p["tp"], p["rank"])
            else:
                body = common.format_alert(c["code"], c["name"], ev, p["tp"], p["rank"])
            msg = f"[오늘의 매수 후보 {rank_no}/{len(final_picks)} · 종합점수 {p['rank']['score']}점]\n" + body
            common.send_telegram(msg)
    elif all_reviewed:
        # 확정 매수신호가 하루 종일 0개 -> 그나마 신뢰도가 높았던 근접 종목을 참고용으로 발송
        all_reviewed.sort(key=lambda x: x["ai"]["confidence"], reverse=True)
        top_watch = all_reviewed[:3]
        common.send_telegram(
            "📋 오늘 아침 기준, 확신 있는 매수 신호는 없습니다.\n"
            "대신 그나마 신뢰도가 높았던 근접 종목을 참고용으로 보여드릴게요 "
            "(매수 추천 아님)."
        )
        for w in top_watch:
            body = ai_judge.format_watchlist_alert(w["c"]["code"], w["c"]["name"], w["ev"], w["ai"], w["news"])
            common.send_telegram(body)
    else:
        common.send_telegram("📋 오늘 아침 기준, 기술적 1차 조건을 만족한 종목조차 없습니다.\n장중에 조건이 바뀌면 알림을 보내드릴게요.")


if __name__ == "__main__":
    main()
