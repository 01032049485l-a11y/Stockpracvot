# -*- coding: utf-8 -*-
"""
telegram_command.py - 몇 분마다 실행되어 텔레그램 채팅에 새 메시지가 있는지 확인하고,
"상태"/"현황"/"/status" 같은 명령어를 인식하면 모의매매 현재 상황으로 자동 응답한다.

보안: 반드시 TELEGRAM_CHAT_ID로 등록된 본인 채팅에서 온 메시지만 처리한다
(저장소가 Public이라도 봇 토큰 자체는 Secret이라 타인이 알 방법이 없지만, 이중 안전장치).
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import FinanceDataReader as fdr

sys.path.insert(0, ".")
import common
import paper_trading as pt

KST = ZoneInfo("Asia/Seoul")
OFFSET_FILE = "telegram_offset.json"

STATUS_KEYWORDS = {"/status", "상태", "현황", "포트폴리오", "모의매매", "진행상황"}
HELP_KEYWORDS = {"/help", "도움말", "명령어"}

HELP_TEXT = (
    "<b>🤖 사용 가능한 명령어</b>\n"
    "  · 상태 / 현황 / 포트폴리오 → 모의매매 실시간 현황\n"
    "  · 도움말 → 이 안내"
)


def get_current_price(code: str) -> float | None:
    today_compact = datetime.now(KST).strftime("%Y%m%d")
    try:
        df = fdr.DataReader(code, today_compact[:4] + "-" + today_compact[4:6] + "-" + today_compact[6:])
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return float(df.iloc[-1]["Close"])


def get_updates(offset: int) -> list:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("[경고] TELEGRAM_BOT_TOKEN이 없습니다.")
        return []
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
        if r.status_code != 200:
            print(f"[경고] getUpdates 실패 {r.status_code}: {r.text[:200]}")
            return []
        return r.json().get("result", [])
    except Exception as e:
        print(f"[경고] getUpdates 예외: {e}")
        return []


def main():
    my_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    state = common.load_json(OFFSET_FILE, {"offset": 0})
    offset = state.get("offset", 0)

    updates = get_updates(offset)
    if not updates:
        print("새 메시지 없음.")
        return

    max_update_id = offset - 1
    for upd in updates:
        max_update_id = max(max_update_id, upd.get("update_id", 0))
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()

        if my_chat_id and chat_id != str(my_chat_id):
            print(f"[무시] 등록된 채팅이 아님 (chat_id={chat_id})")
            continue

        text_norm = text.lower()
        if text_norm in STATUS_KEYWORDS or text in STATUS_KEYWORDS:
            print(f"[명령어] 상태조회 요청: {text}")
            status_msg = pt.build_status_message(get_current_price)
            common.send_telegram(status_msg)
        elif text_norm in HELP_KEYWORDS or text in HELP_KEYWORDS:
            print(f"[명령어] 도움말 요청: {text}")
            common.send_telegram(HELP_TEXT)
        else:
            print(f"[무시] 인식되지 않는 메시지: {text[:50]}")

    common.save_json(OFFSET_FILE, {"offset": max_update_id + 1})


if __name__ == "__main__":
    main()
