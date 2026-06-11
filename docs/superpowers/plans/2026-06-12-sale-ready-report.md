# Sale-ready Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quand un compte atteint sa tranche de trophées cible, arrêter le grind, lire/analyser les données du compte, et envoyer un rapport de revente actionnable sur Telegram.

**Architecture :** Étend la config schedule globale (`sale_target_trophies`) propagée au worker. `play_schedule` gagne un état `sale_ready` (via un trophy-provider DB, miroir du match-count provider) qui ferme le jeu. L'orchestrateur (`telegram_main`) détecte la transition et appelle un nouveau module `sale_report.py` qui agrège le profil brawlace + or/gemmes OCR best-effort, estime un prix, construit une checklist d'actions, et envoie via le bot Telegram du worker. Idempotent via un fichier local gitignored.

**Tech Stack :** Python 3, pytest, SQLite (worker `db.py`), easyocr (déjà présent worker), requests, Postgres (cloud `cloud_panel/db.py`), FastAPI panel, vanilla JS.

**Spec :** `docs/superpowers/specs/2026-06-12-sale-ready-report-design.md`

---

## File Structure

- **Create** `sale_report.py` — agrégation données compte + estimation prix + checklist + format + envoi (worker).
- **Create** `tests/test_sale_report.py` — tests des fonctions pures + gather mocké.
- **Modify** `db.py` — `latest_account_trophies(account_id)`.
- **Modify** `tests/test_db.py` (ou créer si absent) — test de `latest_account_trophies`.
- **Modify** `play_schedule.py` — `_TROPHY_PROVIDER`, `set_trophy_provider`, parse `sale_target_trophies`, état `sale_ready`.
- **Modify** `tests/test_play_schedule.py` — cas `sale_ready`.
- **Modify** `telegram_main.py` — câblage provider + détection transition → envoi.
- **Modify** `cloud_panel/schedule_config.py` — DEFAULTS + scalar `sale_target_trophies` + sérialisation.
- **Modify** `cloud_panel/static/index.html` — input « Cible de revente ».
- **Modify** `cloud_panel/static/app.js` — lecture/écriture du champ.
- **Modify** `.gitignore` — `cfg/sale_report_state.json`.

---

## Task 1 : `db.latest_account_trophies` (worker SQLite)

**Files:**
- Modify: `db.py` (après `count_matches_today`, ~ligne 246)
- Test: `tests/test_db.py`

- [ ] **Step 1 : Écrire le test qui échoue**

Vérifier d'abord si `tests/test_db.py` existe (`ls tests/test_db.py`). S'il existe, regarder comment il initialise une DB éphémère (probablement `db.init(path)` ou monkeypatch de `conn()`), et reproduire ce pattern. S'il n'existe pas, le créer avec ce contenu (adapter `db.init`/setup au pattern réel observé dans `db.py` — chercher `def init` / comment les autres tests ouvrent la DB) :

```python
import time
import db


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "t.db"), raising=False)
    # Forcer une nouvelle connexion sur le chemin de test.
    db._CONN = None  # si db.py cache la connexion ; sinon adapter
    db.init() if hasattr(db, "init") else None
    return db


def test_latest_account_trophies_returns_most_recent(tmp_path, monkeypatch):
    d = _fresh_db(tmp_path, monkeypatch)
    aid = d.upsert_account(instance_id=1, tag="ABC", name="t") \
        if hasattr(d, "upsert_account") else 1
    # Insérer 2 matches avec account_trophies_after croissant.
    d.record_match(account_id=aid, brawler="shelly", result="victory",
                   trophies_before=10, trophies_after=18,
                   account_trophies_after=21000)
    time.sleep(0.01)
    d.record_match(account_id=aid, brawler="shelly", result="victory",
                   trophies_before=18, trophies_after=26,
                   account_trophies_after=21008)
    assert d.latest_account_trophies(aid) == 21008


def test_latest_account_trophies_none_when_empty(tmp_path, monkeypatch):
    d = _fresh_db(tmp_path, monkeypatch)
    assert d.latest_account_trophies(99999) is None
```

