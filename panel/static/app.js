// Polling-based dashboard. SSE/WebSocket can come later if needed.

const REFRESH_MS = 3000;
let selectedAccountId = null;
let progressionChart = null;
let winrateChart = null;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return await r.json();
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}
function deltaClass(n) {
  if (n > 0) return "delta-pos";
  if (n < 0) return "delta-neg";
  return "";
}

async function refreshAccounts() {
  let accounts = [];
  try { accounts = await api("/api/accounts"); } catch (e) { return; }

  const fleet = await api("/api/fleet").catch(() => ({}));
  document.getElementById("fleet-summary").textContent =
    `${fleet.running_workers || 0} / ${fleet.total_accounts || 0} workers running`;

  const ul = document.getElementById("accounts");
  ul.innerHTML = "";
  for (const acc of accounts) {
    const li = document.createElement("li");
    li.dataset.id = acc.id;
    if (acc.id === selectedAccountId) li.classList.add("active");
    const running = acc.worker && acc.worker.running;
    li.innerHTML = `
      <div class="name"><span class="status-dot ${running ? "running" : "stopped"}"></span>${acc.name || acc.tag}</div>
      <div class="meta">#${acc.tag}${acc.worker && acc.worker.current_brawler ? " • " + acc.worker.current_brawler : ""}</div>
    `;
    li.onclick = () => selectAccount(acc.id);
    ul.appendChild(li);
  }
  if (!selectedAccountId && accounts.length) selectAccount(accounts[0].id);
}

async function selectAccount(id) {
  if (selectedAccountId !== id) {
    // Drop existing chart instances so they're rebuilt for the new account.
    if (progressionChart) { progressionChart.destroy(); progressionChart = null; }
    if (winrateChart) { winrateChart.destroy(); winrateChart = null; }
  }
  selectedAccountId = id;
  document.getElementById("empty-state").hidden = true;
  document.getElementById("detail-content").hidden = false;
  for (const li of document.querySelectorAll("#accounts li")) {
    li.classList.toggle("active", parseInt(li.dataset.id) === id);
  }
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

  const w = acc.worker || {};
  document.getElementById("kpi-status").textContent = w.running ? "RUNNING" : "stopped";
  document.getElementById("kpi-status").style.color = w.running ? "#4ade80" : "#6b7686";
  document.getElementById("kpi-brawler").textContent = w.current_brawler || "—";
  document.getElementById("kpi-trophies").textContent = w.current_trophies ?? "—";
  const delta = (w.current_trophies != null && w.initial_trophies)
    ? (w.current_trophies - w.initial_trophies) : null;
  const dEl = document.getElementById("kpi-delta");
  dEl.textContent = delta != null ? (delta >= 0 ? "+" : "") + delta : "—";
  dEl.className = delta != null ? deltaClass(delta) : "";
  document.getElementById("kpi-matches").textContent = w.matches_in_session ?? 0;
  const wr = w.matches_in_session ? (w.wins / w.matches_in_session * 100).toFixed(0) + "%" : "—";
  document.getElementById("kpi-wr").textContent = wr;

  // Controls
  const ctrl = document.getElementById("acc-controls");
  if (w.running) {
    ctrl.innerHTML = `
      <button onclick="stopWorker(${acc.id})">Stop (soft)</button>
      <button class="danger" onclick="forceStopWorker(${acc.id})">Force stop</button>`;
  } else {
    ctrl.innerHTML = `
      <button class="primary" onclick="startWorker(${acc.id})">Start…</button>
      <button onclick="pushMax(${acc.id})" title="Push every brawler to plateau">🚀 Push max</button>
    `;
  }

  // Progression chart: account-wide trophy total over time (not per-brawler).
  const ordered = [...matches].reverse().filter(m => m.account_trophies_after != null);
  const labels = ordered.map(m => fmtTime(m.timestamp));
  const trophies = ordered.map(m => m.account_trophies_after);
  renderProgression(labels, trophies, ordered.map(m => m.brawler));

  // Win rate by brawler
  renderWinRate(acc.win_rate_by_brawler || []);

  // Matches table
  const mtbody = document.querySelector("#matches-table tbody");
  mtbody.innerHTML = "";
  for (const m of matches.slice(0, 30)) {
    const delta = (m.trophies_after ?? 0) - (m.trophies_before ?? 0);
    mtbody.innerHTML += `
      <tr>
        <td>${fmtTime(m.timestamp)}</td>
        <td>${m.brawler}</td>
        <td class="result-${m.result}">${m.result}</td>
        <td class="${deltaClass(delta)}">${delta >= 0 ? "+" : ""}${delta}</td>
        <td>${m.trophies_before} → ${m.trophies_after}</td>
      </tr>`;
  }

  // Sessions table
  const stbody = document.querySelector("#sessions-table tbody");
  stbody.innerHTML = "";
  for (const s of acc.sessions || []) {
    const sDelta = (s.end_trophies != null && s.start_trophies != null)
      ? s.end_trophies - s.start_trophies : null;
    stbody.innerHTML += `
      <tr>
        <td>${fmtDate(s.started_at)}</td>
        <td>${s.brawler}</td>
        <td>${s.target_trophies}</td>
        <td class="${sDelta != null ? deltaClass(sDelta) : ''}">${sDelta != null ? (sDelta >= 0 ? '+' : '') + sDelta : '—'}</td>
        <td>${s.status}</td>
      </tr>`;
  }
}

