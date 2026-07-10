# -*- coding: utf-8 -*-
"""
paper_status.py - 언제든 수동 실행해서 모의매매 현재 상황을 텔레그램으로 받는다.
(예약 실행 없음, GitHub Actions "Run workflow" 버튼을 누를 때만 동작 -> 비용 0원)
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


def get_current_price(code: str, today_compact: str) -> float | None:
    try:
        df = fdr.DataReader(code, today_compact[:4] + "-" + today_compact[4:6] + "-" + today_compact[6:])
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return float(df.iloc[-1]["Close"])


def main():
    p = pt.load_portfolio()
    today = pt.today_str()
    today_compact = now_kst().strftime("%Y%m%d")

    lines = [f"<b>🔍 모의매매 현재 상황</b> ({now_kst().strftime('%Y-%m-%d %H:%M')} 기준)"]
    lines.append(f"현금: {p['cash']:,.0f}원")

    if p["positions"]:
        lines.append(f"\n<b>보유 중 ({len(p['positions'])}종목)</b>")
        total_unrealized = 0
        for code, pos in p["positions"].items():
            cur = get_current_price(code, today_compact)
            if cur is None:
                lines.append(f"  · {pos['name']}: 현재가 조회 실패")
                continue
            unrealized = (cur - pos["entry_price"]) * pos["shares"]
            unrealized_pct = (cur - pos["entry_price"]) / pos["entry_price"] * 100
            total_unrealized += unrealized
            emoji = "🟢" if unrealized >= 0 else "🔴"
            lines.append(
                f"  {emoji} {pos['name']}: {pos['entry_price']:,}→{cur:,.0f}원 "
                f"({unrealized_pct:+.2f}%, 평가손익 {unrealized:+,.0f}원)"
            )
        est_total = p["cash"] + sum(
            pos["shares"] * (get_current_price(c, today_compact) or pos["entry_price"])
            for c, pos in p["positions"].items()
        )
        lines.append(f"\n평가 총자산(추정): {est_total:,.0f}원")
    else:
        lines.append("\n현재 보유 중인 포지션 없음")

    todays = [h for h in p["history"] if h["date"] == today]
    if todays:
        wins = sum(1 for h in todays if h["pnl"] > 0)
        total_pnl = sum(h["pnl"] for h in todays)
        lines.append(f"\n<b>오늘 청산 거래 ({len(todays)}건, 승 {wins})</b>")
        for h in todays:
            emoji = "🟢" if h["pnl"] >= 0 else "🔴"
            lines.append(f"  {emoji} {h['name']}: {h['pnl']:+,}원 ({h['pnl_pct']:+.2f}%) - {h['reason']}")
        lines.append(f"오늘 실현손익: {total_pnl:+,}원")
    else:
        lines.append("\n오늘 청산된 거래 없음")

    total_trades = len(p["history"])
    if total_trades:
        total_wins = sum(1 for h in p["history"] if h["pnl"] > 0)
        total_pnl_all = sum(h["pnl"] for h in p["history"])
        lines.append(
            f"\n<b>누적 (첫날부터)</b>\n"
            f"총 거래 {total_trades}건, 승률 {total_wins/total_trades*100:.0f}%, "
            f"누적손익 {total_pnl_all:+,}원"
        )

    msg = "\n".join(lines)
    common.send_telegram(msg)
    print(msg)


if __name__ == "__main__":
    main()