> ⚠️ **Avant d'écrire l'implémentation**, lire le vrai `db.py` : signature exacte de `record_match` (ordre des args), nom du helper d'init / de la variable de chemin DB, et comment les tests existants ouvrent une DB jetable. Adapter `_fresh_db` et les appels en conséquence. Le comportement testé reste : « renvoie le dernier `account_trophies_after` non-NULL, sinon None ».

- [ ] **Step 2 : Lancer le test, vérifier l'échec**

Run: `python3 -m pytest tests/test_db.py -k latest_account_trophies -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'latest_account_trophies'`

- [ ] **Step 3 : Implémenter `latest_account_trophies`**

Dans `db.py`, juste après `count_matches_today` :

```python
def latest_account_trophies(account_id: int | None) -> "int | None":
    """Most recent account-wide trophy total logged for this account, or None.

    Source of the play-schedule trophy gate (sale_ready). Reads the latest
    non-NULL account_trophies_after — the authoritative running total written
    after each match.
    """
    if account_id is None:
        return None
    with _lock:
        row = conn().execute(
            "SELECT account_trophies_after FROM matches "
            "WHERE account_id = ? AND account_trophies_after IS NOT NULL "
            "ORDER BY timestamp DESC, id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None
```

- [ ] **Step 4 : Lancer le test, vérifier le succès**

Run: `python3 -m pytest tests/test_db.py -k latest_account_trophies -v`
Expected: PASS (2 passed)

- [ ] **Step 5 : Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat(db): latest_account_trophies for the sale-ready trophy gate"
```

---

## Task 2 : `play_schedule` — provider trophées + état `sale_ready`

**Files:**
- Modify: `play_schedule.py` (`_DEFAULTS` ~66, provider ~31-60, `_apply_cfg` ~294, `state` ~445, `should_play_now` ~470)
- Test: `tests/test_play_schedule.py`

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter à `tests/test_play_schedule.py` (les tests injectent un cfg dict → `PlaySchedule(cfg)`, voir tests existants pour le style) :

```python
def test_sale_ready_when_total_reaches_target():
    s = play_schedule.PlaySchedule({"enabled": True, "sleep_start_hour": 3,
                                    "sleep_end_hour": 4, "sale_target_trophies": 25000})
    s.set_trophy_total_provider(lambda: 24999)
    # midi, hors sommeil → play tant qu'on est sous la cible
    noon = _at(12, 0)
    assert s.state(noon) == "play"
    s.set_trophy_total_provider(lambda: 25000)
    assert s.state(noon) == "sale_ready"


def test_sale_target_zero_never_triggers():
    s = play_schedule.PlaySchedule({"enabled": True, "sleep_start_hour": 3,
                                    "sleep_end_hour": 4, "sale_target_trophies": 0})
    s.set_trophy_total_provider(lambda: 999999)
    assert s.state(_at(12, 0)) == "play"


def test_sleep_outranks_sale_ready():
    s = play_schedule.PlaySchedule({"enabled": True, "sleep_start_hour": 1,
                                    "sleep_end_hour": 9, "sleep_jitter_minutes": 0,
                                    "sale_target_trophies": 25000})
    s.set_trophy_total_provider(lambda: 30000)
    assert s.state(_at(3, 0)) == "sleep"
```

> ⚠️ **Avant**, ouvrir `tests/test_play_schedule.py` : récupérer le vrai helper de construction de timestamp (ici nommé `_at(h, m)` — utiliser le helper réel du fichier ; s'il s'appelle autrement, l'utiliser) et le style d'instanciation. La méthode provider est `set_trophy_total_provider` (cohérent avec l'implémentation ci-dessous).

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run: `python3 -m pytest tests/test_play_schedule.py -k "sale_ready or sale_target or outranks" -v`
Expected: FAIL — `AttributeError: 'PlaySchedule' object has no attribute 'set_trophy_total_provider'`

- [ ] **Step 3a : Ajouter le provider module-level**

Dans `play_schedule.py`, après le bloc `_MATCH_COUNT_PROVIDER` (~ligne 60) :

```python
# Provider (set by the worker) returning the account-wide trophy TOTAL, read
# from the DB (latest account_trophies_after). Drives the sale_ready gate.
_TROPHY_TOTAL_PROVIDER = None


def _trophy_total() -> "int | None":
    fn = _TROPHY_TOTAL_PROVIDER
    if fn is None:
        return None
    try:
        v = fn()
        return int(v) if v is not None else None
    except Exception:
        log.debug("trophy-total provider failed", exc_info=True)
        return None


