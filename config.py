SYMBOL_BLACKLIST = [
    "ALPACAUSDT",
    "BNXUSDT",
    # 在這裡加入其他你想拉黑的幣種 (記得加引號和逗號)
]
# ================= 基本參數 =================
DAILY_TARGET_PCT = 0.30     # 當日達標 +1.5%
DAILY_LOSS_CAP   = -0.15     # 當日最大虧損 -2%
PER_TRADE_RISK   = 0.035    # 每筆風險 0.75% 權益
TP_PCT           = 0.015     # 單筆停利 +1.5%
SL_PCT           = 0.0075    # 單筆止損 -0.75%
MAX_TRADES_DAY   = 50         # 每日最多交易筆數
SCAN_INTERVAL_S  = 5        # Top10 刷新頻率（秒）
USE_LIVE         = True     # 先跑模擬；接實盤請改 True
ENABLE_LONG  = True      # <--- 新增：啟用做多
ENABLE_SHORT = True      # <--- 新增：啟用做空

# ================= 訊號參數（版本 C） =================
KLINE_INTERVAL   = "5m"
KLINE_LIMIT      = 120
HH_N             = 96        # (修改) 從 96 (8小時) 改成 48 (4小時)
LL_N             = 96        # (修改) 從 96 (8小時) 改成 48 (4小時)
OVEREXTEND_CAP   = 0.02
VOL_BASE_WIN     = 48
VOL_SPIKE_K      = 2       # (修改) 從 2.0 改成 1.5
# (總量能要求從 6 倍降低到 1.5 * 3 = 4.5 倍)
VOL_LOOKBACK_CONFIRM = 3
EMA_FAST         = 20
EMA_SLOW         = 50

# API 基礎
BINANCE_FUTURES_BASE = "https://fapi.binance.com"  # USDT 永續
# ===== 實盤連線與風控補充 =====
# 先用 Futures 測試網驗證，OK 再改成 False
USE_TESTNET = False

# 限價單等待成交的逾時秒數（逾時會自動撤單）
ORDER_TIMEOUT_SEC = 90

# WebSocket 開關：面板即時價
USE_WEBSOCKET = True
