# V1.3
"""
Local web dashboard for the Kalshi BTC 15-min bot.

Runs alongside the existing bot.py completely unmodified - this reads
bot_state.json and tails bot.log (both already produced by bot.py), and
starts/stops bot.py as a subprocess. Config edits are written straight to
config.yaml using ruamel.yaml, which preserves the extensive comments
already in that file (a plain yaml.safe_load/dump round-trip would silently
strip them all).

Run:
    python server.py
Then open http://localhost:8420 (or http://<this-pc's-LAN-IP>:8420 from your phone).

SECURITY NOTE: this binds to 0.0.0.0 so it's reachable from other devices on
your network (e.g. your phone), and it can start/stop the bot and edit its
config - including your Kalshi credentials' file path and dry_run. A password
is required on first load (set via the DASHBOARD_PASSWORD environment
variable, or a random one is generated and printed to the console on first
run). This is basic protection against other devices on the same Wi-Fi/LAN,
not bank-grade security - don't expose this port to the open internet
(e.g. via port forwarding) without adding real authentication and HTTPS.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session
from ruamel.yaml import YAML

BOT_DIR = Path(__file__).resolve().parent.parent  # the kalshi_btc_bot/ folder, one level up from webapp/
CONFIG_PATH = BOT_DIR / "config.yaml"
BOT_SCRIPT = BOT_DIR / "bot.py"
SECRET_FILE = Path(__file__).resolve().parent / ".dashboard_secret"
DATA_DIR = BOT_DIR / "data"

# bot.py / tick_backtest.py / simulator.py / state.py etc. all live in BOT_DIR
# (one level up from this file), not in webapp/ - add it to sys.path so the
# Simulator tab's routes below can import simulator.py the same way this
# file already runs bot.py as a subprocess for the Monitor tab.
sys.path.insert(0, str(BOT_DIR))
try:
    import simulator as sim_module
    _SIM_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 - missing/broken dependency shouldn't take down the whole dashboard
    sim_module = None
    _SIM_IMPORT_ERROR = f"{type(e).__name__}: {e}"

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = secrets.token_hex(32)  # session signing key - regenerates each server restart, logging everyone out (intentional, simplest safe default)

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)


# ---------- Password ----------

def _get_dashboard_password() -> str:
    env_pw = os.environ.get("DASHBOARD_PASSWORD")
    if env_pw:
        return env_pw
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    pw = secrets.token_urlsafe(9)
    SECRET_FILE.write_text(pw)
    return pw


DASHBOARD_PASSWORD = _get_dashboard_password()


def require_auth(fn):
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return jsonify({"error": "not authenticated"}), 401
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------- Config helpers ----------
# (Defined before BotProcess since start() reads config.yaml at launch time.)

# How many times to retry a read that fails to parse, and how long to wait
# between attempts. A failed parse here almost always means the read landed
# on config.yaml at the exact instant something else was mid-write - in this
# project's case, config.yaml is edited on Windows and reached over an
# SMB/CIFS mount, which can hand back a torn/partial read while the Windows
# side is still flushing a write (editor autosave, Explorer touching the
# file, AV scan, etc.). The file on disk in that moment is momentarily
# incomplete, NOT actually corrupted. A short retry resolves that without
# surfacing a scary traceback for what is really just a timing hiccup.
# save_config_raw() below is also made atomic (write-to-temp + os.replace)
# specifically to eliminate this race for THIS process's own writes; this
# retry is a defensive backstop for the SMB-mount case and any other
# external, non-atomic writer.
_CONFIG_READ_RETRIES = 3
_CONFIG_READ_RETRY_DELAY_SEC = 0.15


def load_config_raw():
    """Returns the ruamel CommentedMap (preserves comments/structure) - used
    for serving /api/config, reading individual keys (e.g. /api/live_tick's
    live_tick.file), and snapshotting launch-time env in BotProcess.start().
    Retries a few times on a parse failure before giving up - see
    _CONFIG_READ_RETRIES above."""
    last_err = None
    for attempt in range(_CONFIG_READ_RETRIES + 1):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return yaml.load(f)
        except Exception as e:  # noqa: BLE001 - any parse/IO error is retried the same way
            last_err = e
            if attempt < _CONFIG_READ_RETRIES:
                time.sleep(_CONFIG_READ_RETRY_DELAY_SEC)
    raise last_err


def config_to_plain(node):
    """Recursively converts a ruamel CommentedMap/Seq to plain dict/list for JSON serialization."""
    if hasattr(node, "items"):
        return {k: config_to_plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [config_to_plain(v) for v in node]
    return node


def apply_updates(node, updates):
    """Recursively applies a plain dict of updates onto a ruamel CommentedMap IN PLACE,
    preserving comments on every key that already existed. New keys (shouldn't normally
    happen from this UI, but handled defensively) are added plainly."""
    for k, v in updates.items():
        if isinstance(v, dict) and k in node and hasattr(node[k], "items"):
            apply_updates(node[k], v)
        else:
            node[k] = v


def save_config_raw(node):
    """
    Writes the config atomically: dump to a temp file in the SAME directory
    as config.yaml, then os.replace() it over the real path. os.replace() is
    an atomic rename on both Linux and Windows - any concurrent reader (e.g.
    the dashboard's own /api/live_tick polling, which reloads config.yaml
    every couple seconds) will see either the complete old file or the
    complete new file, NEVER a half-written one. Writing directly to
    CONFIG_PATH (the old approach) has a window where a reader can land on a
    truncated/partial file mid-write.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(CONFIG_PATH.parent), prefix=".config.yaml.", suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            yaml.dump(node, f)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------- Bot process control ----------

