# ws_client.py
# Binance Futures WebSocket helper with price, microstructure, and 1m kline caches.
# 保留既有 API：start_ws(symbols, use_testnet), stop_ws(), ws_best_price(symbol)

import json
import threading
import time
from typing import Dict, List, Optional
import asyncio
from collections import deque, defaultdict
from threading import Lock

import websockets  # pip install websockets

# ==== 可選設定（若 config 沒有對應鍵，這裡提供安全預設） ====
try:
    import config
    _IMB_LOOKBACK_S = int(getattr(config, "TRADE_IMB_LOOKBACK_S", 15))
except Exception:
    _IMB_LOOKBACK_S = 15  # seconds

# ==== 連線端點 ====
_HOST = {
    "test": "wss://stream.binancefuture.com",
    "main": "wss://fstream.binance.com",
}

# ==== 全域狀態 ====
_WS_THREAD: Optional[threading.Thread] = None
_WS_STOP = False
_PRICE: Dict[str, float] = {}                    # last price from 24h ticker
_SUBS: List[str] = []                            # current subscribed symbols (upper)
_LOCK = Lock()

# Microstructure + Kline caches
_MICRO = defaultdict(lambda: {
    "obi": None,                # 頂層委買賣不平衡（0..1）
    "spread": None,             # 相對價差（%）
    "trade_buy_ratio": None,    # 最近窗口主動買量比
    "ts": 0.0,
})
_K1M = defaultdict(lambda: deque(maxlen=200))    # (o,h,l,c,v,ts)
_IMB_WINDOWS: Dict[str, deque] = {}              # symbol -> deque[(t, price, qty, is_aggr_buy)]

# ==== 對外查價（保留舊名） ====
def ws_best_price(symbol: str) -> Optional[float]:
    """取得最近的 last price（24h ticker 的 c）"""
    with _LOCK:
        return _PRICE.get(symbol.upper())

# ==== micro/kline 對外讀取 ====
def get_micro(symbol: str) -> Dict:
    """回傳 micro 結構的淺拷貝（obi/spread/trade_buy_ratio/ts）"""
    s = symbol.upper()
    with _LOCK:
        return dict(_MICRO[s])

def get_k1m(symbol: str, n: int = 50):
    """取得最近 n 根 1m K 線（關盤後寫入）"""
    s = symbol.upper()
    with _LOCK:
        data = list(_K1M[s])[-n:]
    return data

# ==== 產生訂閱串 ====
def _make_streams(symbols: List[str]) -> List[str]:
    # Binance futures streams 需小寫 symbol
    sts = []
    for s in symbols:
        sl = s.lower()
        sts.append(f"{sl}@ticker")         # 24h ticker（取 c 當 last）
        sts.append(f"{sl}@kline_1m")       # 1m kline（用已收盤 k 寫入）
        sts.append(f"{sl}@depth5@100ms")   # 頂層五檔，計算 OBI / spread
        sts.append(f"{sl}@aggTrade")       # 聚合成交，計算主動量比
    return sts

# ==== WS 主循環 ====
async def _run_ws(loop_syms: List[str], use_testnet: bool):
    global _PRICE
    url = (_HOST["test"] if use_testnet else _HOST["main"]) + "/ws"
    streams = _make_streams(loop_syms)
    sub_msg = {"method": "SUBSCRIBE", "params": streams, "id": 1}

    while not _WS_STOP:
        try:
            async with websockets.connect(
                url, ping_interval=15, ping_timeout=15, close_timeout=5
            ) as ws:
                # 訂閱
                await ws.send(json.dumps(sub_msg))

                # 讀取循環
                while not _WS_STOP:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        # 嘗試 ping 保活
                        try:
                            await ws.ping()
                        except Exception:
                            break
                        continue

                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue

                    # 兼容 /ws 與 /stream（/stream 會包 {stream,data}）
                    data = payload.get("data", payload)
                    if not isinstance(data, dict):
                        continue

                    # ACK 訊息：{"result": None, "id": 1}
                    if "result" in data:
                        continue

                    # 事件型別
                    et = data.get("e")
                    if not et:
                        # 無事件型別；忽略
                        continue

                    # === 24h Ticker（last price）===
                    if et == "24hrTicker":
                        s = data.get("s")
                        c = data.get("c")
                        if s and c is not None:
                            try:
                                c = float(c)
                                with _LOCK:
                                    _PRICE[s] = c
                            except Exception:
                                pass

                    # === 1m K 線（只收已結束的 bar）===
                    elif et == "kline":
                        s = data.get("s")
                        k = data.get("k") or {}
                        # k: { t,o,h,l,c,v, T, x ... }；x=True 表示該 bar 已收盤
                        if s and k.get("x"):
                            try:
                                o = float(k["o"]); h = float(k["h"]); l = float(k["l"])
                                c = float(k["c"]); v = float(k["v"])
                                ts = float(k.get("T", 0)) / 1000.0
                                with _LOCK:
                                    _K1M[s].append((o, h, l, c, v, ts))
                            except Exception:
                                pass

                    # === 深度（頂層）===
                    elif et == "depthUpdate":
                        s = data.get("s")
                        bids = data.get("b") or []
                        asks = data.get("a") or []
                        if s and bids and asks:
                            try:
                                bp = float(bids[0][0]); bq = float(bids[0][1])
                                ap = float(asks[0][0]); aq = float(asks[0][1])
                                obi = bq / (bq + aq) if (bq + aq) > 0 else None
                                spread = (ap - bp) / ((ap + bp) / 2.0) if (ap + bp) != 0 else None
                                with _LOCK:
                                    _MICRO[s].update({"obi": obi, "spread": spread, "ts": time.time()})
                            except Exception:
                                pass

                    # === 聚合成交（主動量比）===
                    elif et == "aggTrade":
                        s = data.get("s")
                        if s:
                            try:
                                p = float(data["p"])
                                q = float(data["q"])
                                # m=True 表示 Buyer 是 market maker（即賣方主動），所以主動買為 not m
                                is_buyer_maker = bool(data["m"])
                                is_aggr_buy = not is_buyer_maker

                                now = time.time()
                                win = _IMB_WINDOWS.setdefault(s, deque(maxlen=200))
                                win.append((now, p, q, is_aggr_buy))

                                cutoff = now - _IMB_LOOKBACK_S
                                buys = 0.0
                                sells = 0.0
                                # 累積最近窗口
                                for t, _p, _q, is_buy in win:
                                    if t >= cutoff:
                                        if is_buy:
                                            buys += _q
                                        else:
                                            sells += _q
                                ratio = buys / (buys + sells) if (buys + sells) > 0 else None
                                with _LOCK:
                                    _MICRO[s].update({"trade_buy_ratio": ratio, "ts": now})
                            except Exception:
                                pass

                    # 其他事件忽略
        except Exception:
            # 短暫睡一下再重連
            await asyncio.sleep(1.0)

