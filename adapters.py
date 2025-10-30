import os, hmac, hashlib, time, logging
from decimal import Decimal, ROUND_DOWN
import requests

from utils import (
    now_ts_ms, SESSION, BINANCE_FUTURES_BASE, TIME_OFFSET_MS,
    EXCHANGE_INFO, load_exchange_info, log
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
        self.base = (BINANCE_FUTURES_TEST_BASE if USE_TESTNET else BINANCE_FUTURES_BASE)
        self.open = None
        self._placing = False  # 簡單「單飛」鎖，避免同時打進場

    # --- 公用 ---
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

    # --- 取消該 symbol 所有未成交委託 ---
    def cancel_open_orders(self, symbol: str):
        try:
            self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception:
            pass

    # --- 讀倉位大小 ---
    def _position_size(self, symbol: str) -> float:
        arr = self._get("/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(arr, list):
            for it in arr:
                if it.get("symbol") == symbol:
                    try:
                        return float(it.get("positionAmt") or "0")
                    except Exception:
                        return 0.0
        return 0.0

    # --- 等待倉位建立（最多 2 秒）---
    def _wait_filled(self, symbol: str, want_side: str, want_qty: float, timeout_ms=2000) -> bool:
        t0 = now_ts_ms()
        sign = 1.0 if want_side.upper() == "LONG" else -1.0
        target = sign * want_qty * 0.98  # 放寬到 98%（避開四捨五入）
        while now_ts_ms() - t0 <= timeout_ms:
            pos = self._position_size(symbol)
            if (sign > 0 and pos >= target) or (sign < 0 and pos <= target):
                return True
            time.sleep(0.08)
        return False

    # --- 下單：市價進場 + 掛 reduce-only TP/SL ---
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

            # 1) 清乾淨殘單
            self.cancel_open_orders(symbol)

            # 2) 直接市價進場（避免 -2021）
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
                logger.warning(f"[WARN] Position not confirmed for {symbol}, still attach TP/SL (best effort)")

            # 取精度
            info = EXCHANGE_INFO[symbol]
            price_prec = int(info.get("pricePrecision", 8))
            sl_s_fmt = f"{sl_f:.{price_prec}f}"
            tp_s_fmt = f"{tp_f:.{price_prec}f}"

            # 止損
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
            logger.info(f"[BRACKETS] {symbol} SL={sl_s_fmt} TP={tp_s_fmt}")

            # 面板狀態
            self.open = {
                "symbol": symbol, "side": side_u, "qty": float(qty_s),
                "entry": float(entry_s), "sl": float(sl_s_fmt), "tp": float(tp_s_fmt)
            }
            return "OK"

        finally:
            self._placing = False

    # --- 輪詢平倉檢查：若命中 TP/SL，清殘單、記帳 ---
    def poll_and_close_if_hit(self, day_guard):
        if not self.open:
            return False, None, None, None, None

        symbol = self.open["symbol"]
        side   = self.open["side"]
        entry  = float(self.open["entry"])
        p      = self.best_price(symbol)
        hit_tp = (p >= self.open["tp"]) if side == "LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side == "LONG" else (p >= self.open["sl"])

        if not (hit_tp or hit_sl):
            return False, None, None, None, None

        # 認定觸發 → 強制收倉（保險），並取消殘單
        exit_price = self.open["tp"] if hit_tp else self.open["sl"]
        pct = (exit_price - entry) / entry
        if side == "SHORT":
            pct = -pct
        reason = "TP" if hit_tp else "SL"

        try:
            self._post("/fapi/v1/order", {
                "symbol": symbol,
                "side": ("SELL" if side == "LONG" else "BUY"),
                "type": "MARKET",
                "quantity": f"{float(self.open['qty']):.6f}",
                "newClientOrderId": f"force_close_{now_ts_ms()}",
            })
        except Exception as e:
            logger.warning(f"[CLOSE] force market close failed {symbol}: {e}")

        try:
            self.cancel_open_orders(symbol)
        except Exception:
            pass

        # 清面板、記損益
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

        logger.info(f"[CLOSE] {symbol} {reason} PnL={pct*100:.2f}%")
        return True, pct, symbol, reason, exit_price
