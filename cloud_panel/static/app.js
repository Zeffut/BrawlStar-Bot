// Cloud panel — aggregates multiple bot instances.

const REFRESH_MS = 15000;  // structural refresh only; live data comes via SSE
let selectedAccountId = null;
let selectedInstanceId = null;  // null = all
let progressionChart = null;
let winrateChart = null;

// Latest snapshot per instance, kept in sync by SSE.
const SNAPSHOTS = {};
// Map account_tag -> instance_id for fast lookup.
const TAG_TO_INSTANCE = {};

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return await r.json();
}

const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
const fmtDate = ts => new Date(ts * 1000).toLocaleString([], {dateStyle:"short",timeStyle:"short"});
const ago = ts => {
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 60) return s+"s ago";
  if (s < 3600) return Math.floor(s/60)+"m ago";
  if (s < 86400) return Math.floor(s/3600)+"h ago";
  return Math.floor(s/86400)+"d ago";
};
const deltaClass = n => n>0 ? "delta-pos" : n<0 ? "delta-neg" : "";

async function refreshAll() {
  let instances = [], accounts = [], healths = {};
  try {
    [instances, accounts] = await Promise.all([
      api("/api/instances"),
      api("/api/accounts"),
    ]);
    // Fetch live health/worker info for each instance (parallel)
    const hs = await Promise.all(instances.map(i =>
      api(`/api/instances/${i.id}/health`).catch(() => ({connected: false}))
    ));
    instances.forEach((i, idx) => { healths[i.id] = hs[idx]; });
  } catch (e) { return; }

  _lastAccounts = accounts;
  const available = instances.filter(i => i.status === "available" || i.status === "running").length;
  const running = instances.filter(i => i.status === "running").length;
  const totalAccounts = accounts.length;
  document.getElementById("fleet-summary").textContent =
    `${available}/${instances.length} available · ${running} running · ${totalAccounts} accounts`;
  document.getElementById("sidebar-meta").textContent =
    `${available}/${instances.length}`;

  // Build a tree: instances → accounts
  const accountsByInstance = {};
  for (const a of accounts) {
    (accountsByInstance[a.instance_uid] ||= []).push(a);
  }

  const tree = document.getElementById("instances-tree");
  tree.innerHTML = "";

  if (instances.length === 0) {
    tree.innerHTML = `<div class="inst-empty">No instances yet. Start a bot worker to populate.</div>`;
    return;
  }

  for (const inst of instances) {
    const card = document.createElement("div");
    card.className = "instance-card";
    const instAccounts = accountsByInstance[inst.instance_id] || [];
    const isSelected = instAccounts.some(a => a.id === selectedAccountId);
    if (isSelected) card.classList.add("selected");

    const health = healths[inst.id] || {};
    const liveBrawl = health.brawlstars_pid ? "running" : "off";
    const battery = health.battery != null ? `${health.battery}%` : "—";
    const ramFree = health.ram_free_mb != null ? `${Math.round(health.ram_free_mb/1024*10)/10} GB` : "—";

    // Head
    const head = document.createElement("div");
    head.className = "instance-head";
    const statusLabel = inst.status === "running" ? "running"
                      : inst.status === "available" ? "available"
                      : "offline";
    head.innerHTML = `
      <span class="inst-dot ${inst.status}"></span>
      <div class="inst-main">
        <div class="inst-name">${inst.name || inst.instance_id}</div>
        <div class="inst-id">${inst.instance_id} · ${ago(inst.last_seen_at)}</div>
      </div>
      <span class="inst-status-pill ${inst.status}">
        ${statusLabel}
      </span>
    `;
    card.appendChild(head);

    // Activity (only if WS connected)
    if (health.connected) {
      const act = document.createElement("div");
      act.className = "inst-activity";
      act.innerHTML = `
        <div class="inst-activity-row">
          <span class="label">📱 ${health.model || 'device'}</span>
          <span class="value">${health.android ? 'Android ' + health.android : ''}</span>
        </div>
        <div class="inst-activity-row">
          <span class="label">🔋 Battery</span>
          <span class="value">${battery}</span>
        </div>
        <div class="inst-activity-row">
          <span class="label">🎮 Brawl Stars</span>
          <span class="value ${liveBrawl === 'running' ? 'delta-pos' : ''}">${liveBrawl}</span>
        </div>
        <div class="inst-activity-row">
          <span class="label">🧠 RAM free</span>
          <span class="value">${ramFree}</span>
        </div>
      `;
      card.appendChild(act);
    }

    // Accounts list
    if (instAccounts.length) {
      const acctBox = document.createElement("div");
      acctBox.className = "inst-accounts";
      for (const a of instAccounts) {
        const row = document.createElement("div");
        row.className = "account-row";
        if (a.id === selectedAccountId) row.classList.add("selected");
        row.innerHTML = `
          <span class="a-name">${a.name || a.tag}</span>
          <span class="a-tag">#${a.tag}</span>
        `;
        row.onclick = () => selectAccount(a.id);
        acctBox.appendChild(row);
      }
      card.appendChild(acctBox);
    } else {
      const empty = document.createElement("div");
      empty.className = "inst-empty";
      empty.textContent = "no account detected yet";
      card.appendChild(empty);
    }

    tree.appendChild(card);
  }

  if (!selectedAccountId && accounts.length) selectAccount(accounts[0].id);
}

