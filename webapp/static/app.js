// V1.9
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
    document.getElementById("stat-recovery-attempts").textContent = s.recovery_attempts || 0;
  } catch (e) {
    // transient - next poll retries
  }
}

// ============================================================
// Monitor: session (current 15-min window) time gauge
// ============================================================
const WINDOW_MIN = 15;
const GAUGE_CX = 110, GAUGE_CY = 110, GAUGE_R = 78;
const GAUGE_START_ANGLE = 135;   // degrees - bottom-left
const GAUGE_END_ANGLE = 405;     // degrees (=45) - bottom-right, 270deg total sweep
const NEEDLE_LEN = 68;

function gaugePolar(cx, cy, r, angleDeg) {
  const rad = (angleDeg * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}
function gaugeArcPath(cx, cy, r, startAngle, endAngle) {
  const start = gaugePolar(cx, cy, r, startAngle);
  const end = gaugePolar(cx, cy, r, endAngle);
  const largeArc = endAngle - startAngle <= 180 ? "0" : "1";
  return `M ${start.x.toFixed(2)} ${start.y.toFixed(2)} A ${r} ${r} 0 ${largeArc} 1 ${end.x.toFixed(2)} ${end.y.toFixed(2)}`;
}
function gaugeValueToAngle(minutes) {
  const clamped = Math.max(0, Math.min(WINDOW_MIN, minutes));
  return GAUGE_START_ANGLE + (clamped / WINDOW_MIN) * (GAUGE_END_ANGLE - GAUGE_START_ANGLE);
}

function buildGaugeStatic() {
  const track = document.getElementById("gauge-track");
  if (!track) return; // gauge markup not present on this page
  track.setAttribute("d", gaugeArcPath(GAUGE_CX, GAUGE_CY, GAUGE_R, GAUGE_START_ANGLE, GAUGE_END_ANGLE));

  const ticksGroup = document.getElementById("gauge-ticks");
  const numbersGroup = document.getElementById("gauge-numbers");
  ticksGroup.innerHTML = "";
  numbersGroup.innerHTML = "";

  for (let m = 0; m <= WINDOW_MIN; m++) {
    const isMajor = m % 3 === 0; // labeled ticks at 0,3,6,9,12,15 - minor ticks every 1 min
    const angle = gaugeValueToAngle(m);
    const outer = gaugePolar(GAUGE_CX, GAUGE_CY, GAUGE_R, angle);
    const inner = gaugePolar(GAUGE_CX, GAUGE_CY, GAUGE_R - (isMajor ? 10 : 5), angle);

    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", outer.x); tick.setAttribute("y1", outer.y);
    tick.setAttribute("x2", inner.x); tick.setAttribute("y2", inner.y);
    tick.setAttribute("class", isMajor ? "gauge-tick-major" : "gauge-tick-minor");
    ticksGroup.appendChild(tick);

    if (isMajor) {
      const isExtreme = m === 0 || m === WINDOW_MIN;
      const labelRadius = isExtreme ? GAUGE_R + 14 : GAUGE_R - 24;
      const labelPos = gaugePolar(GAUGE_CX, GAUGE_CY, labelRadius, angle);
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", labelPos.x); text.setAttribute("y", labelPos.y);
      text.setAttribute("class", "gauge-number");
      text.textContent = String(m);
      numbersGroup.appendChild(text);
    }
  }
}

function updateGaugeBand(entryStartMin, entryEndMin) {
  const band = document.getElementById("gauge-band");
  const before = document.getElementById("gauge-band-before");
  const after = document.getElementById("gauge-band-after");
  const sub = document.getElementById("timegauge-min");
  if (!band) return;
  if (entryStartMin == null || entryEndMin == null || entryEndMin <= entryStartMin) {
    band.setAttribute("d", "");
    if (before) before.setAttribute("d", "");
    if (after) after.setAttribute("d", "");
    if (sub) sub.textContent = "of 15:00 window";
    return;
  }
  const startAngle = gaugeValueToAngle(entryStartMin);
  const endAngle = gaugeValueToAngle(entryEndMin);
  band.setAttribute("d", gaugeArcPath(GAUGE_CX, GAUGE_CY, GAUGE_R, startAngle, endAngle));

  // Red before entry_start_min and after entry_end_min - the parts of the
  // window the bot will NOT place a bet in - flanking the green entry band.
  if (before) {
    before.setAttribute(
      "d",
      entryStartMin > 0 ? gaugeArcPath(GAUGE_CX, GAUGE_CY, GAUGE_R, GAUGE_START_ANGLE, startAngle) : "",
    );
  }
  if (after) {
    after.setAttribute(
      "d",
      entryEndMin < WINDOW_MIN ? gaugeArcPath(GAUGE_CX, GAUGE_CY, GAUGE_R, endAngle, GAUGE_END_ANGLE) : "",
    );
  }

  if (sub) sub.textContent = `${entryStartMin} - ${entryEndMin} min`;
}

function updateSessionGauge() {
  const now = new Date();
  const elapsedSec = (now.getMinutes() % WINDOW_MIN) * 60 + now.getSeconds();
  const elapsedMin = elapsedSec / 60;

  const tip = gaugePolar(GAUGE_CX, GAUGE_CY, NEEDLE_LEN, gaugeValueToAngle(elapsedMin));
  const needle = document.getElementById("gauge-needle");
  if (needle) { needle.setAttribute("x2", tip.x); needle.setAttribute("y2", tip.y); }

  const mm = Math.floor(elapsedSec / 60).toString().padStart(2, "0");
  const ss = (elapsedSec % 60).toString().padStart(2, "0");
  const readout = document.getElementById("gauge-readout");
  if (readout) readout.textContent = `${mm}:${ss}`;
}

// ============================================================
// Monitor: spot-lean gauge (Target price / CF Benchmarks spot / gap %)
// ============================================================
// Semicircle across the TOP of the dial only: left=180deg (red, spot below
// target), top=270deg (target price itself, gap=0), right=360deg (green,
// spot above target) - so the needle visually leans left/right of the
// target price exactly the way strategy.decide_spot_lean_side() decides a
// side. Unlike the session gauge, the numeric scale isn't fixed (a 0.02%
// gap and a 0.30% gap are both plausible) so the +/- range auto-grows to
// fit whatever's actually been seen, instead of clipping the needle.
const SPOTGAUGE_CX = 110, SPOTGAUGE_CY = 110, SPOTGAUGE_R = 78;
const SPOTGAUGE_START_ANGLE = 135;  // matches the session gauge's 270deg sweep for visual consistency
const SPOTGAUGE_END_ANGLE = 405;
const SPOTGAUGE_CENTER_ANGLE = (SPOTGAUGE_START_ANGLE + SPOTGAUGE_END_ANGLE) / 2; // 270 = straight up = target price / gap=0
const SPOTGAUGE_NEEDLE_LEN = SPOTGAUGE_R - 10; // close to the tick radius, so crossing a threshold marker is visually obvious
let spotGaugeMaxPct = 0.05;       // current +/- scale of the dial; only ever grows (see buildSpotGaugeScale)
let spotGaugeThresholdPct = 0.02; // strategy.spot_lean.threshold_pct, mirrored from cfg

// UP/DOWN price gauge + momentum-trend gauge share the spot-lean gauge's
// geometry/constants (SPOTGAUGE_CX/CY/R, spotGaugeValueToAngle, etc.) below.
let momGaugeMaxPct = 0.02;   // current +/- scale of the momentum dial; only ever grows
let momLookbackSec = 30;     // strategy.momentum_filter.lookback_sec, mirrored from cfg
let momPriceHistory = [];    // rolling client-side buffer: [{t: epoch_ms, p: price}, ...]

function spotGaugeValueToAngle(pct, maxPct) {
  const clamped = Math.max(-maxPct, Math.min(maxPct, pct));
  const t = (clamped + maxPct) / (2 * maxPct);
  return SPOTGAUGE_START_ANGLE + t * (SPOTGAUGE_END_ANGLE - SPOTGAUGE_START_ANGLE); // start (bottom-left) -> center (top) -> end (bottom-right)
}

function buildSpotGaugeScale(thresholdPct, lastGapPct) {
  spotGaugeThresholdPct = thresholdPct != null ? thresholdPct : spotGaugeThresholdPct;
  const desiredMax = Math.max(spotGaugeThresholdPct * 5, Math.abs(lastGapPct || 0) * 1.3, 0.05);
  // Hysteresis: only rebuild (and only ever grow) the scale when the desired
  // range meaningfully exceeds the current one, so a normal 1-tick jitter in
  // the live gap doesn't constantly redraw/rescale the whole dial.
  if (desiredMax <= spotGaugeMaxPct * 1.02) return;
  spotGaugeMaxPct = desiredMax;

  const maxPct = spotGaugeMaxPct;
  const decimals = maxPct < 0.05 ? 3 : 2;

  const redTrack = document.getElementById("spotgauge-track-red");
  const greenTrack = document.getElementById("spotgauge-track-green");
  if (!redTrack || !greenTrack) return; // widget not present on this page
  redTrack.setAttribute("d", gaugeArcPath(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, SPOTGAUGE_START_ANGLE, SPOTGAUGE_CENTER_ANGLE));
  greenTrack.setAttribute("d", gaugeArcPath(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, SPOTGAUGE_CENTER_ANGLE, SPOTGAUGE_END_ANGLE));

  const deadzone = document.getElementById("spotgauge-deadzone");
  const dzStart = spotGaugeValueToAngle(-spotGaugeThresholdPct, maxPct);
  const dzEnd = spotGaugeValueToAngle(spotGaugeThresholdPct, maxPct);
  deadzone.setAttribute("d", gaugeArcPath(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, dzStart, dzEnd));

  const ticksGroup = document.getElementById("spotgauge-ticks");
  const numbersGroup = document.getElementById("spotgauge-numbers");
  ticksGroup.innerHTML = "";
  numbersGroup.innerHTML = "";
  const fractions = [-1, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1];
  for (const frac of fractions) {
    const value = frac * maxPct;
    const isMajor = frac === -1 || frac === -0.5 || frac === 0.5 || frac === 1;
    const isCenter = frac === 0;
    const angle = spotGaugeValueToAngle(value, maxPct);
    const outer = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, angle);
    const inner = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R - (isMajor || isCenter ? 12 : 6), angle);
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", outer.x); tick.setAttribute("y1", outer.y);
    tick.setAttribute("x2", inner.x); tick.setAttribute("y2", inner.y);
    tick.setAttribute("class", isCenter ? "spotgauge-tick-center" : (isMajor ? "spotgauge-tick-major" : "spotgauge-tick-minor"));
    ticksGroup.appendChild(tick);

    if (isMajor) {
      const isExtreme = frac === -1 || frac === 1;
      const labelRadius = isExtreme ? SPOTGAUGE_R + 14 : SPOTGAUGE_R - 24;
      const labelPos = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, labelRadius, angle);
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", labelPos.x); text.setAttribute("y", labelPos.y);
      text.setAttribute("class", "spotgauge-number");
      text.textContent = (value > 0 ? "+" : "") + value.toFixed(decimals);
      numbersGroup.appendChild(text);
    }
  }

  const thresholdsGroup = document.getElementById("spotgauge-thresholds");
  thresholdsGroup.innerHTML = "";
  for (const sign of [-1, 1]) {
    const value = sign * spotGaugeThresholdPct;
    const angle = spotGaugeValueToAngle(value, maxPct);
    const outer = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R + 5, angle);
    const inner = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R - 14, angle);
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", outer.x); tick.setAttribute("y1", outer.y);
    tick.setAttribute("x2", inner.x); tick.setAttribute("y2", inner.y);
    tick.setAttribute("class", "spotgauge-threshold-tick");
    thresholdsGroup.appendChild(tick);

    const labelPos = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R + 16, angle);
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", labelPos.x); text.setAttribute("y", labelPos.y);
    text.setAttribute("class", "spotgauge-threshold-label");
    text.textContent = spotGaugeThresholdPct.toFixed(decimals);
    thresholdsGroup.appendChild(text);
  }
}

