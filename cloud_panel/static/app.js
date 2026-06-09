// Cloud panel — aggregates multiple bot instances.

const REFRESH_MS = 15000;  // structural refresh only; live data comes via SSE
let selectedAccountId = null;
let selectedInstanceId = null;  // null = all
let progressionChart = null;
let winrateChart = null;
let activityChart = null;
let fleetCumChart = null, fleetDailyChart = null, fleetHourlyChart = null;

// Latest snapshot per instance, kept in sync by SSE.
const SNAPSHOTS = {};
// Map account_tag -> instance_id for fast lookup.
const TAG_TO_INSTANCE = {};

// Track last failure per endpoint so silent background polls don't spam toasts.
const _failedRecently = new Set();

// Friendly labels for the raw screen states the worker detects. Used as a
// fallback when the bot hasn't published a rich activity (idle / manual play).
const STATE_LABELS = {
  lobby: "In lobby",
  match: "Playing",
  end: "Match ended",
  brawler_selection: "Selecting brawler",
  star_drop: "Opening Star Drop",
  trophy_reward: "Trophy reward",
  shop: "In shop",
  popup: "Dismissing popup",
  play_store: "Reopening Brawl Stars",
  unknown: "—",
};

// The single source of truth for "what is the bot doing" shown across the UI.
// Prefers the worker-published activity ("Selecting Brock", "Playing as
// Shelly", "Waiting — charging 42%"), then the prettified screen state.
function statusText(snap) {
  if (!snap) return "—";
  if (snap.activity) return snap.activity;
  if (snap.state) return STATE_LABELS[snap.state] || snap.state;
  return "—";
}

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
  const available = instances.filter(i => i.status === "ready" || i.status === "running").length;
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
    card.dataset.instance = inst.instance_id;
    const instAccounts = accountsByInstance[inst.instance_id] || [];
    const isSelected = instAccounts.some(a => a.id === selectedAccountId);
    if (isSelected) card.classList.add("selected");

    const health = healths[inst.id] || {};
    // Distinguish "process alive" from "actually playable":
    //  - foreground = BS app on top, screen unlocked → "running"
    //  - locked     = screen off or keyguard up        → "locked"
    //  - pid only   = process exists in background     → "background"
    //  - no pid                                         → "off"
    let liveBrawl = "off";
    if (health.brawlstars_foreground) liveBrawl = "running";
    else if (health.screen_locked && health.brawlstars_pid) liveBrawl = "locked";
    else if (health.brawlstars_pid) liveBrawl = "background";
    const battery = health.battery != null ? `${health.battery}%` : "—";
    const ramFree = health.ram_free_mb != null ? `${Math.round(health.ram_free_mb/1024*10)/10} GB` : "—";

    // Head
    const head = document.createElement("div");
    head.className = "instance-head";
    const statusLabel = inst.status;  // running/ready/preparing/booting/stale/offline
    const expandedKey = `inst-expanded:${inst.instance_id}`;
    const isExpandedNow = localStorage.getItem(expandedKey) === "1";
    // "What is the bot doing" line — rich activity, else prettified state.
    const doing = inst.activity
      || (inst.game_state ? (STATE_LABELS[inst.game_state] || inst.game_state) : "");
    head.innerHTML = `
      <span class="inst-dot ${inst.status}"></span>
      <div class="inst-main">
        <div class="inst-name">${inst.name || inst.instance_id}</div>
        <div class="inst-id">${inst.instance_id} · ${ago(inst.last_seen_at)}</div>
        <div class="inst-doing"${doing ? "" : " hidden"}>▸ ${doing}</div>
      </div>
      <span class="inst-chevron ${isExpandedNow ? 'expanded' : ''}" aria-hidden="true">▾</span>
      <span class="inst-status-pill ${inst.status}">
        ${statusLabel}
      </span>
    `;
    card.appendChild(head);

    // Activity details are now collapsed by default — click the
    // instance head to expand. Keeps the sidebar compact when you
    // just want to see who's running. Persisted per-instance in
    // localStorage so the user's preference sticks.
    if (health.connected) {
      const storageKey = `inst-expanded:${inst.instance_id}`;
      const expanded = localStorage.getItem(storageKey) === "1";
      const act = document.createElement("div");
      act.className = "inst-activity" + (expanded ? "" : " collapsed");
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
          <span class="value ${liveBrawl === 'running' ? 'delta-pos' : (liveBrawl === 'off' ? '' : 'delta-neg')}">${liveBrawl}</span>
        </div>
        <div class="inst-activity-row">
          <span class="label">🧠 RAM free</span>
          <span class="value">${ramFree}</span>
        </div>
      `;
      card.appendChild(act);
      // Make the header clickable to toggle.
      head.style.cursor = "pointer";
      head.title = expanded ? "Click to hide details" : "Click to show details";
      head.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;  // ignore inner buttons
        const isExpanded = !act.classList.contains("collapsed");
        act.classList.toggle("collapsed", isExpanded);
        localStorage.setItem(storageKey, isExpanded ? "0" : "1");
        head.title = isExpanded ? "Click to show details" : "Click to hide details";
        const chev = head.querySelector(".inst-chevron");
        if (chev) chev.classList.toggle("expanded", !isExpanded);
      });
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
      empty.innerHTML = `
        <div>no account detected yet</div>
        <button class="header-btn" style="margin-top:8px;font-size:11px" data-inst-id="${inst.id}" data-inst-uid="${inst.instance_id}">🔧 Device console</button>`;
      empty.querySelector("button").onclick = (e) => {
        e.stopPropagation();
        openDeviceConsoleForInstance(inst.id, inst.instance_id);
      };
      card.appendChild(empty);
    }

    tree.appendChild(card);
  }

  // Wire the charging guard to whatever instance owns the selected
  // account. When the worker reports battery_paused → status "charging",
  // we lock out all action buttons.
  if (selectedAccountId) {
    const acc = accounts.find(a => a.id === selectedAccountId);
    if (acc) {
      const inst = instances.find(i => i.instance_id === acc.instance_uid);
      _applyChargingGuard(!!(inst && inst.status === "charging"));
    }
  }

  if (!_fleetView && !selectedAccountId && accounts.length) selectAccount(accounts[0].id);
  // Keep the fleet overview fresh while it's the active view.
  if (_fleetView) _loadFleetOverview();
}

// ---- Fleet overview (landing / "🛰️ Flotte" button) ----
// Default landing view: the fleet overview (not an auto-selected account).
let _fleetView = true;
function showFleetOverview() {
  _fleetView = true;
  selectedAccountId = null;
  for (const li of document.querySelectorAll("#accounts li")) li.classList.remove("active");
  document.getElementById("detail-content").hidden = true;
  document.getElementById("empty-state").hidden = false;
  _loadFleetOverview();
}
async function _loadFleetOverview() {
  let d;
  try { d = await api("/api/fleet/overview", {silent: true}); } catch (e) { return; }
  if (!_fleetView) return;
  renderFleetOverview(d);
}
function renderFleetOverview(d) {
  const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  const sgn = (n) => (n > 0 ? "+" : "") + n;
  const st = d.instances_by_status || {};
  const inGame = (st.running || 0);
  set("fl-troph", (d.total_trophies != null ? d.total_trophies.toLocaleString() : "—") + " 🏆");
  set("fl-today", d.net_today != null ? `<span class="${d.net_today>=0?'delta-pos':'delta-neg'}">${sgn(d.net_today)} 🏆</span>` : "—");
  set("fl-tph", d.trophies_per_hour != null ? `<span class="delta-pos">+${d.trophies_per_hour}/h</span>` : "—");
  const t = d.today || {};
  set("fl-wr", t.win_rate_pct != null ? `${t.win_rate_pct}%` : "—");
  set("fl-matches", t.total != null ? t.total : "—");
  set("fl-active", `${inGame} / ${d.instances_total || 0}`);
  set("fl-sessions", d.active_sessions != null ? d.active_sessions : "—");
  set("fl-accounts", d.accounts_total != null ? d.accounts_total : "—");
  set("fleet-updated", "à jour · " + new Date().toLocaleTimeString());
  // Per-account table.
  const tb = document.querySelector("#fleet-accounts-table tbody");
  if (tb) {
    tb.innerHTML = (d.accounts || []).map(a => {
      const stat = a.running ? "running" : (a.status || "offline");
      const today = a.net_today != null
        ? `<span class="${a.net_today>=0?'delta-pos':'delta-neg'}">${sgn(a.net_today)}</span>` : "—";
      const tph = a.trophies_per_hour != null ? `+${a.trophies_per_hour}/h` : "—";
      return `<tr class="fleet-acc-row" data-acc="${a.id}" style="cursor:pointer">
        <td>${a.name}</td>
        <td><span class="inst-status-pill ${stat}">${stat}</span></td>
        <td>${(a.trophies||0).toLocaleString()} 🏆</td>
        <td>${today}</td>
        <td>${a.win_rate!=null?a.win_rate+'%':'—'}</td>
        <td>${tph}</td></tr>`;
    }).join("") || `<tr><td colspan="6" class="muted">Aucun compte</td></tr>`;
    tb.querySelectorAll(".fleet-acc-row").forEach(row =>
      row.addEventListener("click", () => selectAccount(parseInt(row.dataset.acc))));
  }
  renderFleetCharts(d.timeseries || {daily: [], hourly: []}, d.total_trophies || 0);
}

// Create-or-update a themed bar/line chart (shared by the fleet charts).
function _barLine(chart, canvasId, type, labels, datasets) {
  const el = document.getElementById(canvasId);
  if (!el || typeof Chart === "undefined") return chart;
  if (!chart) {
    chart = new Chart(el, { type, data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false },
          tooltip: { backgroundColor: "#11161f", borderColor: "#2a3445", borderWidth: 1,
                     titleColor: "#f1f4f9", bodyColor: "#c4ccd8", padding: 10 } },
        scales: { x: { grid: { color: "rgba(255,255,255,.03)" }, ticks: { color: "#7a8597", font: { family: "Inter", size: 9 }, autoSkip: true, maxRotation: 0 }, border: { display: false } },
                  y: { grid: { color: "rgba(255,255,255,.03)" }, ticks: { color: "#7a8597", font: { family: "Inter", size: 10 }, precision: 0 }, border: { display: false } } } } });
  } else {
    chart.data.labels = labels;
    chart.data.datasets = datasets;
    chart.update("none");
  }
  // Force a re-measure: Chart.js sizes to the container at creation time, but
  // the flex/grid height isn't final yet → it drew short with empty space
  // below. resize() now + on the next frame makes it fill the (tall) card.
  try { chart.resize(); } catch (_) {}
  requestAnimationFrame(() => { try { chart.resize(); } catch (_) {} });
  return chart;
}

function renderFleetCharts(ts, total) {
  const daily = ts.daily || [];
  const labels = daily.map(d => {
    const dt = new Date(d.day * 86400000);
    return ("0"+dt.getDate()).slice(-2) + "/" + ("0"+(dt.getMonth()+1)).slice(-2);
  });
  const nets = daily.map(d => d.net);
  // Cumulative fleet total over time, anchored to the current total (work
  // backward subtracting each day's gain).
  const cum = new Array(daily.length);
  let run = total || 0;
  for (let i = daily.length - 1; i >= 0; i--) { cum[i] = run; run -= nets[i]; }
  fleetCumChart = _barLine(fleetCumChart, "fleet-cum-chart", "line", labels, [{
    label: "Trophées", data: cum, borderColor: "#4f8cf0",
    backgroundColor: "rgba(79,140,240,0.12)", fill: true, tension: 0.3,
    pointRadius: 0, pointHoverRadius: 5, borderWidth: 2 }]);
  fleetDailyChart = _barLine(fleetDailyChart, "fleet-daily-chart", "bar", labels, [{
    label: "Trophées", data: nets,
    backgroundColor: nets.map(n => n >= 0 ? "#22c55e" : "#ef4444"), borderRadius: 3 }]);
  // Hourly (UTC buckets → browser-local hours).
  const h = (ts.hourly && ts.hourly.length === 24) ? ts.hourly : new Array(24).fill(0);
  const off = Math.round(-new Date().getTimezoneOffset() / 60);
  const local = new Array(24).fill(0);
  for (let u = 0; u < 24; u++) local[(((u + off) % 24) + 24) % 24] = h[u];
  const hlabels = local.map((_, i) => String(i).padStart(2, "0") + "h");
  fleetHourlyChart = _barLine(fleetHourlyChart, "fleet-hourly-chart", "bar", hlabels, [{
    label: "Matchs", data: local, backgroundColor: "#4f8cf0", borderRadius: 3 }]);
}
document.getElementById("btn-fleet-overview")?.addEventListener("click", showFleetOverview);
// Brand logo/title = "home" → fleet overview (standard UX reflex).
const _brandEl = document.querySelector(".brand");
if (_brandEl) { _brandEl.style.cursor = "pointer"; _brandEl.title = "Accueil — vue flotte";
                _brandEl.addEventListener("click", showFleetOverview); }

async function selectAccount(id) {
  _fleetView = false;
  if (selectedAccountId !== id) {
    if (progressionChart) { progressionChart.destroy(); progressionChart = null; }
    if (winrateChart)     { winrateChart.destroy();     winrateChart = null; }
    if (activityChart)    { activityChart.destroy();    activityChart = null; }
    _progressionSig = null;   // chart gone → force a re-render for the new account
  }
  selectedAccountId = id;
  document.getElementById("empty-state").hidden = true;
  document.getElementById("detail-content").hidden = false;
  for (const li of document.querySelectorAll("#accounts li"))
    li.classList.toggle("active", parseInt(li.dataset.id) === id);
  // Brawlers come bundled in the dashboard endpoint now — no need
  // for an extra round-trip.
  await refreshDetail();
}

async function refreshDetail() {
  if (!selectedAccountId) return;
  const accId = selectedAccountId;
  // 1. Show last-known snapshot from localStorage IMMEDIATELY so the
  //    user sees something before any network round-trip.
  const cacheKey = `dash:${accId}`;
  try {
    const cached = JSON.parse(localStorage.getItem(cacheKey) || "null");
    if (cached && cached.account && cached.matches) {
      _applyDashboard(cached);
    }
  } catch (e) {}
  // 2. Fetch fresh data in one round-trip (dashboard endpoint).
  let dash;
  try {
    dash = await api(`/api/accounts/${accId}/dashboard?matches_limit=200`);
  } catch (e) { return; }
  if (selectedAccountId !== accId) return;  // user clicked elsewhere
  _applyDashboard(dash);
  try { localStorage.setItem(cacheKey, JSON.stringify(dash)); } catch (e) {}
  // Chart: full history, loaded ONCE per refresh (single source → no flicker).
  _loadProgression(accId);
  // 3. Fire-and-forget secondary calls (alerts, session state, GC).
  refreshSessionState();
  gcRefreshAll();
  // Telegram alerts are GLOBAL now — configured once via the ⚙️ Global Config
  // modal (header), not per-account. (No per-instance alerts panel anymore.)
}

function _applyDashboard(dash) {
  const acc = dash.account;
  const matches = dash.matches || [];
  // Render brawlers from cached payload (saves an extra round-trip).
  if (dash.brawlers && dash.brawlers.list?.length) {
    _renderBrawlers(dash.brawlers.list, dash.brawlers.refreshed_at);
    if (dash.brawlers.total_trophies != null) {
      _renderAuthoritativeTrophies(dash.brawlers.total_trophies);
    }
  }

  document.getElementById("acc-name").textContent = acc.name || acc.tag;
  document.getElementById("acc-tag").textContent = `#${acc.tag}`;
  document.getElementById("acc-instance").textContent =
    `on ${acc.instance_name || acc.instance_uid}`;
  // Remember this account's instance for the main-page live toggle.
  _setMainStreamUid(acc.instance_uid);

  // Use the whole-history stats (real COUNT + win/loss) so the KPIs reflect
  // every match, not just the recent page loaded for the table below.
  const stats = dash.stats || {};
  const totalMatches = stats.total != null ? stats.total : matches.length;
  const totalWins = stats.wins != null
    ? stats.wins
    : matches.filter(m=>m.result==="victory").length;
  const wr = totalMatches ? Math.round(totalWins/totalMatches*100)+"%" : "—";
  document.getElementById("kpi-sessions").textContent = (acc.sessions||[]).length;
  document.getElementById("kpi-matches").textContent = totalMatches;
  document.getElementById("kpi-wr").textContent = wr;
  document.getElementById("kpi-seen").textContent = ago(acc.last_seen_at);
  // Rich analytics (efficiency / performance / activity).
  _renderAnalytics(dash.analytics || {});
  renderActivity((dash.analytics && dash.analytics.hourly) || []);

  // Chart is driven by a SINGLE source — the full downsampled history loaded
  // by _loadProgression (called once from refreshDetail). We deliberately do
  // NOT seed it from the capped dashboard matches here: that produced two
  // renders with different X-axis extents (last-200 vs full) → visible flicker
  // on every match. _loadProgression is idempotent + dedupes redundant renders.
  renderWinRate(acc.win_rate_by_brawler || []);
  loadBrawlerEfficiency(acc.id);

  // Cap displayed rows to keep the page lightweight. The full history
  // is still available via the API; tables also become scrollable
  // (see .list-card max-height in style.css).
  const MAX_SESSIONS_DISPLAYED = 20;

  // Recent matches: render the first page, then lazy-load more on scroll
  // (like the activity feed) instead of capping at a fixed count.
  _matchTotal = totalMatches;
  const mt = document.querySelector("#matches-table tbody");
  const firstPage = matches.slice(0, MATCH_PAGE);
  mt.innerHTML = firstPage.map(_matchRowHtml).join("");
  _matchOffset = firstPage.length;
  _updateTableCount("matches-table", _matchOffset, _matchTotal);
  _bindMatchScroll();

  const st = document.querySelector("#sessions-table tbody"); st.innerHTML = "";
  const allSessions = acc.sessions || [];
  const shownSessions = allSessions.slice(0, MAX_SESSIONS_DISPLAYED);
  for (const s of shownSessions) {
    const d = (s.end_trophies!=null && s.start_trophies!=null) ? s.end_trophies - s.start_trophies : null;
    // push_max uses 99999 as a "no per-session target" sentinel — show it as
    // "Push Max" instead of a meaningless number.
    const tgt = (s.target_trophies == null || s.target_trophies >= 99999)
      ? '<span class="muted">Push Max</span>'
      : `${s.target_trophies} 🏆`;
    const sc = s.status === 'running' ? 'sess-running'
      : (s.status === 'stopped' ? 'sess-stopped' : 'sess-done');
    st.innerHTML += `<tr><td>${fmtDate(s.started_at)}</td><td>${s.brawler}</td>
      <td>${tgt}</td>
      <td class="${d!=null?deltaClass(d):''}">${d!=null ? (d>=0?'+':'')+d : '—'}</td>
      <td><span class="sess-badge ${sc}">${s.status}</span></td></tr>`;
  }
  _updateTableCount("sessions-table", shownSessions.length, allSessions.length);
}

