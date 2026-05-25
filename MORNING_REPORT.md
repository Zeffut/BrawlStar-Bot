# Rapport du matin — BrawlStar-Bot v2

> Travail effectué pendant que tu dormais (nuit du 2026-05-25).
> Tu peux supprimer ce fichier une fois lu.

## TL;DR

✅ **Squelette complet du bot v2 codé, 101 tests verts, dry-run validé.**

🔴 **Un seul blocker à débloquer toi-même au réveil** : la popup USB
debugging sur ton téléphone Android n'a pas été acceptée. Tant qu'elle ne
l'est pas, aucun test sur device réel n'est possible.

## Ce qui a été fait

| Jalon | Statut | Détail |
|---|---|---|
| Design doc | ✅ | `docs/superpowers/specs/2026-05-25-bsbot-design.md` (382 lignes) |
| M1 — Capture & inputs | ✅ | buses, controls/adb, workers/capture, workers/control |
| M2 — Vision pipeline | ✅ | detect.py (adapté de PylaAI), state_finder, postprocess, vision worker |
| M3 — Strategies & menus | ✅ | base, menu, colt, brain worker |
| M5a — A\* pathfinding | ✅ | Bonus : pathfinding tile-based intégré dans ColtStrategy |
| Main entry + CLI | ✅ | `python -m bsbot.main` (et `--dry-run` pour smoke test) |
| README + install.sh | ✅ | Documentation complète |
| Tests | ✅ | **101 tests pytest verts**, tous avec mocks |

### Commits

```
a1c9465 feat(strategies): A* tile-based pathfinding (M5a)
7d53ef7 feat(v2): full M1+M2+M3 skeleton — buses, workers, strategies, ONNX wiring
73f801b docs: add v2 design spec (bsbot multi-thread, Solo Showdown + Colt)
```

### Structure du repo

```
BrawlStar-Bot/
├── README.md                 ← réécrit pour v2
├── MORNING_REPORT.md         ← ce fichier
├── install.sh                ← installer (gère conflits scrcpy/adbutils)
├── config.toml               ← config user
├── pyproject.toml
├── requirements.txt
├── venv/                     ← Python 3.12 + deps installées (~2 Go)
├── src/bsbot/
│   ├── main.py
│   ├── buses.py              ← LatestSlot + ControlBus
│   ├── workers/              ← capture, vision, brain, control
│   ├── vision/               ← detect (PylaAI), state_finder, postprocess
│   ├── strategies/           ← base, menu, colt
│   ├── controls/             ← adb, inputs (Action dataclass)
│   ├── utils/                ← geometry, pathfinding (A*), logging
│   ├── models/               ← 4 modèles ONNX (45 Mo)
│   └── data/                 ← brawlers_info.json + state_templates/ (vide)
├── tests/                    ← 101 tests
├── tools/
│   ├── smoke_test.py         ← vérif ADB + screencap + ONNX
│   └── capture_template.py   ← capture templates pour state_finder
├── docs/superpowers/specs/   ← design doc
└── legacy/                   ← ancien code v1 (à supprimer plus tard)
```

## Validations effectuées sans device

- ✅ `pip install` complet (PyTorch, ONNX, scrcpy-client, adbutils, ultralytics…)
- ✅ Tous les imports OK
- ✅ 101 tests unitaires verts en 1.6s
- ✅ `python -m bsbot.main --dry-run` boote, charge les 3 ONNX en ~2s
  sur **CoreMLExecutionProvider** (Apple Silicon natif), accepte SIGINT,
  s'arrête proprement avec exit 0

## Ce que TU dois faire au réveil

### 1. Débloquer le téléphone (1 min)

Le téléphone Android (`22002522`) est branché en USB mais en état
`unauthorized`. Il y a une popup "Authorize USB debugging" sur son écran.

```bash
adb devices    # tu dois voir "22002522    device" (pas "unauthorized")
```

Si toujours unauthorized :
- Débranche/rebranche le câble
- Sur le téléphone, dans Developer Options : "Revoke USB debugging
  authorizations" puis rebranche → la popup réapparaît
- Coche "Always allow from this computer" avant de cliquer OK

### 2. Smoke test — bouton vert (2 min, ne touche pas au jeu)

```bash
cd ~/Desktop/Projets/BrawlStar-Bot
source venv/bin/activate
python tools/smoke_test.py
```

