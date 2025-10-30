import os, hmac, hashlib, requests, time
from utils import now_ts_ms, SESSION, BINANCE_FUTURES_BASE, TIME_OFFSET_MS, safe_get_json, ws_best_price, EXCHANGE_INFO
try:
    TIME_OFFSET_MS
except NameError:
    TIME_OFFSET_MS = 0  # fallback if not imported
from dotenv import dotenv_values
import os, config
try:
    from ws_client import ws_best_price as _ws_best_price
except Exception:
    _ws_best_price = None
from config import USE_TESTNET, ORDER_TIMEOUT_SEC,STOP_BUFFER_PCT, LIMIT_BUFFER_PCT
from utils import conform_to_filters
from risk_frame import compute_stop_limit as rf_compute_stop_limit
from journal import log_trade # <-- 確保匯入 log_trade
from risk_frame import compute_stop_limit as rf_compute_stop_limit
class SimAdapter:
    def __init__(self):
        self.open = None
    def has_open(self): return self.open is not None
    def best_price(self, symbol: str) -> float:
        # 先試 WS
        if _ws_best_price:
            try:
                p = _ws_best_price(symbol)
                if p is not None:
                    return float(p)
            except Exception:
                pass
        # 後備：REST
        base = getattr(self, "base", BINANCE_FUTURES_BASE)
        r = SESSION.get(f"{base}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    def place_entry_smart(self, symbol, side, qty, ref_price, tick_size):
        # 模擬：maker 嘗試立即成交；未成→直接以 ref_price±slippage 成交
        from random import random
        filled_px = ref_price
        # 50% 機率 maker 秒成
        if random() < 0.5:
            return {"orderId": f"SIM_{time.time()}", "avgPrice": filled_px}
        # taker with slippage cap
        slip = ref_price * min(config.SLIPPAGE_CAP_PCT, 0.0005)
        filled_px = ref_price + (slip if side == "LONG" else -slip)
        return {"orderId": f"SIM_{time.time()}", "avgPrice": filled_px}
        
    def place_bracket(self, symbol, side, qty, entry, sl, tp):
        """
        模擬端改為 Stop-Limit 模式：
          - 用 entry 當「參考位」計算 stop/limit
          - 以 limit 價當作模擬成交價（保守估計滑價）
        簽名與回傳值不變。
        """
        # side: "LONG" / "SHORT"
        is_bull = (side == "LONG")

        # 1) 由 entry 作為參考位，計算 stop / limit
        try:
            ref_price = float(entry)
        except Exception:
            # 若外部傳字串，仍嘗試轉 float；失敗就退回最佳價
            ref_price = float(self.best_price(symbol))

        stop_px, limit_px = rf_compute_stop_limit(ref_price, side=("LONG" if is_bull else "SHORT"), stop_offset_pct=STOP_BUFFER_PCT, limit_offset_pct=LIMIT_BUFFER_PCT)

        # 2) 以 limit 價作為模擬成交價；TP/SL 沿用呼叫端傳入
        self.open = {
            "symbol": symbol,
            "side": side,
            "qty": float(qty),
            "entry": float(limit_px),      # <--- 用 limit 當模擬成交價
            "sl": float(sl),
            "tp": float(tp),
            # 非必要，但可保留計算痕跡（方便除錯/觀察）
            "entry_ref": float(ref_price),
            "entry_stop": float(stop_px),
            "entry_limit": float(limit_px),
            "orderType": "SIM-STOP-LIMIT"
        }
        return "SIM-ORDER"
    
    # --- .csv Bug 修復 (A.1) ---
    def poll_and_close_if_hit(self, day_guard):
        if not self.open: return False, None, None, None, None # <--- 修改
        try:
            p = self.best_price(self.open["symbol"])
        except Exception as _e:
            return False, None, None, None, None # <--- 修改
        side = self.open["side"]
        hit_tp = (p >= self.open["tp"]) if side=="LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side=="LONG" else (p >= self.open["sl"])
        if hit_tp or hit_sl:
            exit_price = self.open["tp"] if hit_tp else self.open["sl"]
            pct = (exit_price - self.open["entry"]) / self.open["entry"]
            if side == "SHORT": pct = -pct
            symbol = self.open["symbol"]
            reason = "TP" if hit_tp else "SL"       # <--- 新增
            trade_data = self.open                  # <--- 新增 (複製)
            self.open = None
            day_guard.on_trade_close(pct)
            
            # --- ↓↓↓ 呼叫交易日誌 ↓↓↓ ---
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
                pass # (不在 adapter 中處理日誌錯誤)
            # --- ↑↑↑ 結束呼叫 ↑↑↑ ---

            return True, pct, symbol, reason, exit_price # <--- 修改
        return False, None, None, None, None # <--- 修改

class LiveAdapter:
    """
    Binance USDT-M Futures — 限價進場 + 兩條互斥條件單（TP/SL，closePosition=true）
    流程：
      1) LIMIT 進場（GTC），等待成交（逾時撤單）
      2) 成交後同時掛 TAKE_PROFIT_MARKET 與 STOP_MARKET（closePosition=true）
      3) 任一成交後撤另一單，回報 PnL%
    """
    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        # (修復) 由於 config.py 中沒有定義測試網，我們在這裡定義
        BINANCE_FUTURES_TEST_BASE = "https://testnet.binancefuture.com"
        self.base = (BINANCE_FUTURES_TEST_BASE if USE_TESTNET else BINANCE_FUTURES_BASE)
        self.open = None  # {symbol, side, qty, entry, sl, tp, entryId, tpId, slId}
        
    def _sign(self, params:dict):
        q = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return q + "&signature=" + sig
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
    def _post(self, path, params):
            params = dict(params)
            params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
            params.setdefault("recvWindow", 60000)
            qs = self._sign(params)
            r = SESSION.post(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
            
            # --- 新增除錯訊息 ---
            if not r.ok:
                print(f"[API ERROR] POST {path} returned {r.status_code}")
                # (我們不在這裡 print r.text，因為 main.py 會處理)
            # ------------------

            r.raise_for_status()
            return r.json()

    def _get(self, path, params):
            params = dict(params or {})
            params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
            params.setdefault("recvWindow", 60000)

            qs = self._sign(params)
            r = SESSION.get(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
            
            # --- 新增除錯訊息 ---
            if not r.ok:
                print(f"[API ERROR] {path} returned {r.status_code}")
                # (我們不在這裡 print r.text，因為 main.py 會處理)
            # ------------------
            
            r.raise_for_status()
            return r.json()

    def _delete(self, path, params):
            params = dict(params or {})
            params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
            params.setdefault("recvWindow", 60000)
            qs = self._sign(params)
            r = SESSION.delete(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
            
            # --- 新增除錯訊息 ---
            if not r.ok:
                print(f"[API ERROR] DELETE {path} returned {r.status_code}")
                # (我們不在這裡 print r.text，因為 main.py 會處理)
            # ------------------

            r.raise_for_status()
            return r.json()

    def has_open(self): return self.open is not None

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
    def place_entry_smart(self, symbol, side, qty, ref_price, tick_size):
        """
        maker 優先：以不穿價的限價下單；X 毫秒未成交則撤單→市價（限制最大滑點）
        不影響既有 STOP-LIMIT 進場流程；只在 Scalp 模式使用。
        """
        # 1) maker 限價（若交易所支援 post-only，可帶 timeInForce='GTX'；否則將價格壓到不跨檔）
        px = ref_price
        # 讓買單價 <= bestBid、賣單價 >= bestAsk（外部請餵入）
        order_id = self._place_limit(symbol, side, qty, px, post_only=True)
        t0 = time.time()
        filled = False
        while time.time() - t0 < 1.5:  # 1.5 秒觀察窗
            st = self._query_order(symbol, order_id)
            if st["status"] in ("FILLED", "PARTIALLY_FILLED"):
                filled = True
                break
            time.sleep(0.1)

        if not filled:
            self._cancel_order(symbol, order_id)
            # 2) 市價 / 可成交限價（滑點上限）
            last = self._get_last_price(symbol)  # 由 ws / ticker 提供
            # 價格保護
            allowed = ref_price * config.SLIPPAGE_CAP_PCT
            if side == "LONG" and last > ref_price * (1 + config.SLIPPAGE_CAP_PCT):
                raise RuntimeError("slippage too large on taker entry (long)")
            if side == "SHORT" and last < ref_price * (1 - config.SLIPPAGE_CAP_PCT):
                raise RuntimeError("slippage too large on taker entry (short)")
            order_id = self._place_market(symbol, side, qty)

        avg_entry = self._get_filled_price(symbol, order_id)
        return {"orderId": order_id, "avgPrice": avg_entry}
        
    # adapters.py (修改後的版本)
    def place_bracket(self, symbol, side, qty_str, entry_str, sl_str, tp_str):
        """
        Binance USDT-M Futures — 以 STOP-LIMIT 進場 + 兩條互斥條件單（TP/SL，closePosition=true）
        流程：
          1) STOP-LIMIT 進場（type="STOP"，stopPrice=觸發價，price=限價），等待成交（逾時撤單）
          2) 成交後掛 TAKE_PROFIT_MARKET / STOP_MARKET（closePosition=true）
        簽名與回傳值不變。
        """
        if side not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG/SHORT")
        order_side = "BUY" if side == "LONG" else "SELL"

        # 1) 由 entry_str 作為「參考位」計算 stop / limit
        try:
            ref_price = float(entry_str)
        except Exception:
            # 保底：用 ticker 價當參考
            ref_price = float(self.best_price(symbol))

        stop_px, limit_px = compute_stop_limit(ref_price, is_bull=(side == "LONG"),
                                               stop_buf=STOP_BUFFER_PCT, limit_buf=LIMIT_BUFFER_PCT)

        # 2) 送出 STOP-LIMIT 進場單
        #   Binance Futures 進場觸發單：type="STOP"（需同時提供 stopPrice 與 price）
        #   timeInForce 一般用 GTC；workingType 可用 CONTRACT_PRICE
        entry_params = {
            "symbol": symbol,
            "side": order_side,
            "type": "STOP",            # <--- 關鍵：STOP-LIMIT（有 price + stopPrice）
            "timeInForce": "GTC",
            "quantity": qty_str,
            "price": f"{limit_px:.10f}",       # 限價
            "stopPrice": f"{stop_px:.10f}",    # 觸發價
            "workingType": "CONTRACT_PRICE",
            "newClientOrderId": f"entry_{int(time.time())}"
        }
        entry_res = self._post("/fapi/v1/order", entry_params)
        entry_id = entry_res["orderId"]

        # 3) 等待成交或逾時撤單
        t0 = time.time()
        filled = False
        while time.time() - t0 < ORDER_TIMEOUT_SEC:
            q = self._get("/fapi/v1/order", {"symbol": symbol, "orderId": entry_id})
            st = q.get("status")
            if st == "FILLED":
                filled = True
                break
            # 若已成為過期或被撤銷，也結束等待
            if st in ("CANCELED", "EXPIRED", "REJECTED"):
                break
            time.sleep(0.6)

        if not filled:
            try:
                self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": entry_id})
            finally:
                self.open = None
            raise TimeoutError("Entry stop-limit order not filled within timeout; canceled.")

        # 4) 成交後同時掛 TP / SL（closePosition=true）
        exit_side = "SELL" if side == "LONG" else "BUY"

        tp_res = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": tp_str,
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE"
        })
        sl_res = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "STOP_MARKET",
            "stopPrice": sl_str,
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE"
        })

        # 5) 記錄部位（以限價作為 entry 記錄；實際成交價可再用查單補寫）
        self.open = {
            "symbol": symbol, "side": side, "qty": float(qty_str),
            "entry": float(limit_px), "sl": float(sl_str), "tp": float(tp_str),
            "entryId": entry_id,
            "tpId": tp_res["orderId"], "slId": sl_res["orderId"],
            "entry_ref": float(ref_price),
            "entry_stop": float(stop_px),
            "entry_limit": float(limit_px),
            "orderType": "STOP-LIMIT"
        }
        return str(entry_id)

    # --- .csv Bug 修復 (A.2) ---
    def poll_and_close_if_hit(self, day_guard):
        if not self.open: return False, None, None, None, None # <--- 修改
        symbol = self.open["symbol"]
        side   = self.open["side"]
        entry  = float(self.open["entry"])
        tpId   = self.open["tpId"]
        slId   = self.open["slId"]

        tp_q = self._get("/fapi/v1/order", {"symbol":symbol, "orderId":tpId})
        sl_q = self._get("/fapi/v1/order", {"symbol":symbol, "orderId":slId})
        tp_filled = tp_q.get("status") == "FILLED"
        sl_filled = sl_q.get("status") == "FILLED"

        if not tp_filled and not sl_filled:
            return False, None, None, None, None # <--- 修改

        exit_price = float(self.open["tp"] if tp_filled else self.open["sl"])
        pct = (exit_price - entry) / entry
        if side == "SHORT": pct = -pct
        reason = "TP" if tp_filled else "SL" # <--- 新增
        trade_data = self.open               # <--- 新增 (複製)

        # 撤另一條未成交單
        try:
            other_id = slId if tp_filled else tpId
            other_q  = self._get("/fapi/v1/order", {"symbol":symbol, "orderId":other_id})
            if other_q.get("status") in ("NEW","PARTIALLY_FILLED"):
                self._delete("/fapi/v1/order", {"symbol":symbol, "orderId":other_id})
        except Exception:
            pass

        self.open = None
        day_guard.on_trade_close(pct)
        
        # --- ↓↓↓ 呼叫交易日誌 ↓↓↓ ---
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
            pass # (不在 adapter 中處理日誌錯誤)
        # --- ↑↑↑ 結束呼叫 ↑↑↑ ---
            
        return True, pct, symbol, reason, exit_price # <--- 修改