def set_trophy_total_provider(fn) -> None:
    """Register a callable() -> int|None giving the account's current trophy
    total (DB-backed). Used by the sale_ready gate to auto-stop the grind when
    the configured sale_target_trophies is reached."""
    global _TROPHY_TOTAL_PROVIDER
    _TROPHY_TOTAL_PROVIDER = fn
```

Et une méthode d'instance pour permettre l'injection en test (juste après `__init__`, près des compat properties ~274) :

```python
    def set_trophy_total_provider(self, fn) -> None:
        """Per-instance provider override (used by tests; the module-level
        provider is used in production via _trophy_total)."""
        self._trophy_provider = fn
```

Et initialiser `self._trophy_provider = None` dans `__init__` (vers ligne 261, près de `self._active_pause_label`).

Helper de lecture (méthode) qui préfère le provider d'instance puis le module :

```python
    def _trophy_now(self) -> "int | None":
        fn = getattr(self, "_trophy_provider", None)
        if fn is not None:
            try:
                v = fn()
                return int(v) if v is not None else None
            except Exception:
                log.debug("instance trophy provider failed", exc_info=True)
                return None
        return _trophy_total()
```

- [ ] **Step 3b : Parser `sale_target_trophies`**

Dans `_DEFAULTS` (~ligne 82, après `dayoff_chance`) ajouter :

```python
    "sale_target_trophies": 0,    # 0 = disabled; >0 → sale_ready at this total
```

Ajouter `"sale_target_trophies"` au tuple `_SCALAR_KEYS` (~ligne 86-89).

Dans `_apply_cfg` (après `self.dayoff_chance = ...`, ~ligne 314) :

```python
        self.sale_target = max(0, int(cfg.get("sale_target_trophies", 0) or 0))
```

- [ ] **Step 3c : Brancher l'état `sale_ready` dans `state()`**

Dans `state()` (~ligne 462), insérer le gate **après** `cap` et **avant** `break` (priorité dayoff>sleep>pause>cap>sale_ready>break>play) :

```python
            if self._today_block_cap and self._blocks_today >= self._today_block_cap:
                return "cap"
            if self.sale_target > 0:
                total = self._trophy_now()
                if total is not None and total >= self.sale_target:
                    return "sale_ready"
            if time.monotonic() < self._break_until:
                return "break"
            return "play"
```

Dans `should_play_now` (~ligne 482, avant le `return False, "pause"` final) :

```python
        if st == "sale_ready":
            return False, f"pret a vendre (cible {self.sale_target} tr atteinte)"
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run: `python3 -m pytest tests/test_play_schedule.py -v`
Expected: PASS (tous, y compris les 3 nouveaux ; aucune régression)

- [ ] **Step 5 : Commit**

```bash
git add play_schedule.py tests/test_play_schedule.py
git commit -m "feat(schedule): sale_ready state + trophy-total provider (auto-stop at sale target)"
```

---

## Task 3 : `sale_report.py` — agrégation, estimation, checklist, format

**Files:**
- Create: `sale_report.py`
- Test: `tests/test_sale_report.py`

- [ ] **Step 1 : Écrire les tests qui échouent (fonctions pures)**

`tests/test_sale_report.py` :

