# V1.0
"""
Fetches the current BTC/USD spot price from public sources.

Kalshi's own market data does NOT include a live intra-window BTC price feed
(only floor_strike, set once when the window opens, and expiration_value, set
once at settlement) - so the "spot_lean" strategy needs an external price
source. This tries CF Benchmarks' BRTI first (see below), then falls back
through a couple of free, public, no-auth exchange endpoints, since any
single one can be briefly down/rate-limited.

CF Benchmarks' BRTI (Bitcoin Real Time Index) is notable because it's
reportedly the actual index Kalshi uses to settle these BTC markets - so it
should be the most accurate source for this specific use case. It's scraped
from CF Benchmarks' public page (https://www.cfbenchmarks.com/data/assets/BTC)
using Playwright, updating every second - free, no account needed. Requires
`pip install playwright` and a one-time `playwright install chromium`. A
single headless browser tab is kept open and reused across calls (opening a
fresh browser on every 1-second poll would be far slower than the poll
interval itself) - it launches lazily on first use and is cleaned up on exit.
If Playwright isn't installed, this source is skipped silently and the bot
falls back to the free exchange sources below - no setup required if you
don't want to use CF Benchmarks at all.

get_btc_spot_price() returns (price, source_name) so callers can show which
source the price actually came from.
"""
from __future__ import annotations

import atexit
import logging
import time
import threading
from typing import Optional

import requests

log = logging.getLogger("spot_price")

_TIMEOUT_SEC = 5

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

_CFBENCHMARKS_URL = "https://www.cfbenchmarks.com/data/assets/BTC"
_CFBENCHMARKS_SELECTOR = "span.tabular-nums"

# If the scraped price hasn't changed in this long, assume the page's own
# live-update JS has silently stalled (e.g. throttled as a backgrounded tab,
# or its underlying feed disconnected) and force a page reload. This doesn't
# raise an exception on its own - inner_text() keeps successfully returning
# the last (now frozen) value - so it has to be caught by comparing values
# over time, not by error handling.
_STALE_THRESHOLD_SEC = 15

# Module-level state for the persistent scraping browser/page - kept alive
# across calls to get_btc_spot_price() rather than relaunched every poll.
_playwright_ctx = None
_browser = None
_page = None
_last_scraped_value = None
_last_scraped_value_since = None  # monotonic timestamp this value was first seen

# --- Thread-safe cache for CF Benchmarks price ---
# Playwright's sync API is NOT thread-safe. If we call it from the main thread
# and the LiveTickLogger background thread simultaneously, it crashes.
# Solution: A dedicated background thread handles ALL Playwright interactions
# and updates a thread-safe cache. Both the main thread and the logger just
# read from this cache.
_cfbenchmarks_lock = threading.Lock()
_cfbenchmarks_latest_price = None
_cfbenchmarks_latest_time = 0.0
_cfbenchmarks_worker_started = False

# Set by force_reload_cfbenchmarks() (callable from ANY thread, e.g. the
# LiveTickLogger thread at the start of a new session) - checked and cleared
# only by the dedicated worker thread, which is the only thread allowed to
# touch Playwright objects directly.
_force_reload_requested = threading.Event()


def _cleanup_cfbenchmarks_browser():
    global _playwright_ctx, _browser, _page, _last_scraped_value, _last_scraped_value_since
    try:
        if _browser is not None:
            _browser.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if _playwright_ctx is not None:
            _playwright_ctx.stop()
    except Exception:  # noqa: BLE001
        pass
    _playwright_ctx, _browser, _page = None, None, None
    _last_scraped_value, _last_scraped_value_since = None, None  # stale baseline is meaningless for a fresh page


atexit.register(_cleanup_cfbenchmarks_browser)


def _ensure_cfbenchmarks_page():
    """Lazily launches (or re-launches, after a prior failure) the persistent scraping browser tab."""
    global _playwright_ctx, _browser, _page
    if _page is not None:
        return _page
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    try:
        _playwright_ctx = sync_playwright().start()
        _browser = _playwright_ctx.chromium.launch(headless=True)
        page = _browser.new_page()
        page.goto(_CFBENCHMARKS_URL)
        page.wait_for_selector(_CFBENCHMARKS_SELECTOR, timeout=15000)
        _page = page
        log.debug("CF Benchmarks scraping browser started successfully.")
        return _page
    except Exception as e:  # noqa: BLE001
        log.debug("Failed to start CF Benchmarks scraping browser: %s", e)
        _cleanup_cfbenchmarks_browser()
        return None


