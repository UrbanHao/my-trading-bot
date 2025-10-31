import os, hmac, hashlib, time, logging
from decimal import Decimal, ROUND_DOWN
import requests

from utils import (
    now_ts_ms, SESSION, BINANCE_FUTURES_BASE, TIME_OFFSET_MS,
    EXCHANGE_INFO, load_exchange_info, log
)
import config
from config import USE_TESTNET, ORDER_TIMEOUT_SEC, STOP_BUFFER_PCT, LIMIT_BUFFER_PCT
import logging
from journal import log_trade

try:
    from ws_client import ws_best_price as _ws_best_price
except Exception:
    _ws_best_price = None

from risk_frame import compute_stop_limit as rf_compute_stop_limit, compute_bracket
compute_stop_limit = rf_compute_stop_limit

logger = logging.getLogger(__name__)  # ← 用 logger，不要叫 log()

# ---------- 最終對齊工具：conform_to_filters 後再依 tick/step/precision 下切 ----------
def _final_align(symbol: str, price: float, qty: float):
    symbol = symbol.upper()
    info = EXCHANGE_INFO.get(symbol)
    if not info:
        load_exchange_info()
        info = EXCHANGE_INFO.get(symbol)
    if not info:
        return conform_to_filters(symbol, price, qty)

    price_prec = int(info.get("pricePrecision", 8))
    qty_prec   = int(info.get("quantityPrecision", 0))
    tick = step = None
    for f in info.get("filters", []):
        if f.get("filterType") in ("PRICE_FILTER", "PRICE_FILTER_V2"):
            tick = float(f.get("tickSize", 0) or 0)
        if f.get("filterType") in ("LOT_SIZE", "MARKET_LOT_SIZE"):
            step = float(f.get("stepSize", 0) or 0)

    p1, q1, _, _ = conform_to_filters(symbol, price, qty)

    def _floor(val: float, step_sz: float, prec: int) -> float:
        if step_sz and step_sz > 0:
            q = Decimal(str(step_sz))
            v = (Decimal(str(val)) / q).to_integral_value(rounding=ROUND_DOWN) * q
        else:
            v = Decimal(str(val))
        return float(f"{v:.{prec}f}")

    p2 = _floor(p1, tick or 0.0, price_prec)
    q2 = _floor(q1, step or 0.0, qty_prec)
    return p2, q2, price_prec, qty_prec

# ================================== 模擬 Adapter ==================================
class SimAdapter:
    def __init__(self):
        self.open = None

    def has_open(self): return self.open is not None
    def get_mark_price(self, symbol: str) -> float:
            """獲取當前標記價格 (Mark Price) 用於風控輪詢"""
            try:
                # 使用 premiumIndex 端點，它比 positionRisk 輕量
                r = SESSION.get(f"{self.base}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=3)
                r.raise_for_status()
                return float(r.json()["markPrice"])
            except Exception as e:
                # 如果標記價格失敗，才退回使用最新成交價
                logger.warning(f"Failed to get markPrice for {symbol}: {e}, falling back to best_price (lastPrice)")
                return self.best_price(symbol)
    def best_price(self, symbol: str) -> float:
        if _ws_best_price:
            try:
                p = _ws_best_price(symbol)
                if p is not None:
                    return float(p)
            except Exception:
                pass
        r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])

    def place_bracket(self, symbol: str, side: str, qty_s: str, entry_s: str, sl_s: str, tp_s: str):
        """
        簽名不變（qty_s, entry_s, sl_s, tp_s）— 模擬只做計算與狀態記錄。
        """
        symbol = symbol.upper()
        if symbol not in EXCHANGE_INFO:
            raise ValueError(f"symbol not tradable or not found in exchangeInfo: {symbol}")

        side_u = side.upper()
        qty_f   = float(qty_s)
        entry_f = float(entry_s)
        sl_f    = float(sl_s)
        tp_f    = float(tp_s)

        try:
            stop_px_raw, _ = compute_stop_limit(entry_f, side=side_u)
        except Exception:
            stop_px_raw = entry_f

        entry_f, qty_f, price_prec, qty_prec = _final_align(symbol, entry_f, qty_f)
        stop_f,  _,     _,          _        = _final_align(symbol, stop_px_raw, qty_f)
        sl_f,    _,     _,          _        = _final_align(symbol, sl_f,        qty_f)
        tp_f,    _,     _,          _        = _final_align(symbol, tp_f,        qty_f)

        self.open = {
            "symbol": symbol, "side": side_u, "qty": qty_f,
            "entry": entry_f, "sl": sl_f, "tp": tp_f, "entry_stop": stop_f
        }
        log.info(f"[SIM OPEN] {side_u} {symbol} entry={entry_f} stop={stop_f} sl={sl_f} tp={tp_f} qty={qty_f}")
        return "SIM_ORDER"