// ---- Recent matches pagination (lazy-load on scroll) ----
const MATCH_PAGE = 50;
let _matchOffset = 0, _matchTotal = 0, _matchLoading = false;

function _matchRowHtml(m) {
  const d = (m.trophies_after ?? 0) - (m.trophies_before ?? 0);
  return `<tr><td>${fmtTime(m.timestamp)}</td><td>${m.brawler}</td>
    <td class="result-${m.result}">${m.result}</td>
    <td class="${deltaClass(d)}">${d>=0?'+':''}${d}</td>
    <td>${m.trophies_before} → ${m.trophies_after}</td></tr>`;
}

async function loadMoreMatches() {
  if (!selectedAccountId || _matchLoading || _matchOffset >= _matchTotal) return;
  _matchLoading = true;
  try {
    const r = await api(
      `/api/accounts/${selectedAccountId}/matches?limit=${MATCH_PAGE}&offset=${_matchOffset}`,
      {silent: true});
    const items = (r && r.items) || [];
    if (items.length) {
      document.querySelector("#matches-table tbody")
        .insertAdjacentHTML("beforeend", items.map(_matchRowHtml).join(""));
      _matchOffset += items.length;
      if (typeof r.total === "number") _matchTotal = r.total;
      _updateTableCount("matches-table", _matchOffset, _matchTotal);
    }
  } catch (e) {} finally { _matchLoading = false; }
}

