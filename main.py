#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime
import time, os ,config

from dotenv import load_dotenv

from config import (USE_WEBSOCKET, USE_TESTNET, USE_LIVE, SCAN_INTERVAL_S, DAILY_TARGET_PCT, DAILY_LOSS_CAP,
                    PER_TRADE_RISK, ENABLE_LONG, ENABLE_SHORT,
                    SCALP_MODE, MAKER_ENTRY) # <-- 在最後加上
                    
                    
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
import logging
logger = logging.getLogger("bot")
load_exchange_info(force_refresh=True)

def state_iter():
    # hotkeys local imports (ensure available even if top-level imports failed)
    import sys, threading, termios, tty, select  # hotkeys
    from datetime import datetime
    # 這段放在 state_iter() 內，取代原本的 ui_log() 函式
    def ui_log(msg, tag="SYS"):
        ts = datetime.now().strftime("%H:%M:%S")
        events.append((ts, f"{tag}: {msg}"))
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

    def ui_log(msg, tag="SYS"):
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
                        ui_log(f"toggle pause -> {paused['scan']}", "KEY")
                    elif ch == "x":
                        if adapter.has_open():
                            try:
                                sym = adapter.open["symbol"]
                                entry = float(adapter.open["entry"])
                                side = adapter.open["side"]
                                nowp = adapter.best_price(sym)
                                pct = (nowp - entry) / entry if side == "LONG" else (entry - nowp) / entry
                                log_trade(sym, side, adapter.open.get("qty", 0), entry, nowp, pct, "hotkey_x", mode=SCALP_MODE or "MANUAL")
                                day.on_trade_close(pct)
                                adapter.open = None
                                ui_log("force close position", "KEY")
                            except Exception as e:
                                ui_log(f"close error: {e}", "KEY")
                        else:
                            ui_log("no position to close", "KEY")
                    elif ch == "!":
                        day.state.halted = True
                        ui_log("manual HALT for today", "KEY")
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
                ui_log(f"Time offset re-synced: {new_offset} ms", "SYS")
            except Exception as e:
                ui_log(f"Time offset sync failed: {e}", "ERROR")
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
                ui_log(f"poll error: {e}")
                closed, pct, sym, reason, exit_price = (False, None, None, None, None)

            if closed:
                ui_log(f"CLOSE {sym} ({reason}) pct={pct*100:.2f}% day={day.state.pnl_pct*100:.2f}%")
                # 記帳
                try:
                    log_trade(
                        symbol=sym,
                        side=trade_data_copy["side"],
                        qty=trade_data_copy["qty"],
                        entry=trade_data_copy["entry"],
                        exit_price=exit_price,
                        ret_pct=pct,
                        reason=reason,
                        mode=SCALP_MODE or "VOL" # 如果不是 Scalp 模式，就當作是 VOL 策略
                    )
                except Exception as e:
                    ui_log(f"Journal log_trade failed: {e}", "ERROR")

                cooldown["until"] = time.time() + COOLDOWN_SEC
                last_bal_ts = 0.0
                open_ts = None

                # 更新權益
                try:
                    if USE_LIVE:
                        equity = adapter.balance_usdt()
                        account["balance"] = equity
                        ui_log(f"Balance updated: {equity:.2f}", "SYS")
                    else:
                        equity = start_equity * (1.0 + day.state.pnl_pct)
                except Exception as e:
                    ui_log(f"Balance update failed: {e}", "SYS")
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
                        log_trade(sym, side, adapter.open.get("qty", 0), entry, nowp, pct, "time-stop", mode=SCALP_MODE)
                        day.on_trade_close(pct)
                        adapter.open = None
                        ui_log(f"time-stop close {sym}", "SYS")
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
                        ui_log(f"time-stop error: {e}", "ERROR")

        # ========== 2) 無持倉：掃描與找入場 ==========
        else:
            if not day.state.halted and not paused["scan"] and (t_now > last_scan + SCAN_INTERVAL_S):
                try:
                    ws_syms = []

                    # 2a) 抓取 Gainers / Losers（遵守 ENABLE_LONG / ENABLE_SHORT）
                    if ENABLE_LONG:
                        top10 = fetch_top_gainers(10)          # 面板顯示照舊
                        ws_syms.extend([t[0] for t in top10])
                        ui_log("top10_gainers ok", "SCAN")
                    else:
                        top10 = []

                    if ENABLE_SHORT:
                        top10_losers = fetch_top_losers(10)    # 面板顯示照舊
                        ws_syms.extend([t[0] for t in top10_losers])
                        ui_log("top10_losers ok", "SCAN")
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
                    ui_log(f"scan error: {e}", "SCAN")
                    candidate = None


                # 2d) 執行下單（共用原本下單流程與風控）
                if candidate:
                    symbol, entry = candidate

                    # 避免重複下單同一標的（面板與實際倉位不同步時，不要再打同一標的）
                    if adapter.has_open() and adapter.open and adapter.open.get("symbol") == symbol:
                        ui_log(f"Skipping {symbol}: already have open position", "SYS")
                        continue

                    # 僅允許交易所期貨清單內的符號
                    if not is_futures_symbol(symbol):
                        ui_log(f"Skipping {symbol}: not in futures exchangeInfo", "SCAN")
                        cooldown["symbol_lock"][symbol] = time.time() + 60
                        continue

                    notional = position_size_notional(equity)

                    try:
                        # 計算 TP/SL、對齊交易所規則
                        sl_raw, tp_raw = compute_bracket(entry, side)
                        qty_raw = notional / max(entry, 1e-9)

                        entry_f, qty_f, price_prec, qty_prec = conform_to_filters(symbol, entry, qty_raw)
                        sl_f,    _,     _,          _        = conform_to_filters(symbol, sl_raw, qty_raw)
                        tp_f,    _,     _,          _        = conform_to_filters(symbol, tp_raw, qty_raw)

                    except ValueError as e:
                        ui_log(f"Skipping {symbol}: {e}", "ERR")
                        cooldown["symbol_lock"][symbol] = time.time() + 60
                        candidate = None
                    except Exception as e:
                        ui_log(f"Filter/Conform error for {symbol}: {e}", "ERR")
                        candidate = None

                    if candidate:
                        if qty_f == 0.0:
                            ui_log(f"Skipping {symbol}, calculated qty is zero (Notional={notional:.2f})", "SYS")
                        else:
                            entry_s = f"{entry_f:.{price_prec}f}"
                            qty_s   = f"{qty_f:.{qty_prec}f}"
                            sl_s    = f"{sl_f:.{price_prec}f}"
                            tp_s    = f"{tp_f:.{price_prec}f}"

                            try:
                                # 先鎖一下，避免同一秒重複打單
                                cooldown["symbol_lock"][symbol] = time.time() + 10

                                # 入場前，先清乾淨舊掛單（避免越掛越多）
                                try:
                                    if hasattr(adapter, "cancel_open_orders"):
                                        adapter.cancel_open_orders(symbol)
                                except Exception as e:
                                    logger.warning(f"cancel_open_orders warn: {e}")

                                # 一次性流程（在 adapter 內）：市價進場 + 掛 reduceOnly 的 TP/SL
                                # === [修改] 執行下單路由 ===
                                if SCALP_MODE and MAKER_ENTRY:
                                    # 1. Scalp 模式 + 啟用 Maker：
                                    #    呼叫新的智慧下單 (Maker->Taker)，此函式會回報真實成交價
                                    adapter.place_scalp_bracket(symbol, side, qty_s, entry_s, sl_s, tp_s)
                                    # 狀態由 adapter 內部設定，但我們需要更新面板
                                    # (adapter.open 內有真實成交價，但為求即時顯示，先用 entry_f)
                                    position_view = {"symbol": symbol, "side": side, "qty": qty_f,
                                                     "entry": entry_f, "sl": sl_f, "tp": tp_f}
                                    ui_log(f"OPEN (SCALP) {symbol} qty={qty_f} ref_entry={entry_f:.6f} | {reason}", "ORDER")

                                else:
                                    # 2. 舊策略 (VOL) 或 Scalp (Taker 模式)：
                                    #    呼叫原本的純市價下單
                                    adapter.place_bracket(symbol, side, qty_s, entry_s, sl_s, tp_s)

                                    position_view = {"symbol": symbol, "side": side, "qty": qty_f,
                                                     "entry": entry_f, "sl": sl_f, "tp": tp_f}
                                    ui_log(f"OPEN ({SCALP_MODE or 'VOL'}) {symbol} qty={qty_f} entry={entry_f:.6f} | {reason}", "ORDER")

                                # (刪除你原本的 ui_log)
                                # 冷卻與重入鎖
                                cooldown["until"] = time.time() + COOLDOWN_SEC
                                cooldown["symbol_lock"][symbol] = time.time() + REENTRY_BLOCK_SEC
                                open_ts = time.time()

                            except requests.exceptions.HTTPError as e:
                                try:
                                    server_msg = e.response.json()
                                    code = server_msg.get("code")
                                    msg = server_msg.get("msg")
                                    ui_log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                                    ui_log(f"SERVER MSG: {msg} (Code: {code})", "ERROR")
                                    logger.error(f"order http error {symbol}: {code} {msg}")
                                    # 立即觸發之類 -2021，可延長鎖
                                    if code == -2021:
                                        cooldown["symbol_lock"][symbol] = time.time() + 180
                                except Exception:
                                    ui_log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                                    ui_log(f"SERVER MSG: {e.response.text}", "ERROR")
                                    logger.error(f"order http error raw {symbol}: {e.response.text}")
                            except Exception as e:
                                ui_log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                                logger.error(f"order failed {symbol}: {e}")

        # 3) 更新顯示用 Equity
        account["equity"] = equity
        if USE_LIVE and account.get("balance") is None:
            account["balance"] = equity
            try:
                if hasattr(adapter, "sync_state"):
                    adapter.sync_state()
            except Exception:
                pass
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
