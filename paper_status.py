# -*- coding: utf-8 -*-
"""
paper_status.py - 언제든 수동 실행해서 모의매매 현재 상황을 텔레그램으로 받는다.
(예약 실행 없음, GitHub Actions "Run workflow" 버튼을 누를 때만 동작)
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import FinanceDataReader as fdr

sys.path.insert(0, ".")
import common
import paper_trading as pt

KST = ZoneInfo("Asia/Seoul")


def get_current_price(code: str) -> float | None:
    today_compact = datetime.now(KST).strftime("%Y%m%d")
    try:
        df = fdr.DataReader(code, today_compact[:4] + "-" + today_compact[4:6] + "-" + today_compact[6:])
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return float(df.iloc[-1]["Close"])


def main():
    msg = pt.build_status_message(get_current_price)
    common.send_telegram(msg)
    print(msg)


if __name__ == "__main__":
    main()
