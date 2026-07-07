# -*- coding: utf-8 -*-
"""
common.py - 국내주식 신호 시스템 공통 모듈
- 지표 계산 (이동평균, RSI, MACD, 볼린저밴드, 거래량, ATR)
- 정확도를 높이기 위한 '엄격 판정' 로직:
    1) 장기추세 필터: MA60 대비 위치로 상승/하락 추세 확인
    2) 다중지표 확인(confluence): 5개 지표 중 다수가 같은 방향일 때만 신호 채택
    3) 거래량 검증: 거래량이 평균 이상 실릴 때만 신호 채택
- 목표매도가 / 손절가: ATR(변동성) 기반 산출 (위험대비수익비 약 1:2)
- 텔레그램 메시지 전송
"""
import os
import json
import numpy as np
import pandas as pd


# ----------------------------------------------------------------
# 지표 계산
# ----------------------------------------------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    df["bb_mid"] = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    band_width = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
    df["bb_pctb"] = (df["close"] - df["bb_lower"]) / band_width

    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"].replace(0, np.nan)

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    return df


# ----------------------------------------------------------------
# 개별 지표 판정 (-1 ~ +1)
# ----------------------------------------------------------------
def _ma_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    gap_now, gap_prev = last["ma5"] - last["ma20"], prev["ma5"] - prev["ma20"]
    if gap_prev <= 0 < gap_now:
        return 1.0, "골든크로스 발생"
    if gap_prev >= 0 > gap_now:
        return -1.0, "데드크로스 발생"
    return (0.3 if gap_now > 0 else -0.3), "이평 정배열/역배열 유지"


def _rsi_signal(df, trend_up: bool, trend_down: bool):
    v = df["rsi"].iloc[-1]
    prev = df["rsi"].iloc[-2]
    rising = v > prev
    if v < 30:
        return 1.0, f"RSI 과매도({v:.0f}) - 반등 구간"
    if v >= 85:
        return -1.0, f"RSI 극단 과열({v:.0f})"
    if trend_up and 45 < v < 85 and rising:
        return 0.5, f"RSI 상승 모멘텀({v:.0f})"
    if trend_down and 15 < v < 55 and not rising:
        return -0.5, f"RSI 하락 모멘텀({v:.0f})"
    if v > 70 and trend_down:
        return -1.0, f"하락추세 중 RSI 과매수({v:.0f}) - 반락 주의"
    return 0.0, f"RSI 중립({v:.0f})"


def _macd_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    if prev["macd_hist"] <= 0 < last["macd_hist"]:
        return 1.0, "MACD 골든크로스"
    if prev["macd_hist"] >= 0 > last["macd_hist"]:
        return -1.0, "MACD 데드크로스"
    return (0.3 if last["macd_hist"] > 0 else -0.3), "MACD 추세 유지"


def _bb_signal(df, trend_up: bool, trend_down: bool, vol_ratio: float):
    v = df["bb_pctb"].iloc[-1]
    if pd.isna(v):
        return 0.0, "데이터 부족"
    strong_vol = (not pd.isna(vol_ratio)) and vol_ratio >= 1.5
    if v >= 0.95:
        if trend_up and strong_vol:
            return 0.5, "상승추세 + 대량거래 밴드 상단 돌파 (강세 지속)"
        return -1.0, "볼린저 상단 근접/이탈 - 과열 주의"
    if v <= 0.05:
        if trend_down and strong_vol:
            return -0.5, "하락추세 + 대량거래 밴드 하단 이탈 (약세 지속)"
        return 1.0, "볼린저 하단 근접/이탈 - 반등 구간"
    return 0.0, "밴드 중앙"


def _volume_signal(df):
    last = df.iloc[-1]
    ratio = last["vol_ratio"]
    if pd.isna(ratio):
        return 0.0, "데이터 부족", 0.0
    price_up = last["close"] > df["close"].iloc[-2]
    if ratio >= 1.5 and price_up:
        return 0.6, f"거래량 {ratio:.1f}배 + 상승", ratio
    if ratio >= 1.5 and not price_up:
        return -0.6, f"거래량 {ratio:.1f}배 + 하락", ratio
    return 0.0, f"거래량 평상({ratio:.1f}배)", ratio


