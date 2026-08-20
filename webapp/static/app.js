// ============================================================
// Auth / bootstrap
// ============================================================
async function checkAuthAndInit() {
  const res = await fetch("/api/state");
  if (res.status === 401) {
    document.getElementById("login-screen").classList.remove("hidden");
    return;
  }
  showApp();
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    const data = await res.json();
    if (data.ok) {
      showApp();
    } else {
      errEl.textContent = data.error || "Incorrect password";
    }
  } catch (err) {
    errEl.textContent = "Could not reach the server.";
  }
});

function showApp() {
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  initApp();
}

// ============================================================
// Tabs (Monitor / Configuration)
// ============================================================
function wireTopTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-panel").forEach((p) =>
        p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab)
      );
    });
  });
}

// ============================================================
// Monitor: bot control (start/stop/status)
// ============================================================
async function refreshBotStatus() {
  try {
    const res = await fetch("/api/control/status");
    if (!res.ok) return;
    const data = await res.json();
    const badge = document.getElementById("bot-status-badge");
    const running = !!data.running;
    const crashed = !!data.crashed;

    if (crashed) {
      badge.textContent = "CRASHED";
      badge.className = "badge badge-crashed";
    } else {
      badge.textContent = running ? "RUNNING" : "STOPPED";
      badge.className = "badge " + (running ? "badge-running" : "badge-stopped");
    }
    document.getElementById("btn-start").disabled = running;
    document.getElementById("btn-stop").disabled = !running;

    const crashPanel = document.getElementById("crash-panel");
    if (crashed) {
      document.getElementById("crash-exit-code").textContent = data.exit_code;
      document.getElementById("crash-tail").textContent = data.crash_tail || "(no output captured)";
      crashPanel.classList.remove("hidden");
    } else {
      crashPanel.classList.add("hidden");
    }
  } catch (e) {
    // transient network hiccup - next poll will retry
  }
}

document.getElementById("btn-start").addEventListener("click", async () => {
  const btn = document.getElementById("btn-start");
  btn.disabled = true;
  try {
    const res = await fetch("/api/control/start", { method: "POST" });
    const data = await res.json();
    if (!data.ok) alert(data.message || "Failed to start the bot.");
  } finally {
    refreshBotStatus();
  }
});

document.getElementById("btn-stop").addEventListener("click", async () => {
  const btn = document.getElementById("btn-stop");
  btn.disabled = true;
  try {
    const res = await fetch("/api/control/stop", { method: "POST" });
    const data = await res.json();
    if (!data.ok) alert(data.message || "Failed to stop the bot.");
  } finally {
    refreshBotStatus();
  }
});

// ============================================================
// Monitor: state stats (bot_state.json)
// ============================================================
function fmtDollars(cents) {
  return "$" + (cents / 100).toFixed(2);
}

async function refreshState() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) return;
    const s = await res.json();
    if (!s.exists) return;

    const pnlEl = document.getElementById("stat-pnl");
    pnlEl.textContent = fmtDollars(s.total_pnl_cents || 0);
    pnlEl.className = "stat-value " + (s.total_pnl_cents > 0 ? "pos" : s.total_pnl_cents < 0 ? "neg" : "");

    document.getElementById("stat-record").textContent = `${s.total_wins || 0}-${s.total_losses || 0}`;
    document.getElementById("stat-cumloss").textContent = fmtDollars(s.cumulative_loss_cents || 0);
    document.getElementById("stat-drawdown").textContent = fmtDollars(s.max_drawdown_cents || 0);
    document.getElementById("stat-stake").textContent = s.current_stake != null ? s.current_stake : "-";
    document.getElementById("stat-chop").textContent = s.skipped_chop || 0;
  } catch (e) {
    // transient - next poll retries
  }
}

// ============================================================
// Monitor: live log tail (offset-based polling)
// ============================================================
let logOffset = 0;
let logLinesShown = 0;
const MAX_LOG_LINES_DOM = 1000; // trim oldest lines past this to keep the DOM light on long-running sessions