function _bindMatchScroll() {
  const tbl = document.querySelector("#matches-table");
  if (!tbl || tbl._scrollBound) return;
  tbl._scrollBound = true;
  tbl.addEventListener("scroll", () => {
    if (tbl.scrollTop + tbl.clientHeight >= tbl.scrollHeight - 60) loadMoreMatches();
  });
}

function _updateTableCount(tableId, shown, total) {
  // Inject a small "showing N of M" caption above each table's parent
  // list-card if there's something to say.
  const tbl = document.getElementById(tableId);
  if (!tbl) return;
  const card = tbl.closest(".list-card");
  if (!card) return;
  let cap = card.querySelector(".table-count");
  if (shown >= total) {
    if (cap) cap.remove();
    return;
  }
  if (!cap) {
    cap = document.createElement("div");
    cap.className = "table-count muted";
    cap.style.cssText = "font-size:11px;margin:-6px 0 6px;padding:0 2px";
    card.querySelector("h3")?.after(cap);
  }
  cap.textContent = `Affichage ${shown} sur ${total}`;
}

let _progressionFullData = [];        // [{timestamp, account_trophies_after, brawler, result, …}]
let _progressionRange = "24h";        // active filter: 1h | 6h | 24h | 7d | all

function _rangeSeconds(r) {
  return {"1h":3600, "6h":21600, "24h":86400, "7d":604800, "all":null}[r];
}

function _filterByRange(data, range) {
  const span = _rangeSeconds(range);
  if (span == null) return data;
  const cutoff = Date.now()/1000 - span;
  return data.filter(m => m.timestamp >= cutoff);
}

// Fetch the FULL account-trophy history (lightweight {t,y}, downsampled
// server-side) and re-render the chart. This is what fixes "no data before
// 16h" — the dashboard endpoint only returns the last 200 matches for the
// table, which on a busy day starts mid-afternoon.
let _progressionInflight = null;   // accId currently being fetched (dedupe)
let _progressionSig = null;        // signature of the last rendered series
async function _loadProgression(accId) {
  if (_progressionInflight === accId) return;  // already fetching this account
  _progressionInflight = accId;
  let res;
  try {
    res = await api(`/api/accounts/${accId}/progression?max_points=2000`, {silent: true});
  } catch (e) { return; }
  finally { if (_progressionInflight === accId) _progressionInflight = null; }
  if (selectedAccountId !== accId) return;  // user switched accounts
  const pts = (res && res.points) || [];
  if (!pts.length) return;
  // Skip the re-render when nothing changed (same count + same last point) —
  // avoids a redundant chart redraw (flicker) on every match-triggered refresh.
  const last = pts[pts.length - 1];
  const sig = `${accId}:${pts.length}:${last.t}:${last.y}`;
  if (sig === _progressionSig) return;
  _progressionSig = sig;
  _progressionFullData = pts.map(p => ({timestamp: p.t, account_trophies_after: p.y}));
  renderProgression(_filterByRange(_progressionFullData, _progressionRange));
}

function _formatTimeTick(ts, rangeSpan) {
  const d = new Date(ts * 1000);
  if (rangeSpan != null && rangeSpan <= 86400) {
    return d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  }
  return d.toLocaleDateString([], {day:"2-digit", month:"2-digit"}) +
         " " + d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
}

// Crosshair plugin: draws a vertical line at the cursor X position
// across the entire chart area. Independent of point hover detection.
const _crosshairPlugin = {
  id: "verticalCrosshair",
  afterDatasetsDraw(chart) {
    const tooltip = chart.tooltip;
    if (!tooltip || tooltip.opacity === 0 || !tooltip.dataPoints?.length) return;
    const ctx = chart.ctx;
    const x = tooltip.dataPoints[0].element.x;
    const top = chart.chartArea.top;
    const bottom = chart.chartArea.bottom;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(255,255,255,0.18)";
    ctx.setLineDash([4, 3]);
    ctx.stroke();
    ctx.restore();
  },
};

function renderProgression(rows) {
  // Points carry their actual timestamp so the X axis is temporally
  // proportional — long idle gaps now show as wide empty stretches,
  // dense grind clusters as packed lines. Previous version used
  // evenly-spaced string labels and lost all temporal nuance.
  // Smooth monotone curve through the points (no more staircase steps) —
  // nicer to read at a glance. We extend the last value to "now" so the
  // chart doesn't end on a stale point with an ugly trailing gap.
  const points = rows.map(m => ({x: m.timestamp, y: m.account_trophies_after}));
  if (points.length > 0) {
    const last = points[points.length - 1];
    const nowTs = Date.now() / 1000;
    if (nowTs - last.x > 60) {  // > 1 min idle: extend flat line to now
      points.push({x: nowTs, y: last.y});
    }
  }
  const span = _rangeSeconds(_progressionRange);
  if (!progressionChart) {
    progressionChart = new Chart(document.getElementById("chart-progression"), {
      type: "line",
      plugins: [_crosshairPlugin],
      data: { datasets: [{ label: "Trophies", data: points,
        borderColor: "#4f8cf0", backgroundColor: "rgba(79,140,240,0.12)",
        borderWidth: 2, tension: 0.35, stepped: false, cubicInterpolationMode: "monotone",
        pointRadius: 0, pointHoverRadius: 6,
        pointHoverBackgroundColor: "#6ea4ff",
        pointHoverBorderColor: "#fff", pointHoverBorderWidth: 2,
        fill: true, parsing: false }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        // Tooltip fires wherever the cursor hovers — picks the closest
        // point on the X axis. Vertical line + hover ring make the
        // current reading position obvious.
        interaction: { mode: "nearest", intersect: false, axis: "x" },
        // LTTB decimation: thins dense series to ~150 points while preserving
        // the curve's shape (peaks/dips). Cuts render cost + visual clutter
        // when the full history has hundreds/thousands of matches. Requires
        // parsing:false + linear x scale + animation:false (all set).
        parsing: false,
        plugins: {
          decimation: { enabled: true, algorithm: "lttb", samples: 150 },
          legend: { display: false },
          tooltip: {
            backgroundColor: "#11161f", borderColor: "#2a3445", borderWidth: 1,
            titleColor: "#f1f4f9", bodyColor: "#c4ccd8", padding: 10,
            displayColors: false,
            callbacks: {
              title: (items) => {
                if (!items.length) return "";
                const ts = items[0].parsed.x;
                return new Date(ts * 1000).toLocaleString();
              },
              label: (item) => `${item.parsed.y} 🏆`,
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            grid: {color:"rgba(255,255,255,.03)"},
            ticks: {
              color: "#7a8597", font: {family:"Inter", size:10},
              maxRotation: 0, autoSkipPadding: 20,
              callback: (val) => _formatTimeTick(val, _rangeSeconds(_progressionRange)),
            },
            border: {display:false},
          },
          y: {
            grid: {color:"rgba(255,255,255,.03)"},
            ticks: {color:"#7a8597", font:{family:"Inter", size:10}},
            border: {display:false},
          },
        },
      },
    });
  }
  progressionChart.data.datasets[0].data = points;
  // Constrain X to the requested range so the curve uses the full width
  // even when the data falls in just one corner of the window.
  if (span != null && points.length > 0) {
    progressionChart.options.scales.x.min = Date.now()/1000 - span;
    progressionChart.options.scales.x.max = Date.now()/1000;
  } else {
    delete progressionChart.options.scales.x.min;
    delete progressionChart.options.scales.x.max;
  }
  progressionChart.update("none");
}

// Range buttons
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#chart-range button");
  if (!btn) return;
  _progressionRange = btn.dataset.range;
  document.querySelectorAll("#chart-range button").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  if (_progressionFullData.length) {
    renderProgression(_filterByRange(_progressionFullData, _progressionRange));
  }
});

function renderWinRate(data) {
  // Show ALL played brawlers (was capped at the top 8). The DB only returns
  // brawlers that have matches, so this is every brawler the bot has played.
  const sorted = data.slice().sort((a,b)=>b.total-a.total);
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
        scales: { x: {stacked:true,grid:{color:"rgba(255,255,255,.03)"},ticks:{color:"#7a8597",font:{family:"Inter",size:10},autoSkip:false,maxRotation:90,minRotation:45},border:{display:false}},
                  y: {stacked:true,grid:{color:"rgba(255,255,255,.03)"},ticks:{color:"#7a8597",font:{family:"Inter",size:10}},border:{display:false}} } }
    });
  }
  winrateChart.data.labels = sorted.map(d=>d.brawler);
  winrateChart.data.datasets[0].data = sorted.map(d=>d.wins);
  winrateChart.data.datasets[1].data = sorted.map(d=>d.losses);
  winrateChart.data.datasets[2].data = sorted.map(d=>d.draws);
  winrateChart.update("none");
}

async function loadBrawlerEfficiency(accountId) {
  const tbody = document.querySelector("#brawler-eff-table tbody");
  if (!tbody) return;
  try {
    const r = await api(`/api/accounts/${accountId}/brawler_efficiency?days=7`, {silent: true});
    const rows = (r && r.brawlers) || [];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center;padding:14px">Pas encore de données.</td></tr>`;
      return;
    }
    const cap = (s) => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
    const tphTxt = (v) => v == null ? '<span class="muted">—</span>'
      : `<strong class="${v > 0 ? 'delta-pos' : (v < 0 ? 'delta-neg' : '')}">${v > 0 ? '+' : ''}${v}</strong>`;
    const netTxt = (v) => `<span class="${v > 0 ? 'delta-pos' : (v < 0 ? 'delta-neg' : '')}">${v > 0 ? '+' : ''}${v}</span>`;
    const fmtTime = (min) => min >= 60 ? `${Math.floor(min/60)}h${String(min%60).padStart(2,'0')}` : `${min}m`;
    tbody.innerHTML = rows.map(b => `
      <tr>
        <td>${cap(b.brawler)}</td>
        <td>${tphTxt(b.trophies_per_hour)}</td>
        <td>${netTxt(b.net)}</td>
        <td>${b.net_per_match > 0 ? '+' : ''}${b.net_per_match}</td>
        <td>${b.win_rate != null ? b.win_rate + '%' : '—'}</td>
        <td>${b.matches}</td>
        <td class="muted">${fmtTime(b.active_minutes)}</td>
      </tr>`).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center;padding:14px">Erreur de chargement.</td></tr>`;
  }
}

