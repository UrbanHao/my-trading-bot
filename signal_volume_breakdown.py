# signal_volume_breakdown.py
import statistics
from config import (KLINE_INTERVAL, KLINE_LIMIT, LL_N, OVEREXTEND_CAP,
                    VOL_BASE_WIN, VOL_SPIKE_K, VOL_LOOKBACK_CONFIRM,
                    EMA_FAST, EMA_SLOW)
from utils import fetch_klines, ema

def volume_breakdown_ok(symbol: str) -> bool:
    try:
        # 1. 獲取 K 線 (注意：現在回傳 4 個值)
        closes, highs, lows, vols = fetch_klines(symbol, KLINE_INTERVAL, KLINE_LIMIT)
        if len(closes) < max(LL_N, VOL_BASE_WIN) + VOL_LOOKBACK_CONFIRM + 2:
            return False

        # 2. 價格：檢查「跌破前低」
        curr_close = closes[-1]
        prev_low  = min(lows[-(LL_N+1):-1])  # 排除目前這根
        if curr_close >= prev_low:
            return False
        
        # 3. 幅度：檢查「過度跌破」
        breakdown_ratio = (prev_low - curr_close) / prev_low
        if breakdown_ratio > OVEREXTEND_CAP:
            return False

        # 4. 量能：檢查（邏輯與做多相同，跌破也需要量）
        base_window = vols[-(VOL_BASE_WIN+VOL_LOOKBACK_CONFIRM):-VOL_LOOKBACK_CONFIRM]
        base_med = statistics.median(base_window)
        recent_sum = sum(vols[-VOL_LOOKBACK_CONFIRM:])
        need_sum = VOL_SPIKE_K * base_med * VOL_LOOKBACK_CONFIRM
        if recent_sum < need_sum:
            return False

        # 5. 結構：檢查「空頭排列」 (EMA_FAST < EMA_SLOW)
        segment = closes[-(EMA_SLOW+10):]
        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast >= e_slow: # <-- 修改
            return False

        return True
    except Exception:
        return False
