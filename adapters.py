import os, hmac, hashlib, time, logging
from decimal import Decimal, ROUND_DOWN

from utils import (
    now_ts_ms, SESSION, BINANCE_FUTURES_BASE, TIME_OFFSET_MS,
    ws_best_price, EXCHANGE_INFO, load_exchange_info, conform_to_filters
)
import config
from config import USE_TESTNET, ORDER_TIMEOUT_SEC, STOP_BUFFER_PCT, LIMIT_BUFFER_PCT
from journal import log_trade

try:
    from ws_client import ws_best_price as _ws_best_price
except Exception:
    _ws_best_price = None

from risk_frame import compute_stop_limit as rf_compute_stop_limit
compute_stop_limit = rf_compute_stop_limit

log = logging.getLogger(__name__)

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

    def poll_and_close_if_hit(self, day_guard):
        if not self.open:
            return False, None, None, None, None
        p = self.best_price(self.open["symbol"])
        side = self.open["side"]
        hit_tp = (p >= self.open["tp"]) if side=="LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side=="LONG" else (p >= self.open["sl"])
        if hit_tp or hit_sl:
            exit_price = self.open["tp"] if hit_tp else self.open["sl"]
            pct = (exit_price - self.open["entry"]) / self.open["entry"]
            if side == "SHORT": pct = -pct
            symbol = self.open["symbol"]
            reason = "TP" if hit_tp else "SL"
            trade_data = self.open
            self.open = None
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
            return True, pct, symbol, reason, exit_price
        return False, None, None, None, None

