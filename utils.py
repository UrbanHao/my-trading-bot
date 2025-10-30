from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time,math
import statistics
import requests
from datetime import datetime, timezone
from config import BINANCE_FUTURES_BASE, SYMBOL_BLACKLIST

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "daily-gainer-bot/vC"})

SESSION.headers.update({"Cache-Control": "no-cache"})
EXCLUDE_KEYWORDS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSD")

def now_ts_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# utils.py（任一合適位置）
def ws_best_price(symbol: str):
    try:
        from ws_client import ws_best_price as _ws
        return _ws(symbol)
    except Exception:
        return None
# --- 安全版：Top Losers（跌幅榜），和 fetch_top_gainers 結構一致 ---
def fetch_top_gainers(limit=10):
    """
    獲取漲幅榜 Top 10
    """
    rows = _rest_json("/fapi/v1/ticker/24hr", timeout=6, tries=3)
    items = []
    for x in rows:
        s = x.get("symbol", "")
        if (not s.endswith("USDT")) or any(k in s for k in EXCLUDE_KEYWORDS):
            continue
        if s in SYMBOL_BLACKLIST:
            continue
        try:
            pct = float(x.get("priceChangePercent", 0.0))
            last = float(x.get("lastPrice", 0.0))
            vol  = float(x.get("volume", 0.0))
        except:
            continue
        if last <= 0 or vol <= 0 or pct <= 0: # <-- 關鍵差異：只看 pct > 0
            continue
        items.append((s, pct, last, vol))
    items.sort(key=lambda t: t[1], reverse=True) # <-- 關鍵差異：True (由大到小)
    return items[:limit]

def fetch_top_losers(limit=10):
    """
    獲取跌幅榜 Top 10
    """
    rows = _rest_json("/fapi/v1/ticker/24hr", timeout=6, tries=3)
    items = []
    for x in rows:
        s = x.get("symbol", "")
        if (not s.endswith("USDT")) or any(k in s for k in EXCLUDE_KEYWORDS):
            continue
        if s in SYMBOL_BLACKLIST:
            continue
        try:
            pct = float(x.get("priceChangePercent", 0.0))
            last = float(x.get("lastPrice", 0.0))
            vol  = float(x.get("volume", 0.0))
        except:
            continue
        if last <= 0 or vol <= 0 or pct >= 0: # <-- 關鍵差異：只看 pct < 0
            continue
        items.append((s, pct, last, vol))
    items.sort(key=lambda t: t[1], reverse=False) # <-- 關鍵差異：False (由小到大)
    return items[:limit]

def fetch_klines(symbol, interval, limit):
    rows = _rest_json("/fapi/v1/klines", params={"symbol":symbol, "interval":interval, "limit":limit}, timeout=6, tries=3)
    closes = [float(k[4]) for k in rows]
    highs  = [float(k[2]) for k in rows]
    lows   = [float(k[3]) for k in rows]
    vols   = [float(k[5]) for k in rows]
    return closes, highs, lows, vols

def ema(vals, n):
    if len(vals) < n: return None
    k = 2.0/(n+1.0)
    e = vals[0]
    for v in vals[1:]:
        e = v*k + e*(1-k)
    return e


# 安裝全域重試（429/5xx，帶退避）
_retry = Retry(total=3, backoff_factor=0.4, status_forcelist=[429,500,502,503,504], allowed_methods=["GET","POST","DELETE"])
SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
SESSION.mount("http://",  HTTPAdapter(max_retries=_retry))


def safe_get_json(url: str, params=None, timeout=4, tries=2):
    """帶重試/timeout 的 GET，失敗拋例外讓上層 decide。"""
    params = params or {}
    last = None
    for i in range(max(1, tries)):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(0.3 * (i + 1))
    raise last

# --- Resilient REST hosts (main & testnet) ---
# 刪除 FUTURES_HOSTS_MAIN 和 FUTURES_HOSTS_TEST 列表
# 我們將依賴 config.py 中的 BINANCE_FUTURES_BASE (已在第 9 行匯入)
from config import USE_TESTNET # 匯入 USE_TESTNET

# 由於 config.py 中沒有定義測試網，我們在這裡定義
BINANCE_FUTURES_TEST_BASE = "https://testnet.binancefuture.com"

