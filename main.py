#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime
import time, os ,config

from dotenv import load_dotenv

from config import (USE_WEBSOCKET, USE_TESTNET, USE_LIVE, SCAN_INTERVAL_S, DAILY_TARGET_PCT, DAILY_LOSS_CAP,
                    PER_TRADE_RISK, ENABLE_LONG, ENABLE_SHORT) # <-- 在最後加上
from utils import (fetch_top_gainers,fetch_top_losers, SESSION, load_exchange_info,EXCHANGE_INFO, update_time_offset, conform_to_filters)
from risk_frame import DayGuard, position_size_notional, compute_bracket, PositionClock
from adapters import SimAdapter, LiveAdapter
from signal_volume_breakout import volume_breakout_ok
from signal_volume_breakdown import volume_breakdown_ok
from panel import live_render
from ws_client import start_ws, stop_ws
import threading
from journal import log_trade
import sys, threading, termios, tty, select, math
import requests
from utils import (
    fetch_top_gainers_fut, fetch_top_losers_fut,
    is_futures_symbol, load_exchange_info
)
from signals.signal_scalp_breakout import scalp_breakout_signal
from signals.signal_scalp_vwap import scalp_vwap_signal
load_exchange_info(force_refresh=True)

def state_iter():
    # hotkeys local imports (ensure available even if top-level imports failed)
    import sys, threading, termios, tty, select  # hotkeys
    from datetime import datetime

    # === 讀取 Scalp 設定（不改 config.py 也能用環境變數覆蓋） ===
    SCALP_MODE = (os.getenv("SCALP_MODE") or getattr(config, "SCALP_MODE", "") or "").lower()  # "", "breakout", "vwap"
    TIME_STOP_SEC = int(os.getenv("TIME_STOP_SEC", str(getattr(config, "TIME_STOP_SEC", 180))))  # 預設 180 秒
    COOLDOWN_SEC = 3          # 平倉後全域冷卻
    REENTRY_BLOCK_SEC = 45    # 同標的平倉後禁止再次進場秒數

    load_dotenv(override=True)
    load_exchange_info()
    day = DayGuard()
    adapter = LiveAdapter() if USE_LIVE else SimAdapter()

    # --- 從 API 取得權益（Live）或用 .env（Sim） ---
    if USE_LIVE:
        try:
            equity = adapter.balance_usdt()
            print(f"--- 成功獲取初始餘額: {equity:.2f} USDT ---")
        except Exception as e:
            print(f"--- 致命錯誤：無法獲取初始餘額: {e} ---")
            print("請檢查 API Key 權限或 .env 設定。程式即將退出。")
            sys.exit(1)
    else:
        equity = float(os.getenv("EQUITY_USDT", "10000"))  # 模擬模式
        print(f"--- 模擬 (SIM) 模式啟動，初始權益: {equity:.2f} USDT ---")

    start_equity = equity
    last_scan = 0.0
    last_time_sync = time.time()
    last_bal_ts = 0.0

    account = {"equity": equity, "balance": None, "testnet": USE_TESTNET}
    paused = {"scan": False}

    top10 = []
    top10_losers = []
    events = []
    position_view = None

    # ---- 冷卻 / 重入鎖 ----
    cooldown = {"until": 0.0, "symbol_lock": {}}
    open_ts = None  # 開倉時間（for time-stop）

    def log(msg, tag="SYS"):
        ts = datetime.now().strftime("%H:%M:%S")
        events.append((ts, f"{tag}: {msg}"))

    # --- 非阻塞鍵盤監聽（p: 暫停/恢復掃描, x: 立即平倉, !: 今日停機） ---
    def _keyloop():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r:
                    ch = sys.stdin.read(1)
                    if ch == "p":
                        paused["scan"] = not paused["scan"]
                        log(f"toggle pause -> {paused['scan']}", "KEY")
                    elif ch == "x":
                        if adapter.has_open():
                            try:
                                sym = adapter.open["symbol"]
                                entry = float(adapter.open["entry"])
                                side = adapter.open["side"]
                                nowp = adapter.best_price(sym)
                                pct = (nowp - entry) / entry if side == "LONG" else (entry - nowp) / entry
                                log_trade(sym, side, adapter.open.get("qty", 0), entry, nowp, pct, "hotkey_x")
                                day.on_trade_close(pct)
                                adapter.open = None
                                log("force close position", "KEY")
                            except Exception as e:
                                log(f"close error: {e}", "KEY")
                        else:
                            log("no position to close", "KEY")
                    elif ch == "!":
                        day.state.halted = True
                        log("manual HALT for today", "KEY")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    threading.Thread(target=_keyloop, daemon=True).start()

    # === 主回圈 ===
    while True:
        t_now = time.time()
        day.rollover()

        # --- 每 30 分鐘校時一次 ---
        if t_now - last_time_sync > 1800:
            try:
                new_offset = update_time_offset()
                log(f"Time offset re-synced: {new_offset} ms", "SYS")
            except Exception as e:
                log(f"Time offset sync failed: {e}", "ERROR")
            finally:
                last_time_sync = t_now

        # ========== 1) 有持倉：監控 TP/SL 與 Time-Stop ==========
        if adapter.has_open():
            # 保留資料給日誌
            trade_data_copy = adapter.open.copy()

            # 1a) 先檢查 TP/SL 是否命中
            try:
                closed, pct, sym, reason, exit_price = adapter.poll_and_close_if_hit(day)
            except Exception as e:
                log(f"poll error: {e}")
                closed, pct, sym, reason, exit_price = (False, None, None, None, None)

            if closed:
                log(f"CLOSE {sym} ({reason}) pct={pct*100:.2f}% day={day.state.pnl_pct*100:.2f}%")
                # 記帳
                try:
                    log_trade(
                        symbol=sym,
                        side=trade_data_copy["side"],
                        qty=trade_data_copy["qty"],
                        entry=trade_data_copy["entry"],
                        exit_price=exit_price,
                        ret_pct=pct,
                        reason=reason
                    )
                except Exception as e:
                    log(f"Journal log_trade failed: {e}", "ERROR")

                cooldown["until"] = time.time() + COOLDOWN_SEC
                last_bal_ts = 0.0
                open_ts = None

                # 更新權益
                try:
                    if USE_LIVE:
                        equity = adapter.balance_usdt()
                        account["balance"] = equity
                        log(f"Balance updated: {equity:.2f}", "SYS")
                    else:
                        equity = start_equity * (1.0 + day.state.pnl_pct)
                except Exception as e:
                    log(f"Balance update failed: {e}", "SYS")
                    equity = start_equity * (1.0 + day.state.pnl_pct)

                position_view = None

            else:
                # 1b) 時間停損（僅在 Scalp 模式生效）
                if SCALP_MODE and (open_ts is not None) and ((t_now - open_ts) >= TIME_STOP_SEC):
                    try:
                        sym = adapter.open["symbol"]
                        entry = float(adapter.open["entry"])
                        side = adapter.open["side"]
                        nowp = adapter.best_price(sym)
                        pct = (nowp - entry) / entry if side == "LONG" else (entry - nowp) / entry
                        log_trade(sym, side, adapter.open.get("qty", 0), entry, nowp, pct, "time-stop")
                        day.on_trade_close(pct)
                        adapter.open = None
                        log(f"time-stop close {sym}", "SYS")
                        cooldown["until"] = time.time() + COOLDOWN_SEC
                        open_ts = None

                        # 更新權益顯示
                        if USE_LIVE:
                            try:
                                equity = adapter.balance_usdt()
                                account["balance"] = equity
                            except Exception:
                                pass
                        else:
                            equity = start_equity * (1.0 + day.state.pnl_pct)
                    except Exception as e:
                        log(f"time-stop error: {e}", "ERROR")

        # ========== 2) 無持倉：掃描與找入場 ==========
        else:
            if not day.state.halted and not paused["scan"] and (t_now > last_scan + SCAN_INTERVAL_S):
                try:
                    ws_syms = []

                    # 2a) 抓取 Gainers / Losers（遵守 ENABLE_LONG / ENABLE_SHORT）
                    if ENABLE_LONG:
                        top10 = fetch_top_gainers(10)          # 面板顯示照舊
                        ws_syms.extend([t[0] for t in top10])
                        log("top10_gainers ok", "SCAN")
                    else:
                        top10 = []

                    if ENABLE_SHORT:
                        top10_losers = fetch_top_losers(10)    # 面板顯示照舊
                        ws_syms.extend([t[0] for t in top10_losers])
                        log("top10_losers ok", "SCAN")
                    else:
                        top10_losers = []

                    # 另取候選（僅期貨可交易）
                    gainers_fut = fetch_top_gainers_fut(20)    # 抓寬一點，讓策略好挑
                    losers_fut  = fetch_top_losers_fut(20)

                    last_scan = t_now

                    # 2b) 找候選
                    candidate = None
                    side = None
                    reason = ""

                    # === 路由：Scalp 模式 ===
                    if SCALP_MODE in ("breakout", "vwap"):
                        # 下單 universe 只用「期貨版」清單；面板仍顯示原始 top10
                        universe = gainers_fut + losers_fut
                        for s, pct, last, vol in universe:
                            if t_now < cooldown['symbol_lock'].get(s, 0):
                                continue
                            if t_now < cooldown['until']:
                                continue

                            if SCALP_MODE == "breakout":
                                sig = scalp_breakout_signal(s, timeframe="1m")
                            else:
                                sig = scalp_vwap_signal(s, timeframe="1m")

                            if sig.ok:
                                candidate = (s, sig.entry)
                                side = sig.side
                                reason = sig.reason
                                break

                    # === 路由：原本的 volume 策略（預設） ===
                    else:
                        if ENABLE_LONG and (t_now > cooldown["until"]):
                            for s, pct, last, vol in gainers_fut:
                                if t_now < cooldown['symbol_lock'].get(s, 0):
                                    continue
                                if volume_breakout_ok(s):
                                    candidate = (s, last)
                                    side = "LONG"
                                    reason = "volume-breakout"
                                    break

                        if (not candidate) and ENABLE_SHORT and (t_now > cooldown["until"]):
                            for s, pct, last, vol in losers_fut:
                                if t_now < cooldown['symbol_lock'].get(s, 0):
                                    continue
                                if volume_breakdown_ok(s):
                                    candidate = (s, last)
                                    side = "SHORT"
                                    reason = "volume-breakdown"
                                    break

                    # 2c) 啟動/更新 WS（若開啟）
                    if USE_WEBSOCKET and ws_syms:
                        start_ws(list(set(ws_syms)), USE_TESTNET)

                except Exception as e:
                    log(f"scan error: {e}", "SCAN")
                    candidate = None


                # 2d) 執行下單（共用原本下單流程與風控）
                if candidate:
                    symbol, entry = candidate
                    # 最終保險：僅允許交易所期貨清單內的符號
                    if not is_futures_symbol(symbol):
                        log(f"Skipping {symbol}: not in futures exchangeInfo", "SCAN")
                        cooldown["symbol_lock"][symbol] = time.time() + 60
                        continue

                    # 'side' 變數已在上面的掃描邏輯中設定 (LONG 或 SHORT)
                    notional = position_size_notional(equity)


                    try:
                        # 計算 TP/SL、對齊交易所規則
                        sl_raw, tp_raw = compute_bracket(entry, side)
                        qty_raw = notional / max(entry, 1e-9)

                        entry_f, qty_f, price_prec, qty_prec = conform_to_filters(symbol, entry, qty_raw)
                        sl_f, _, _, _ = conform_to_filters(symbol, sl_raw, qty_raw)
                        tp_f, _, _, _ = conform_to_filters(symbol, tp_raw, qty_raw)

                    except ValueError as e:
                        log(f"Skipping {symbol}: {e}", "ERR")
                        cooldown["symbol_lock"][symbol] = time.time() + 60
                        candidate = None
                    except Exception as e:
                        log(f"Filter/Conform error for {symbol}: {e}", "ERR")
                        candidate = None

                    if candidate:
                        if qty_f == 0.0:
                            log(f"Skipping {symbol}, calculated qty is zero (Notional={notional:.2f})", "SYS")
                        else:
                            entry_s = f"{entry_f:.{price_prec}f}"
                            qty_s   = f"{qty_f:.{qty_prec}f}"
                            sl_s    = f"{sl_f:.{price_prec}f}"
                            tp_s    = f"{tp_f:.{price_prec}f}"

                            try:
                                # === 🧱 新增：入場前防呆檢查，避免立即觸發 (-2021) ===
                                try:
                                    mp = adapter._get("/fapi/v1/premiumIndex", {"symbol": symbol})
                                    mark_price = float(mp["markPrice"])
                                except Exception:
                                    r = SESSION.get(f"{adapter.base}/fapi/v1/ticker/price",
                                                    params={"symbol": symbol}, timeout=5)
                                    r.raise_for_status()
                                    mark_price = float(r.json()["price"])

                                will_immediately_trigger = (
                                    (side.upper() == "LONG" and entry_f <= mark_price)
                                    or (side.upper() == "SHORT" and entry_f >= mark_price)
                                )
                                if will_immediately_trigger:
                                    log(f"Skip {symbol}: entry would immediately trigger (entry={entry_f}, mark={mark_price})", "SCAN")
                                    cooldown["symbol_lock"][symbol] = time.time() + 60
                                    candidate = None
                                    continue  # <== 跳過這筆下單

                                # === 原本的下單邏輯從這裡繼續 ===
                                cooldown["symbol_lock"][symbol] = time.time() + 10  # 先鎖10秒，避免爆衝
                                try:
                                    # 若 adapter 有內建清掛單功能，先清掉舊掛單
                                    if hasattr(adapter, "cancel_open_orders"):
                                        adapter.cancel_open_orders(symbol)
                                except Exception:
                                    pass

                                adapter.place_bracket(symbol, side, qty_s, entry_s, sl_s, tp_s)
                                position_view = {"symbol": symbol, "side": side,
                                                 "qty": qty_f, "entry": entry_f,
                                                 "sl": sl_f, "tp": tp_f}
                                log(f"OPEN {symbol} qty={qty_f} entry={entry_f:.6f} | {reason}", "ORDER")

                                # 冷卻與重入鎖
                                cooldown["until"] = time.time() + COOLDOWN_SEC
                                cooldown["symbol_lock"][symbol] = time.time() + REENTRY_BLOCK_SEC
                                open_ts = time.time()  # 記錄開倉時間（給 time-stop 用）

                            except requests.exceptions.HTTPError as e:
                                try:
                                    server_msg = e.response.json()
                                    code = server_msg.get("code")
                                    msg = server_msg.get("msg")
                                    log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                                    log(f"SERVER MSG: {msg} (Code: {code})", "ERROR")
                                    # 🧱 若為 -2021，鎖 180 秒，避免重複下錯單
                                    if code == -2021:
                                        cooldown["symbol_lock"][symbol] = time.time() + 180
                                except Exception:
                                    log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                                    log(f"SERVER MSG: {e.response.text}", "ERROR")
                            except Exception as e:
                                log(f"ORDER FAILED for {symbol}: {e}", "ERROR")


        # 3) 更新顯示用 Equity
        account["equity"] = equity
        if USE_LIVE and account.get("balance") is None:
            account["balance"] = equity

        # 4) 輸出給面板
        yield {
            "top10": top10,
            "top10_losers": top10_losers,
            "day_state": day.state,
            "position": adapter.open if hasattr(adapter, "open") else (None if position_view is None else position_view),
            "events": events,
            "account": account,
        }

        time.sleep(0.8)


if __name__ == "__main__":
    try:
        live_render(state_iter())
    finally:
        try:
            stop_ws()
        except Exception:
            pass