function fmtUsd(value) {
  return value == null ? "--" : "$" + value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function updateSpotGaugeReadouts(targetPrice, spotPrice, gapPct) {
  const needle = document.getElementById("spotgauge-needle");
  const targetEl = document.getElementById("spotgauge-target");
  const spotEl = document.getElementById("spotgauge-spot");
  const gapEl = document.getElementById("spotgauge-gap");
  if (!needle) return; // widget not present on this page

  if (targetEl) targetEl.textContent = fmtUsd(targetPrice);
  if (spotEl) spotEl.textContent = fmtUsd(spotPrice);

  if (gapPct == null) {
    if (gapEl) { gapEl.textContent = "--"; gapEl.classList.remove("pos", "neg"); }
    needle.classList.remove("signal-up", "signal-down");
    const center = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_NEEDLE_LEN, SPOTGAUGE_CENTER_ANGLE);
    needle.setAttribute("x2", center.x); needle.setAttribute("y2", center.y);
    return;
  }

  const tip = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_NEEDLE_LEN, spotGaugeValueToAngle(gapPct, spotGaugeMaxPct));
  needle.setAttribute("x2", tip.x); needle.setAttribute("y2", tip.y);
  needle.classList.toggle("signal-up", gapPct > spotGaugeThresholdPct);
  needle.classList.toggle("signal-down", gapPct < -spotGaugeThresholdPct);

  if (gapEl) {
    gapEl.textContent = (gapPct > 0 ? "+" : "") + gapPct.toFixed(3) + "%";
    gapEl.classList.toggle("pos", gapPct > 0);
    gapEl.classList.toggle("neg", gapPct < 0);
  }
}

