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
        print(f"[진단] getUpdates 응답 코드: {r.status_code}")
        if r.status_code != 200:
            print(f"[경고] getUpdates 실패 {r.status_code}: {r.text[:300]}")
            return []
        result = r.json().get("result", [])
        print(f"[진단] 조회된 업데이트 개수: {len(result)} (offset={offset})")
        return result
    except Exception as e:
        print(f"[경고] getUpdates 예외: {e}")
        return []


def matches(text: str, keywords: set) -> bool:
    """정확히 일치하거나, 메시지 안에 키워드가 포함되어 있으면 매칭 (공백/문장부호에 관대하게)"""
    t = text.strip().lower()
    if t in keywords:
        return True
    return any(kw.lower() in t for kw in keywords)


def main():
    my_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    print(f"[진단] TELEGRAM_BOT_TOKEN 존재: {bool(token)}, TELEGRAM_CHAT_ID: {my_chat_id}")

    state = common.load_json(OFFSET_FILE, {"offset": 0})
    offset = state.get("offset", 0)
    print(f"[진단] 현재 저장된 offset: {offset}")

    updates = get_updates(offset)
    if not updates:
        print("새 메시지 없음.")
        return

    max_update_id = offset - 1
    for upd in updates:
        max_update_id = max(max_update_id, upd.get("update_id", 0))
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            print(f"[진단] message 필드 없는 업데이트 (예: 콜백 등): {upd}")
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        print(f"[진단] 수신 메시지: chat_id={chat_id}, text='{text}'")

        if my_chat_id and chat_id != str(my_chat_id):
            print(f"[무시] 등록된 채팅({my_chat_id})이 아님 (수신 chat_id={chat_id})")
            continue

        if matches(text, STATUS_KEYWORDS):
            print(f"[명령어] 상태조회 요청: {text}")
            status_msg = pt.build_status_message(get_current_price)
            common.send_telegram(status_msg)
        elif matches(text, HELP_KEYWORDS):
            print(f"[명령어] 도움말 요청: {text}")
            common.send_telegram(HELP_TEXT)
        else:
            print(f"[무시] 인식되지 않는 메시지: {text[:50]}")

    common.save_json(OFFSET_FILE, {"offset": max_update_id + 1})
    print(f"[진단] offset을 {max_update_id + 1}로 갱신함")


if __name__ == "__main__":
    main()