function renderProgression(labels, trophies, brawlers) {
  // Create the chart once, then update its data in place to avoid the
  // flicker that comes from destroy()+new on every poll.
  if (!progressionChart) {
    const ctx = document.getElementById("chart-progression");
    progressionChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          label: "Trophies (after match)",
          data: [],
          borderColor: "#4f8cf0",
          backgroundColor: "rgba(79, 140, 240, 0.1)",
          tension: 0.2,
          pointRadius: 3,
          fill: true,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => {
                const i = items[0].dataIndex;
                const lbls = progressionChart.data.labels;
                const bs = progressionChart._brawlers || [];
                return `${lbls[i]}${bs[i] ? " — " + bs[i] : ""}`;
              },
            },
          },
        },
        scales: {
          x: { grid: { color: "#1a1f29" }, ticks: { color: "#8b95a5", maxRotation: 0, autoSkipPadding: 20 } },
          y: { grid: { color: "#1a1f29" }, ticks: { color: "#8b95a5" } },
        },
      },
    });
  }
  progressionChart.data.labels = labels;
  progressionChart.data.datasets[0].data = trophies;
  progressionChart._brawlers = brawlers;
  progressionChart.update("none");  // no animation = no flicker
}

function renderWinRate(data) {
  const sorted = data.slice().sort((a, b) => b.total - a.total).slice(0, 8);
  if (!winrateChart) {
    const ctx = document.getElementById("chart-winrate");
    winrateChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: [],
        datasets: [
          { label: "Wins", data: [], backgroundColor: "#4ade80" },
          { label: "Losses", data: [], backgroundColor: "#ef4444" },
          { label: "Draws", data: [], backgroundColor: "#fbbf24" },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { labels: { color: "#c0c8d4" } } },
        scales: {
          x: { stacked: true, grid: { color: "#1a1f29" }, ticks: { color: "#8b95a5" } },
          y: { stacked: true, grid: { color: "#1a1f29" }, ticks: { color: "#8b95a5" } },
        },
      },
    });
  }
  winrateChart.data.labels = sorted.map(d => d.brawler);
  winrateChart.data.datasets[0].data = sorted.map(d => d.wins);
  winrateChart.data.datasets[1].data = sorted.map(d => d.losses);
  winrateChart.data.datasets[2].data = sorted.map(d => d.draws);
  winrateChart.update("none");
}

// ------------------- start-cycle modal -------------------

let modalState = { accountId: null, brawlers: [] };

async function startWorker(accountId) {
  modalState = { accountId, brawlers: [] };
  document.getElementById("start-modal").hidden = false;
  document.getElementById("brawler-grid").innerHTML =
    `<div class="muted">Loading brawlers…</div>`;
  try {
    const brawlers = await api(`/api/accounts/${accountId}/brawlers`);
    modalState.brawlers = brawlers;
    renderBrawlerGrid();
  } catch (e) {
    document.getElementById("brawler-grid").innerHTML =
      `<div class="muted">Failed to load: ${e.message}</div>`;
  }
}

function renderBrawlerGrid() {
  const grid = document.getElementById("brawler-grid");
  grid.innerHTML = "";
  const list = modalState.brawlers.slice().sort((a, b) => b.trophies - a.trophies);
  for (const b of list) {
    const card = document.createElement("div");
    card.className = "brawler-card";
    card.innerHTML = `
      <img src="${b.image_url}" alt="${b.name}" onerror="this.style.opacity=0.2" />
      <div class="b-name">${b.name}</div>
      <div class="b-trophies">🏆 ${b.trophies}</div>
    `;
    card.onclick = () => launchBrawler(b, card);
    grid.appendChild(card);
  }
}

async function launchBrawler(b, card) {
  const target = parseInt(document.getElementById("target-input").value, 10);
  if (!target || target <= 0) { alert("Enter a valid target."); return; }
  card.style.opacity = "0.5";
  card.style.pointerEvents = "none";
  try {
    await api(`/api/accounts/${modalState.accountId}/start`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ brawler: b.name, target }),
    });
    closeStartModal();
    refreshDetail();
  } catch (e) {
    alert("Failed: " + e.message);
    card.style.opacity = ""; card.style.pointerEvents = "";
  }
}