# ----------------------------------------------------------------
# 종합 판정 (엄격 기준: 추세필터 + 다중지표확인 + 거래량검증)
# ----------------------------------------------------------------
def evaluate(df: pd.DataFrame) -> dict:
    """
    return: {
      verdict: 'BUY'|'SELL'|'NEUTRAL',
      confidence: 0~1,
      score: float,
      reasons: [str,...],
      close, ma60, ...
    }
    """
    if len(df) < 65:
        return {"verdict": "NEUTRAL", "confidence": 0, "score": 0, "reasons": ["데이터 부족"]}

    last = df.iloc[-1]

    # 추세/거래량을 먼저 판정해서 각 지표 해석에 맥락으로 전달
    trend_up = last["close"] > last["ma60"]
    trend_down = last["close"] < last["ma60"]
    vol_s, vol_msg, vol_ratio = _volume_signal(df)

    ma_s, ma_msg = _ma_signal(df)
    rsi_s, rsi_msg = _rsi_signal(df, trend_up, trend_down)
    macd_s, macd_msg = _macd_signal(df)
    bb_s, bb_msg = _bb_signal(df, trend_up, trend_down, vol_ratio)

    signals = [ma_s, rsi_s, macd_s, bb_s]  # 거래량은 '검증' 용도로 별도 처리
    n_pos = sum(1 for s in signals if s > 0)
    n_neg = sum(1 for s in signals if s < 0)

    weighted = ma_s * 1.5 + rsi_s * 1.0 + macd_s * 1.5 + bb_s * 1.0 + vol_s * 0.8

    # 다중지표 확인: 4개 중 3개 이상 동일 방향이어야 '신호'로 인정
    confluence_buy = n_pos >= 3
    confluence_sell = n_neg >= 3

    # 3) 거래량 검증: 평균 대비 1.2배 이상 실려야 신뢰도 인정
    volume_ok = (not pd.isna(vol_ratio)) and vol_ratio >= 1.2

    verdict = "NEUTRAL"
    confidence = 0.0
    reasons = [ma_msg, rsi_msg, macd_msg, bb_msg, vol_msg]

    if confluence_buy and trend_up and volume_ok and weighted > 0:
        verdict = "BUY"
        confidence = min(1.0, (n_pos / 4) * 0.6 + min(vol_ratio / 3, 1) * 0.4)
    elif confluence_sell and trend_down and volume_ok and weighted < 0:
        verdict = "SELL"
        confidence = min(1.0, (n_neg / 4) * 0.6 + min(vol_ratio / 3, 1) * 0.4)

    return {
        "verdict": verdict,
        "confidence": round(confidence, 2),
        "score": round(weighted, 2),
        "reasons": reasons,
        "close": float(last["close"]),
        "ma60": float(last["ma60"]) if not pd.isna(last["ma60"]) else None,
        "atr14": float(last["atr14"]) if not pd.isna(last["atr14"]) else None,
        "trend": "상승추세" if trend_up else ("하락추세" if trend_down else "횡보"),
        "volume_ok": volume_ok,
        "n_pos": n_pos,
        "n_neg": n_neg,
    }


# ----------------------------------------------------------------
# 목표매도가 / 손절가 (ATR 변동성 기반, 위험대비수익 약 1:2)
# ----------------------------------------------------------------
def price_targets(entry: float, atr: float, direction: str) -> dict:
    if atr is None or atr <= 0:
        atr = entry * 0.01  # ATR 계산 불가시 1% 대체
    if direction == "BUY":
        stop = entry - 1.5 * atr
        target = entry + 3.0 * atr
    else:  # SELL(공매도 관점이 아니라 '하락 예상' 참고용 - 반등시 재매수 손절 기준)
        stop = entry + 1.5 * atr
        target = entry - 3.0 * atr
    return {
        "entry": round(entry),
        "stop": round(stop),
        "target": round(target),
        "risk_reward": "1:2",
    }


# ----------------------------------------------------------------
# 텔레그램 전송
# ----------------------------------------------------------------
def send_telegram(text: str):
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[경고] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없습니다. 콘솔에만 출력합니다.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    if resp.status_code != 200:
        print(f"[텔레그램 전송 실패] {resp.status_code} {resp.text}")


def format_alert(code: str, name: str, ev: dict, tp: dict) -> str:
    direction = "🔴 상승 신호 (매수 관점)" if ev["verdict"] == "BUY" else "🔵 하락 신호 (매도/주의 관점)"
    reasons = "\n".join(f"  · {r}" for r in ev["reasons"] if "중립" not in r and "평상" not in r and "유지" not in r or True)
    return (
        f"<b>{direction}</b>\n"
        f"종목: <b>{name}</b> ({code})\n"
        f"현재가(기준가): {tp['entry']:,}원\n"
        f"목표매도가(예상): {tp['target']:,}원\n"
        f"손절가(참고): {tp['stop']:,}원  (위험대비수익 {tp['risk_reward']})\n"
        f"신뢰도: {ev['confidence']*100:.0f}%   추세: {ev['trend']}\n"
        f"근거:\n{reasons}\n"
        f"─────────────\n"
        f"※ 과거 데이터 기반 기술적 추정치이며 투자 권유가 아닙니다."
    )


# ----------------------------------------------------------------
# 상태 파일 (중복 알림 방지)
# ----------------------------------------------------------------
def load_json(path: str, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