async function selectAccount(id) {
  if (selectedAccountId !== id) {
    if (progressionChart) { progressionChart.destroy(); progressionChart = null; }
    if (winrateChart)     { winrateChart.destroy();     winrateChart = null; }
  }
  selectedAccountId = id;
  document.getElementById("empty-state").hidden = true;
  document.getElementById("detail-content").hidden = false;
  for (const li of document.querySelectorAll("#accounts li"))
    li.classList.toggle("active", parseInt(li.dataset.id) === id);
  // Kick off the brawlers fetch immediately so the dropdown fills ASAP
  // (in parallel with the heavier account-detail call).
  gcLoadBrawlers();
  await refreshDetail();
}

async function refreshDetail() {
  if (!selectedAccountId) return;
  let acc, matches;
  try {
    [acc, matches] = await Promise.all([
      api(`/api/accounts/${selectedAccountId}`),
      api(`/api/accounts/${selectedAccountId}/matches?limit=200`),
    ]);
  } catch (e) { return; }
  refreshSessionState();
  gcRefreshAll();
  gcLoadBrawlers();

  document.getElementById("acc-name").textContent = acc.name || acc.tag;
  document.getElementById("acc-tag").textContent = `#${acc.tag}`;
  document.getElementById("acc-instance").textContent =
    `on ${acc.instance_name || acc.instance_uid}`;

  const totalWins = matches.filter(m=>m.result==="victory").length;
  const wr = matches.length ? Math.round(totalWins/matches.length*100)+"%" : "—";
  document.getElementById("kpi-sessions").textContent = (acc.sessions||[]).length;
  document.getElementById("kpi-matches").textContent = matches.length;
  document.getElementById("kpi-wr").textContent = wr;
  document.getElementById("kpi-seen").textContent = ago(acc.last_seen_at);

  const ordered = [...matches].reverse().filter(m=>m.account_trophies_after!=null);
  renderProgression(
    ordered.map(m => fmtTime(m.timestamp)),
    ordered.map(m => m.account_trophies_after),
    ordered.map(m => m.brawler),
  );
  renderWinRate(acc.win_rate_by_brawler || []);

  const mt = document.querySelector("#matches-table tbody"); mt.innerHTML = "";
  for (const m of matches.slice(0, 30)) {
    const d = (m.trophies_after ?? 0) - (m.trophies_before ?? 0);
    mt.innerHTML += `<tr><td>${fmtTime(m.timestamp)}</td><td>${m.brawler}</td>
      <td class="result-${m.result}">${m.result}</td>
      <td class="${deltaClass(d)}">${d>=0?'+':''}${d}</td>
      <td>${m.trophies_before} → ${m.trophies_after}</td></tr>`;
  }
  const st = document.querySelector("#sessions-table tbody"); st.innerHTML = "";
  for (const s of acc.sessions || []) {
    const d = (s.end_trophies!=null && s.start_trophies!=null) ? s.end_trophies - s.start_trophies : null;
    st.innerHTML += `<tr><td>${fmtDate(s.started_at)}</td><td>${s.brawler}</td>
      <td>${s.target_trophies}</td>
      <td class="${d!=null?deltaClass(d):''}">${d!=null ? (d>=0?'+':'')+d : '—'}</td>
      <td>${s.status}</td></tr>`;
  }
}

