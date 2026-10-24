# V1.1
import yaml, datetime as dt
from kalshi_client import KalshiClient
from strategy import current_window, _fetch_candidate_markets, _match_market_to_window

cfg = yaml.safe_load(open("config.yaml"))
c = KalshiClient(cfg["kalshi"]["key_id"], cfg["kalshi"]["private_key_path"], cfg["kalshi"]["base_url"])
w = current_window()
cands = _fetch_candidate_markets(c, cfg["market"]["series_ticker"])
print("window close:", w.close_time.isoformat(), "candidates:", len(cands))
for m in sorted(cands, key=lambda m: m.get("close_time") or "")[-6:]:
    print(m.get("ticker"), m.get("status"), m.get("close_time"),
          {k: v for k, v in m.items() if "strike" in k.lower()})
m = _match_market_to_window(cands, w)
print("matched:", m.get("ticker") if m else None)
cands2 = _fetch_candidate_markets(c, cfg["market"]["series_ticker"], around=[w])
m2 = _match_market_to_window(cands2, w)
print("filtered:", len(cands2), "matched:", m2.get("ticker") if m2 else None,
      {k: v for k, v in (m2 or {}).items() if "strike" in k.lower()})