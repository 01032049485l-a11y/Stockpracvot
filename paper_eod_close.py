# -*- coding: utf-8 -*-
"""
paper_eod_close.py - 평일 15:20 KST 1회 실행
그 시점까지 청산 안 된 포지션을 전량 강제 매도(오버나잇 금지)하고,
오늘 하루 모의매매 결산을 텔레그램으로 발송한다.
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import FinanceDataReader as fdr

sys.path.insert(0, ".")
import common
import paper_trading as pt

KST = ZoneInfo("Asia/Seoul")


EOD_MARKER_FILE = "eod_marker.json"


def now_kst() -> datetime:
    return datetime.now(KST)


def is_weekday() -> bool:
    return now_kst().weekday() < 5


def report_already_sent_today() -> bool:
    marker = common.load_json(EOD_MARKER_FILE, None)
    return bool(marker) and marker.get("date") == now_kst().strftime("%Y-%m-%d")


def get_current_price(code: str, today_compact: str) -> float | None:
    try:
        df = fdr.DataReader(code, today_compact[:4] + "-" + today_compact[4:6] + "-" + today_compact[6:])
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if df.index[-1].strftime("%Y%m%d") != today_compact:
        return None
    return float(df.iloc[-1]["Close"])


def main():
    if not is_weekday():
        print("주말이므로 건너뜁니다.")
        return

    portfolio = pt.load_portfolio()
    today_compact = now_kst().strftime("%Y%m%d")

    remaining = list(portfolio["positions"].keys())
    if remaining:
        print(f"장마감 강제청산 대상 {len(remaining)}건")
        for code in remaining:
            pos = portfolio["positions"][code]
            price = get_current_price(code, today_compact)
            if price is None:
                price = pos["entry_price"]  # 최후 수단: 조회 실패시 매수가로라도 청산 처리
                reason = "장마감 강제청산 (현재가 조회 실패, 매수가로 처리)"
            else:
                reason = "장마감 강제청산"
            record = pt.close_position(portfolio, code, price, reason)
            common.send_telegram(pt.format_close_notice(record))
            print(f"  [강제청산] {pos['name']}({code}) @ {price:,.0f}원")
    else:
        print("강제청산 대상 없음 (이미 전부 청산됨)")

    pt.save_portfolio(portfolio)

    if report_already_sent_today():
        print("오늘 결산 리포트는 이미 발송됨 (백업 트리거 중복방지, 청산 점검만 수행함).")
        return

    report = pt.format_daily_report(portfolio, pt.today_str())
    common.send_telegram(report)
    print("\n" + report)
    common.save_json(EOD_MARKER_FILE, {"date": now_kst().strftime("%Y-%m-%d")})


if __name__ == "__main__":
    main()