function renderProgression(labels, trophies, brawlers) {
  if (!progressionChart) {
    progressionChart = new Chart(document.getElementById("chart-progression"), {
      type: "line",
      data: { labels: [], datasets: [{ label: "Trophies", data: [],
        borderColor: "#4f8cf0", backgroundColor: "rgba(79,140,240,0.12)",
        borderWidth: 2, tension: 0.3, pointRadius: 0, pointHoverRadius: 5,
        pointHoverBackgroundColor: "#6ea4ff", fill: true }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false }, tooltip: { backgroundColor: "#11161f", borderColor: "#2a3445", borderWidth: 1, titleColor: "#f1f4f9", bodyColor: "#c4ccd8", padding: 10, displayColors: false } },
        scales: { x: {grid:{color:"rgba(255,255,255,.03)"},ticks:{color:"#7a8597",font:{family:"Inter",size:10},maxRotation:0,autoSkipPadding:20},border:{display:false}},
                  y: {grid:{color:"rgba(255,255,255,.03)"},ticks:{color:"#7a8597",font:{family:"Inter",size:10}},border:{display:false}} } }
    });
  }
  progressionChart.data.labels = labels;
  progressionChart.data.datasets[0].data = trophies;
  progressionChart.update("none");
}

function renderWinRate(data) {
  const sorted = data.slice().sort((a,b)=>b.total-a.total).slice(0,8);
  if (!winrateChart) {
    winrateChart = new Chart(document.getElementById("chart-winrate"), {
      type: "bar",
      data: { labels: [], datasets: [
        { label: "Wins",   data: [], backgroundColor: "#22c55e", borderRadius: 4 },
        { label: "Losses", data: [], backgroundColor: "#ef4444", borderRadius: 4 },
        { label: "Draws",  data: [], backgroundColor: "#f59e0b", borderRadius: 4 },
      ]},
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: "#c4ccd8", font: { family: "Inter", size: 11 }, boxWidth: 10, padding: 12 } },
                   tooltip: { backgroundColor: "#11161f", borderColor: "#2a3445", borderWidth: 1, titleColor: "#f1f4f9", bodyColor: "#c4ccd8", padding: 10 } },
        scales: { x: {stacked:true,grid:{color:"rgba(255,255,255,.03)"},ticks:{color:"#7a8597",font:{family:"Inter",size:10}},border:{display:false}},
                  y: {stacked:true,grid:{color:"rgba(255,255,255,.03)"},ticks:{color:"#7a8597",font:{family:"Inter",size:10}},border:{display:false}} } }
    });
  }
  winrateChart.data.labels = sorted.map(d=>d.brawler);
  winrateChart.data.datasets[0].data = sorted.map(d=>d.wins);
  winrateChart.data.datasets[1].data = sorted.map(d=>d.losses);
  winrateChart.data.datasets[2].data = sorted.map(d=>d.draws);
  winrateChart.update("none");
}

// ----------------- device console (opt-in) -----------------

let selectedInstanceForDevice = null;
let deviceTimer = null;
let deviceConsoleOpen = false;

function openDeviceConsole() {
  document.getElementById("device-panel").hidden = false;
  deviceConsoleOpen = true;
  refreshDevicePanel();           // load immediately on open
  if (deviceTimer) clearInterval(deviceTimer);
  deviceTimer = setInterval(refreshDevicePanel, 5000);
}
function closeDeviceConsole() {
  document.getElementById("device-panel").hidden = true;
  deviceConsoleOpen = false;
  if (deviceTimer) { clearInterval(deviceTimer); deviceTimer = null; }
}