// Capitalize a brawler name for display (FR names are lowercase from brawlace).
function _cap(s){ return s ? s.charAt(0).toUpperCase()+s.slice(1) : "—"; }

// Fill the KPI cards from the analytics payload (efficiency / performance).
function _renderAnalytics(a) {
  const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  const sgn = (n) => (n > 0 ? "+" : "") + n;
  const dCls = (n) => n >= 0 ? "delta-pos" : "delta-neg";
  set("kpi-today", a.net_today != null ? `<span class="${dCls(a.net_today)}">${sgn(a.net_today)} 🏆</span>` : "—");
  set("kpi-today-n", a.matches_today != null ? a.matches_today : "—");
  set("kpi-tph", a.trophies_per_hour != null ? `<span class="${dCls(a.trophies_per_hour)}">${sgn(a.trophies_per_hour)}/h</span>` : "—");
  set("kpi-eta", a.eta_hours != null ? `~${a.eta_hours} h` : "—");
  set("kpi-wr24", a.win_rate_24h != null
    ? `${a.win_rate_24h}% <span class="muted" style="font-size:11px">(${a.matches_24h})</span>` : "—");
  if (a.streak_count) {
    const k = a.streak_kind;
    const cls = k === "victory" ? "delta-pos" : k === "defeat" ? "delta-neg" : "";
    const lbl = k === "victory" ? "victoires" : k === "defeat" ? "défaites" : "nuls";
    set("kpi-streak", `<span class="${cls}">${a.streak_count} ${lbl}</span>`);
  } else set("kpi-streak", "—");
  set("kpi-avgdelta", a.avg_delta != null ? `<span class="${dCls(a.avg_delta)}">${sgn(a.avg_delta)}</span>` : "—");
  const bw = (b) => b ? `${_cap(b.brawler)} <span class="muted" style="font-size:11px">${b.win_rate}% · ${b.total}</span>` : "—";
  set("kpi-best", bw(a.best_brawler));
  set("kpi-worst", bw(a.worst_brawler));
}

// Activity-by-hour bar chart. Server sends 24 UTC buckets; shift to the
// browser's local hours so it reads naturally.
function renderActivity(hourly) {
  const h = (hourly && hourly.length === 24) ? hourly : new Array(24).fill(0);
  const off = Math.round(-new Date().getTimezoneOffset() / 60);
  const local = new Array(24).fill(0);
  for (let u = 0; u < 24; u++) local[(((u + off) % 24) + 24) % 24] = h[u];
  const labels = local.map((_, i) => String(i).padStart(2, "0") + "h");
  if (!activityChart) {
    activityChart = new Chart(document.getElementById("chart-activity"), {
      type: "bar",
      data: { labels, datasets: [{ label: "Matchs", data: local, backgroundColor: "#4f8cf0", borderRadius: 3 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false,
        plugins: { legend: { display: false },
          tooltip: { backgroundColor: "#11161f", borderColor: "#2a3445", borderWidth: 1,
                     titleColor: "#f1f4f9", bodyColor: "#c4ccd8", padding: 10 } },
        scales: { x: { grid: { color: "rgba(255,255,255,.03)" }, ticks: { color: "#7a8597", font: { family: "Inter", size: 9 }, autoSkip: true, maxRotation: 0 }, border: { display: false } },
                  y: { grid: { color: "rgba(255,255,255,.03)" }, ticks: { color: "#7a8597", font: { family: "Inter", size: 10 }, precision: 0 }, border: { display: false } } } }
    });
  }
  activityChart.data.labels = labels;
  activityChart.data.datasets[0].data = local;
  activityChart.update("none");
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
  // Stream starts inside refreshDevicePanel once the instance id is resolved.
}
function closeDeviceConsole() {
  document.getElementById("device-panel").hidden = true;
  deviceConsoleOpen = false;
  if (deviceTimer) { clearInterval(deviceTimer); deviceTimer = null; }
  _stopStream();
}

function openDeviceConsoleForInstance(instanceId, instanceUid) {
  // Direct opening for instances that have no account yet (e.g. fresh
  // PC_Upec with BlueStacks before Brawl Stars is installed/logged-in).
  selectedInstanceForDevice = instanceId;
  // Make sure the detail pane is visible so #device-panel renders.
  document.getElementById("empty-state").hidden = true;
  document.getElementById("detail-content").hidden = false;
  // Hide the account-specific KPIs/charts since there's no account.
  document.getElementById("acc-name").textContent = instanceUid || "Instance";
  document.getElementById("acc-tag").textContent = "";
  document.getElementById("acc-instance").textContent = "no account yet";
  document.getElementById("device-panel").hidden = false;
  deviceConsoleOpen = true;
  refreshDevicePanelForInstance(instanceId);
  if (deviceTimer) clearInterval(deviceTimer);
  deviceTimer = setInterval(() => refreshDevicePanelForInstance(instanceId), 5000);
  _startStream(instanceId);   // live ~13fps stream
}

async function refreshDevicePanelForInstance(instanceId) {
  if (!deviceConsoleOpen) return;
  const [healthRes, logsRes] = await Promise.all([
    api(`/api/instances/${instanceId}/health`).catch(() => ({connected:false})),
    api(`/api/instances/${instanceId}/logs?limit=120`).catch(() => []),
  ]);
  _renderDevicePanel(healthRes, logsRes, {available: false});
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
  // Start the live stream now that we know the instance (idempotent: only if
  // not already streaming). Also re-arms it if the WS dropped between ticks.
  if (deviceConsoleOpen && !_streamActive()) _startStream(inst.id);

  // Fetch health and logs only (screenshot is on-demand via "↻ refresh now").
  const [healthRes, logsRes] = await Promise.all([
    api(`/api/instances/${inst.id}/health`).catch(() => ({connected:false})),
    api(`/api/instances/${inst.id}/logs?limit=120`).catch(() => []),
  ]);
  _renderDevicePanel(healthRes, logsRes, {available: false});
}

function _renderDevicePanel(healthRes, logsRes, shotRes) {
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
  if (shotRes && shotRes.available) {
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

  // Battery-gate toggle: sync checkbox to worker-reported state without
  // firing the change event (would re-POST and loop).
  if (healthRes.battery_gate_enabled != null) {
    const tg = document.getElementById("battery-gate-toggle");
    if (tg && tg.checked !== !!healthRes.battery_gate_enabled) {
      tg.checked = !!healthRes.battery_gate_enabled;
    }
  }

  // Logs
  const logsEl = document.getElementById("device-logs");
  const logsCount = document.getElementById("logs-count");
  if (logsRes && logsRes.length != null) {
    logsCount.textContent = `(${logsRes.length} lines)`;
    const atBottom = logsEl.scrollTop + logsEl.clientHeight + 10 >= logsEl.scrollHeight;
    logsEl.textContent = logsRes.map(e => e.line).join("\n");
    if (atBottom) logsEl.scrollTop = logsEl.scrollHeight;
  }
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

// ------- Battery gate toggle -------------------------------------
// The worker persists the enabled flag to disk; the panel just sends
// the desired state. The current state is read from /health on each
// device console refresh so the checkbox stays in sync with reality
// (including after a bot restart that toggled it from another tab).
async function sendBatteryGate(enabled) {
  if (!selectedInstanceForDevice) return;
  const status = document.getElementById("battery-gate-status");
  status.textContent = "…";
  try {
    const r = await fetch(`/api/instances/${selectedInstanceForDevice}/cmd`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: "battery_gate_set", args: {enabled}}),
    });
    const j = await r.json();
    if (r.ok && j.ok) {
      status.textContent = enabled ? "✓ on" : "✓ off";
    } else {
      status.textContent = "✗ " + (j.error || r.statusText);
    }
  } catch (e) {
    status.textContent = "✗ " + e.message;
  } finally {
    setTimeout(() => { status.textContent = ""; }, 3000);
  }
}
document.getElementById("battery-gate-toggle").addEventListener("change", e => {
  sendBatteryGate(e.target.checked);
});

// ------- Remote device control -----------------------------------
// Tap = quick click (no/small drag). Swipe = drag with > SWIPE_PX
// movement. Coords are normalized 0..1 on the screen overlay and the
// worker resolves the actual device pixel size at command time. We
// auto-refresh the screenshot right after each input so the operator
// sees the effect within ~1s.
const SWIPE_PX = 8;
let _remoteEnabled = false;
let _remoteDrag = null;     // {x, y, t} on mousedown
let _remoteAutoRefresh = null;

function _sendDeviceInput(name, args) {
  if (!selectedInstanceForDevice) return Promise.resolve();
  return fetch(`/api/instances/${selectedInstanceForDevice}/cmd`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name, args: args || {}}),
  }).then(r => r.json()).catch(e => ({ok: false, error: String(e)}));
}

function _flashTapFeedback(overlay, evt) {
  const rect = overlay.getBoundingClientRect();
  overlay.style.setProperty("--fx", (evt.clientX - rect.left) + "px");
  overlay.style.setProperty("--fy", (evt.clientY - rect.top) + "px");
  overlay.classList.remove("feedback");
  // Trigger reflow so the animation restarts on rapid taps.
  void overlay.offsetWidth;
  overlay.classList.add("feedback");
}

function _enableRemoteControl(on) {
  _remoteEnabled = on;
  const overlay = document.getElementById("screen-overlay");
  const keys = document.getElementById("remote-keys");
  overlay.hidden = !on;
  keys.hidden = !on;
  // Auto-refresh tighter loop only when remote is engaged so we see
  // the result of taps without spamming when idle.
  if (_remoteAutoRefresh) { clearInterval(_remoteAutoRefresh); _remoteAutoRefresh = null; }
  if (on) _remoteAutoRefresh = setInterval(_remoteRefreshScreen, 4000);
}

