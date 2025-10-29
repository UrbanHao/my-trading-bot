import csv, os, time

HEAD = ["ts","symbol","side","qty","entry","exit","ret_pct","reason"]
PATH = "journal.csv"

def _ensure_file():
    if not os.path.exists(PATH):
        with open(PATH, "w", newline="") as f:
            csv.writer(f).writerow(HEAD)

def log_trade(symbol:str, side:str, qty:float, entry:float, exit_price:float, ret_pct:float, reason:str):
    _ensure_file()
    with open(PATH, "a", newline="") as f:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        row = [ts, symbol, side, f"{qty:.6g}", f"{entry:.10g}", f"{exit_price:.10g}", f"{ret_pct*100:.4f}", reason]
        csv.writer(f).writerow(row)
