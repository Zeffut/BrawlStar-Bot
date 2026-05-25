# BrawlStar-Bot v2

Bot Brawl Stars en Python qui joue **en autonomie sur un téléphone Android
branché en USB**, avec un Mac qui sert de cerveau (capture vidéo via `scrcpy`,
inférence ML via `onnxruntime` + CoreML Apple Silicon, contrôle via `adbutils`).

**Statut** : v0.1.0 — squelette complet, en attente de calibration sur device
réel. Voir [`docs/superpowers/specs/2026-05-25-bsbot-design.md`](docs/superpowers/specs/2026-05-25-bsbot-design.md)
pour le design détaillé.

## Architecture

Quatre threads communiquent via des bus thread-safe :

```
Phone Android ─USB─ Mac ─┬─ CaptureWorker (scrcpy → frames)
                         ├─ VisionWorker  (ONNX → GameState)
                         ├─ BrainWorker   (Strategy → Action)
                         └─ ControlWorker (Action → ADB inputs)
```

## Crédit

Ce projet réutilise (avec attribution) les modèles ONNX et la logique de
détection de [**PylaAI**](https://github.com/PylaAI/PylaAI) (branche
`compatibility`, port macOS par Maayan080). Licence "No Selling" — usage
personnel uniquement.

## Prérequis

- macOS Apple Silicon (testé sur Darwin 25.x, M-series)
- Python 3.12 (via Homebrew : `brew install python@3.12 python-tk@3.12`)
- ADB : `brew install android-platform-tools` (fournit `adb`)
- scrcpy : `brew install scrcpy`
- Téléphone Android branché en USB, avec **USB Debugging activé** dans
  *Developer Options*, et la popup "Authorize this computer" acceptée
- Brawl Stars installé sur le téléphone

## Installation

```bash
git clone … BrawlStar-Bot
cd BrawlStar-Bot
./install.sh
```

Le script crée `venv/`, installe les deps, gère le conflit
`scrcpy-client` ⇄ `adbutils` (cf. `install.sh`), et installe le package
en éditable.

## Smoke test (sans toucher au jeu)

```bash
source venv/bin/activate
python tools/smoke_test.py
```

Vérifie :
1. La connexion ADB au téléphone
2. Une capture d'écran via `adb exec-out screencap`
3. Le chargement et l'inférence des 4 modèles ONNX (sur CoreML si dispo)

Aucune action n'est envoyée au jeu.

## Calibration des templates (à faire une fois)

Le `StateFinder` détecte l'état du jeu (lobby / match / end / popup /
disconnect) par template matching. Au début, le dossier
`src/bsbot/data/state_templates/` est vide. Pour le peupler :

```bash
# Ouvre Brawl Stars sur le téléphone, va dans le lobby, puis :
python tools/capture_template.py --state lobby --name play_button \
    --crop 1500,1000,1900,1200

# Aller dans un match…
python tools/capture_template.py --state match --name joystick_ring \
    --crop 100,1600,400,1900

# Après un match, écran de résultat :
python tools/capture_template.py --state end --name continue_button \
    --crop 1500,1800,1900,1980
```

Les coordonnées de crop dépendent de la résolution de ton téléphone — utilise
un screenshot complet (`--crop` omis) pour identifier les zones, puis crop
proprement avec un éditeur d'image.

Une fois qu'au moins un template par état est en place, le bot peut tourner.

## Lancer le bot

```bash
source venv/bin/activate
python -m bsbot.main                # config par défaut = ./config.toml
python -m bsbot.main --dry-run      # sans phone (smoke check uniquement)
```

Ctrl-C pour arrêter (graceful : drain des queues, ferme scrcpy proprement).

## Configuration

Éditer `config.toml`. Sections :

- `[adb]` — `device_serial` vide = premier device détecté
- `[game]` — `mode` et `brawler` (v1 : `solo_showdown` + `colt`)
- `[session]` — `max_duration_minutes` (0 = illimité)
- `[performance]` — `target_ips`, taille/bitrate scrcpy, device d'inférence
- `[debug]` — niveau log, capture d'écran sur erreur

## Tests

```bash
source venv/bin/activate
pytest                # 84+ tests unitaires
```

Tous les tests utilisent des mocks — pas besoin de phone réel.

## Limitations connues (v1)

- **Templates manquants** → le bot reste en état `unknown` au démarrage tant
  que la calibration n'est pas faite.
- **HP / Super charge non lus** depuis l'écran → les règles 1 (fuite si HP
  bas) et 4 (super si chargé) de la ColtStrategy sont neutralisées (HP=None,
  super=None signifie "skip").
- **Pas de pathfinding tile-based** — déplacements en vecteurs directs (OK
  sur map ouverte comme Solo Showdown la plupart du temps).
- **Détection de la zone qui rétrécit** non implémentée.
- **Coordonnées des boutons (joystick, attack, super)** sont des valeurs par
  défaut pour 1080×2400 portrait. À calibrer pour ton phone (M3 du design).

## Roadmap

Voir le design doc, section 11. Jalons :
- M1 — Capture & inputs ✅
- M2 — Vision pipeline ✅
- M3 — Orchestration & menus ⚠️ (code OK, calibration à faire)
- M4 — Combat Colt basique ✅ (code OK, à tester sur device)
- M5 — Combat Colt complet (super, cubes, pathfinding tile)
- M6 — Stabilisation 10h+

## Licence

Code original sous MIT (à confirmer si tu veux publier). Modèles ONNX et
`detect.py` adaptés de PylaAI sous "No Selling" — usage personnel uniquement,
pas de redistribution commerciale.

## Avertissement

L'utilisation de bots sur Brawl Stars **peut entraîner un ban** de ton compte
Supercell. Utilise à tes risques et périls, idéalement sur un compte
secondaire dédié.
