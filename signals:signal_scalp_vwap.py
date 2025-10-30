# signals/signal_scalp_vwap.py
from dataclasses import dataclass
import config
from utils import vwap_from_klines, zscore_price_vs_vwap, last_close

@dataclass
class ScalpSignal:
    ok: bool
    side: str      # "LONG" / "SHORT"
    entry: float
    reason: str

def scalp_vwap_signal(symbol, ws_client) -> ScalpSignal:
    k1m = ws_client.get_k1m(symbol, n=40)
    m = ws_client.get_micro(symbol)
    c = last_close(k1m)
    if not k1m or c is None or m["spread"] is None or m["trade_buy_ratio"] is None:
        return ScalpSignal(False, "", 0.0, "insufficient-data")

    if m["spread"] > config.SPREAD_MAX_PCT:
        return ScalpSignal(False, "", 0.0, "wide-spread")

    z = zscore_price_vs_vwap(k1m, lookback=20)
    vw = vwap_from_klines(k1m, lookback=20)
    if z is None or vw is None:
        return ScalpSignal(False, "", 0.0, "no-vwap")

    # 偏離→回歸：z 超閾值且出現反向主動量
    if z <= -1.0 and m["trade_buy_ratio"] >= 0.55:
        return ScalpSignal(True, "LONG", float(c), f"vwap-meanrev-long z={z:.2f}")
    if z >=  1.0 and m["trade_buy_ratio"] <= 0.45:
        return ScalpSignal(True, "SHORT", float(c), f"vwap-meanrev-short z={z:.2f}")

    return ScalpSignal(False, "", 0.0, "no-signal")
