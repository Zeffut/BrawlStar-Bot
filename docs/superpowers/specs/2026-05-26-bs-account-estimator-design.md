# BS Account Estimator — Design

**Date** : 2026-05-26
**Auteur** : Claude (sales)
**Statut** : validé (brainstorm)

## 1. Objectif

CLI Python qui prend un tag de compte Brawl Stars (`#XXXXXX`) et retourne une estimation de prix de revente, en comparant le compte aux listings actifs sur les marketplaces tiers.

Usage cible : décider du prix de mise en vente d'un compte farmé par le bot, ou estimer la valeur d'un compte externe avant achat/échange.

## 2. Périmètre

**Inclus** :
- Récupération des données compte via API officielle Supercell.
- Scraping live des marketplaces Eldorado, PlayerAuctions, iGV.
- Matching pondéré compte ↔ listings, rendu rapport markdown.

**Exclus (non-buts)** :
- Vente/listing automatisé sur les marketplaces.
- Estimation de skins (l'API officielle ne les expose pas).
- Estimation de l'ancienneté du compte (idem).
- Interface web (le projet a déjà un panel, mais hors scope ici).
- Toute intégration avec le bot de farming (`play.py`, `device.py`, etc.). Module isolé.

## 3. Architecture

Nouveau module `sales/` à la racine du projet, sans dépendance vers le code du bot.

```
sales/
├── __init__.py
├── fetcher.py              # Wrapper API officielle Supercell
├── scrapers/
│   ├── __init__.py
│   ├── base.py             # Interface Scraper
│   ├── eldorado.py
│   ├── playerauctions.py
│   └── igv.py
├── normalizer.py           # Extrait features depuis listings bruts
├── valuator.py             # Matching + estimation
├── report.py               # Rendu rapport markdown
├── cli.py                  # Entrypoint `python -m sales <tag>`
├── cache/                  # Cache disque scrapes (gitignored)
└── tests/
    ├── fixtures/           # JSON API + HTML marketplaces
    ├── test_fetcher.py
    ├── test_scrapers.py
    ├── test_normalizer.py
    ├── test_valuator.py
    └── test_cli.py
```

## 4. Flow de données

```
[tag] ──> fetcher (API) ──> AccountProfile
                                    │
[scrapers] ──> RawListing[] ──> normalizer ──> ListingProfile[]
                                                       │
                            valuator(AccountProfile, ListingProfile[])
                                                       │
                                                PriceEstimate
                                                       │
                                                  report ──> stdout
```

## 5. Composants

### 5.1 `fetcher.py` — API Supercell

- Endpoint : `GET https://api.brawlstars.com/v1/players/%23<TAG>`
- Auth : header `Authorization: Bearer <BRAWL_API_KEY>` (variable d'env, clé créée sur developer.brawlstars.com avec whitelist IP)
- Erreurs gérées : 401 (clé invalide), 403 (IP non whitelistée), 404 (tag inconnu), 429 (rate limit), 5xx (retry × 3 backoff)
- Sortie : `AccountProfile`

```python
@dataclass
class BrawlerInfo:
    id: int
    name: str
    power: int                # 1-11
    rank: int                 # rang trophées
    trophies: int
    highest_trophies: int
    gadgets: int              # nb gadgets débloqués
    star_powers: int
    gears: int
    has_hypercharge: bool     # déduit de la présence dans accessories

@dataclass
class AccountProfile:
    tag: str
    name: str
    total_trophies: int
    highest_trophies: int
    exp_level: int
    brawlers: list[BrawlerInfo]

    # Champs dérivés (calculés à la construction)
    brawler_count: int
    hypercharge_count: int
    legendary_count: int        # Leon, Spike, Sandy, Crow, Amber, Meg, Chester, Kenji, etc. (liste statique)
    maxed_count: int            # power 11
```

Liste des légendaires en constante module-level, à maintenir manuellement (rare mise à jour ~2/an).

### 5.2 `scrapers/` — Marketplaces

Interface commune :

```python
class Scraper(ABC):
    name: str
    base_url: str

    @abstractmethod
    def search(self, filters: SearchFilters) -> list[RawListing]: ...
```

`SearchFilters` minimal : `min_trophies`, `max_trophies` (fourchette ±30% autour du compte cible pour limiter le scrape).

`RawListing` : `{source, url, title, description, price_usd, raw_html_snippet}`.

**Implémentation** :
- `httpx` (sync, plus simple) avec timeout 10s
- `selectolax` pour parser HTML (rapide, pas besoin de JS pour les listings publics)
- User-agent réaliste navigateur
- Throttle 1 req/s par scraper (semaphore)
- Retry × 2 sur 5xx avec backoff 2s, 4s
- Cache disque par URL (TTL 1h) dans `sales/cache/` — clé = `sha256(url)`, valeur = `{fetched_at, html}`

**Robustesse** :
- Si scraper renvoie 403/429 ou parse rate <50%, le scraper est marqué "dégradé" et signalé dans le rapport, mais l'estimation continue avec les autres sources.
- Pas de proxy/rotation IP dans la v1 (à ajouter seulement si on se fait bloquer en pratique).

### 5.3 `normalizer.py` — Feature extraction

Transforme chaque `RawListing` en `ListingProfile` :

```python
@dataclass
class ListingProfile:
    source: str
    url: str
    price_usd: float
    trophies: int | None
    brawler_count: int | None
    legendary_count: int | None
    hypercharge_count: int | None
    has_og_skins: bool        # Star Shelly / Virus 8-Bit / Challenger Colt mentionnés
    raw_title: str
    extraction_confidence: float  # 0.0-1.0, % de champs extraits avec succès
```

Heuristiques regex sur titre + description :
- Trophées : `(\d[\d,.\s]*)\s*(k|K)?\s*troph` + variantes EN/FR
- Brawlers : `(\d+)\s*brawl(ers)?\s*(unlock|debl)`
- Hypercharges : `(\d+)\s*hyper`
- Légendaires : `(\d+)\s*legend(ary|aire)`
- OG skins : recherche substring case-insensitive de la liste connue

Si un champ ne peut pas être extrait, il reste `None` (et n'entre pas dans le scoring distance).

### 5.4 `valuator.py` — Matching + estimation

```python
@dataclass
class PriceEstimate:
    low: float          # P25
    median: float       # P50
    high: float         # P75
    sample_size: int
    sources_breakdown: dict[str, int]   # {"eldorado": 8, ...}
    top_comparables: list[ListingProfile]  # top 5 closest
    warnings: list[str]
```

Distance pondérée entre un `AccountProfile` et un `ListingProfile`, normalisée [0,1] par feature, agrégée pondérée :
- trophies : 0.30
- brawler_count : 0.25
- hypercharge_count : 0.25
- legendary_count : 0.15
- has_og_skins : 0.05

Si un champ du listing est `None`, son poids est redistribué proportionnellement sur les champs présents (les listings très incomplets seront naturellement moins représentatifs mais pas exclus).

Pipeline :
1. Filtre listings avec `extraction_confidence >= 0.5`
2. Calcule distance vers chaque listing
3. Garde les 20 plus proches
4. Si <5 retenus → warning "données insuffisantes, estimation indicative"
5. Calcule percentiles P25/P50/P75 sur les prix
6. Top 5 comparables (les 5 distances les plus faibles) pour audit manuel

### 5.5 `report.py` — Rendu markdown stdout

Format cible :

```
🎯 Compte #ABC123 (NomJoueur) — Estimation

📊 Profil
   Trophées        : 28,450
   Brawlers        : 67
   Hypercharges    : 12
   Légendaires     : 4
   Power 11 maxés  : 18

💰 Estimation prix : $35 – $65 (médiane $48)
   Basé sur 18 listings comparables
   Eldorado: 8 | PlayerAuctions: 7 | iGV: 3

🏷️  Top 5 comparables
   1. $42 — 27k 🏆, 65 brawlers, 11 hyper  →  eldorado.gg/listing/xxx
   2. $51 — 29k 🏆, 68 brawlers, 13 hyper  →  playerauctions.com/yyy
   ...

⚠️  Notes
   - Skins non pris en compte (API ne les expose pas)
   - Date création compte inconnue
```

### 5.6 `cli.py` — Entrypoint

```bash
python -m sales <tag> [--no-cache] [--json] [--scrapers eldorado,playerauctions]
```

- Tag accepté avec ou sans `#`.
- `--no-cache` force re-scrape.
- `--json` sortie machine-readable (pour intégration future).
- `--scrapers` restreint la liste (debug).
- Exit codes : 0 succès, 1 erreur fatale (tag invalide, API down), 2 succès dégradé (warnings présents).

## 6. Configuration

Fichier `sales/.env.example` :
```
BRAWL_API_KEY=
SALES_CACHE_DIR=sales/cache
SALES_HTTP_TIMEOUT=10
```

`.gitignore` : `sales/cache/`, `sales/.env`.

## 7. Dépendances nouvelles

À ajouter à `requirements.txt` (section commentée "# sales tool") :
- `httpx>=0.27`
- `selectolax>=0.3`
- `python-dotenv>=1.0`

Pas de nouvelles deps lourdes ; toutes pures Python.

## 8. Tests

- **`test_fetcher.py`** : mock httpx, vérifie parsing JSON Supercell, erreurs 401/404/429.
- **`test_scrapers.py`** : fixtures HTML capturées, vérifie extraction listings + prix.
- **`test_normalizer.py`** : table de cas titre→features attendues (FR + EN).
- **`test_valuator.py`** : compte synthétique + listings synthétiques, vérifie distance, percentiles, redistribution poids quand champs `None`.
- **`test_cli.py`** : invocation bout-en-bout avec tout mocké, vérifie exit code + format report.

Cible : >85% coverage sur `sales/`, tous tests offline (pas d'appel réseau réel).

## 9. Risques & mitigations

| Risque | Mitigation |
|---|---|
| Marketplaces changent leur HTML → scrapers cassent | Fixtures versionnées + test régression. Si parse_rate <50% pendant >24h, alerte manuelle. |
| Rate limit API Supercell (~10/s) | Pas un souci pour usage CLI ponctuel. Cache résultat 5min si même tag re-requêté. |
| Blocage IP par marketplace | Throttle 1 req/s + UA réaliste. Si bloqué, v1 abandonne ce scraper. Proxy à voir en v2. |
| Liste légendaires obsolète | Constante avec commentaire "dernière MAJ : YYYY-MM-DD". Revue trimestrielle. |
| ToS Supercell : usage commercial de l'API | API officielle utilisée en lecture seule pour analyse, pas pour le bot. Risque légal limité mais à connaître. |

## 10. Évolutions futures (hors v1)

- v2 : ajout scrapers supplémentaires (Skycoach, GameBoost, U7BUY).
- v2 : détection automatique OG skins via OCR sur screenshots du compte (récup via le bot existant).
- v2 : intégration endpoint dans `cloud_panel/` pour estimation depuis UI web.
- v2 : tracking historique des prix pour suivre les tendances marché.
