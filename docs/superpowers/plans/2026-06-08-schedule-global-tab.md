# Onglet Planning global — Implementation Plan

Remplace l'éditeur per-instance (device console) par une **config globale** stockée en DB cloud, éditée via un **vrai onglet** avec **formulaire soigné**, appliquée à tous les workers (fan-out, hot-reload). Réutilise les commandes worker `schedule_get`/`schedule_set` (déjà déployées) → **aucun changement worker**, redéploiement cloud (Dokploy) seul.

**Modèle de config (JSON, stocké via `db.set_config("schedule", cfg)`):**
```
enabled:bool, timezone:str,
sleep_start_hour:int, sleep_end_hour:int, sleep_jitter_minutes:int,
block_min_minutes:int, block_max_minutes:int, break_min_minutes:int, break_max_minutes:int,
max_blocks_per_day:int, blocks_jitter:int, daily_match_cap:int, daily_cap_jitter:int,
dayoff_weekdays:[str], dayoff_chance:float,
pause_windows:[{start,end,jitter_minutes,label}], weekend:{<scalar overrides>}
```

## Backend (cloud_panel)
- `cloud_panel/schedule_config.py` (NEW): `DEFAULTS`, `merge_defaults(cfg)`, `to_toml(cfg)` (génère `[schedule]` + `[schedule.weekend]` + `[[schedule.pause_windows]]`, ordre TOML valide). + tests round-trip (`to_toml` → `tomllib.loads`).
- `cloud_panel/app.py`:
  - `GET /api/config/schedule` → `merge_defaults(db.get_config("schedule"))` + `effective` (best-effort `schedule_get` du 1er worker connecté via `HUB.list()`).
  - `PUT /api/config/schedule` → valide, `db.set_config("schedule", merged)`, `to_toml`, **fan-out** `schedule_set` à `HUB.list()`, renvoie `{ok, applied:[...], config}`.
  - SUPPRIMER `GET/PUT /api/instances/{id}/schedule` + `SchedulePayload`.

## Frontend (cloud_panel/static)
- SUPPRIMER la carte « Planning » du `#device-panel` (index.html + les fns loadSchedule/saveSchedule liées à l'instance dans app.js, + l'appel dans refreshDevicePanel/openDeviceConsole).
- `index.html`: bouton header `⏰ Planning` (après `btn-fleet-overview`) + section `#planning-view` (full view, togglée comme la flotte) contenant le formulaire (cartes : état live, sommeil, sessions, quota, jour de repos, fenêtres de pause, week-end repliable, frise 24h, bouton Appliquer + statut).
- `app.js`: `showPlanningView()` (hide autres vues), `loadGlobalSchedule()` (GET → remplit le formulaire + bannière effective + frise), `collectScheduleForm()` (form → JSON), `saveGlobalSchedule()` (PUT → statut). Pause-windows dynamiques (add/remove). Wire le bouton header + nav.
- Style : réutiliser le thème sombre + classes existantes (`header-btn`, `cfg-section`, `cfg-fields`, `cfg-toggle`), ajouter des contrôles soignés (chips jours, lignes pause, slider proba, frise CSS). Suivre frontend-design (hiérarchie claire, espace, feedback).

## Deploy
Push → Dokploy redéploie le cloud. Worker inchangé (schedule_get/set déjà là). Vérif : onglet Planning charge la config + effective, modif + Appliquer → fan-out → worker hot-reload (cap/pauses changent), reload de l'onglet reflète l'effective.

## Self-review
- Global (DB canonique + fan-out), vrai onglet (header + full view), formulaire (pas de TOML brut). ✓
- Réutilise schedule_get/set (worker intact) + db.get/set_config + HUB.list fan-out. ✓
- Per-jour précis = hors périmètre (avancé futur), noté. ✓