function classifyLogLine(line) {
  if (/^\s*==>\s*WIN/.test(line) || / WIN /.test(line)) return "line-win";
  if (/^\s*==>\s*LOSS/.test(line) || / LOSS /.test(line)) return "line-loss";
  if (/\[ERROR\]/.test(line)) return "line-error";
  if (/\[WARNING\]/.test(line)) return "line-warning";
  if (/ORDER PLACED/.test(line)) return "line-order";
  if (/New window/.test(line)) return "line-window";
  return "line-info";
}

function appendLogLines(lines) {
  if (!lines.length) return;
  const view = document.getElementById("log-view");
  const emptyNote = view.querySelector(".log-empty");
  if (emptyNote) emptyNote.remove();

  const frag = document.createDocumentFragment();
  for (const line of lines) {
    const div = document.createElement("div");
    div.className = classifyLogLine(line);
    div.textContent = line;
    frag.appendChild(div);
  }
  view.appendChild(frag);
  logLinesShown += lines.length;

  while (logLinesShown > MAX_LOG_LINES_DOM && view.firstChild) {
    view.removeChild(view.firstChild);
    logLinesShown--;
  }

  if (document.getElementById("autoscroll").checked) {
    view.scrollTop = view.scrollHeight;
  }
}

async function pollLogs() {
  try {
    const res = await fetch(`/api/logs?offset=${logOffset}`);
    if (!res.ok) return;
    const data = await res.json();
    appendLogLines(data.lines || []);
    logOffset = data.offset;
  } catch (e) {
    // transient - next poll retries
  }
}

async function loadInitialLogs() {
  try {
    const res = await fetch("/api/logs?max_lines=500");
    if (!res.ok) return;
    const data = await res.json();
    appendLogLines(data.lines || []);
    logOffset = data.offset;
  } catch (e) {
    document.getElementById("log-view").innerHTML = '<div class="log-empty">Could not load the log file.</div>';
  }
}

// ============================================================
// Configuration form
// ============================================================
const BASE_URLS = {
  demo: "https://demo-api.kalshi.co/trade-api/v2",
  production: "https://api.elections.kalshi.com/trade-api/v2",
};

let cfg = null;         // the full config object as loaded from the server (nested, mirrors config.yaml)
let currentEnv = "production";
const fieldMap = [];    // {id, path: ['strategy','max_price_cents'], kind}

function getPath(obj, path) {
  let cur = obj;
  for (const key of path) {
    if (cur == null) return undefined;
    cur = cur[key];
  }
  return cur;
}
function setPath(obj, path, value) {
  let cur = obj;
  for (let i = 0; i < path.length - 1; i++) {
    if (cur[path[i]] == null || typeof cur[path[i]] !== "object") cur[path[i]] = {};
    cur = cur[path[i]];
  }
  cur[path[path.length - 1]] = value;
}

function bindField(id, path, kind) {
  fieldMap.push({ id, path, kind });
  const el = document.getElementById(id);
  if (!el) return;
  const eventName = kind === "select" || kind === "toggle" ? "change" : "input";
  el.addEventListener(eventName, () => {
    let value;
    if (kind === "toggle") value = el.checked;
    else if (kind === "number") value = el.value === "" ? 0 : parseFloat(el.value);
    else value = el.value;
    setPath(cfg, path, value);
    onConfigFieldChanged();
  });
}

function applyFieldsFromConfig() {
  for (const f of fieldMap) {
    const el = document.getElementById(f.id);
    if (!el) continue;
    const value = getPath(cfg, f.path);
    if (value === undefined) continue;
    if (f.kind === "toggle") el.checked = !!value;
    else el.value = value;
  }
}