// ============================================================
// Monitor: UP/DOWN price gauge (fixed 0-100c scale)
// ============================================================
function buildUpDownGaugeStatic() {
  const redTrack = document.getElementById("updowngauge-track-red");
  const greenTrack = document.getElementById("updowngauge-track-green");
  if (!redTrack || !greenTrack) return; // widget not present on this page
  redTrack.setAttribute("d", gaugeArcPath(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, SPOTGAUGE_START_ANGLE, SPOTGAUGE_CENTER_ANGLE));
  greenTrack.setAttribute("d", gaugeArcPath(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, SPOTGAUGE_CENTER_ANGLE, SPOTGAUGE_END_ANGLE));

  const ticksGroup = document.getElementById("updowngauge-ticks");
  const numbersGroup = document.getElementById("updowngauge-numbers");
  ticksGroup.innerHTML = "";
  numbersGroup.innerHTML = "";
  // Fixed scale: 0c (fully DOWN-favored) .. 50c (even) .. 100c (fully UP-favored).
  // Reuses spotGaugeValueToAngle's -maxPct..+maxPct mapping with maxPct=50 by
  // centering the cents value on 50 (cents - 50).
  for (const cents of [0, 25, 50, 75, 100]) {
    const pct = cents - 50;
    const isCenter = cents === 50;
    const angle = spotGaugeValueToAngle(pct, 50);
    const outer = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, angle);
    const inner = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R - (isCenter ? 12 : 10), angle);
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", outer.x); tick.setAttribute("y1", outer.y);
    tick.setAttribute("x2", inner.x); tick.setAttribute("y2", inner.y);
    tick.setAttribute("class", isCenter ? "spotgauge-tick-center" : "spotgauge-tick-major");
    ticksGroup.appendChild(tick);

    const isExtreme = cents === 0 || cents === 100;
    const labelRadius = isExtreme ? SPOTGAUGE_R + 14 : SPOTGAUGE_R - 24;
    const labelPos = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, labelRadius, angle);
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", labelPos.x); text.setAttribute("y", labelPos.y);
    text.setAttribute("class", "spotgauge-number");
    text.textContent = cents + "c";
    numbersGroup.appendChild(text);
  }
}

