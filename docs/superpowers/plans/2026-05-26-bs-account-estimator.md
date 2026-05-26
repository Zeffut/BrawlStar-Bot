# BS Account Estimator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLI Python qui estime le prix de revente d'un compte Brawl Stars via API officielle + scraping live des marketplaces.

**Architecture:** Module `sales/` isolé, sans coupling avec le bot. Pipeline `fetcher (API Supercell) → scrapers (3 marketplaces) → normalizer → valuator (matching pondéré) → report markdown`. Cache disque TTL 1h pour les scrapes.

**Tech Stack:** Python 3.12, httpx, selectolax, python-dotenv, pytest, pytest-httpx.

**Spec:** `docs/superpowers/specs/2026-05-26-bs-account-estimator-design.md`

---

## Task 1: Scaffold module + dépendances

**Files:**
- Create: `sales/__init__.py`
- Create: `sales/.env.example`
- Create: `sales/cache/.gitkeep`
- Create: `sales/tests/__init__.py`
- Create: `sales/tests/fixtures/__init__.py`
- Modify: `requirements.txt` (append section)
- Modify: `.gitignore` (append entries)

- [ ] **Step 1: Créer la structure de dossiers**

```bash
mkdir -p sales/scrapers sales/tests/fixtures sales/cache
touch sales/__init__.py sales/scrapers/__init__.py sales/tests/__init__.py sales/tests/fixtures/__init__.py sales/cache/.gitkeep
```

- [ ] **Step 2: Créer `sales/.env.example`**

```
BRAWL_API_KEY=
SALES_CACHE_DIR=sales/cache
SALES_HTTP_TIMEOUT=10
```

- [ ] **Step 3: Ajouter dépendances dans `requirements.txt`**

Ajouter à la fin du fichier :

```
# sales tool (account estimator)
httpx>=0.27
selectolax>=0.3
python-dotenv>=1.0
pytest-httpx>=0.30
```

- [ ] **Step 4: Mettre à jour `.gitignore`**

Ajouter à la fin de `.gitignore` :

```
# sales tool
sales/cache/*
!sales/cache/.gitkeep
sales/.env
```

- [ ] **Step 5: Installer dépendances**

Run: `pip install httpx selectolax python-dotenv pytest-httpx`
Expected: install OK, no conflicts

- [ ] **Step 6: Commit**

```bash
git add sales/ requirements.txt .gitignore
git commit -m "sales: scaffold module + deps"
```

---

## Task 2: Modèles de données

**Files:**
- Create: `sales/models.py`
- Create: `sales/tests/test_models.py`

- [ ] **Step 1: Écrire les tests d'abord**

Créer `sales/tests/test_models.py` :

```python
from sales.models import AccountProfile, BrawlerInfo, ListingProfile, PriceEstimate


def _make_brawler(name="Shelly", power=11, has_hc=False, trophies=500):
    return BrawlerInfo(
        id=16000000,
        name=name,
        power=power,
        rank=20,
        trophies=trophies,
        highest_trophies=trophies,
        gadgets=2,
        star_powers=2,
        gears=3,
        has_hypercharge=has_hc,
    )


def test_account_profile_derives_counts():
    brawlers = [
        _make_brawler("Shelly", power=11, has_hc=True),
        _make_brawler("Leon", power=11, has_hc=False),
        _make_brawler("Colt", power=9, has_hc=False),
    ]
    acc = AccountProfile.from_brawlers(
        tag="#ABC", name="Bob", total_trophies=15000,
        highest_trophies=18000, exp_level=120, brawlers=brawlers,
    )
    assert acc.brawler_count == 3
    assert acc.hypercharge_count == 1
    assert acc.maxed_count == 2
    assert acc.legendary_count == 1  # Leon


def test_listing_profile_extraction_confidence():
    lp = ListingProfile(
        source="eldorado", url="http://x", price_usd=50.0,
        trophies=20000, brawler_count=50, legendary_count=None,
        hypercharge_count=None, has_og_skins=False, raw_title="x",
    )
    # 3 sur 5 champs comparables présents
    assert 0.55 < lp.extraction_confidence < 0.65


def test_price_estimate_dataclass():
    pe = PriceEstimate(low=10, median=20, high=30, sample_size=15,
                       sources_breakdown={"eldorado": 15},
                       top_comparables=[], warnings=[])
    assert pe.median == 20
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `pytest sales/tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sales.models'`

- [ ] **Step 3: Implémenter `sales/models.py`**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import ClassVar

LEGENDARY_BRAWLERS: frozenset[str] = frozenset({
    "Leon", "Spike", "Sandy", "Crow", "Amber", "Meg",
    "Chester", "Kenji", "Cordelius", "Mico", "Charlie",
    "Berry", "Larry & Lawrie", "Melodie", "Angelo", "Draco",
    "Kaze", "Juju", "Sirius",
})


@dataclass(frozen=True)
class BrawlerInfo:
    id: int
    name: str
    power: int
    rank: int
    trophies: int
    highest_trophies: int
    gadgets: int
    star_powers: int
    gears: int
    has_hypercharge: bool


@dataclass(frozen=True)
class AccountProfile:
    tag: str
    name: str
    total_trophies: int
    highest_trophies: int
    exp_level: int
    brawlers: tuple[BrawlerInfo, ...]
    brawler_count: int
    hypercharge_count: int
    legendary_count: int
    maxed_count: int

    @classmethod
    def from_brawlers(cls, tag: str, name: str, total_trophies: int,
                      highest_trophies: int, exp_level: int,
                      brawlers: list[BrawlerInfo]) -> "AccountProfile":
        bts = tuple(brawlers)
        return cls(
            tag=tag, name=name, total_trophies=total_trophies,
            highest_trophies=highest_trophies, exp_level=exp_level,
            brawlers=bts,
            brawler_count=len(bts),
            hypercharge_count=sum(1 for b in bts if b.has_hypercharge),
            legendary_count=sum(1 for b in bts if b.name in LEGENDARY_BRAWLERS),
            maxed_count=sum(1 for b in bts if b.power >= 11),
        )


@dataclass
class ListingProfile:
    source: str
    url: str
    price_usd: float
    trophies: int | None
    brawler_count: int | None
    legendary_count: int | None
    hypercharge_count: int | None
    has_og_skins: bool
    raw_title: str

    _COMPARABLE_FIELDS: ClassVar[tuple[str, ...]] = (
        "trophies", "brawler_count", "legendary_count",
        "hypercharge_count", "has_og_skins",
    )

    @property
    def extraction_confidence(self) -> float:
        present = sum(1 for f in self._COMPARABLE_FIELDS if getattr(self, f) is not None)
        return present / len(self._COMPARABLE_FIELDS)


@dataclass
class PriceEstimate:
    low: float
    median: float
    high: float
    sample_size: int
    sources_breakdown: dict[str, int]
    top_comparables: list[ListingProfile]
    warnings: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Relancer les tests**

Run: `pytest sales/tests/test_models.py -v`
Expected: tous PASS

- [ ] **Step 5: Commit**