async function refreshDevicePanel() {
  if (!selectedAccountId || !deviceConsoleOpen) return;
  // The device panel is keyed by the instance that owns the selected account.
  let acc;
  try { acc = await api(`/api/accounts/${selectedAccountId}`); } catch (e) { return; }
  // Need the instance DB id — accounts response gives instance_id (uid) and instance_name.
  // We look it up via /api/instances.
  let instances;
  try { instances = await api("/api/instances"); } catch (e) { return; }
  const inst = instances.find(i => i.instance_id === acc.instance_uid);
  if (!inst) { document.getElementById("device-panel").hidden = true; return; }
  selectedInstanceForDevice = inst.id;
  document.getElementById("device-panel").hidden = false;

  // Fetch health and logs only (screenshot is on-demand via "↻ refresh now").
  const [healthRes, logsRes] = await Promise.all([
    api(`/api/instances/${inst.id}/health`).catch(() => ({connected:false})),
    api(`/api/instances/${inst.id}/logs?limit=120`).catch(() => []),
  ]);
  const shotRes = {available: false};  // skip auto fetch

  // WS status indicator
  const wsEl = document.getElementById("ws-status");
  if (healthRes.connected) {
    wsEl.textContent = "● online";
    wsEl.className = "ws-status connected";
  } else {
    wsEl.textContent = "● offline (worker WS not connected)";
    wsEl.className = "ws-status offline";
  }

  // Screen
  const screenEl = document.getElementById("device-screen");
  const ageEl = document.getElementById("screen-age");
  if (shotRes.available) {
    const mime = shotRes.mime || "image/png";
    const b64 = shotRes.b64 || shotRes.png_b64;
    screenEl.src = `data:${mime};base64,${b64}`;
  } else if (!screenEl.src) {
    ageEl.textContent = "click ↻ refresh now to capture a frame";
  }

  // Health
  const hEl = document.getElementById("device-health");
  hEl.innerHTML = "";
  const fields = [
    ["Model", healthRes.model],
    ["Android", healthRes.android],
    ["Battery", healthRes.battery != null ? healthRes.battery + "%" : null],
    ["Temp", healthRes.battery_temp_c != null ? healthRes.battery_temp_c + "°C" : null],
    ["RAM free", healthRes.ram_free_mb != null ? healthRes.ram_free_mb + " MB" : null],
    ["Storage free", healthRes.storage_free_mb != null ? Math.round(healthRes.storage_free_mb/1024) + " GB" : null],
    ["Brawl Stars", healthRes.brawlstars_pid ? "running ✓" : "off"],
    ["Uptime", healthRes.uptime_s != null ? Math.floor(healthRes.uptime_s/60) + " min" : null],
  ];
  for (const [label, value] of fields) {
    if (value == null) continue;
    const item = document.createElement("div");
    item.className = "h-item";
    item.innerHTML = `<label>${label}</label><div>${value}</div>`;
    hEl.appendChild(item);
  }

  // Logs
  const logsEl = document.getElementById("device-logs");
  const logsCount = document.getElementById("logs-count");
  logsCount.textContent = `(${logsRes.length} lines)`;
  // Only scroll-to-bottom if user is already at the bottom (avoid yanking while reading)
  const atBottom = logsEl.scrollTop + logsEl.clientHeight + 10 >= logsEl.scrollHeight;
  logsEl.textContent = logsRes.map(e => e.line).join("\n");
  if (atBottom) logsEl.scrollTop = logsEl.scrollHeight;
}

async function sendDeviceCmd(cmd, confirmMsg) {
  if (!selectedInstanceForDevice) return;
  if (confirmMsg && !(await showConfirm({title: "Confirmer", body: confirmMsg, kind: "danger"}))) return;
  const btns = document.querySelectorAll('.control-grid button');
  btns.forEach(b => b.disabled = true);
  const status = document.getElementById("cmd-status");
  status.textContent = `Running ${cmd}…`;
  try {
    const r = await fetch(`/api/instances/${selectedInstanceForDevice}/cmd`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: cmd}),
    });
    const j = await r.json();
    if (r.ok && j.ok) {
      status.textContent = `✓ ${cmd} OK`;
    } else {
      status.textContent = `✗ ${j.error || r.statusText}`;
    }
  } catch (e) {
    status.textContent = "✗ " + e.message;
  } finally {
    btns.forEach(b => b.disabled = false);
    setTimeout(() => status.textContent = "", 3000);
    refreshDevicePanel();
  }
}

