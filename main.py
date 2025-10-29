#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime
import time, os

from dotenv import load_dotenv

from config import (USE_WEBSOCKET, USE_TESTNET, USE_LIVE, SCAN_INTERVAL_S, DAILY_TARGET_PCT, DAILY_LOSS_CAP,
                    PER_TRADE_RISK, ENABLE_LONG, ENABLE_SHORT) # <-- 在最後加上
from utils import (fetch_top_gainers,fetch_top_losers, SESSION, load_exchange_info,EXCHANGE_INFO, update_time_offset, conform_to_filters)
from risk_frame import DayGuard, position_size_notional, compute_bracket
from adapters import SimAdapter, LiveAdapter
from signal_volume_breakout import volume_breakout_ok
from signal_volume_breakdown import volume_breakdown_ok
from panel import live_render
from ws_client import start_ws, stop_ws
import threading
from journal import log_trade
import sys, threading, termios, tty, select, math
import requests


def state_iter():
    # hotkeys local imports (ensure available even if top-level imports failed)
    import sys, threading, termios, tty, select  # hotkeys

    load_dotenv(override=True)
    load_exchange_info()
    day = DayGuard()
    adapter = LiveAdapter() if USE_LIVE else SimAdapter()

    # --- 修正：從 API 獲取真實餘額，而不是 .env ---
    if USE_LIVE:
        try:
            equity = adapter.balance_usdt()
            print(f"--- 成功獲取初始餘額: {equity:.2f} USDT ---")
        except Exception as e:
            print(f"--- 致命錯誤：無法獲取初始餘額: {e} ---")
            print("請檢查 API Key 權限或 .env 設定。程式即將退出。")
            sys.exit(1) # 退出程式
    else:
        equity = float(os.getenv("EQUITY_USDT", "10000")) # 模擬模式
        print(f"--- 模擬 (SIM) 模式啟動，初始權益: {equity:.2f} USDT ---")
        
    start_equity = equity
    last_scan = 0
    last_time_sync = time.time() # <-- 新增：記錄啟動時間
    prev_syms = []
    last_bal_ts = 0.0
    account = {"equity": equity, "balance": None, "testnet": USE_TESTNET}
    paused = {"scan": False}
    top10 = []
    top10_losers = [] # <--- 新增
    events = []
    position_view = None
    # ---- anti-churn / re-entry guard ----
    COOLDOWN_SEC = 3             # 平倉後全域冷卻，避免下一輪又馬上下單
    REENTRY_BLOCK_SEC = 45       # 同一幣種平倉後禁止再次進場秒數
    cooldown = {"until": 0.0, "symbol_lock": {}}

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
                r,_,_ = select.select([sys.stdin],[],[],0.05)
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
                                pct = (nowp-entry)/entry if side=="LONG" else (entry-nowp)/entry
                                log_trade(sym, side, adapter.open.get("qty",0), entry, nowp, pct, "hotkey_x")
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


    while True:
        t_now = time.time() # 取得一次當前時間
        day.rollover()

        # --- 新增：修復時間漂移 (Issue #2) ---
        if t_now - last_time_sync > 1800: # 每 30 分鐘 (1800 秒)
            try:
                new_offset = update_time_offset()
                log(f"Time offset re-synced: {new_offset} ms", "SYS")
                last_time_sync = t_now
            except Exception as e:
                log(f"Time offset sync failed: {e}", "ERROR")
                last_time_sync = t_now # 即使失敗也更新時間，避免 0.8s 後重試
        # --- 結束 ---

        # 1) 平倉監控
        if adapter.has_open():
            try:
                closed, pct, sym = adapter.poll_and_close_if_hit(day)
            except Exception as e:
                log(f"poll error: {e}")
                closed, pct, sym = False, None, None

            if closed:
                log(f"CLOSE {sym} pct={pct*100:.2f}% day={day.state.pnl_pct*100:.2f}%")
                # 冷卻避免馬上下單、並讓下一輪立即抓 balance
                cooldown["until"] = time.time() + COOLDOWN_SEC
                last_bal_ts = 0.0

                # --- 修正：平倉後立即更新權益 (equity) ---
                try:
                    if USE_LIVE:
                        equity = adapter.balance_usdt() # 更新用於下單的 equity
                        account["balance"] = equity    # 更新用於顯示的 balance
                        log(f"Balance updated: {equity:.2f}", "SYS")
                    else:
                        # 模擬模式：用 PnL 計算
                        equity = start_equity * (1.0 + day.state.pnl_pct)
                except Exception as e:
                    log(f"Balance update failed: {e}", "SYS")
                    equity = start_equity * (1.0 + day.state.pnl_pct) # 失敗時回退
                # --- 修正結束 ---
                
                position_view = None
        else:
            # 2) 無持倉：若未停機，掃描與找入場
            
            # main.py (新邏輯)
            # main.py (修改後的掃描邏輯)
            if not day.state.halted and not paused["scan"] and (t_now > last_scan + SCAN_INTERVAL_S):
                
                # --- 1. 更新掃描 (共用 top10 變數) ---
                try:
                    ws_syms = [] # <--- 移到 try 的頂部
                    
                    # --- 2. 抓取 Gainers ---
                    if ENABLE_LONG:
                        top10 = fetch_top_gainers(10)
                        last_scan = t_now # <--- last_scan 移到這裡
                        log("top10_gainers ok", "SCAN")
                        ws_syms.extend([t[0] for t in top10]) # 加入 WS 訂閱
                    else:
                        top10 = [] # 如果沒啟用 LONG，清空

                    # --- 3. 抓取 Losers ---
                    if ENABLE_SHORT:
                        # (注意：我們把 top10_losers 的抓取移出 if not candidate)
                        top10_losers = fetch_top_losers(10)
                        log("top10_losers ok", "SCAN")
                        ws_syms.extend([t[0] for t in top10_losers]) # <--- 新增
                    else:
                        top10_losers = [] # 如果沒啟用 SHORT，清空

                    last_scan = t_now # <--- 確保 last_scan 總是被更新

                    # --- 4. 找候選 (LONG) ---
                    candidate = None
                    side = None
                    if ENABLE_LONG and (t_now > cooldown["until"]):
                        for s, pct, last, vol in top10:
                            if t_now < cooldown['symbol_lock'].get(s, 0): continue
                            if volume_breakout_ok(s):
                                candidate = (s, last)
                                side = "LONG"
                                break

                    # --- 5. 找候選 (SHORT) ---
                    if not candidate and ENABLE_SHORT and (t_now > cooldown["until"]):
                        for s, pct, last, vol in top10_losers: # (現在使用已抓取的 top10_losers)
                            if t_now < cooldown['symbol_lock'].get(s, 0): continue
                            if volume_breakdown_ok(s):
                                candidate = (s, last)
                                side = "SHORT"
                                break

                    # --- 6. 更新 WebSocket ---
                    if USE_WEBSOCKET:
                        start_ws(list(set(ws_syms)), USE_TESTNET) # (使用 set 避免重複訂閱)

                except Exception as e:
                    log(f"scan error: {e}", "SCAN")
                    candidate = None

                # --- 5. 執行下單 (邏輯不變，但 side 是動態的) ---
                if candidate:
                    symbol, entry = candidate
                    # 'side' 變數已在上面的掃描邏輯中設定 (LONG 或 SHORT)
                    notional = position_size_notional(equity)

                    # main.py (修改後的版本)
                    try:
                        # 1. 計算「原始」止盈止損
                        sl_raw, tp_raw = compute_bracket(entry, side)
                        qty_raw = notional / entry
                        
                        # 2. 呼叫新工具，一次性完成「刷新、對齊、格式化」
                        # conform_to_filters 會處理新幣種 (B2USDT) 的刷新或拋錯
                        entry_f, qty_f, price_prec, qty_prec = conform_to_filters(symbol, entry, qty_raw)
                        sl_f, _, _, _ = conform_to_filters(symbol, sl_raw, qty_raw)
                        tp_f, _, _, _ = conform_to_filters(symbol, tp_raw, qty_raw)

                    except ValueError as e:
                        # 捕捉 conform_to_filters 拋出的錯誤 (例如 B2USDT 刷新後仍找不到)
                        log(f"Skipping {symbol}: {e}", "ERR")
                        cooldown["symbol_lock"][symbol] = time.time() + 60 # 鎖定 60 秒
                        continue # 安全跳過
                    except Exception as e:
                        log(f"Filter/Conform error for {symbol}: {e}", "ERR")
                        continue # 其他錯誤也跳過

                    # 3. 最終檢查 (避免 0 數量)
                    if qty_f == 0.0:
                        log(f"Skipping {symbol}, calculated qty is zero (Notional={notional:.2f})", "SYS")
                        continue
                    
                    # 4. 格式化字串 (傳給 adapter)
                    # (我們使用 _f 結尾的變數，代表 "Formatted")
                    entry_s = f"{entry_f:.{price_prec}f}"
                    qty_s   = f"{qty_f:.{qty_prec}f}"
                    sl_s    = f"{sl_f:.{price_prec}f}"
                    tp_s    = f"{tp_f:.{price_prec}f}"
                    
                    # --- 修正結束 ---

                    try:
                        # --- 修復：捕捉下單時的所有錯誤 (Issue #1) ---
                        # (注意：我們傳入的是格式化後的字串)
                        adapter.place_bracket(symbol, side, qty_s, entry_s, sl_s, tp_s)
                        
                        # 只有在下單成功時，才執行以下動作
                        # (日誌和 view 仍然使用 "float" 變數，更易讀)
                        position_view = {"symbol":symbol, "side":side, "qty":qty_f, "entry":entry_f, "sl":sl_f, "tp":tp_f}
                        log(f"OPEN {symbol} qty={qty_f} entry={entry_f:.6f}", "ORDER")
                        cooldown["until"] = time.time() + COOLDOWN_SEC
                        cooldown["symbol_lock"][symbol] = time.time() + REENTRY_BLOCK_SEC

                    except requests.exceptions.HTTPError as e:
                    # 專門捕捉 HTTP 錯誤 (例如 400 Bad Request)
                        try:
                            # 嘗試獲取幣安伺服器回傳的 JSON 錯誤訊息
                            server_msg = e.response.json()
                            log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                            log(f"SERVER MSG: {server_msg.get('msg')} (Code: {server_msg.get('code')})", "ERROR")
                        except:
                            # 如果回傳的不是 JSON，就印出原始文字
                            log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                            log(f"SERVER MSG: {e.response.text}", "ERROR")
                        pass # 繼續下一輪

                    except Exception as e:
                        # 捕捉其他所有錯誤 (例如 TimeoutError)
                        log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                        pass # 繼續下一輪

        # 依照可用資訊更新 Equity (顯示用)
        # (實際的 equity 變數已在啟動時和平倉後更新)
        account["equity"] = equity
        if USE_LIVE and account.get("balance") is None: # 處理第一次啟動時
            account["balance"] = equity

        # 3) 輸出給面板
        yield {
            "top10": top10,
            "top10_losers": top10_losers, # <--- 新增
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