function updateUpDownGauge(upCents, downCents) {
  const needle = document.getElementById("updowngauge-needle");
  const upEl = document.getElementById("updowngauge-up");
  const downEl = document.getElementById("updowngauge-down");
  if (!needle) return; // widget not present on this page

  if (upCents == null) {
    needle.classList.remove("signal-up", "signal-down");
    const center = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_NEEDLE_LEN, SPOTGAUGE_CENTER_ANGLE);
    needle.setAttribute("x2", center.x); needle.setAttribute("y2", center.y);
    if (upEl) upEl.textContent = "--";
    if (downEl) downEl.textContent = "--";
    return;
  }

  const tip = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_NEEDLE_LEN, spotGaugeValueToAngle(upCents - 50, 50));
  needle.setAttribute("x2", tip.x); needle.setAttribute("y2", tip.y);
  needle.classList.toggle("signal-up", upCents > 50);
  needle.classList.toggle("signal-down", upCents < 50);

  if (upEl) upEl.textContent = `UP ${upCents}c`;
  if (downEl) downEl.textContent = downCents != null ? `DOWN ${downCents}c` : "--";
}

// ============================================================
// Monitor: momentum-filter trend gauge (auto-growing %, like spot-lean)
// ============================================================
// Approximates strategy.check_momentum_filter()'s short-term trend using a
// client-side rolling buffer of CF Benchmarks spot samples (same feed the
// spot-lean gauge already reads from /api/live_tick), over the configured
// momentum_filter.lookback_sec window. This mirrors the bot's own
// oldest-vs-newest-in-window comparison, but is computed here in the
// browser rather than read from the bot process, so treat it as indicative
// rather than the exact live value the bot acted on for any given poll.
function buildMomGaugeScale(lastPct) {
  const desiredMax = Math.max(Math.abs(lastPct || 0) * 1.3, 0.02);
  // Hysteresis: only grow the scale, and only when meaningfully exceeded -
  // same rationale as buildSpotGaugeScale (avoid constant redraw on jitter).
  if (desiredMax <= momGaugeMaxPct * 1.02) return;
  momGaugeMaxPct = desiredMax;

  const maxPct = momGaugeMaxPct;
  const decimals = maxPct < 0.05 ? 3 : 2;

  const redTrack = document.getElementById("momgauge-track-red");
  const greenTrack = document.getElementById("momgauge-track-green");
  if (!redTrack || !greenTrack) return; // widget not present on this page
  redTrack.setAttribute("d", gaugeArcPath(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, SPOTGAUGE_START_ANGLE, SPOTGAUGE_CENTER_ANGLE));
  greenTrack.setAttribute("d", gaugeArcPath(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, SPOTGAUGE_CENTER_ANGLE, SPOTGAUGE_END_ANGLE));

  const ticksGroup = document.getElementById("momgauge-ticks");
  const numbersGroup = document.getElementById("momgauge-numbers");
  ticksGroup.innerHTML = "";
  numbersGroup.innerHTML = "";
  for (const frac of [-1, -0.5, 0, 0.5, 1]) {
    const value = frac * maxPct;
    const isCenter = frac === 0;
    const angle = spotGaugeValueToAngle(value, maxPct);
    const outer = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R, angle);
    const inner = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_R - (isCenter ? 12 : 10), angle);
    const tick = document.createElementNS("http://www.w3.org/2000/svg", "line");
    tick.setAttribute("x1", outer.x); tick.setAttribute("y1", outer.y);
    tick.setAttribute("x2", inner.x); tick.setAttribute("y2", inner.y);
    tick.setAttribute("class", isCenter ? "spotgauge-tick-center" : "spotgauge-tick-major");
    ticksGroup.appendChild(tick);

    const isExtreme = frac === -1 || frac === 1;
    const labelRadius = isExtreme ? SPOTGAUGE_R + 14 : SPOTGAUGE_R - 24;
    const labelPos = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, labelRadius, angle);
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", labelPos.x); text.setAttribute("y", labelPos.y);
    text.setAttribute("class", "spotgauge-number");
    text.textContent = (value > 0 ? "+" : "") + value.toFixed(decimals);
    numbersGroup.appendChild(text);
  }
}

