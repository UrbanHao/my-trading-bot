# -*- coding: utf-8 -*-
from dataclasses import dataclass
from typing import Optional
import statistics
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
    return sum(c * v_ for c, v_ in zip(closes[-21:], vols[-21:])) / v

def _zscore(vals) -> Optional[float]:
    if len(vals) < 2:
        return None
    m = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    if sd == 0:
        return None
    return (vals[-1] - m) / sd

@dataclass
class ScalpSignal:
    ok: bool
    side: str       # "LONG" / "SHORT"
    entry: float
    reason: str

def scalp_vwap_signal(symbol: str, timeframe: str = "1m") -> ScalpSignal:
    """
    1m VWAP 均值回歸：|z| >= zthr（預設 1.0）時反向進場 | return ScalpSignal
    """
    try:
        closes, highs, lows, vols = _fetch_klines(symbol, timeframe, 40)
    except Exception:
        return ScalpSignal(False, "", 0.0, "kline-error")

    if len(closes) < 25:
        return ScalpSignal(False, "", 0.0, "insufficient-bars")

    last = closes[-1]
    vw = _vwap_from_klines(closes, highs, lows, vols)
    if vw is None:
        return ScalpSignal(False, "", 0.0, "no-vwap")

    dev = _zscore(closes[-21:])
    if dev is None:
        return ScalpSignal(False, "", 0.0, "no-z")

    zthr = float(getattr(config, "SCALP_Z_THR", 1.0))
    if dev <= -zthr:
        return ScalpSignal(True, "LONG", float(last), f"vwap-meanrev-long z={dev:.2f}")
    if dev >=  zthr:
        return ScalpSignal(True, "SHORT", float(last), f"vwap-meanrev-short z={dev:.2f}")
    return ScalpSignal(False, "", 0.0, "no-signal")