```bash
git add sales/models.py sales/tests/test_models.py
git commit -m "sales: data models (AccountProfile, ListingProfile, PriceEstimate)"
```

---

## Task 3: Fetcher API Supercell

**Files:**
- Create: `sales/fetcher.py`
- Create: `sales/tests/fixtures/api_player_ok.json`
- Create: `sales/tests/test_fetcher.py`

- [ ] **Step 1: Créer la fixture JSON API**

Créer `sales/tests/fixtures/api_player_ok.json` :

```json
{
  "tag": "#ABC123",
  "name": "TestPlayer",
  "trophies": 28450,
  "highestTrophies": 30100,
  "expLevel": 145,
  "brawlers": [
    {
      "id": 16000000,
      "name": "SHELLY",
      "power": 11,
      "rank": 25,
      "trophies": 800,
      "highestTrophies": 1000,
      "gadgets": [{"id": 1}, {"id": 2}],
      "starPowers": [{"id": 1}, {"id": 2}],
      "gears": [{"id": 1}, {"id": 2}, {"id": 3}],
      "accessories": [{"id": 1}, {"id": 2}, {"id": 62000000, "name": "HYPERCHARGE"}]
    },
    {
      "id": 16000020,
      "name": "LEON",
      "power": 9,
      "rank": 18,
      "trophies": 600,
      "highestTrophies": 700,
      "gadgets": [{"id": 1}],
      "starPowers": [],
      "gears": [],
      "accessories": []
    }
  ]
}
```

- [ ] **Step 2: Écrire les tests**

Créer `sales/tests/test_fetcher.py` :

```python
import json
from pathlib import Path
import pytest
from sales.fetcher import fetch_account, FetcherError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "api_player_ok.json").read_text())


def test_fetch_account_ok(httpx_mock):
    httpx_mock.add_response(
        url="https://api.brawlstars.com/v1/players/%23ABC123",
        json=FIXTURE,
    )
    acc = fetch_account("ABC123", api_key="fake-key")
    assert acc.tag == "#ABC123"
    assert acc.name == "TestPlayer"
    assert acc.total_trophies == 28450
    assert acc.brawler_count == 2
    assert acc.hypercharge_count == 1  # Shelly
    assert acc.maxed_count == 1        # Shelly power 11
    assert acc.legendary_count == 1    # Leon
    # Brawler name normalisé en Title Case
    assert any(b.name == "Shelly" for b in acc.brawlers)


def test_fetch_account_strips_hash_prefix(httpx_mock):
    httpx_mock.add_response(
        url="https://api.brawlstars.com/v1/players/%23ABC123",
        json=FIXTURE,
    )
    acc = fetch_account("#ABC123", api_key="fake-key")
    assert acc.tag == "#ABC123"


def test_fetch_account_404(httpx_mock):
    httpx_mock.add_response(
        url="https://api.brawlstars.com/v1/players/%23BADTAG",
        status_code=404,
        json={"reason": "notFound", "message": "Player not found"},
    )
    with pytest.raises(FetcherError, match="not found"):
        fetch_account("BADTAG", api_key="fake-key")


def test_fetch_account_401(httpx_mock):
    httpx_mock.add_response(
        url="https://api.brawlstars.com/v1/players/%23ABC",
        status_code=401,
        json={"reason": "accessDenied.invalidIp"},
    )
    with pytest.raises(FetcherError, match="auth"):
        fetch_account("ABC", api_key="bad")


def test_fetch_account_missing_key():
    with pytest.raises(FetcherError, match="BRAWL_API_KEY"):
        fetch_account("ABC", api_key=None)
```

- [ ] **Step 3: Lancer les tests pour vérifier qu'ils échouent**

Run: `pytest sales/tests/test_fetcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sales.fetcher'`

- [ ] **Step 4: Implémenter `sales/fetcher.py`**

```python
from __future__ import annotations
import httpx
from sales.models import AccountProfile, BrawlerInfo

API_BASE = "https://api.brawlstars.com/v1"
HYPERCHARGE_ID_MIN = 62000000  # Range officiel des accessoires hypercharge


class FetcherError(Exception):
    pass


def _normalize_tag(tag: str) -> str:
    t = tag.strip().upper()
    if not t.startswith("#"):
        t = "#" + t
    return t


def _parse_brawler(data: dict) -> BrawlerInfo:
    has_hc = any(
        acc.get("id", 0) >= HYPERCHARGE_ID_MIN
        for acc in data.get("accessories", [])
    )
    return BrawlerInfo(
        id=data["id"],
        name=data["name"].title(),
        power=data["power"],
        rank=data["rank"],
        trophies=data["trophies"],
        highest_trophies=data["highestTrophies"],
        gadgets=len(data.get("gadgets", [])),
        star_powers=len(data.get("starPowers", [])),
        gears=len(data.get("gears", [])),
        has_hypercharge=has_hc,
    )


def fetch_account(tag: str, api_key: str | None, timeout: float = 10.0) -> AccountProfile:
    if not api_key:
        raise FetcherError("BRAWL_API_KEY manquante (variable d'env)")

    norm_tag = _normalize_tag(tag)
    url = f"{API_BASE}/players/{norm_tag.replace('#', '%23')}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise FetcherError(f"erreur réseau: {e}") from e

    if resp.status_code == 404:
        raise FetcherError(f"compte {norm_tag} not found")
    if resp.status_code in (401, 403):
        raise FetcherError(f"auth refusée ({resp.status_code}) — clé API ou IP invalide")
    if resp.status_code == 429:
        raise FetcherError("rate limit API Supercell")
    if resp.status_code >= 500:
        raise FetcherError(f"erreur serveur Supercell {resp.status_code}")
    if resp.status_code != 200:
        raise FetcherError(f"réponse inattendue {resp.status_code}")

    data = resp.json()
    brawlers = [_parse_brawler(b) for b in data.get("brawlers", [])]
    return AccountProfile.from_brawlers(
        tag=data["tag"],
        name=data["name"],
        total_trophies=data["trophies"],
        highest_trophies=data["highestTrophies"],
        exp_level=data["expLevel"],
        brawlers=brawlers,
    )
```

- [ ] **Step 5: Relancer les tests**

Run: `pytest sales/tests/test_fetcher.py -v`
Expected: tous PASS

- [ ] **Step 6: Commit**

```bash
git add sales/fetcher.py sales/tests/test_fetcher.py sales/tests/fixtures/api_player_ok.json
git commit -m "sales: Supercell API fetcher"
```

---

## Task 4: Cache disque pour scrapers

**Files:**
- Create: `sales/cache_store.py`
- Create: `sales/tests/test_cache_store.py`

- [ ] **Step 1: Écrire les tests**

Créer `sales/tests/test_cache_store.py` :

