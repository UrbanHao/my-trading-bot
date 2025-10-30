# signal_volume_breakdown.py
import statistics
# ===== module-level state（新增） =====
STATE = {}  # key: symbol -> {'armed': bool, 'level': float, 'armed_bar': int}

# 小工具（就地實作，避免外部依賴）
def _pct_dist(a: float, b: float) -> float:
    return 0.0 if a == 0 else abs(a - b) / a

def _vwap(closes, highs, lows, vols):
    v = sum(vols)
    return (sum(c * v_ for c, v_ in zip(closes, vols)) / v) if v > 0 else closes[-1]

def _ema_slope(vals, n: int) -> float:
    if len(vals) < n + 2:
        return 0.0
    k = 2 / (n + 1)
    ema_now = vals[-(n+1)]
    track = []
    for x in vals[-n:]:
        ema_now = k * x + (1 - k) * ema_now
        track.append(ema_now)
    return (track[-1] - track[-2]) if len(track) >= 2 else 0.0

def _atr_like(highs, lows, closes, n: int = 14) -> float:
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    if not trs:
        return 0.0
    if len(trs) < n:
        return sum(trs)/len(trs)
    return sum(trs[-n:])/n

def _box_base_ok(highs, lows, closes, min_bars: int, atr_q: float) -> bool:
    if len(closes) < min_bars + 2:
        return False
    sub_h = highs[-(min_bars+1):-1]
    sub_l = lows [-(min_bars+1):-1]
    width = (max(sub_h) - min(sub_l))
    base_atr = _atr_like(highs, lows, closes, 14)
    if base_atr == 0:
        return False
    return (width / base_atr) <= (atr_q / 0.5)

def _fatigue_exhausted(closes, m: int, min_streak: int, total_move: float, bullish: bool) -> bool:
    if len(closes) < m + 1:
        return False
    seg = closes[-(m+1):]
    streak = 0
    acc = 0.0
    for i in range(1, len(seg)):
        d = seg[i] - seg[i-1]
        if (d > 0 and bullish) or (d < 0 and not bullish):
            streak += 1
            acc += abs(d) / max(seg[i-1], 1e-9)
        else:
            streak = 0
            acc = 0.0
        if streak >= min_streak and acc >= total_move:
            return True
    return False

# 這些參數你可能已在 config 中；若沒有，就用預設值以免報錯
try:
    RETEST_BUFFER_PCT
except NameError:
    RETEST_BUFFER_PCT = 0.001
try:
    RETEST_EXPIRE_N
except NameError:
    RETEST_EXPIRE_N = 6
try:
    VOL_COOLDOWN_ALPHA
except NameError:
    VOL_COOLDOWN_ALPHA = 0.80
try:
    VWAP_DIST_MAX
except NameError:
    VWAP_DIST_MAX = 0.008
try:
    BASE_MIN_BARS
except NameError:
    BASE_MIN_BARS = 12
try:
    BASE_MAX_ATR_Q
except NameError:
    BASE_MAX_ATR_Q = 0.35
try:
    EMA_SLOPE_N
except NameError:
    EMA_SLOPE_N = 50
    
from config import (KLINE_INTERVAL, KLINE_LIMIT, LL_N, OVEREXTEND_CAP,
                    VOL_BASE_WIN, VOL_SPIKE_K, VOL_LOOKBACK_CONFIRM,
                    EMA_FAST, EMA_SLOW)
from utils import fetch_klines, ema

def volume_breakdown_ok(symbol: str) -> bool:
    # signal_volume_breakdown.py
    try:
        # 1. 取 K 線
        closes, highs, lows, vols = fetch_klines(symbol, KLINE_INTERVAL, KLINE_LIMIT)
        if len(closes) < max(LL_N, VOL_BASE_WIN) + VOL_LOOKBACK_CONFIRM + 2:
            return False

        price = closes[-1]
        # 2. 前低（排除當根）
        prev_low  = min(lows[-(LL_N+1):-1])

        # 3. 跌破幅度（相對 prev_low 的「超伸」限制以 VWAP 距離落地）
        session_vwap = _vwap(closes, highs, lows, vols)
        vwap_dist_ok = _pct_dist(price, session_vwap) <= VWAP_DIST_MAX
        overextend_ok = _pct_dist(price, session_vwap) <= OVEREXTEND_CAP

        # 4. 量能：尖峰 + 降溫
        base_window = vols[-(VOL_BASE_WIN+VOL_LOOKBACK_CONFIRM):-VOL_LOOKBACK_CONFIRM]
        base_med = statistics.median(base_window)
        recent_sum = sum(vols[-VOL_LOOKBACK_CONFIRM:])
        need_sum = VOL_SPIKE_K * base_med * VOL_LOOKBACK_CONFIRM
        vol_spike = recent_sum >= need_sum

        vol_cool_ok = True
        if len(vols) >= 2:
            peak = max(vols[-2], vols[-1])
            vol_cool_ok = (vols[-1] <= VOL_COOLDOWN_ALPHA * peak) or (vols[-2] <= VOL_COOLDOWN_ALPHA * peak)

        # 5. 結構：空頭排列 + EMA 斜率向下（近似多週期偏空）
        segment = closes[-(EMA_SLOW+10):]
        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None:
            return False
        ema_slope_ok = _ema_slope(closes, EMA_SLOPE_N) < 0.0

        # 6. 箱體基底 & 疲勞（避免追跌尾）
        base_ok = _box_base_ok(highs, lows, closes, BASE_MIN_BARS, BASE_MAX_ATR_Q)
        fatigue_short = _fatigue_exhausted(closes, m=4, min_streak=2, total_move=0.015, bullish=False)

        # 是否破底
        short_break = (price < prev_low)

        # 1) 未 armed → 符合條件則 armed（不立刻進）
        st = STATE.setdefault(symbol, {'armed': False, 'level': None, 'armed_bar': None})
        if (not st['armed']):
            if short_break and vol_spike and vol_cool_ok and vwap_dist_ok and overextend_ok and (e_fast < e_slow) and (not fatigue_short) and ema_slope_ok and base_ok:
                st['armed'] = True
                st['level'] = prev_low   # 回測位=前低
                st['armed_bar'] = 0
                return False  # 本次不進，等待回測

        # 2) 已 armed → 等回測到「前低 ± buffer」才進空
        if st['armed'] and st['level'] is not None:
            st['armed_bar'] += 1
            expired = (st['armed_bar'] > RETEST_EXPIRE_N)
            retest_ok = (abs(price - st['level']) / max(price, 1e-9)) <= RETEST_BUFFER_PCT
            if retest_ok and not expired:
                st['armed'] = False
                st['level'] = None
                st['armed_bar'] = None
                return True
            if expired:
                st['armed'] = False
                st['level'] = None
                st['armed_bar'] = None
                return False

        return False
    except Exception:
        return False
