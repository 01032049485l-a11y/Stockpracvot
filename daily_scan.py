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


def main():
    now = datetime.now(KST)
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

    candidates.sort(key=lambda c: abs(c["score"]), reverse=True)
    candidates = candidates[:CANDIDATE_MAX]

    out = {
        "generated_at": now.isoformat(),
        "count": len(candidates),
        "candidates": candidates,
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

    # 2단계: 뉴스 수집 + AI 종합 판단
    print(f"\n[AI 검토] 규칙기반 후보 {len(picks)}개에 대해 뉴스 수집 + AI 판단 중...")
    ai_picks = []
    for conf, c, ev, tp in picks:
        news = ai_judge.fetch_news(c["name"])
        ai = ai_judge.ai_analyze(c["code"], c["name"], ev, tp, news)
        if ai is None:
            # AI 판단 불가 시 규칙기반 결과로 대체(폴백)
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

    # 규칙기반 폴백 항목도 동일한 기준으로 종합점수 계산
    for p in ai_picks:
        if p["mode"] == "rule" and "rank" not in p:
            p["rank"] = common.rank_score(p["ev"]["confidence"] * 100, p["tp"]["entry"], p["tp"]["target"])

    ai_picks.sort(key=lambda x: x["rank"]["score"], reverse=True)
    final_picks = ai_picks  # 개수 상한 없음: AI가 승인한 만큼(목표 약 10개, 많으면 더/적으면 덜)

    if not final_picks:
        common.send_telegram("📋 오늘 아침 기준, AI 검토를 통과한 매수 신호가 없습니다.\n장중에 조건이 확정되면 알림을 보내드릴게요.")
    for rank_no, p in enumerate(final_picks, 1):
        c, ev = p["c"], p["ev"]
        if p["mode"] == "ai":
            body = ai_judge.format_ai_alert(c["code"], c["name"], ev, p["ai"], p["news"], p["rank"])
        else:
            body = common.format_alert(c["code"], c["name"], ev, p["tp"], p["rank"])
        msg = f"[오늘의 매수 후보 {rank_no}/{len(final_picks)} · 종합점수 {p['rank']['score']}점]\n" + body
        common.send_telegram(msg)


if __name__ == "__main__":
    main()
