# Planning de jeu ultra-configurable + hot-reload — Design

**Date** : 2026-06-08
**Module** : `play_schedule.py`, `cfg/general_config.toml`, intégration `telegram_main.py`
**Objectif** : rendre le système de pauses / heures de jeu entièrement paramétrable et **modifiable à chaud** (sans redémarrer le worker), pour pouvoir peaufiner le comportement humain au fil du temps.

## Contexte

`play_schedule.py` (module existant) empêche le grind 24/7 : fenêtre de sommeil nocturne, blocs de jeu randomisés alternés avec des pauses, plafond de matchs/jour, le tout jitteré. Aujourd'hui il est chargé **une seule fois** (singleton `_SCHEDULE` figé) → tout changement de config exige un redémarrage du worker. La config vit dans `cfg/general_config.toml` `[schedule]`.

Limites actuelles : un seul créneau de pause (le sommeil), pas d'overrides week-end/par-jour, pas de jour de repos, pas de hot-reload.

## Principes directeurs

1. **Rétro-compatibilité totale.** Toutes les clés existantes gardent leur nom et leur sémantique. `enabled = false` restaure le 24/7. Une config qui ne contient que les anciennes clés produit un comportement **identique** à aujourd'hui.
2. **Peaufiner sans redémarrer.** Hot-reload sur changement de mtime, et couche d'override gitignorée éditable directement sur le HP.
3. **Sûreté forward-only.** Le quota du jour et les fenêtres jitterées sont tirés d'un RNG seedé sur la date → stables au sein d'un jour et au redémarrage. Un reload ne re-tire JAMAIS le quota ni ne réinitialise les compteurs du jour (sinon on pourrait dépasser le quota).
4. **Unités isolées et testables.** Loader pur, helper de fenêtre réutilisable, `PlaySchedule` avec état explicite.

## Config en couches

Résolution, la dernière source gagne clé par clé :

1. `_DEFAULTS` (dans le code).
2. `cfg/general_config.toml` table `[schedule]` (commitée — la base documentée).
3. `cfg/schedule.local.toml` (gitignorée, optionnelle) — réglages live sur le HP, **jamais écrasés par un `git pull`**. Même table `[schedule]` (ou clés à plat).

La couche locale permet de tester un réglage directement sur la machine sans passer par git ; la couche commitée reste la source de vérité partagée.

## Hot-reload

- `PlaySchedule` retient le mtime des deux fichiers de config.
- À chaque appel de `state()` (throttlé : au plus une vérif mtime toutes les ~10 s), si un mtime a changé → recharge la config en couches et **reconstruit les paramètres** (`enabled`, fenêtres, blocs, pauses, plafonds, overrides…) **en place**.
- **Préservé à travers un reload** : `_day`, `_matches_today`, `_blocks_today`, le quota tiré du jour, les fenêtres jitterées du jour, `_break_until`. On ne touche qu'aux *tunables*, pas à l'*état courant*. Le prochain `_ensure_day` (changement de date) re-résoudra les params du jour avec la nouvelle config.
- Tout échec de reload → on garde la config en cours + log debug (jamais planter le grind).

## Réglages de configuration

### Existants (conservés à l'identique)
`enabled`, `sleep_start_hour`, `sleep_end_hour`, `sleep_jitter_minutes`, `block_min_minutes`, `block_max_minutes`, `break_min_minutes`, `break_max_minutes`, `daily_match_cap`, `daily_cap_jitter`.

### Nouveaux
- `timezone` (str, défaut `"Europe/Paris"`) — **documentaire** : les heures sont en heure locale du worker (tz OS). Au chargement, si `time.tzname`/offset ne correspond pas, log un WARNING (ne change pas le comportement, signale juste une incohérence).
- `max_blocks_per_day` (int, défaut `0` = illimité) + `blocks_jitter` (int, défaut `0`) — plafond du nombre de blocs de jeu par jour. Quand atteint → état `cap` (même sémantique que le quota de matchs : journée terminée).
- `dayoff_weekdays` (list[str], défaut `[]`) — jours fixes de repos total, ex. `["sunday"]`. Noms anglais lowercase.
- `dayoff_chance` (float 0..1, défaut `0.0`) — probabilité qu'un jour donné soit un jour de repos, tirée du RNG seedé sur la date (déterministe : un même jour est toujours repos ou non). Cumulatif avec `dayoff_weekdays`.
- **`[[schedule.pause_windows]]`** — liste de fenêtres de pause diurnes, chacune : `start = "HH:MM"`, `end = "HH:MM"`, `jitter_minutes` (int, défaut `0`), `label` (str, défaut `"pause"`). Pendant une fenêtre → état `pause` (jeu fermé, comme le sommeil). Gère le passage minuit (start > end). Le sommeil nocturne reste une clé first-class distincte (pas obligé de le mettre en pause_window).

