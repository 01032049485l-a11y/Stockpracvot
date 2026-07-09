# -*- coding: utf-8 -*-
"""
paper_monitor.py - 평일 09:10~15:15 KST 10분마다 실행
보유 중인 가상 포지션의 현재가를 확인해서:
1) 손절가 도달 -> AI 재량 없이 즉시 강제 매도 (유일한 고정 규칙)
2) 가격이 의미 있게 움직였거나(±1.5%), 목표가 근접, 또는 장마감 임박 시
   -> AI에게 그 순간 다시 판단시켜 SELL(익절 확정) 또는 HOLD(계속 보유) 결정
   -> 목표가는 참고치일 뿐, AI가 상황봐서 더 들고 가거나 일찍 팔 수 있음
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import FinanceDataReader as fdr

sys.path.insert(0, ".")
import common
import ai_judge
import paper_trading as pt

KST = ZoneInfo("Asia/Seoul")
PRICE_MOVE_TRIGGER_PCT = 1.5   # 마지막 AI 체크 이후 이 이상 움직이면 재판단
LATE_CHECK_HOUR_MIN = 1430     # 14:30 이후엔 30분마다 강제 재판단 (장마감 대비)
LATE_CHECK_INTERVAL_MIN = 30


def now_kst() -> datetime:
    return datetime.now(KST)


def is_monitor_window() -> bool:
    now = now_kst()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 905 <= hm <= 1515


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


def needs_ai_check(pos: dict, current_price: float) -> bool:
    last_price = pos["last_ai_check_price"]
    move_pct = abs(current_price - last_price) / last_price * 100 if last_price else 0
    if move_pct >= PRICE_MOVE_TRIGGER_PCT:
        return True
    if current_price >= pos["target_price"] and last_price < pos["target_price"]:
        return True  # 목표가 최초 근접/돌파 시점
    now = now_kst()
    hm = now.hour * 100 + now.minute
    if hm >= LATE_CHECK_HOUR_MIN:
        last_check = datetime.fromisoformat(pos["last_ai_check_time"])
        elapsed_min = (now - last_check).total_seconds() / 60
        if elapsed_min >= LATE_CHECK_INTERVAL_MIN:
            return True
    return False


def main():
    if not is_monitor_window():
        print("모니터링 시간대(09:05~15:15)가 아니므로 건너뜁니다.")
        return

    portfolio = pt.load_portfolio()
    if not portfolio["positions"]:
        print("보유 중인 포지션이 없습니다.")
        return

    today_compact = now_kst().strftime("%Y%m%d")
    codes = list(portfolio["positions"].keys())
    print(f"보유 {len(codes)}종목 점검 중...")

    for code in codes:
        pos = portfolio["positions"].get(code)
        if pos is None:
            continue
        name = pos["name"]
        current_price = get_current_price(code, today_compact)
        if current_price is None:
            print(f"  {name}({code}): 현재가 조회 실패, 건너뜀")
            continue

        # 1) 손절 - 고정 규칙, AI 재량 없음
        if current_price <= pos["stop_price"]:
            record = pt.close_position(portfolio, code, current_price, "손절가 도달 (자동)")
            common.send_telegram(pt.format_close_notice(record))
            print(f"  [손절] {name}({code}) @ {current_price:,.0f}원")
            continue

        # 2) AI 동적 재판단
        if needs_ai_check(pos, current_price):
            ai = ai_judge.reevaluate_position(code, name, pos["entry_price"], current_price,
                                               pos["stop_price"], pos["target_price"], pos["entry_time"])
            if ai is None:
                print(f"  {name}({code}): AI 재판단 불가, 보류")
                continue
            if ai["action"] == "SELL":
                record = pt.close_position(portfolio, code, current_price, f"AI 판단: {ai['reason']}")
                common.send_telegram(pt.format_close_notice(record))
                print(f"  [AI 매도] {name}({code}) @ {current_price:,.0f}원 - {ai['reason']}")
            else:
                pos["last_ai_check_price"] = current_price
                pos["last_ai_check_time"] = now_kst().isoformat()
                print(f"  [AI 홀드] {name}({code}) @ {current_price:,.0f}원 - {ai['reason']}")
        else:
            pass  # 유의미한 변화 없음, 조용히 지나감

    pt.save_portfolio(portfolio)


if __name__ == "__main__":
    main()
