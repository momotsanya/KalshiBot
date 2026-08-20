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
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session
from ruamel.yaml import YAML

BOT_DIR = Path(__file__).resolve().parent.parent  # the kalshi_btc_bot/ folder, one level up from webapp/
CONFIG_PATH = BOT_DIR / "config.yaml"
BOT_SCRIPT = BOT_DIR / "bot.py"
SECRET_FILE = Path(__file__).resolve().parent / ".dashboard_secret"

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


# ---------- Bot process control ----------

CRASH_LOG_PATH = BOT_DIR / "webapp_bot_crash.log"


class BotProcess:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.stopped_intentionally = False

    def start(self) -> tuple[bool, str]:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "Bot is already running."
            try:
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
                return {"running": False, "pid": None, "crashed": False}
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
            }


def _tail_crash_log(max_lines: int = 40) -> str:
    try:
        with open(CRASH_LOG_PATH, "r", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return ""


bot_process = BotProcess()


# ---------- Config helpers ----------

def load_config_raw():
    """Returns the ruamel CommentedMap (preserves comments/structure) - used only for saving."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.load(f)


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
    node = load_config_raw()
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
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(node, f)
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


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
    app.run(host="0.0.0.0", port=8420, debug=False)
