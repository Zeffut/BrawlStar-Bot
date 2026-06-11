# Sale-ready — arrêt cible + rapport de revente

Date : 2026-06-12
Statut : approuvé (mode autonome)
Lié : [[project-revente-strategie]], [[project-optimisation-rentabilite]], [[project-schedule-powersave]]

## Objectif

Quand un compte grindé atteint sa tranche de trophées cible, le bot doit :
1. **s'arrêter automatiquement** de grinder (le compte EST le stock, sur-grinder = risque ban pour ~1 $/jour de valeur) ;
2. **lire les données du compte** et **analyser ce qu'il faut améliorer** avant de vendre ;
3. **envoyer un rapport Telegram** listant les actions à faire (achats hypercharges, etc.) et l'estimation de prix ;
4. Zeffut exécute les achats à la main et met en vente lui-même (pas d'automatisation de la vente).

## Périmètre

**Dans le périmètre :**
- Config `sale_target_trophies` dans la config schedule globale (DB → fan-out → worker).
- Nouvel état `play_schedule` : `sale_ready` (ferme le jeu, pas d'auto-resume).
- Module `sale_report.py` : collecte données + analyse + estimation + checklist + format Telegram.
- Lecture or/gemmes **best-effort** par OCR (easyocr déjà présent sur le worker, GPU).
- Envoi one-shot idempotent.
- Champ « Cible de revente » dans le formulaire Planning du panel.
- Tests unitaires (analyse + format purs, transition d'état).

**Hors périmètre (volontaire) :**
- Inventaire automatique des **skins** et **hypercharges** : aucune API ne les expose, la nav collection est trop fragile → le rapport DEMANDE à Zeffut de confirmer ce qu'il possède.
- Mise en vente automatique (Eldorado) : manuelle, à la demande de Zeffut.
- Multi-comptes / UI flotte : un seul compte aujourd'hui ; le provider est par-compte mais aucune UI de flotte n'est construite.

## Architecture

### Flux

```
grind (state=play)
  └─ après chaque match : db enregistre account_trophies_after
        └─ play_schedule.state() lit le trophy_provider
              └─ si total >= sale_target (>0)  ─►  state = "sale_ready"
                    ├─ _manage_schedule_powersave ferme le jeu (déjà : tout != "play")
                    └─ orchestrateur (telegram_main) détecte la transition play→sale_ready
                          └─ si pas déjà notifié pour cette cible :
                                ├─ sale_report.build_and_send(tag, target)
                                └─ db.set_config("sale_report:<tag>", target)
```

### Composants

**1. `play_schedule.py` — état `sale_ready`**
- Module-level `_TROPHY_PROVIDER` + `set_trophy_provider(fn)` (miroir exact de `_MATCH_COUNT_PROVIDER` / `set_match_count_provider`).
- Parse `sale_target_trophies` (défaut 0) depuis la config layered.
- `state()` : avant de renvoyer `play`, si `sale_target > 0` et `trophy_provider() >= sale_target`, renvoie `sale_ready`. `sale_ready` a priorité sur `play` mais PAS sur `sleep`/`dayoff` (inutile de notifier en pleine nuit ; on notifiera au prochain créneau de jeu — acceptable). Ordre de priorité : `dayoff > sleep > pause > cap > sale_ready > break > play`.
- Exposé : `PlaySchedule.sale_target` (int) pour l'orchestrateur.
- `should_play_now()` renvoie `(False, "sale_ready")` dans cet état → le runner s'arrête proprement comme pour `cap`.

**2. `db.py`**
- `latest_account_trophies(account_id) -> int | None` : dernier `account_trophies_after` non-NULL pour ce compte. Source du trophy_provider.
- (réutilise `get_config`/`set_config` existants pour le flag d'idempotence.)

**3. `sale_report.py` (nouveau)**
- `gather(tag) -> dict` : appelle `account_detect.fetch_account_profile(tag)` (brawlers+power+trophies) ; calcule total, nb brawlers, nb P11, brawlers sous plafond 750 (headroom restant), liste P11. Tente `read_currencies` lobby (or/gemmes) en best-effort (try/except → None).
- `estimate_price(data) -> (low, high)` : table inline dérivée de `revente/grille_prix.md` (base trophées ~0,7 $/1000, +5-8 $/hypercharge connue, +P11). Renvoie une fourchette $.
- `build_actions(data) -> list[str]` : checklist priorisée — hypercharges finançables (`or//5000` si or lu, sinon « vérifie ton or, 5000/HC ») sur les P11 listés ; « ne dépense pas les gemmes » ; « liste à $X-Y sur Eldorado » ; « confirme tes skins rares (Star Shelly, etc.) — non lisibles par le bot ».
- `format_telegram(data, actions, price) -> str` : message HTML lisible.
- `build_and_send(tag, target) -> None` : orchestration ; envoie via le canal Telegram du worker (réutilise le `TelegramBot.send` / `alerts`), best-effort, loggé.
- Fonctions pures (`estimate_price`, `build_actions`, `format_telegram`) testables sans réseau.

**4. `telegram_main.py` — câblage**
- Près du `set_match_count_provider` existant : `play_schedule.set_trophy_provider(lambda: db.latest_account_trophies(_aid))`.
- Dans la boucle qui appelle `_manage_schedule_powersave` : détecter la transition vers `sale_ready` ; si `db.get_config("sale_report:<tag>") != target`, appeler `sale_report.build_and_send(tag, target)` puis `set_config`. One-shot.

**5. Panel — champ config**
- `cloud_panel/schedule_config.py` : ajouter `sale_target_trophies: 0` aux DEFAULTS + sérialisation `[schedule]`.
- `cloud_panel/static/index.html` + `app.js` : un input numérique « Cible de revente (trophées, 0 = off) » dans le formulaire Planning ; inclus dans le PUT.

## Données & estimation

Estimation prix (table inline, alignée `revente/grille_prix.md` 2026-06-12) :
- Base : `trophées/1000 * 0.7 $`.
- + `nb_P11 * 1.5 $` (proxy de « compte travaillé »).
- + `hypercharges_connues * 6 $` (si l'utilisateur confirme ; par défaut 0 → fourchette basse).
- Fourchette = base..(base + bonus), bornée par paliers de la grille (15k/20k/25k/30k).
Le rapport affiche clairement « estimation plancher (sans skins/HC confirmés) — le vrai prix dépend de ce que tu valides ».

## Gestion d'erreurs

- OCR or/gemmes échoue / valeur implausible (>1e6 ou négative) → champ `None`, le rapport dit « à vérifier à la main ». Ne bloque jamais l'envoi.
- `fetch_account_profile` échoue (flaresolverr down) → retry court ; si échec total, envoyer un rapport minimal « cible atteinte (~N tr depuis la DB), profil détaillé indisponible » + ne PAS poser le flag idempotent (réessaiera).
- Envoi Telegram échoue → loggé, flag NON posé (réessaiera au prochain passage).

## Tests

- `tests/test_sale_report.py` : `estimate_price` (bornes paliers), `build_actions` (or lu vs non lu, P11 présents/absents), `format_telegram` (placeholders remplis, pas de None brut).
- `tests/test_play_schedule.py` : ajout cas `sale_ready` (provider < cible → play ; >= cible → sale_ready ; cible 0 → jamais sale_ready ; sleep prioritaire sur sale_ready).
- `db.latest_account_trophies` : test sur DB éphémère.

## Décisions tranchées (mode autonome)

1. **Cible dans la config schedule** (pas un fichier séparé) — réutilise fan-out + panel, sémantiquement c'est une porte de planning.
2. **Skins/HC hors scope auto** — non exposés par API, nav fragile ; le rapport délègue la confirmation à Zeffut.
3. **Or/gemmes best-effort** — enrichit sans jamais bloquer ; la valeur load-bearing (trophées/P11/headroom) vient de l'API fiable.
4. **`sale_ready` < `sleep` en priorité** — pas de notif en pleine nuit, on notifie au prochain créneau diurne.
5. **One-shot idempotent par (tag,cible)** — re-notifie seulement si la cible change.