function wireConfigFields() {
  bindField("kalshi_key_id", ["kalshi", "key_id"], "text");
  bindField("kalshi_private_key_path", ["kalshi", "private_key_path"], "text");
  bindField("market_series_ticker", ["market", "series_ticker"], "text");

  bindField("runtime_dry_run", ["runtime", "dry_run"], "toggle");
  bindField("runtime_poll_interval_sec", ["runtime", "poll_interval_sec"], "number");
  bindField("runtime_result_wait_timeout_sec", ["runtime", "result_wait_timeout_sec"], "number");
  bindField("runtime_state_file", ["runtime", "state_file"], "text");
  bindField("runtime_log_file", ["runtime", "log_file"], "text");

  bindField("sizing_mode", ["sizing", "mode"], "select");
  bindField("sizing_base_size", ["sizing", "base_size"], "number");
  bindField("sizing_fee_per_contract_cents", ["sizing", "fee_per_contract_cents"], "number");
  bindField("sizing_martingale_multiplier", ["sizing", "martingale_multiplier"], "number");
  bindField("sizing_max_martingale_steps", ["sizing", "max_martingale_steps"], "number");
  bindField("sizing_max_stake", ["sizing", "max_stake"], "number");

  bindField("recovery_min_profit_cents", ["recovery", "min_profit_cents"], "number");
  bindField("recovery_max_contracts", ["recovery", "max_contracts"], "number");
  bindField("recovery_max_price_cents", ["recovery", "max_price_cents"], "number");
  bindField("recovery_min_price_cents", ["recovery", "min_price_cents"], "number");
  bindField("recovery_max_cumulative_loss_cents", ["recovery", "max_cumulative_loss_cents"], "number");

  bindField("strategy_entry_start_min", ["strategy", "entry_start_min"], "number");
  bindField("strategy_entry_end_min", ["strategy", "entry_end_min"], "number");
  bindField("strategy_min_price_cents", ["strategy", "min_price_cents"], "number");
  bindField("strategy_max_price_cents", ["strategy", "max_price_cents"], "number");
  bindField("strategy_order_timeout_sec", ["strategy", "order_timeout_sec"], "number");

  bindField("mf_enabled", ["strategy", "momentum_filter", "enabled"], "toggle");
  bindField("mf_lookback_sec", ["strategy", "momentum_filter", "lookback_sec"], "number");
  bindField("cf_enabled", ["strategy", "chop_filter", "enabled"], "toggle");
  bindField("cf_lookback", ["strategy", "chop_filter", "lookback"], "number");
  bindField("cf_max_alternations", ["strategy", "chop_filter", "max_alternations"], "number");

  bindField("adaptive_default_mode", ["strategy", "adaptive_default_mode"], "select");
  bindField("pt_lookback_cycles", ["strategy", "price_trend", "lookback_cycles"], "number");
  bindField("pt_threshold_pct", ["strategy", "price_trend", "threshold_pct"], "number");

  bindField("sl_threshold_pct", ["strategy", "spot_lean", "threshold_pct"], "number");
  bindField("sl_poll_interval_sec", ["strategy", "spot_lean", "poll_interval_sec"], "number");

  bindField("hedge_enabled", ["strategy", "spot_lean", "hedge", "enabled"], "toggle");
  bindField("hedge_threshold_pct", ["strategy", "spot_lean", "hedge", "threshold_pct"], "number");
  bindField("hedge_max_hedges_per_window", ["strategy", "spot_lean", "hedge", "max_hedges_per_window"], "number");
  bindField("hedge_fresh_start_only", ["strategy", "spot_lean", "hedge", "fresh_start_only"], "toggle");
  bindField("hedge_net_session_sizing", ["strategy", "spot_lean", "hedge", "net_session_sizing"], "toggle");
  bindField("hedge_smart_sizing", ["strategy", "spot_lean", "hedge", "smart_sizing"], "toggle");
  bindField("hedge_min_profit_cents", ["strategy", "spot_lean", "hedge", "min_profit_cents"], "number");
  bindField("hedge_max_contracts", ["strategy", "spot_lean", "hedge", "max_contracts"], "number");

  document.querySelectorAll("#envSeg button").forEach((btn) => {
    btn.addEventListener("click", () => {
      currentEnv = btn.dataset.val;
      setPath(cfg, ["kalshi", "base_url"], BASE_URLS[currentEnv]);
      renderEnvSeg();
      onConfigFieldChanged();
    });
  });

  document.querySelectorAll(".use-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      setPath(cfg, ["strategy", "mode"], btn.dataset.usemode);
      renderModeState();
      onConfigFieldChanged();
    });
  });

  document.querySelectorAll(".strategy-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".strategy-tab").forEach((t) => t.classList.toggle("active", t === tab));
      document.querySelectorAll(".strategy-panel").forEach((p) =>
        p.classList.toggle("active", p.dataset.panel === tab.dataset.tab)
      );
    });
  });
}