def _rest_json(path: str, params=None, timeout=5, tries=3):
    """對 Binance Futures REST 做單一主機 + 退避重試。
    (修改：強制使用 config.py 中的 BINANCE_FUTURES_BASE)
    """
    
    # 依據 USE_TESTNET 選擇正確的 base URL
    base = BINANCE_FUTURES_TEST_BASE if USE_TESTNET else BINANCE_FUTURES_BASE

    params = params or {}
    last_err = None
    # 移除多主機輪詢，只保留重試
    for t in range(max(1, tries)):
        try:
            r = SESSION.get(f"{base}{path}", params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
        # 退避 + 一點 jitter
        time.sleep(0.3 * (t + 1))
    # 把最後一個錯誤丟回去，外層會捕捉並 log，不讓主迴圈掛掉
    raise last_err if last_err else RuntimeError("REST all hosts failed")


# --- Binance Futures server time offset (ms) ---
def _fapi_server_time_ms():
    try:
        import requests
        r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/time", timeout=5)
        r.raise_for_status()
        return int(r.json().get("serverTime", now_ts_ms()))
    except Exception:
        return now_ts_ms()

TIME_OFFSET_MS = 0 # 先宣告為全域變數

def update_time_offset():
    """重新計算並更新全域的 TIME_OFFSET_MS"""
    global TIME_OFFSET_MS
    try:
        _local = now_ts_ms()
        _server = _fapi_server_time_ms()
        TIME_OFFSET_MS = _server - _local
        return TIME_OFFSET_MS
    except Exception:
        return TIME_OFFSET_MS # 同步失敗時，維持舊的 offset

# 啟動時執行第一次校正
update_time_offset()

EXCHANGE_INFO = {}
def _get_decimals_from_string(s: str) -> int:
    """從 tickSize/stepSize 字串計算所需的小數位數"""
    if 'e-' in s: # 處理科學記號, e.g., "1e-5"
        try:
            return int(s.split('e-')[-1])
        except Exception:
            return 8 # Fallback
    
    s = s.rstrip('0') # 移除尾隨的 0, e.g., "0.0100" -> "0.01"
    
    if '.' not in s:
        return 0 # e.g., "1"
    
    return len(s.split('.')[-1])
# utils.py (修改後的版本)
# utils.py
def load_exchange_info(force_refresh=False):
    """
    抓回 Futures 的精度 + Filters（tickSize/stepSize/minNotional 等），快取於 EXCHANGE_INFO。
    """
    global EXCHANGE_INFO
    if EXCHANGE_INFO and not force_refresh:
        return
    try:
        info = _rest_json("/fapi/v1/exchangeInfo")
        data = {}
        # utils.py (修改後的版本)
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            
            filt = {f.get("filterType"): f for f in s.get("filters", [])}
            pf = filt.get("PRICE_FILTER", {})
            ls = filt.get("LOT_SIZE", {})
            mn = filt.get("MIN_NOTIONAL", {})
            
            # --- ↓↓↓ 這是關鍵修復 ↓↓↓ ---
            tick_str = pf.get('tickSize', '0.0001') or '0.0001'
            step_str = ls.get('stepSize', '1') or '1'
            
            data[s["symbol"]] = {
                "pricePrecision":    _get_decimals_from_string(tick_str), # 從 tickSize 推導
                "quantityPrecision": _get_decimals_from_string(step_str), # 從 stepSize 推導
            # --- ↑↑↑ 結束修復 ↑↑↑ ---
                
                "tickSize":    float(tick_str),
                "minPrice":    float(pf.get("minPrice", "0") or 0),
                "maxPrice":    float(pf.get("maxPrice", "0") or 0),
                "stepSize":    float(step_str),
                "minQty":      float(ls.get("minQty", "0") or 0),
                "maxQty":      float(ls.get("maxQty", "0") or 0),
                "minNotional": float(mn.get("notional", "5") or 5),
            }
        EXCHANGE_INFO = data
        print(f"--- 成功載入 {len(EXCHANGE_INFO)} 個幣種的精度與過濾規則 ---")
    except Exception as e:
        print(f"--- 致命錯誤：無法載入 Exchange Info: {e} ---")
        print("--- 程式可能因無法獲取精度而下單失敗 ---")
        
def conform_to_filters(symbol: str, price: float, qty: float):
    """
    依 tickSize / stepSize / minQty / minNotional 將 price/qty 修到交易所會接受的格子。
    回傳 (price, qty, pricePrecision, quantityPrecision)
    """
    info = EXCHANGE_INFO.get(symbol)
    if not info:
        load_exchange_info(force_refresh=True) # 嘗試刷新
        info = EXCHANGE_INFO.get(symbol)
        if not info:
            # 刷新後仍找不到，拋出錯誤讓 main.py 捕捉
            raise ValueError(f"Symbol {symbol} not found in EXCHANGE_INFO after refresh")

    tick = float(info["tickSize"])
    step = float(info["stepSize"])
    min_price = float(info["minPrice"])
    min_qty   = float(info["minQty"])
    min_not   = float(info["minNotional"])

    # 價格對齊 tick (向下取)
    if tick > 0:
        price = math.floor(price / tick) * tick
    if min_price > 0 and price < min_price:
        price = min_price # (雖然不太可能，但做個保護)

    # 數量對齊 step (向下取)
    if step > 0:
        qty = math.floor(qty / step) * step
    if min_qty > 0 and qty < min_qty:
        qty = min_qty

    # 名目最小值：price*qty 不足就把 qty 依 step 補到位 (向上取)
    if (price * qty) < min_not:
        need = min_not / max(price, 1e-12)
        if step > 0:
            qty = math.ceil(need / step) * step
        else:
            qty = max(qty, need)

    return price, qty, int(info["pricePrecision"]), int(info["quantityPrecision"])

# ===== Helpers for Breakout Pullback 強化 =====
import math
import statistics
from typing import Sequence, Tuple

def vwap(prices: Sequence[float], highs: Sequence[float], lows: Sequence[float], vols: Sequence[float]) -> float:
    """簡易 session VWAP（以 close 近似典型價）"""
    if not prices or not vols or sum(vols) == 0:
        return prices[-1] if prices else float('nan')
    pv = sum(p * v for p, v in zip(prices, vols))
    vv = sum(vols)
    return pv / vv

def ema_slope(values: Sequence[float], n: int) -> float:
    """回傳 EMA(n) 的近似斜率（最後兩點差）"""
    if len(values) < n + 2:
        return 0.0
    k = 2 / (n + 1)
    ema_vals = []
    ema_now = values[-(n+1)]
    for x in values[-n:]:
        ema_now = k * x + (1 - k) * ema_now
        ema_vals.append(ema_now)
    if len(ema_vals) < 2:
        return 0.0
    return ema_vals[-1] - ema_vals[-2]

def pct_dist(a: float, b: float) -> float:
    """|a-b| / a"""
    if a == 0:
        return 0.0
    return abs(a - b) / a

def atr(series_high: Sequence[float], series_low: Sequence[float], series_close: Sequence[float], n: int = 14) -> float:
    """簡易 ATR"""
    trs = []
    for i in range(1, len(series_close)):
        tr = max(series_high[i]-series_low[i], abs(series_high[i]-series_close[i-1]), abs(series_low[i]-series_close[i-1]))
        trs.append(tr)
    if len(trs) < n:
        return statistics.mean(trs) if trs else 0.0
    return statistics.mean(trs[-n:])

def box_base_ok(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
                min_bars: int, atr_q: float) -> bool:
    """檢查箱體基底是否足夠長且窄幅（以 ATR 的分位做門檻）"""
    if len(closes) < min_bars + 2:
        return False
    sub_h = highs[-(min_bars+1):-1]
    sub_l = lows [-(min_bars+1):-1]
    sub_c = closes[-(min_bars+1):-1]
    width = max(sub_h) - min(sub_l)
    base_atr = atr(highs, lows, closes, n=14)
    if base_atr == 0:
        return False
    # 以 ATR 比例視為窄幅
    return (width / base_atr) <= (atr_q / 0.5)  # 粗略映射，足夠實用

def fatigue_exhausted(closes: Sequence[float], m: int, min_streak: int, total_move: float, bullish: bool) -> bool:
    """檢測最近 m 根是否有「連續上/下漲且總變動>門檻」"""
    if len(closes) < m + 1:
        return False
    seg = closes[-(m+1):]
    diffs = [seg[i] - seg[i-1] for i in range(1, len(seg))]
    streak = 0
    total = 0.0
    for d in diffs:
        if (d > 0 and bullish) or (d < 0 and not bullish):
            streak += 1
            total += abs(d) / seg[-2]  # 近似百分比
        else:
            streak = 0
            total = 0.0
        if streak >= min_streak and total >= total_move:
            return True
    return False

def compute_stop_limit(entry_ref: float, bullish: bool, stop_buf: float, limit_buf: float) -> Tuple[float, float]:
    """計算 stop 與 limit 價位（實盤/模擬共用）"""
    if bullish:
        stop  = entry_ref * (1 + stop_buf)
        limit = stop      * (1 + limit_buf)
    else:
        stop  = entry_ref * (1 - stop_buf)
        limit = stop      * (1 - limit_buf)
    return (stop, limit)
    
