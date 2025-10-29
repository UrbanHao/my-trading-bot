import json, threading, time
from typing import Dict, List, Optional
import websockets, asyncio

_WS_THREAD = None
_WS_STOP = False
_PRICE: Dict[str, float] = {}
_SUBS: List[str] = []
_HOST = {"test": "wss://stream.binancefuture.com", "main": "wss://fstream.binance.com"}

def ws_best_price(symbol: str) -> Optional[float]:
    return _PRICE.get(symbol.upper())

async def _run_ws(loop_syms: List[str], use_testnet: bool):
    global _PRICE
    url = (_HOST["test"] if use_testnet else _HOST["main"]) + "/ws"
    streams = [f"{s.lower()}@ticker" for s in loop_syms]
    sub_msg = {"method":"SUBSCRIBE","params":streams,"id":1}
    while not _WS_STOP:
        try:
            async with websockets.connect(url, ping_interval=15, ping_timeout=15) as ws:
                await ws.send(json.dumps(sub_msg))
                while not _WS_STOP:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    d = json.loads(msg)
                    # 24hr ticker 回傳：s=SYMBOL, c=lastPrice
                    if "s" in d and "c" in d:
                        _PRICE[d["s"]] = float(d["c"])
        except Exception:
            # 短暫睡一下再重連
            time.sleep(1.0)

def start_ws(symbols: List[str], use_testnet: bool):
    """啟動/更新訂閱；重複呼叫會更新 _SUBS 並重啟 thread。"""
    global _WS_THREAD, _WS_STOP, _SUBS
    syms = [s.upper() for s in symbols]
    if syms == _SUBS and _WS_THREAD and _WS_THREAD.is_alive():
        return
    _SUBS = syms
    stop_ws()
    _WS_STOP = False
    def _t():
        asyncio.run(_run_ws(_SUBS, use_testnet))
    _WS_THREAD = threading.Thread(target=_t, daemon=True)
    _WS_THREAD.start()

def stop_ws():
    global _WS_THREAD, _WS_STOP
    _WS_STOP = True
    if _WS_THREAD and _WS_THREAD.is_alive():
        try:
            _WS_THREAD.join(timeout=0.5)
        except Exception:
            pass
    _WS_THREAD = None
