# -*- coding: utf-8 -*-
"""
ai_judge.py - 기술적 신호를 통과한 종목에 대해
1) 최근 뉴스를 수집하고 (네이버 뉴스 검색 API)
2) Claude(Anthropic API)에게 기술적 지표 + 뉴스를 함께 보여주고
   "정말 매수할 만한가 / 목표가 / 예상 도달 기간"을 종합 판단시킨다.

필요한 환경변수(Secrets):
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET  - 네이버 뉴스 검색 API (무료)
  ANTHROPIC_API_KEY                     - Claude API (사용량 과금)
"""
import os
import re
import json
import html
import requests

ANTHROPIC_MODEL = "claude-sonnet-5"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def fetch_news(stock_name: str, display: int = 5) -> list:
    """종목명으로 최근 뉴스 헤드라인을 가져온다. 실패하면 빈 리스트 반환."""
    cid = os.environ.get("NAVER_CLIENT_ID")
    secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        return []
    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
    params = {"query": stock_name, "display": display, "sort": "date"}
    try:
        r = requests.get(NAVER_NEWS_URL, headers=headers, params=params, timeout=8)
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
    except Exception:
        return []

    news = []
    for it in items:
        news.append({
            "title": _strip_tags(it.get("title", "")),
            "date": it.get("pubDate", ""),
            "summary": _strip_tags(it.get("description", "")),
        })
    return news


def _build_prompt(code: str, name: str, ev: dict, tp: dict, news: list) -> str:
    news_block = "\n".join(f"- {n['title']} ({n['date']})" for n in news) if news else "(관련 뉴스 없음)"
    return f"""당신은 신중한 국내주식 초단기 트레이딩 애널리스트입니다.
아래는 기술적 지표 기반 시스템이 1차로 걸러낸 매수 후보 종목입니다.
이 시스템은 "당일~3거래일 이내"의 빠른 상승을 노리는 신호입니다.
완만한 중장기 상승 기대만으로는 부족하고, 며칠 안에 뚜렷하게 움직일
근거(강한 모멘텀, 임박한 재료, 명확한 뉴스 촉매 등)가 있을 때만 BUY로 판단하세요.
확신이 부족하면 과감히 PASS 하세요.

기술적 신호, 최근 뉴스, 그리고 당신이 알고 있는 해당 종목/업종/시장 관련
다른 유효한 정보(업황, 경쟁사 동향, 최근 실적 흐름, 거시 환경 등)까지 폭넓게
종합해서 판단해주세요.

[종목] {name} ({code})
[현재가] {ev['close']:,.0f}원
[기술적 판단] {ev['verdict']} (규칙기반 신뢰도 {ev['confidence']*100:.0f}%)
[추세] {ev['trend']}
[기술적 근거]
{chr(10).join('- ' + r for r in ev['reasons'])}
[규칙기반 목표가] {tp['target']:,}원 / 손절가 {tp['stop']:,}원

[최근 뉴스 헤드라인]
{news_block}

다음 기준으로 신중하게 판단하세요:
- 뉴스에 악재(실적 부진, 소송, 규제, 경영진 리스크 등)가 있으면 기술적 신호가 좋아도 PASS
- 뉴스가 중립/무관하면 기술적 신호를 신뢰
- 뉴스가 명확한 호재(신규 계약, 실적 서프라이즈, 정책 수혜 등)면 confidence를 높여도 됨
- 뉴스 외에도 업황, 경쟁사 상황, 최근 실적 추이, 거시경제(금리/환율 등)처럼
  당신이 알고 있는 관련 정보가 있다면 반드시 판단에 반영하세요
- target_days는 반드시 0~3 사이의 정수로만 답하세요 (0=오늘 중, 1=익일, 2~3=2~3거래일 내).
  이보다 긴 호흡이 필요해 보이는 종목은 BUY가 아니라 PASS로 처리하세요
- target_price는 0~3거래일이라는 짧은 기간 안에 현실적으로 도달 가능한 수준으로
  판단하세요. 규칙기반 목표가는 참고용이며, 기간이 짧으므로 그보다 낮게 잡는 것이
  일반적입니다. 재료가 매우 강할 때만 규칙기반 목표가 수준까지 볼 수 있습니다

판단에 사용한 근거는 "reasons" 배열에 항목별로 나눠 담아주세요. 각 항목은:
- 15~40자 내외로 간결하게, 어떤 요인인지 앞에 태그를 붙여서 작성
  (예: "[기술적] 골든크로스와 거래량 급증 동반", "[뉴스] 2분기 실적 서프라이즈 발표",
   "[업황] 반도체 업사이클 진입 국면", "[리스크] 밸류에이션 부담 존재" 등)
- 매수(BUY) 판단이면 근거가 되는 긍정 요인 위주로 3~5개
- 반려(PASS) 판단이면 반려 사유가 되는 요인 위주로 2~4개
- 근거는 반드시 위에 제시된 기술적 지표/뉴스 내용 또는 당신이 실제로 알고 있는
  사실에 기반해야 하며, 확인되지 않은 내용을 지어내지 마세요

반드시 아래 JSON 형식으로만 답하세요. 다른 설명 텍스트는 절대 포함하지 마세요:
{{"decision": "BUY 또는 PASS", "target_price": 정수, "target_days": 0~3 사이 정수, "confidence": 0~100 정수, "summary": "한 줄 종합 요약(30자 내외)", "reasons": ["근거1", "근거2", "근거3"]}}"""


def ai_analyze(code: str, name: str, ev: dict, tp: dict, news: list) -> dict | None:
    """Claude API 호출. 실패하거나 파싱 안 되면 None 반환 (호출부에서 스킵 처리)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  [경고] ANTHROPIC_API_KEY가 없어 AI 판단을 건너뜁니다.")
        return None

    prompt = _build_prompt(code, name, ev, tp, news)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=30)
        if r.status_code != 200:
            print(f"  [경고] Claude API 오류 {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = text.strip()
        # 혹시 코드블록으로 감싸져 오면 제거
        text = re.sub(r"^```(json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        parsed = json.loads(text)
        # 필수 필드 검증
        for f in ("decision", "target_price", "target_days", "confidence", "summary", "reasons"):
            if f not in parsed:
                return None
        if not isinstance(parsed["reasons"], list) or not parsed["reasons"]:
            return None
        return parsed
    except Exception as e:
        print(f"  [경고] AI 판단 파싱 실패: {e}")
        return None


def format_ai_alert(code: str, name: str, ev: dict, ai: dict, news: list) -> str:
    reasons_lines = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(ai["reasons"]))
    news_lines = "\n".join(f"  · {n['title']}" for n in news[:3]) if news else "  · (관련 뉴스 없음)"
    days = ai["target_days"]
    when = "오늘 중" if days <= 0 else f"약 {days}거래일 이내"
    return (
        f"<b>🤖 AI 종합 매수 신호</b>\n"
        f"종목: <b>{name}</b> ({code})\n"
        f"현재가: {ev['close']:,.0f}원\n"
        f"AI 목표매도가: {ai['target_price']:,}원\n"
        f"예상 도달 시점: {when}\n"
        f"AI 신뢰도: {ai['confidence']}%   (기술적 신뢰도 {ev['confidence']*100:.0f}%)\n"
        f"\n"
        f"📌 왜 오를 것으로 판단했나\n"
        f"{ai['summary']}\n"
        f"{reasons_lines}\n"
        f"\n"
        f"참고 뉴스:\n{news_lines}\n"
        f"─────────────\n"
        f"※ AI가 뉴스·지표·업황 등을 종합한 추정치이며 투자 권유가 아닙니다."
    )