# --- 偵測命中 TP/SL 後，強制收倉、清殘單、記帳 ---
    def poll_and_close_if_hit(self, day_guard):
        if not self.open:
            return False, None, None, None, None

        symbol = self.open["symbol"]
        side   = self.open["side"]
        entry  = float(self.open["entry"])
        
        # ⬇️ *** 修正點 1：改用 Mark Price 輪詢 ***
        p = self.get_mark_price(symbol) # 原本是 self.best_price(symbol)
        
        hit_tp = (p >= self.open["tp"]) if side == "LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side == "LONG" else (p >= self.open["sl"])

        if not (hit_tp or hit_sl):
            return False, None, None, None, None

        # --- 價格已觸發 ---
        exit_price = self.open["tp"] if hit_tp else self.open["sl"]
        pct = (exit_price - entry) / entry
        if side == "SHORT":
            pct = -pct
        reason = "TP" if hit_tp else "SL"

        # ⬇️ *** 修正點 2：確保市價平倉成功才清狀態 ***
        try:
            # 保險：現市價強制平倉
            logger.info(f"[CLOSE] {symbol} {reason} hit by markPrice={p}. Attempting force market close...")
            self._post("/fapi/v1/order", {
                "symbol": symbol,
                "side": ("SELL" if side == "LONG" else "BUY"),
                "type": "MARKET",
                "quantity": f"{float(self.open['qty']):.6f}",
                "reduceOnly": "true", # 盡可能用 reduceOnly 避免反向開倉
                "newClientOrderId": f"force_close_{now_ts_ms()}",
            })
            logger.info(f"[CLOSE] Force market close for {symbol} SUCCEEDED.")

        except Exception as e:
            logger.error(f"[CLOSE] FORCE MARKET CLOSE FAILED for {symbol}: {e}")
            # ❗ 平倉失敗！絕對不能清空 self.open
            # 返回 False，讓主迴圈下一輪 (0.8s 後) 繼續嘗試
            # 這時幣安的真實 STOP_MARKET/TAKE_PROFIT_MARKET 掛單可能也會觸發
            return False, None, None, None, None

        # --- 平倉成功後，才執行以下清理 ---
        
        # 清殘單 (TP/SL 掛單)
        try:
            self.cancel_open_orders(symbol)
        except Exception:
            pass

        # 記帳、清狀態
        trade_data = self.open
        self.open = None # <-- 現在清空是安全的
        day_guard.on_trade_close(pct)
        try:
            log_trade(
                symbol=symbol,
                side=trade_data["side"],
                qty=trade_data["qty"],
                entry=trade_data["entry"],
                exit_price=exit_price,
                ret_pct=pct,
                reason=reason
            )
        except Exception:
            pass

        logger.info(f"[CLOSE] {symbol} {reason} PnL={pct*100:.2f}% (State cleared)")
        return True, pct, symbol, reason, exit_price

# ================================== 實單 Adapter ==================================
def _fmt(v: float, prec: int) -> str:
    return f"{v:.{prec}f}"

