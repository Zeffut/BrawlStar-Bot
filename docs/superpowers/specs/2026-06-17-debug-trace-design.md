# Design — Système de trace de debug (`debug_trace`)

Date : 2026-06-17
Statut : approuvé (mode autonome — CLAUDE.md)

## 1. Objectif

Donner au bot un **système de log ultra-détaillé avec captures d'écran**, conçu pour
**débugger petit à petit au fil de l'évolution du bot**. À chaque point de décision
important, on enregistre :

- un **événement structuré** (JSON, machine-lisible, greppable), et
- une **capture d'écran du moment** (réutilisée depuis une frame déjà décodée, jamais
  un screencap frais), optionnellement avec le crop exact analysé.

Cas d'usage immédiat qui motive la fonctionnalité : le bug **« affiche Playing as Rico
mais joue en réalité avec béa »**. Il faut pouvoir voir, côte à côte, la capture du
lobby + le crop OCR + le texte brut lu par `read_current_brawler`, pour trancher : OCR
qui lit mal `bea`→`rico`, ou brawler réellement équipé = rico.

C'est aussi une **fondation réutilisable** : chaque futur bug s'instrumente en une ligne
`trace(...)`.

## 2. Contraintes dures (issues du contexte projet)

Ces contraintes sont non négociables — elles viennent de leçons coûteuses déjà payées
(voir mémoire `project-device-control-optimization`).

1. **Ne jamais réduire/toucher la résolution du stream `screen_capture` partagé.** Il
   alimente à la fois le live panel et la vision du bot (`state_finder` calibré 1280).