// ---- Live stream (WebSocket, ~13 fps JPEG pushed by the worker) ----
// Multi-target: one stream paints every active <img> (the main-page Game
// Control view AND the Device console). The worker streams only while ≥1
// target is active (cloud start/stop_stream on first/last subscriber).
let _streamWS = null;
let _streamReconnect = null;
let _streamInstanceId = null;
// Each stream "target" is an on-demand <img> with an overlay <video>. We mux
// the worker's raw H264 into each target's <video> via jMuxer + MSE.
const _STREAM_VIDEO = {                 // img target id → overlay <video> id
  "gc-screenshot": "gc-video",
  "device-screen": "device-video",
};
const _streamMuxers = new Map();        // targetId → {video, muxer, lastBytes, lastT, frozen}
let _streamBytes = 0;                    // total H264 bytes received this connection
let _streamWatchdog = null;

function _streamMakeMuxer(targetId) {
  if (_streamMuxers.has(targetId)) return;
  if (typeof JMuxer === "undefined") {
    console.error("[stream] jMuxer not loaded — live video unavailable");
    return;
  }
  const vid = document.getElementById(_STREAM_VIDEO[targetId]);
  if (!vid) return;
  let muxer;
  try {
    muxer = new JMuxer({
      node: vid,
      mode: "video",
      flushingTime: 100,      // ms; 0 can stall some builds
      maxDelay: 300,
      clearBuffer: true,
      fps: 30,
      debug: false,
      onReady: () => { try { vid.play().catch(() => {}); } catch (_) {} },
      onError: (e) => { console.warn("[stream] jMuxer error — reinit", e);
                        _streamReinit(targetId); },
    });
  } catch (e) { console.error("[stream] JMuxer init failed", e); return; }
  // Show the video, hide the on-demand capture <img> behind it.
  vid.hidden = false;
  try { vid.play().catch(() => {}); } catch (_) {}
  const frame = vid.closest(".gc-screen-frame, .screen-wrap");
  if (frame) frame.classList.add("streaming");
  // Grace period: after (re)creation jMuxer must wait for the next keyframe
  // (screenrecord GOP ~10s) before currentTime advances — don't let the
  // watchdog mistake that for a freeze and reinit-loop.
  _streamMuxers.set(targetId, {
    video: vid, muxer, lastBytes: _streamBytes, lastT: 0, frozen: 0,
    graceUntil: performance.now() + 12000,
  });
  _streamStartWatchdog();
}
function _streamDropMuxer(targetId) {
  const m = _streamMuxers.get(targetId);
  if (!m) return;
  try { m.muxer.destroy(); } catch (_) {}
  try { m.video.removeAttribute("src"); m.video.load(); } catch (_) {}
  m.video.hidden = true;
  const frame = m.video.closest(".gc-screen-frame, .screen-wrap");
  if (frame) frame.classList.remove("streaming");
  _streamMuxers.delete(targetId);
  if (!_streamMuxers.size && _streamWatchdog) {
    clearInterval(_streamWatchdog); _streamWatchdog = null;
  }
}
// Rebuild the muxer/SourceBuffer for a target WITHOUT touching the WS. Used to
// self-heal when MSE freezes (screenrecord respawns every ~175s, a dropped
// chunk corrupts H264 until the next keyframe ~10s, MSE can stall) — a frozen
// picture recovers in ~2s instead of staying broken.
function _streamReinit(targetId) {
  const m = _streamMuxers.get(targetId);
  if (!m) return;
  try { m.muxer.destroy(); } catch (_) {}
  _streamMuxers.delete(targetId);
  _streamMakeMuxer(targetId);
}
function _streamStartWatchdog() {
  if (_streamWatchdog) return;
  _streamWatchdog = setInterval(() => {
    const now = performance.now();
    for (const [tid, m] of _streamMuxers.entries()) {
      // Skip while in the post-(re)create grace window (waiting for a keyframe).
      if (m.graceUntil && now < m.graceUntil) {
        m.lastBytes = _streamBytes; m.lastT = m.video.currentTime || 0;
        continue;
      }
      const t = m.video.currentTime || 0;
      const dataFlowing = _streamBytes > m.lastBytes;   // new bytes since last check
      // CHANGED (not just advanced): a reinit resets currentTime to 0, which is
      // a change → not a freeze. Only an unchanging currentTime = truly frozen.
      const changed = Math.abs(t - m.lastT) > 0.05;
      if (dataFlowing && !changed) {
        m.frozen++;
        if (m.frozen >= 3) {                            // ~6s truly frozen with data
          console.warn("[stream] frozen → reinit", tid);
          _streamReinit(tid);                           // grants a fresh grace window
          continue;
        }
      } else {
        m.frozen = 0;
      }
      // Stay at the LIVE edge: MSE plays at its own pace and drifts behind
      // real-time (the "slow-motion + desync" bug — latency just grows). Nudge
      // playbackRate up when a little behind, hard-seek to live on big drift.
      _streamSyncLive(m.video);
      m.lastBytes = _streamBytes;
      m.lastT = m.video.currentTime || 0;   // re-read (a seek may have moved it)
    }
  }, 1500);
}
function _streamSyncLive(v) {
  try {
    if (!v.buffered || !v.buffered.length) return;
    const end = v.buffered.end(v.buffered.length - 1);
    const drift = end - (v.currentTime || 0);
    if (drift > 2.5) {            // way behind → jump to the live edge
      v.currentTime = end - 0.2;
      v.playbackRate = 1.0;
    } else if (drift > 0.6) {     // a bit behind → speed up to catch up smoothly
      v.playbackRate = 1.4;
    } else if (drift < 0.25) {    // caught up → normal speed
      v.playbackRate = 1.0;
    }
  } catch (_) {}
}
function _streamFeed(u8) {
  _streamBytes += u8.byteLength || 0;
  for (const {muxer} of _streamMuxers.values()) {
    try { muxer.feed({video: u8}); } catch (_) {}
  }
}

function _streamConnect(instanceId) {
  if (_streamWS) { try { _streamWS.close(); } catch (_) {} _streamWS = null; }
  if (_streamReconnect) { clearTimeout(_streamReconnect); _streamReconnect = null; }
  _streamInstanceId = instanceId;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let ws;
  try { ws = new WebSocket(`${proto}//${location.host}/ws/stream/${instanceId}`); }
  catch (_) { return; }
  ws.binaryType = "arraybuffer";
  _streamWS = ws;
  ws.onopen = () => console.log("[stream] WS open →", instanceId);
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") return;   // (no text frames in H264 path)
    _streamFeed(new Uint8Array(ev.data));
  };
  ws.onclose = () => {
    if (_streamWS === ws) _streamWS = null;
    if (_streamMuxers.size && !_streamReconnect) {
      _streamReconnect = setTimeout(() => {
        _streamReconnect = null;
        if (_streamMuxers.size && _streamInstanceId) _streamConnect(_streamInstanceId);
      }, 2000);
    }
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}
function _streamEnsure(instanceId, targetId) {
  if (!instanceId || !targetId) return;
  _streamMakeMuxer(targetId);
  if (!_streamActive() || _streamInstanceId !== instanceId) _streamConnect(instanceId);
}
function _streamDrop(targetId) {
  _streamDropMuxer(targetId);
  if (!_streamMuxers.size) {
    if (_streamReconnect) { clearTimeout(_streamReconnect); _streamReconnect = null; }
    if (_streamWS) { try { _streamWS.close(); } catch (_) {} _streamWS = null; }
    _streamInstanceId = null;
  }
}
function _streamActive() {
  return !!_streamWS && _streamWS.readyState === WebSocket.OPEN;
}
// Device-console callers (target = the console's #device-screen img).
function _startStream(instanceId) { _streamEnsure(instanceId, "device-screen"); }
function _stopStream() { _streamDrop("device-screen"); }

// ---- Main-page live toggle (Game Control, target = #gc-screenshot) ----
let _mainStreamUid = null;   // instance uid of the currently-selected account
function _setMainStreamUid(uid) {
  // Account switched: stop streaming the old one if it was live.
  if (uid !== _mainStreamUid) _stopMainStream();
  _mainStreamUid = uid || null;
}
function _toggleMainStream() {
  const btn = document.getElementById("gc-live-toggle");
  const on = _streamMuxers.has("gc-screenshot");
  if (on) { _stopMainStream(); }
  else if (_mainStreamUid) {
    _streamEnsure(_mainStreamUid, "gc-screenshot");
    if (btn) { btn.textContent = "⏸ Live"; btn.classList.add("live-on"); }
    const meta = document.getElementById("gc-screen-meta");
    if (meta) meta.textContent = "live · H264 (video)";
  }
}
function _stopMainStream() {
  _streamDrop("gc-screenshot");
  const btn = document.getElementById("gc-live-toggle");
  if (btn) { btn.textContent = "▶ Live"; btn.classList.remove("live-on"); }
}
document.getElementById("gc-live-toggle")?.addEventListener("click", _toggleMainStream);

let _remoteRefreshing = false;
async function _remoteRefreshScreen() {
  // The live WS stream is the primary source — skip the slow poll when it's up.
  if (_streamActive()) return;
  if (!selectedInstanceForDevice || !_remoteEnabled) return;
  // A live capture takes ~2s (real phone screencap over WiFi→Pi). Skip a
  // tick if the previous one is still in flight so requests don't pile up.
  if (_remoteRefreshing) return;
  _remoteRefreshing = true;
  try {
    const r = await api(`/api/instances/${selectedInstanceForDevice}/screenshot?refresh=true`);
    if (r.available) {
      const mime = r.mime || "image/png";
      const b64 = r.b64 || r.png_b64;
      document.getElementById("device-screen").src = `data:${mime};base64,${b64}`;
    }
  } catch (_) { /* swallow; next tick retries */ }
  finally { _remoteRefreshing = false; }
}

document.getElementById("remote-control-toggle").addEventListener("change", e => {
  _enableRemoteControl(e.target.checked);
});

