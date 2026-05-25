# BrawlStar-Bot v2 — Design

**Date** : 2026-05-25
**Auteur** : Zeffut + Claude
**Statut** : design validé, en attente de plan d'implémentation

---

## 1. Contexte et objectif

`BrawlStar-Bot` v1 est un bot Brawl Stars maison écrit en Python qui s'appuyait sur de la capture de fenêtre BlueStacks (Quartz/mss), du template matching OpenCV, de l'OCR Tesseract et des mouvements aléatoires. Il fonctionne mais reste un POC très limité.

L'objectif de la v2 est de **reconstruire** le bot autour d'une architecture multi-thread propre, en jouant sur un **téléphone Android physique branché en USB** au Mac (plus d'émulateur), et en **réutilisant les modèles ML et la logique de combat de PylaAI** (branche `compatibility` du repo officiel) en mode hybride : on copie les modules complexes et déjà éprouvés (modèles ONNX, code de détection, base de données brawlers), on réécrit le reste (orchestration, capture, contrôle) pour qu'il soit adapté à notre setup et à notre sauce.

**Objectif v1 atteignable** : faire tourner sans intervention humaine une session de farm en **Solo Showdown** avec **Colt**, avec une IA combat de niveau "full" (pathfinding tile-based, esquive, gestion super).

**Hors scope v1** :
- Multi-brawler / sélection automatique
- Modes coopératifs (Gem Grab, Brawl Ball…)
- GUI graphique
- Notifications Telegram/Discord (peut s'ajouter plus tard)
- Sélection de map / refus de map
- Système de login cloud PylaAI

---

## 2. Contraintes et choix structurants

| Contrainte / choix | Justification |
|---|---|
| Téléphone Android dédié + USB | Latence ~10ms vs 50-200ms en Wi-Fi ADB. Setup le plus fiable pour 24/7. |
| Mac comme cerveau (modèles ML + logique) | Apple Silicon + CoreMLExecutionProvider → inference ONNX rapide et native. |
| Solo Showdown comme mode v1 | Le mode le plus simple à automatiser : pas d'objectif coopératif, juste survivre + tirer. |
| Colt comme brawler v1 | Tir en ligne droite longue portée, super en ligne droite. Simple à modéliser. |
| IA combat full (pathfinding, esquive, super) | Choix utilisateur. On vise une qualité comparable à PylaAI. |
| Réutilisation hybride PylaAI | Économise des semaines de travail tout en gardant un code à nous. Licence "No Selling" respectée (usage perso, pas de redistribution publique). |
| Interface CLI seule | Minimal, suffisant pour MVP. Pas de Telegram en v1. |
| Architecture multi-thread (CaptureWorker, VisionWorker, BrainWorker, ControlWorker) | Permet ~30 IPS vs ~15 IPS en linéaire. Décorrèle la latence d'inference de la capture, ce qui est important pour l'esquive de projectiles. |

---

## 3. Architecture haut niveau

```
                            ┌─────────────────────────────┐
                            │       PHONE Android         │
                            │      (Brawl Stars)          │
                            └──────────┬──────────────────┘
                                       │ USB
                                       │ ADB + scrcpy stream
                                       ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │                              MAC                                       │
   │                                                                        │
   │   [Thread 1: CaptureWorker]                                            │
   │   scrcpy_client → décode H.264 → push dans LatestFrameBuffer (1 slot)  │
   │                         │                                              │
   │                         ▼                                              │
   │            ┌─────────────────────────┐                                 │
   │            │  LatestFrameBuffer      │ ← thread-safe, drop-old        │
   │            └────────────┬────────────┘                                 │
   │                         │                                              │
   │   [Thread 2: VisionWorker]                                             │
   │   pull frame → run state_finder (lobby/match/end/popup)                │
   │              → si match: run brawlersInGame + tileDetector ONNX        │
   │              → push detections dans GameStateBus                       │
   │                         │                                              │
   │                         ▼                                              │
   │            ┌─────────────────────────┐                                 │
   │            │  GameStateBus           │ ← derniers résultats + état    │
   │            └────────────┬────────────┘                                 │
   │                         │                                              │
   │   [Thread 3: BrainWorker]                                              │
   │   lit GameStateBus, délègue à la Strategy adéquate                     │
   │     - état lobby  → MenuStrategy.click_play                            │
   │     - état match  → ColtStrategy.decide(GameState)                     │
   │     - état end    → MenuStrategy.click_continue                        │
   │                         │                                              │
   │                         ▼                                              │
   │            ┌─────────────────────────┐                                 │
   │            │  ControlBus (queue)     │                                 │
   │            └────────────┬────────────┘                                 │
   │                         │                                              │
   │   [Thread 4: ControlWorker]                                            │
   │   pull commands → traduit en inputs ADB (tap, swipe, joystick)         │
   │                                                                        │
   │   [Main thread: CLI/logging]                                           │
   │   affiche stats, gère SIGINT pour arrêt propre                         │
   └───────────────────────────────────────────────────────────────────────┘
```

### Composants

1. **CaptureWorker** — wrapper autour de `py-scrcpy-client`. Garde toujours la frame la plus récente dans `LatestFrameBuffer` (slot unique, écrase les anciennes pour éviter le lag).
2. **VisionWorker** — charge les 4 modèles ONNX au démarrage. Sur chaque tick : pull la dernière frame, run `state_finder` (template matching, cheap), puis si état `match` run `brawlersInGame.onnx` + `tileDetector.onnx`. Push un `GameState` dans le bus.
3. **BrainWorker** — orchestrateur. Lit le `GameState`, dispatche vers la `Strategy` correspondante au state courant. Une `Strategy` retourne une `Action` (ou rien).
4. **Strategies**
   - **MenuStrategy** : gère lobby/end/popup (clique sur Play, Continue, ferme les popups).
   - **ColtStrategy** : combat Colt v1 — kite, tir, super, ramassage cubes, fuite zone qui rétrécit, pathfinding tile-based autour des murs.
5. **ControlWorker** — abstraction au-dessus d'`adbutils`. Traduit une `Action` en commande ADB (`input tap X Y`, `input swipe` pour joystick virtuel).

### Communication inter-thread

- `LatestFrameBuffer` : slot unique avec lock, `set()` écrase, `get()` retourne la dernière (drop-old).
- `GameStateBus` : similaire, slot unique du dernier `GameState` calculé.
- `ControlBus` : `queue.Queue` FIFO classique des `Action` à exécuter.
- `stop_event` : `threading.Event()` partagé, mis à `True` sur SIGINT, chaque worker check à chaque itération.

---

## 4. Stack technique

| Couche | Lib | Version | Pourquoi |
|---|---|---|---|
| Capture vidéo téléphone | `py-scrcpy-client` | 0.5.0+ | Standard de fait, USB, frames PIL/numpy directes. Déjà installé via setup PylaAI. |
| Inputs téléphone | `adbutils` | 2.12.0 | Tap/swipe propres et fiables. |
| Inference ML | `onnxruntime` | 1.26+ | `CoreMLExecutionProvider` natif Apple Silicon. |
| Post-process détections | `ultralytics.utils.nms` + numpy | — | Repris de `detect.py` PylaAI. |
| Template matching state | `opencv-python` | 4.8 | `cv2.matchTemplate` standard. |
| Config | `tomllib` (stdlib) | Python 3.11+ | Zéro dep. |
| Logs | `logging` stdlib + `rich` (optionnel) | — | Affichage CLI propre. |
| Tests | `pytest` | — | Standard. |
| Python | 3.12 (Homebrew) | 3.12.13 | Version utilisée par PylaAI compat, testée. |

**Environnement Python** : `venv` propre dans `BrawlStar-Bot/venv`, créé à partir d'un `requirements.txt` minimal listant uniquement les deps nécessaires (pas tout l'écosystème PylaAI). Garantit la reproductibilité et l'indépendance vis-à-vis du repo `PylaAI-compat` (qu'on peut supprimer une fois les modèles + fichiers utiles copiés).