```python
import time
from sales.cache_store import CacheStore


def test_cache_set_get(tmp_path):
    c = CacheStore(tmp_path, ttl_seconds=60)
    c.set("http://x", "<html>hi</html>")
    assert c.get("http://x") == "<html>hi</html>"


def test_cache_miss(tmp_path):
    c = CacheStore(tmp_path, ttl_seconds=60)
    assert c.get("http://nope") is None


def test_cache_expired(tmp_path):
    c = CacheStore(tmp_path, ttl_seconds=0)
    c.set("http://x", "data")
    time.sleep(0.01)
    assert c.get("http://x") is None


def test_cache_persists_across_instances(tmp_path):
    CacheStore(tmp_path, ttl_seconds=60).set("http://x", "data")
    assert CacheStore(tmp_path, ttl_seconds=60).get("http://x") == "data"
```

- [ ] **Step 2: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_cache_store.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implémenter `sales/cache_store.py`**

```python
from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path


class CacheStore:
    def __init__(self, cache_dir: Path | str, ttl_seconds: int = 3600):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds

    def _key_path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.dir / f"{h}.json"

    def get(self, key: str) -> str | None:
        path = self._key_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() - payload["fetched_at"] > self.ttl:
            return None
        return payload["data"]

    def set(self, key: str, data: str) -> None:
        path = self._key_path(key)
        path.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
```

- [ ] **Step 4: Relancer les tests**

Run: `pytest sales/tests/test_cache_store.py -v`
Expected: tous PASS

- [ ] **Step 5: Commit**

```bash
git add sales/cache_store.py sales/tests/test_cache_store.py
git commit -m "sales: disk cache store with TTL"
```

---

## Task 5: Base scraper + normalizer

**Files:**
- Create: `sales/scrapers/base.py`
- Create: `sales/normalizer.py`
- Create: `sales/tests/test_normalizer.py`

- [ ] **Step 1: Implémenter `sales/scrapers/base.py`**

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchFilters:
    min_trophies: int | None = None
    max_trophies: int | None = None


@dataclass(frozen=True)
class RawListing:
    source: str
    url: str
    title: str
    description: str
    price_usd: float


class Scraper(ABC):
    name: str
    base_url: str

    @abstractmethod
    def search(self, filters: SearchFilters) -> list[RawListing]: ...
```

- [ ] **Step 2: Écrire les tests du normalizer**

Créer `sales/tests/test_normalizer.py` :

```python
from sales.normalizer import normalize_listing
from sales.scrapers.base import RawListing


def _raw(title="", desc="", price=50.0, source="eldorado", url="http://x"):
    return RawListing(source=source, url=url, title=title,
                      description=desc, price_usd=price)


def test_normalize_trophies_simple():
    lp = normalize_listing(_raw(title="Account 28000 trophies, 60 brawlers"))
    assert lp.trophies == 28000
    assert lp.brawler_count == 60


def test_normalize_trophies_k_suffix():
    lp = normalize_listing(_raw(title="BS account 28K trophies"))
    assert lp.trophies == 28000


def test_normalize_french():
    lp = normalize_listing(_raw(title="Compte 25000 trophées, 55 brawlers débloqués"))
    assert lp.trophies == 25000
    assert lp.brawler_count == 55


def test_normalize_hypercharges_legendaries():
    lp = normalize_listing(_raw(title="30k trophies, 70 brawlers, 15 hypercharges, 5 legendary"))
    assert lp.hypercharge_count == 15
    assert lp.legendary_count == 5


def test_normalize_og_skins_detected():
    lp = normalize_listing(_raw(desc="Includes rare Star Shelly skin"))
    assert lp.has_og_skins is True


def test_normalize_missing_fields():
    lp = normalize_listing(_raw(title="Generic BS account for sale"))
    assert lp.trophies is None
    assert lp.brawler_count is None
    assert lp.has_og_skins is False


def test_normalize_preserves_source_url_price():
    lp = normalize_listing(_raw(title="10k trophies", price=30.0,
                                 source="igv", url="http://igv.com/x"))
    assert lp.source == "igv"
    assert lp.url == "http://igv.com/x"
    assert lp.price_usd == 30.0
```

- [ ] **Step 3: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_normalizer.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'sales.normalizer'`

- [ ] **Step 4: Implémenter `sales/normalizer.py`**

```python
from __future__ import annotations
import re
from sales.models import ListingProfile
from sales.scrapers.base import RawListing

_OG_SKINS = ("star shelly", "challenger colt", "virus 8-bit", "virus 8 bit")

_TROPHY_RE = re.compile(
    r"(\d[\d,.\s]*)\s*(k|K)?\s*(trophies|trophy|trophées|troph[ée]es?)",
    re.IGNORECASE,
)
_BRAWLER_RE = re.compile(
    r"(\d+)\s*brawl(?:ers|er)?(?:\s*(?:unlock|d[eé]bloqu[eé]))?",
    re.IGNORECASE,
)
_HYPER_RE = re.compile(r"(\d+)\s*hyper", re.IGNORECASE)
_LEGEND_RE = re.compile(r"(\d+)\s*legend(?:ary|aire)s?", re.IGNORECASE)


def _parse_int(s: str) -> int:
    return int(s.replace(",", "").replace(".", "").replace(" ", ""))


def _extract_trophies(text: str) -> int | None:
    m = _TROPHY_RE.search(text)
    if not m:
        return None
    raw, k = m.group(1), m.group(2)
    n = _parse_int(raw)
    if k:
        n *= 1000
    return n


def _extract_first_int(regex: re.Pattern, text: str) -> int | None:
    m = regex.search(text)
    return int(m.group(1)) if m else None


def normalize_listing(raw: RawListing) -> ListingProfile:
    blob = f"{raw.title}\n{raw.description}"
    blob_lower = blob.lower()
    return ListingProfile(
        source=raw.source,
        url=raw.url,
        price_usd=raw.price_usd,
        trophies=_extract_trophies(blob),
        brawler_count=_extract_first_int(_BRAWLER_RE, blob),
        legendary_count=_extract_first_int(_LEGEND_RE, blob),
        hypercharge_count=_extract_first_int(_HYPER_RE, blob),
        has_og_skins=any(s in blob_lower for s in _OG_SKINS),
        raw_title=raw.title,
    )
```

- [ ] **Step 5: Relancer les tests**

Run: `pytest sales/tests/test_normalizer.py -v`
Expected: tous PASS

- [ ] **Step 6: Commit**

```bash
git add sales/scrapers/base.py sales/normalizer.py sales/tests/test_normalizer.py
git commit -m "sales: scraper base interface + listing normalizer"
```

---

## Task 6: HTTP fetcher helper pour scrapers

**Files:**
- Create: `sales/scrapers/http.py`
- Create: `sales/tests/test_scraper_http.py`

- [ ] **Step 1: Écrire les tests**

Créer `sales/tests/test_scraper_http.py` :

