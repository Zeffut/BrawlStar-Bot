# Spec — Moteur d'actions « shop/upgrade » (déblocage hypercharges, montée de power)

Date : 2026-06-14
Statut : approuvé (mode autonome) — prêt pour writing-plans
Auteur : Claude (pour Zeffut)

## 1. Objectif

Donner au bot la capacité d'effectuer des **actions d'amélioration sur les fiches
brawler** :

1. **Débloquer des hypercharges** sur les brawlers maxés (P11) qui n'en ont pas
   encore, tant que c'est abordable. C'est l'action à plus forte valeur : elle
   convertit l'or dormant en « charge » qui augmente le tier de revente.
2. **Monter le power level** d'un brawler (bouton AMÉLIORER) jusqu'à un niveau
   cible, tant que c'est abordable (powerpoints + coins).

Le moteur est conçu pour être **étendu** (star power, gadget, gear, offres shop)
sans réécriture, mais la v1 n'implémente que les deux actions ci-dessus (YAGNI).

## 2. Contraintes & contexte technique (vérifiés dans le code)

- **Contrôle device** : adb shell `input tap/swipe`. La navigation fiche existe
  déjà et est **autonome / basée sur un `serial`** dans `revente/read_hypercharges.py`
  (`_screencap(serial)`, `_tap(serial,w,h,xr,yr)`, `_enter_detail`,
  `_swipe_carousel_next`, geom `_geom_for(w,h)`). Le nouveau module **réutilise
  ce socle** et reste lui aussi standalone/serial (pas de dépendance au singleton
  `game_api`, pas de hop HTTP local).
