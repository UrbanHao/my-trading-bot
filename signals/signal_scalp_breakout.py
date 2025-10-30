# -*- coding: utf-8 -*-
from dataclasses import dataclass
from typing import Optional
import requests

import config

# 優先用 utils.fetch_klines；若不存在則 fallback REST
try:
    from utils import fetch_klines as _fetch_klines
except Exception:
    def _fetch_klines(symbol: str, interval: str, limit: int):
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        arr = r.json()
        closes = [float(x[4]) for x in arr]
        highs  = [float(x[2]) for x in arr]
        lows   = [float(x[3]) for x in arr]
        vols   = [float(x[5]) for x in arr]
        return closes, highs, lows, vols

def _vwap_from_klines(closes, highs, lows, vols) -> Optional[float]:
    if not closes or not vols:
        return None
    v = sum(vols[-21:])
    if v <= 0:
        return closes[-1]
    # 以收盤近似 VWAP，對超伸限制已足夠
    return sum(c * v_ for c, v_ in zip(closes[-21:], vols[-21:])) / v

@dataclass
class ScalpSignal:
    ok: bool
    side: str       # "LONG" / "SHORT"
    entry: float
    reason: str

def scalp_breakout_signal(symbol: str, timeframe: str = "1m") -> ScalpSignal:
    """
    1m 立即突破（不要求回測），帶 VWAP 超伸限制 | return ScalpSignal
    """
    try:
        closes, highs, lows, vols = _fetch_klines(symbol, timeframe, 40)
    except Exception:
        return ScalpSignal(False, "", 0.0, "kline-error")

    if len(closes) < 25:
        return ScalpSignal(False, "", 0.0, "insufficient-bars")

    last = closes[-1]
    prev_high = max(highs[-21:-1])
    prev_low  = min(lows [-21:-1])

    # VWAP 超伸限制（預設 0.40%；若 config 無此鍵則用預設）
    vwap_dist_max = float(getattr(config, "VWAP_DIST_MAX", 0.004))
    vw = _vwap_from_klines(closes, highs, lows, vols)
    if vw and abs(last - vw) / vw > vwap_dist_max:
        return ScalpSignal(False, "", 0.0, "overextended-vwap")

    if last >= prev_high:
        return ScalpSignal(True, "LONG", float(last), "scalp-breakout-long")
    if last <= prev_low:
        return ScalpSignal(True, "SHORT", float(last), "scalp-breakout-short")
    return ScalpSignal(False, "", 0.0, "no-breakout")