```python
import pytest
from sales.scrapers.http import ScraperHttp, ScraperBlockedError


def test_http_fetch_uses_cache(tmp_path, httpx_mock):
    httpx_mock.add_response(url="http://x.com/p", text="<html>1</html>")
    h = ScraperHttp(cache_dir=tmp_path, ttl_seconds=60, user_agent="test")
    assert h.get("http://x.com/p") == "<html>1</html>"
    # 2e appel : cache hit, pas de nouvelle requête mock nécessaire
    assert h.get("http://x.com/p") == "<html>1</html>"


def test_http_blocked_raises(tmp_path, httpx_mock):
    httpx_mock.add_response(url="http://x.com/p", status_code=403)
    h = ScraperHttp(cache_dir=tmp_path, ttl_seconds=60, user_agent="test")
    with pytest.raises(ScraperBlockedError):
        h.get("http://x.com/p")


def test_http_429_raises(tmp_path, httpx_mock):
    httpx_mock.add_response(url="http://x.com/p", status_code=429)
    h = ScraperHttp(cache_dir=tmp_path, ttl_seconds=60, user_agent="test")
    with pytest.raises(ScraperBlockedError):
        h.get("http://x.com/p")


def test_http_bypass_cache(tmp_path, httpx_mock):
    httpx_mock.add_response(url="http://x.com/p", text="v1")
    httpx_mock.add_response(url="http://x.com/p", text="v2")
    h = ScraperHttp(cache_dir=tmp_path, ttl_seconds=60, user_agent="test")
    assert h.get("http://x.com/p") == "v1"
    assert h.get("http://x.com/p", use_cache=False) == "v2"
```

- [ ] **Step 2: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_scraper_http.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implémenter `sales/scrapers/http.py`**

```python
from __future__ import annotations
import time
import httpx
from pathlib import Path
from sales.cache_store import CacheStore


class ScraperBlockedError(Exception):
    pass


class ScraperHttp:
    def __init__(self, cache_dir: Path | str, ttl_seconds: int = 3600,
                 user_agent: str = "Mozilla/5.0", timeout: float = 10.0,
                 throttle_seconds: float = 1.0):
        self.cache = CacheStore(cache_dir, ttl_seconds)
        self.ua = user_agent
        self.timeout = timeout
        self.throttle = throttle_seconds
        self._last_request_at = 0.0

    def get(self, url: str, use_cache: bool = True) -> str:
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return cached

        elapsed = time.time() - self._last_request_at
        if elapsed < self.throttle:
            time.sleep(self.throttle - elapsed)

        resp = httpx.get(url, headers={"User-Agent": self.ua}, timeout=self.timeout,
                         follow_redirects=True)
        self._last_request_at = time.time()

        if resp.status_code in (403, 429):
            raise ScraperBlockedError(f"{url} blocked ({resp.status_code})")
        if resp.status_code >= 400:
            raise ScraperBlockedError(f"{url} HTTP {resp.status_code}")

        text = resp.text
        self.cache.set(url, text)
        return text
```

- [ ] **Step 4: Relancer les tests**

Run: `pytest sales/tests/test_scraper_http.py -v`
Expected: tous PASS

- [ ] **Step 5: Commit**

```bash
git add sales/scrapers/http.py sales/tests/test_scraper_http.py
git commit -m "sales: shared scraper HTTP helper (cache, throttle, block detection)"
```

---

## Task 7: Scraper Eldorado

**Files:**
- Create: `sales/scrapers/eldorado.py`
- Create: `sales/tests/fixtures/eldorado_search.html`
- Create: `sales/tests/test_scraper_eldorado.py`

- [ ] **Step 1: Capturer la fixture HTML**

⚠️ **Action manuelle requise** : récupérer le HTML d'une page de listings Brawl Stars d'Eldorado. Soit via `curl https://www.eldorado.gg/brawl-stars-accounts/a/56-1-0 -A "Mozilla/5.0" -o sales/tests/fixtures/eldorado_search.html`, soit en sauvant depuis le navigateur. Si Eldorado bloque les requêtes non-navigateur, sauver via "View Source" → fichier.

Vérifier que le fichier contient bien des prix (`$xx`) et des titres de listings.

- [ ] **Step 2: Inspecter la structure HTML capturée**

Run: `grep -oE '(class|data-[a-z]+)="[^"]+"' sales/tests/fixtures/eldorado_search.html | sort -u | head -40`

Identifier les sélecteurs CSS pour : conteneur listing, titre, prix, URL. Noter dans un commentaire au début du fichier de test.

- [ ] **Step 3: Écrire les tests**

Créer `sales/tests/test_scraper_eldorado.py`. Adapter les assertions selon le contenu réel de la fixture (nombre de listings, valeurs).

```python
from pathlib import Path
from sales.scrapers.eldorado import EldoradoScraper
from sales.scrapers.base import SearchFilters

FIXTURE = (Path(__file__).parent / "fixtures" / "eldorado_search.html").read_text()


class _FakeHttp:
    def __init__(self, text): self.text = text
    def get(self, url, use_cache=True): return self.text


def test_eldorado_parses_listings():
    s = EldoradoScraper(http=_FakeHttp(FIXTURE))
    results = s.search(SearchFilters())
    assert len(results) > 0
    first = results[0]
    assert first.source == "eldorado"
    assert first.url.startswith("https://www.eldorado.gg/")
    assert first.price_usd > 0
    assert first.title  # non vide
```

- [ ] **Step 4: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_scraper_eldorado.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 5: Implémenter `sales/scrapers/eldorado.py`**

⚠️ Adapter les sélecteurs CSS aux observations de l'étape 2. Voici la structure attendue ; ajuster les `css_first()` / `css()`.

```python
from __future__ import annotations
import re
from selectolax.parser import HTMLParser
from sales.scrapers.base import Scraper, SearchFilters, RawListing
from sales.scrapers.http import ScraperHttp


class EldoradoScraper(Scraper):
    name = "eldorado"
    base_url = "https://www.eldorado.gg"
    search_path = "/brawl-stars-accounts/a/56-1-0"

    def __init__(self, http: ScraperHttp | None = None):
        self.http = http or ScraperHttp(cache_dir="sales/cache",
                                        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                    "Chrome/126.0 Safari/537.36"))

    def search(self, filters: SearchFilters) -> list[RawListing]:
        html = self.http.get(self.base_url + self.search_path)
        return list(self._parse(html))

    def _parse(self, html: str):
        tree = HTMLParser(html)
        # ⚠️ Sélecteur à adapter selon la fixture réelle
        for card in tree.css("a[href*='/brawl-stars-accounts/']"):
            href = card.attributes.get("href", "")
            if not href or "/a/" in href:  # skip pagination
                continue
            url = href if href.startswith("http") else self.base_url + href
            title = card.text(strip=True)
            price = self._extract_price(card.text())
            if price is None:
                continue
            yield RawListing(
                source=self.name, url=url,
                title=title, description="", price_usd=price,
            )

    @staticmethod
    def _extract_price(text: str) -> float | None:
        m = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
        return float(m.group(1)) if m else None
```

- [ ] **Step 6: Itérer sur les sélecteurs jusqu'à ce que le test passe**

Run: `pytest sales/tests/test_scraper_eldorado.py -v`
Si FAIL → inspecter la fixture, ajuster les sélecteurs CSS, relancer. Itérer jusqu'à PASS.

- [ ] **Step 7: Commit**