```python
import sale_report


def _data(**kw):
    base = dict(total=22000, brawler_count=34, p11=["maisie", "brock", "shelly"],
                below_ceiling=2, headroom=300, gold=12000, gems=1600,
                name="Zeffut5.0", tag="QPRCQ9RV2")
    base.update(kw)
    return base


def test_estimate_price_floor_scales_with_trophies():
    low, high = sale_report.estimate_price(_data(total=20000, p11=[]))
    # base ~0.7$/1000 => ~14$ ; bonus P11 nul
    assert 10 <= low <= 18
    assert high >= low


def test_estimate_price_p11_bonus_raises_high():
    low_a, high_a = sale_report.estimate_price(_data(total=22000, p11=[]))
    low_b, high_b = sale_report.estimate_price(_data(total=22000,
                                                     p11=["a", "b", "c", "d", "e"]))
    assert high_b > high_a


def test_build_actions_quantifies_hypercharges_when_gold_known():
    acts = sale_report.build_actions(_data(gold=12000,
                                           p11=["maisie", "brock", "shelly"]))
    joined = " ".join(acts).lower()
    assert "2" in joined            # 12000 // 5000 = 2 hypercharges finançables
    assert "hypercharge" in joined
    assert any("gemme" in a.lower() for a in acts)   # ne pas dépenser les gemmes


def test_build_actions_degrades_when_gold_unknown():
    acts = sale_report.build_actions(_data(gold=None))
    joined = " ".join(acts).lower()
    assert "5000" in joined         # rappelle le coût par HC sans quantifier
    assert "hypercharge" in joined


def test_format_telegram_has_no_raw_none_and_includes_total():
    d = _data(gold=None, gems=None)
    msg = sale_report.format_telegram(d, sale_report.build_actions(d),
                                      sale_report.estimate_price(d))
    assert "None" not in msg
    assert "22000" in msg or "22 000" in msg
    assert d["name"] in msg
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run: `python3 -m pytest tests/test_sale_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sale_report'`

- [ ] **Step 3 : Implémenter `sale_report.py`**

