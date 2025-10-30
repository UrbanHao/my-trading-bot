# signals/signal_scalp_breakout.py
from dataclasses import dataclass
import time
import config
from utils import vwap_from_klines, last_close

@dataclass
class ScalpSignal:
    ok: bool
    side: str        # "LONG" / "SHORT"
    entry: float
    reason: str

def scalp_breakout_signal(symbol, ws_client) -> ScalpSignal:
    k1m = ws_client.get_k1m(symbol, n=30)
    m = ws_client.get_micro(symbol)
    c = last_close(k1m)
    if not k1m or c is None or m["obi"] is None or m["trade_buy_ratio"] is None or m["spread"] is None:
        return ScalpSignal(False, "", 0.0, "insufficient-data")

    # 檢查價差與 VWAP 超伸
    vwap = vwap_from_klines(k1m, lookback=20)
    if vwap and abs(c - vwap)/vwap > config.VWAP_DIST_MAX:
        return ScalpSignal(False, "", 0.0, "overextended-vwap")
    if m["spread"] > config.SPREAD_MAX_PCT:
        return ScalpSignal(False, "", 0.0, "wide-spread")

    # 立即突破條件：收盤創近 N 高/低 + OBI + 主動量
    highs = max(h for (_,h,_,_,_,_) in k1m[-20:])
    lows  = min(l for (_,_,l,_,_,_) in k1m[-20:])
    buy_bias  = (m["obi"] >= config.OBI_THRESHOLD) and (m["trade_buy_ratio"] is not None and m["trade_buy_ratio"] >= 0.55)
    sell_bias = (m["obi"] <= (1.0 - config.OBI_THRESHOLD)) and (m["trade_buy_ratio"] is not None and m["trade_buy_ratio"] <= 0.45)

    if c >= highs and buy_bias:
        return ScalpSignal(True, "LONG", float(c), "breakout-long")
    if c <= lows and sell_bias:
        return ScalpSignal(True, "SHORT", float(c), "breakout-short")

    return ScalpSignal(False, "", 0.0, "no-breakout")