Doit afficher :

```
1/3  ADB connection            → OK
2/3  Screencap                 → OK (NN KB PNG received)
3/3  ONNX inference            → OK pour les 4 modèles, backend CoreMLExecutionProvider
```

Une capture `debug_screencap.png` est sauvée à la racine — tu peux
l'ouvrir pour vérifier que ton téléphone est bien capté.

### 3. Calibrer les templates de menus (15-30 min)

Le bot ne fait **rien** tant que le `state_finder` retourne `unknown`.
Pour qu'il reconnaisse l'état du jeu, il faut au moins un template par
état dans `src/bsbot/data/state_templates/<state>/`.

**Procédure recommandée** :
1. Ouvre Brawl Stars sur le téléphone
2. Va dans le lobby → identifie une zone unique et toujours visible
   (e.g. le bouton "Play")
3. Lance :
   ```bash
   python tools/capture_template.py --state lobby --name play_button
   ```
4. Ça sauvegarde un screenshot complet dans `lobby/play_button.png`.
   Ouvre-le, crop la zone unique avec Preview / Photoshop / GIMP, sauve
   au même endroit en écrasant.
5. Refais pour chaque état : `match` (joystick visible), `end` (écran de
   résultats), `popup` (croix de fermeture), `disconnect` (bouton
   reconnexion).

### 4. Calibrer les coordonnées des boutons (10 min)

Édite `src/bsbot/workers/control.py` → classe `ButtonLayout`. Les valeurs
actuelles (joystick à `(250, 1800)`, attack à `(1700, 1850)`, etc.) sont
des **placeholders** pour un téléphone 1080×2400 en paysage. À ajuster
en regardant un screenshot de Brawl Stars en partie.

Idem pour `src/bsbot/strategies/menu.py` → classe `MenuCoords` (bouton
Play en lobby, Continue en fin de match, etc.).

### 5. Premier vrai lancement (à tes risques 🎯)

Une fois templates + coords calibrés :

```bash
python -m bsbot.main
```

⚠️ **RISQUE DE BAN du compte Supercell**. Lance d'abord sur un compte
secondaire / nouveau. Surveille les premières minutes pour vérifier que :
- Le bot voit les bons états (regarde les logs : `state=lobby`, etc.)
- Le joystick bouge ton perso dans la bonne direction
- L'attaque vise les ennemis détectés

Ctrl-C pour arrêter, propre (les workers font un graceful shutdown).

## Limitations connues (sera vu après M5)

1. **Pas de lecture HP / Super charge** depuis l'écran. Donc la règle 1
   (fuir si HP bas) et la règle 4 (super si chargé) de la ColtStrategy
   ne se déclenchent pas — le code est branché, il manque juste les
   méthodes `read_hp_from_frame` et `read_super_from_frame`. Tu pourras
   les ajouter dans `vision/postprocess.py` après calibration.

2. **Pas de détection de la zone qui rétrécit** (Solo Showdown). La
   règle 2 est un stub. Idem : tu pourras ajouter un détecteur après
   avoir vu à quoi ressemble la zone en partie.

3. **Coordonnées par défaut** des boutons et menus = à calibrer.

4. **Pas de tests d'intégration end-to-end** — seuls les tests unitaires
   sont là. Le vrai test sera le premier match joué.

5. **scrcpy-client 0.4.7** installée alors que la version git tag v0.5.0
   était demandée — le tag a été buildé mais reste en version 0.4.7 dans
   son setup. Pas un souci pratique, mais à savoir si tu vois un warning.

## Si tu veux comprendre l'archi vite

Lis dans cet ordre (30 min) :
1. `docs/superpowers/specs/2026-05-25-bsbot-design.md` — vision globale
2. `src/bsbot/buses.py` — les primitives de communication
3. `src/bsbot/main.py` — comment tout est câblé ensemble
4. `src/bsbot/strategies/colt.py` — la "matière grise" du bot

## Si quelque chose plante

1. Lance `pytest` — si 101/101 ça veut dire que ton code de base marche
2. Lance `python -m bsbot.main --dry-run` — vérifie boot OK
3. Lance `python tools/smoke_test.py` — vérifie le device
4. Lis le log dans `~/.bsbot/logs/session-*.jsonl` — JSONL structuré,
   facile à grep

## Bonne journée 👋

— Claude (Opus 4.7)
