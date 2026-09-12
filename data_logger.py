# V1.0
"""
Continuous live tick data logger.
Runs in a background thread, writing a JSON line every second with:
- CF Benchmarks BTC spot price
- Kalshi current window floor strike (USD)
- Kalshi UP/DOWN prices (cents)
"""
from __future__ import annotations
import datetime as dt
import json
import os
import threading
import time
import logging
from typing import Optional

from kalshi_client import KalshiClient
from strategy import (
    current_window,
    find_market_for_window,
    get_strike_price,
    current_price_cents,
    price_from_orderbook,
)
import spot_price

log = logging.getLogger("data_logger")


def _window_output_path(base_path: str, window_open_time: dt.datetime) -> str:
    """
    Turns a configured base path (e.g. './live_ticks.jsonl') into a path
    specific to one 15-minute window, using that window's own open time
    (converted to local time, matching the 'time' field's convention) -
    e.g. './live_ticks_20260822_1430.jsonl'. Minute precision is enough
    since windows always start on :00/:15/:30/:45.
    """
    base_dir, base_name = os.path.split(base_path)
    stem, ext = os.path.splitext(base_name)
    if not ext:
        ext = ".jsonl"
    local_open = window_open_time.astimezone()
    timestamp = local_open.strftime("%Y%m%d_%H%M")
    window_name = f"{stem}_{timestamp}{ext}"
    return os.path.join(base_dir, window_name) if base_dir else window_name

class LiveTickLogger:
    def __init__(
        self,
        client: KalshiClient,
        series_ticker: str,
        output_file: str = "live_ticks.jsonl",
        interval_sec: float = 1.0,
        new_file_per_session: bool = True,
    ):
        self.client = client
        self.series_ticker = series_ticker
        self._base_output_file = output_file
        # If enabled, a new file is created every time the 15-min window
        # rolls over (checked on every tick in _log_tick, not just once at
        # startup) - so a single long-running bot process still gets one
        # file per session/window instead of one giant file for its whole
        # uptime. Resolved lazily: self.output_file/_current_window start
        # unset and get filled in on the very first tick.
        self.new_file_per_session = new_file_per_session
        self.output_file = output_file if not new_file_per_session else None
        self._current_window = None
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        # daemon=True ensures it dies automatically if the main bot crashes
        self._thread = threading.Thread(target=self._run, daemon=True, name="LiveTickLogger")
        self._thread.start()
        if self.new_file_per_session:
            log.info(
                "LiveTickLogger started -> writing %ss ticks, one file per 15-min window (base: %s)",
                self.interval_sec, self._base_output_file,
            )
        else:
            log.info("LiveTickLogger started -> writing %ss ticks to %s", self.interval_sec, self.output_file)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("LiveTickLogger stopped.")

    def _run(self):
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                self._log_tick()
            except Exception as e:
                log.debug("LiveTickLogger tick error: %s", e)

            # Sleep the remainder of interval_sec in small increments to
            # remain instantly responsive to stop(), while also accounting
            # for however long _log_tick() itself just took.
            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, self.interval_sec - elapsed)
            steps = max(1, int(remaining / 0.1))
            for _ in range(steps):
                if self._stop_event.is_set():
                    break
                time.sleep(remaining / steps)

    def _log_tick(self):
        now = dt.datetime.now(dt.timezone.utc)
        window = current_window(now)

        if self.new_file_per_session and window != self._current_window:
            self._current_window = window
            self.output_file = _window_output_path(self._base_output_file, window.open_time)
            log.info("LiveTickLogger: new 15-min window -> switching to %s", self.output_file)
        
        # 1. CF Benchmarks spot price
        cf_price = None
        try:
            cf_price = spot_price.get_cf_benchmarks_price()
        except Exception:
            pass

        # 2. Kalshi market data
        strike_usd = None
        up_price_cents = None
        down_price_cents = None

        try:
            market = find_market_for_window(self.client, self.series_ticker, window)
            if market:
                strike_usd = get_strike_price(market)
                
                # Get the live YES (UP) price
                try:
                    ticker = market["ticker"]
                    orderbook = self.client.get_orderbook(ticker, depth=1)
                    up_price_cents = price_from_orderbook(orderbook, "yes")
                    down_price_cents = price_from_orderbook(orderbook, "no")
                except Exception:
                    pass
                
                # Fallback to market summary if orderbook is empty
                if up_price_cents is None:
                    up_price_cents = current_price_cents(market, "yes")
                if down_price_cents is None:
                    down_price_cents = current_price_cents(market, "no")
                
                # --- THE FIX ---
                # In a binary market, UP (YES) + DOWN (NO) = 100 cents.
                # Instead of trusting the API to return the inverted NO price 
                # (which often fails or returns the same YES price), we 
                # mathematically guarantee the DOWN price is correct.
                #if up_price_cents is not None:
                    #down_price_cents = 100 - up_price_cents

        except Exception:
            pass

        # Convert UTC to local timezone for the timestamp
        local_now = now.astimezone()
        # Format time to match your example exactly (local time, no timezone suffix)
        time_str = local_now.replace(tzinfo=None).isoformat()

        record = {
            "time": time_str,
            "btc_spot_cfbenchmarks": cf_price,
            "kalshi_strike_usd": strike_usd,
            "kalshi_up_price_cents": up_price_cents,
            "kalshi_down_price_cents": down_price_cents
        }

        # Append to file
        try:
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            log.error("Failed to write live tick: %s", e)