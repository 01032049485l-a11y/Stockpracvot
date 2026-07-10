# -*- coding: utf-8 -*-
"""
paper_buy.py - 평일 09:07 KST 1회 실행
daily_scan.py가 아침에 만들어둔 today_picks.json(AI 승인 종목)을 대상으로,
"이 스크립트가 실제로 실행되는 시점의 최신가"로 가상매수를 체결한다.
(과거에는 그날 시가로 고정 매수했는데, 늦게 실행되면 몇 시간 전 가격에
 산 것처럼 기록되는 오류가 있어 수정함 - 항상 실행 시점 기준 최신 체결가를 사용)
가상자금(기본 500만원, 전날 손익 반영된 현재 cash)을 상위 랭킹 종목에 균등분배.
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import FinanceDataReader as fdr

sys.path.insert(0, ".")
import common
import paper_trading as pt

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


def is_weekday() -> bool:
    return now_kst().weekday() < 5


def get_current_price(code: str, today: str, today_compact: str) -> float | None:
    """이 함수를 호출하는 '지금 이 순간' 기준 가장 최신 체결가를 가져온다 (시가 아님)."""
    try:
        df = fdr.DataReader(code, today)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    d = df.index[-1]
    if d.strftime("%Y%m%d") != today_compact:
        return None  # 오늘자 데이터가 아직 없음
    return float(df.iloc[-1]["Close"])  # 오늘자 최신 체결가(장중이면 현재가, 장마감 후면 종가)


def main():
    if not is_weekday():
        print("주말이므로 건너뜁니다.")
        return

    data = common.load_json("today_picks.json", None)
    today = now_kst().strftime("%Y-%m-%d")
    today_compact = now_kst().strftime("%Y%m%d")

    if not data or data.get("date") != today or not data.get("picks"):
        print("오늘자 승인 종목이 없습니다 (today_picks.json 없음/날짜불일치/빈 리스트). 모의매수 건너뜁니다.")
        return

    picks = data["picks"][:pt.MAX_POSITIONS]
    portfolio = pt.load_portfolio()

    if portfolio["positions"]:
        print(f"[경고] 이미 보유 중인 포지션이 {len(portfolio['positions'])}건 있습니다. "
              f"전날 강제청산이 안 된 것일 수 있어 확인이 필요합니다.")

    budget_per = portfolio["cash"] / max(len(picks), 1)
    print(f"오늘 매수 대상 {len(picks)}종목, 종목당 예산 약 {budget_per:,.0f}원 (총현금 {portfolio['cash']:,.0f}원)")
    print(f"(실행 시각 {now_kst().strftime('%H:%M')} 기준 현재가로 매수합니다)")

    bought = 0
    for pick in picks:
        code, name = pick["code"], pick["name"]
        price = get_current_price(code, today, today_compact)
        if price is None or price <= 0:
            print(f"  {name}({code}): 현재가 조회 실패, 건너뜀")
            continue
        shares = int(budget_per // price)
        if shares < 1:
            print(f"  {name}({code}): 예산 부족으로 1주도 못 삼 (현재가 {price:,.0f}원), 건너뜀")
            continue

        pt.open_position(portfolio, code, name, shares, price,
                          pick["stop_price"], pick["target_price"], pick["target_days"])
        common.send_telegram(pt.format_buy_notice(code, name, shares, price,
                                                    pick["stop_price"], pick["target_price"]))
        print(f"  [매수] {name}({code}) {shares}주 @ {price:,.0f}원 (실행시점 현재가)")
        bought += 1

    pt.save_portfolio(portfolio)
    print(f"\n[완료] {bought}건 가상매수, 잔여현금 {portfolio['cash']:,.0f}원")


if __name__ == "__main__":
    main()
