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
from datetime import datetime
from zoneinfo import ZoneInfo

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


FUNDAMENTAL_URL = "https://finance.naver.com/item/main.naver"


def fetch_fundamentals(code: str) -> dict:
    """네이버 금융에서 PER/PBR을 가져온다. 실패하면 빈 dict 반환(프롬프트에서 N/A 처리)."""
    try:
        r = requests.get(FUNDAMENTAL_URL, params={"code": code},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code != 200:
            return {}
        html_text = r.text
        per_m = re.search(r'id="_per"[^>]*>\s*([\d,\.]+)', html_text)
        pbr_m = re.search(r'id="_pbr"[^>]*>\s*([\d,\.]+)', html_text)
        result = {}
        if per_m:
            result["per"] = per_m.group(1)
        if pbr_m:
            result["pbr"] = pbr_m.group(1)
        return result
    except Exception:
        return {}


def fetch_earnings_news(stock_name: str, display: int = 3) -> list:
    """실적/컨퍼런스콜 관련 뉴스를 별도로 가져온다 (fetch_news와 동일 API, 쿼리만 다름)."""
    return fetch_news(f"{stock_name} 실적", display=display)


def _build_prompt(code: str, name: str, ev: dict, tp: dict, news: list,
                   fundamentals: dict = None, earnings_news: list = None,
                   market_sentiment: dict = None) -> str:
    fundamentals = fundamentals or {}
    earnings_news = earnings_news or []
    news_block = "\n".join(f"- {n['title']} ({n['date']})" for n in news) if news else "(관련 뉴스 없음)"
    earnings_block = "\n".join(f"- {n['title']} ({n['date']})" for n in earnings_news) if earnings_news else "(관련 실적 뉴스 없음)"
    per = fundamentals.get("per", "N/A")
    pbr = fundamentals.get("pbr", "N/A")

    if market_sentiment:
        sentiment_block = (
            f"[시장 전체 심리 - 자체 산출 공포/탐욕 지수: {market_sentiment['score']}/100 "
            f"({market_sentiment['label']})]\n"
            f"(0에 가까울수록 시장 전체가 극단적 공포·과매도, 100에 가까울수록 극단적 탐욕·과열 상태.\n"
            f" 오늘 스캔한 코스피/코스닥 전종목 중 20일선 위/RSI 50 이상 비율로 자체 산출)\n"
        )
    else:
        sentiment_block = "[시장 전체 심리] 산출 안 됨\n"

    return f"""당신은 30년 경력의 월스트리트 트레이더 출신 전문 애널리스트입니다.
워런 버핏이 조언을 구할 정도로 이 분야에 정통하며, 뉴스 헤드라인이나 단순 차트 패턴에
휩쓸리지 않고 실적, 밸류에이션, 거시 환경, 시장 전체 심리까지 종합해서 냉정하게
판단하는 것으로 유명합니다. 유행이나 소문이 아니라 데이터와 근거로만 결론을 냅니다.

※ 이 시스템은 대한민국 국내 증시(코스피/코스닥)에 상장된 종목만 다룹니다.
  해외 종목은 절대 고려하거나 언급하지 마세요.

아래는 기술적 지표 기반 시스템이 1차로 걸러낸 국내주식 매수 후보입니다.
이 신호는 "당일~3거래일 이내"의 빠른 상승을 노리는 단기 신호이지만,
판단 자체는 중장기 투자자의 안목으로 진행하세요. 즉, 단기 차트가 좋아 보여도
밸류에이션이 지나치게 부담스럽거나, 시장 전체가 과열(탐욕) 국면이라 단기 조정
위험이 크거나, 펀더멘털에 구조적 문제가 있다면 과감히 PASS 하세요.
반대로 시장이 과도한 공포로 짓눌려 있는데 이 종목만 개별 호재로 반등하는
그림이면, 그 근거가 확실할 때 오히려 확신 있게 BUY 할 수 있습니다.

[종목] {name} ({code})
[현재가] {ev['close']:,.0f}원
[밸류에이션] PER {per}배 / PBR {pbr}배
[기술적 판단] {ev['verdict']} (규칙기반 신뢰도 {ev['confidence']*100:.0f}%)
[추세] {ev['trend']}
[기술적 근거 - 이동평균크로스/RSI/MACD/볼린저밴드/거래량]
{chr(10).join('- ' + r for r in ev['reasons'])}
[규칙기반 목표가] {tp['target']:,}원 / 손절가 {tp['stop']:,}원

{sentiment_block}
[최근 뉴스 헤드라인]
{news_block}

[실적/컨퍼런스콜 관련 뉴스]
{earnings_block}

다음 기준으로 신중하게 판단하세요:
- 뉴스에 악재(실적 부진, 소송, 규제, 경영진 리스크 등)가 있으면 기술적 신호가 좋아도 PASS
- PER/PBR이 동종업계 대비 지나치게 고평가된 상태라면(당신의 지식 기준으로 판단) 신중하게 접근하고 confidence를 낮추세요. 밸류에이션 데이터가 N/A이면 이 조건은 건너뛰세요
- 실적/컨퍼런스콜 뉴스에 가이던스 하향, 어닝 쇼크 등이 있으면 기술적 신호와 무관하게 PASS
- 시장 전체 심리(공포/탐욕 지수)가 극단적 탐욕(80 이상)이면 개별 종목이 아무리 좋아도 단기 과열/조정 위험을 감안해 신중하게, 극단적 공포(20 이하)면 진짜 옥석만 가려 신중하되 기회로 볼 수 있음
- 뉴스 외에도 업황, 경쟁사 상황, 최근 실적 추이, 거시경제(금리/환율 등)처럼
  당신이 알고 있는 관련 정보가 있다면 반드시 판단에 반영하세요
- target_days는 반드시 0~3 사이의 정수로만 답하세요 (0=오늘 중, 1=익일, 2~3=2~3거래일 내).
  이보다 긴 호흡이 필요해 보이는 종목은 BUY가 아니라 PASS로 처리하세요
- target_price는 0~3거래일이라는 짧은 기간 안에 현실적으로 도달 가능한 수준으로
  판단하세요. 규칙기반 목표가는 참고용이며, 기간이 짧으므로 그보다 낮게 잡는 것이
  일반적입니다. 재료가 매우 강할 때만 규칙기반 목표가 수준까지 볼 수 있습니다

판단에 사용한 근거는 "reasons" 배열에 항목별로 나눠 담아주세요. 각 항목은:
- 15~40자 내외로 간결하게, 어떤 요인인지 앞에 태그를 붙여서 작성
  (예: "[기술적] 골든크로스와 거래량 급증 동반", "[밸류에이션] PER 업종 평균 하회",
   "[실적] 컨센서스 상회 어닝 서프라이즈", "[시장심리] 과열 국면으로 단기 조정 유의",
   "[업황] 반도체 업사이클 진입 국면", "[리스크] 밸류에이션 부담 존재" 등)
- 매수(BUY) 판단이면 근거가 되는 긍정 요인 위주로 3~5개
- 반려(PASS) 판단이면 반려 사유가 되는 요인 위주로 2~4개
- 근거는 반드시 위에 제시된 데이터 또는 당신이 실제로 알고 있는 사실에 기반해야
  하며, 확인되지 않은 내용을 지어내지 마세요

반드시 아래 JSON 형식으로만 답하세요. 다른 설명 텍스트는 절대 포함하지 마세요:
{{"decision": "BUY 또는 PASS", "target_price": 정수, "target_days": 0~3 사이 정수, "confidence": 0~100 정수, "summary": "한 줄 종합 요약(30자 내외)", "reasons": ["근거1", "근거2", "근거3"]}}"""


def ai_analyze(code: str, name: str, ev: dict, tp: dict, news: list,
                fundamentals: dict = None, earnings_news: list = None,
                market_sentiment: dict = None) -> dict | None:
    """Claude API 호출. 실패하거나 파싱 안 되면 None 반환 (호출부에서 스킵 처리)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  [경고] ANTHROPIC_API_KEY가 없어 AI 판단을 건너뜁니다.")
        return None

    prompt = _build_prompt(code, name, ev, tp, news, fundamentals, earnings_news, market_sentiment)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 500,
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


def format_watchlist_alert(code: str, name: str, ev: dict, ai: dict, news: list) -> str:
    """확정 매수신호가 하루 종일 하나도 없을 때, 그나마 근접했던 종목을
    '참고용 관찰종목'으로 명확히 구분해서 보여주는 포맷. 매수 추천이 아님을 강조."""
    reasons_lines = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(ai["reasons"]))
    news_lines = "\n".join(f"  · {n['title']}" for n in news[:3]) if news else "  · (관련 뉴스 없음)"
    return (
        f"<b>🔎 참고용 관찰종목 (매수 신호 아님)</b>\n"
        f"종목: <b>{name}</b> ({code})\n"
        f"현재가: {ev['close']:,.0f}원\n"
        f"AI 신뢰도: {ai['confidence']}%   (매수신호 기준선 70%에는 미달)\n"
        f"AI 판단: {ai['decision']}\n"
        f"\n"
        f"📌 이 종목이 그나마 근접했던 이유 / 아쉬웠던 점\n"
        f"{ai['summary']}\n"
        f"{reasons_lines}\n"
        f"\n"
        f"참고 뉴스:\n{news_lines}\n"
        f"─────────────\n"
        f"※ 확신 있는 매수신호가 아닌 참고용 정보입니다. 매수 권유가 아닙니다."
    )


POSITION_MODEL = "claude-sonnet-5"


def reevaluate_position(code: str, name: str, entry_price: float, current_price: float,
                         stop_price: float, target_price: float, entry_time: str) -> dict | None:
    """이미 보유 중인 포지션을 그 순간 다시 판단: 지금 팔아서 확정할지, 더 들고 갈지.
    목표는 '오늘 하루 동안 이 포지션의 수익 최대화'. 목표가는 참고치일 뿐 절대기준 아님.
    손절가는 이 함수 호출 전에 이미 별도로 강제 처리되므로 여기서는 다루지 않는다."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    pnl_pct = (current_price - entry_price) / entry_price * 100
    to_target_pct = (target_price - current_price) / current_price * 100 if current_price else 0
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    hm = now.hour * 100 + now.minute
    near_close = hm >= 1430

    prompt = f"""당신은 당일 데이트레이딩 포지션을 관리하는 30년 경력 트레이더입니다.
목표는 단 하나, "오늘 장 마감 전까지 이 포지션에서 실현손익을 최대화"하는 것입니다.
목표가는 아침에 잡아둔 참고치일 뿐 절대적인 매도 기준이 아닙니다.
지금 이 순간의 모멘텀을 보고 스스로 판단하세요.

[종목] {name} ({code})
[매수가] {entry_price:,.0f}원
[현재가] {current_price:,.0f}원 (현재 수익률 {pnl_pct:+.2f}%)
[원래 목표가] {target_price:,.0f}원 (남은 거리 {to_target_pct:+.2f}%)
[매수 시각] {entry_time}
[현재 시각] {now.strftime('%H:%M')} {"(장마감 임박, 15:30 전 반드시 청산되어야 함)" if near_close else ""}

판단 기준:
- 상승 모멘텀이 아직 살아있고 추가 상승 여력이 보이면 HOLD (목표가를 넘어서도 더 들고 가도 됨)
- 상승 탄력이 눈에 띄게 죽었거나, 목표가 근처에서 정체/반락 조짐이 보이면 SELL로 지금 이익을 확정
- 장마감이 임박했는데(15:30 전) 어중간하게 플러스 상태면, 수익을 그냥 반납하고 강제청산 당하기보다
  지금 SELL로 확정하는 쪽을 더 적극적으로 고려하세요
- 현재 마이너스 상태라도 손절가에는 아직 안 닿았고 반등 근거가 있다면 HOLD 가능(단, 장마감 임박시엔 신중하게)

반드시 아래 JSON 형식으로만 답하세요:
{{"action": "SELL 또는 HOLD", "reason": "1문장 이내 간결한 근거"}}"""

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {"model": POSITION_MODEL, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]}
    try:
        r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=20)
        if r.status_code != 200:
            print(f"  [경고] 포지션 재판단 API 오류 {r.status_code}")
            return None
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        text = re.sub(r"^```(json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        parsed = json.loads(text)
        if "action" not in parsed or "reason" not in parsed:
            return None
        return parsed
    except Exception as e:
        print(f"  [경고] 포지션 재판단 파싱 실패: {e}")
        return None


def format_ai_alert(code: str, name: str, ev: dict, ai: dict, news: list, rank: dict = None) -> str:
    reasons_lines = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(ai["reasons"]))
    news_lines = "\n".join(f"  · {n['title']}" for n in news[:3]) if news else "  · (관련 뉴스 없음)"
    days = ai["target_days"]
    when = "오늘 중" if days <= 0 else f"약 {days}거래일 이내"
    rank_line = f"종합순위 점수: {rank['score']}점 (신뢰도+수익률 합산)\n" if rank else ""
    return (
        f"<b>🟢🤖 AI 종합 매수 신호</b>\n"
        f"종목: <b>{name}</b> ({code})\n"
        f"현재가: {ev['close']:,.0f}원\n"
        f"AI 목표매도가: {ai['target_price']:,}원 (예상 수익률 +{rank['return_pct'] if rank else 0:.1f}%)\n"
        f"예상 도달 시점: {when}\n"
        f"AI 신뢰도: {ai['confidence']}%   (기술적 신뢰도 {ev['confidence']*100:.0f}%)\n"
        f"{rank_line}"
        f"\n"
        f"📌 왜 오를 것으로 판단했나\n"
        f"{ai['summary']}\n"
        f"{reasons_lines}\n"
        f"\n"
        f"참고 뉴스:\n{news_lines}\n"
        f"─────────────\n"
        f"※ AI가 뉴스·지표·업황 등을 종합한 추정치이며 투자 권유가 아닙니다."
    )