def _parse_scraped_price(text: str) -> Optional[float]:
    text = text.strip()
    if not text or text in ("-", "--"):
        return None  # page hasn't populated a real value yet
    try:
        return float(text.replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _reload_cfbenchmarks_page():
    """Reloads the persistent page in place (cheaper than a full browser relaunch), used when a
    stuck/stale value is detected. Falls back to a full relaunch on the next call if this fails."""
    global _page
    if _page is None:
        return None
    try:
        _page.reload()
        _page.wait_for_selector(_CFBENCHMARKS_SELECTOR, timeout=15000)
        log.info("CF Benchmarks scraping page reloaded (stale value detected).")
        return _page
    except Exception as e:  # noqa: BLE001
        log.debug("Failed to reload CF Benchmarks page, will relaunch the browser next call: %s", e)
        _cleanup_cfbenchmarks_browser()
        return None

"""
def _from_cfbenchmarks_scrape() -> Optional[float]:
    global _last_scraped_value, _last_scraped_value_since
    page = _ensure_cfbenchmarks_page()
    if page is None:
        return None
    try:
        text = page.locator(_CFBENCHMARKS_SELECTOR).first.inner_text(timeout=3000)
        price = _parse_scraped_price(text)
        if price is None:
            # Placeholder ("-") shown briefly on page load - give it one short retry.
            page.wait_for_timeout(500)
            text = page.locator(_CFBENCHMARKS_SELECTOR).first.inner_text(timeout=3000)
            price = _parse_scraped_price(text)
        if price is None:
            return None

        now = time.monotonic()
        if price != _last_scraped_value:
            _last_scraped_value = price
            _last_scraped_value_since = now
            return price

        # Same value as last time - only a problem if it's been stuck a while, since BTC
        # can genuinely go a few seconds without moving.
        stale_for = now - _last_scraped_value_since if _last_scraped_value_since is not None else 0
        if stale_for <= _STALE_THRESHOLD_SEC:
            return price

        log.warning(
            "CF Benchmarks scraped price has been stuck at $%.2f for over %.0fs - the page's live "
            "feed may have stalled, reloading...",
            price, stale_for,
        )
        page = _reload_cfbenchmarks_page()
        if page is not None:
            try:
                text = page.locator(_CFBENCHMARKS_SELECTOR).first.inner_text(timeout=3000)
                refreshed = _parse_scraped_price(text)
                if refreshed is not None:
                    price = refreshed
            except Exception as e:  # noqa: BLE001
                log.debug("Read after reload failed: %s", e)
        # Reset the baseline either way, so a genuinely-unchanged price after a
        # successful reload doesn't immediately re-trigger another reload next call.
        _last_scraped_value, _last_scraped_value_since = price, time.monotonic()
        return price
    except Exception as e:  # noqa: BLE001
        log.debug("CF Benchmarks scrape read failed, will reinitialize the browser next call: %s", e)
        _cleanup_cfbenchmarks_browser()
        return None
"""

def _cfbenchmarks_worker():
    """Dedicated background thread that continuously scrapes CF Benchmarks."""
    global _cfbenchmarks_latest_price, _cfbenchmarks_latest_time
    while True:
        try:
            if _force_reload_requested.is_set():
                _force_reload_requested.clear()
                log.info("CF Benchmarks: forced reload requested - relaunching the scraping browser.")
                # A full relaunch (not just page.reload()) so a new session always
                # starts from a genuinely fresh browser/page, in case the "stuck"
                # value was caused by something below the page level (e.g. a dead
                # websocket the JS itself never notices) that an in-place reload
                # wouldn't necessarily clear. _ensure_cfbenchmarks_page() will
                # lazily relaunch on the scrape call right below.
                _cleanup_cfbenchmarks_browser()
            price = _do_cfbenchmarks_scrape()
            if price is not None:
                with _cfbenchmarks_lock:
                    _cfbenchmarks_latest_price = price
                    _cfbenchmarks_latest_time = time.time()
        except Exception as e:  # noqa: BLE001
            log.debug("CF Benchmarks worker error: %s", e)
        time.sleep(1.0)

def _start_cfbenchmarks_worker():
    global _cfbenchmarks_worker_started
    if _cfbenchmarks_worker_started:
        return
    _cfbenchmarks_worker_started = True
    t = threading.Thread(target=_cfbenchmarks_worker, daemon=True, name="CFBenchmarksWorker")
    t.start()
    log.info("CF Benchmarks background scraper started.")

def force_reload_cfbenchmarks():
    """
    Requests that the CF Benchmarks scraping browser be relaunched from
    scratch, picked up by the worker thread on its next loop iteration
    (within ~1s). Safe to call from any thread - it only sets a flag; the
    actual Playwright calls stay on the dedicated worker thread, since
    Playwright's sync API isn't thread-safe.

    Intended to be called once per new 15-minute window (bot.py's main loop
    does this at the same point it logs "New window ..."), since the scraped
    value can occasionally get stuck on one price for longer than the
    passive staleness check's threshold catches - starting each window with
    a guaranteed-fresh page avoids carrying a stale reading into a new
    session's tick data or into spot_lean's own live decisions.
    """
    _force_reload_requested.set()

def _from_cfbenchmarks_scrape() -> Optional[float]:
    """Returns the latest cached CF Benchmarks price from the worker thread."""
    _start_cfbenchmarks_worker()
    with _cfbenchmarks_lock:
        if _cfbenchmarks_latest_price is not None:
            return _cfbenchmarks_latest_price
    return None

def _do_cfbenchmarks_scrape() -> Optional[float]:
    """The actual scraping logic, called ONLY from the dedicated worker thread."""
    global _last_scraped_value, _last_scraped_value_since
    page = _ensure_cfbenchmarks_page()
    if page is None:
        return None
    try:
        text = page.locator(_CFBENCHMARKS_SELECTOR).first.inner_text(timeout=3000)
        price = _parse_scraped_price(text)
        if price is None:
            # Placeholder ("-") shown briefly on page load - give it one short retry.
            page.wait_for_timeout(500)
            text = page.locator(_CFBENCHMARKS_SELECTOR).first.inner_text(timeout=3000)
            price = _parse_scraped_price(text)
            if price is None:
                return None

        now = time.monotonic()
        if price != _last_scraped_value:
            _last_scraped_value = price
            _last_scraped_value_since = now
            return price

        # Same value as last time - only a problem if it's been stuck a while, since BTC
        # can genuinely go a few seconds without moving.
        stale_for = now - _last_scraped_value_since if _last_scraped_value_since is not None else 0
        if stale_for <= _STALE_THRESHOLD_SEC:
            return price

        log.warning(
            "CF Benchmarks scraped price has been stuck at $%.2f for over %.0fs - the page's live "
            "feed may have stalled, reloading...",
            price, stale_for,
        )
        page = _reload_cfbenchmarks_page()
        if page is not None:
            try:
                text = page.locator(_CFBENCHMARKS_SELECTOR).first.inner_text(timeout=3000)
                refreshed = _parse_scraped_price(text)
                if refreshed is not None:
                    price = refreshed
            except Exception as e:  # noqa: BLE001
                log.debug("Read after reload failed: %s", e)

        # Reset the baseline either way, so a genuinely-unchanged price after a
        # successful reload doesn't immediately re-trigger another reload next call.
        _last_scraped_value, _last_scraped_value_since = price, time.monotonic()
        return price
    except Exception as e:  # noqa: BLE001
        log.debug("CF Benchmarks scrape read failed, will reinitialize the browser next call: %s", e)
        _cleanup_cfbenchmarks_browser()
        return None

def _from_coinbase() -> Optional[float]:
    resp = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=_TIMEOUT_SEC)
    resp.raise_for_status()
    return float(resp.json()["data"]["amount"])


