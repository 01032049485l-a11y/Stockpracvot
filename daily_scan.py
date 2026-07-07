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
    """코스피+코스닥 종목 리스트 (Code, Name)"""
    frames = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
            df["Market"] = market
            frames.append(df)
        except Exception as e:
            print(f"  [경고] {market} 리스트 조회 실패: {e}")
    if not frames:
        raise RuntimeError("종목 리스트를 가져오지 못했습니다.")
    alldf = pd.concat(frames, ignore_index=True)
    # 컬럼명 버전차 대응
    code_col = "Code" if "Code" in alldf.columns else "Symbol"
    name_col = "Name" if "Name" in alldf.columns else "name"
    alldf = alldf[[code_col, name_col]].rename(columns={code_col: "Code", name_col: "Name"})
    alldf = alldf.dropna(subset=["Code"])
    alldf["Code"] = alldf["Code"].astype(str).str.zfill(6)
    # 우선주/스팩 등 이름 필터(간단): 스팩 제외
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

        is_confirmed = ev["verdict"] in ("BUY", "SELL")
        is_near = (not is_confirmed) and abs(ev["score"]) >= 2.0 and ev["trend"] != "횡보"
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
    sell_n = sum(1 for c in candidates if c["verdict"] == "SELL")
    print(f"\n[완료] 후보 {len(candidates)}개 (확정매수 {buy_n} / 확정매도 {sell_n} / 근접 {len(candidates)-buy_n-sell_n})")

    # 확정 신호는 장 시작 전 1회 사전 알림
    for c in candidates:
        if c["verdict"] in ("BUY", "SELL"):
            df = pd.DataFrame(c["history"])
            ind = common.add_indicators(df)
            ev = common.evaluate(ind)
            if ev["verdict"] not in ("BUY", "SELL"):
                continue
            tp = common.price_targets(ev["close"], ev["atr14"], ev["verdict"])
            msg = "[전일 종가 기준 사전 신호]\n" + common.format_alert(c["code"], c["name"], ev, tp)
            common.send_telegram(msg)


if __name__ == "__main__":
    main()
