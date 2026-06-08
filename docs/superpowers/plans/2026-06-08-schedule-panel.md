# Édition du planning depuis le panel — Implementation Plan

> Extension de `2026-06-08-schedule-config.md`. Ajoute l'édition de la couche locale (`cfg/schedule.local.toml`) depuis le panel cloud, appliquée à chaud (hot-reload, sans redémarrage).

**Goal:** Pouvoir lire + éditer le planning depuis `brawlpanel.zeffut.fr` (éditeur TOML des overrides + aperçu de la config effective), poussé au worker qui l'écrit dans `cfg/schedule.local.toml`.

**Architecture:** Calqué sur le pattern `alerts`. cloud_panel → `HUB.send_command` → `worker_link` → `panel/app.py` (process qui détient le singleton `play_schedule`). GET renvoie {raw local toml, effective live config}. PUT valide le TOML et écrit (ou supprime si vide) `cfg/schedule.local.toml` ; le hot-reload applique sous ~10 s.

**Tech:** Python 3.12 worker (tomllib dispo), FastAPI, vanilla JS. Worker écrit le TOML verbatim (pas de writer maison — on stocke le texte de l'utilisateur après validation `tomllib.loads`).

## Fichiers
- `panel/app.py` (worker) : + `GET /api/schedule`, `PUT /api/schedule`.
- `worker_link.py` : + `_cmd_schedule_get`, `_cmd_schedule_set` + entrées COMMANDS.
- `cloud_panel/app.py` : + `GET/PUT /api/instances/{id}/schedule`.
- `cloud_panel/static/index.html` + `app.js` : section « Planning ».

## Task 1 — Worker : endpoints `panel/app.py`

Ajouter (après le bloc alerts). `SCHEDULE_LOCAL_PATH = "cfg/schedule.local.toml"`.

```python
import os, tomllib

@app.get("/api/schedule")
def api_get_schedule() -> dict:
    """Return the editable local override (raw TOML text) + the live effective
    config from the running play_schedule singleton."""
    raw = ""
    try:
        if os.path.exists("cfg/schedule.local.toml"):
            with open("cfg/schedule.local.toml", "r", encoding="utf-8") as f:
                raw = f.read()
    except OSError:
        pass
    effective = {}
    try:
        import play_schedule, time as _t
        s = play_schedule.get()
        s.state()  # force a day-resolve so _today_* are current
        effective = {
            "enabled": s.enabled,
            "state": s.state(),
            "sleep": f"{play_schedule._hhmm(s._today_sleep.start_min)}-{play_schedule._hhmm(s._today_sleep.end_min)}",
            "today_cap": s._today_cap,
            "matches_today": s._matches_today,
            "today_block_cap": s._today_block_cap,
            "blocks_today": s._blocks_today,
            "pause_windows": len(s._today_pause_windows),
            "is_dayoff": s._today_is_dayoff,
            "block_minutes": f"{s.block_min}-{s.block_max}",
            "break_minutes": f"{s.break_min}-{s.break_max}",
        }
    except Exception as exc:
        effective = {"error": str(exc)}
    return {"raw": raw, "effective": effective}


class ScheduleUpdate(BaseModel):
    toml: str = ""


@app.put("/api/schedule")
def api_put_schedule(payload: ScheduleUpdate) -> dict:
    """Write the local schedule override. Validates it parses as TOML first.
    Empty text deletes the file (revert to committed defaults). Hot-reload
    applies within ~10s — no restart."""
    text = (payload.toml or "").strip()
    if not text:
        try:
            if os.path.exists("cfg/schedule.local.toml"):
                os.remove("cfg/schedule.local.toml")
        except OSError as exc:
            return {"ok": False, "error": f"remove failed: {exc}"}
        return {"ok": True, "removed": True}
    try:
        parsed = tomllib.loads(text)
    except Exception as exc:
        return {"ok": False, "error": f"TOML invalide: {exc}"}
    # Accept either a [schedule] table or flat keys (loader handles both).
    if "schedule" not in parsed and not parsed:
        return {"ok": False, "error": "config vide"}
    try:
        os.makedirs("cfg", exist_ok=True)
        with open("cfg/schedule.local.toml", "w", encoding="utf-8") as f:
            f.write(text + ("\n" if not text.endswith("\n") else ""))
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "bytes": len(text)}
```

Tests: `python3 -c "import ast; ast.parse(open('panel/app.py').read())"` (full env unavailable locally → compile-check). Real validation on HP.

## Task 2 — Worker : proxy `worker_link.py`

Add handlers + register in COMMANDS (`LOCAL_PANEL` exists):

```python
def _cmd_schedule_get(args: dict) -> dict:
    return _local_get("/api/schedule")

def _cmd_schedule_set(args: dict) -> dict:
    import requests
    r = requests.put(f"{LOCAL_PANEL}/api/schedule",
                     json={"toml": args.get("toml", "")}, timeout=10)
    r.raise_for_status()
    return r.json()
```
COMMANDS += `"schedule_get": _cmd_schedule_get, "schedule_set": _cmd_schedule_set,`.

## Task 3 — Cloud : proxy `cloud_panel/app.py`

```python
class SchedulePayload(BaseModel):
    toml: str = ""

@app.get("/api/instances/{instance_db_id}/schedule")
async def api_instance_schedule_get(instance_db_id: int) -> dict:
    inst_id = _resolve_instance(instance_db_id)
    if not inst_id:
        raise HTTPException(404, "instance not found")
    try:
        data = await HUB.send_command(inst_id, "schedule_get", {}, timeout_s=12)
        return {"ok": True, "data": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

@app.put("/api/instances/{instance_db_id}/schedule")
async def api_instance_schedule_put(instance_db_id: int, payload: SchedulePayload) -> dict:
    inst_id = _resolve_instance(instance_db_id)
    if not inst_id:
        raise HTTPException(404, "instance not found")
    try:
        data = await HUB.send_command(inst_id, "schedule_set", {"toml": payload.toml}, timeout_s=12)
        return {"ok": True, "data": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
```
(Use the existing `_resolve_instance`, `HUB`, `HTTPException`, `BaseModel` already imported in cloud_panel/app.py.)

## Task 4 — Cloud UI : `cloud_panel/static`

In `index.html`, add a "Planning" card (near the instance/device controls) with: a `<pre id="sched-effective">` for the effective summary, a `<textarea id="sched-toml">`, a `<button id="sched-load">↻</button>`, a `<button id="sched-save">Appliquer</button>`, and a `<span id="sched-status">`.

In `app.js`, add load/save wired to the selected instance (reuse `selectedInstanceForDevice` and the `api()` helper):
```javascript
async function loadSchedule() {
  if (!selectedInstanceForDevice) return;
  const r = await api(`/api/instances/${selectedInstanceForDevice}/schedule`);
  if (r.ok && r.data) {
    document.getElementById("sched-toml").value = r.data.raw || SCHED_TEMPLATE;
    const e = r.data.effective || {};
    document.getElementById("sched-effective").textContent =
      e.error ? ("erreur: " + e.error) :
      `état=${e.state} · sommeil ${e.sleep} · quota ${e.matches_today}/${e.today_cap}` +
      ` · blocs ${e.blocks_today}/${e.today_block_cap||"∞"} · pauses ${e.pause_windows}` +
      (e.is_dayoff ? " · JOUR DE REPOS" : "");
  }
}
async function saveSchedule() {
  const toml = document.getElementById("sched-toml").value;
  const st = document.getElementById("sched-status");
  st.textContent = "envoi…";
  const r = await api(`/api/instances/${selectedInstanceForDevice}/schedule`,
                      {method: "PUT", body: JSON.stringify({toml})});
  const d = r.data || {};
  st.textContent = (r.ok && d.ok) ? "✅ appliqué (à chaud sous ~10s)"
                                  : "❌ " + (d.error || r.error || "échec");
  if (r.ok && d.ok) setTimeout(loadSchedule, 1500);
}
```
`SCHED_TEMPLATE` = a commented `[schedule]` starter string. Wire buttons + load on instance select. Match existing panel styles/classes.

Verify: `node --check cloud_panel/static/app.js`.

## Task 5 — Deploy + verify (HP, manual)
Merge → push → git_update. On the panel: open the instance, load schedule (see effective config + raw), edit (e.g. add a pause window in the current hour), Apply → confirm worker writes `cfg/schedule.local.toml`, log `config reloaded`, state → `pause`. Then clear (empty → file removed → back to committed 180).

## Self-review
- Coverage: GET/PUT worker (T1), proxy (T2), cloud proxy (T3), UI (T4), deploy (T5). ✓
- Reuses alerts/battery command pattern + existing cloud helpers. ✓
- Empty-text → delete file (clean revert). TOML validated before write. Hot-reload = no restart. ✓
