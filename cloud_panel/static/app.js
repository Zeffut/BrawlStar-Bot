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
  // Remember this account's instance for the main-page live toggle (stops the
  // stream if we switched accounts).
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

  // Seed the chart from the dashboard's (capped) matches so something shows
  // instantly, then replace with the FULL downsampled history below.
  const ordered = [...matches].reverse().filter(m=>m.account_trophies_after!=null);
  _progressionFullData = ordered;
  renderProgression(_filterByRange(_progressionFullData, _progressionRange));
  // Full history (all matches, not just the last 200) so the chart isn't
  // truncated to "only after 16h". Lightweight {t,y} points, downsampled
  // server-side; Chart.js LTTB decimation thins them further for display.
  _loadProgression(acc.id);
  renderWinRate(acc.win_rate_by_brawler || []);

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
async function _loadProgression(accId) {
  let res;
  try {
    res = await api(`/api/accounts/${accId}/progression?max_points=2000`, {silent: true});
  } catch (e) { return; }  // keep the dashboard-seeded chart on failure
  if (selectedAccountId !== accId) return;  // user switched accounts
  const pts = (res && res.points) || [];
  if (!pts.length) return;
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
const _streamTargets = new Set();   // element ids to paint each frame onto

function _streamConnect(instanceId) {
  if (_streamWS) { try { _streamWS.close(); } catch (_) {} _streamWS = null; }
  if (_streamReconnect) { clearTimeout(_streamReconnect); _streamReconnect = null; }
  _streamInstanceId = instanceId;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let ws;
  try { ws = new WebSocket(`${proto}//${location.host}/ws/stream/${instanceId}`); }
  catch (_) { return; }
  _streamWS = ws;
  ws.onmessage = (ev) => {
    try {
      const f = JSON.parse(ev.data);
      if (f && f.b64) {
        const url = "data:image/jpeg;base64," + f.b64;
        _streamTargets.forEach(id => {
          const img = document.getElementById(id);
          if (img) img.src = url;
        });
      }
    } catch (_) {}
  };
  ws.onclose = () => {
    if (_streamWS === ws) _streamWS = null;
    if (_streamTargets.size && !_streamReconnect) {
      _streamReconnect = setTimeout(() => {
        _streamReconnect = null;
        if (_streamTargets.size && _streamInstanceId) _streamConnect(_streamInstanceId);
      }, 2000);
    }
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}
function _streamEnsure(instanceId, targetId) {
  if (!instanceId || !targetId) return;
  _streamTargets.add(targetId);
  if (!_streamActive() || _streamInstanceId !== instanceId) _streamConnect(instanceId);
}
function _streamDrop(targetId) {
  _streamTargets.delete(targetId);
  if (!_streamTargets.size) {
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
  const on = _streamTargets.has("gc-screenshot");
  if (on) { _stopMainStream(); }
  else if (_mainStreamUid) {
    _streamEnsure(_mainStreamUid, "gc-screenshot");
    if (btn) { btn.textContent = "⏸ Live"; btn.classList.add("live-on"); }
    const meta = document.getElementById("gc-screen-meta");
    if (meta) meta.textContent = "live ~13 fps · streaming";
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
const SESSION_GUARDED_BUTTONS = [
  "gc-play-one", "gc-goto-lobby", "gc-refresh-state",
  "gc-refresh-brawlers", "gc-capture",
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
        <p class="confirm-body">Le bot pousse un brawler à la fois (S → A → B → C selon affinité Pyla) jusqu'à stagnation, puis passe au suivant. Il s'arrête à l'objectif global OU quand tous les brawlers stagnent.</p>
        <div style="margin-bottom:18px">
          <label style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:6px">Total trophies cible (compte)</label>
          <input type="number" id="pm-target" min="1" value="${suggested}" style="width:100%;background:var(--surface-2);border:1px solid var(--border-2);color:var(--text);padding:10px 12px;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:14px" />
        </div>
        <div style="margin-bottom:18px">
          <label style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:6px">Max trophées par brawler — optionnel</label>
          <input type="number" id="pm-cap" min="1" placeholder="ex: 900 · vide = aucun cap" style="width:100%;background:var(--surface-2);border:1px solid var(--border-2);color:var(--text);padding:10px 12px;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:14px" />
          <span style="font-size:10px;color:var(--muted);display:block;margin-top:5px">Chaque brawler s'arrête à ce nombre, puis le bot passe au suivant.</span>
        </div>
        <div class="confirm-actions">
          <button class="confirm-btn" data-act="cancel">Annuler</button>
          <button class="confirm-btn primary" data-act="ok">▶ Lancer</button>
        </div>
      </div>`;
    const close = (val) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(val); };
    const submit = () => {
      const t = parseInt(overlay.querySelector("#pm-target").value, 10);
      if (!Number.isFinite(t) || t <= 0) { close(null); return; }
      const capRaw = (overlay.querySelector("#pm-cap").value || "").trim();
      const cap = capRaw === "" ? null : parseInt(capRaw, 10);
      close({ target: t, perBrawlerMax: (Number.isFinite(cap) && cap > 0) ? cap : null });
    };
    const onKey = (e) => {
      if (e.key === "Escape") close(null);
      if (e.key === "Enter") submit();
    };
    overlay.addEventListener("click", e => {
      if (e.target === overlay) close(null);
      else if (e.target.dataset.act === "cancel") close(null);
      else if (e.target.dataset.act === "ok") submit();
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    setTimeout(() => overlay.querySelector("#pm-target").select(), 50);
  });
}

document.getElementById("btn-push-max").addEventListener("click", async (e) => {
  if (!selectedAccountId) return;
  const btn = e.currentTarget;
  // Hard double-click guard: if already disabled (inflight or session
  // active) → ignore. session-active toggles `hidden`, not `disabled`,
  // so cover both cases.
  if (btn.disabled || btn.hidden) return;
  const res = await askPushMaxTarget();
  if (!res) return;
  const target = res.target;
  const perBrawlerMax = res.perBrawlerMax;
  // Optimistic UI: hide Push Max immediately + show Stop, so a second
  // click is impossible during the 10s window before session_state
  // catches up.
  document.getElementById("btn-push-max").hidden = true;
  document.getElementById("btn-stop").hidden = false;
  await withLoader("btn-push-max", async () => {
    const r = await postSession("/push_max", {target_total_trophies: target, per_brawler_max_trophies: perBrawlerMax});
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
      showToast(`Push Max démarré — objectif ${target} 🏆${perBrawlerMax ? ` · cap ${perBrawlerMax}/brawler` : ""}`, "ok");
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