CRASH_LOG_PATH = BOT_DIR / "webapp_bot_crash.log"


class BotProcess:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.stopped_intentionally = False
        # The runtime.dry_run / kalshi.base_url the CURRENTLY (or most recently)
        # running process was actually launched with - captured once at start()
        # time, not re-read from config.yaml afterward. This is deliberately
        # separate from whatever the Configuration tab's form currently holds:
        # the form can have unsaved edits, so it can't be trusted to describe
        # what the live subprocess is actually doing.
        self.launched_dry_run: bool | None = None
        self.launched_base_url: str | None = None

    def start(self) -> tuple[bool, str]:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "Bot is already running."
            try:
                # Snapshot the env this launch is using BEFORE spawning the
                # subprocess, straight from config.yaml on disk (i.e. whatever
                # was last actually saved - not the in-browser form state).
                try:
                    launch_cfg = load_config_raw()
                    self.launched_dry_run = bool(launch_cfg.get("runtime", {}).get("dry_run", True))
                    self.launched_base_url = str(launch_cfg.get("kalshi", {}).get("base_url", "") or "")
                except Exception:  # noqa: BLE001
                    self.launched_dry_run = None
                    self.launched_base_url = None

                # stdout/stderr are captured to a crash log instead of discarded -
                # otherwise a startup failure (bad key path, bad credentials,
                # network issue) would be completely invisible: the process would
                # just exit and the dashboard would show "STOPPED" with no clue why.
                crash_log = open(CRASH_LOG_PATH, "w")
                self.proc = subprocess.Popen(
                    [sys.executable, str(BOT_SCRIPT), "--config", str(CONFIG_PATH)],
                    cwd=str(BOT_DIR),
                    stdout=crash_log,
                    stderr=subprocess.STDOUT,
                )
                self.stopped_intentionally = False
            except Exception as e:  # noqa: BLE001
                return False, f"Failed to start: {e}"
            return True, f"Started (pid {self.proc.pid})."

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.proc = None
                return False, "Bot is not running."
            self.stopped_intentionally = True
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None
            return True, "Stopped."

    def status(self) -> dict:
        with self.lock:
            if self.proc is None:
                return {
                    "running": False,
                    "pid": None,
                    "crashed": False,
                    "launched_dry_run": self.launched_dry_run,
                    "launched_base_url": self.launched_base_url,
                }
            exit_code = self.proc.poll()
            running = exit_code is None
            crashed = (not running) and (not self.stopped_intentionally) and exit_code != 0
            crash_tail = None
            if crashed:
                crash_tail = _tail_crash_log()
            return {
                "running": running,
                "pid": self.proc.pid if running else None,
                "crashed": crashed,
                "exit_code": exit_code,
                "crash_tail": crash_tail,
                "launched_dry_run": self.launched_dry_run,
                "launched_base_url": self.launched_base_url,
            }