```bash
git add sales/scrapers/eldorado.py sales/tests/test_scraper_eldorado.py sales/tests/fixtures/eldorado_search.html
git commit -m "sales: Eldorado scraper"
```

---

## Task 8: Scraper PlayerAuctions

**Files:**
- Create: `sales/scrapers/playerauctions.py`
- Create: `sales/tests/fixtures/playerauctions_search.html`
- Create: `sales/tests/test_scraper_playerauctions.py`

- [ ] **Step 1: Capturer la fixture HTML**

```bash
curl 'https://www.playerauctions.com/brawl-stars-account/' \
  -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36" \
  -o sales/tests/fixtures/playerauctions_search.html
```

Vérifier que la page contient des listings (`grep -c '\$' sales/tests/fixtures/playerauctions_search.html` doit être >0).

- [ ] **Step 2: Inspecter la structure HTML**

Run: `grep -oE '(class|data-[a-z]+)="[^"]+"' sales/tests/fixtures/playerauctions_search.html | sort -u | head -40`

Identifier sélecteurs pour conteneur listing, titre, prix, URL.

- [ ] **Step 3: Écrire les tests**

Créer `sales/tests/test_scraper_playerauctions.py` (même structure que Task 7, adapter source="playerauctions" et préfixe URL).

```python
from pathlib import Path
from sales.scrapers.playerauctions import PlayerAuctionsScraper
from sales.scrapers.base import SearchFilters

FIXTURE = (Path(__file__).parent / "fixtures" / "playerauctions_search.html").read_text()


class _FakeHttp:
    def __init__(self, text): self.text = text
    def get(self, url, use_cache=True): return self.text


def test_playerauctions_parses_listings():
    s = PlayerAuctionsScraper(http=_FakeHttp(FIXTURE))
    results = s.search(SearchFilters())
    assert len(results) > 0
    first = results[0]
    assert first.source == "playerauctions"
    assert first.url.startswith("https://www.playerauctions.com/")
    assert first.price_usd > 0
```

- [ ] **Step 4: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_scraper_playerauctions.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 5: Implémenter `sales/scrapers/playerauctions.py`**

⚠️ Adapter sélecteurs CSS à la fixture réelle.

```python
from __future__ import annotations
import re
from selectolax.parser import HTMLParser
from sales.scrapers.base import Scraper, SearchFilters, RawListing
from sales.scrapers.http import ScraperHttp


class PlayerAuctionsScraper(Scraper):
    name = "playerauctions"
    base_url = "https://www.playerauctions.com"
    search_path = "/brawl-stars-account/"

    def __init__(self, http: ScraperHttp | None = None):
        self.http = http or ScraperHttp(cache_dir="sales/cache",
                                        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"))

    def search(self, filters: SearchFilters) -> list[RawListing]:
        html = self.http.get(self.base_url + self.search_path)
        return list(self._parse(html))

    def _parse(self, html: str):
        tree = HTMLParser(html)
        # ⚠️ Sélecteur à adapter
        for card in tree.css("[data-offer], .offer-item, a[href*='/offer/']"):
            url_el = card.css_first("a[href*='/offer/']") or card
            href = url_el.attributes.get("href", "")
            if not href:
                continue
            url = href if href.startswith("http") else self.base_url + href
            title_el = card.css_first("h3, .offer-title, [class*='title']")
            title = title_el.text(strip=True) if title_el else card.text(strip=True)[:200]
            price = self._extract_price(card.text())
            if price is None:
                continue
            yield RawListing(
                source=self.name, url=url,
                title=title, description="", price_usd=price,
            )

    @staticmethod
    def _extract_price(text: str) -> float | None:
        m = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
        return float(m.group(1)) if m else None
```

- [ ] **Step 6: Itérer sélecteurs jusqu'à passing**

Run: `pytest sales/tests/test_scraper_playerauctions.py -v`
Itérer jusqu'à PASS.

- [ ] **Step 7: Commit**

```bash
git add sales/scrapers/playerauctions.py sales/tests/test_scraper_playerauctions.py sales/tests/fixtures/playerauctions_search.html
git commit -m "sales: PlayerAuctions scraper"
```

---

## Task 9: Scraper iGV

**Files:**
- Create: `sales/scrapers/igv.py`
- Create: `sales/tests/fixtures/igv_search.html`
- Create: `sales/tests/test_scraper_igv.py`

- [ ] **Step 1: Capturer la fixture**

```bash
curl 'https://www.igv.com/brawl-stars/accounts' \
  -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36" \
  -o sales/tests/fixtures/igv_search.html
```

- [ ] **Step 2: Inspecter structure**

Run: `grep -oE '(class|data-[a-z]+)="[^"]+"' sales/tests/fixtures/igv_search.html | sort -u | head -40`

- [ ] **Step 3: Écrire les tests**

Créer `sales/tests/test_scraper_igv.py` (calque sur Task 7/8, adapter `source="igv"` et préfixe URL `https://www.igv.com/`).

```python
from pathlib import Path
from sales.scrapers.igv import IgvScraper
from sales.scrapers.base import SearchFilters

FIXTURE = (Path(__file__).parent / "fixtures" / "igv_search.html").read_text()


class _FakeHttp:
    def __init__(self, text): self.text = text
    def get(self, url, use_cache=True): return self.text


def test_igv_parses_listings():
    s = IgvScraper(http=_FakeHttp(FIXTURE))
    results = s.search(SearchFilters())
    assert len(results) > 0
    first = results[0]
    assert first.source == "igv"
    assert first.url.startswith("https://www.igv.com/")
    assert first.price_usd > 0
```

- [ ] **Step 4: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_scraper_igv.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 5: Implémenter `sales/scrapers/igv.py`**

⚠️ Adapter sélecteurs à la fixture.

```python
from __future__ import annotations
import re
from selectolax.parser import HTMLParser
from sales.scrapers.base import Scraper, SearchFilters, RawListing
from sales.scrapers.http import ScraperHttp


class IgvScraper(Scraper):
    name = "igv"
    base_url = "https://www.igv.com"
    search_path = "/brawl-stars/accounts"

    def __init__(self, http: ScraperHttp | None = None):
        self.http = http or ScraperHttp(cache_dir="sales/cache",
                                        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"))

    def search(self, filters: SearchFilters) -> list[RawListing]:
        html = self.http.get(self.base_url + self.search_path)
        return list(self._parse(html))

    def _parse(self, html: str):
        tree = HTMLParser(html)
        # ⚠️ Sélecteur à adapter
        for card in tree.css(".product-item, [class*='offer'], a[href*='/brawl-stars/']"):
            href = (card.css_first("a") or card).attributes.get("href", "")
            if not href or "/accounts" in href.rstrip("/").split("/")[-1]:
                continue
            url = href if href.startswith("http") else self.base_url + href
            title_el = card.css_first("h3, h2, [class*='title']")
            title = title_el.text(strip=True) if title_el else card.text(strip=True)[:200]
            price = self._extract_price(card.text())
            if price is None:
                continue
            yield RawListing(
                source=self.name, url=url,
                title=title, description="", price_usd=price,
            )

    @staticmethod
    def _extract_price(text: str) -> float | None:
        m = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
        return float(m.group(1)) if m else None
```

