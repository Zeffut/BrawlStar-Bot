"""Sale-ready report — collecte les donnees d'un compte arrive a sa cible de
trophees, estime un prix plancher, construit une checklist d'actions avant
mise en vente, et envoie le tout sur Telegram.

Source fiable : profil brawlace (trophees/power/brawlers) via account_detect.
Enrichissement best-effort : or/gemmes lus en OCR sur le lobby (easyocr) — si
la lecture echoue, le rapport le signale au lieu de bloquer.

La mise en vente reste manuelle (Zeffut). Skins & hypercharges ne sont PAS
lisibles automatiquement (aucune API) -> le rapport demande de les confirmer.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("sale_report")

CEILING = 750          # plafond d'efficacite (cf. push_max.EFFICIENCY_CEILING)
HC_COST = 5000         # or par hypercharge
_STATE_PATH = Path(__file__).resolve().parent / "cfg" / "sale_report_state.json"


def gather(tag: str) -> dict:
    """Agrege le profil + or/gemmes (best-effort) en un dict plat."""
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
        import device
        from revente.read_currencies import read_lobby_numbers
        nums = read_lobby_numbers(device.adb_serial())
    except Exception:
        log.info("currency OCR unavailable — report degrades gracefully",
                 exc_info=True)
        return None, None

    def _ok(v):
        return v if isinstance(v, int) and 0 <= v <= 10_000_000 else None

    return _ok(nums.get("gold")), _ok(nums.get("gems"))


def estimate_price(data: dict) -> "tuple[int, int]":
    total = int(data.get("total") or 0)
    base = total / 1000.0 * 0.7
    p11_bonus = len(data.get("p11") or []) * 1.5
    low = int(round(base))
    high = int(round(base + p11_bonus))
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
        "A FAIRE AVANT DE LISTER :",
    ]
    for a in actions:
        lines.append(f"  - {a}")
    lines.append("")
    lines.append("Une fois fait : liste sur Eldorado et previens-moi.")
    return "\n".join(lines)


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


def build_and_send(tag: str, target: int, send_fn) -> bool:
    """Construit le rapport et l'envoie via send_fn(text). Renvoie True si envoye
    (et marque l'idempotence). best-effort : toute exception -> False SANS
    marquer (reessaiera)."""
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
