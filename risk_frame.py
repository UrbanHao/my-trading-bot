from dataclasses import dataclass
from datetime import datetime
from config import DAILY_TARGET_PCT, DAILY_LOSS_CAP, PER_TRADE_RISK, SL_PCT, TP_PCT
import time
import config

@dataclass
class DayState:
    key: str
    pnl_pct: float = 0.0
    trades: int = 0
    halted: bool = False

class DayGuard:
    def __init__(self):
        self.state = DayState(key=datetime.now().date().isoformat())

    def rollover(self):
        k = datetime.now().date().isoformat()
        if k != self.state.key:
            self.state = DayState(key=k)

    def can_trade(self):
        return not self.state.halted

    def on_trade_close(self, pct):
        if self.state.halted: return
        self.state.trades += 1
        self.state.pnl_pct += pct
        if self.state.pnl_pct >= DAILY_TARGET_PCT or self.state.pnl_pct <= DAILY_LOSS_CAP:
            self.state.halted = True

def position_size_notional(equity: float) -> float:
    risk_amt = equity * PER_TRADE_RISK
    notional = risk_amt / SL_PCT
    return max(notional, 0.0)

def compute_bracket(entry: float, side: str):
    """
    回傳 (sl_price, tp_price)。只用 entry & 常數百分比，直觀好懂。
    """
    side_u = (side or "").upper()
    if side_u == "LONG":
        sl = entry * (1.0 - SL_PCT)
        tp = entry * (1.0 + TP_PCT)
    else:
        sl = entry * (1.0 + SL_PCT)
        tp = entry * (1.0 - TP_PCT)
    return sl, tp

class PositionClock:
    """追蹤持倉存活時間（以 bar 計）"""
    def __init__(self, timeframe="1m"):
        self.open_ts = None
        self.bars = 0
        self.timeframe = timeframe

    def on_new_bar(self):
        if self.open_ts is not None:
            self.bars += 1

    def on_open(self):
        self.open_ts = time.time()
        self.bars = 0

    def on_close(self):
        self.open_ts = None
        self.bars = 0

    def should_time_stop(self):
        return self.open_ts is not None and self.bars >= config.TIME_STOP_BARS

def compute_stop_limit(price,
                       side: str = None,
                       is_bull: bool = None,
                       stop_offset_pct: float = None,
                       limit_offset_pct: float = None,
                       **_):
    """
    Backward/forward 兼容版本：
    - 可用 side="LONG"/"SHORT" 或 is_bull=True/False 其中之一來表示方向
    - 接受 stop_offset_pct / limit_offset_pct；若未給則使用 config 的預設
    - **_ 吞掉額外的 keyword，避免舊/新介面不一致時拋錯
    回傳: (stop_px, limit_px) 皆為 float
    """
    # --- 參數清理 ---
    try:
        p = float(price)
    except Exception:
        raise ValueError(f"compute_stop_limit: invalid price {price!r}")

    if is_bull is None and side is None:
        raise TypeError("compute_stop_limit requires 'side' or 'is_bull'")

    if is_bull is None and side is not None:
        is_bull = str(side).upper() == "LONG"
    if side is None and is_bull is not None:
        side = "LONG" if is_bull else "SHORT"

    # --- 預設偏移（可在 config.py 覆寫） ---
    try:
        import config
        default_stop = getattr(config, "ENTRY_STOP_OFFSET_PCT", 0.0005)   # 5 bps
        default_limit = getattr(config, "ENTRY_LIMIT_OFFSET_PCT", 0.0010) # 10 bps
    except Exception:
        default_stop, default_limit = 0.0005, 0.0010

    stop_off  = float(stop_offset_pct)  if stop_offset_pct  is not None else default_stop
    limit_off = float(limit_offset_pct) if limit_offset_pct is not None else default_limit

    # --- 計算 ---
    if is_bull:
        stop_px  = p * (1.0 + stop_off)
        limit_px = p * (1.0 + limit_off)
    else:
        stop_px  = p * (1.0 - stop_off)
        limit_px = p * (1.0 - limit_off)

    return float(stop_px), float(limit_px)

# --- BEGIN: compat wrapper for compute_stop_limit (accepts side or is_bull) ---
def compute_stop_limit_compat(price,
                              side: str = None,
                              is_bull: bool = None,
                              stop_offset_pct: float = None,
                              limit_offset_pct: float = None,
                              **_):
    """
    兼容版本：
    - 可用 side="LONG"/"SHORT" 或 is_bull=True/False 表示方向（二者擇一）
    - 可傳 stop_offset_pct / limit_offset_pct；未提供則用 config 預設
    - **_ 會吞掉多餘的 keyword，避免舊/新呼叫不一致時拋錯
    回傳: (stop_px, limit_px)
    """
    try:
        p = float(price)
    except Exception:
        raise ValueError(f"compute_stop_limit: invalid price {price!r}")

    if is_bull is None and side is None:
        raise TypeError("compute_stop_limit requires 'side' or 'is_bull'")
    if is_bull is None and side is not None:
        is_bull = str(side).upper() == "LONG"
    if side is None and is_bull is not None:
        side = "LONG" if is_bull else "SHORT"

    # 預設偏移（如 config 無此鍵，給安全缺省）
    try:
        import config
        default_stop  = float(getattr(config, "ENTRY_STOP_OFFSET_PCT",  0.0005))  # 5 bps
        default_limit = float(getattr(config, "ENTRY_LIMIT_OFFSET_PCT", 0.0010))  # 10 bps
    except Exception:
        default_stop, default_limit = 0.0005, 0.0010

    stop_off  = float(stop_offset_pct)  if stop_offset_pct  is not None else default_stop
    limit_off = float(limit_offset_pct) if limit_offset_pct is not None else default_limit

    if is_bull:
        stop_px  = p * (1.0 + stop_off)
        limit_px = p * (1.0 + limit_off)
    else:
        stop_px  = p * (1.0 - stop_off)
        limit_px = p * (1.0 - limit_off)

    return float(stop_px), float(limit_px)

# 將公開名稱繫結到相容版本，確保所有 import 都用到這個簽名
compute_stop_limit = compute_stop_limit_compat
# --- END: compat wrapper ---