# ==== 啟動 / 更新訂閱（保留舊名與行為） ====
def start_ws(symbols: List[str], use_testnet: bool):
    """
    啟動或更新 WebSocket 訂閱。
    重複呼叫會比對 symbols，若不同會重啟並重新訂閱。
    """
    global _WS_THREAD, _WS_STOP, _SUBS
    syms = [s.upper() for s in symbols if isinstance(s, str)]
    # 相同訂閱且執行緒存活就不重啟
    if syms == _SUBS and _WS_THREAD and _WS_THREAD.is_alive():
        return

    _SUBS = syms
    stop_ws()               # 確保先停掉舊連線
    if not _SUBS:
        return              # 沒有要訂閱的標的

    _WS_STOP = False

    def _t():
        try:
            asyncio.run(_run_ws(_SUBS, use_testnet))
        except Exception:
            pass

    _WS_THREAD = threading.Thread(target=_t, daemon=True)
    _WS_THREAD.start()

def stop_ws():
    """停止 WebSocket 連線。"""
    global _WS_THREAD, _WS_STOP
    _WS_STOP = True
    if _WS_THREAD and _WS_THREAD.is_alive():
        try:
            _WS_THREAD.join(timeout=0.5)
        except Exception:
            pass
    _WS_THREAD = None

# ==== 可選：類別介面（不破壞原本函數名稱；給未來擴充用） ====
class WSClient:
    """
    輕量封裝：維持與先前草稿一致的成員/方法名稱，
    內部直接使用上面全域 caches 與 start_ws/stop_ws。
    """
    def __init__(self):
        self._lock = _LOCK  # 與全域共用鎖

    def subscribe_scalp_streams(self, symbols: List[str], use_testnet: bool = False):
        start_ws(symbols, use_testnet)

    # 回呼樣板（保留介面，不一定會被外部呼叫；如被調用，直接寫入全域緩存）
    def on_kline_1m(self, symbol, k_tuple):
        # k_tuple: (o,h,l,c,v,ts)
        s = symbol.upper()
        with _LOCK:
            _K1M[s].append(k_tuple)

    def on_depth5(self, symbol, bids, asks):
        s = symbol.upper()
        try:
            bp = float(bids[0][0]); bq = float(bids[0][1])
            ap = float(asks[0][0]); aq = float(asks[0][1])
            obi = bq / (bq + aq) if (bq + aq) > 0 else None
            spread = (ap - bp) / ((ap + bp) / 2.0) if (ap + bp) != 0 else None
            with _LOCK:
                _MICRO[s].update({"obi": obi, "spread": spread, "ts": time.time()})
        except Exception:
            pass

    def on_agg_trade(self, symbol, price, qty, is_buyer_maker):
        s = symbol.upper()
        now = time.time()
        is_aggr_buy = not bool(is_buyer_maker)
        win = _IMB_WINDOWS.setdefault(s, deque(maxlen=200))
        win.append((now, float(price), float(qty), is_aggr_buy))
        cutoff = now - _IMB_LOOKBACK_S
        buys = 0.0; sells = 0.0
        for t, _p, _q, is_buy in win:
            if t >= cutoff:
                if is_buy: buys += _q
                else: sells += _q
        ratio = buys / (buys + sells) if (buys + sells) > 0 else None
        with _LOCK:
            _MICRO[s].update({"trade_buy_ratio": ratio, "ts": now})

    # 對外取數
    def get_micro(self, symbol):
        return get_micro(symbol)

    def get_k1m(self, symbol, n=50):
        return get_k1m(symbol, n)