function updateMomGaugeReadouts(pct) {
  const needle = document.getElementById("momgauge-needle");
  const pctEl = document.getElementById("momgauge-pct");
  const dirEl = document.getElementById("momgauge-dir");
  if (!needle) return; // widget not present on this page

  if (pct == null) {
    needle.classList.remove("signal-up", "signal-down");
    const center = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_NEEDLE_LEN, SPOTGAUGE_CENTER_ANGLE);
    needle.setAttribute("x2", center.x); needle.setAttribute("y2", center.y);
    if (pctEl) { pctEl.textContent = "--"; pctEl.classList.remove("pos", "neg"); }
    if (dirEl) dirEl.textContent = `${momLookbackSec}s`;
    return;
  }

  const tip = gaugePolar(SPOTGAUGE_CX, SPOTGAUGE_CY, SPOTGAUGE_NEEDLE_LEN, spotGaugeValueToAngle(pct, momGaugeMaxPct));
  needle.setAttribute("x2", tip.x); needle.setAttribute("y2", tip.y);
  needle.classList.toggle("signal-up", pct > 0);
  needle.classList.toggle("signal-down", pct < 0);

  if (pctEl) {
    pctEl.textContent = (pct > 0 ? "+" : "") + pct.toFixed(3) + "%";
    pctEl.classList.toggle("pos", pct > 0);
    pctEl.classList.toggle("neg", pct < 0);
  }
  if (dirEl) dirEl.textContent = `${momLookbackSec}s: ` + (pct > 0 ? "UP" : pct < 0 ? "DOWN" : "FLAT");
}

function updateMomentumBuffer(spot) {
  if (spot == null) return; // no fresh sample this tick - leave the buffer/readout as-is
  const now = Date.now();
  momPriceHistory.push({ t: now, p: spot });
  const cutoff = now - momLookbackSec * 1000;
  momPriceHistory = momPriceHistory.filter((s) => s.t >= cutoff);

  if (momPriceHistory.length < 2) {
    updateMomGaugeReadouts(null);
    return;
  }
  const oldest = momPriceHistory[0].p;
  const newest = momPriceHistory[momPriceHistory.length - 1].p;
  const pct = ((newest - oldest) / oldest) * 100;
  buildMomGaugeScale(pct);
  updateMomGaugeReadouts(pct);
}

