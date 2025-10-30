SYMBOL_BLACKLIST = [
    "ALPACAUSDT",
    "BNXUSDT",
    # 在這裡加入其他你想拉黑的幣種 (記得加引號和逗號)
]
# ================= 基本參數 =================
DAILY_TARGET_PCT = 0.30     # 當日達標 +1.5%
DAILY_LOSS_CAP   = -0.15     # 當日最大虧損 -2%
PER_TRADE_RISK   = 0.030    # 每筆風險 0.75% 權益
MAX_TRADES_DAY   = 50         # 每日最多交易筆數
SCAN_INTERVAL_S  = 5        # Top10 刷新頻率（秒）
USE_LIVE         = True     # 先跑模擬；接實盤請改 True
ENABLE_LONG  = True      # <--- 新增：啟用做多
ENABLE_SHORT = True      # <--- 新增：啟用做空

# ================= 訊號參數（版本 C） =================
KLINE_INTERVAL   = "5m"
KLINE_LIMIT      = 120
HH_N             = 60        # (修改) 從 96 (8小時) 改成 48 (4小時)
LL_N             = 60        # (修改) 從 96 (8小時) 改成 48 (4小時)
OVEREXTEND_CAP   = 0.012
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

# === Breakout Pullback — 強化參數（新增） ===
# 1) 回測入場
RETEST_BUFFER_PCT = 0.001   # 0.10% 緩衝
RETEST_EXPIRE_N    = 6      # 觸發後 N 根內須回測成功

# 2) 疲勞濾網（連續上漲/下跌）
FATIGUE_LOOKBACK   = 4      # 最近 M 根（3~5 之間合適）
FATIGUE_MIN_STREAK = 2      # 連漲/連跌 最少根數
FATIGUE_TOTAL_MOVE = 0.015  # 總漲跌幅門檻（1.5%）

# 3) 量能「降溫」條件
VOL_COOLDOWN_ALPHA = 0.80   # 下一根/當前量 <= 峰值的 80% 視為健康

# 4) 多週期對齊（15m/1h）
MTF_USE            = True
EMA_SLOPE_N        = 50     # 15m EMA(50) 斜率
MTF_EMA_SLOPE_MIN  = 0.0    # 斜率需 > 0 才多；< 0 才空
MTF_REQUIRE_VWAP   = True   # 收在 15m/1h VWAP 上(多) / 下(空)

# 5) VWAP 距離限制
VWAP_DIST_MAX      = 0.008  # 0.8%

# 6) 箱體基底
BASE_MIN_BARS      = 12     # 窄幅箱體至少 10~20 根
BASE_MAX_ATR_Q     = 0.35   # 箱體寬度 ≤ ATR 的 35% 分位（可依幣種微調）

# 7) Stop-Limit 下單 buffer（實盤用；模擬同邏輯估成交）
STOP_BUFFER_PCT    = 0.001 # 0.05%
LIMIT_BUFFER_PCT   = 0.0007 # 0.07%

# ===== SCALP PRESET (新增) =====
SCALP_MODE = None            # None / "breakout" / "vwap"
USE_RETEST = False           # Scalp 模式下關閉回測入場（原策略可設 True）

# 時間框架與掃描
SCALP_TIMEFRAME = "1m"
SCAN_INTERVAL_S = 1.0        # 提高掃描頻率（原值保留，此值會在 Scalp 路由使用）

# 風控與出場（短線）
TP_PCT = 0.0024              # 20 bps
SL_PCT = 0.0010              # 12 bps
TIME_STOP_BARS = 3           # 3 根 1m 未觸發即離場
COOLDOWN_S = 60              # 同標的冷卻秒數
PER_TRADE_RISK = 0.0025      # 單筆 0.25% 權益風險
DAILY_LOSS_CAP = -0.015      # 日停損 -1.5%
DAILY_TARGET_PCT = 0.010     # 日停利 +1%

# 輕量市場結構濾網
VWAP_DIST_MAX = 0.004        # |price-vwap|/vwap ≤ 0.40%
SPREAD_MAX_PCT = 0.0005      # 5 bps
OBI_THRESHOLD = 0.60         # 頂層委買/賣佔比
TRADE_IMB_LOOKBACK_S = 15    # 主動成交量觀察窗（秒）

# 執行策略
MAKER_ENTRY = True           # 先以 maker 進場
TAKER_EXIT = True            # 允許以市價快速離場
SLIPPAGE_CAP_PCT = 0.0007    # 市價/可成交限價最大滑點 7 bps
MAX_OPEN_POSITIONS = 1       # 建議同時僅持 1 檔（降干擾）
