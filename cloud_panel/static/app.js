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

// Track last failure per endpoint so silent background polls don't spam toasts.
const _failedRecently = new Set();

async function api(path, opts = {}) {
  const {silent = false, method = "GET", body = null} = opts;
  try {
    const init = {method};
    if (body !== null) {
      init.headers = {"Content-Type": "application/json"};
      init.body = JSON.stringify(body);
    }
    const r = await fetch(path, init);
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      const msg = `${method} ${path} → HTTP ${r.status}${detail ? ': ' + detail.slice(0, 120) : ''}`;
      if (!silent && !_failedRecently.has(path)) {
        _failedRecently.add(path);
        setTimeout(() => _failedRecently.delete(path), 5000);
        showToast(msg, "err");
      }
      throw new Error(msg);
    }
    _failedRecently.delete(path);
    return await r.json();
  } catch (e) {
    if (!silent && e.name === "TypeError" && !_failedRecently.has(path)) {
      // Network error (cloud down). Show once.
      _failedRecently.add(path);
      setTimeout(() => _failedRecently.delete(path), 5000);
      showToast("Cloud unreachable — retrying…", "err");
    }
    throw e;
  }
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
      api("/api/instances", {silent: true}),
      api("/api/accounts", {silent: true}),
    ]);
    // Fetch live health/worker info for each instance (parallel)
    const hs = await Promise.all(instances.map(i =>
      api(`/api/instances/${i.id}/health`, {silent: true}).catch(() => ({connected: false}))
    ));
    instances.forEach((i, idx) => { healths[i.id] = hs[idx]; });
  } catch (e) { return; }

  _lastAccounts = accounts;
  const available = instances.filter(i => i.status === "available" || i.status === "running").length;
  document.getElementById("sidebar-meta").textContent =
    `${available}/${instances.length}`;
  // Fleet overview KPIs (one-shot aggregated endpoint).
  api("/api/fleet/overview", {silent: true}).then(o => {
    if (!o) return;
    const b = o.instances_by_status || {};
    document.getElementById("fkpi-inst").textContent =
      `${o.instances_total} (${b.running||0}▶ ${b.available||0}✓ ${b.stale||0}⚠ ${b.offline||0}○)`;
    document.getElementById("fkpi-sess").textContent = o.active_sessions ?? "0";
    document.getElementById("fkpi-troph").textContent = (o.total_trophies ?? 0).toLocaleString();
    const t = o.today || {};
    document.getElementById("fkpi-wld").textContent =
      `${t.victory||0}/${t.defeat||0}/${t.draw||0}`;
    const wrEl = document.getElementById("fkpi-wr");
    if (t.win_rate_pct != null) {
      wrEl.textContent = t.win_rate_pct + "%";
      wrEl.className = "fkpi-v " + (t.win_rate_pct >= 50 ? "green" : "red");
    } else {
      wrEl.textContent = "—"; wrEl.className = "fkpi-v muted";
    }
  }).catch(() => {});

  // Build a tree: instances → accounts
  const accountsByInstance = {};
  for (const a of accounts) {
    (accountsByInstance[a.instance_uid] ||= []).push(a);
  }

  const tree = document.getElementById("instances-tree");
  tree.innerHTML = "";

  if (instances.length === 0) {
    tree.innerHTML = `
      <div class="empty-card">
        <span class="icon">📭</span>
        <div>No instances yet.</div>
        <div style="margin-top:6px;font-size:11px">Start a bot worker (HP or other host) and it'll appear here within 30s.</div>
      </div>`;
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
        const running = a.session_running ? '<span class="a-running" title="session active"></span>' : '';
        const troph = a.total_trophies != null ? `<span class="a-troph">${a.total_trophies} 🏆</span>` : '';
        row.innerHTML = `
          ${running}
          <div class="a-main">
            <span class="a-name">${a.name || a.tag}</span>
            <span class="a-tag">#${a.tag}</span>
          </div>
          ${troph}
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
  loadAlerts();

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

// Tracks whether a grinding session is active for the selected account.
let _sessionActive = false;

// Buttons that mustn't run during a session (could derail the bot).
const SESSION_GUARDED_BUTTONS = [
  "gc-play-one", "gc-goto-lobby", "gc-refresh-state",
  "gc-refresh-brawlers", "gc-capture",
];
const SESSION_GUARDED_SELECTS = ["gc-brawler-select"];

function _applySessionGuards(active) {
  _sessionActive = active;
  for (const id of SESSION_GUARDED_BUTTONS) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.disabled = active;
    el.title = active ? "Désactivé : session de grind en cours" : el.dataset.origTitle || "";
    if (!el.dataset.origTitle && el.title && !active) el.dataset.origTitle = el.title;
  }
  for (const id of SESSION_GUARDED_SELECTS) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.disabled = active;
    el.title = active ? "Désactivé : session de grind en cours" : "";
  }
  // Push Max button stays visible-but-disabled when active (Stop is shown).
}

async function refreshSessionState() {
  if (!selectedAccountId) return;
  const banner = document.getElementById("session-banner");
  const btnPush = document.getElementById("btn-push-max");
  const btnStop = document.getElementById("btn-stop");
  try {
    const r = await api(`/api/accounts/${selectedAccountId}/session_state`, {silent: true});
    if (r.ok && r.data && r.data.ok && r.data.state && r.data.state.active) {
      const s = r.data.state;
      const brawlers = s.brawlers || [];
      const total = brawlers.length;
      const done = brawlers.filter(b => b.exhausted).length;
      const current = brawlers.find(b => !b.exhausted);
      const targetTxt = s.target_total_trophies
        ? ` · target ${s.target_total_trophies} 🏆`
        : "";
      banner.hidden = false;
      banner.innerHTML = `
        <span class="dot"></span>
        <span><strong>Push Max running</strong> · ${current ? current.name + ' (' + current.trophies + ' 🏆)' : 'rotating'} · ${done}/${total} done${targetTxt}${s.summary ? ' · ' + s.summary : ''}</span>
      `;
      btnPush.hidden = true;
      btnStop.hidden = false;
      _applySessionGuards(true);
    } else {
      banner.hidden = true;
      btnPush.hidden = false;
      btnStop.hidden = true;
      _applySessionGuards(false);
    }
  } catch (e) {
    banner.hidden = true;
    btnPush.hidden = false;
    btnStop.hidden = true;
    _applySessionGuards(false);
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

async function askPushMaxTarget() {
  // Suggest current total + 200 as a reasonable next goal.
  let suggested = 0;
  try {
    const r = await api(`/api/accounts/${selectedAccountId}/brawlers`, {silent: true});
    suggested = (r?.total_trophies || 0) + 200;
  } catch (e) {}
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    overlay.innerHTML = `
      <div class="confirm-dialog">
        <h3 class="confirm-title">Push Max — Objectif</h3>
        <p class="confirm-body">Le bot va rotater les brawlers et s'arrêter quand l'objectif est atteint OU quand tous les brawlers sont au max.</p>
        <div style="margin-bottom:18px">
          <label style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:6px">Total trophies cible</label>
          <input type="number" id="pm-target" min="1" value="${suggested}" style="width:100%;background:var(--surface-2);border:1px solid var(--border-2);color:var(--text);padding:10px 12px;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:14px" />
        </div>
        <div class="confirm-actions">
          <button class="confirm-btn" data-act="cancel">Annuler</button>
          <button class="confirm-btn primary" data-act="ok">▶ Lancer</button>
        </div>
      </div>`;
    const close = (val) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(val); };
    const onKey = (e) => {
      if (e.key === "Escape") close(null);
      if (e.key === "Enter") {
        const v = parseInt(overlay.querySelector("#pm-target").value, 10);
        close(Number.isFinite(v) && v > 0 ? v : null);
      }
    };
    overlay.addEventListener("click", e => {
      if (e.target === overlay) close(null);
      else if (e.target.dataset.act === "cancel") close(null);
      else if (e.target.dataset.act === "ok") {
        const v = parseInt(overlay.querySelector("#pm-target").value, 10);
        close(Number.isFinite(v) && v > 0 ? v : null);
      }
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    setTimeout(() => overlay.querySelector("#pm-target").select(), 50);
  });
}

document.getElementById("btn-push-max").addEventListener("click", async () => {
  if (!selectedAccountId) return;
  const target = await askPushMaxTarget();
  if (!target) return;
  await withLoader("btn-push-max", async () => {
    const r = await postSession("/push_max", {target_total_trophies: target});
    if (!r.ok || (r.data && !r.data.ok)) {
      showToast("Push Max failed: " + (r.data?.error || r.error || "unknown"), "err");
    } else {
      showToast(`Push Max démarré — objectif ${target} 🏆`, "ok");
    }
    refreshSessionState();
  });
});

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

// Floating toast notifications (bottom-right). Stack newest at the bottom.
// `opts`: {sticky: bool} → if true, toast never auto-dismisses (caller
// must call .dismiss() or update via .update(text)).
function showToast(text, kind, opts = {}) {
  if (!text) return;
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = "toast " + (kind || "");
  const icons = { ok: "✓", err: "✗", run: "⟳" };
  toast.innerHTML = `
    <span class="toast-icon">${icons[kind] || "•"}</span>
    <span class="toast-text"></span>
    <button class="toast-close" title="dismiss">×</button>`;
  toast.querySelector(".toast-text").textContent = text;
  stack.appendChild(toast);
  const dismiss = () => {
    if (!toast.parentNode) return;
    toast.classList.add("fading");
    setTimeout(() => toast.remove(), 320);
  };
  toast.querySelector(".toast-close").addEventListener("click", dismiss);
  toast.dismiss = dismiss;
  toast.update = (newText, newKind) => {
    toast.querySelector(".toast-text").textContent = newText;
    if (newKind) toast.className = "toast " + newKind;
  };
  if (!opts.sticky) {
    const delay = kind === "err" ? 10000 : kind === "run" ? 8000 : 4000;
    setTimeout(dismiss, delay);
  }
  return toast;
}

// Backwards-compat alias: existing call sites still use gcSetResult().
function gcSetResult(text, kind) {
  return showToast(text, kind);
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
  // Note: current_brawler isn't reliably OCR-able from the lobby (no
  // text on screen). The dropdown selection is the source of truth.
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
    const r = await api(`/api/accounts/${selectedAccountId}/brawlers`, {silent: true});
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
    const brawler = document.getElementById("gc-brawler-select").value || null;
    if (!(await showConfirm({
      title: "Lancer une partie ?",
      body: `Brawler: ${brawler || "current"} · Mode: Brawl Ball. Le bot va passer en Brawl Ball si besoin, jouer le match et revenir au lobby.`,
      confirmText: "▶ Lancer",
    }))) return;
    // Sticky toast with live elapsed counter (match takes 3-5 min).
    const startedAt = Date.now();
    const toast = showToast("Match starting…", "run", {sticky: true});
    const tick = setInterval(() => {
      const s = Math.floor((Date.now() - startedAt) / 1000);
      const mm = String(Math.floor(s / 60)).padStart(2, "0");
      const ss = String(s % 60).padStart(2, "0");
      toast?.update(`Match in progress · ${mm}:${ss}`, "run");
    }, 1000);
    try {
      const r = await gcCall("POST", "/play_one_match",
                              {brawler, timeout_s: 420, required_mode: "brawlball"});
      clearInterval(tick);
      toast?.dismiss();
      if (r?.ok && r.data?.ok) {
        const d = r.data;
        showToast(`Match done · ${d.brawler} · W:${d.wins} L:${d.losses} D:${d.draws} · ${d.duration_s}s`, "ok");
      } else {
        showToast("Failed: " + (r?.data?.error || r?.error || "unknown"), "err");
      }
    } catch (e) {
      clearInterval(tick);
      toast?.dismiss();
      showToast("Error: " + e.message, "err");
    }
    gcRefreshAll();
  }));

setInterval(refreshAll, REFRESH_MS);
refreshAll();

// Keep session-active guards fresh even when the user is idle on the panel.
setInterval(() => {
  if (selectedAccountId) refreshSessionState();
}, 10000);

// ----------------- SSE live stream -----------------
//
// Single long-lived EventSource. Every worker snapshot lands here and
// updates the relevant DOM bits without any polling.

let _sse = null;
function _setConnStatus(state, text) {
  const el = document.getElementById("conn-status");
  if (!el) return;
  el.className = "conn-status " + state;
  el.querySelector(".conn-text").textContent = text;
}
function startSSE() {
  try { if (_sse) _sse.close(); } catch (e) {}
  _setConnStatus("connecting", "connecting");
  _sse = new EventSource("/api/events");
  _sse.addEventListener("open", () => _setConnStatus("live", "live"));
  _sse.addEventListener("message", (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.type === "snapshot") onSnapshot(m);
    else if (m.type === "ready") _setConnStatus("live", "live");
    else if (m.type === "brawlers_refreshed") {
      const acc = _lastAccounts.find(a => a.id === m.account_id);
      if (acc && acc.id === selectedAccountId) gcLoadBrawlers();
    } else if (m.type === "match") {
      _prependActivity(m);
      // Trigger brawler list refresh ~5s later (let the bot's match
      // hook + brawlace itself catch up). Background, silent.
      const acc = _lastAccounts.find(a => a.tag === m.tag);
      if (acc) {
        setTimeout(() => {
          fetch(`/api/accounts/${acc.id}/brawlers/refresh`, {method: "POST"})
            .catch(() => {});
        }, 5000);
      }
    }
  });
  _sse.addEventListener("error", () => {
    _setConnStatus("offline", "reconnecting");
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

// ----------------- Activity feed -----------------

function _activityRowHtml(m) {
  const resChar = m.result === "victory" ? "W" : m.result === "defeat" ? "L" : "D";
  const delta = m.delta;
  const deltaTxt = delta == null ? "" : (delta >= 0 ? "+" : "") + delta;
  const deltaCls = delta == null ? "" : delta > 0 ? "pos" : delta < 0 ? "neg" : "";
  const accLabel = m.account_name || m.tag;
  return `
    <div class="activity-row ${m.result}">
      <span class="res">${resChar}</span>
      <div class="who">
        <span class="b">${m.brawler}</span>
        <span class="a">${accLabel}</span>
      </div>
      <span class="delta ${deltaCls}">${deltaTxt}</span>
      <span class="when">${ago(m.timestamp)}</span>
    </div>`;
}

async function loadActivity() {
  try {
    const rows = await api("/api/activity/recent?limit=30", {silent: true});
    const feed = document.getElementById("activity-feed");
    if (!rows || !rows.length) {
      feed.innerHTML = `<div class="empty-card" style="padding:14px">
        <span class="icon">🎮</span>
        <div style="font-size:11px">No matches yet</div>
      </div>`;
      document.getElementById("activity-count").textContent = "0";
      return;
    }
    feed.innerHTML = rows.map(_activityRowHtml).join("");
    document.getElementById("activity-count").textContent = rows.length;
  } catch (e) {}
}

function _prependActivity(m) {
  const feed = document.getElementById("activity-feed");
  if (!feed) return;
  // Remove the empty-state card if present.
  if (feed.querySelector(".empty-card")) feed.innerHTML = "";
  const tmp = document.createElement("div");
  tmp.innerHTML = _activityRowHtml(m).trim();
  feed.insertBefore(tmp.firstChild, feed.firstChild);
  // Cap at 30 rows.
  while (feed.children.length > 30) feed.removeChild(feed.lastChild);
  const countEl = document.getElementById("activity-count");
  if (countEl) countEl.textContent = feed.children.length;
}

loadActivity();

// ----------------- Telegram alerts config -----------------

const ALERT_LABELS = {
  match: "Chaque match",
  target_reached: "Objectif atteint",
  cycle_started: "Début de cycle",
  cycle_started_no_ocr: "Cycle (OCR fail)",
  stop_during_init: "Stop pendant init",
  bot_stuck: "Bot bloqué",
  battery_low: "Batterie faible",
  battery_resumed: "Batterie OK",
  session_ended: "Session terminée",
};

async function loadAlerts() {
  if (!selectedAccountId) return;
  const acc = _lastAccounts.find(a => a.id === selectedAccountId);
  if (!acc) return;
  const instances = await api("/api/instances", {silent: true}).catch(() => []);
  const inst = instances.find(i => i.instance_id === acc.instance_uid);
  if (!inst) return;
  const list = document.getElementById("alerts-list");
  const statusEl = document.getElementById("alerts-status");
  statusEl.textContent = "loading…";
  try {
    const r = await api(`/api/instances/${inst.id}/alerts`, {silent: true});
    const cfg = (r?.ok && r.data) || {};
    const known = Object.keys(ALERT_LABELS);
    list.innerHTML = "";
    const enabled = known.filter(k => cfg[k]?.enabled).length;
    statusEl.textContent = `${enabled}/${known.length} active`;
    for (const event of known) {
      const c = cfg[event] || {enabled: false};
      const row = document.createElement("div");
      row.className = "alert-row";
      row.innerHTML = `
        <label class="toggle">
          <input type="checkbox" ${c.enabled ? "checked" : ""} data-event="${event}">
          <span class="slider"></span>
        </label>
        <div class="name">${ALERT_LABELS[event]}</div>
        <div class="meta">${event}</div>
      `;
      list.appendChild(row);
      row.querySelector("input").addEventListener("change", async (e) => {
        const enabled = e.target.checked;
        try {
          await api(`/api/instances/${inst.id}/alerts`, {
            method: "PUT", body: {event, enabled},
          });
          showToast(`${ALERT_LABELS[event]} : ${enabled ? "ON" : "OFF"}`, "ok");
          // Refresh count.
          const newCount = Array.from(list.querySelectorAll("input"))
            .filter(i => i.checked).length;
          statusEl.textContent = `${newCount}/${known.length} active`;
        } catch (err) {
          e.target.checked = !enabled;  // revert
        }
      });
    }
  } catch (e) {
    list.innerHTML = `<div class="empty-card" style="padding:14px"><div>Failed to load alerts</div></div>`;
    statusEl.textContent = "error";
  }
}

// ----------------- Mobile sidebar toggle + keyboard shortcuts -----------------

document.getElementById("sidebar-toggle")?.addEventListener("click", () => {
  document.getElementById("account-list").classList.toggle("open");
});
// Close sidebar after picking an account on mobile.
document.addEventListener("click", e => {
  if (window.innerWidth > 800) return;
  if (e.target.closest(".account-row")) {
    document.getElementById("account-list")?.classList.remove("open");
  }
});

// Esc dismisses toast stack and closes the device console / sidebar.
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    // Dismiss all toasts
    document.querySelectorAll(".toast").forEach(t => t.remove());
    // Close mobile sidebar
    document.getElementById("account-list")?.classList.remove("open");
    // Close device console
    if (typeof closeDeviceConsole === "function" &&
        !document.getElementById("device-panel").hidden) {
      closeDeviceConsole();
    }
  }
});
