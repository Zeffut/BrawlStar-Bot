// Cloud panel — aggregates multiple bot instances.

const REFRESH_MS = 5000;
let selectedAccountId = null;
let selectedInstanceId = null;  // null = all
let progressionChart = null;
let winrateChart = null;

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

  const online = instances.filter(i => i.fresh).length;
  const totalAccounts = accounts.length;
  document.getElementById("fleet-summary").textContent =
    `${online}/${instances.length} online · ${totalAccounts} accounts`;
  document.getElementById("sidebar-meta").textContent =
    `${online}/${instances.length}`;

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
    head.innerHTML = `
      <span class="inst-dot ${inst.fresh ? 'online' : ''}"></span>
      <div class="inst-main">
        <div class="inst-name">${inst.name || inst.instance_id}</div>
        <div class="inst-id">${inst.instance_id} · ${ago(inst.last_seen_at)}</div>
      </div>
      <span class="inst-status-pill ${inst.fresh ? 'online' : ''}">
        ${inst.fresh ? 'online' : 'offline'}
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
        borderColor: "#4f8cf0", backgroundColor: "rgba(79,140,240,0.1)",
        tension: 0.2, pointRadius: 3, fill: true }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false } },
        scales: { x: {grid:{color:"#1a1f29"},ticks:{color:"#8b95a5",maxRotation:0,autoSkipPadding:20}},
                  y: {grid:{color:"#1a1f29"},ticks:{color:"#8b95a5"}} } }
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
        { label: "Wins",   data: [], backgroundColor: "#4ade80" },
        { label: "Losses", data: [], backgroundColor: "#ef4444" },
        { label: "Draws",  data: [], backgroundColor: "#fbbf24" },
      ]},
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { labels: { color: "#c0c8d4" } } },
        scales: { x: {stacked:true,grid:{color:"#1a1f29"},ticks:{color:"#8b95a5"}},
                  y: {stacked:true,grid:{color:"#1a1f29"},ticks:{color:"#8b95a5"}} } }
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

  // Fetch health, screenshot, logs in parallel
  const [healthRes, shotRes, logsRes] = await Promise.all([
    api(`/api/instances/${inst.id}/health`).catch(() => ({connected:false})),
    api(`/api/instances/${inst.id}/screenshot`).catch(() => ({available:false})),
    api(`/api/instances/${inst.id}/logs?limit=120`).catch(() => []),
  ]);

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
    const b64 = shotRes.b64 || shotRes.png_b64;  // backward compat
    screenEl.src = `data:${mime};base64,${b64}`;
    const age = shotRes.age_s ?? 0;
    ageEl.textContent = `last frame ${age.toFixed(1)} s ago — auto every 15s`;
  } else {
    ageEl.textContent = "no screenshot yet";
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
  if (confirmMsg && !confirm(confirmMsg)) return;
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

setInterval(refreshAll, REFRESH_MS);
refreshAll();