```python
"""Sale-ready report — collecte les données d'un compte arrivé à sa cible de
trophées, estime un prix plancher, construit une checklist d'actions avant
mise en vente, et envoie le tout sur Telegram.

Source fiable : profil brawlace (trophées/power/brawlers) via account_detect.
Enrichissement best-effort : or/gemmes lus en OCR sur le lobby (easyocr) — si
la lecture échoue, le rapport le signale au lieu de bloquer.

La mise en vente reste manuelle (Zeffut). Skins & hypercharges ne sont PAS
lisibles automatiquement (aucune API) → le rapport demande de les confirmer.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("sale_report")

CEILING = 750          # plafond d'efficacité (cf. push_max.EFFICIENCY_CEILING)
HC_COST = 5000         # or par hypercharge
_STATE_PATH = Path(__file__).resolve().parent / "cfg" / "sale_report_state.json"


# ---- collecte ----
def gather(tag: str) -> dict:
    """Agrège le profil + or/gemmes (best-effort) en un dict plat."""
    import account_detect
    prof = account_detect.fetch_account_profile(tag, force=True)
    brawlers = prof.get("brawlers") or []
    total = sum(int(b.get("trophies") or 0) for b in brawlers)
    p11 = [b["name"] for b in brawlers if int(b.get("power") or 0) >= 11]
    below = [b for b in brawlers if int(b.get("trophies") or 0) < CEILING]
    headroom = sum(CEILING - int(b.get("trophies") or 0) for b in below)
    gold, gems = _read_currencies_best_effort()
    return {
        "name": prof.get("name") or tag,
        "tag": tag,
        "total": total,
        "brawler_count": len(brawlers),
        "p11": p11,
        "below_ceiling": len(below),
        "headroom": headroom,
        "gold": gold,
        "gems": gems,
    }


def _read_currencies_best_effort() -> "tuple[int | None, int | None]":
    """(or, gemmes) lus sur le lobby ; (None, None) si indisponible/implausible."""
    try:
        from revente.read_currencies import read_lobby_numbers
        nums = read_lobby_numbers()      # {"gold": int, "gems": int, ...}
    except Exception:
        log.info("currency OCR unavailable — report degrades gracefully",
                 exc_info=True)
        return None, None

    def _ok(v):
        return v if isinstance(v, int) and 0 <= v <= 10_000_000 else None

    return _ok(nums.get("gold")), _ok(nums.get("gems"))


# ---- estimation prix (table inline, alignee revente/grille_prix.md 2026-06-12) ----
def estimate_price(data: dict) -> "tuple[int, int]":
    total = int(data.get("total") or 0)
    base = total / 1000.0 * 0.7
    p11_bonus = len(data.get("p11") or []) * 1.5
    low = int(round(base))
    high = int(round(base + p11_bonus))
    # Bornes plancher par palier (compte « basique » de la grille).
    if total >= 30000:
        low = max(low, 35)
    elif total >= 25000:
        low = max(low, 20)
    elif total >= 20000:
        low = max(low, 13)
    elif total >= 15000:
        low = max(low, 9)
    high = max(high, low)
    return low, high


# ---- checklist d'actions ----
def build_actions(data: dict) -> "list[str]":
    acts: list[str] = []
    p11 = data.get("p11") or []
    gold = data.get("gold")
    if gold is not None:
        n = gold // HC_COST
        if n > 0 and p11:
            cibles = ", ".join(p11[:max(1, n)])
            acts.append(f"Achete {n} hypercharge(s) avec tes {gold} or "
                        f"(5000/HC) sur : {cibles}.")
        elif p11:
            acts.append(f"Or insuffisant pour une hypercharge ({gold}/5000) — "
                        f"garde-le pour plus tard.")
        else:
            acts.append(f"Tu as {gold} or mais aucun brawler P11 — maxe un "
                        f"brawler meta P11 d'abord, puis hypercharge-le.")
    else:
        if p11:
            acts.append(f"Verifie ton or et achete des hypercharges (5000 or/HC) "
                        f"sur tes P11 : {', '.join(p11)}.")
        else:
            acts.append("Verifie ton or ; maxe un brawler meta P11 puis "
                        "hypercharge-le (5000 or/HC).")
    acts.append("Ne depense PAS les gemmes (argument de revente).")
    acts.append("Confirme tes skins rares (Star Shelly, Virus 8-Bit, etc.) — "
                "le bot ne peut pas les lire, ils peuvent doubler le prix.")
    return acts


# ---- format Telegram ----
def format_telegram(data: dict, actions: "list[str]",
                    price: "tuple[int, int]") -> str:
    low, high = price
    gold = data.get("gold")
    gems = data.get("gems")
    gold_s = f"{gold}" if gold is not None else "a verifier a la main"
    gems_s = f"{gems}" if gems is not None else "a verifier a la main"
    lines = [
        f"\U0001F3C1 Compte PRET A VENDRE — {data.get('name')}",
        f"Tag : {data.get('tag')}",
        "",
        f"\U0001F3C6 Trophees : {data.get('total')}",
        f"\U0001F9CD Brawlers : {data.get('brawler_count')}  |  "
        f"P11 : {len(data.get('p11') or [])}",
        f"\U0001FA99 Or : {gold_s}  |  \U0001F48E Gemmes : {gems_s}",
        f"\U0001F4B0 Estimation plancher : {low}-{high} $ "
        f"(hors skins/HC a confirmer)",
        "",
        "✅ A FAIRE AVANT DE LISTER :",
    ]
    for a in actions:
        lines.append(f"  • {a}")
    lines.append("")
    lines.append("Une fois fait : liste sur Eldorado et previens-moi.")
    return "\n".join(lines)


# ---- idempotence ----
def already_notified(tag: str, target: int) -> bool:
    try:
        state = json.loads(_STATE_PATH.read_text())
        return int(state.get(tag, 0)) == int(target)
    except Exception:
        return False


def mark_notified(tag: str, target: int) -> None:
    try:
        state = {}
        if _STATE_PATH.exists():
            state = json.loads(_STATE_PATH.read_text())
        state[tag] = int(target)
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state))
    except Exception:
        log.warning("could not persist sale_report state", exc_info=True)


# ---- orchestration ----
def build_and_send(tag: str, target: int, send_fn) -> bool:
    """Construit le rapport et l'envoie via send_fn(text:str). Renvoie True si
    envoye (et marque l'idempotence). best-effort : toute exception → False
    SANS marquer (reessaiera)."""
    try:
        data = gather(tag)
    except Exception:
        log.warning("sale_report.gather failed for %s", tag, exc_info=True)
        return False
    if not data.get("total"):
        log.warning("sale_report: empty profile for %s — skipping send", tag)
        return False
    try:
        msg = format_telegram(data, build_actions(data), estimate_price(data))
        send_fn(msg)
    except Exception:
        log.warning("sale_report send failed for %s", tag, exc_info=True)
        return False
    mark_notified(tag, target)
    log.info("sale-ready report sent for %s (target %d, total %d)",
             tag, target, data["total"])
    return True
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run: `python3 -m pytest tests/test_sale_report.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5 : Commit**

```bash
git add sale_report.py tests/test_sale_report.py
git commit -m "feat(revente): sale_report module — gather, price estimate, action checklist, telegram format"
```

---

## Task 4 : Câblage dans `telegram_main.py`

