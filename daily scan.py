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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import FinanceDataReader as fdr

sys.path.insert(0, ".")
import common

KST = ZoneInfo("Asia/Seoul")
CANDIDATE_MAX = 80
CANDIDATES_FILE = "candidates.json"
MIN_ROWS = 65          # 지표 계산 최소 거래일
REQ_SLEEP = 0.03       # 요청 간격 (서버 배려)


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
    alldf = alldf[~alldf["Name"].astype(str).str.contains("스팩", na=False)]
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

    print("[2/2] 종목별 시세 수집 + 지표 분석 중... (10~30분 소요)")
    candidates = []
    n_ok = 0
    for i, row in tickers.iterrows():
        code, name = row["Code"], row["Name"]
        df = fetch_history(code, start)
        time.sleep(REQ_SLEEP)
        if df is None:
            continue
        n_ok += 1

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

        if (i + 1) % 300 == 0:
            print(f"  진행: {i+1}/{len(tickers)} (데이터 확보 {n_ok}, 후보 {len(candidates)})")

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

    # 아침 매수용: 확정 BUY 중 신뢰도 70% 이상만, 신뢰도 순 상위 5개 알림
    MIN_CONFIDENCE = 0.70
    TOP_N = 5
    picks = []
    for c in candidates:
        if c["verdict"] != "BUY":
            continue
        df = pd.DataFrame(c["history"])
        ind = common.add_indicators(df)
        ev = common.evaluate(ind)
        if ev["verdict"] == "BUY" and ev["confidence"] >= MIN_CONFIDENCE:
            picks.append((ev["confidence"], c, ev))
    picks.sort(key=lambda x: x[0], reverse=True)

    if not picks:
        common.send_telegram("📋 오늘 아침 기준, 신뢰도 70% 이상의 매수 신호가 없습니다.\n장중에 조건이 확정되면 알림을 보내드릴게요.")
    for rank, (conf, c, ev) in enumerate(picks[:TOP_N], 1):
        tp = common.price_targets(ev["close"], ev["atr14"], "BUY")
        msg = f"[오늘의 매수 후보 {rank}/{min(len(picks), TOP_N)}]\n" + common.format_alert(c["code"], c["name"], ev, tp)
        common.send_telegram(msg)


if __name__ == "__main__":
    main()