def _tail_crash_log(max_lines: int = 40) -> str:
    try:
        with open(CRASH_LOG_PATH, "r", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return ""


bot_process = BotProcess()


# ---------- Routes ----------

@app.route("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("password") == DASHBOARD_PASSWORD:
        session["authed"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Incorrect password"}), 401


@app.route("/api/state")
@require_auth
def api_state():
    state_path = BOT_DIR / "bot_state.json"
    if not state_path.exists():
        return jsonify({"exists": False})
    try:
        import json
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["exists"] = True
        return jsonify(data)
    except Exception as e:  # noqa: BLE001
        return jsonify({"exists": False, "error": str(e)})


@app.route("/api/logs")
@require_auth
def api_logs():
    """Returns the last N lines of the log file, or lines after a given byte offset for
    efficient polling (pass ?offset=<bytes already seen> to only get new content)."""
    log_path = BOT_DIR / "bot.log"
    if not log_path.exists():
        return jsonify({"lines": [], "offset": 0})

    offset = request.args.get("offset", type=int)
    max_lines = request.args.get("max_lines", default=500, type=int)

    size = log_path.stat().st_size
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        if offset is not None and 0 <= offset <= size:
            f.seek(offset)
            content = f.read()
            lines = content.splitlines()
        else:
            f.seek(0)
            all_lines = f.read().splitlines()
            lines = all_lines[-max_lines:]
    return jsonify({"lines": lines, "offset": size})


@app.route("/api/live_tick")
@require_auth
def api_live_tick():
    """
    Returns the single most recent record written by data_logger.py's
    LiveTickLogger (CF Benchmarks BTC spot price, this window's target price
    i.e. floor_strike, and live UP/DOWN prices) - used by the dashboard's
    spot-lean gauge. Independent of bot_state.json; reads straight from the
    live_tick JSONL file(s), so it only has data when live_tick.enabled: true
    in config.yaml and the bot has been running long enough to write a tick.

    With new_file_per_session (the default), a fresh file is created every
    15-min window named after that window's start time, so this picks
    whichever matching file was modified most recently rather than assuming
    a fixed filename.

    This route reloads config.yaml on every poll (every ~2s from the
    frontend) just to read live_tick.file/new_file_per_session - if that
    read fails for any reason (including a config save mid-write, even
    after load_config_raw()'s own retries), degrade to "no data this poll"
    instead of a 500: the frontend already treats {"exists": false} as
    "nothing to show yet" and will simply pick it up again next poll.
    """
    try:
        node = load_config_raw()
    except Exception as e:  # noqa: BLE001
        return jsonify({"exists": False, "error": f"config unreadable: {e}"})

    lt_cfg = node.get("live_tick", {}) or {}
    base_file = lt_cfg.get("file", "./data/live_ticks.jsonl")
    base_path = (BOT_DIR / base_file).resolve()
    new_file_per_session = lt_cfg.get("new_file_per_session", True)

    latest_file = None
    if new_file_per_session:
        stem = base_path.stem
        suffix = base_path.suffix or ".jsonl"
        try:
            candidates = sorted(
                base_path.parent.glob(f"{stem}_*{suffix}"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
        except OSError:
            candidates = []
        if candidates:
            latest_file = candidates[0]
    if latest_file is None and base_path.exists():
        latest_file = base_path

    if latest_file is None or not latest_file.exists():
        return jsonify({"exists": False})

    try:
        with open(latest_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        last_line = next((l for l in reversed(lines) if l.strip()), None)
        if not last_line:
            return jsonify({"exists": False})
        import json
        record = json.loads(last_line)
        record["exists"] = True
        return jsonify(record)
    except Exception as e:  # noqa: BLE001
        return jsonify({"exists": False, "error": str(e)})


@app.route("/api/control/start", methods=["POST"])
@require_auth
def api_start():
    ok, msg = bot_process.start()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/control/stop", methods=["POST"])
@require_auth
def api_stop():
    ok, msg = bot_process.stop()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/control/status")
@require_auth
def api_status():
    return jsonify(bot_process.status())


@app.route("/api/config", methods=["GET"])
@require_auth
def api_get_config():
    try:
        node = load_config_raw()
    except Exception as e:  # noqa: BLE001 - surface as a clean JSON error instead of a raw Flask 500
        return jsonify({"error": f"Could not read config.yaml: {e}"}), 500
    return jsonify(config_to_plain(node))


@app.route("/api/config", methods=["POST"])
@require_auth
def api_save_config():
    updates = request.get_json(force=True, silent=True)
    if not isinstance(updates, dict):
        return jsonify({"ok": False, "error": "Invalid payload"}), 400
    try:
        node = load_config_raw()
        apply_updates(node, updates)
        save_config_raw(node)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------- Simulator tab ----------

def _sim_data_dir() -> str:
    """
    Resolves the tick-data folder the Simulator tab should read: whatever
    directory live_tick.file currently points at (config.yaml's own source
    of truth for where data_logger.py writes), falling back to ./data if
    that can't be read or doesn't exist yet.
    """
    try:
        node = load_config_raw()
        lt_file = node.get("live_tick", {}).get("file", "./data/live_ticks.jsonl")
        resolved = (BOT_DIR / lt_file).resolve().parent
        if resolved.exists():
            return str(resolved)
    except Exception:  # noqa: BLE001
        pass
    return str(DATA_DIR)


@app.route("/api/simulator/chart")
@require_auth
def api_simulator_chart():
    """
    Returns one "page" of recorded tick data for the Simulator tab's chart:
    `count` windows' worth of ticks, `offset` windows back from the most
    recent one (offset=0 = latest data) - lets the frontend page left/right
    through history without loading every recorded tick at once.
    """
    if sim_module is None:
        return jsonify({"error": f"Simulator unavailable: {_SIM_IMPORT_ERROR}"}), 500

    offset = request.args.get("offset", default=0, type=int)
    count = request.args.get("count", default=20, type=int)
    try:
        windows = sim_module.load_all_windows(_sim_data_dir())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500

    total = len(windows)
    if total == 0:
        return jsonify({"total_windows": 0, "offset": 0, "count": count, "windows": [], "ticks": []})

    offset = max(0, min(offset, total - 1))
    end_idx = max(0, total - offset)
    start_idx = max(0, end_idx - count)
    chunk = windows[start_idx:end_idx]

    windows_out = [
        {"open_time": w.open_time.isoformat(), "close_time": w.close_time.isoformat(), "strike": w.strike}
        for w in chunk
    ]
    ticks_out = [
        {"t": tk.t.isoformat(), "spot": tk.spot, "up": tk.up_cents, "down": tk.down_cents}
        for w in chunk for tk in w.ticks
    ]
    return jsonify({
        "total_windows": total,
        "offset": offset,
        "count": count,
        "start_index": start_idx,
        "end_index": end_idx,
        "windows": windows_out,
        "ticks": ticks_out,
    })


@app.route("/api/simulator/run", methods=["POST"])
@require_auth
def api_simulator_run():
    """
    Runs ONE simulated pass of the strategy currently saved in config.yaml
    against every recorded tick file, using simulator.py (which drives the
    exact same sizing/hedge/take-profit/scoring code bot.py itself uses -
    see that module's docstring). This can take a little while on a lot of
    recorded data since it's a real per-tick replay, not a shortcut.
    """
    if sim_module is None:
        return jsonify({"ok": False, "error": f"Simulator unavailable: {_SIM_IMPORT_ERROR}"}), 500
    try:
        cfg = config_to_plain(load_config_raw())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Could not read config.yaml: {e}"}), 500

    try:
        result = sim_module.run_simulation(cfg, data_dir=_sim_data_dir())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]})
    result["ok"] = True
    return jsonify(result)


def _open_browser_when_ready(url: str, timeout_sec: float = 10.0):
    """
    Polls the dashboard's own port until it responds, then opens the default
    browser. Waiting for a real response (instead of a fixed sleep) avoids
    opening the tab before Flask has actually started accepting connections,
    which would just show a "can't connect" page.
    """
    import urllib.request
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    webbrowser.open(url)


if __name__ == "__main__":
    print("=" * 60)
    print("Kalshi Bot Dashboard")
    print("=" * 60)
    print(f"Dashboard password: {DASHBOARD_PASSWORD}")
    print("(set DASHBOARD_PASSWORD env var to use your own instead of this generated one)")
    print()
    print("Open on this PC:      http://localhost:8420")
    print("Open from your phone: http://<this-PC's-LAN-IP>:8420  (same Wi-Fi network)")
    print("=" * 60)

    threading.Thread(
        target=_open_browser_when_ready, args=("http://localhost:8420",), daemon=True,
    ).start()

    app.run(host="0.0.0.0", port=8420, debug=False)