# ================================== 實單 Adapter ==================================
class LiveAdapter:
    """
    真實下單流程（不破壞你現有功能）：
    1) 先用 MARKET 進場（BUY/SELL），確保一定有部位（不再只有TP/SL孤兒單）。
    2) 成交後，補掛 reduceOnly 的 STOP_MARKET(SL) 與 TAKE_PROFIT_MARKET(TP)。
    3) 任一保護單成交 -> 立刻取消另一張（避免殘留越掛越多）。
    4) 入場前會先清掉該 symbol 所有未成交委託（安全閥）。
    """

    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        BINANCE_FUTURES_TEST_BASE = "https://testnet.binancefuture.com"
        self.base = (BINANCE_FUTURES_TEST_BASE if USE_TESTNET else BINANCE_FUTURES_BASE)
        # self.open: 紀錄目前部位&保護單 id，方便後續取消
        self.open = None

    # ---------- 低階 API ----------
    def _sign(self, params: dict):
        # 用實際會發送的 URL 編碼字串簽名，避免 -1022
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

    # ---------- 查餘額 / 最佳價 ----------
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

    # ---------- 公用：取消該標的所有未成交委託 ----------
    def cancel_open_orders(self, symbol: str):
        """取消該 symbol 的所有未成交掛單（含殘留 TP/SL），避免越掛越多。"""
        try:
            self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception:
            # 沒有掛單時 API 可能回錯，安全忽略
            pass

    # ---------- 內部：查淨部位數量 / 列出掛單 / 取消指定掛單 ----------
    def _position_amt(self, symbol: str) -> float:
        """>0 多倉；<0 空倉；=0 無倉"""
        arr = self._get("/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(arr, list) and arr:
            pos = arr[0]
        else:
            pos = arr
        try:
            return float(pos.get("positionAmt") or 0.0)
        except Exception:
            return 0.0

    def _orders_by_symbol(self, symbol: str):
        try:
            return self._get("/fapi/v1/openOrders", {"symbol": symbol})
        except Exception:
            return []

    def _cancel_orders(self, symbol: str, order_ids):
        for oid in order_ids:
            try:
                self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": oid})
            except Exception:
                pass

    # ---------- 市價強平（給 hotkey/time-stop 用） ----------
    def close_position_market(self, symbol: str):
        amt = self._position_amt(symbol)
        if abs(amt) < 1e-12:
            return
        side = "SELL" if amt > 0 else "BUY"
        self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{abs(amt)}",
            "reduceOnly": "true",
            "newClientOrderId": f"force_close_{int(time.time()*1000)}",
        })
        try:
            self.cancel_open_orders(symbol)
        except Exception:
            pass

    # ---------- 進場 + 掛保護單（關鍵） ----------
    def place_bracket(self, symbol: str, side: str, qty_s: str, entry_s: str, sl_s: str, tp_s: str):
        """
        一次性完成：
        1) 先取消舊掛單（避免殘留）。
        2) 用 MARKET 直接進場（BUY/SELL）。
        3) 確認有部位後，補掛 reduceOnly 的 STOP_MARKET(SL) 與 TAKE_PROFIT_MARKET(TP)。
        """
        symbol = symbol.upper().strip()
        if symbol not in EXCHANGE_INFO:
            raise ValueError(f"symbol not tradable or not found in exchangeInfo: {symbol}")

        is_bull = (side.upper() == "LONG")
        qty_f   = float(qty_s)
        sl_f    = float(sl_s)
        tp_f    = float(tp_s)

        # 1) 清掉該 symbol 既有未成交掛單
        self.cancel_open_orders(symbol)

        # 2) 市價進場（保證有部位）
        self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side":   ("BUY" if is_bull else "SELL"),
            "type":   "MARKET",
            "quantity": qty_s,
            "newClientOrderId": f"mkt_entry_{int(time.time()*1000)}",
        })

        # 等撮合完成，確認有部位
        time.sleep(0.3)
        pos_amt = self._position_amt(symbol)
        if abs(pos_amt) < 1e-12:
            time.sleep(0.5)
            pos_amt = self._position_amt(symbol)
        if abs(pos_amt) < 1e-12:
            raise RuntimeError("Entry MARKET not filled, abort bracket")

        # 3) 以 reduceOnly 掛 SL/TP（使用 *_MARKET + stopPrice；不會開新倉）
        sl_order = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side":   ("SELL" if is_bull else "BUY"),
            "type":   "STOP_MARKET",
            "stopPrice": f"{sl_f}",
            "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
            "newClientOrderId": f"sl_{int(time.time()*1000)}",
        })
        tp_order = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side":   ("SELL" if is_bull else "BUY"),
            "type":   "TAKE_PROFIT_MARKET",
            "stopPrice": f"{tp_f}",
            "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
            "newClientOrderId": f"tp_{int(time.time()*1000)}",
        })

        # 記錄目前部位與保護單 id
        self.open = {
            "symbol": symbol,
            "side":   ("LONG" if is_bull else "SHORT"),
            "qty":    abs(pos_amt),
            "entry":  self.best_price(symbol),  # 近似紀錄
            "sl":     sl_f,
            "tp":     tp_f,
            "sl_id":  sl_order.get("orderId"),
            "tp_id":  tp_order.get("orderId"),
        }
        return "OK"

    # ---------- 偵測是否已平倉 + 清殘單 + 記帳 ----------
    def poll_and_close_if_hit(self, day_guard):
        """
        不再用「比價」推估 TP/SL，
        直接以「該 symbol 部位是否歸 0」為準：若為 0 代表已由交易所的 reduceOnly 單平掉。
        然後主動取消另一張殘掛單；紀錄交易後回傳。
        """
        if not self.open:
            return False, None, None, None, None

        symbol = self.open["symbol"]
        side   = self.open["side"]
        entry  = float(self.open["entry"])

        pos_amt = self._position_amt(symbol)
        if abs(pos_amt) > 1e-12:
            # 仍有倉位 → 尚未觸發 TP/SL
            return False, None, None, None, None

        # 部位已為 0 → 其中一張成交了，取消另一張殘掛單
        try:
            self.cancel_open_orders(symbol)
        except Exception:
            pass

        # 估算出場價（用最新價格近似）
        try:
            exit_price = self.best_price(symbol)
        except Exception:
            exit_price = entry

        pct = (exit_price - entry) / entry
        if side == "SHORT":
            pct = -pct

        trade_data = self.open
        self.open = None

        # 更新日績效
        day_guard.on_trade_close(pct)

        # 記帳
        try:
            log_trade(
                symbol=symbol,
                side=trade_data["side"],
                qty=trade_data["qty"],
                entry=entry,
                exit_price=exit_price,
                ret_pct=pct,
                reason="TP/SL by exchange"
            )
        except Exception:
            pass

        return True, pct, symbol, "TP/SL", exit_price