(function wireRemoteOverlay() {
  const overlay = document.getElementById("screen-overlay");
  function normCoords(evt) {
    const rect = overlay.getBoundingClientRect();
    const x = (evt.clientX - rect.left) / rect.width;
    const y = (evt.clientY - rect.top) / rect.height;
    return {x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y))};
  }
  overlay.addEventListener("mousedown", evt => {
    if (!_remoteEnabled) return;
    evt.preventDefault();
    _remoteDrag = {...normCoords(evt), px: evt.clientX, py: evt.clientY, t: Date.now()};
  });
  overlay.addEventListener("mouseup", async evt => {
    if (!_remoteEnabled || !_remoteDrag) return;
    const end = normCoords(evt);
    const dxPx = Math.abs(evt.clientX - _remoteDrag.px);
    const dyPx = Math.abs(evt.clientY - _remoteDrag.py);
    const dur = Date.now() - _remoteDrag.t;
    const start = _remoteDrag; _remoteDrag = null;
    _flashTapFeedback(overlay, evt);
    if (dxPx < SWIPE_PX && dyPx < SWIPE_PX) {
      await _sendDeviceInput("input_tap", {x: end.x, y: end.y});
    } else {
      await _sendDeviceInput("input_swipe", {
        x1: start.x, y1: start.y, x2: end.x, y2: end.y,
        duration_ms: Math.max(120, Math.min(dur, 1500)),
      });
    }
    setTimeout(_remoteRefreshScreen, 600);
  });
  overlay.addEventListener("mouseleave", () => { _remoteDrag = null; });
  // Context menu would hijack right-click; suppress to keep right-click free.
  overlay.addEventListener("contextmenu", e => { if (_remoteEnabled) e.preventDefault(); });
})();

// Hardware keys row.
document.querySelectorAll("#remote-keys button[data-key]").forEach(btn => {
  btn.addEventListener("click", async () => {
    await _sendDeviceInput("input_key", {keycode: btn.dataset.key});
    setTimeout(_remoteRefreshScreen, 600);
  });
});

// Text input prompt.
document.getElementById("remote-text-btn").addEventListener("click", async () => {
  const text = window.prompt("Text to type on device:");
  if (!text) return;
  await _sendDeviceInput("input_text", {text});
  setTimeout(_remoteRefreshScreen, 600);
});

// Close remote refresh loop when console closes.
const _origCloseDeviceConsole = closeDeviceConsole;
closeDeviceConsole = function() {
  _enableRemoteControl(false);
  document.getElementById("remote-control-toggle").checked = false;
  _origCloseDeviceConsole();
};

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
let _instanceCharging = false;

// Buttons that mustn't run during a session (could derail the bot).
// NOTE: "gc-capture" is deliberately NOT here — a screenshot is read-only
// (GET /screenshot, ADB screencap, no taps) so it's always safe, and being
// able to grab a frame *during* a push is exactly how you check the bot.
const SESSION_GUARDED_BUTTONS = [
  "gc-play-one", "gc-goto-lobby", "gc-refresh-state",
  "gc-refresh-brawlers",
];
const SESSION_GUARDED_SELECTS = ["gc-brawler-select"];
// Also gate the top-bar action: starting a new session while the
// phone is charging would be refused by the worker anyway, but
// disabling the UI is friendlier than a backend error.
const CHARGING_EXTRA_BUTTONS = ["btn-push-max"];

function _disableButton(id, disabled, reason) {
  const el = document.getElementById(id);
  if (!el) return;
  el.disabled = disabled;
  if (disabled) {
    if (!el.dataset.origTitle) el.dataset.origTitle = el.title || "";
    el.title = reason;
  } else {
    el.title = el.dataset.origTitle || "";
  }
}

function _refreshGuards() {
  const sessionMsg = "Désactivé : session de grind en cours";
  const chargingMsg = "Désactivé : téléphone en recharge";
  const disabled = _sessionActive || _instanceCharging;
  const reason = _instanceCharging ? chargingMsg : sessionMsg;
  for (const id of SESSION_GUARDED_BUTTONS) _disableButton(id, disabled, reason);
  for (const id of CHARGING_EXTRA_BUTTONS) _disableButton(id, _instanceCharging, chargingMsg);
  for (const id of SESSION_GUARDED_SELECTS) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.disabled = disabled;
    el.title = disabled ? reason : "";
  }
}

function _applySessionGuards(active) {
  _sessionActive = active;
  _refreshGuards();
}

function _applyChargingGuard(charging) {
  _instanceCharging = charging;
  _refreshGuards();
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
      // Show the brawler the worker is ACTUALLY playing (summary.current),
      // not just the first non-exhausted one in the list (which was showing a
      // 900+ brock the strategy never picks). Fall back to first-active.
      const curName = s.summary && s.summary.current;
      const current = (curName && brawlers.find(b => b.name === curName))
        || brawlers.find(b => !b.exhausted);
      const targetTxt = s.target_total_trophies
        ? ` · target ${s.target_total_trophies} 🏆`
        : "";
      banner.hidden = false;
      const tierTxt = current?.tier ? ` [${current.tier}]` : "";
      banner.innerHTML = `
        <span class="dot"></span>
        <span><strong>Push Max running</strong> · ${current ? current.name + tierTxt + ' (' + current.trophies + ' 🏆)' : 'rotating'} · ${done}/${total} done${targetTxt}</span>
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
  try {
    const r = await fetch(`/api/accounts/${selectedAccountId}${path}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: body ? JSON.stringify(body) : "{}",
    });
    if (!r.ok) {
      const txt = await r.text().catch(() => "");
      return {ok: false, error: `HTTP ${r.status}${txt ? ': ' + txt.slice(0, 120) : ''}`};
    }
    return await r.json();
  } catch (e) {
    return {ok: false, error: String(e.message || e)};
  }
}

