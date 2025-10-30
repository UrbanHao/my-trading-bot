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
    進場用 STOP_MARKET（只送 stopPrice），避免 price 精度錯誤；
    TP/SL 用 *_MARKET（closePosition=true）。
    """
    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        BINANCE_FUTURES_TEST_BASE = "https://testnet.binancefuture.com"
        self.base = (BINANCE_FUTURES_TEST_BASE if USE_TESTNET else BINANCE_FUTURES_BASE)
        self.open = None
    def cancel_open_orders(self, symbol: str):
        # 取消該 symbol 所有未成交委託，避免越掛越多
        self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})

    def _sign(self, params: dict):
        # 用「實際會發送的 URL 編碼字串」做 HMAC，避免 -1022
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

    def has_open(self): return self.open is not None

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

    def place_bracket(self, symbol: str, side: str, qty_s: str, entry_s: str, sl_s: str, tp_s: str):
        """
        現價突破入場：
        - 進場用 STOP_MARKET（只送 stopPrice）
        - SL 用 STOP_MARKET(closePosition=true)
        - TP 用 TAKE_PROFIT_MARKET(closePosition=true)
        - 下單前先『清空該 symbol 的所有未成交委託』，避免 SL/TP 越堆越多
        - workingType 改用 MARK_PRICE，比 CONTRACT_PRICE 更穩
        - 嚴格檢查 stopPrice 與 MarkPrice 的相對位置，避免 -2021
        """
        symbol = symbol.upper().strip()
        if symbol not in EXCHANGE_INFO:
            raise ValueError(f"symbol not tradable or not found in exchangeInfo: {symbol}")

        # 先取消該 symbol 既有的未成交單（把殘留的 SL/TP 清掉）
        self._cancel_all_symbol_orders(symbol)

        side_u = side.upper()
        is_bull = (side_u == "LONG")

        qty_f   = float(qty_s)
        entry_f = float(entry_s)
        sl_f    = float(sl_s)
        tp_f    = float(tp_s)

        # 讀取精度/最小跳動
        info = EXCHANGE_INFO[symbol]
        price_prec = int(info.get("pricePrecision", 8))
        qty_prec   = int(info.get("quantityPrecision", 0))
        tick_size  = float(info.get("tickSize", 0.0) or 0.0)

        # 取 Mark Price（避免 CONTRACT / Last 價造成 -2021）
        try:
            mp = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
            mark_price = float(mp["markPrice"])
        except Exception:
            # 後備：取 ticker/price 當近似
            r = SESSION.get(f"{self.base}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
            r.raise_for_status()
            mark_price = float(r.json()["price"])

        # 進場 stopPrice 必須在「突破方向的外側」再推一格 tick，避免『立即觸發』
        # BUY：stopPrice > mark_price
        # SELL：stopPrice < mark_price
        if is_bull:
            if entry_f <= mark_price:
                entry_f = mark_price + (tick_size or 1e-8)
        else:
            if entry_f >= mark_price:
                entry_f = max(mark_price - (tick_size or 1e-8), 0.0)

        # 同步把 SL/TP 對齊交易所格子
        from decimal import Decimal, ROUND_DOWN
        def _floor_to_step(val: float, step: float, prec: int) -> float:
            if step and step > 0:
                q = Decimal(str(step))
                v = (Decimal(str(val)) / q).to_integral_value(rounding=ROUND_DOWN) * q
            else:
                v = Decimal(str(val))
            return float(f"{v:.{prec}f}")

        entry_f = _floor_to_step(entry_f, tick_size, price_prec)
        sl_f    = _floor_to_step(sl_f,    tick_size, price_prec)
        tp_f    = _floor_to_step(tp_f,    tick_size, price_prec)

        qty_f   = float(f"{qty_f:.{qty_prec}f}")
        entry_s = f"{entry_f:.{price_prec}f}"
        sl_s    = f"{sl_f:.{price_prec}f}"
        tp_s    = f"{tp_f:.{price_prec}f}"
        qty_s   = f"{qty_f:.{qty_prec}f}"

        # --- 最後一次檢查：避免入場單會立刻觸發 ---
        if is_bull and not (float(entry_s) > mark_price):
            raise ValueError(f"BUY entry stopPrice({entry_s}) must be > markPrice({mark_price}) to avoid -2021")
        if (not is_bull) and not (float(entry_s) < mark_price):
            raise ValueError(f"SELL entry stopPrice({entry_s}) must be < markPrice({mark_price}) to avoid -2021")

        # === 組參數 ===
        base_params = {
            "symbol": symbol,
            "workingType": "MARK_PRICE",     # 改用 Mark Price
            "newOrderRespType": "RESULT",
        }

        # 進場（STOP_MARKET，只送 stopPrice）
        params_entry = dict(base_params)
        params_entry.update({
            "side": ("BUY" if is_bull else "SELL"),
            "type": "STOP_MARKET",
            "stopPrice": entry_s,
            "quantity": qty_s,
            "timeInForce": "GTC",
            "priceProtect": "true",
            "newClientOrderId": f"entry_{int(time.time()*1000)}",
        })

        # 止損（closePosition=true）
        params_sl = dict(base_params)
        params_sl.update({
            "side": ("SELL" if is_bull else "BUY"),
            "type": "STOP_MARKET",
            "stopPrice": sl_s,
            "closePosition": "true",
            "priceProtect": "true",
            "newClientOrderId": f"sl_{int(time.time()*1000)}",
        })

        # 止盈（closePosition=true）
        params_tp = dict(base_params)
        params_tp.update({
            "side": ("SELL" if is_bull else "BUY"),
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": tp_s,
            "closePosition": "true",
            "priceProtect": "true",
            "newClientOrderId": f"tp_{int(time.time()*1000)}",
        })

        # === 送單 ===
        self._post("/fapi/v1/order", params_entry)
        self._post("/fapi/v1/order", params_sl)
        self._post("/fapi/v1/order", params_tp)

        self.open = {
            "symbol": symbol, "side": side_u, "qty": qty_f,
            "entry": entry_f, "sl": float(sl_s), "tp": float(tp_s)
        }
        return "OK"



    def poll_and_close_if_hit(self, day_guard):
        if not self.open:
            return False, None, None, None, None

        # 先嘗試補掛（若剛成交）
        try:
            self._attach_brackets_if_needed()
        except Exception:
            pass
        symbol = self.open["symbol"]
        side   = self.open["side"]
        entry  = float(self.open["entry"])
        p = self.best_price(symbol)
        hit_tp = (p >= self.open["tp"]) if side=="LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side=="LONG" else (p >= self.open["sl"])
        if not (hit_tp or hit_sl):
            return False, None, None, None, None

        exit_price = self.open["tp"] if hit_tp else self.open["sl"]
        pct = (exit_price - entry) / entry
        if side == "SHORT": pct = -pct
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

    def _position_size(self, symbol: str) -> float:
        # 用 /fapi/v2/positionRisk 抓當前倉位數量
        arr = self._get("/fapi/v2/positionRisk", {"symbol": symbol})
        # 回傳的可能是 list; 找對 symbol
        if isinstance(arr, list):
            for it in arr:
                if it.get("symbol") == symbol:
                    sz = float(it.get("positionAmt") or "0")
                    return sz
        return 0.0

    def _attach_brackets_if_needed(self):
        """若 self.open.pending=True 但倉位已建立，補掛 SL/TP"""
        if not self.open or not self.open.get("pending"):
            return
        symbol = self.open["symbol"]
        side_u = self.open["side"]
        is_bull = (side_u == "LONG")

        pos_sz = self._position_size(symbol)
        if abs(pos_sz) < 1e-12:
            return  # 還沒成交

        # 成交了 → 補掛 reduce-only 的 SL/TP（用 *_MARKET + closePosition=true）
        sl_s = f"{float(self.open['sl']):.8f}"
        tp_s = f"{float(self.open['tp']):.8f}"

        params_sl = {
            "symbol": symbol,
            "side":   ("SELL" if is_bull else "BUY"),
            "type":   "STOP_MARKET",
            "stopPrice": sl_s,
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE",
            "newClientOrderId": f"sl_{int(time.time()*1000)}",
        }
        params_tp = {
            "symbol": symbol,
            "side":   ("SELL" if is_bull else "BUY"),
            "type":   "TAKE_PROFIT_MARKET",
            "stopPrice": tp_s,
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE",
            "newClientOrderId": f"tp_{int(time.time()*1000)}",
        }
        self._post("/fapi/v1/order", params_sl)
        self._post("/fapi/v1/order", params_tp)

        self.open["pending"] = False  # 已補齊 SL/TP

    def _cancel_all_symbol_orders(self, symbol: str):
        """
        先把該標的所有『尚未成交』的委託（包含先前殘留的 SL/TP）全部取消。
        避免每輪掃描又多丟一組 closePosition 單，越堆越多。
        """
        try:
            self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception:
            # 沒單/或 API 回覆 400, 就當無事發生
            pass