- [ ] **Step 6: Itérer sélecteurs jusqu'à passing**

Run: `pytest sales/tests/test_scraper_igv.py -v`

- [ ] **Step 7: Commit**

```bash
git add sales/scrapers/igv.py sales/tests/test_scraper_igv.py sales/tests/fixtures/igv_search.html
git commit -m "sales: iGV scraper"
```

---

## Task 10: Valuator (matching + estimation)

**Files:**
- Create: `sales/valuator.py`
- Create: `sales/tests/test_valuator.py`

- [ ] **Step 1: Écrire les tests**

Créer `sales/tests/test_valuator.py` :

```python
from sales.valuator import estimate_price
from sales.models import AccountProfile, BrawlerInfo, ListingProfile


def _acc(troph=25000, brawlers=60, hyper=10, legend=3):
    bs = []
    for i in range(brawlers):
        bs.append(BrawlerInfo(
            id=i, name="Leon" if i < legend else "X",
            power=11 if i < (brawlers // 2) else 9,
            rank=20, trophies=400, highest_trophies=500,
            gadgets=2, star_powers=2, gears=2,
            has_hypercharge=(i < hyper),
        ))
    return AccountProfile.from_brawlers(
        tag="#X", name="t", total_trophies=troph,
        highest_trophies=troph, exp_level=100, brawlers=bs,
    )


def _lp(price, troph=25000, brawlers=60, hyper=10, legend=3,
        og=False, source="eldorado"):
    return ListingProfile(
        source=source, url=f"http://{source}/{price}", price_usd=price,
        trophies=troph, brawler_count=brawlers, legendary_count=legend,
        hypercharge_count=hyper, has_og_skins=og, raw_title="t",
    )


def test_estimate_median_low_high():
    acc = _acc()
    listings = [_lp(p) for p in [30, 40, 50, 60, 70, 80]]
    est = estimate_price(acc, listings)
    assert est.median == 55.0  # P50 of 30..80
    assert est.low == 42.5     # P25
    assert est.high == 67.5    # P75
    assert est.sample_size == 6


def test_estimate_warns_when_few_listings():
    acc = _acc()
    listings = [_lp(50), _lp(60)]
    est = estimate_price(acc, listings)
    assert any("insuffisant" in w.lower() or "indicative" in w.lower()
               for w in est.warnings)


def test_estimate_filters_low_confidence():
    acc = _acc()
    listings = [
        _lp(50),
        ListingProfile(  # 1 seul champ comparable
            source="x", url="http://y", price_usd=999.0,
            trophies=None, brawler_count=None, legendary_count=None,
            hypercharge_count=None, has_og_skins=False, raw_title="t",
        ),
    ]
    est = estimate_price(acc, listings)
    assert est.sample_size == 1
    assert 999.0 not in [c.price_usd for c in est.top_comparables]


def test_estimate_prefers_closest():
    acc = _acc(troph=25000, brawlers=60)
    far = _lp(1000, troph=5000, brawlers=10)
    near = _lp(50, troph=24000, brawlers=58)
    est = estimate_price(acc, [far, near])
    assert est.top_comparables[0].price_usd == 50


def test_sources_breakdown():
    acc = _acc()
    listings = [_lp(40, source="eldorado"), _lp(50, source="eldorado"),
                _lp(60, source="igv")]
    est = estimate_price(acc, listings)
    assert est.sources_breakdown == {"eldorado": 2, "igv": 1}


def test_handles_none_fields_in_listing():
    acc = _acc()
    listings = [
        _lp(50),
        ListingProfile(  # 3/5 champs (60% confidence, passe le filtre)
            source="pa", url="http://z", price_usd=55.0,
            trophies=25000, brawler_count=58, legendary_count=None,
            hypercharge_count=10, has_og_skins=False, raw_title="t",
        ),
    ]
    est = estimate_price(acc, listings)
    assert est.sample_size == 2
```

- [ ] **Step 2: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_valuator.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implémenter `sales/valuator.py`**

```python
from __future__ import annotations
from statistics import quantiles
from collections import Counter
from sales.models import AccountProfile, ListingProfile, PriceEstimate

WEIGHTS = {
    "trophies": 0.30,
    "brawler_count": 0.25,
    "hypercharge_count": 0.25,
    "legendary_count": 0.15,
    "has_og_skins": 0.05,
}
MIN_CONFIDENCE = 0.5
MIN_LISTINGS_FOR_CONFIDENT = 5
TOP_N = 20
TOP_COMPARABLES = 5


def _norm_diff(account_val: float, listing_val: float, scale: float) -> float:
    return min(abs(account_val - listing_val) / scale, 1.0) if scale > 0 else 0.0


def _distance(acc: AccountProfile, lp: ListingProfile) -> float:
    weights_used: dict[str, float] = {}
    diffs: dict[str, float] = {}

    if lp.trophies is not None:
        diffs["trophies"] = _norm_diff(acc.total_trophies, lp.trophies, max(acc.total_trophies, 1))
        weights_used["trophies"] = WEIGHTS["trophies"]
    if lp.brawler_count is not None:
        diffs["brawler_count"] = _norm_diff(acc.brawler_count, lp.brawler_count, max(acc.brawler_count, 1))
        weights_used["brawler_count"] = WEIGHTS["brawler_count"]
    if lp.hypercharge_count is not None:
        diffs["hypercharge_count"] = _norm_diff(acc.hypercharge_count, lp.hypercharge_count, max(acc.hypercharge_count, 1))
        weights_used["hypercharge_count"] = WEIGHTS["hypercharge_count"]
    if lp.legendary_count is not None:
        diffs["legendary_count"] = _norm_diff(acc.legendary_count, lp.legendary_count, max(acc.legendary_count, 1))
        weights_used["legendary_count"] = WEIGHTS["legendary_count"]
    # has_og_skins toujours présent (bool)
    diffs["has_og_skins"] = 0.0 if lp.has_og_skins == (False) else 1.0
    # ↑ comparé à False par défaut côté account (l'API n'expose pas les skins)
    weights_used["has_og_skins"] = WEIGHTS["has_og_skins"]

    total_weight = sum(weights_used.values())
    if total_weight == 0:
        return 1.0
    return sum(diffs[k] * (w / total_weight) for k, w in weights_used.items())


def _percentiles(prices: list[float]) -> tuple[float, float, float]:
    if len(prices) == 1:
        p = prices[0]
        return p, p, p
    q = quantiles(prices, n=4, method="inclusive")
    return q[0], q[1], q[2]  # P25, P50, P75


def estimate_price(acc: AccountProfile, listings: list[ListingProfile]) -> PriceEstimate:
    warnings: list[str] = []

    filtered = [lp for lp in listings if lp.extraction_confidence >= MIN_CONFIDENCE]
    if len(filtered) < len(listings):
        warnings.append(f"{len(listings) - len(filtered)} listing(s) filtré(s) (confidence < {MIN_CONFIDENCE})")

    if not filtered:
        return PriceEstimate(
            low=0, median=0, high=0, sample_size=0,
            sources_breakdown={}, top_comparables=[],
            warnings=warnings + ["aucun listing exploitable"],
        )

    scored = sorted(((lp, _distance(acc, lp)) for lp in filtered), key=lambda x: x[1])
    top = scored[:TOP_N]
    sample = [lp for lp, _ in top]
    prices = [lp.price_usd for lp in sample]
    low, median, high = _percentiles(prices)

    if len(sample) < MIN_LISTINGS_FOR_CONFIDENT:
        warnings.append(
            f"échantillon insuffisant ({len(sample)} listings) — estimation indicative"
        )

    return PriceEstimate(
        low=round(low, 2),
        median=round(median, 2),
        high=round(high, 2),
        sample_size=len(sample),
        sources_breakdown=dict(Counter(lp.source for lp in sample)),
        top_comparables=sample[:TOP_COMPARABLES],
        warnings=warnings,
    )
```