function setTarget(n) {
  document.getElementById("target-input").value = n;
}

function closeStartModal() {
  document.getElementById("start-modal").hidden = true;
}

// Close on backdrop click or ESC.
document.getElementById("start-modal").addEventListener("click", e => {
  if (e.target.id === "start-modal") closeStartModal();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeStartModal();
});

async function pushMax(accountId) {
  if (!confirm("Start push-max mode? Bot will rotate through ALL owned brawlers, swapping after 3 consecutive defeats.")) return;
  try {
    const r = await api(`/api/accounts/${accountId}/push_max`, { method: "POST" });
    alert(r.msg || "Push-max started.");
  } catch (e) { alert("Failed: " + e.message); }
  refreshDetail();
}

async function stopWorker(id) {
  const r = await api(`/api/accounts/${id}/stop`, {method: "POST"});
  alert(r.msg); refreshDetail();
}

async function forceStopWorker(id) {
  const r = await api(`/api/accounts/${id}/forcestop`, {method: "POST"});
  alert(r.msg); refreshDetail();
}

// ---------------- alerts config ----------------

const ALERT_LABELS = {
  match: ["Match result", "Sent after every match"],
  target_reached: ["Target reached", "Trophy goal hit"],
  cycle_started: ["Cycle started", "Bot started a new push session"],
  cycle_started_no_ocr: ["Cycle started (no OCR)", "Trophy read failed at start"],
  stop_during_init: ["Stopped during init", "Force-stop before first match"],
};

async function renderAlerts() {
  const cfg = await api("/api/alerts");
  const container = document.getElementById("alerts-config");
  container.innerHTML = "";
  for (const [event, [title, desc]] of Object.entries(ALERT_LABELS)) {
    const entry = cfg[event] || {};
    const row = document.createElement("div");
    row.className = "alert-row";
    row.dataset.evt = event;
    let filterHtml = "";
    if (event === "match") {
      const f = entry.filter || {};
      filterHtml = `
        <div class="filter">
          Filter:
          <label><input type="checkbox" data-filter="victory" ${f.victory !== false ? "checked" : ""}> victory</label>
          <label><input type="checkbox" data-filter="defeat" ${f.defeat !== false ? "checked" : ""}> defeat</label>
          <label><input type="checkbox" data-filter="draw" ${f.draw !== false ? "checked" : ""}> draw</label>
        </div>`;
    }
    row.innerHTML = `
      <div class="name">${title}<small>${desc}</small></div>
      <div class="toggle">
        <label><input type="checkbox" data-toggle ${entry.enabled ? "checked" : ""}></label>
      </div>
      <div>
        <textarea data-template>${(entry.template || "").replace(/</g, "&lt;")}</textarea>
        ${filterHtml}
        <div class="save-status muted" data-status></div>
      </div>
    `;
    container.appendChild(row);
    // Auto-save: on checkbox change AND on textarea blur (so people
    // editing the template can finish typing first).
    row.querySelector("[data-toggle]").addEventListener("change", () => saveAlert(event));
    row.querySelector("[data-template]").addEventListener("blur", () => saveAlert(event));
    row.querySelectorAll("[data-filter]").forEach(el => {
      el.addEventListener("change", () => saveAlert(event));
    });
  }
}

async function saveAlert(event) {
  const row = document.querySelector(`.alert-row[data-evt="${event}"]`);
  if (!row) return;
  const enabled = row.querySelector(`[data-toggle]`).checked;
  const template = row.querySelector(`[data-template]`).value;
  const filterInputs = row.querySelectorAll(`[data-filter]`);
  const filter = {};
  filterInputs.forEach(i => filter[i.dataset.filter] = i.checked);
  const body = { enabled, template };
  if (filterInputs.length) body.filter = filter;
  const status = row.querySelector("[data-status]");
  status.textContent = "Saving…";
  try {
    await api(`/api/alerts/${event}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    status.textContent = "✓ Saved";
    setTimeout(() => { status.textContent = ""; }, 1500);
  } catch (e) {
    status.textContent = "✗ " + e.message;
  }
}

function openSettings() {
  document.getElementById("settings-modal").hidden = false;
  renderAlerts();
}
function closeSettings() {
  document.getElementById("settings-modal").hidden = true;
}
document.getElementById("settings-modal").addEventListener("click", e => {
  if (e.target.id === "settings-modal") closeSettings();
});

// Polling loop
setInterval(refreshAccounts, REFRESH_MS);
setInterval(refreshDetail, REFRESH_MS);
refreshAccounts();