document.querySelectorAll('.control-grid button').forEach(btn => {
  btn.addEventListener("click", () =>
    sendDeviceCmd(btn.dataset.cmd, btn.dataset.confirm || null));
});

// Manual screenshot refresh — triggers an on-demand capture.
document.getElementById("refresh-screen-btn").addEventListener("click", async () => {
  if (!selectedInstanceForDevice) return;
  const btn = document.getElementById("refresh-screen-btn");
  btn.disabled = true; btn.textContent = "↻ capturing…";
  try {
    const r = await api(`/api/instances/${selectedInstanceForDevice}/screenshot?refresh=true`);
    if (r.available) {
      const mime = r.mime || "image/png";
      const b64 = r.b64 || r.png_b64;
      document.getElementById("device-screen").src = `data:${mime};base64,${b64}`;
      document.getElementById("screen-age").textContent = "last frame just now";
    }
  } catch (e) {
    document.getElementById("screen-age").textContent = "refresh failed: " + e.message;
  } finally {
    btn.disabled = false; btn.textContent = "↻ refresh now";
  }
});

// Hook the device console open button (created in detail-header).
document.addEventListener("click", e => {
  if (e.target && e.target.id === "open-device-console") openDeviceConsole();
});

// ----------------- bot session control -----------------

async function refreshSessionState() {
  if (!selectedAccountId) return;
  const banner = document.getElementById("session-banner");
  const btnPush = document.getElementById("btn-push-max");
  const btnStop = document.getElementById("btn-stop");
  try {
    const r = await api(`/api/accounts/${selectedAccountId}/session_state`);
    if (r.ok && r.data && r.data.ok && r.data.state && r.data.state.active) {
      const s = r.data.state;
      const brawlers = s.brawlers || [];
      const total = brawlers.length;
      const done = brawlers.filter(b => b.exhausted).length;
      const current = brawlers.find(b => !b.exhausted);
      banner.hidden = false;
      banner.innerHTML = `
        <span class="dot"></span>
        <span><strong>Push Max running</strong> · ${current ? current.name + ' (' + current.trophies + ' 🏆)' : 'rotating'} · ${done}/${total} done${s.summary ? ' · ' + s.summary : ''}</span>
      `;
      btnPush.hidden = true;
      btnStop.hidden = false;
    } else {
      banner.hidden = true;
      btnPush.hidden = false;
      btnStop.hidden = true;
    }
  } catch (e) {
    banner.hidden = true;
    btnPush.hidden = false;
    btnStop.hidden = true;
  }
}