- [ ] **Step 4: Relancer les tests**

Run: `pytest sales/tests/test_valuator.py -v`
Expected: tous PASS

- [ ] **Step 5: Commit**

```bash
git add sales/valuator.py sales/tests/test_valuator.py
git commit -m "sales: valuator with weighted distance matching + percentile estimation"
```

---

## Task 11: Report markdown

**Files:**
- Create: `sales/report.py`
- Create: `sales/tests/test_report.py`

- [ ] **Step 1: Écrire les tests**

Créer `sales/tests/test_report.py` :

```python
from sales.report import render_report
from sales.models import AccountProfile, BrawlerInfo, ListingProfile, PriceEstimate


def _acc():
    bs = [BrawlerInfo(id=1, name="Shelly", power=11, rank=20,
                      trophies=500, highest_trophies=600,
                      gadgets=2, star_powers=2, gears=3, has_hypercharge=True)]
    return AccountProfile.from_brawlers(
        tag="#ABC", name="Bob", total_trophies=28450,
        highest_trophies=30100, exp_level=145, brawlers=bs,
    )


def _est():
    lp = ListingProfile(source="eldorado", url="http://e/1", price_usd=42.0,
                        trophies=27000, brawler_count=65, legendary_count=3,
                        hypercharge_count=11, has_og_skins=False, raw_title="x")
    return PriceEstimate(low=35, median=48, high=65, sample_size=18,
                         sources_breakdown={"eldorado": 8, "playerauctions": 7, "igv": 3},
                         top_comparables=[lp], warnings=[])


def test_report_contains_key_fields():
    r = render_report(_acc(), _est())
    assert "#ABC" in r
    assert "Bob" in r
    assert "28,450" in r
    assert "$35" in r and "$48" in r and "$65" in r
    assert "18" in r  # sample size
    assert "eldorado" in r.lower()
    assert "http://e/1" in r


def test_report_renders_warnings():
    est = _est()
    est.warnings = ["échantillon insuffisant"]
    r = render_report(_acc(), est)
    assert "échantillon insuffisant" in r
```

- [ ] **Step 2: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_report.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implémenter `sales/report.py`**

```python
from __future__ import annotations
from sales.models import AccountProfile, PriceEstimate


def render_report(acc: AccountProfile, est: PriceEstimate) -> str:
    lines: list[str] = []
    lines.append(f"🎯 Compte {acc.tag} ({acc.name}) — Estimation")
    lines.append("")
    lines.append("📊 Profil")
    lines.append(f"   Trophées        : {acc.total_trophies:,}".replace(",", ","))
    lines.append(f"   Brawlers        : {acc.brawler_count}")
    lines.append(f"   Hypercharges    : {acc.hypercharge_count}")
    lines.append(f"   Légendaires     : {acc.legendary_count}")
    lines.append(f"   Power 11 maxés  : {acc.maxed_count}")
    lines.append("")

    if est.sample_size == 0:
        lines.append("❌ Aucune estimation possible (pas de listings exploitables)")
    else:
        lines.append(f"💰 Estimation prix : ${est.low:g} – ${est.high:g} (médiane ${est.median:g})")
        lines.append(f"   Basé sur {est.sample_size} listings comparables")
        breakdown = " | ".join(f"{k}: {v}" for k, v in est.sources_breakdown.items())
        lines.append(f"   {breakdown}")
        lines.append("")
        lines.append("🏷️  Top comparables")
        for i, c in enumerate(est.top_comparables, 1):
            troph = f"{c.trophies/1000:.0f}k" if c.trophies else "?"
            br = c.brawler_count or "?"
            hc = c.hypercharge_count if c.hypercharge_count is not None else "?"
            lines.append(f"   {i}. ${c.price_usd:g} — {troph} 🏆, {br} brawlers, {hc} hyper  →  {c.url}")

    if est.warnings:
        lines.append("")
        lines.append("⚠️  Notes")
        for w in est.warnings:
            lines.append(f"   - {w}")

    lines.append("")
    lines.append("   - Skins non pris en compte (API ne les expose pas)")
    lines.append("   - Date création compte inconnue")
    return "\n".join(lines)
```

- [ ] **Step 4: Relancer les tests**

Run: `pytest sales/tests/test_report.py -v`
Expected: tous PASS

- [ ] **Step 5: Commit**

```bash
git add sales/report.py sales/tests/test_report.py
git commit -m "sales: markdown report renderer"
```

---

## Task 12: CLI entrypoint

**Files:**
- Create: `sales/cli.py`
- Create: `sales/__main__.py`
- Create: `sales/tests/test_cli.py`

- [ ] **Step 1: Créer `sales/__main__.py`**

```python
from sales.cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Écrire les tests CLI**

Créer `sales/tests/test_cli.py` :

```python
import json
from unittest.mock import patch
from sales.cli import main
from sales.models import AccountProfile, BrawlerInfo, ListingProfile, PriceEstimate
from sales.scrapers.base import RawListing


def _fake_account():
    bs = [BrawlerInfo(id=1, name="Shelly", power=11, rank=20,
                      trophies=500, highest_trophies=600,
                      gadgets=2, star_powers=2, gears=3, has_hypercharge=True)]
    return AccountProfile.from_brawlers(
        tag="#ABC", name="Bob", total_trophies=25000,
        highest_trophies=26000, exp_level=100, brawlers=bs,
    )


def _fake_listings():
    return [RawListing(source="eldorado", url="http://e/1",
                       title="25000 trophies 60 brawlers 10 hyper",
                       description="", price_usd=50.0)] * 10


def test_cli_success(capsys):
    with patch("sales.cli.fetch_account", return_value=_fake_account()), \
         patch("sales.cli._run_scrapers", return_value=_fake_listings()), \
         patch("sales.cli._load_api_key", return_value="key"):
        code = main(["ABC"])
    out = capsys.readouterr().out
    assert code == 0
    assert "#ABC" in out
    assert "$50" in out


def test_cli_missing_tag(capsys):
    code = main([])
    assert code != 0