**Files:**
- Modify: `telegram_main.py` (provider près de `set_match_count_provider` ~1106-1111 ; détection transition dans/près de `_manage_schedule_powersave` ~230-280)

- [ ] **Step 1 : Brancher le trophy provider**

Repérer le bloc (lu en Task 0) où `play_schedule.set_match_count_provider(lambda: db.count_matches_today(_aid))` est posé (~ligne 1111). Juste après, ajouter :

```python
            play_schedule.set_trophy_total_provider(
                lambda: db.latest_account_trophies(_aid))
```

(`_aid` est l'account id déjà résolu dans ce scope ; vérifier le nom exact de la variable dans le bloc et le réutiliser.)

- [ ] **Step 2 : Détecter la transition `sale_ready` → envoyer le rapport**

Dans `_manage_schedule_powersave(bot)` (~ligne 230), après le calcul de `st = play_schedule.get().state()` (~ligne 244) et la fermeture du jeu (le code existant gère déjà `st != "play"`), ajouter le déclenchement one-shot. Insérer après la branche qui ferme le jeu (vers la fin de la fonction) :

```python
    if st == "sale_ready":
        try:
            import sale_report
            sched = play_schedule.get()
            target = int(getattr(sched, "sale_target", 0) or 0)
            tag = getattr(bot.runner, "account_tag", None) or _current_tag()
            if tag and target and not sale_report.already_notified(tag, target):
                sale_report.build_and_send(tag, target, bot.send)
        except Exception:
            log.warning("sale-ready report trigger failed", exc_info=True)
```

> ⚠️ **Avant d'écrire**, vérifier dans `telegram_main.py` : (a) comment obtenir le tag du compte courant dans ce scope — chercher `account_tag`, `_current_tag`, ou la variable `tag`/`_aid`→tag déjà disponible ; utiliser le mécanisme réel (ne pas inventer `_current_tag` s'il n'existe pas — préférer la variable de tag déjà en portée, ou résoudre via `bot.runner`). (b) que `bot.send(text)` est bien la méthode d'envoi Telegram du worker (confirmé : `TelegramBot.send` ~ligne 1581). Si `_manage_schedule_powersave` n'a pas accès au tag, passer le tag en paramètre depuis l'appelant (le call-site dans la keepalive a `_aid`/le runner en portée).

- [ ] **Step 3 : Vérifier la compilation + non-régression**

Run: `python3 -m py_compile telegram_main.py && python3 -m pyflakes telegram_main.py`
Expected: aucune erreur (pyflakes peut signaler des imports préexistants — ne corriger que ce qui touche le nouveau code).

Run: `python3 -m pytest tests/test_play_schedule.py tests/test_sale_report.py -q`
Expected: PASS

- [ ] **Step 4 : Commit**

```bash
git add telegram_main.py
git commit -m "feat(worker): wire trophy provider + fire sale-ready report on transition"
```

---

## Task 5 : Config cloud `schedule_config.py`

**Files:**
- Modify: `cloud_panel/schedule_config.py` (DEFAULTS ~5, `_SCALARS` ~25-28, `to_toml`)

- [ ] **Step 1 : Ajouter le défaut + scalar**

Dans `DEFAULTS` (après `daily_cap_jitter`) :

```python
    "sale_target_trophies": 0,
```

Dans `_SCALARS`, ajouter `"sale_target_trophies"`.

- [ ] **Step 2 : Vérifier la sérialisation `to_toml`**

Ouvrir `to_toml` : si elle sérialise les scalaires en bouclant sur `_SCALARS` (ou sur les clés de `[schedule]`), `sale_target_trophies` sera inclus automatiquement — vérifier. Sinon, ajouter explicitement la ligne `sale_target_trophies = {n}` au bloc `[schedule]`. La clé DOIT atterrir dans `[schedule]` pour que `play_schedule` la lise.

- [ ] **Step 3 : Test rapide de round-trip**

Run:
```bash
cd cloud_panel && python3 -c "
import schedule_config as sc
m = sc.merge_defaults({'sale_target_trophies': 25000})
assert m['sale_target_trophies'] == 25000, m
t = sc.to_toml(m)
assert 'sale_target_trophies = 25000' in t, t
print('OK', [l for l in t.splitlines() if 'sale_target' in l])
"
```
Expected: `OK ['sale_target_trophies = 25000']`

- [ ] **Step 4 : Commit**

```bash
git add cloud_panel/schedule_config.py
git commit -m "feat(panel): sale_target_trophies in global schedule config"
```

---

## Task 6 : UI panel — champ « Cible de revente »

**Files:**
- Modify: `cloud_panel/static/index.html` (formulaire `#planning-view`, près du champ `daily_match_cap`)
- Modify: `cloud_panel/static/app.js` (remplissage du formulaire + construction du PUT)

- [ ] **Step 1 : Ajouter l'input dans le formulaire**

Dans `index.html`, repérer le champ du quota quotidien (`id` du style `sched-daily_match_cap` ou `name="daily_match_cap"` — chercher `daily_match_cap`). Juste après son groupe, ajouter un groupe identique :

```html
<div class="field">
  <label for="sched-sale_target_trophies">Cible de revente (trophées, 0 = off)</label>
  <input type="number" id="sched-sale_target_trophies" min="0" step="500" value="0">
  <small>À ce total, le bot s'arrête et envoie un rapport de revente sur Telegram.</small>
</div>
```

> ⚠️ Reprendre EXACTEMENT le markup/classes du champ voisin existant (le projet a sa propre convention de `class`/`id` — `sched-<clé>` est l'hypothèse ; vérifier le préfixe réel utilisé par les autres champs et l'aligner).

- [ ] **Step 2 : Remplir le champ au chargement**

Dans `app.js`, repérer la fonction qui peuple le formulaire depuis la config (chercher `daily_match_cap` dans les assignations `.value =`). Ajouter, sur le même modèle :

```javascript
document.getElementById('sched-sale_target_trophies').value =
    cfg.sale_target_trophies ?? 0;
```

(Adapter le nom de la variable `cfg` au code réel.)

- [ ] **Step 3 : Inclure le champ dans le PUT**

Repérer l'objet envoyé au `PUT /api/config/schedule` (chercher `daily_match_cap:` dans la construction du body). Ajouter :

```javascript
sale_target_trophies: parseInt(
    document.getElementById('sched-sale_target_trophies').value, 10) || 0,
```

- [ ] **Step 4 : Bump du cache-bust**

Dans `index.html`, incrémenter le paramètre `?v=` sur l'inclusion de `app.js` (ex. `?v=20260609g` → `?v=20260612a`) — sinon le navigateur sert l'ancien JS.

- [ ] **Step 5 : Vérification syntaxe JS**

Run: `node --check cloud_panel/static/app.js`
Expected: aucune sortie (OK)

- [ ] **Step 6 : Commit**

```bash
git add cloud_panel/static/index.html cloud_panel/static/app.js
git commit -m "feat(panel): sale-target field in planning form + cache-bust"
```

---

## Task 7 : `.gitignore` + vérification finale

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1 : Ignorer l'état d'idempotence**

Ajouter à `.gitignore` :

```
cfg/sale_report_state.json
```

- [ ] **Step 2 : Suite de tests complète**

Run: `python3 -m pytest tests/test_sale_report.py tests/test_play_schedule.py tests/test_db.py -q`
Expected: PASS (aucune régression)

- [ ] **Step 3 : Compilation des modules touchés**

Run: `python3 -m py_compile sale_report.py play_schedule.py db.py telegram_main.py cloud_panel/schedule_config.py`
Expected: aucune erreur

- [ ] **Step 4 : Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore sale_report idempotency state"
```

---

## Notes de déploiement (post-implémentation, hors plan TDD)

- Le cloud (panel) redéploie au push (Dokploy). Le worker pull le nouveau code au prochain `git_update` (fin de match). `cfg/sale_report_state.json` est local au HP, non écrasé par les pulls.
- Activer en posant `sale_target_trophies = 28000` via le formulaire Planning (ou PUT API) — cf. estimation : plateau naturel du compte Zeffut5.0 ≈ 28k.
- L'OCR or/gemmes utilise les crops BlueStacks 16:9 de `revente/read_currencies.py` ; sur le Mi9T (2340×1080) ils peuvent être faux → le garde d'implausibilité renverra None et le rapport dira « à vérifier à la main ». Calibration Mi9T = amélioration future (non bloquante).
```
