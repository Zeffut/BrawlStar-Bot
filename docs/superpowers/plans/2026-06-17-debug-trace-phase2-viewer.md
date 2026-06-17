# Debug Trace Phase 2 — Panel Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Une page `/static/debug.html` dans le panel cloud pour browser à distance les events `debug_trace` + leurs captures (sans SSH), filtrable par event/instance, avec vignettes cliquables.

**Architecture:** Réutilise le proxy générique existant `POST /api/instances/{id}/cmd` (cloud → WS → worker `COMMANDS`). On ajoute deux commandes worker (`debug_events`, `debug_capture`) dont la LOGIQUE vit dans `debug_trace.py` (seul module 3.9-importable → testable), wrappées trivialement dans `worker_link.py`. Les captures voyagent en **base64 sur le WS** (comme le screenshot live). Le front est une page statique autonome (aucune modif Python du cloud → pas de rebuild d'image fragile).

**Tech Stack:** Python 3 (stdlib `json`/`base64`/`pathlib`), `pytest` ; HTML/CSS/JS vanilla (fetch).

## Global Constraints

- **Aucune modification de `cloud_panel/app.py`** (évite le rebuild Dokploy fragile) : on réutilise `POST /api/instances/{instance_db_id}/cmd` (`cloud_panel/app.py:1087`) et `GET /api/instances` (`cloud_panel/app.py:426`). Le front est servi par le mount statique existant (`app.mount("/static", StaticFiles(...))`, `cloud_panel/app.py:37`) → accessible à `/static/debug.html`.
- **Captures = base64 sur le WS** (le worker n'a PAS d'URL publique ; mirror `_cmd_screenshot` qui renvoie `jpeg_b64`, `worker_link.py:127-131`). Jamais d'URL worker.
- **Logique testable dans `debug_trace.py`** (stdlib-only → importe sur Python 3.9) ; `worker_link.py` (utilise `X | None` → n'importe pas sur 3.9) ne contient que des wrappers triviaux non-unit-testés (vérifiés en live).
- **Sécurité chemin** : le paramètre `name` de capture doit rejeter `/`, `\`, `..`, un `.` initial, et `len > 80` ; vérifier en plus que le chemin résolu reste sous `_CAPTURE_DIR`.
- **Best-effort / robustesse** : fichier events manquant (rotation quotidienne) → `{ok:True, events:[]}` (jamais 404) ; lignes JSONL malformées (écriture best-effort) → ignorées ; capture absente (rétention GC) → `{ok:False, error:"capture not found"}`. Cap `limit` à 1000.
- **Réutiliser les constantes de chemin** `debug_trace._TRACE_DIR` / `debug_trace._CAPTURE_DIR` (définies `debug_trace.py:29-30`) — ne pas recalculer les chemins.
- `logs/` est gitignored ; aucun artefact commité.
- Déploiement : worker via git (worker_link.py + debug_trace.py) ; `debug.html` via `docker cp` dans le container cloud (pas de rebuild). Vérifier le grind intact + le panel HTTP 200 après.

---

### Task 1: Lecteurs `debug_trace` — `events_command` + `capture_command`

**Files:**
- Modify: `debug_trace.py` (ajouter deux fonctions publiques en fin de module)
- Test: `tests/test_debug_trace_read.py`

**Interfaces:**
- Consumes: `debug_trace._TRACE_DIR`, `debug_trace._CAPTURE_DIR` (Path, déjà définis).
- Produces:
  - `events_command(args: dict) -> dict` → `{"ok": True, "count": int, "events": [dict,...]}` (ou `{"ok": False, "error": str}`). `args = {"limit": int=100, "day": str|None}`. `day` défaut = aujourd'hui (`time.strftime("%Y%m%d")`). Tail des `limit` dernières lignes (cap 1000), lignes malformées ignorées, fichier absent → events vides.
  - `capture_command(args: dict) -> dict` → `{"ok": True, "name": str, "jpeg_b64": str, "bytes": int}` ou `{"ok": False, "error": str}`. `args = {"name": str}`. Valide `name` (anti-traversal), lit `_CAPTURE_DIR/name`, base64.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_debug_trace_read.py
import base64
import json
import pytest


@pytest.fixture
def dt(tmp_path, monkeypatch):
    import debug_trace as d
    monkeypatch.setattr(d, "_TRACE_DIR", tmp_path / "trace")
    monkeypatch.setattr(d, "_CAPTURE_DIR", tmp_path / "trace" / "captures")
    (tmp_path / "trace" / "captures").mkdir(parents=True)
    return d


def _write_events(dt, day, records):
    p = dt._TRACE_DIR / f"events-{day}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_events_missing_file_returns_empty(dt):
    out = dt.events_command({"day": "20000101"})
    assert out == {"ok": True, "count": 0, "events": []}


def test_events_tail_and_limit(dt):
    recs = [{"event": "e", "i": i} for i in range(10)]
    _write_events(dt, "20260617", recs)
    out = dt.events_command({"day": "20260617", "limit": 3})
    assert out["ok"] is True
    assert out["count"] == 3
    assert [e["i"] for e in out["events"]] == [7, 8, 9]


def test_events_skips_malformed_lines(dt):
    p = dt._TRACE_DIR / "events-20260617.jsonl"
    p.write_text('{"event":"ok"}\nNOT JSON\n{"event":"ok2"}\n', encoding="utf-8")
    out = dt.events_command({"day": "20260617"})
    assert out["count"] == 2
    assert [e["event"] for e in out["events"]] == ["ok", "ok2"]


def test_events_limit_capped_at_1000(dt):
    _write_events(dt, "20260617", [{"event": "e", "i": i} for i in range(5)])
    out = dt.events_command({"day": "20260617", "limit": 99999})
    assert out["count"] == 5  # cap doesn't error, just bounds the slice


def test_capture_valid(dt):
    raw = b"\xff\xd8\xff\xe0jpegbytes"
    (dt._CAPTURE_DIR / "brawler_read_123_1.jpg").write_bytes(raw)
    out = dt.capture_command({"name": "brawler_read_123_1.jpg"})
    assert out["ok"] is True
    assert base64.b64decode(out["jpeg_b64"]) == raw
    assert out["bytes"] == len(raw)
    assert out["name"] == "brawler_read_123_1.jpg"


def test_capture_missing_returns_error(dt):
    out = dt.capture_command({"name": "nope.jpg"})
    assert out["ok"] is False
    assert "not found" in out["error"]


@pytest.mark.parametrize("bad", ["../secret", "a/b.jpg", "..", ".hidden", "", "x" * 81, "a\\b.jpg"])
def test_capture_rejects_traversal(dt, bad):
    out = dt.capture_command({"name": bad})
    assert out["ok"] is False
    assert "invalid" in out["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_debug_trace_read.py -v`
Expected: FAIL (`AttributeError: module 'debug_trace' has no attribute 'events_command'`)

- [ ] **Step 3: Implement the two functions**

Append to `debug_trace.py`:

```python
# ---- read side (used by worker_link debug commands) ------------------------

def events_command(args: dict) -> dict:
    """Return the tail of today's (or args['day']) events JSONL as a list.

    args: {"limit": int=100 (capped 1000), "day": "YYYYMMDD"|None}
    Missing file → empty list (not an error). Malformed lines are skipped.
    """
    try:
        limit = min(max(int(args.get("limit", 100) or 100), 1), 1000)
    except Exception:
        limit = 100
    day = args.get("day") or time.strftime("%Y%m%d")
    path = _TRACE_DIR / f"events-{day}.jsonl"
    if not path.exists():
        return {"ok": True, "count": 0, "events": []}
    events = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                pass  # best-effort writes can leave a partial line
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "count": len(events), "events": events}


def _valid_capture_name(name: str) -> bool:
    return bool(name) and len(name) <= 80 and not (
        "/" in name or "\\" in name or ".." in name or name.startswith(".")
    )


def capture_command(args: dict) -> dict:
    """Return a single capture JPEG as base64. args: {"name": str}.

    Validates name against path traversal; missing/GC'd capture → error dict.
    """
    name = str(args.get("name", "") or "")
    if not _valid_capture_name(name):
        return {"ok": False, "error": "invalid capture name"}
    path = _CAPTURE_DIR / name
    try:
        if not str(path.resolve()).startswith(str(_CAPTURE_DIR.resolve())):
            return {"ok": False, "error": "invalid capture name"}
    except Exception:
        return {"ok": False, "error": "invalid capture name"}
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": "capture not found"}
    try:
        import base64 as _b64
        data = path.read_bytes()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": name,
            "jpeg_b64": _b64.b64encode(data).decode("ascii"), "bytes": len(data)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_debug_trace_read.py tests/test_debug_trace.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add debug_trace.py tests/test_debug_trace_read.py
git commit -m "feat(debug): debug_trace events_command + capture_command (read side for viewer)"
```

---

### Task 2: Commandes worker `debug_events` + `debug_capture`

**Files:**
- Modify: `worker_link.py` (deux wrappers + entrées dans `COMMANDS`, dict à `worker_link.py:879-923`)

**Interfaces:**
- Consumes: `debug_trace.events_command`, `debug_trace.capture_command` (Task 1).
- Produces: commandes WS `"debug_events"` et `"debug_capture"` dans `COMMANDS`, appelables via `HUB.send_command(inst, "debug_events", {limit})` / `("debug_capture", {name})`.

> Note: `worker_link.py` n'importe pas sous Python 3.9 (signatures `X | None`), donc pas de test unitaire ici — la logique est intégralement testée dans Task 1 ; ces wrappers sont triviaux et vérifiés en live (Task 4).

- [ ] **Step 1: Add the two wrapper functions**

Dans `worker_link.py`, juste avant la définition du dict `COMMANDS` (avant `worker_link.py:879`), ajouter :

```python
# ---- debug trace (read side; logic lives in debug_trace) ----------

def _cmd_debug_events(args: dict) -> dict:
    """Tail of the debug-trace events JSONL. args = {limit:int, day:str?}."""
    try:
        import debug_trace
        return debug_trace.events_command(args or {})
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _cmd_debug_capture(args: dict) -> dict:
    """A single debug-trace capture as base64 JPEG. args = {name:str}."""
    try:
        import debug_trace
        return debug_trace.capture_command(args or {})
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
```

- [ ] **Step 2: Register them in `COMMANDS`**

Dans le dict `COMMANDS` (`worker_link.py:879-923`), avant l'accolade fermante `}` (après la ligne `"shop_upgrade_power": _cmd_shop_upgrade_power,`), ajouter :

```python
    # debug trace (read side for the /static/debug.html viewer)
    "debug_events":           _cmd_debug_events,
    "debug_capture":          _cmd_debug_capture,
```

- [ ] **Step 3: Sanity-check import locally (best-effort)**

Run: `python3 -c "import ast; ast.parse(open('worker_link.py').read()); print('syntax ok')"`
Expected: `syntax ok` (worker_link n'importe pas sous 3.9 ; on valide juste la syntaxe — le runtime worker est en 3.11+).

- [ ] **Step 4: Commit**

```bash
git add worker_link.py
git commit -m "feat(debug): worker debug_events + debug_capture commands (proxy to debug_trace)"
```

---

### Task 3: Page viewer `cloud_panel/static/debug.html`

**Files:**
- Create: `cloud_panel/static/debug.html`

**Interfaces:**
- Consumes (endpoints cloud existants) : `GET /api/instances` (liste `[{id, instance_id, ...}]`, `cloud_panel/app.py:426`) ; `POST /api/instances/{id}/cmd` body `{"name": "...", "args": {...}}` → `{"ok": True, "data": <worker result>}` (`cloud_panel/app.py:1087`).

> Page statique autonome — pas de test unitaire (HTML/JS), vérifiée en live (Task 4). Style sombre simple, indépendant du gros `app.js`.

- [ ] **Step 1: Create the viewer page**

Créer `cloud_panel/static/debug.html` :

```html
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Debug Trace — BrawlStar Bot</title>
<style>
  :root { --bg:#07090d; --surface:#11161f; --line:#1f2733; --txt:#cdd6e4; --mut:#7a8597; --pri:#4f8cf0; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt); font:14px/1.4 system-ui,sans-serif; }
  header { padding:12px 16px; border-bottom:1px solid var(--line); display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0 12px 0 0; }
  select, input, button { background:var(--surface); color:var(--txt); border:1px solid var(--line); border-radius:6px; padding:6px 10px; font:inherit; }
  button { cursor:pointer; }
  #list { padding:8px 16px; }
  .row { display:grid; grid-template-columns:150px 160px 1fr 64px; gap:10px; align-items:center; padding:8px; border-bottom:1px solid var(--line); cursor:pointer; }
  .row:hover { background:var(--surface); }
  .ev { font-weight:600; color:var(--pri); }
  .mut { color:var(--mut); font-size:12px; }
  .data { font-family:ui-monospace,monospace; font-size:12px; color:var(--txt); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .thumb { width:56px; height:32px; object-fit:cover; border-radius:4px; background:var(--surface); }
  #modal { position:fixed; inset:0; background:rgba(0,0,0,.85); display:none; align-items:center; justify-content:center; flex-direction:column; gap:12px; padding:20px; }
  #modal img { max-width:90vw; max-height:60vh; border-radius:8px; }
  #modal pre { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:12px; max-width:90vw; overflow:auto; color:var(--txt); }
  #modal .crop { max-height:120px; }
  #empty { color:var(--mut); padding:16px; }
</style>
</head>
<body>
<header>
  <h1>🐛 Debug Trace</h1>
  <select id="inst"></select>
  <label class="mut">event <input id="filter" placeholder="(tous)" size="14"></label>
  <label class="mut">limit <input id="limit" type="number" value="200" min="1" max="1000" size="5"></label>
  <button id="refresh">Rafraîchir</button>
  <label class="mut"><input type="checkbox" id="auto"> auto 5s</label>
  <span id="status" class="mut"></span>
</header>
<div id="list"></div>
<div id="empty">Choisis une instance.</div>
<div id="modal"><img id="m-frame"><img id="m-crop" class="crop" hidden><pre id="m-data"></pre><button onclick="document.getElementById('modal').style.display='none'">Fermer (Esc)</button></div>

<script>
const $ = s => document.querySelector(s);
let instances = [], autoTimer = null;

async function jget(path) { const r = await fetch(path); if (!r.ok) throw new Error(r.status); return r.json(); }
async function cmd(instId, name, args) {
  const r = await fetch(`/api/instances/${instId}/cmd`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name, args: args || {}, timeout_s: 8}),
  });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

async function loadInstances() {
  instances = await jget("/api/instances");
  $("#inst").innerHTML = instances.map(i =>
    `<option value="${i.id}">${i.instance_id}</option>`).join("");
  if (instances.length) refresh();
}

function summarize(data) {
  if (!data) return "";
  return Object.entries(data).map(([k, v]) =>
    `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ");
}

async function refresh() {
  const instId = $("#inst").value;
  if (!instId) return;
  const filt = $("#filter").value.trim().toLowerCase();
  const limit = Math.min(parseInt($("#limit").value) || 200, 1000);
  $("#status").textContent = "…";
  try {
    const res = await cmd(instId, "debug_events", {limit});
    const data = res.data || {};
    let events = (data.events || []);
    if (filt) events = events.filter(e => (e.event || "").toLowerCase().includes(filt));
    events.reverse(); // newest first
    render(instId, events);
    $("#status").textContent = `${events.length} events`;
  } catch (e) {
    $("#status").textContent = "erreur: " + e.message;
  }
}

function render(instId, events) {
  $("#empty").style.display = events.length ? "none" : "block";
  $("#list").innerHTML = events.map((e, idx) => {
    const t = (e.iso || "").replace("T", " ");
    const hasCap = !!e.capture;
    return `<div class="row" data-idx="${idx}">
      <div class="mut">${t}</div>
      <div><span class="ev">${e.event || "?"}</span><div class="mut">${e.account || ""}${e.tag ? " · " + e.tag : ""}</div></div>
      <div class="data">${summarize(e.data)}</div>
      <div>${hasCap ? '🖼️' : ''}</div>
    </div>`;
  }).join("");
  $("#list").querySelectorAll(".row").forEach(row => {
    row.onclick = () => openEvent(instId, events[parseInt(row.dataset.idx)]);
  });
}

async function openEvent(instId, e) {
  $("#m-data").textContent = JSON.stringify(e, null, 2);
  $("#m-frame").removeAttribute("src");
  $("#m-crop").hidden = true;
  $("#modal").style.display = "flex";
  if (e.capture) {
    try {
      const res = await cmd(instId, "debug_capture", {name: e.capture});
      if (res.data && res.data.jpeg_b64) $("#m-frame").src = "data:image/jpeg;base64," + res.data.jpeg_b64;
    } catch (_) {}
  }
  if (e.crop) {
    try {
      const res = await cmd(instId, "debug_capture", {name: e.crop});
      if (res.data && res.data.jpeg_b64) { $("#m-crop").src = "data:image/jpeg;base64," + res.data.jpeg_b64; $("#m-crop").hidden = false; }
    } catch (_) {}
  }
}

$("#refresh").onclick = refresh;
$("#inst").onchange = refresh;
$("#filter").onchange = refresh;
$("#auto").onchange = e => {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  if (e.target.checked) autoTimer = setInterval(refresh, 5000);
};
document.addEventListener("keydown", e => { if (e.key === "Escape") $("#modal").style.display = "none"; });
loadInstances();
</script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add cloud_panel/static/debug.html
git commit -m "feat(debug): static /static/debug.html trace viewer (events + captures via generic cmd proxy)"
```

---

### Task 4: Déployer + vérifier en live

**Files:** aucun (déploiement + vérification)

- [ ] **Step 1: Push main**

```bash
git push origin main
```
(Le commit touche worker_link.py + debug_trace.py → le webhook GitHub déclenche le git_update des workers, PAS un skip panel-only. `debug.html` est sous `cloud_panel/` mais le commit n'est pas panel-only → workers se mettent à jour.)

- [ ] **Step 2: Déployer le worker (HP)** — si le webhook ne l'a pas déjà fait

```bash
sshpass -p <hp_pw> ssh -p 2222 zeffut@72.60.94.131 'cd ~/BrawlStar-Bot && git fetch origin && git reset --hard origin/main && sudo systemctl restart brawlbot'
```
(Identifiants via `~/.claude/secrets.env` / mémoire `reference-credentials` — ne jamais afficher. `cfg/device.toml` untracked = préservé. ⚠️ sauvegarder d'abord `cfg/match_history.toml` + `latest_brawler_data.json` comme au déploiement précédent.)
Expected: `HEAD now at <sha>`, service actif, lobby atteint, `screenrec spawn 1280x576`, 0 traceback (observer ~100s de `journalctl -u brawlbot`).

- [ ] **Step 3: Déployer `debug.html` dans le container cloud** (docker cp — pas de rebuild)

```bash
# Copier le fichier sur le VPS puis dans le container du service panel.
sshpass -p <vps_pw> scp -P 22 cloud_panel/static/debug.html root@72.60.94.131:/tmp/debug.html
sshpass -p <vps_pw> ssh root@72.60.94.131 'CID=$(docker ps -q -f name=brawlstarbot-controlpanel); docker cp /tmp/debug.html $CID:/app/static/debug.html && echo copied to $CID'
```
(Container = service swarm `brawlstarbot-controlpanel-a7d2g7`, statics dans `/app/static/`, cf. mémoire `reference-credentials`. ⚠️ docker cp tient jusqu'au prochain reschedule swarm ; pour persister il faudra un rebuild image — acceptable pour un viewer de debug.)

- [ ] **Step 4: Vérifier la commande worker end-to-end (curl via le proxy cloud)**

```bash
# Récupérer un instance id, puis appeler debug_events via le proxy générique.
curl -s https://brawlpanel.zeffut.fr/api/instances | python3 -m json.tool | grep -E '"id"|instance_id' | head
curl -s -X POST https://brawlpanel.zeffut.fr/api/instances/<ID>/cmd \
  -H 'Content-Type: application/json' \
  -d '{"name":"debug_events","args":{"limit":5}}' | python3 -m json.tool | head -40
```
Expected: `{"ok": true, "data": {"ok": true, "count": N, "events": [...]}}` avec de vrais events `brawler_read`/`brawler_reconcile`/`match_end`.

- [ ] **Step 5: Vérifier une capture + la page**

```bash
# capture d'un event (prendre un name dans la sortie events ci-dessus)
curl -s -X POST https://brawlpanel.zeffut.fr/api/instances/<ID>/cmd \
  -H 'Content-Type: application/json' \
  -d '{"name":"debug_capture","args":{"name":"<capture_name>.jpg"}}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('ok' if d.get('data',{}).get('jpeg_b64') else d)"
# page accessible
curl -s -o /dev/null -w "%{http_code}\n" https://brawlpanel.zeffut.fr/static/debug.html
```
Expected: `ok` pour la capture (base64 présent) ; `200` pour la page. Ouvrir `https://brawlpanel.zeffut.fr/static/debug.html` dans un navigateur, choisir l'instance, voir les events + cliquer une vignette → la capture + le crop + le JSON s'affichent.

- [ ] **Step 6: Confirmer le grind intact**

```bash
sshpass -p <hp_pw> ssh -p 2222 zeffut@72.60.94.131 'journalctl -u brawlbot --since "3 min ago" --no-pager | grep -ciE "traceback|exception:"'
```
Expected: `0`. Si cassé → revert worker (`git reset --hard <sha avant>` + restart) et stop.

---

## Self-Review

**Spec coverage (vs §3.4 Phase 2 de la spec)** : routes worker events/capture → Tasks 1-2 (via debug_trace + worker_link, et non panel/app.py — simplification : proxy `/cmd` générique) ; proxy cloud → réutilisé (aucun code) ; page `/debug` → `cloud_panel/static/debug.html` servie à `/static/debug.html` (Task 3) ; liste filtrable + vignettes → Task 3 ; déploiement docker cp → Task 4. ✓

**Placeholder scan** : aucun TBD ; tout le code (helpers, wrappers, HTML/JS, commandes de déploiement) est fourni complet. Les `<ID>`/`<sha>`/`<hp_pw>` sont des valeurs runtime à substituer, pas des placeholders de code. ✓

**Type consistency** : `events_command(args)->{ok,count,events}` et `capture_command(args)->{ok,name,jpeg_b64,bytes}` identiques entre debug_trace (Task 1), les wrappers worker (Task 2) et la consommation front (Task 3 : `res.data.events`, `res.data.jpeg_b64`). Noms de commandes `debug_events`/`debug_capture` cohérents bout-en-bout. ✓

**Sécurité** : validation anti-traversal du `name` testée (Task 1, paramétrée) et appliquée côté lecture (la seule qui touche le disque). Le front n'envoie que `debug_events`/`debug_capture`. ✓