def _from_kraken() -> Optional[float]:
    resp = requests.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=_TIMEOUT_SEC)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise ValueError(f"Kraken API error: {data['error']}")
    result = data["result"]
    pair_key = next(iter(result))  # Kraken's pair key naming (XXBTZUSD) can vary
    return float(result[pair_key]["c"][0])  # 'c' = last trade closed [price, lot volume]


def _from_binance_us() -> Optional[float]:
    resp = requests.get("https://api.binance.us/api/v3/ticker/price?symbol=BTCUSD", timeout=_TIMEOUT_SEC)
    resp.raise_for_status()
    return float(resp.json()["price"])


_SOURCES = [
    ("CF Benchmarks", _from_cfbenchmarks_scrape),
    ("Coinbase", _from_coinbase),
    ("Kraken", _from_kraken),
    ("Binance.US", _from_binance_us),
]


def get_btc_spot_price() -> tuple:
    """
    Try each source in order (CF Benchmarks' scraped BRTI first if Playwright
    is installed, then free exchange APIs). Returns (price, source_name) - both
    None if every source failed.
    """
    for name, fetch_fn in _SOURCES:
        try:
            price = fetch_fn()
            if price and price > 0:
                return price, name
        except Exception as e:  # noqa: BLE001 - any network/parsing failure just falls through to the next source
            log.debug("Spot price fetch from %s failed: %s", name, e)
    log.warning("Could not fetch BTC spot price from any source (tried: %s).", ", ".join(n for n, _ in _SOURCES))
    return None, None


# Add to the bottom of spot_price.py

def get_cf_benchmarks_price() -> Optional[float]:
    """Fetches the BTC spot price directly from CF Benchmarks (BRTI)."""
    return _from_cfbenchmarks_scrape()