async function refreshLiveTickGauges() {
  try {
    const res = await fetch("/api/live_tick");
    if (!res.ok) return;
    const tick = await res.json();
    if (!tick.exists) {
      updateSpotGaugeReadouts(null, null, null);
      updateUpDownGauge(null, null);
      updateMomentumBuffer(null);
      return;
    }
    const target = tick.kalshi_strike_usd;
    const spot = tick.btc_spot_cfbenchmarks;
    const gapPct = (target && spot) ? ((spot - target) / target) * 100 : null;
    if (gapPct != null) buildSpotGaugeScale(spotGaugeThresholdPct, gapPct);
    updateSpotGaugeReadouts(target ?? null, spot ?? null, gapPct);

    updateUpDownGauge(tick.kalshi_up_price_cents ?? null, tick.kalshi_down_price_cents ?? null);
    updateMomentumBuffer(spot ?? null);
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
  if (/SESSION NET/.test(line)) return "line-window";
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
  bindField("sizing_martingale_unit", ["sizing", "martingale_unit"], "number");
  bindField("sizing_max_martingale_steps", ["sizing", "max_martingale_steps"], "number");
  bindField("sizing_max_stake", ["sizing", "max_stake"], "number");
  bindField("sizing_dalembert_unit", ["sizing", "dalembert_unit"], "number");
  // Shared by BOTH "dalembert" and "dalembert_reverse" - see state.py's
  // record_dalembert_result / record_dalembert_reverse_result and
  // config.yaml's sizing.profit_lock_cents / sizing.loss_floor_cents.
  bindField("dal_profit_lock_cents", ["sizing", "profit_lock_cents"], "number");
  bindField("dal_loss_floor_cents", ["sizing", "loss_floor_cents"], "number");
  bindField("am_unit", ["sizing", "anti_martingale", "unit"], "number");
  bindField("am_multiplier", ["sizing", "anti_martingale", "multiplier"], "number");
  bindField("sizing_max_anti_martingale_steps", ["sizing", "max_anti_martingale_steps"], "number");

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

  bindField("lf_min_threshold_pct", ["strategy", "late_fade", "min_threshold_pct"], "number");
  bindField("lf_max_threshold_pct", ["strategy", "late_fade", "max_threshold_pct"], "number");

  bindField("hedge_enabled", ["strategy", "hedge", "enabled"], "toggle");
  bindField("hedge_threshold_pct", ["strategy", "hedge", "threshold_pct"], "number");
  bindField("hedge_max_hedges_per_window", ["strategy", "hedge", "max_hedges_per_window"], "number");
  bindField("hedge_fresh_start_only", ["strategy", "hedge", "fresh_start_only"], "toggle");
  bindField("hedge_net_session_sizing", ["strategy", "hedge", "net_session_sizing"], "toggle");
  bindField("hedge_smart_sizing", ["strategy", "hedge", "smart_sizing"], "toggle");
  bindField("hedge_min_profit_cents", ["strategy", "hedge", "min_profit_cents"], "number");
  bindField("hedge_max_contracts", ["strategy", "hedge", "max_contracts"], "number");

  bindField("lt_enabled", ["live_tick", "enabled"], "toggle");
  bindField("lt_interval_sec", ["live_tick", "interval_sec"], "number");
  bindField("lt_file", ["live_tick", "file"], "text");
  bindField("lt_new_file_per_session", ["live_tick", "new_file_per_session"], "toggle");

  document.querySelectorAll("#envSeg button").forEach((btn) => {
    btn.addEventListener("click", () => {
      currentEnv = btn.dataset.val;
      setPath(cfg, ["kalshi", "base_url"], BASE_URLS[currentEnv]);
      renderEnvSeg();
      onConfigFieldChanged();
    });
  });

  document.querySelectorAll("#martingaleVariantSeg button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setPath(cfg, ["sizing", "martingale_variant"], btn.dataset.val);
      renderMartingaleVariantSeg();
      onConfigFieldChanged();
    });
  });

  document.querySelectorAll("#amVariantSeg button").forEach((btn) => {
    btn.addEventListener("click", () => {
      setPath(cfg, ["sizing", "anti_martingale", "variant"], btn.dataset.val);
      renderAmVariantSeg();
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

function renderMartingaleVariantSeg() {
  const variant = getPath(cfg, ["sizing", "martingale_variant"]) || "multiplier";
  document.querySelectorAll("#martingaleVariantSeg button").forEach((b) => b.classList.toggle("on", b.dataset.val === variant));
}

function renderAmVariantSeg() {
  const variant = getPath(cfg, ["sizing", "anti_martingale", "variant"]) || "plus";
  document.querySelectorAll("#amVariantSeg button").forEach((b) => b.classList.toggle("on", b.dataset.val === variant));
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
  const mode = getPath(cfg, ["sizing", "mode"]);
  const isRecovery = mode === "recovery";
  // "dalembertFields" now holds sizing.dalembert_unit AND the shared
  // profit_lock_cents/loss_floor_cents fields, so it's shown for BOTH
  // dalembert variants - there's no separate dalembert_reverse-only panel
  // anymore.
  const isDalembert = mode === "dalembert" || mode === "dalembert_reverse";
  const isAntiMartingale = mode === "anti_martingale";
  document.getElementById("recoveryFields").classList.toggle("dimmed", !isRecovery);
  document.getElementById("dalembertFields").classList.toggle("dimmed", !isDalembert);
  document.getElementById("antiMartingaleFields").classList.toggle("dimmed", !isAntiMartingale);
  document.getElementById("martingaleFields").style.opacity = (isRecovery || isDalembert || isAntiMartingale) ? "0.4" : "1";
}

function renderHedgeVisibility() {
  const enabled = !!getPath(cfg, ["strategy", "hedge", "enabled"]);
  document.getElementById("hedgeSub").classList.toggle("dimmed", !enabled);
}

function renderDryRunStyling() {
  const dryRun = !!getPath(cfg, ["runtime", "dry_run"]);
  document.getElementById("dryrunCard").classList.toggle("is-live", !dryRun);
}

function renderEnvBadges() {
  const dryRun = !!getPath(cfg, ["runtime", "dry_run"]);
  const modeBadge = document.getElementById("env-mode-badge");
  if (modeBadge) {
    modeBadge.textContent = dryRun ? "DRY RUN" : "LIVE";
    modeBadge.className = "badge " + (dryRun ? "badge-stopped" : "badge-crashed");
  }

  const baseUrl = getPath(cfg, ["kalshi", "base_url"]) || "";
  const isDemo = baseUrl.includes("demo");
  const nameBadge = document.getElementById("env-name-badge");
  if (nameBadge) {
    nameBadge.textContent = isDemo ? "DEMO" : "PRODUCTION";
    nameBadge.className = "badge " + (isDemo ? "badge-stopped" : "badge-crashed");
  }
}

function renderConfigDerived() {
  const baseUrl = getPath(cfg, ["kalshi", "base_url"]) || "";
  currentEnv = baseUrl.includes("demo") ? "demo" : "production";
  renderEnvSeg();
  renderMartingaleVariantSeg();
  renderAmVariantSeg();
  renderModeState();
  renderSizingVisibility();
  renderHedgeVisibility();
  renderDryRunStyling();
  renderEnvBadges();
}

function onConfigFieldChanged() {
  renderMartingaleVariantSeg();
  renderAmVariantSeg();
  renderModeState();
  renderSizingVisibility();
  renderHedgeVisibility();
  renderDryRunStyling();
  renderEnvBadges();
  updateGaugeBand(getPath(cfg, ["strategy", "entry_start_min"]), getPath(cfg, ["strategy", "entry_end_min"]));
  spotGaugeThresholdPct = getPath(cfg, ["strategy", "spot_lean", "threshold_pct"]) ?? spotGaugeThresholdPct;
  buildSpotGaugeScale(spotGaugeThresholdPct, 0);
  momLookbackSec = getPath(cfg, ["strategy", "momentum_filter", "lookback_sec"]) ?? momLookbackSec;
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
  updateGaugeBand(getPath(cfg, ["strategy", "entry_start_min"]), getPath(cfg, ["strategy", "entry_end_min"]));
  spotGaugeThresholdPct = getPath(cfg, ["strategy", "spot_lean", "threshold_pct"]) ?? spotGaugeThresholdPct;
  buildSpotGaugeScale(spotGaugeThresholdPct, 0);
  momLookbackSec = getPath(cfg, ["strategy", "momentum_filter", "lookback_sec"]) ?? momLookbackSec;
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
// Simulator tab
// ============================================================
const SIM_CHART_COUNT = 40;   // settled bets per chart page
let simPnlSeries = [];        // full pnl_series from the last /api/simulator/run: {time, ticker, total_pnl_cents, pnl_delta_cents, contracts, result}
let simChartOffset = 0;       // bets back from the most recent (0 = latest page)

function currentSimChartSlice() {
  if (!simPnlSeries.length) return [];
  const total = simPnlSeries.length;
  const end = Math.max(0, total - simChartOffset);
  const start = Math.max(0, end - SIM_CHART_COUNT);
  return simPnlSeries.slice(start, end);
}

function renderSimChartRange() {
  const rangeEl = document.getElementById("sim-chart-range");
  const prevBtn = document.getElementById("sim-chart-prev");
  const nextBtn = document.getElementById("sim-chart-next");
  if (!simPnlSeries.length) {
    rangeEl.textContent = "Run a simulation to see the P&L chart.";
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    return;
  }
  const total = simPnlSeries.length;
  const end = Math.max(0, total - simChartOffset);
  const start = Math.max(0, end - SIM_CHART_COUNT);
  const slice = simPnlSeries.slice(start, end);
  if (!slice.length) {
    rangeEl.textContent = "No settled bets in this range.";
    prevBtn.disabled = start <= 0;
    nextBtn.disabled = simChartOffset <= 0;
    return;
  }
  const first = new Date(slice[0].time);
  const last = new Date(slice[slice.length - 1].time);
  const fmt = (d) => d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  rangeEl.textContent =
    `${fmt(first)} \u2192 ${fmt(last)}  ` +
    `(bets ${start + 1}-${end} of ${total})`;
  prevBtn.disabled = start <= 0;
  nextBtn.disabled = simChartOffset <= 0;
}

function drawSimChart(slice) {
  const canvas = document.getElementById("sim-chart-canvas");
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 800;
  const cssHeight = canvas.clientHeight || 260;
  canvas.width = Math.round(cssWidth * dpr);
  canvas.height = Math.round(cssHeight * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  if (!slice.length) {
    ctx.fillStyle = "#5C5F66";
    ctx.font = "12px monospace";
    ctx.fillText("No settled bets to chart yet - run a simulation.", 12, cssHeight / 2);
    return;
  }

  const padL = 62, padR = 12, padT = 10, padB = 4;
  const countAreaH = 60;   // bottom strip reserved for the Count bars
  const gapBetween = 14;   // gap between the PnL plot and the Count strip
  const pnlPlotH = Math.max(1, cssHeight - padT - padB - countAreaH - gapBetween);
  const plotW = Math.max(1, cssWidth - padL - padR);

  const pnlValues = slice.map((p) => p.total_pnl_cents / 100);
  const minPnl = Math.min(0, ...pnlValues);
  const maxPnl = Math.max(0, ...pnlValues);
  const padY = (maxPnl - minPnl) * 0.1 || 1;
  const yLo = minPnl - padY, yHi = maxPnl + padY;

  const n = slice.length;
  const xOf = (i) => (n === 1) ? padL + plotW / 2 : padL + (i / (n - 1)) * plotW;
  const yOf = (v) => padT + (1 - (v - yLo) / (yHi - yLo || 1)) * pnlPlotH;

  // Zero line
  ctx.strokeStyle = "#2B2E34";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, yOf(0));
  ctx.lineTo(padL + plotW, yOf(0));
  ctx.stroke();

  // Total PnL curve (from the Simulation Log's "Total PnL", one point per settled bet)
  ctx.strokeStyle = "#E0982F";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  slice.forEach((p, i) => {
    const x = xOf(i);
    const y = yOf(p.total_pnl_cents / 100);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Dot at each settled bet, colored by that bet's own result
  slice.forEach((p, i) => {
    const x = xOf(i);
    const y = yOf(p.total_pnl_cents / 100);
    ctx.beginPath();
    ctx.arc(x, y, 2.5, 0, Math.PI * 2);
    ctx.fillStyle = p.result === "win" ? "#4FAE72" : "#DB5B52";
    ctx.fill();
  });

  ctx.fillStyle = "#8A8D95";
  ctx.font = "10px monospace";
  ctx.fillText(`$${yHi.toFixed(2)}`, 4, padT + 8);
  ctx.fillText(`$${yLo.toFixed(2)}`, 4, padT + pnlPlotH);

  // ---- Count bars (bottom strip): contract count per bet, green=win, red=loss ----
  const countTop = padT + pnlPlotH + gapBetween;
  const maxContracts = Math.max(1, ...slice.map((p) => p.contracts || 0));
  const barW = Math.max(1, (plotW / n) * 0.7);
  const barAreaH = countAreaH - 14;

  ctx.strokeStyle = "#2B2E34";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, countTop + barAreaH);
  ctx.lineTo(padL + plotW, countTop + barAreaH);
  ctx.stroke();

  slice.forEach((p, i) => {
    const x = xOf(i) - barW / 2;
    const h = ((p.contracts || 0) / maxContracts) * barAreaH;
    const y = countTop + barAreaH - h;
    ctx.fillStyle = p.result === "win" ? "#4FAE72" : "#DB5B52";
    ctx.fillRect(x, y, barW, Math.max(1, h));
  });

  ctx.fillStyle = "#8A8D95";
  ctx.font = "10px monospace";
  ctx.fillText("COUNT", 4, countTop + 10);
}

function renderSimChartPage() {
  renderSimChartRange();
  drawSimChart(currentSimChartSlice());
}

function fmtSimDollars(v) {
  return v == null || Number.isNaN(v) ? "-" : "$" + Number(v).toFixed(2);
}

function renderSimStats(stats, finalState) {
  document.getElementById("sim-stat-bets").textContent = stats.bets ?? "-";
  document.getElementById("sim-stat-record").textContent = `${stats.wins ?? 0}-${stats.losses ?? 0}`;
  document.getElementById("sim-stat-winrate").textContent = stats.win_rate_pct != null ? `${stats.win_rate_pct}%` : "-";

  const pnlEl = document.getElementById("sim-stat-pnl");
  pnlEl.textContent = fmtSimDollars(stats.pnl_usd);
  pnlEl.className = "stat-value " + (stats.pnl_usd > 0 ? "pos" : stats.pnl_usd < 0 ? "neg" : "");

  document.getElementById("sim-stat-drawdown").textContent = fmtSimDollars(stats.max_drawdown_usd);

  const cumLossEl = document.getElementById("sim-stat-cumloss");
  cumLossEl.textContent = finalState && finalState.cumulative_loss_cents != null
    ? fmtSimDollars(finalState.cumulative_loss_cents / 100) : "-";

  document.getElementById("sim-stat-stake").textContent =
    finalState && finalState.current_stake != null ? finalState.current_stake : "-";
}

function appendSimLogLines(lines) {
  if (!lines || !lines.length) return;
  const view = document.getElementById("sim-log-view");
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
  if (document.getElementById("sim-autoscroll").checked) {
    view.scrollTop = view.scrollHeight;
  }
}

function wireSimulatorTab() {
  document.getElementById("sim-chart-prev").addEventListener("click", () => {
    if (!simPnlSeries.length) return;
    simChartOffset = Math.min(simPnlSeries.length, simChartOffset + SIM_CHART_COUNT);
    renderSimChartPage();
  });
  document.getElementById("sim-chart-next").addEventListener("click", () => {
    if (!simPnlSeries.length) return;
    simChartOffset = Math.max(0, simChartOffset - SIM_CHART_COUNT);
    renderSimChartPage();
  });

  document.getElementById("btn-run-sim").addEventListener("click", async () => {
    const btn = document.getElementById("btn-run-sim");
    const statusEl = document.getElementById("sim-run-status");
    btn.disabled = true;
    statusEl.textContent = "Running simulation...";
    statusEl.className = "save-status";
    try {
      const res = await fetch("/api/simulator/run", { method: "POST" });
      const data = await res.json();
      if (!data.ok) {
        statusEl.textContent = "Failed: " + (data.error || "unknown error");
        statusEl.className = "save-status err";
        return;
      }
      statusEl.textContent =
        `Done - ${data.windows_simulated} window(s), ${data.segments} segment(s), strategy=${data.strategy}`;
      simPnlSeries = data.pnl_series || [];
      simChartOffset = 0; // jump to the latest data
      renderSimStats(data.stats || {}, data.final_state || {});
      document.getElementById("sim-log-view").innerHTML = "";
      appendSimLogLines(data.log_lines || []);
      renderSimChartPage();
    } catch (e) {
      statusEl.textContent = "Failed: network error";
      statusEl.className = "save-status err";
    } finally {
      btn.disabled = false;
    }
  });

  renderSimChartRange(); // placeholder text until a simulation has actually been run
}

// ============================================================
// Init
// ============================================================
function initApp() {
  wireTopTabs();
  wireSimulatorTab();
  wireConfigFields();
  buildGaugeStatic();
  buildUpDownGaugeStatic();
  loadConfig();
  loadInitialLogs();
  refreshBotStatus();
  refreshState();
  updateSessionGauge();
  refreshLiveTickGauges();

  setInterval(refreshBotStatus, 4000);
  setInterval(refreshState, 4000);
  setInterval(pollLogs, 2000);
  setInterval(updateSessionGauge, 1000);
  setInterval(refreshLiveTickGauges, 2000);
}

checkAuthAndInit();
