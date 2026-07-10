# -*- coding: utf-8 -*-
"""
paper_trading.py - 자동 모의매매(당일 데이트레이딩) 핵심 로직
실제 주문은 하지 않고 가상으로 사고팔아 손익을 기록한다.
총 가상자금 500만원, 당일 매수 -> 당일 반드시 청산(오버나잇 없음).
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import common

KST = ZoneInfo("Asia/Seoul")
PORTFOLIO_FILE = "paper_portfolio.json"
TODAY_PICKS_FILE = "today_picks.json"
TOTAL_CAPITAL = 5_000_000
MAX_POSITIONS = 5   # 동시 보유 최대 종목 수 (너무 잘게 쪼개지지 않도록)


def now_kst() -> datetime:
    return datetime.now(KST)


def today_str() -> str:
    return now_kst().strftime("%Y-%m-%d")


def load_portfolio() -> dict:
    default = {"cash": TOTAL_CAPITAL, "positions": {}, "history": []}
    return common.load_json(PORTFOLIO_FILE, default)


def save_portfolio(p: dict):
    common.save_json(PORTFOLIO_FILE, p)


def portfolio_value(p: dict, price_lookup: dict) -> float:
    """현금 + 보유 포지션 평가금액 = 총 자산"""
    total = p["cash"]
    for code, pos in p["positions"].items():
        price = price_lookup.get(code, pos["entry_price"])
        total += pos["shares"] * price
    return total


def open_position(p: dict, code: str, name: str, shares: int, entry_price: float,
                   stop_price: float, target_price: float, target_days: int):
    cost = shares * entry_price
    p["cash"] -= cost
    p["positions"][code] = {
        "name": name,
        "shares": shares,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "target_days": target_days,
        "entry_time": now_kst().isoformat(),
        "last_ai_check_price": entry_price,
        "last_ai_check_time": now_kst().isoformat(),
        "date": today_str(),
    }


def close_position(p: dict, code: str, exit_price: float, reason: str) -> dict:
    pos = p["positions"].pop(code)
    proceeds = pos["shares"] * exit_price
    cost = pos["shares"] * pos["entry_price"]
    pnl = proceeds - cost
    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
    p["cash"] += proceeds
    record = {
        "date": pos["date"],
        "code": code,
        "name": pos["name"],
        "shares": pos["shares"],
        "entry_price": round(pos["entry_price"]),
        "exit_price": round(exit_price),
        "pnl": round(pnl),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "entry_time": pos["entry_time"],
        "exit_time": now_kst().isoformat(),
    }
    p["history"].append(record)
    return record


def format_buy_notice(code: str, name: str, shares: int, entry_price: float,
                       stop_price: float, target_price: float) -> str:
    return (
        f"<b>🧪 [모의매매] 매수 체결</b>\n"
        f"종목: <b>{name}</b> ({code})\n"
        f"매수가: {entry_price:,.0f}원 x {shares}주 = {shares*entry_price:,.0f}원\n"
        f"목표가: {target_price:,.0f}원 (참고치, AI가 상황봐서 재판단)\n"
        f"손절가: {stop_price:,.0f}원 (도달시 무조건 즉시 매도)"
    )


def format_close_notice(record: dict) -> str:
    emoji = "🟢" if record["pnl"] >= 0 else "🔴"
    return (
        f"<b>🧪 [모의매매] 매도 체결 {emoji}</b>\n"
        f"종목: <b>{record['name']}</b> ({record['code']})\n"
        f"매수 {record['entry_price']:,}원 → 매도 {record['exit_price']:,}원\n"
        f"손익: {record['pnl']:+,}원 ({record['pnl_pct']:+.2f}%)\n"
        f"사유: {record['reason']}"
    )


def format_daily_report(p: dict, date: str) -> str:
    todays = [h for h in p["history"] if h["date"] == date]
    if not todays:
        return (
            f"<b>📊 [모의매매 일일결산] {date}</b>\n"
            f"오늘은 매매가 없었습니다.\n"
            f"현재 총자산: {p['cash']:,.0f}원"
        )
    wins = [h for h in todays if h["pnl"] > 0]
    losses = [h for h in todays if h["pnl"] <= 0]
    total_pnl = sum(h["pnl"] for h in todays)
    win_rate = len(wins) / len(todays) * 100
    lines = "\n".join(
        f"  {'🟢' if h['pnl']>=0 else '🔴'} {h['name']}: {h['pnl']:+,}원 ({h['pnl_pct']:+.2f}%) - {h['reason']}"
        for h in todays
    )
    return (
        f"<b>📊 [모의매매 일일결산] {date}</b>\n"
        f"거래 {len(todays)}건 (승 {len(wins)} / 패 {len(losses)}) 승률 {win_rate:.0f}%\n"
        f"오늘 손익: {total_pnl:+,}원\n"
        f"현재 총자산: {p['cash']:,.0f}원 (시작 {TOTAL_CAPITAL:,}원 대비 {p['cash']-TOTAL_CAPITAL:+,}원)\n"
        f"\n{lines}"
    )


def build_status_message(get_current_price_fn) -> str:
    """현재 포트폴리오 상황 메시지를 만든다.
    get_current_price_fn(code) -> float | None 형태의 현재가 조회 함수를 주입받는다
    (paper_status.py, telegram_command.py가 동일 로직을 공유하기 위함)."""
    p = load_portfolio()
    today = today_str()

    lines = [f"<b>🔍 모의매매 현재 상황</b> ({now_kst().strftime('%Y-%m-%d %H:%M')} 기준)"]
    lines.append(f"현금: {p['cash']:,.0f}원")

    if p["positions"]:
        lines.append(f"\n<b>보유 중 ({len(p['positions'])}종목)</b>")
        for code, pos in p["positions"].items():
            cur = get_current_price_fn(code)
            if cur is None:
                lines.append(f"  · {pos['name']}: 현재가 조회 실패")
                continue
            unrealized = (cur - pos["entry_price"]) * pos["shares"]
            unrealized_pct = (cur - pos["entry_price"]) / pos["entry_price"] * 100
            emoji = "🟢" if unrealized >= 0 else "🔴"
            lines.append(
                f"  {emoji} {pos['name']}: {pos['entry_price']:,}→{cur:,.0f}원 "
                f"({unrealized_pct:+.2f}%, 평가손익 {unrealized:+,.0f}원)"
            )
        est_total = p["cash"] + sum(
            pos["shares"] * (get_current_price_fn(c) or pos["entry_price"])
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

    return "\n".join(lines)
