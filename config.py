# ================= 黑名單幣種 =================
SYMBOL_BLACKLIST = [
    "ALPACAUSDT",
    "BNXUSDT",
    # 在這裡加入其他你想拉黑的幣種 (記得加引號和逗號)
]

# ================= 基本參數 =================
DAILY_TARGET_PCT = 0.20      # (調整) 當日達標 +5%
DAILY_LOSS_CAP   = -0.15     # (調整) 當日最大虧損 -5%
PER_TRADE_RISK   = 0.03      # (調整) 每筆風險 1% 權益
MAX_TRADES_DAY   = 50        # 每日最多交易筆數
SCAN_INTERVAL_S  = 1.0       # (調整) 提高掃描頻率，配合 scalp
USE_LIVE         = True      # 先跑模擬；接實盤請改 True
ENABLE_LONG  = True          # 啟用做多
ENABLE_SHORT = True          # 啟用做空

# ================= 訊號參數（版本 C） =================
KLINE_INTERVAL   = "5m"
KLINE_LIMIT      = 120
HH_N             = 60        # 從 96 改成 60 (約 5小時)
LL_N             = 60        # 從 96 改成 60 (約 5小時)
OVEREXTEND_CAP   = 0.012
VOL_BASE_WIN     = 48
VOL_SPIKE_K      = 1.5       # (調整) 量能尖峰閾值：2.0 -> 1.5
VOL_LOOKBACK_CONFIRM = 3
EMA_FAST         = 20
EMA_SLOW         = 50

# API 基礎
BINANCE_FUTURES_BASE = "https://fapi.binance.com"  # USDT 永續
USE_TESTNET = False
ORDER_TIMEOUT_SEC = 90
USE_WEBSOCKET = True

# === Breakout Pullback — 強化參數 ===
RETEST_BUFFER_PCT = 0.001
RETEST_EXPIRE_N    = 6
FATIGUE_LOOKBACK   = 4
FATIGUE_MIN_STREAK = 2
FATIGUE_TOTAL_MOVE = 0.015
VOL_COOLDOWN_ALPHA = 0.80
MTF_USE            = True
EMA_SLOPE_N        = 50
MTF_EMA_SLOPE_MIN  = 0.0
MTF_REQUIRE_VWAP   = True
VWAP_DIST_MAX      = 0.01    # (調整) 1%
BASE_MIN_BARS      = 12
BASE_MAX_ATR_Q     = 0.35
STOP_BUFFER_PCT    = 0.001
LIMIT_BUFFER_PCT   = 0.0007

# ===== SCALP PRESET =====
SCALP_MODE = "vwap"
USE_RETEST = False
SCALP_TIMEFRAME = "1m"
SCAN_INTERVAL_S = 1.0        # 與上面一致，確保高頻掃描
TP_PCT = 0.0055              # (調整) 止盈 0.55%
SL_PCT = 0.0040              # (調整) 止損 0.45%
TIME_STOP_BARS = 5           # (調整) 持倉最久 5 根K棒
COOLDOWN_S = 60
PER_TRADE_RISK = 0.03        # (調整) 單筆 1% 權益風險
DAILY_LOSS_CAP = -0.05       # (調整) 日停損 -5%
DAILY_TARGET_PCT = 0.05      # (調整) 日停利 +5%
VWAP_DIST_MAX = 0.01         # (調整) 價格與VWAP距離限制 1%
SPREAD_MAX_PCT = 0.0010      # (調整) 0.10%
OBI_THRESHOLD = 0.65
TRADE_IMB_LOOKBACK_S = 15
MAKER_ENTRY = True
TAKER_EXIT = True
SLIPPAGE_CAP_PCT = 0.0007
MAX_OPEN_POSITIONS = 1       # (保守) 同時僅持有 1 檔
LEVERAGE = 3                 # (保守) 槓桿上限 3 倍