def test_cli_json_mode(capsys):
    with patch("sales.cli.fetch_account", return_value=_fake_account()), \
         patch("sales.cli._run_scrapers", return_value=_fake_listings()), \
         patch("sales.cli._load_api_key", return_value="key"):
        code = main(["ABC", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["tag"] == "#ABC"
    assert "estimate" in payload
    assert code == 0


def test_cli_fetcher_error(capsys):
    from sales.fetcher import FetcherError
    with patch("sales.cli.fetch_account", side_effect=FetcherError("not found")), \
         patch("sales.cli._load_api_key", return_value="key"):
        code = main(["BADTAG"])
    err = capsys.readouterr().err
    assert code == 1
    assert "not found" in err
```

- [ ] **Step 3: Vérifier que les tests échouent**

Run: `pytest sales/tests/test_cli.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 4: Implémenter `sales/cli.py`**

```python
from __future__ import annotations
import argparse
import json
import os
import sys
from dataclasses import asdict
from dotenv import load_dotenv

from sales.fetcher import fetch_account, FetcherError
from sales.scrapers.base import SearchFilters, RawListing
from sales.scrapers.eldorado import EldoradoScraper
from sales.scrapers.playerauctions import PlayerAuctionsScraper
from sales.scrapers.igv import IgvScraper
from sales.scrapers.http import ScraperBlockedError
from sales.normalizer import normalize_listing
from sales.valuator import estimate_price
from sales.report import render_report

SCRAPERS = {
    "eldorado": EldoradoScraper,
    "playerauctions": PlayerAuctionsScraper,
    "igv": IgvScraper,
}


def _load_api_key() -> str | None:
    load_dotenv("sales/.env")
    return os.environ.get("BRAWL_API_KEY")


def _run_scrapers(names: list[str], filters: SearchFilters) -> list[RawListing]:
    results: list[RawListing] = []
    for name in names:
        cls = SCRAPERS.get(name)
        if cls is None:
            print(f"⚠️  scraper inconnu: {name}", file=sys.stderr)
            continue
        try:
            results.extend(cls().search(filters))
        except ScraperBlockedError as e:
            print(f"⚠️  {name} bloqué: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  {name} erreur: {e}", file=sys.stderr)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sales", description="Brawl Stars account estimator")
    parser.add_argument("tag", nargs="?", help="Player tag (avec ou sans #)")
    parser.add_argument("--json", action="store_true", help="Sortie JSON")
    parser.add_argument("--scrapers", default="eldorado,playerauctions,igv",
                        help="Liste de scrapers séparés par virgule")
    parser.add_argument("--no-cache", action="store_true", help="Ignore le cache disque")
    args = parser.parse_args(argv)

    if not args.tag:
        parser.print_usage(sys.stderr)
        return 2

    api_key = _load_api_key()

    try:
        account = fetch_account(args.tag, api_key=api_key)
    except FetcherError as e:
        print(f"erreur: {e}", file=sys.stderr)
        return 1

    scraper_names = [s.strip() for s in args.scrapers.split(",") if s.strip()]
    raw_listings = _run_scrapers(scraper_names, SearchFilters())
    listings = [normalize_listing(r) for r in raw_listings]
    estimate = estimate_price(account, listings)

    if args.json:
        payload = {
            "tag": account.tag,
            "name": account.name,
            "account": {
                "total_trophies": account.total_trophies,
                "brawler_count": account.brawler_count,
                "hypercharge_count": account.hypercharge_count,
                "legendary_count": account.legendary_count,
                "maxed_count": account.maxed_count,
            },
            "estimate": {
                "low": estimate.low, "median": estimate.median, "high": estimate.high,
                "sample_size": estimate.sample_size,
                "sources_breakdown": estimate.sources_breakdown,
                "warnings": estimate.warnings,
                "top_comparables": [asdict(c) for c in estimate.top_comparables],
            },
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_report(account, estimate))

    return 2 if estimate.warnings else 0
```

- [ ] **Step 5: Relancer les tests**

Run: `pytest sales/tests/test_cli.py -v`
Expected: tous PASS

- [ ] **Step 6: Lancer la suite complète**

Run: `pytest sales/ -v`
Expected: tous PASS, >85% coverage sur `sales/`

- [ ] **Step 7: Test d'invocation réelle (sans API key, doit échouer proprement)**

Run: `python -m sales TEST123`
Expected: stderr `erreur: BRAWL_API_KEY manquante (variable d'env)`, exit code 1

- [ ] **Step 8: Commit**

```bash
git add sales/cli.py sales/__main__.py sales/tests/test_cli.py
git commit -m "sales: CLI entrypoint (text + JSON output)"
```

---

## Task 13: README + smoke test manuel

**Files:**
- Create: `sales/README.md`

- [ ] **Step 1: Écrire le README**

Créer `sales/README.md` :

````markdown
# sales — Brawl Stars account estimator

CLI Python qui estime le prix de revente d'un compte Brawl Stars via
l'API officielle Supercell + scraping live des marketplaces.

## Setup

1. Créer une clé API sur https://developer.brawlstars.com (whitelist
   l'IP de la machine).
2. Copier `.env.example` vers `.env` et coller la clé :
   ```
   cp sales/.env.example sales/.env
   # éditer sales/.env → BRAWL_API_KEY=...
   ```
3. Installer les deps :
   ```
   pip install httpx selectolax python-dotenv
   ```

## Usage

```bash
python -m sales #ABC123                          # rapport markdown
python -m sales ABC123 --json                    # sortie machine
python -m sales ABC123 --scrapers eldorado       # restreindre
python -m sales ABC123 --no-cache                # forcer re-scrape
```

Exit codes : 0 succès, 1 erreur fatale, 2 succès avec warnings.

## Tests

```bash
pytest sales/ -v
```

## Limitations connues

- L'API officielle n'expose pas les skins ni la date de création du compte.
- Les scrapers parsent du HTML public ; ils peuvent casser quand les
  marketplaces changent leur layout. Réviser les fixtures dans
  `sales/tests/fixtures/` si besoin.
````

- [ ] **Step 2: Smoke test manuel avec vraie clé API**

⚠️ Action manuelle requise : créer la clé API sur developer.brawlstars.com, la mettre dans `sales/.env`, puis :

```bash
python -m sales <ton_tag_perso>
```

Vérifier :
- Le profil compte s'affiche correctement.
- Au moins 1 scraper retourne des listings (les autres peuvent être bloqués → warning).
- L'estimation a du sens (compare avec une recherche manuelle sur Eldorado).

Si un scraper est constamment bloqué, c'est attendu (à itérer plus tard avec proxy).

- [ ] **Step 3: Commit**

```bash
git add sales/README.md
git commit -m "sales: README + usage docs"
```

---

## Récap final

- 13 tâches, ~3-4h d'exécution si scrapers parsent du premier coup, plus si itération sélecteurs nécessaire.
- Toutes les étapes TDD : test d'abord, implémentation minimale, commit.
- Aucune dépendance au code du bot principal.
- Couverture cible : >85% sur `sales/`.
