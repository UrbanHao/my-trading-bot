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
    進場：MARKET
    立即補掛：STOP_MARKET (SL, closePosition=true) + TAKE_PROFIT_MARKET (TP, closePosition=true)
    關鍵原則：以「交易所實際倉位與委託狀態」為唯一真相，面板只讀取同步狀態。
    """

    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        self.base = (BINANCE_FUTURES_TEST_BASE if USE_TESTNET else BINANCE_FUTURES_BASE)
        self.open = None  # {"symbol","side","qty","entry","sl","tp"}
        load_exchange_info()

    # ---------------- low-level http ----------------
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

    def _get(self, path, params=None):
        params = dict(params or {})
        params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
        params.setdefault("recvWindow", 60000)
        qs = self._sign(params)
        r = SESSION.get(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()

    def _delete(self, path, params=None):
        params = dict(params or {})
        params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
        params.setdefault("recvWindow", 60000)
        qs = self._sign(params)
        r = SESSION.delete(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()

    # ---------------- public helpers ----------------
    def has_open(self):
        return self.open is not None

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

    def best_price(self, symbol: str) -> float:
        r = SESSION.get(f"{self.base}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])

    # ---------------- exchange state ----------------
    def _position_amt(self, symbol: str) -> float:
        arr = self._get("/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(arr, list):
            for it in arr:
                if it.get("symbol") == symbol:
                    return float(it.get("positionAmt") or "0")
        return 0.0

    def _avg_entry_price(self, symbol: str) -> float:
        arr = self._get("/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(arr, list):
            for it in arr:
                if it.get("symbol") == symbol:
                    p = it.get("entryPrice") or "0"
                    try: return float(p)
                    except: return 0.0
        return 0.0

    def _open_orders(self, symbol: str):
        try:
            return self._get("/fapi/v1/openOrders", {"symbol": symbol})
        except:
            return []

    def cancel_open_orders(self, symbol: str):
        try:
            self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception:
            pass  # 沒單就算了

    def _ensure_oneway_and_leverage(self, symbol: str):
        # 確保單向持倉（避免對沖模式造成 reduceOnly 行為怪異）
        try:
            self._post("/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})
        except Exception:
            pass
        # 設置槓桿（若 config 有 LEVERAGE）
        try:
            lev = int(LEVERAGE) if LEVERAGE else 10
            self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})
        except Exception:
            pass

    # ---------------- main contract ----------------
    def place_bracket(self, symbol: str, side: str, qty_s: str, entry_s: str, sl_s: str, tp_s: str):
        """
        契約：主流程呼叫一次 → 這裡做完整流程：
        1) 取消 symbol 既有未成委託
        2) 市價進場
        3) 以 closePosition=true 補掛 SL/TP（MARK_PRICE）
        4) self.open 記錄入場（只做顯示用途；真實狀態以交易所為準，靠 sync_state()）
        """
        load_exchange_info()
        symbol = symbol.upper()
        if symbol not in EXCHANGE_INFO:
            raise ValueError(f"symbol not tradable or not found in exchangeInfo: {symbol}")

        info = EXCHANGE_INFO[symbol]
        price_prec = int(info.get("pricePrecision", 8))
        qty_prec   = int(info.get("quantityPrecision", 0))
        tick       = float(info.get("tickSize", 0.0) or 0.0)

        # 對齊格子（保守向下取格）
        sl_f = _floor_to_step(float(sl_s), tick,  price_prec)
        tp_f = _floor_to_step(float(tp_s), tick,  price_prec)
        qty_f = float(f"{float(qty_s):.{qty_prec}f}")

        side_u = side.upper()
        is_bull = (side_u == "LONG")

        # 準備環境 + 清殘單
        self._ensure_oneway_and_leverage(symbol)
        self.cancel_open_orders(symbol)

        # 1) 市價進場
        params_entry = {
            "symbol": symbol,
            "side":   ("BUY" if is_bull else "SELL"),
            "type":   "MARKET",
            "quantity": f"{qty_f:.{qty_prec}f}",
            "newOrderRespType": "RESULT",
            "newClientOrderId": f"mkt_entry_{int(time.time()*1000)}",
        }
        self._post("/fapi/v1/order", params_entry)
        log(f"MARKET ENTRY for {symbol} {side_u} qty={qty_f}", "ORDER")

        # 2) 立即補掛 reduce-only SL/TP（以 Mark Price 觸發）
        base = {
            "symbol": symbol,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "newOrderRespType": "RESULT",
        }

        params_sl = dict(base)
        params_sl.update({
            "side": ("SELL" if is_bull else "BUY"),
            "type": "STOP_MARKET",
            "stopPrice": _fmt(sl_f, price_prec),
            "newClientOrderId": f"sl_{int(time.time()*1000)}",
        })
        params_tp = dict(base)
        params_tp.update({
            "side": ("SELL" if is_bull else "BUY"),
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": _fmt(tp_f, price_prec),
            "newClientOrderId": f"tp_{int(time.time()*1000)}",
        })
        self._post("/fapi/v1/order", params_sl)
        self._post("/fapi/v1/order", params_tp)
        log(f"ATTACHED SL/TP for {symbol} SL={_fmt(sl_f, price_prec)} TP={_fmt(tp_f, price_prec)}", "ORDER")

        # 3) 記錄顯示用狀態（面板會透過 sync_state() 校正）
        entry_px = self._avg_entry_price(symbol)
        self.open = {
            "symbol": symbol, "side": side_u, "qty": qty_f,
            "entry": entry_px or float(entry_s), "sl": sl_f, "tp": tp_f
        }
        return "OK"

    def poll_and_close_if_hit(self, day_guard):
        """
        不再用本地「價格交叉」來判 TP/SL。
        只看「倉位是否歸零」。若歸零 → 認定已由 TP/SL（或手動）平倉，做清理與記帳。
        """
        if not self.open:
            return False, None, None, None, None

        symbol = self.open["symbol"]
        pos_sz = self._position_amt(symbol)

        if abs(pos_sz) > 1e-12:
            # 倉位仍在 → 若 SL/TP 不存在則補掛
            self._attach_brackets_if_needed()
            return False, None, None, None, None

        # 倉位已經沒了 → 視為觸發 TP 或 SL（或手動關）
        # 嘗試判斷哪個被觸發（以當前價格接近誰為準，僅供日誌）
        side = self.open["side"]
        entry = float(self.open["entry"])
        sl = float(self.open["sl"])
        tp = float(self.open["tp"])
        try:
            p = self.best_price(symbol)
        except Exception:
            p = entry

        diff_tp = abs(p - tp)
        diff_sl = abs(p - sl)
        reason = "TP" if diff_tp <= diff_sl else "SL"
        exit_price = tp if reason == "TP" else sl

        pct = (exit_price - entry) / entry
        if side == "SHORT": pct = -pct

        # 清乾淨所有未成交委託（殘單）
        self.cancel_open_orders(symbol)

        # 記帳 + 清本地
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

        log(f"{reason} HIT {symbol} pnl={pct*100:.2f}%", "ORDER")
        return True, pct, symbol, reason, exit_price

    def _attach_brackets_if_needed(self):
        if not self.open:
            return
        symbol = self.open["symbol"]
        side_u = self.open["side"]
        is_bull = (side_u == "LONG")

        # 若委託裡已經有 reduce-only 的條件單就不補
        orders = self._open_orders(symbol) or []
        has_sl = any(o.get("type") == "STOP_MARKET" and o.get("closePosition") for o in orders)
        has_tp = any(o.get("type") == "TAKE_PROFIT_MARKET" and o.get("closePosition") for o in orders)
        if has_sl and has_tp:
            return

        info = EXCHANGE_INFO[symbol]
        price_prec = int(info.get("pricePrecision", 8))
        sl_s = _fmt(float(self.open["sl"]), price_prec)
        tp_s = _fmt(float(self.open["tp"]), price_prec)

        base = {
            "symbol": symbol,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "newOrderRespType": "RESULT",
        }
        if not has_sl:
            self._post("/fapi/v1/order", {
                **base,
                "side": ("SELL" if is_bull else "BUY"),
                "type": "STOP_MARKET",
                "stopPrice": sl_s,
                "newClientOrderId": f"sl_{int(time.time()*1000)}",
            })
        if not has_tp:
            self._post("/fapi/v1/order", {
                **base,
                "side": ("SELL" if is_bull else "BUY"),
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": tp_s,
                "newClientOrderId": f"tp_{int(time.time()*1000)}",
            })

    # 讓面板對齊交易所真實狀態：若本地有 open，但交易所倉位已 0，則清理；反之更新 entry/qty
    def sync_state(self):
        if not self.open:
            return
        symbol = self.open["symbol"]
        amt = self._position_amt(symbol)
        if abs(amt) < 1e-12:
            self.open = None
            return
        # 更新面板用欄位
        self.open["entry"] = self._avg_entry_price(symbol) or self.open["entry"]
        self.open["qty"] = abs(amt)