2. **Les captures debug RÉUTILISENT une frame déjà décodée en mémoire** (`get_frame()`
   du stream, ou une frame passée par l'appelant). **Jamais `_adb_screencap()`.** Un
   screencap frais entre en contention avec le `screenrecord` → flux gelé → cascade qui
   casse le grind. C'est **la** propriété de sécurité centrale du module.
3. **Hors du chemin chaud.** L'appelant ne fait qu'**enqueue** un record (référence
   frame incluse) ; un **thread writer** dédié encode le JPEG et écrit le JSONL. Queue
   bornée, **drop-oldest** si saturée (même pattern que la decode-queue de
   `screen_capture.py`). L'appelant ne bloque jamais sur du disque/encodage.
4. **Rétention bornée.** Cap disque total pour les captures (suppression des plus
   vieilles au-delà), rotation du JSONL par jour. `logs/` est déjà gitignored.
5. **Best-effort total.** Toute exception levée dans `trace()` ou le writer est avalée :
   le bot/grind n'est **jamais** impacté par le système de debug. Si le writer meurt,
   on perd des events, pas le bot.

## 3. Architecture

### 3.1 Module `debug_trace.py`

API unique, simple à appeler depuis n'importe où :

```python
trace(event: str,
      data: dict | None = None,
      frame=None,            # np.ndarray / PIL.Image déjà en mémoire (réutilisée)
      crop=None,             # sous-région analysée (np.ndarray), capturée à part
      capture: bool = True,  # joindre une capture ?
      level: str = "info",   # niveau du miroir humain (info/debug/warning)
      tag: str | None = None) -> None
```

Comportement :

1. Construit un record :
   `{"ts": <epoch_ms>, "iso": <ISO8601>, "event": event, "tag": tag,
     "account": <account_tag courant>, "data": data}`.
2. **Si `capture` et `frame is None`** : tente de récupérer la **dernière frame décodée
   du stream** via `screen_capture.get(serial).get_frame()` (cheap, en mémoire). Si
   indisponible → on enregistre l'event **sans** capture (jamais de fallback screencap).
3. **Enqueue** `(record, frame, crop)` dans une `queue.Queue(maxsize=32)`, drop-oldest si
   pleine.
4. **Miroir humain** : émet **une ligne** concise via `logging.getLogger("trace")` au
   `level` demandé → apparaît automatiquement dans `logs/bot.log` et donc dans le flux
   de logs du panel (worker_link tail bot.log).

**Thread writer** (démarré à la première utilisation, daemon) :

- Dépile les records ; append une ligne JSON dans `logs/trace/events-YYYYMMDD.jsonl`.
- Si capture : encode la frame en JPEG (qualité ~70) → `logs/trace/captures/<ts>_<event>.jpg`.
  Si crop fourni : `logs/trace/captures/<ts>_<event>.crop.jpg`. Le chemin relatif est
  ajouté au record JSONL (`"capture": "<ts>_<event>.jpg"`).
- Applique le **throttle de capture** : au plus 1 capture par `event` toutes les
  `TRACE_CAPTURE_MIN_INTERVAL_S` (défaut 3 s). Au-delà → event écrit sans capture
  (champ `"capture_throttled": true`). Les events de décision rares (reconcile,
  match start/end) peuvent forcer la capture via un flag `force_capture` interne.
- Applique la **rétention disque** : si la taille totale de `captures/` dépasse
  `TRACE_CAPTURE_MAX_MB` (défaut 400 MB), supprime les fichiers les plus anciens
  jusqu'à repasser sous le seuil.

### 3.2 Configuration

Via variables d'environnement (lues une fois, avec défauts) :

- `BOT_DEBUG_TRACE` — `off` | `on` (défaut) | `verbose`.
  - `off` : `trace()` est un no-op (zéro coût, pour désactiver totalement si besoin).
  - `on` (défaut) : events toujours écrits ; captures throttlées comme décrit.
  - `verbose` : throttle désactivé (toutes les captures écrites) — pour une session de
    debug ciblée.
- `TRACE_CAPTURE_MIN_INTERVAL_S` (défaut 3), `TRACE_CAPTURE_MAX_MB` (défaut 400).

Le défaut `on` est volontaire : events structurés peu coûteux toujours disponibles, et
captures bornées en débit et en disque → sûr pour tourner en continu sur le worker.

### 3.3 Points instrumentés (Phase 1)

| Point | Fichier | Contenu de l'event | Capture |
|---|---|---|---|
| Lecture brawler équipé | `game_api.read_current_brawler` | texte OCR brut (tout le dict), token retenu | frame lobby + **crop** |
| Réconciliation brawler | `stage_manager` (~156-182) | intended / equipped_ocr / canonical / corrigé? | frame lobby (force) |
| Démarrage match | `stage_manager` (PLAY tapé) | brawler enregistré, mode | frame (force) |
| Fin de match | flux résultat (`trophy_observer`/`stage_manager`) | brawler, delta, trophées après | frame résultat (force) |
| Recovery `goto_lobby` | `game_api._goto_lobby_impl` | type de recovery (restart BS / dialogue Quitter / bouton accueil / mauvaise classif state) | frame (force) |

Ces points couvrent le bug courant (rico/bea) **et** la douleur récurrente de navigation.

### 3.4 Visualisation

**Phase 1 (ce build)** — fichiers sur le worker, consultables en SSH :
- `logs/trace/events-YYYYMMDD.jsonl` : `grep`/`jq` direct.
- `logs/trace/captures/*.jpg` : récupérables via `scp`.
- Suffit pour trancher le bug immédiat et pose la fondation.

**Phase 2 (suite — même spec, déployée APRÈS vérification que Phase 1 n'impacte pas le
grind)** — page panel `/debug` :
- `worker_link` expose `GET /api/debug/events?limit=N` (tail du JSONL, JSON) et
  `GET /api/debug/capture/{name}` (sert un JPEG de `captures/`).
- Le cloud panel proxifie ces deux routes (réutilise exactement le pattern proxy
  snapshot/capture déjà en place) et ajoute une page listant les events récents
  (chronologique, filtrable par `event`/`tag`) avec vignettes cliquables.
- Déploiement panel = `docker cp` des statiques (rebuild image fragile — cf. mémoire).

Phase 2 est **explicitement séparée** pour livrer la valeur (le bug) sans risquer le
panel d'abord.

## 4. Gestion d'erreurs & robustesse

- `trace()` : `try/except` global → toute erreur loggée en `debug` et avalée.
- Writer thread : boucle protégée ; une erreur sur un record n'arrête pas la boucle.
- Frame manquante → event sans capture (jamais de screencap de secours).
- Aucune dépendance nouvelle (réutilise `cv2`/`PIL` + `numpy` déjà présents).

## 5. Tests

Unitaires (`tests/`) :
- `trace()` enqueue sans bloquer et ne lève jamais (même avec frame invalide).
- Le writer écrit un JSONL **valide** (1 objet JSON/ligne) avec les champs attendus.
- Le **throttle** limite les captures à ≤1 / event / intervalle ; `verbose` les laisse
  toutes passer.
- La **rétention** supprime bien les plus anciennes captures au-delà du cap MB.
- `frame=None` **sans** stream actif → event écrit **sans** capture et **sans** appel à
  `_adb_screencap` (vérifié par mock/spy).
- `BOT_DEBUG_TRACE=off` → `trace()` est un no-op (rien écrit).

Garde-fou d'intégration : un test/scan vérifie qu'aucun point instrumenté n'appelle
`_adb_screencap` dans son chemin de trace.

## 6. YAGNI / hors périmètre

- Pas de base de données pour les events (JSONL suffit).
- Pas d'upload streaming des captures vers le cloud (on-demand seulement, Phase 2).
- Pas de viewer riche en Phase 1.
- Pas de rotation par taille fine du JSONL (rotation quotidienne + cap captures suffit).

## 7. Découpage de livraison

1. **Phase 1** : `debug_trace.py` + instrumentation des 5 points + tests. Déployer sur
   le worker, vérifier que le grind tourne toujours (matchs loggés, stream OK, 0 erreur),
   puis trancher le bug rico/bea avec les premières captures.
2. **Phase 2** (après validation Phase 1) : routes `worker_link` + proxy + page panel
   `/debug`.
