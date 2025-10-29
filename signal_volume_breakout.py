import statistics
from config import (KLINE_INTERVAL, KLINE_LIMIT, HH_N, OVEREXTEND_CAP,
                    VOL_BASE_WIN, VOL_SPIKE_K, VOL_LOOKBACK_CONFIRM,
                    EMA_FAST, EMA_SLOW)
from utils import fetch_klines, ema

def volume_breakout_ok(symbol: str) -> bool:
    # signal_volume_breakout.py
    try:
        closes, highs, lows, vols = fetch_klines(symbol, KLINE_INTERVAL, KLINE_LIMIT) # <--- 修改此行
        if len(closes) < max(HH_N, VOL_BASE_WIN) + VOL_LOOKBACK_CONFIRM + 2:
            return False

        curr_close = closes[-1]
        prev_high  = max(highs[-(HH_N+1):-1])  # 排除目前這根
        if curr_close <= prev_high:
            return False
        breakout_ratio = (curr_close - prev_high) / prev_high
        if breakout_ratio > OVEREXTEND_CAP:
            return False

        base_window = vols[-(VOL_BASE_WIN+VOL_LOOKBACK_CONFIRM):-VOL_LOOKBACK_CONFIRM]
        base_med = statistics.median(base_window)
        recent_sum = sum(vols[-VOL_LOOKBACK_CONFIRM:])
        need_sum = VOL_SPIKE_K * base_med * VOL_LOOKBACK_CONFIRM
        if recent_sum < need_sum:
            return False

        # 結構過濾：EMA20 > EMA50
        segment = closes[-(EMA_SLOW+10):]  # 足夠長度避免邊界
        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast <= e_slow:
            return False

        return True
    except Exception:
        return False