function renderEnvSeg() {
  document.querySelectorAll("#envSeg button").forEach((b) => b.classList.toggle("on", b.dataset.val === currentEnv));
}

function renderModeState() {
  const mode = getPath(cfg, ["strategy", "mode"]);
  document.querySelectorAll(".strategy-tab").forEach((t) => t.classList.toggle("is-selected", t.dataset.tab === mode));
  document.querySelectorAll(".use-btn").forEach((b) => {
    const selected = b.dataset.usemode === mode;
    b.classList.toggle("selected", selected);
    b.textContent = selected ? "Currently active" : "Use this strategy";
  });
}

function renderSizingVisibility() {
  const isRecovery = getPath(cfg, ["sizing", "mode"]) === "recovery";
  document.getElementById("recoveryFields").classList.toggle("dimmed", !isRecovery);
  document.getElementById("martingaleFields").style.opacity = isRecovery ? "0.4" : "1";
}

function renderHedgeVisibility() {
  const enabled = !!getPath(cfg, ["strategy", "spot_lean", "hedge", "enabled"]);
  const smart = !!getPath(cfg, ["strategy", "spot_lean", "hedge", "smart_sizing"]);
  document.getElementById("hedgeSub").classList.toggle("dimmed", !enabled);
}

function renderDryRunStyling() {
  const dryRun = !!getPath(cfg, ["runtime", "dry_run"]);
  document.getElementById("dryrunCard").classList.toggle("is-live", !dryRun);
}

function renderConfigDerived() {
  const baseUrl = getPath(cfg, ["kalshi", "base_url"]) || "";
  currentEnv = baseUrl.includes("demo") ? "demo" : "production";
  renderEnvSeg();
  renderModeState();
  renderSizingVisibility();
  renderHedgeVisibility();
  renderDryRunStyling();
}

function onConfigFieldChanged() {
  renderModeState();
  renderSizingVisibility();
  renderHedgeVisibility();
  renderDryRunStyling();
  const statusEl = document.getElementById("save-status");
  statusEl.textContent = "Unsaved changes";
  statusEl.className = "save-status";
}

async function loadConfig() {
  const res = await fetch("/api/config");
  if (!res.ok) {
    document.getElementById("save-status").textContent = "Could not load config.yaml";
    document.getElementById("save-status").className = "save-status err";
    return;
  }
  cfg = await res.json();
  applyFieldsFromConfig();
  renderConfigDerived();
}

document.getElementById("btn-save-config").addEventListener("click", async () => {
  const statusEl = document.getElementById("save-status");
  statusEl.textContent = "Saving...";
  statusEl.className = "save-status";
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json();
    if (data.ok) {
      statusEl.textContent = "Saved";
      statusEl.className = "save-status";
    } else {
      statusEl.textContent = "Save failed: " + (data.error || "unknown error");
      statusEl.className = "save-status err";
    }
  } catch (e) {
    statusEl.textContent = "Save failed: network error";
    statusEl.className = "save-status err";
  }
});

// ============================================================
// Init
// ============================================================
function initApp() {
  wireTopTabs();
  wireConfigFields();
  loadConfig();
  loadInitialLogs();
  refreshBotStatus();
  refreshState();

  setInterval(refreshBotStatus, 4000);
  setInterval(refreshState, 4000);
  setInterval(pollLogs, 2000);
}

checkAuthAndInit();