document.getElementById("btn-push-max").addEventListener("click", async (e) => {
  if (!selectedAccountId) return;
  const btn = e.currentTarget;
  // Hard double-click guard: if already disabled (inflight or session
  // active) → ignore. session-active toggles `hidden`, not `disabled`,
  // so cover both cases.
  if (btn.disabled || btn.hidden) return;
  // No config modal and no global target: push_max simply grinds every
  // brawler up to the efficiency ceiling (configured in the global config,
  // default 750 🏆) then stops. The ceiling is applied server-side.
  // Optimistic UI: hide Push Max immediately + show Stop, so a second
  // click is impossible during the 10s window before session_state
  // catches up.
  document.getElementById("btn-push-max").hidden = true;
  document.getElementById("btn-stop").hidden = false;
  await withLoader("btn-push-max", async () => {
    const r = await postSession("/push_max", {});
    if (!r.ok || (r.data && !r.data.ok)) {
      const reason = r.data?.msg || r.data?.error || r.error || "unknown";
      showToast("Push Max failed: " + reason, "err");
      // If the bot says a session is already running, keep Stop visible
      // and let the session_state poll confirm. Otherwise, roll back.
      const alreadyRunning = /already running/i.test(reason);
      if (!alreadyRunning) {
        document.getElementById("btn-push-max").hidden = false;
        document.getElementById("btn-stop").hidden = true;
      }
      refreshSessionState();
    } else {
      showToast("Push Max démarré — grind jusqu'au plafond 🏆", "ok");
      // Poll session_state aggressively for ~30s so the banner appears
      // as soon as pick_next sets _push_max on the runner.
      for (let i = 0; i < 15; i++) {
        await new Promise(r => setTimeout(r, 2000));
        await refreshSessionState();
      }
    }
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
    document.getElementById("gc-state").textContent = statusText(snap);
    if (snap.trophies != null) document.getElementById("gc-trophies").textContent = snap.trophies + " 🏆";
  } else {
    document.getElementById("gc-state").textContent = "—";
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
      const img = document.getElementById("gc-screenshot");
      img.src = `data:${r.data.mime};base64,${r.data.b64}`;
      // Force landscape: rotate via CSS class if the capture came back
      // portrait (screen-off, locked, wrong rotation, …).
      const frame = document.querySelector(".gc-screen-frame");
      if (frame && r.data.w && r.data.h) {
        frame.classList.toggle("portrait", r.data.h > r.data.w);
      }
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
  el.textContent = `${cloudTotal} 🏆`;
  el.dataset.fromBrawlace = "1";  // tells SSE handler not to override
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

// ============================================================
// Global Planning Tab
// ============================================================

function showPlanningView() {
  _fleetView = false;
  selectedAccountId = null;
  // #planning-view is a sibling AFTER <main> (which is display:grid, 100vh,
  // overflow:hidden). Just unhiding planning-view leaves it below the fold
  // behind the full-height main → black screen. Hide <main> via style.display
  // (its `display:grid` rule beats the [hidden] attribute) and show the view.
  const main = document.querySelector("main");
  if (main) main.style.display = "none";
  document.getElementById("planning-view").hidden = false;
  loadGlobalSchedule();
}

function _hidePlanningView() {
  document.getElementById("planning-view").hidden = true;
  const main = document.querySelector("main");
  if (main) main.style.display = "";   // restore the grid layout
}

// Patch showFleetOverview to leave the planning view + restore <main>.
const _origShowFleetOverview = showFleetOverview;
showFleetOverview = function() {
  _hidePlanningView();
  _origShowFleetOverview();
};

// Patch selectAccount to leave the planning view + restore <main>.
const _origSelectAccount = selectAccount;
selectAccount = async function(id) {
  _hidePlanningView();
  return _origSelectAccount(id);
};

async function loadGlobalSchedule() {
  const banner = document.getElementById("sched-effective-banner");
  if (banner) banner.textContent = "chargement…";
  try {
    const r = await api("/api/config/schedule");
    const cfg = r.config || {};
    const eff = r.effective || null;

    // Fill form controls
    const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
    const num = (id, v) => { const el = document.getElementById(id); if (el && v != null) el.value = v; };

    chk("sched-enabled", cfg.enabled !== false);
    num("sched-sleep-start", cfg.sleep_start_hour);
    num("sched-sleep-end", cfg.sleep_end_hour);
    num("sched-sleep-jitter", cfg.sleep_jitter_minutes);
    num("sched-block-min", cfg.block_min_minutes);
    num("sched-block-max", cfg.block_max_minutes);
    num("sched-break-min", cfg.break_min_minutes);
    num("sched-break-max", cfg.break_max_minutes);
    num("sched-max-blocks", cfg.max_blocks_per_day);
    num("sched-blocks-jitter", cfg.blocks_jitter);
    num("sched-cap", cfg.daily_match_cap);
    num("sched-cap-jitter", cfg.daily_cap_jitter);

    // Dayoff weekday chips
    const activeDays = new Set(cfg.dayoff_weekdays || []);
    document.querySelectorAll("#sched-dayoff-chips .pv-chip").forEach(chip => {
      chip.classList.toggle("active", activeDays.has(chip.dataset.day));
    });

    // Dayoff chance slider (0..1 → 0..100)
    const chanceSlider = document.getElementById("sched-dayoff-chance");
    const chanceVal = document.getElementById("sched-dayoff-chance-val");
    const chance = Math.round((cfg.dayoff_chance || 0) * 100);
    if (chanceSlider) chanceSlider.value = chance;
    if (chanceVal) chanceVal.textContent = chance + "%";

    // Pause windows
    const container = document.getElementById("sched-pause-windows");
    if (container) {
      container.innerHTML = "";
      for (const w of (cfg.pause_windows || [])) {
        addPauseRow(w);
      }
    }

    // Weekend overrides
    const we = cfg.weekend || {};
    const weN = (id, v) => { const el = document.getElementById(id); if (el) el.value = (v != null) ? v : ""; };
    weN("sched-we-sleep-start", we.sleep_start_hour);
    weN("sched-we-sleep-end", we.sleep_end_hour);
    weN("sched-we-cap", we.daily_match_cap);

    // Live banner
    if (banner) {
      if (!eff) {
        banner.textContent = "worker hors-ligne";
      } else {
        banner.textContent =
          `état=${eff.state} · sommeil ${eff.sleep} · quota ${eff.matches_today}/${eff.today_cap}` +
          ` · pauses ${eff.pause_windows}` +
          (eff.is_dayoff ? " · JOUR DE REPOS" : "");
      }
    }

    renderTimeline();
  } catch (e) {
    if (banner) banner.textContent = "❌ " + e.message;
  }
}

function collectScheduleForm() {
  const intVal = (id) => { const el = document.getElementById(id); return el && el.value !== "" ? parseInt(el.value, 10) : null; };
  const chkVal = (id) => { const el = document.getElementById(id); return el ? el.checked : false; };

  const dayoffDays = [];
  document.querySelectorAll("#sched-dayoff-chips .pv-chip.active").forEach(chip => {
    dayoffDays.push(chip.dataset.day);
  });

  const chanceSlider = document.getElementById("sched-dayoff-chance");
  const dayoffChance = chanceSlider ? parseInt(chanceSlider.value, 10) / 100 : 0;

  // Pause windows
  const pauseWindows = [];
  document.querySelectorAll("#sched-pause-windows .pv-pause-row").forEach(row => {
    const start = row.querySelector(".pw-start")?.value || "";
    const end = row.querySelector(".pw-end")?.value || "";
    const jitter = parseInt(row.querySelector(".pw-jitter")?.value || "0", 10);
    const label = row.querySelector(".pw-label")?.value || "";
    if (start && end) pauseWindows.push({start, end, jitter_minutes: jitter, label});
  });

  // Weekend overrides: only include non-empty fields
  const weekend = {};
  const weStart = intVal("sched-we-sleep-start");
  const weEnd   = intVal("sched-we-sleep-end");
  const weCap   = intVal("sched-we-cap");
  if (weStart != null) weekend.sleep_start_hour = weStart;
  if (weEnd   != null) weekend.sleep_end_hour   = weEnd;
  if (weCap   != null) weekend.daily_match_cap  = weCap;

  const cfg = {
    enabled:              chkVal("sched-enabled"),
    sleep_start_hour:     intVal("sched-sleep-start"),
    sleep_end_hour:       intVal("sched-sleep-end"),
    sleep_jitter_minutes: intVal("sched-sleep-jitter"),
    block_min_minutes:    intVal("sched-block-min"),
    block_max_minutes:    intVal("sched-block-max"),
    break_min_minutes:    intVal("sched-break-min"),
    break_max_minutes:    intVal("sched-break-max"),
    max_blocks_per_day:   intVal("sched-max-blocks"),
    blocks_jitter:        intVal("sched-blocks-jitter"),
    daily_match_cap:      intVal("sched-cap"),
    daily_cap_jitter:     intVal("sched-cap-jitter"),
    dayoff_weekdays:      dayoffDays,
    dayoff_chance:        dayoffChance,
    pause_windows:        pauseWindows,
  };
  if (Object.keys(weekend).length) cfg.weekend = weekend;
  return cfg;
}

async function saveGlobalSchedule() {
  const statusEl = document.getElementById("sched-apply-status");
  if (statusEl) { statusEl.textContent = "envoi…"; statusEl.style.color = ""; }
  try {
    const r = await api("/api/config/schedule", {method: "PUT", body: {config: collectScheduleForm()}});
    if (r && r.ok) {
      const applied = r.applied != null ? r.applied : "";
      const summary = applied ? ` (${applied} worker${applied > 1 ? "s" : ""})` : "";
      if (statusEl) { statusEl.textContent = "✅ appliqué à la flotte" + summary; statusEl.style.color = "#22c55e"; }
      setTimeout(loadGlobalSchedule, 1500);
    } else {
      const err = (r && r.error) || "échec";
      if (statusEl) { statusEl.textContent = "❌ " + err; statusEl.style.color = "#ef4444"; }
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = "❌ " + e.message; statusEl.style.color = "#ef4444"; }
  }
  setTimeout(() => { if (statusEl) { statusEl.textContent = ""; statusEl.style.color = ""; } }, 6000);
}

function addPauseRow(w) {
  w = w || {};
  const container = document.getElementById("sched-pause-windows");
  if (!container) return;
  const row = document.createElement("div");
  row.className = "pv-pause-row";
  row.innerHTML = `
    <label>Début<input type="time" class="pw-start" value="${w.start || ""}"></label>
    <label>Fin<input type="time" class="pw-end" value="${w.end || ""}"></label>
    <label>Variation ± (min)<input type="number" class="pw-jitter" min="0" max="120" value="${w.jitter_minutes != null ? w.jitter_minutes : 0}" style="width:60px"></label>
    <label>Label<input type="text" class="pw-label" placeholder="ex: déjeuner" value="${w.label || ""}" style="width:110px"></label>
    <button class="pv-rm" title="Supprimer">✕</button>
  `;
  row.querySelector(".pv-rm").addEventListener("click", () => { row.remove(); renderTimeline(); });
  // Re-render timeline on pause time changes
  row.querySelector(".pw-start").addEventListener("change", renderTimeline);
  row.querySelector(".pw-end").addEventListener("change", renderTimeline);
  container.appendChild(row);
  renderTimeline();
}

function renderTimeline() {
  const track = document.getElementById("tl-track");
  if (!track) return;
  track.innerHTML = "";

  const sleepStart = parseInt(document.getElementById("sched-sleep-start")?.value || "23", 10);
  const sleepEnd   = parseInt(document.getElementById("sched-sleep-end")?.value   || "7",  10);

  // Helper: hour (0–24) → percentage across 24h bar
  const pct = (h) => (h / 24 * 100).toFixed(3) + "%";
  const timePct = (hhmm) => {
    const [h, m] = (hhmm || "0:0").split(":").map(Number);
    return ((h + m / 60) / 24 * 100).toFixed(3) + "%";
  };
  const timeW = (startHH, endHH) => {
    // wraps midnight
    let d = endHH - startHH;
    if (d < 0) d += 24;
    return (d / 24 * 100).toFixed(3) + "%";
  };

  // Sleep segment (may wrap midnight)
  const seg = document.createElement("div");
  seg.className = "tl-seg sleep";
  const sleepDur = ((sleepEnd - sleepStart + 24) % 24) || 24;
  seg.style.left = pct(sleepStart);
  seg.style.width = (sleepDur / 24 * 100).toFixed(3) + "%";
  // Handle wrap: clip at 100% and add a second segment for the wrapped part
  if (sleepStart + sleepDur > 24) {
    seg.style.width = ((24 - sleepStart) / 24 * 100).toFixed(3) + "%";
    const seg2 = document.createElement("div");
    seg2.className = "tl-seg sleep";
    seg2.style.left = "0%";
    seg2.style.width = (sleepEnd / 24 * 100).toFixed(3) + "%";
    track.appendChild(seg2);
  }
  track.appendChild(seg);

  // Pause segments
  document.querySelectorAll("#sched-pause-windows .pv-pause-row").forEach(row => {
    const startStr = row.querySelector(".pw-start")?.value || "";
    const endStr   = row.querySelector(".pw-end")?.value   || "";
    if (!startStr || !endStr) return;
    const [sh, sm] = startStr.split(":").map(Number);
    const [eh, em] = endStr.split(":").map(Number);
    const startH = sh + sm / 60;
    const endH   = eh + em / 60;
    const dur    = endH - startH;
    if (dur <= 0) return;
    const pseg = document.createElement("div");
    pseg.className = "tl-seg pause";
    pseg.style.left = (startH / 24 * 100).toFixed(3) + "%";
    pseg.style.width = (dur / 24 * 100).toFixed(3) + "%";
    track.appendChild(pseg);
  });

  // Hour ticks (0, 6, 12, 18, 24)
  for (const h of [0, 6, 12, 18, 24]) {
    const tick = document.createElement("div");
    tick.className = "tl-tick";
    tick.style.left = pct(h);
    tick.style.bottom = "2px";
    tick.textContent = h === 24 ? "24h" : h + "h";
    track.appendChild(tick);
  }
}

// Wire dayoff chips
document.querySelectorAll("#sched-dayoff-chips .pv-chip").forEach(chip => {
  chip.addEventListener("click", () => chip.classList.toggle("active"));
});

// Wire dayoff chance slider
document.getElementById("sched-dayoff-chance")?.addEventListener("input", (e) => {
  const v = document.getElementById("sched-dayoff-chance-val");
  if (v) v.textContent = e.target.value + "%";
});

// Wire timeline re-render on sleep inputs
["sched-sleep-start", "sched-sleep-end"].forEach(id => {
  document.getElementById(id)?.addEventListener("input", renderTimeline);
});

// Wire nav button + apply button
document.getElementById("btn-planning")?.addEventListener("click", showPlanningView);
document.getElementById("sched-apply")?.addEventListener("click", saveGlobalSchedule);
document.getElementById("sched-add-pause")?.addEventListener("click", () => addPauseRow(null));

setInterval(refreshAll, REFRESH_MS);
refreshAll();

// Keep session-active guards + detail charts fresh even when idle.
setInterval(() => {
  if (selectedAccountId) {
    refreshSessionState();
    refreshDetail();   // re-pull matches/sessions → updates charts + tables
  }
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
      const acc = _lastAccounts.find(a => a.tag === m.tag);
      if (acc) {
        // Live trophy update: the worker computed the new total from the
        // match delta (no OCR re-read needed). Apply it immediately so
        // the badge moves in sync with each match — brawlace refresh
        // later confirms the authoritative value.
        if (acc.id === selectedAccountId && m.account_trophies_after != null) {
          _renderAuthoritativeTrophies(m.account_trophies_after);
        }
        // Refresh brawler trophies via brawlace ~5s later.
        setTimeout(() => {
          fetch(`/api/accounts/${acc.id}/brawlers/refresh`, {method: "POST"})
            .catch(() => {});
        }, 5000);
        // If user is viewing this account, refresh charts/tables now.
        if (acc.id === selectedAccountId) {
          refreshDetail();
        }
      }
    } else if (m.type === "instance_connected") {
      // Worker just (re)connected — sidebar + KPIs need a refresh.
      showToast(`Instance ${m.instance_id} connected`, "ok");
      refreshAll();
      // Auto-fetch a fresh capture for the selected account if it
      // belongs to this instance (wait 3s so the worker's capture
      // pipeline is warm).
      const acc = _lastAccounts.find(a => a.id === selectedAccountId);
      if (acc && acc.instance_uid === m.instance_id) {
        _previewFetched.delete(selectedAccountId);
        setTimeout(() => gcCaptureScreenshot(), 3000);
      }
    } else if (m.type === "instance_disconnected") {
      showToast(`Instance ${m.instance_id} disconnected`, "err");
      refreshAll();
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
    const staleTag = snap.stale ? " (last seen)" : "";
    document.getElementById("gc-state").textContent = statusText(snap) + staleTag;
    // Trophies: brawlace cache is the authoritative source (set by
    // _renderAuthoritativeTrophies). The OCR-read snap.trophies is only
    // a fallback when brawlace has no data yet.
    if (snap.trophies != null) {
      const el = document.getElementById("gc-trophies");
      if (!el.dataset.fromBrawlace) {
        el.textContent = snap.trophies + " 🏆 (OCR)";
      }
    }
  }

  // Update the sidebar "doing" line live (between structural redraws) so the
  // status follows the bot in real time instead of every 15s.
  const card = document.querySelector(`.instance-card[data-instance="${snap.instance_id}"]`);
  if (card) {
    const doingEl = card.querySelector(".inst-doing");
    if (doingEl) {
      const t = snap.stale ? "" : statusText(snap);
      if (t && t !== "—") {
        doingEl.textContent = "▸ " + t;
        doingEl.hidden = false;
      } else {
        doingEl.hidden = true;
      }
    }
  }
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

let _activityOffset = 0;
let _activityTotal = 0;
let _activityLoading = false;
const _ACTIVITY_PAGE = 30;

async function loadActivity(reset = true) {
  const feed = document.getElementById("activity-feed");
  if (!feed) return;
  try {
    if (reset) _activityOffset = 0;
    const data = await api(`/api/activity/recent?limit=${_ACTIVITY_PAGE}&offset=${_activityOffset}`, {silent: true});
    const items = (data && data.items) || [];
    _activityTotal = (data && typeof data.total === "number") ? data.total : items.length;
    if (reset && !items.length) {
      feed.innerHTML = `<div class="empty-card" style="padding:14px">
        <span class="icon">🎮</span>
        <div style="font-size:11px">No matches yet</div>
      </div>`;
      document.getElementById("activity-count").textContent = "0";
      return;
    }
    const html = items.map(_activityRowHtml).join("");
    if (reset) feed.innerHTML = html;
    else feed.insertAdjacentHTML("beforeend", html);
    _activityOffset += items.length;
    // Show the REAL total match count, not the page size.
    document.getElementById("activity-count").textContent = _activityTotal;
    _bindActivityScroll();
  } catch (e) {}
}

// Lazy-load the next page when the user scrolls near the bottom of the
// feed — avoids loading the whole (potentially huge) history at once.
function _bindActivityScroll() {
  const feed = document.getElementById("activity-feed");
  if (!feed || feed._scrollBound) return;
  feed._scrollBound = true;
  feed.addEventListener("scroll", () => {
    if (_activityLoading || _activityOffset >= _activityTotal) return;
    if (feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 60) {
      _activityLoading = true;
      loadActivity(false).finally(() => { _activityLoading = false; });
    }
  });
}

function _prependActivity(m) {
  const feed = document.getElementById("activity-feed");
  if (!feed) return;
  // Remove the empty-state card if present.
  if (feed.querySelector(".empty-card")) feed.innerHTML = "";
  const tmp = document.createElement("div");
  tmp.innerHTML = _activityRowHtml(m).trim();
  feed.insertBefore(tmp.firstChild, feed.firstChild);
  // A new match arrived live: bump the real total + paging cursor so the
  // next lazy page stays aligned. No DOM cap — pagination owns growth.
  _activityTotal += 1;
  _activityOffset += 1;
  const countEl = document.getElementById("activity-count");
  if (countEl) countEl.textContent = _activityTotal;
}

loadActivity();

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

// ============================================================
// Global Config modal — panel-side notifications
// ============================================================

const EVENT_LABELS = {
  match: "Chaque match",
  target_reached: "Cible de trophées atteinte",
  battery_low: "Batterie faible",
  battery_resumed: "Batterie OK (reprise)",
  bot_stuck: "Bot bloqué",
  // instance_connected omitted — too noisy with self-update restarts.
  instance_disconnected: "Instance déconnectée",
  session_ended: "Session terminée",
};

async function openGlobalConfig() {
  const modal = document.getElementById("global-config-modal");
  if (!modal) return;
  try {
    const cfg = await api("/api/config/notif");
    try {
      const appCfg = await api("/api/config/app");
      const el = document.getElementById("cfg-push-ceiling");
      if (el) el.value = appCfg?.push_max_ceiling ?? 750;
    } catch (e) {}
    document.getElementById("cfg-tg-enabled").checked = !!cfg.telegram?.enabled;
    document.getElementById("cfg-tg-token").value = cfg.telegram?.bot_token || "";
    document.getElementById("cfg-tg-chat").value = cfg.telegram?.chat_id || "";
    document.getElementById("cfg-dc-enabled").checked = !!cfg.discord?.enabled;
    document.getElementById("cfg-dc-url").value = cfg.discord?.webhook_url || "";
    const tbody = document.querySelector("#cfg-events-table tbody");
    tbody.innerHTML = "";
    for (const [evt, label] of Object.entries(EVENT_LABELS)) {
      const r = cfg.events?.[evt] || {};
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${label}</td>
        <td style="text-align:center"><input type="checkbox" data-evt="${evt}" data-ch="telegram" ${r.telegram ? "checked" : ""}></td>
        <td style="text-align:center"><input type="checkbox" data-evt="${evt}" data-ch="discord" ${r.discord ? "checked" : ""}></td>`;
      tbody.appendChild(tr);
    }
    modal.hidden = false;
    modal.style.display = "flex";
  } catch (e) {
    showToast("Failed to load config: " + e.message, "err");
  }
}

function closeGlobalConfig() {
  const modal = document.getElementById("global-config-modal");
  modal.hidden = true;
  modal.style.display = "none";
}

async function saveGlobalConfig() {
  const events = {};
  for (const cb of document.querySelectorAll("#cfg-events-table input[type=checkbox]")) {
    const evt = cb.dataset.evt, ch = cb.dataset.ch;
    events[evt] = events[evt] || {};
    events[evt][ch] = cb.checked;
  }
  const payload = {
    telegram: {
      enabled: document.getElementById("cfg-tg-enabled").checked,
      bot_token: document.getElementById("cfg-tg-token").value.trim(),
      chat_id: document.getElementById("cfg-tg-chat").value.trim(),
    },
    discord: {
      enabled: document.getElementById("cfg-dc-enabled").checked,
      webhook_url: document.getElementById("cfg-dc-url").value.trim(),
    },
    events,
  };
  try {
    await api("/api/config/notif", {method: "PUT", body: payload});
    // Push-max efficiency ceiling (app config).
    const ceilEl = document.getElementById("cfg-push-ceiling");
    if (ceilEl) {
      const c = parseInt(ceilEl.value, 10);
      if (Number.isFinite(c) && c > 0) {
        await api("/api/config/app", {method: "PUT", body: {push_max_ceiling: c}});
      }
    }
    showToast("Config saved", "ok");
    closeGlobalConfig();
  } catch (e) {
    showToast("Save failed: " + e.message, "err");
  }
}

async function testNotifChannel(channel) {
  try {
    // Save first so the test uses current form values.
    await saveGlobalConfig();
  } catch (e) {}
  try {
    const r = await api(`/api/config/notif/test/${channel}`, {method: "POST"});
    showToast(r.ok ? r.message : `Failed: ${r.message}`, r.ok ? "ok" : "err");
  } catch (e) {
    showToast("Test failed: " + e.message, "err");
  }
}

document.getElementById("btn-global-config")?.addEventListener("click", openGlobalConfig);
document.getElementById("gc-modal-close")?.addEventListener("click", closeGlobalConfig);
document.getElementById("gc-modal-cancel")?.addEventListener("click", closeGlobalConfig);
document.getElementById("gc-modal-save")?.addEventListener("click", saveGlobalConfig);
document.getElementById("cfg-tg-test")?.addEventListener("click", () => testNotifChannel("telegram"));
document.getElementById("cfg-dc-test")?.addEventListener("click", () => testNotifChannel("discord"));
// Close on backdrop click (clicking outside the panel).
document.getElementById("global-config-modal")?.addEventListener("click", (e) => {
  if (e.target.id === "global-config-modal") closeGlobalConfig();
});
// Close on Escape key.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const m = document.getElementById("global-config-modal");
    if (m && !m.hidden) closeGlobalConfig();
  }
});