async function postSession(path, body) {
  const r = await fetch(`/api/accounts/${selectedAccountId}${path}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : "{}",
  });
  return r.json();
}

document.getElementById("btn-push-max").addEventListener("click", () =>
  withLoader("btn-push-max", async () => {
    if (!selectedAccountId) return;
    const r = await postSession("/push_max");
    if (!r.ok || (r.data && !r.data.ok)) {
      alert("Push Max failed: " + (r.data?.error || r.error || "unknown"));
    }
    refreshSessionState();
  }));

document.getElementById("btn-stop").addEventListener("click", () =>
  withLoader("btn-stop", async () => {
    if (!selectedAccountId) return;
    if (!(await showConfirm({
      title: "Arrêter la session ?",
      body: "Le bot va terminer le match en cours puis s'arrêter.",
      confirmText: "Arrêter", kind: "danger",
    }))) return;
    await postSession("/stop", {force: false});
    refreshSessionState();
  }));

// ----------------- Custom confirm modal -----------------

/**
 * Promise-based confirm() replacement. Resolves true/false.
 * Options: {title, body, confirmText, cancelText, kind: 'primary'|'danger'}
 */
function showConfirm(opts) {
  if (typeof opts === "string") opts = {body: opts};
  const {
    title = "Confirmer",
    body = "",
    confirmText = "Confirmer",
    cancelText = "Annuler",
    kind = "primary",
  } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    overlay.innerHTML = `
      <div class="confirm-dialog">
        <h3 class="confirm-title"></h3>
        <p class="confirm-body"></p>
        <div class="confirm-actions">
          <button class="confirm-btn" data-act="cancel"></button>
          <button class="confirm-btn ${kind}" data-act="ok"></button>
        </div>
      </div>`;
    overlay.querySelector(".confirm-title").textContent = title;
    overlay.querySelector(".confirm-body").textContent = body;
    overlay.querySelector('[data-act="cancel"]').textContent = cancelText;
    overlay.querySelector('[data-act="ok"]').textContent = confirmText;
    const close = (val) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(val); };
    const onKey = (e) => {
      if (e.key === "Escape") close(false);
      if (e.key === "Enter") close(true);
    };
    overlay.addEventListener("click", e => {
      if (e.target === overlay) close(false);
      else if (e.target.dataset.act === "cancel") close(false);
      else if (e.target.dataset.act === "ok") close(true);
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    overlay.querySelector('[data-act="ok"]').focus();
  });
}

// ----------------- Loading spinner helper -----------------

/**
 * Wrap a button as "loading" while an async action runs.
 * Preserves the original label (wraps it in a span on first call).
 */
async function withLoader(btn, fn) {
  if (typeof btn === "string") btn = document.getElementById(btn);
  if (!btn) return await fn();
  // Ensure label is wrapped so the spinner overlay hides text but keeps width.
  if (!btn.querySelector(".btn-label")) {
    btn.innerHTML = `<span class="btn-label">${btn.innerHTML}</span>`;
  }
  btn.classList.add("btn-loading");
  btn.disabled = true;
  try {
    return await fn();
  } finally {
    btn.classList.remove("btn-loading");
    btn.disabled = false;
  }
}

// ----------------- Game Control -----------------

async function gcCall(method, path, body) {
  const r = await fetch(`/api/accounts/${selectedAccountId}/game${path}`, {
    method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
}

let _gcResultTimer = null;
function gcSetResult(text, kind) {
  const el = document.getElementById("gc-result");
  el.textContent = text;
  el.className = "gc-result " + (kind || "");
  if (_gcResultTimer) { clearTimeout(_gcResultTimer); _gcResultTimer = null; }
  // Auto-clear: ok/run after 4s, errors after 10s so the user has time to read.
  if (!text) return;
  const delay = kind === "err" ? 10000 : 4000;
  _gcResultTimer = setTimeout(() => {
    el.classList.add("fading");
    setTimeout(() => {
      el.textContent = "";
      el.className = "gc-result";
    }, 350);
  }, delay);
}

// Tracks which accounts have already had their one-shot preview fetched
// in this page session (cleared on full reload).
const _previewFetched = new Set();

async function gcRefreshAll() {
  // State + trophies arrive automatically via SSE snapshots every 10s.
  // We only need to populate from cached snapshot when an account is
  // first selected, and fetch the brawler name (not in the snapshot).
  if (!selectedAccountId) return;
  const acc = _lastAccounts.find(a => a.id === selectedAccountId);
  const snap = acc ? SNAPSHOTS[acc.instance_uid] : null;
  if (snap) {
    if (snap.state) document.getElementById("gc-state").textContent = "state: " + snap.state;
    if (snap.trophies != null) document.getElementById("gc-trophies").textContent = snap.trophies + " 🏆";
  } else {
    document.getElementById("gc-state").textContent = "state: —";
  }
  // Current brawler is not reliably extractable via OCR from the lobby
  // (the name isn't rendered as text). We display the last brawler
  // chosen in the dropdown instead.
  const dropdownVal = document.getElementById("gc-brawler-select")?.value;
  const el = document.getElementById("gc-current-brawler");
  if (el) el.textContent = dropdownVal || "— (use dropdown)";
  // One-shot screenshot preview per account, only the first time it's selected.
  if (!_previewFetched.has(selectedAccountId)) {
    _previewFetched.add(selectedAccountId);
    gcCaptureScreenshot();  // fire-and-forget, runs under the existing loader
  }
}

async function gcCaptureScreenshot() {
  if (!selectedAccountId) return;
  await withLoader("gc-capture", async () => {
    const r = await gcCall("GET", "/screenshot");
    if (r?.ok && r.data?.b64) {
      document.getElementById("gc-screenshot").src = `data:${r.data.mime};base64,${r.data.b64}`;
      const cap = r.data.capture_ms ?? "?";
      document.getElementById("gc-screen-meta").textContent =
        `${r.data.w}×${r.data.h} · capture ${cap}ms · ${new Date().toLocaleTimeString()}`;
    } else {
      document.getElementById("gc-screen-meta").textContent = "capture failed";
    }
  });
}

function _renderBrawlers(brawlers, refreshedAt) {
  const sel = document.getElementById("gc-brawler-select");
  const previouslySelected = sel.value;  // preserve user's choice across re-renders
  sel.innerHTML = '<option value="">— current —</option>';
  const sorted = (brawlers || []).slice().sort((a, b) => (b.trophies || 0) - (a.trophies || 0));
  for (const b of sorted) {
    const o = document.createElement("option");
    o.value = b.name;
    o.textContent = b.trophies != null ? `${b.name} (${b.trophies} 🏆)` : b.name;
    sel.appendChild(o);
  }
  // Restore previously-selected brawler if it's still in the list.
  if (previouslySelected && Array.from(sel.options).some(o => o.value === previouslySelected)) {
    sel.value = previouslySelected;
  }
  const meta = document.getElementById("gc-brawlers-meta");
  if (meta) {
    if (!brawlers || !brawlers.length) {
      meta.textContent = "no brawlers cached yet";
    } else {
      const age = refreshedAt ? Math.round((Date.now()/1000) - refreshedAt) : null;
      meta.textContent = `${brawlers.length} brawlers · refreshed ${age != null ? agoLabel(age) : '—'}`;
    }
  }
}

function agoLabel(s) {
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s/60) + "m ago";
  if (s < 86400) return Math.floor(s/3600) + "h ago";
  return Math.floor(s/86400) + "d ago";
}

async function gcLoadBrawlers() {
  if (!selectedAccountId) return;
  // Show last-known list immediately from localStorage, then update from API.
  const cacheKey = `brawlers:${selectedAccountId}`;
  try {
    const cached = JSON.parse(localStorage.getItem(cacheKey) || "null");
    if (cached?.brawlers?.length) {
      _renderBrawlers(cached.brawlers, cached.refreshed_at);
      if (cached.total_trophies != null) _renderAuthoritativeTrophies(cached.total_trophies);
    }
  } catch (e) {}
  try {
    const r = await api(`/api/accounts/${selectedAccountId}/brawlers`);
    if (r?.brawlers?.length) {
      _renderBrawlers(r.brawlers, r.refreshed_at);
      if (r.total_trophies != null) _renderAuthoritativeTrophies(r.total_trophies);
      localStorage.setItem(cacheKey, JSON.stringify({
        brawlers: r.brawlers, refreshed_at: r.refreshed_at, total_trophies: r.total_trophies,
      }));
    }
  } catch (e) {}
}

// Use the brawlace total as the authoritative trophy display.
// When OCR diverges by more than a few trophies, show both.
function _renderAuthoritativeTrophies(cloudTotal) {
  const el = document.getElementById("gc-trophies");
  if (!el) return;
  // Find current OCR-read value if displayed.
  const ocrText = el.textContent;
  const m = ocrText.match(/(\d+)/);
  const ocrVal = m ? parseInt(m[1]) : null;
  if (ocrVal != null && Math.abs(ocrVal - cloudTotal) > 5) {
    el.innerHTML = `${cloudTotal} 🏆 <span style="color:var(--muted);font-size:12px">(OCR: ${ocrVal})</span>`;
  } else {
    el.textContent = `${cloudTotal} 🏆`;
  }
}

async function gcRefreshBrawlers() {
  if (!selectedAccountId) return;
  await withLoader("gc-refresh-brawlers", async () => {
    gcSetResult("Refreshing brawlers via brawlace (5-15 s)…", "run");
    try {
      const r = await fetch(`/api/accounts/${selectedAccountId}/brawlers/refresh`, {method: "POST"});
      const j = await r.json();
      if (j.ok) {
        _renderBrawlers(j.brawlers, j.refreshed_at);
        gcSetResult(`Refreshed: ${j.brawlers.length} brawlers`, "ok");
      } else {
        gcSetResult("Failed: " + (j.detail || "unknown"), "err");
      }
    } catch (e) {
      gcSetResult("Error: " + e.message, "err");
    }
  });
}

document.getElementById("gc-refresh-state").addEventListener("click", () =>
  withLoader("gc-refresh-state", gcRefreshAll));
document.getElementById("gc-refresh-brawlers").addEventListener("click", gcRefreshBrawlers);
document.getElementById("gc-capture").addEventListener("click", gcCaptureScreenshot);

document.getElementById("gc-goto-lobby").addEventListener("click", () =>
  withLoader("gc-goto-lobby", async () => {
    if (!selectedAccountId) return;
    gcSetResult("Returning to lobby…", "run");
    const r = await gcCall("POST", "/goto_lobby");
    gcSetResult(r?.ok ? "Lobby ✓" : "Failed: " + (r?.error || "unknown"),
                r?.ok ? "ok" : "err");
    gcRefreshAll();
  }));

document.getElementById("gc-play-one").addEventListener("click", () =>
  withLoader("gc-play-one", async () => {
    if (!selectedAccountId) return;
    if (!(await showConfirm({
      title: "Lancer une partie ?",
      body: "Le bot va sélectionner le brawler choisi, lancer un match et jouer jusqu'à la fin.",
      confirmText: "▶ Lancer",
    }))) return;
    const brawler = document.getElementById("gc-brawler-select").value || null;
    gcSetResult("Match in progress (this can take 3-5 min)…", "run");
    try {
      const r = await gcCall("POST", "/play_one_match", {brawler, timeout_s: 420});
      if (r?.ok && r.data?.ok) {
        const d = r.data;
        gcSetResult(`Match done · brawler=${d.brawler} · W:${d.wins} L:${d.losses} D:${d.draws} · ${d.duration_s}s`, "ok");
      } else {
        gcSetResult("Failed: " + (r?.data?.error || r?.error || "unknown"), "err");
      }
    } catch (e) {
      gcSetResult("Error: " + e.message, "err");
    }
    gcRefreshAll();
  }));

setInterval(refreshAll, REFRESH_MS);
refreshAll();

// ----------------- SSE live stream -----------------
//
// Single long-lived EventSource. Every worker snapshot lands here and
// updates the relevant DOM bits without any polling.

let _sse = null;
function startSSE() {
  try { if (_sse) _sse.close(); } catch (e) {}
  _sse = new EventSource("/api/events");
  _sse.addEventListener("message", (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.type === "snapshot") onSnapshot(m);
    else if (m.type === "brawlers_refreshed") {
      // Reload brawlers list if the refreshed account is selected.
      const acc = _lastAccounts.find(a => a.id === m.account_id);
      if (acc && acc.id === selectedAccountId) gcLoadBrawlers();
    }
  });
  _sse.addEventListener("error", () => {
    // EventSource auto-reconnects by itself, just log.
    console.warn("SSE disconnected, reconnecting…");
  });
}

function onSnapshot(snap) {
  SNAPSHOTS[snap.instance_id] = snap;
  if (snap.account_tag) TAG_TO_INSTANCE[snap.account_tag] = snap.instance_id;

  // Update Game Control panel if the currently selected account belongs
  // to this instance.
  const acc = _lastAccounts.find(a => a.id === selectedAccountId);
  if (acc && acc.instance_uid === snap.instance_id) {
    if (snap.state) document.getElementById("gc-state").textContent = "state: " + snap.state;
    if (snap.trophies != null) document.getElementById("gc-trophies").textContent = snap.trophies + " 🏆";
  }

  // Update sidebar pills (running/available transitions) — cheap.
  const dotOrPill = document.querySelector(`[data-instance="${snap.instance_id}"] .inst-dot`);
  // (Sidebar full redraw happens via refreshAll; this is just live state hints.)
}

// Cache last accounts list (set by refreshAll) so SSE handler can correlate.
let _lastAccounts = [];

startSSE();