def _floor_to_step(val: float, step: float, prec: int) -> float:
    if step and step > 0:
        q = Decimal(str(step))
        v = (Decimal(str(val)) / q).to_integral_value(rounding=ROUND_DOWN) * q
    else:
        v = Decimal(str(val))
    return float(f"{v:.{prec}f}")

class LiveAdapter:
    """
    單一入場流程：
      1) 先取消該 symbol 所有未成交掛單（避免越掛越多）
      2) 市價進場 (MARKET)
      3) 等倉位建立（/fapi/v2/positionRisk 確認）
      4) 補掛 reduce-only 的 SL/TP（*_MARKET + closePosition=true）
    """
    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        BINANCE_FUTURES_TEST_BASE = "https://testnet.binancefuture.com"
        self.base = (USE_TESTNET and BINANCE_FUTURES_TEST_BASE) or BINANCE_FUTURES_BASE
        self.open = None
        self._placing = False  # 簡單鎖，避免同時打單

    # --- 基礎 HTTP 簽名/請求 ---
    def _sign(self, params: dict):
        from urllib.parse import urlencode
        q = urlencode(sorted(params.items()), doseq=True)
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return q + "&signature=" + sig

    def _post(self, path, params):
        params = dict(params)
        params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
        params.setdefault("recvWindow", 60000)
        qs = self._sign(params)
        r = SESSION.post(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()

    def _get(self, path, params):
        params = dict(params or {})
        params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
        params.setdefault("recvWindow", 60000)
        qs = self._sign(params)
        r = SESSION.get(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()

    def _delete(self, path, params):
        params = dict(params or {})
        params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
        params.setdefault("recvWindow", 60000)
        qs = self._sign(params)
        r = SESSION.delete(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()
    def _get_order(self, symbol: str, order_id: str):
        """[新增] 查詢特定訂單狀態"""
        try:
            return self._get("/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        except Exception as e:
            logger.error(f"Failed to get order {symbol} {order_id}: {e}")
            return None # 查詢失敗

    def _cancel_order(self, symbol: str, order_id: str):
        """[新增] 取消特定訂單"""
        try:
            return self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        except Exception as e:
            # -2011 (Unknown order) 是可接受的，代表可能已成交
            if "code=-2011" not in str(e):
                logger.error(f"Failed to cancel order {symbol} {order_id}: {e}")
            return False
        return True

    def _get_avg_filled_price(self, symbol: str, order_id: str) -> float | None:
        """[新增] 透過 userTrades 查詢訂單的平均成交價"""
        try:
            trades = self._get("/fapi/v1/userTrades", {"symbol": symbol, "limit": 20})
            for t in trades:
                if str(t.get("orderId")) == str(order_id):
                    return float(t.get("price")) # 找到第一筆就回傳
        except Exception as e:
            logger.error(f"Failed to get userTrades for {symbol}: {e}")
        return None

    # --- 資訊 ---
    def balance_usdt(self) -> float:
        arr = self._get("/fapi/v2/balance", {})
        for a in arr:
            if a.get("asset") == "USDT":
                v = a.get("availableBalance") or a.get("balance") or "0"
                try:
                    return float(v)
                except Exception:
                    return 0.0
        return 0.0

    def has_open(self):
        return self.open is not None

    def best_price(self, symbol: str) -> float:
        if _ws_best_price:
            try:
                p = _ws_best_price(symbol)
                if p is not None:
                    return float(p)
            except Exception:
                pass
        r = SESSION.get(f"{self.base}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])

    # --- 工具 ---
    def cancel_open_orders(self, symbol: str):
        """取消該 symbol 所有未成交委託（避免越掛越多）"""
        try:
            self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception:
            pass

    def _position_size(self, symbol: str) -> float:
        """回傳當前倉位數量（多為正、空為負）"""
        arr = self._get("/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(arr, list):
            for it in arr:
                if it.get("symbol") == symbol:
                    try:
                        return float(it.get("positionAmt") or "0")
                    except Exception:
                        return 0.0
        return 0.0

    def _wait_filled(self, symbol: str, want_side: str, want_qty: float, timeout_ms=2000) -> bool:
        """輪詢等待倉位建立（最多 2 秒）"""
        t0 = now_ts_ms()
        sign = 1.0 if want_side.upper() == "LONG" else -1.0
        target = sign * want_qty * 0.98  # 放寬 98% 避免精度差
        while now_ts_ms() - t0 <= timeout_ms:
            pos = self._position_size(symbol)
            if (sign > 0 and pos >= target) or (sign < 0 and pos <= target):
                return True
            time.sleep(0.08)
        return False

    # --- 進場（市價）+ 補掛 reduce-only TP/SL ---
    def place_bracket(self, symbol: str, side: str, qty_s: str, entry_s: str, sl_s: str, tp_s: str):
        if self._placing:
            raise RuntimeError("placing in progress")
        self._placing = True
        try:
            symbol = symbol.upper().strip()
            if symbol not in EXCHANGE_INFO:
                raise ValueError(f"symbol not tradable or not found in exchangeInfo: {symbol}")

            side_u = side.upper()
            is_bull = (side_u == "LONG")
            qty = float(qty_s)
            sl_f = float(sl_s)
            tp_f = float(tp_s)

            # 1) 清殘單
            self.cancel_open_orders(symbol)

            # 2) 市價進場（避免 -2021）
            params_entry = {
                "symbol": symbol,
                "side": ("BUY" if is_bull else "SELL"),
                "type": "MARKET",
                "quantity": qty_s,
                "newOrderRespType": "RESULT",
                "newClientOrderId": f"mkt_entry_{now_ts_ms()}",
            }
            self._post("/fapi/v1/order", params_entry)
            logger.info(f"[ENTRY] MARKET {symbol} {side_u} qty={qty_s}")

            # 3) 等倉位建立再掛 TP/SL
            filled = self._wait_filled(symbol, side_u, qty, timeout_ms=2000)
            if not filled:
                logger.warning(f"[WARN] Position not confirmed for {symbol}, assuming fill and attaching TP/SL")
                # 即使沒確認，也必須假設已開倉並設定 self.open，否則會重複下單

            info = EXCHANGE_INFO[symbol]
            price_prec = int(info.get("pricePrecision", 8))
            sl_s_fmt = f"{sl_f:.{price_prec}f}"
            tp_s_fmt = f"{tp_f:.{price_prec}f}"

            # ⬇️ *** 修正點 1：立刻設定 self.open ***
            # 必須先設定內部狀態，才去嘗試掛 TP/SL
            self.open = {
                "symbol": symbol, "side": side_u, "qty": float(qty_s),
                "entry": float(entry_s), "sl": float(sl_s_fmt), "tp": float(tp_s_fmt)
            }
            logger.info(f"[STATE SET] {symbol} position locked internally.") # 新增日誌

            # ⬇️ *** 修正點 2：用 try/except 包住 TP/SL ***
            try:
                # 止損（reduce-only：*_MARKET + closePosition=true）
                params_sl = {
                    "symbol": symbol,
                    "side": ("SELL" if is_bull else "BUY"),
                    "type": "STOP_MARKET",
                    "stopPrice": sl_s_fmt,
                    "closePosition": "true",
                    "workingType": "MARK_PRICE",
                    "newClientOrderId": f"sl_{now_ts_ms()}",
                }
                # 止盈
                params_tp = {
                    "symbol": symbol,
                    "side": ("SELL" if is_bull else "BUY"),
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": tp_s_fmt,
                    "closePosition": "true",
                    "workingType": "MARK_PRICE",
                    "newClientOrderId": f"tp_{now_ts_ms()}",
                }
                
                self._post("/fapi/v1/order", params_sl)
                self._post("/fapi/v1/order", params_tp)
                logger.info(f"[BRACKETS] {symbol} SL={sl_s_fmt} TP={tp_s_fmt} attached.")
            
            except Exception as e:
                # 即使 TP/SL 失敗，也不拋出例外
                # 這樣 self.open 狀態會被保留，主迴圈才不會重複下單
                logger.error(f"[CRITICAL] FAILED TO ATTACH TP/SL for {symbol}: {e}")
                # 這裡應該要觸發警報（例如 Telegram/Discord）
                # 但程式會繼續運行，並鎖定倉位
            
            # (刪除了原本在最後的 self.open = {...})
            return "OK"

        finally:
            self._placing = False

    # --- 偵測命中 TP/SL 後，強制收倉、清殘單、記帳 ---
# --- [修正版] 偵測命中 TP/SL 後，強制收倉、清殘單、記帳 ---
    def poll_and_close_if_hit(self, day_guard):
        if not self.open:
            return False, None, None, None, None

        symbol = self.open["symbol"]
        side   = self.open["side"]
        entry  = float(self.open["entry"])

        # === 修正 1：使用 Mark Price (防假跳動) ===
        try:
            p = self.get_mark_price(symbol)
        except Exception:
            p = self.best_price(symbol) # 備援

        hit_tp = (p >= self.open["tp"]) if side == "LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side == "LONG" else (p >= self.open["sl"])

        if not (hit_tp or hit_sl):
            return False, None, None, None, None # 未觸發，正常返回

        # --- 價格已觸發 ---
        exit_price = self.open["tp"] if hit_tp else self.open["sl"]
        pct = (exit_price - entry) / entry
        if side == "SHORT":
            pct = -pct
        reason = "TP" if hit_tp else "SL"

        # === 修正 2：更聰明的平倉邏輯 ===
        try:
            # 1. 嘗試市價平倉 (作為保險)
            logger.info(f"[CLOSE] {symbol} {reason} hit. Attempting force market close...")
            self._post("/fapi/v1/order", {
                "symbol": symbol,
                "side": ("SELL" if side == "LONG" else "BUY"),
                "type": "MARKET",
                "quantity": f"{float(self.open['qty']):.6f}",
                "reduceOnly": "true",
                "newClientOrderId": f"force_close_{now_ts_ms()}",
            })
            logger.info(f"[CLOSE] Force market close for {symbol} SUCCEEDED.")

        except Exception as e:
            # 2. 市價平倉失敗 (最可能的原因：實體 SL/TP 單已成交)
            logger.warning(f"[CLOSE] Force market close FAILED for {symbol}: {e}")

            # 3. 立即反查倉位
            try:
                current_pos = self._position_size(symbol)
                # 檢查倉位是否 "幾乎為 0"
                if abs(current_pos) < (float(self.open['qty']) * 0.01):
                    logger.info(f"[CLOSE] Position size is {current_pos}. Assuming closed by exchange, syncing state.")
                    # 倉位已平，這不是一個錯誤。我們當作平倉成功繼續往下執行。
                    pass
                else:
                    # 倉位還在，且市價平倉失敗。這是一個嚴重錯誤。
                    logger.error(f"[CRITICAL] FAILED to close {symbol}, position {current_pos} still open.")
                    return False, None, None, None, None # 返回 False，下一輪重試
            except Exception as e2:
                logger.error(f"[CRITICAL] Failed to check position size after close failure: {e2}")
                return False, None, None, None, None # 不確定狀態，返回 False 重試

        # --- 平倉成功 (不論是 Bot 還是交易所平的) ---

        # 4. 清理殘單 (例如剩下的 TP 單)
        try:
            logger.info(f"Cancelling remaining orders for {symbol}...")
            self.cancel_open_orders(symbol)
        except Exception:
            pass # 失敗也沒關係，倉位已平

        # 5. 記帳、清空 Bot 內部狀態
        trade_data = self.open
        self.open = None # <-- 清空狀態，解除面板鎖定
        day_guard.on_trade_close(pct)
        try:
            log_trade(
                symbol=symbol,
                side=trade_data["side"],
                qty=trade_data["qty"],
                entry=trade_data["entry"],
                exit_price=exit_price,
                ret_pct=pct,
                reason=reason,
                mode=config.SCALP_MODE or "VOL" # 確保日誌也有 mode
            )
        except Exception as e:
            logger.warning(f"Journal log_trade failed: {e}")

        logger.info(f"[CLOSE] {symbol} {reason} PnL={pct*100:.2f}% (State cleared, loop resolved)")
        return True, pct, symbol, reason, exit_price

    # ⬇️ *** 你還需要這個輔助函式 (get_mark_price) ***
    # 把它貼在 LiveAdapter 類別中的任何地方 (例如 __init__ 之後)
    def get_mark_price(self, symbol: str) -> float:
        """獲取當前標記價格 (Mark Price) 用於風控輪詢"""
        try:
            r = SESSION.get(f"{self.base}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=3)
            r.raise_for_status()
            return float(r.json()["markPrice"])
        except Exception as e:
            logger.warning(f"Failed to get markPrice for {symbol}: {e}, falling back to best_price (lastPrice)")
            return self.best_price(symbol)
    def place_scalp_bracket(self, symbol: str, side: str, qty_s: str, entry_s: str, sl_s: str, tp_s: str, maker_timeout_ms=1500):
            """
            [新增] Scalp 專用：Maker-Taker 智慧下單 + 掛載 TP/SL
            1. 嘗試 Maker (Post-Only) 限價單
            2. 等待 X 毫秒
            3. 若未成交，取消 Maker 單，改下 Taker (Market) 單
            4. 掛載 TP/SL
            """
            if self._placing:
                raise RuntimeError("placing in progress")
            self._placing = True

            symbol = symbol.upper().strip()
            side_u = side.upper()
            is_bull = (side_u == "LONG")
            qty = float(qty_s)
            sl_f = float(sl_s)
            tp_f = float(tp_s)
            entry_f = float(entry_s) # 參考價格
            
            avg_filled_price = None
            order_id = None

            try:
                # 1) 清殘單
                self.cancel_open_orders(symbol)

                # === 步驟 1: 嘗試 Maker (Post-Only) ===
                try:
                    params_maker = {
                        "symbol": symbol,
                        "side": ("BUY" if is_bull else "SELL"),
                        "type": "LIMIT",
                        "quantity": qty_s,
                        "price": entry_s, # 使用訊號參考價
                        "timeInForce": "GTX", # (Post-Only)
                        "newOrderRespType": "RESULT",
                        "newClientOrderId": f"maker_entry_{now_ts_ms()}",
                    }
                    maker_resp = self._post("/fapi/v1/order", params_maker)
                    order_id = maker_resp.get("orderId")
                    logger.info(f"[ENTRY] MAKER {symbol} {side_u} qty={qty_s} @ {entry_s} (ID: {order_id})")

                    # === 步驟 2: 等待 Maker 單成交 ===
                    time.sleep(maker_timeout_ms / 1000.0) # 等待 1.5 秒
                    
                    status_resp = self._get_order(symbol, order_id)
                    status = status_resp.get("status") if status_resp else "UNKNOWN"

                    if status == "FILLED":
                        logger.info(f"[ENTRY] MAKER order {order_id} FILLED.")
                        avg_filled_price = float(status_resp.get("avgPrice", entry_f))
                    
                    else:
                        # === 步驟 3: Maker 未成，取消並改 Taker ===
                        logger.warning(f"[ENTRY] MAKER {order_id} status={status}. Cancelling and switching to TAKER.")
                        self._cancel_order(symbol, order_id) # 嘗試取消
                        
                        # 檢查滑點保護
                        current_price = self.best_price(symbol)
                        slip_pct = abs(current_price - entry_f) / entry_f
                        if slip_pct > config.SLIPPAGE_CAP_PCT:
                            raise RuntimeError(f"Slippage too large on taker fallback ({slip_pct*100:.2f}%)")

                        params_taker = {
                            "symbol": symbol,
                            "side": ("BUY" if is_bull else "SELL"),
                            "type": "MARKET",
                            "quantity": qty_s,
                            "newOrderRespType": "RESULT",
                            "newClientOrderId": f"taker_entry_{now_ts_ms()}",
                        }
                        taker_resp = self._post("/fapi/v1/order", params_taker)
                        logger.info(f"[ENTRY] TAKER {symbol} {side_u} qty={qty_s} FILLED (Fallback).")
                        avg_filled_price = float(taker_resp.get("avgPrice", current_price))

                except Exception as e:
                    # 捕捉下單錯誤 (例如 Post-Only 失敗會立刻拒絕)
                    logger.warning(f"[ENTRY] MAKER attempt failed: {e}. Switching to TAKER.")
                    # 直接嘗試 Taker (市價)
                    params_taker = {
                        "symbol": symbol,
                        "side": ("BUY" if is_bull else "SELL"),
                        "type": "MARKET",
                        "quantity": qty_s,
                        "newOrderRespType": "RESULT",
                        "newClientOrderId": f"taker_entry_{now_ts_ms()}",
                    }
                    taker_resp = self._post("/fapi/v1/order", params_taker)
                    logger.info(f"[ENTRY] TAKER {symbol} {side_u} qty={qty_s} FILLED (Direct).")
                    avg_filled_price = float(taker_resp.get("avgPrice", self.best_price(symbol)))


                # === 步驟 4: 掛載 TP/SL (沿用你已修復的邏輯) ===
                # 使用 avg_filled_price 重新計算 TP/SL (更精準)
                # (如果 Taker 單沒有回傳 avgPrice，我們在上面已經用 best_price() 抓了)
                sl_f_new, tp_f_new = compute_bracket(avg_filled_price, side_u)

                info = EXCHANGE_INFO[symbol]
                price_prec = int(info.get("pricePrecision", 8))
                sl_s_fmt = f"{sl_f_new:.{price_prec}f}"
                tp_s_fmt = f"{tp_f_new:.{price_prec}f}"

                # ⬇️ *** 立刻設定 self.open (你已修復的關鍵邏輯) ***
                self.open = {
                    "symbol": symbol, "side": side_u, "qty": float(qty_s),
                    "entry": avg_filled_price, # <-- 使用真實成交均價
                    "sl": float(sl_s_fmt), "tp": float(tp_s_fmt)
                }
                logger.info(f"[STATE SET] {symbol} position locked internally @ {avg_filled_price:.{price_prec}f}")

                # ⬇️ *** 用 try/except 包住 TP/SL (你已修復的關鍵邏輯) ***
                try:
                    params_sl = {
                        "symbol": symbol, "side": ("SELL" if is_bull else "BUY"),
                        "type": "STOP_MARKET", "stopPrice": sl_s_fmt,
                        "closePosition": "true", "workingType": "MARK_PRICE",
                        "newClientOrderId": f"sl_{now_ts_ms()}",
                    }
                    params_tp = {
                        "symbol": symbol, "side": ("SELL" if is_bull else "BUY"),
                        "type": "TAKE_PROFIT_MARKET", "stopPrice": tp_s_fmt,
                        "closePosition": "true", "workingType": "MARK_PRICE",
                        "newClientOrderId": f"tp_{now_ts_ms()}",
                    }
                    self._post("/fapi/v1/order", params_sl)
                    self._post("/fapi/v1/order", params_tp)
                    logger.info(f"[BRACKETS] {symbol} SL={sl_s_fmt} TP={tp_s_fmt} attached.")
                
                except Exception as e:
                    logger.error(f"[CRITICAL] FAILED TO ATTACH TP/SL for {symbol}: {e}")
                
                return "OK"

            finally:
                self._placing = False
