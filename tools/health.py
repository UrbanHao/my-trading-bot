import re
from pathlib import Path

def ok(p,s): return "✅" if re.search(p,s,re.S) else "❌"

CHECKS = {
 "config.py":[
  ("USE_WEBSOCKET", r"\bUSE_WEBSOCKET\s*=\s*(True|False)"),
  ("USE_TESTNET",   r"\bUSE_TESTNET\s*=\s*(True|False)"),
 ],
 "ws_client.py":[
  ("start_ws()", r"\bdef\s+start_ws\("),
  ("ws_best_price()", r"\bdef\s+ws_best_price\("),
 ],
 "utils.py":[
  ("SESSION", r"\bSESSION\s*=\s*requests\.Session\(\)"),
  ("Retry/HTTPAdapter import", r"from urllib3\.util\.retry import Retry.*from requests\.adapters import HTTPAdapter|from requests\.adapters import HTTPAdapter.*from urllib3\.util\.retry import Retry"),
  ("Cache-Control: no-cache", r'Cache-Control["\']:\s*["\']no-cache'),
  ("safe_get_json", r"\bdef\s+safe_get_json\("),
 ],
 "adapters.py":[
  ("SimAdapter", r"class\s+SimAdapter"),
  ("LiveAdapter", r"class\s+LiveAdapter"),
  ("best_price(ws→REST) Sim", r"class\s+SimAdapter.*?def\s+best_price\(.*?ws_best_price\(.*?return",),
  ("best_price(ws→REST) Live", r"class\s+LiveAdapter.*?def\s+best_price\(.*?ws_best_price\(.*?return",),
  ("balance_usdt(Live)", r"class\s+LiveAdapter.*?def\s+balance_usdt\(self\)"),
 ],
 "panel.py":[
  ("import ws_best_price", r"from\s+utils\s+import\s+ws_best_price"),
  ("_fmt_last()", r"\bdef\s+_fmt_last\(symbol:\s*str,\s*last_val\)"),
  ("Top10 uses _fmt_last", r"_fmt_last\(s,\s*last\)"),
  ("render_layout(account=)", r"def\s+render_layout\(top10,\s*day_state,\s*position,\s*events,\s*account=None\)"),
  ("Status shows equity/balance", r"Equity:|Balance:|\[TESTNET\]"),
 ],
 "main.py":[
  ("live_render(state_iter())", r"live_render\(state_iter\(\)\)"),
  ("subscribe WS after scan", r'log\("top10 ok".*?\)\s*\n\s*if\s+USE_WEBSOCKET:\s*\n\s*syms\s*=\s*\[t\[0\]\s+for\s+t\s+in\s+top10\]\s*\n\s*start_ws\(syms,\s*USE_TESTNET\)'),
  ("account passed to panel", r'"account"\s*:\s*account'),
  ("hotkeys thread", r"threading\.Thread\(target=_keyloop,\s*daemon=True\)"),
  ("poll_and_close_if_hit try/except", r'if\s+adapter\.has_open\(\):\s*\n\s*try:\s*\n\s*closed,\s*pct,\s*sym\s*=\s*adapter\.poll_and_close_if_hit\(day\)\s*\n\s*except\s+Exception\s+as\s+e:'),
  ("cooldown/symbol_lock", r'cooldown\["until"\]|cooldown\["symbol_lock"\]'),
 ],
}

for f, items in CHECKS.items():
    try:
        s = Path(f).read_text(encoding='utf-8')
        print(f"\n=== {f} ===")
        for title, pat in items:
            print(ok(pat,s), title)
    except FileNotFoundError:
        print(f"\n=== {f} ===\n❌ file not found")