---

## 5. Structure des fichiers

```
BrawlStar-Bot/
├── README.md                     ← réécrit pour la v2
├── pyproject.toml
├── requirements.txt
├── config.toml                   ← config user (adb device, mode, brawler, durée session)
├── src/
│   └── bsbot/
│       ├── __init__.py
│       ├── main.py               ← entrée CLI, démarre les threads
│       ├── buses.py              ← LatestFrameBuffer, GameStateBus, ControlBus
│       ├── workers/
│       │   ├── __init__.py
│       │   ├── capture.py        ← CaptureWorker (scrcpy)
│       │   ├── vision.py         ← VisionWorker (ONNX)
│       │   ├── brain.py          ← BrainWorker (dispatch strategies)
│       │   └── control.py        ← ControlWorker (ADB)
│       ├── vision/
│       │   ├── __init__.py
│       │   ├── detect.py         ← COPIÉ de PylaAI (avec attribution)
│       │   ├── state_finder.py   ← template matching lobby/match/end/popup
│       │   └── postprocess.py    ← NMS + parsing en GameState
│       ├── strategies/
│       │   ├── __init__.py
│       │   ├── base.py           ← interface Strategy abstraite
│       │   ├── menu.py           ← MenuStrategy
│       │   └── colt.py           ← ColtStrategy (combat)
│       ├── controls/
│       │   ├── __init__.py
│       │   ├── adb.py            ← wrapper adbutils
│       │   └── inputs.py         ← Action dataclass + ADB translation
│       ├── models/
│       │   ├── mainInGameModel.onnx     ← COPIÉ de PylaAI
│       │   ├── brawlersInGame.onnx      ← COPIÉ de PylaAI
│       │   ├── tileDetector.onnx        ← COPIÉ de PylaAI
│       │   └── startingScreenModel.onnx ← COPIÉ de PylaAI
│       ├── data/
│       │   ├── brawlers_info.json       ← COPIÉ de PylaAI
│       │   └── state_templates/         ← screenshots templates state_finder
│       └── utils/
│           ├── __init__.py
│           ├── logging.py
│           └── geometry.py       ← distance, angle, line-of-sight
├── tests/
│   ├── conftest.py
│   ├── test_buses.py
│   ├── test_postprocess.py
│   ├── test_state_finder.py
│   ├── test_colt_strategy.py
│   └── fixtures/                 ← screenshots de test
├── tools/
│   ├── capture_template.py       ← capture screenshots templates depuis phone
│   └── replay_session.py         ← rejoue une session enregistrée pour debug
├── legacy/                       ← ancien code v1 (à supprimer une fois MVP OK)
│   ├── macos.py
│   ├── windows.py
│   ├── pipette.py
│   ├── select_zone.py
│   ├── references/
│   └── references_win/
└── docs/
    └── superpowers/specs/
        └── 2026-05-25-bsbot-design.md  ← ce document
```