### Overrides week-end / par jour
- `[schedule.weekend]` — table optionnelle ; ses clés surchargent la base les **samedi et dimanche**.
- `[schedule.days.<jour>]` (`monday`…`sunday`) — table optionnelle ; surcharge la base pour ce jour précis.
- Résolution d'un jour J : `base` ← (`weekend` si J ∈ {sam,dim} et présent) ← (`days.<J>` si présent). Les `pause_windows` peuvent aussi être surchargées par jour (remplacement complet de la liste, pas de fusion).
- Résolu dans `_ensure_day` → `self._today_*` reflètent les params effectifs du jour.

## Machine à états

`state()` → `"play" | "break" | "sleep" | "pause" | "cap" | "dayoff"`.

Ordre d'évaluation (le premier qui matche gagne) : `dayoff` → `sleep` → `pause_window` → `cap` (matchs OU blocs) → `break` → `play`.

- **`play`** : grind autorisé.
- **`break`** : pause courte intra-session (20–70 min) sur l'horloge monotone.
- **`sleep`** / **`pause`** / **`cap`** / **`dayoff`** : longues pauses → jeu fermé.

Pour le worker, la seule distinction qui compte est `play` vs non-`play` (pause + fermeture du jeu). `should_play_now()` renvoie `(can_play, raison)` avec une raison FR lisible pour le panel.

## Intégration worker (`telegram_main.py`)

- `_manage_schedule_powersave` : remplacer la liste explicite `if st in ("sleep","cap","break")` par **`if st != "play"`** → tout nouvel état de pause ferme le jeu automatiquement. Table de labels FR avec fallback sur le nom d'état brut.
- Compteur de blocs : incrémenter `_blocks_today` dans `block_minutes()` (appelé à chaque début de bloc).
- Aucun changement au câblage de `record_match()`, `should_play_now()`, `start_break()`, `state()` (mêmes signatures).

## Découpage en unités

- **`_load_layered_cfg()`** — lit les 3 couches, parse `pause_windows` et les tables d'override, renvoie un dict résolu + les mtimes. Pur (hors I/O fichier).
- **`_Window`** (start_min, end_min, jitter, label) + helpers `parse_hhmm`, `roll_jittered(rng)`, `contains(minute_of_day)` (avec wrap minuit). Réutilisé pour le sommeil ET les pause_windows.
- **`_resolve_day_params(base, overrides, weekday)`** — applique weekend/par-jour. Pur.
- **`PlaySchedule`** — tunables + état du jour ; `state/should_play_now/block_minutes/start_break/record_match` ; internes `_ensure_day`, `_maybe_reload`.

## Tests (purs, sans deps lourdes)

`tests/test_play_schedule.py` (étendu) + nouveaux cas :
- parsing `HH:MM`, membership fenêtre, wrap minuit (sommeil 23:00→07:00) ;
- déterminisme du jitter/quota par jour (même date → mêmes valeurs ; restart-stable) ;
- résolution des overrides week-end et par-jour ;
- jour de repos : `dayoff_weekdays` explicite + `dayoff_chance` seedé (déterministe) ;
- plafonds match ET bloc → `cap` ;
- pause_windows → `pause` ; sommeil → `sleep` ; priorité dayoff > sleep > pause > cap > break > play ;
- hot-reload : changement de mtime → nouveaux tunables, état du jour préservé (quota non re-tiré) ;
- rétro-compat : config = anciennes clés seules → comportement identique ;
- `enabled=false` → toujours `play`.

## Fichiers de config

- `cfg/general_config.toml` : section `[schedule]` ré-documentée à fond (chaque clé commentée), avec des exemples commentés de `[[schedule.pause_windows]]`, `[schedule.weekend]`, `[schedule.days.*]`.
- `cfg/schedule.local.toml.example` : gabarit commenté de la couche locale.
- `.gitignore` : ajouter `cfg/schedule.local.toml`.

## Hors périmètre (suite possible)

- UI panel pour éditer le planning (formulaire web → écrit la couche locale via une commande worker). Le hot-reload + la couche locale posent déjà les fondations ; l'UI est un chantier séparé.
- Micro-pauses intra-match, budget en minutes de jeu, délai de démarrage : écartés (risque en match / redondance / YAGNI).

## Déploiement

Commit + push → le worker pull au prochain match-end (git_update différé) → hot-reload applique sans restart de process. Vérification sur le HP : log `play schedule loaded/reloaded`, états cohérents.
