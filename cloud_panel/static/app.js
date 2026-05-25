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
  let instances = [], accounts = [];
  try {
    [instances, accounts] = await Promise.all([
      api("/api/instances"),
      api(selectedInstanceId ? `/api/accounts?instance_id=${selectedInstanceId}` : "/api/accounts"),
    ]);
  } catch (e) { return; }

  document.getElementById("fleet-summary").textContent =
    `${instances.filter(i=>i.fresh).length} / ${instances.length} instances online · ${accounts.length} accounts`;

  // Instances sidebar
  const ul = document.getElementById("instances");
  ul.innerHTML = "";
  const allItem = document.createElement("li");
  allItem.className = "instance-row" + (selectedInstanceId === null ? " active" : "");
  allItem.innerHTML = `<span class="status-dot running"></span><span class="name">All</span>`;
  allItem.onclick = () => { selectedInstanceId = null; refreshAll(); };
  ul.appendChild(allItem);
  for (const inst of instances) {
    const li = document.createElement("li");
    li.className = "instance-row" + (inst.id === selectedInstanceId ? " active" : "");
    li.innerHTML = `
      <span class="status-dot ${inst.fresh ? 'running' : 'stopped'}"></span>
      <div>
        <div class="name">${inst.name || inst.instance_id}</div>
        <div class="meta">${inst.accounts_count} accounts · ${ago(inst.last_seen_at)}</div>
      </div>`;
    li.onclick = () => { selectedInstanceId = inst.id; refreshAll(); };
    ul.appendChild(li);
  }

  // Accounts sidebar
  const ulA = document.getElementById("accounts");
  ulA.innerHTML = "";
  for (const a of accounts) {
    const li = document.createElement("li");
    li.dataset.id = a.id;
    if (a.id === selectedAccountId) li.classList.add("active");
    li.innerHTML = `
      <div class="name">${a.name || a.tag}</div>
      <div class="meta">#${a.tag} · ${a.instance_name || a.instance_uid}</div>`;
    li.onclick = () => selectAccount(a.id);
    ulA.appendChild(li);
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

setInterval(refreshAll, REFRESH_MS);
refreshAll();
