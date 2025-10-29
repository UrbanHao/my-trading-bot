# Daily Gainer Bot — Version C (Volume Spike + Breakout) with Rich Panel

這是一套「可直接執行」的量化交易骨架：
- 掃描幣安 USDT 永續 24h **漲幅 Top10**
- 訊號：**5m 量能放大 + 健康突破（未超伸）+ EMA 結構過濾**
- 風控：**日停利 +1.5% / 日停損 -2%**，單筆 **TP +1.5% / SL -0.75%（R:R=2）**
- 交易：**單線程、逐筆**；同時只允許 1 筆
- 面板：**Rich** 主控台顯示 Top10、日損益、持倉與事件日志
- 運行模式：預設 **模擬**（無需 API Key）；你可切換 **實盤**（自行補上 OCO 下單）

> 先以模擬模式驗證；確定 OK 再接實盤。

---

## 快速開始

```bash
# 建議使用虛擬環境
pip install -r requirements.txt

# 直接模擬運行（無需 API Key）
python main.py

# （可選）建立 .env 後，接實盤：
# 1) 編輯 .env，填入 BINANCE_API_KEY / BINANCE_SECRET
# 2) 在 config.py 把 USE_LIVE 改 True
```

---

## 檔案結構

```
daily_gainer_vC/
├─ main.py                       # 入口：迴圈 + 面板刷新
├─ config.py                     # 可調參數（風控/訊號/掃描）
├─ risk_frame.py                 # 日守門員 + 部位 sizing + bracket 計算
├─ adapters.py                   # SimAdapter（可跑）/ LiveAdapter（留介面）
├─ signal_volume_breakout.py     # 訊號（版本 C：量價突破合成）
├─ panel.py                      # Rich 面板（Top10/持倉/日PnL/事件）
├─ utils.py                      # Binance API 小工具、EMA 等
├─ requirements.txt
├─ .env.sample                   # 參考：實盤需要的環境變數
└─ README.md
```

---

## 主要參數（`config.py`）

- `DAILY_TARGET_PCT = 0.015`：日停利 +1.5%
- `DAILY_LOSS_CAP   = -0.02`：日停損 -2%
- `TP_PCT = 0.015` / `SL_PCT = 0.0075`：單筆 R:R=2
- `SCAN_INTERVAL_S = 25`：Top10 刷新頻率
- `USE_LIVE = False`：預設模擬；接實盤改 True
- 訊號參數（版本 C）：`KLINE_INTERVAL="5m"`, `HH_N=96`, `OVEREXTEND_CAP=0.02`, `VOL_SPIKE_K=2.0` 等

---

## 面板說明

面板提供四塊：
1. **Top10**：當前 24h 漲幅前十名幣種與漲幅
2. **Status**：日損益累計 / 今日筆數 / 是否停機
3. **Position**：當前持倉、進場價、TP/SL 等
4. **Events**：策略事件日志（開倉/平倉/掃描錯誤等）

---

## 實盤注意

- 幣安永續合約 API 並無「現貨式 OCO」端點，常見做法為：
  - 進場單 + 兩條互斥條件單（TP/SL），成交一邊即撤另一邊
- 本專案提供 `LiveAdapter` 介面，你需要把 `place_bracket` 與 `poll_and_close_if_hit` 換成你現有流程
- **永遠用小倉測試**，確認成交/撤單/狀態一致性無誤

---

## 授權

MIT