from dataclasses import dataclass
from datetime import datetime
from config import DAILY_TARGET_PCT, DAILY_LOSS_CAP, PER_TRADE_RISK, SL_PCT, TP_PCT

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
    if side == "LONG":
        return entry*(1-SL_PCT), entry*(1+TP_PCT)
    else:
        return entry*(1+SL_PCT), entry*(1-TP_PCT)