L'ancien code v1 est **déplacé dans `legacy/`** au début de l'implémentation, pas supprimé. Il sera supprimé une fois le MVP v2 stabilisé (jalon M6).

---

## 6. Data flow détaillé

**Cycle de vie d'un tick (~33 ms cible, soit 30 IPS)** :

1. **`CaptureWorker`** : `scrcpy` pousse une frame H.264 décodée. Resize éventuel (downscale 1080p → 720p si besoin de perf). `LatestFrameBuffer.set(frame)` (atomique, écrase l'ancienne).

2. **`VisionWorker`** : pull la dernière frame
   - resize en 640×640 (input des modèles YOLOv8)
   - `state_finder.detect(frame)` → renvoie `state ∈ {lobby, match, end, popup, disconnect, unknown}`
   - si `state == match` : run `brawlersInGame.onnx` + `tileDetector.onnx` (idéalement en parallèle, sinon séquentiel)
   - construit un `GameState`
     ```python
     @dataclass
     class GameState:
         state: str
         frame_id: int
         timestamp: float
         my_pos: tuple[int, int] | None
         enemies: list[Enemy]            # bbox + class + confidence
         walls: list[BBox]
         bushes: list[BBox]
         power_cubes: list[BBox]
         my_health_pct: float | None
         my_super_charge_pct: float | None
         my_gadget_available: bool | None
         danger_zone: Polygon | None     # zone qui se rétrécit
     ```
   - `GameStateBus.set(game_state)`

3. **`BrainWorker`** : pull `GameState`
   - switch sur `state` :
     - `lobby` → `MenuStrategy.handle_lobby(gs)` → `Action(tap, play_button_coords)`
     - `match` → `ColtStrategy.decide(gs)` → `Action(...)` (voir §7)
     - `end` → `MenuStrategy.handle_end(gs)` → `Action(tap, continue_button_coords)`
     - `popup` → `MenuStrategy.close_popup(gs)`
     - `disconnect` → `MenuStrategy.reconnect(gs)`
   - `ControlBus.put(action)`

4. **`ControlWorker`** : consume `Action`
   - `Action(type="tap", x, y)` → `device.shell(f"input tap {x} {y}")`
   - `Action(type="swipe", x1, y1, x2, y2, dur_ms)` → `device.shell(f"input swipe {x1} {y1} {x2} {y2} {dur_ms}")`
   - `Action(type="joystick", dx, dy)` → calcule un swipe maintenu autour du centre du joystick virtuel (les joysticks Brawl Stars sont des swipes longs maintenus, on les simule avec des swipes courts répétés)
   - `Action(type="release_joystick")` → simule fin de swipe

---

## 7. Logique de la `ColtStrategy`

Colt est un brawler à longue portée, tir en ligne droite, 3 munitions, super en ligne droite qui traverse plusieurs ennemis et casse les murs.

**Priorités de décision (chaque tick, dans l'ordre)** :

1. **Survie immédiate** : si `my_health_pct < 30%` et un ennemi est `< 400 unités` → fuir dans la direction opposée (joystick).
2. **Fuite zone** : si `my_pos` est dans la `danger_zone` ou à `< 100 unités` du bord → joystick vers le centre safe.
3. **Tir opportuniste** : pour chaque ennemi détecté, calculer `line_of_sight(my_pos, enemy_pos, walls)`. Si LoS clair et distance `< colt.attack_range (546)` → tirer (tap sur bouton attack ou tap sur ennemi pour attaque dirigée).
4. **Super** : si `my_super_charge_pct >= 100%` ET au moins 2 ennemis alignés OU 1 ennemi derrière un mur cassable → utiliser super (tap bouton super).
5. **Ramassage cubes** : si un `power_cube` est visible dans un rayon de `300 unités` ET aucun ennemi dans `400 unités` → pathfinding vers le cube.
6. **Positionnement** : sinon, se déplacer vers un point qui maximise : (a) distance au plus proche ennemi (kite), (b) proximité aux power cubes restants, (c) couverture (bush proche).

**Pathfinding** : grille 2D dérivée du `tileDetector` (chaque tile fait 64×64 px, mark tile comme bloquante si > 50% wall). A* simple avec heuristique Manhattan. Recalcul de chemin toutes les 5 ticks (économise CPU).

**Référence** : `brawlers_info.json["colt"]` fournit :
- `safe_range: 324` — distance minimale à laquelle se tenir d'un ennemi
- `attack_range: 546` — portée du tir basique
- `super_range: 704` — portée du super
- `super_type: "damage"`
- `ignore_walls_for_attacks: false` — Colt ne tire pas à travers les murs
- `ignore_walls_for_supers: false` — sauf si super casse mur (à vérifier)

---

## 8. Robustesse / error handling

| Cas | Détection | Action |
|---|---|---|
| Phone déconnecté USB | `adbutils` raise `AdbError` | `CaptureWorker` retry exponentiel (1s, 2s, 4s, 8s), au-delà → arrêt propre |
| Frame stale (>2s) | `BrainWorker` check `time.time() - gs.timestamp` | met inputs en pause (release joystick), attend reprise |
| ONNX crash sur une frame | `try/except` dans `VisionWorker` | log + skip frame ; au-delà de 10 erreurs consécutives → arrêt |
| Brawl Stars crash / déconnexion | `state_finder` retourne `disconnect` | `MenuStrategy.reconnect()` tap sur écran pour reconnecter |
| Detection lobby figée >60s | `BrainWorker` track `state` history | tap fallback (recovery click au centre) |
| Bot stuck dans un état inconnu >5min | watchdog dans main thread | log + arrêt propre + envoi notif (futur) |
| SIGINT (Ctrl+C) | handler dans main thread | set `stop_event`, chaque worker drain et exit |

**Logs structurés** : JSON Lines dans `~/.bsbot/logs/session-{ISO-timestamp}.jsonl`. Chaque ligne = un événement (`state_change`, `action`, `detection_summary`, `error`). Permet de rejouer une session pour debug.

---

## 9. Configuration

`config.toml` à la racine du projet :

```toml
[adb]
device_serial = ""          # vide = premier device dispo, sinon "abc123def456"

[game]
mode = "solo_showdown"
brawler = "colt"

[session]
max_duration_minutes = 600   # 10h max par défaut, 0 = illimité
max_matches = 0              # 0 = illimité

[performance]
target_ips = 30
scrcpy_max_size = 1280       # downscale stream pour perf
inference_device = "auto"    # auto = CoreML si dispo, sinon CPU

[debug]
log_level = "INFO"
save_screenshots_on_error = true
record_session = false       # enregistre toutes les frames pour replay
```

---

## 10. Tests

**Unit tests** :
- `test_buses.py` : thread safety du `LatestFrameBuffer`, FIFO du `ControlBus`
- `test_postprocess.py` : parsing de détections en `GameState` à partir de tensors mockés
- `test_state_finder.py` : détection d'état sur screenshots du dossier `fixtures/`
- `test_colt_strategy.py` : `ColtStrategy.decide(gs)` retourne l'action attendue pour des `GameState` construits à la main (survie, kite, tir, super…)
- `test_geometry.py` : distance, angle, line-of-sight avec murs

**Pas de tests d'intégration phone-réel en CI** (impossible). Mais le script `tools/replay_session.py` permet de rejouer un JSONL + les frames enregistrées pour vérifier que la séquence de décisions reste stable après refacto.

**Cible coverage MVP** : 70% sur `strategies/` et `vision/postprocess.py` (les modules les plus prone aux régressions).

---

## 11. Roadmap d'implémentation (jalons)

| Jalon | Périmètre | Effort estimé |
|---|---|---|
| **M1 — Capture & inputs** | Connexion ADB, scrcpy stream affiché en cv2 window, tap manuel via CLI. Vérifier que tout marche avec le téléphone. Setup repo, requirements, structure dossiers. | 2-3 jours |
| **M2 — Vision pipeline** | Copie `detect.py` + modèles ONNX, charge les 4 modèles, affiche les détections overlay sur le stream. Le bot ne fait rien d'autre que regarder. | 3-4 jours |
| **M3 — Orchestration & menus** | `state_finder` complet (capture templates depuis phone), `MenuStrategy` qui clique play/continue. Le bot lance un match tout seul (sans jouer) et retourne au lobby. | 3-4 jours |
| **M4 — Combat Colt basique** | `ColtStrategy.decide()` v1 — kite + tir vers ennemi le plus proche en LoS, fuir la zone. Pas de super ni cubes. | 1 semaine |
| **M5 — Combat Colt complet** | Ramassage cubes, super utilisé intelligemment, pathfinding tile-based autour des murs. | 1-2 semaines |
| **M6 — Stabilisation** | 10h+ session non-stop sans crash, métriques (matches joués, victoires, win rate, IPS moyens). Suppression du `legacy/`. | 1 semaine |

**Total MVP** : ~5-7 semaines à temps partiel.

---

## 12. Licence et attribution

PylaAI est sous licence "No Selling". Notre projet :
- **N'est pas vendu, ni monétisé**, conformément à la licence.
- **Reste privé** (pas de redistribution publique), conformément à la règle Discord PylaAI #10 ("Sharing a 'cheat' that you're making isn't allowed unless it's only for yourself").
- **Attribue PylaAI** dans le README et dans chaque fichier copié, avec un commentaire en en-tête :

```python
# This file is adapted from PylaAI (https://github.com/PylaAI/PylaAI)
# Licensed under "No Selling" terms. Personal use only.
# Original authors: ivanyordanovgt, AngelFireLA, awarzu, Maayan080 (Mac port)
```

- Les modèles ONNX (`models/*.onnx`) sont copiés tels quels avec attribution dans `models/README.md`.

---

## 13. Risques connus et mitigations

| Risque | Probabilité | Mitigation |
|---|---|---|
| Modèles PylaAI entraînés sur capture émulateur, perfs dégradées sur stream phone | Moyenne | Tester tôt (M2), si problème : retrain léger ou ajuster preprocessing (resize/crop) |
| Inputs ADB pas assez réactifs pour gameplay temps réel | Faible | Mesurer latence end-to-end à M1, ajuster si > 100ms |
| Bot détecté par Supercell et compte ban | Faible mais non nulle | C'est un risque connu et accepté par le user, mentionné dans README v1 |
| scrcpy lag/freeze en USB intensif | Faible | Retry automatique dans `CaptureWorker`, restart scrcpy au pire |
| Mac entre en veille et coupe le bot | Moyenne | Préfixer `caffeinate -i python …` ou doc dans README |

---

## Annexe A : décisions tranchées au design

- **Pas de Telegram en v1** — utilisateur veut CLI seulement, on pourra ajouter en v2.
- **Pas de multi-brawler en v1** — Colt only. Architecture `Strategy` permet d'en ajouter sans refacto.
- **Pas de GUI** — CLI suffisant.
- **Pas d'OCR trophées en v1** — pas nécessaire pour "enchaîner les matches", peut être ajouté au M6 si utile pour les métriques.
- **Multi-thread plutôt qu'asyncio** — meilleur trade-off complexité/perf pour ce use case.
- **Hybride PylaAI** — copie modèles ONNX + `detect.py` + `brawlers_info.json` avec attribution, réécrit tout le reste.