- **Identification brawler** : par **hash perceptuel** du portrait, **pas par OCR
  de nom** (l'OCR de nom est explicitement non fiable). → l'action HC s'applique
  « à tout brawler éligible » via parcours du carrousel, ce qui contourne le
  problème d'identification.
- **Détection d'état fiche** (déjà fiable, testée sur fixtures réelles) :
  - `_detail_is_maxed(pil,w,h)` → P11 (label jaune « NIVEAU MAX ! »).
  - `_detail_has_hypercharge(pil,w,h)` → flamme magenta dans le slot HC
    (région `detail_hc = (0.90,0.99,0.18,0.40)`), **valable seulement si maxé**.
- **OCR** : `utils.extract_text_and_positions(img)` (EasyOCR) → `{texte:{center,bbox}}`.
- **Devises** : `revente/read_currencies.read_lobby_numbers(serial)` lit
  gold/coins, gems, bling depuis la barre du lobby. Sur la **fiche**, les coins
  s'affichent aussi en haut-droite (OCR possible pour vérifier après dépense).
- **Coût HC** : `sale_report.HC_COST = 5000` coins par hypercharge (constante du
  projet, réutilisée et rendue configurable).
- **Commandes worker↔cloud** : table `COMMANDS: dict[str,Callable[[dict],dict]]`
  dans `worker_link.py`. Endpoint cloud → `_cmd_for_account(id,name,args)` →
  WS `{type:cmd,id,name,args}` → `COMMANDS[name](args)` → `{type:cmd_result,...}`.
- **Device actuel** : Mi9T potentiellement **offline** (cf. mémoire fleet). Le
  design n'exige donc PAS d'exécution live pour être livrable/testé.

### Géométrie observée sur fixtures réelles (Mi9T 2340×1080, ~19.5:9)

- **Bouton AMÉLIORER** (power upgrade) : bouton **vert**, bas-droite, ~`x∈[0.78,0.97] y∈[0.84,0.96]`,
  texte « AMÉLIORER »/« AMELIORER » + deux coûts (powerpoints, coins). Présent
  **uniquement si power < 11** (absent sur Shelly/Maisie maxés).
- **Slot hypercharge** (à débloquer) : dans le cluster d'emplacements haut-droite,
  même région que `detail_hc ≈ (0.90,0.99,0.18,0.40)` ; centre ~`(0.945, 0.29)`.
  Sur un maxé sans HC, ce slot est « débloqué/achetable » (tooltip « Emplacement
  débloqué » dans la fixture Shelly). ⚠️ **Coordonnée de tap exacte + géométrie du
  popup de confirmation = à calibrer en live** (voir §7). Le moteur est donc
  piloté par des constantes centralisées + dry-run de prévisualisation.

## 3. Architecture & composants

Nouveau module : **`revente/shop_actions.py`** (standalone, serial-based, adb-only).

```
revente/shop_actions.py
├── Détection (pures, testées sur fixtures)
│   ├── detect_power_upgrade(pil, w, h) -> UpgradeButton | None
│   │     # centre (xr,yr) du bouton AMÉLIORER + powerpoint_cost + coin_cost (OCR)
│   ├── hc_buy_eligible(pil, w, h) -> bool         # maxé ET pas de HC
│   └── (réutilise read_hypercharges._detail_is_maxed/_detail_has_hypercharge)
│
├── Planner (pur, testé)
│   ├── plan_hypercharges(states, coins, *, hc_cost, max_count) -> [Action]
│   └── plan_power_upgrade(power_now, target, costs, coins, powerpoints) -> [Action]
│
├── ShopActionEngine(serial, *, dry_run=True, hc_cost=5000)
│   ├── buy_hypercharges(max_count=None, coin_floor=0, confirm=False) -> Report
│   ├── upgrade_power(target_level=11, scope="current"|"walk", confirm=False) -> Report
│   └── (réutilise navigation read_hypercharges : enter_detail, carousel, screencap, tap)
│
└── CLI: python -m revente.shop_actions --plan | --buy-hc [--confirm] | --upgrade ...
```

Promotion d'helpers **publics** (alias minces, non cassants) dans
`revente/read_hypercharges.py` pour réutilisation propre :
`screencap`, `geom_for`, `enter_detail`, `swipe_carousel_next`, `portrait_hash`,
et `detail_status(serial,samples)` (= `check_current_detail`, déjà public).

### Modèle de données

```python
@dataclass
class UpgradeButton:
    xr: float; yr: float            # centre, ratios [0..1]
    powerpoint_cost: int | None
    coin_cost: int | None

@dataclass
class Action:                       # élément de plan
    kind: str                       # "buy_hypercharge" | "upgrade_power"
    coin_cost: int
    powerpoint_cost: int = 0
    note: str = ""                  # ex. "brawler #3 (maxed, no HC)"

@dataclass
class ActionResult:
    action: Action
    executed: bool                  # False en dry-run
    verified: bool                  # re-lecture écran confirme l'effet
    error: str | None = None

@dataclass
class Report:
    dry_run: bool
    coins_before: int | None
    coins_after: int | None
    planned: list[Action]
    results: list[ActionResult]
    summary: str
```

## 4. Flux d'exécution

### buy_hypercharges
1. (live) Vérifier BS au lobby ; lire coins de départ (`read_lobby_numbers`).
2. `enter_detail(serial)` → on est sur une fiche (début carrousel).
3. Boucle carrousel (cap `MAX_CAROUSEL`), dédup par `portrait_hash` (arrêt après
   3 hash répétés = wrap-around) :
   - Lire la fiche (vote multi-frame existant). Si `hc_buy_eligible` :
     - Construire `Action(buy_hypercharge, coin_cost=hc_cost)`.
     - Si `coins_restants - hc_cost < coin_floor` **ou** plafond `max_count`
       atteint → ignorer (et logguer).
     - **dry-run** : enregistrer l'action + le tap prévu, ne PAS taper.
     - **live (confirm)** : taper le slot HC → confirmer le popup → attendre →
       re-lire la fiche : `verified = _detail_has_hypercharge && maxed`.
       Décrémenter `coins_restants -= hc_cost`.
   - `swipe_carousel_next`.
4. (live) Re-lire coins finaux ; remplir `Report`.

### upgrade_power
- `scope="current"` : sur la fiche déjà ouverte (déterministe, testable).
- `scope="walk"` : parcours carrousel, applique la cible à chaque brawler.
- Boucle : tant que `power < target` ET `detect_power_upgrade` renvoie un bouton
  abordable (`coin_cost ≤ coins` et `powerpoint_cost ≤ powerpoints`) :
  - dry-run : enregistrer l'action ; live : taper le bouton → confirmer →
    re-lire le power (OCR badge) → `verified = power_après == power_avant+1` →
    décrémenter devises. Garde anti-boucle (`power_avant == power_après` → stop).

## 5. Surface de commande

`worker_link.py` (handlers `def _cmd_*(args:dict)->dict`, enregistrés dans `COMMANDS`) :

| Commande | args | Effet |
|---|---|---|
| `shop_plan` | `{tag}` | Dry-run read-only : renvoie le plan HC + devises (aucun tap). **Toujours sûr.** |
| `shop_buy_hypercharges` | `{tag, max_count?, coin_floor?, confirm}` | Live si `confirm===true`, sinon dry-run. |
| `shop_upgrade_power` | `{tag, target_level?, scope?, confirm}` | Idem. |

- **Résolution serial** : `device.adb_serial()`.
- **Garde d'exclusivité** : avant tout live, vérifier l'état de session
  (via le chemin `session_state` existant) ; si une session de grind tourne →
  refuser avec un message clair (« stoppe la session d'abord »). Le dry-run n'a
  pas besoin de cette garde (read-only) mais l'applique quand même pour cohérence
  de l'image lue.

`cloud_panel/app.py` (calqué sur `/api/accounts/{id}/start`) :
- `POST /api/accounts/{id}/shop/plan` → `_cmd_for_account(id,"shop_plan",{})`
- `POST /api/accounts/{id}/shop/buy_hypercharges` (body: `max_count?,coin_floor?,confirm`)
  → `_cmd_for_account(..., timeout_s=600)` (parcours carrousel long).
- `POST /api/accounts/{id}/shop/upgrade_power` (body: `target_level?,scope?,confirm`).

## 6. Sécurité & irréversibilité

- **Dépense d'or en jeu = irréversible.** Donc :
  - `dry_run=True` par défaut **partout** ; le live exige `confirm===true` explicite.
  - Garde d'exclusivité de session (pas deux pilotes sur le device).
  - **Vérification post-action** systématique (re-lecture écran) ; une action non
    vérifiée est marquée `verified=False` (n'arrête pas le batch mais est rapportée).
- Claude **ne déclenche pas** d'exécution live lui-même (compte en vente + or non
  remboursable) : livraison testée niveau logique/détection/dry-run ; l'activation
  live est une commande côté utilisateur.

## 7. Stratégie de test (« 100 % fonctionnel »)

Réalité : Mi9T potentiellement offline → le tap live ne peut pas être exécuté par
Claude. Ce qui **est garanti testé et vert sans device** :

1. **Détection (fixtures réelles `tests/fixtures/hc/`)** — `tests/test_shop_actions.py` :
   - `detect_power_upgrade` : renvoie un bouton + coûts (≈20/20) sur `bull_detail_p1.png` ;
     renvoie `None` sur `shelly_detail.png` (maxé) et `maisie_detail.png` (maxé+HC).
   - `hc_buy_eligible` : `True` sur Shelly (maxé, pas de HC) ; `False` sur Maisie
     (HC possédée) et Bull (pas maxé).
2. **Planner (pur)** : coûts, abordabilité, plafond `max_count`, `coin_floor`,
   éligibilité, cas limites (0 coins, aucun éligible, coins non multiples de 5000,
   power déjà à la cible).
3. **Moteur dry-run (mocké)** : `_screencap` renvoie une séquence de fixtures,
   `subprocess.run` mocké → **assert aucun tap émis en dry-run**, plan correct ;
   en mode live mocké → assert taps émis aux coordonnées attendues + boucle de
   vérification appelée.

Chemin live : implémenté + auto-vérifiant, exécuté uniquement sur `confirm` réel
contre le device live (par l'utilisateur).

**Validation live optionnelle, read-only** : lancer `shop_plan` (dry-run) contre
le device réel via le HP (SSH `-p 2222`, adb serial `192.168.60.18:5555`) **si
joignable** — valide la détection sur l'écran courant **sans dépenser**. À faire
en phase de vérification, jamais de dépense sans confirmation.

## 8. Hors périmètre (v1)

- Achat de star powers / gadgets / gears / offres du shop (seams laissés).
- Ciblage d'un brawler **par nom** pour l'upgrade (OCR de nom non fiable) :
  `upgrade_power` cible la fiche courante ou parcourt — pas « monte Colt à P11 ».
- Calibration géométrie 16:9 émulateur (seul le ~19.5:9 phone est calibré).
- Intégration auto dans la boucle de grind (ex. « dépenser l'or en fin de
  session ») — possible plus tard sur ce moteur, mais non implémentée.

## 9. Découvertes test LIVE (2026-06-14, device QPRCQ9RV2 via HP) — RÉVISION NAV

Le test sur le vrai device a invalidé l'hypothèse de navigation par **carrousel**
(réutilisée de `read_hypercharges`). Constats vérifiés :

- **Détection OK sur la bonne carte** : `_find_green_button_center` localise les
  boutons ; green(UPGRADE_REGION)=0 ⇒ maxed ; magenta(detail_hc) ⇒ HC possédée.
  FRANK P11 possédant sa HC → `hc_buy_eligible`=False = correct. La lecture des
  devises marche (or lu = 11527, exact). **Le seul bug est la NAVIGATION.**
- **Navigation correcte (UI actuelle)** : Lobby → taper le **brawler central**
  (~0.52,0.44) ouvre la **GRILLE** « BRAWLERS (n/103) » (3 colonnes, niveau power
  par tuile) → taper une **tuile** ouvre la **carte POUVOIR**. Le bouton gauche
  « BRAWLERS » (0.06,0.435) ouvre les **TROPHÉES** (mauvais écran = bug d'origine).
  **La carte n'a PAS de carrousel** → nav par grille (tuile→carte→retour→scroll).
- **Flux d'achat** : carte → slot abilities (haut-droite) → **panneau à ONGLETS**
  (GADGET / POUVOIR STAR / HYPERCHARGE) → bouton vert prix → dialogue
  « CONFIRMER L'ACHAT » → confirm. ⚠️ **Le panneau peut s'ouvrir sur POUVOIR STAR,
  pas HYPERCHARGE.** Erreur commise en calibration : achat du pouvoir star de FRANK
  (2000 coins) au lieu d'une HC. **Garde obligatoire avant tout achat** : vérifier
  que l'onglet actif est bien HYPERCHARGE (OCR du titre/onglet). Coût réel d'une HC
  sur cette version : non confirmé (jamais atteint le vrai onglet).

### Révisions à faire dans le code (TODO)
1. Remplacer la nav carrousel par une **nav grille** : `_open_grid` (lobby→tap
   centre, vérif header OCR), itération tuiles (tap→carte→détection→retour) + scroll.
2. **Robustesse** : gérer verrou (PIN 2603), invites d'équipe (taper REFUSER),
   BACK ignoré. Mieux : **intégrer `game_api.goto_lobby`/dismiss** (le bot a déjà
   cette logique) plutôt que de la dupliquer en adb standalone.
3. **Garde HYPERCHARGE-tab** avant tout `_spend_tap` d'achat HC (anti star-power).
4. Live buying = passe **supervisée** d'abord (mapper l'onglet HC + coût), jamais
   en autonomie aveugle sur le compte en vente.

