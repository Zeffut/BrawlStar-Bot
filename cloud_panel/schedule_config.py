"""Serialize the global play-schedule config (stored as JSON in the cloud DB)
into the [schedule] TOML the workers consume via schedule_set."""
from __future__ import annotations

DEFAULTS = {
    "enabled": True,
    "timezone": "Europe/Paris",
    "sleep_start_hour": 1,
    "sleep_end_hour": 9,
    "sleep_jitter_minutes": 40,
    "block_min_minutes": 40,
    "block_max_minutes": 85,
    "break_min_minutes": 20,
    "break_max_minutes": 70,
    "max_blocks_per_day": 0,
    "blocks_jitter": 0,
    "daily_match_cap": 180,
    "daily_cap_jitter": 50,
    "sale_target_trophies": 0,
    "dayoff_weekdays": [],
    "dayoff_chance": 0.0,
    "pause_windows": [],
    "weekend": {},
}

_SCALARS = ("sleep_start_hour", "sleep_end_hour", "sleep_jitter_minutes",
            "block_min_minutes", "block_max_minutes", "break_min_minutes",
            "break_max_minutes", "max_blocks_per_day", "blocks_jitter",
            "daily_match_cap", "daily_cap_jitter", "sale_target_trophies")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")


def merge_defaults(cfg: dict | None) -> dict:
    """Return DEFAULTS overlaid with validated values from cfg."""
    out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
           for k, v in DEFAULTS.items()}
    cfg = cfg or {}
    out["enabled"] = bool(cfg.get("enabled", out["enabled"]))
    if cfg.get("timezone"):
        out["timezone"] = str(cfg["timezone"])
    for k in _SCALARS:
        if cfg.get(k) is not None:
            try:
                out[k] = int(cfg[k])
            except (TypeError, ValueError):
                pass
    if isinstance(cfg.get("dayoff_weekdays"), list):
        out["dayoff_weekdays"] = [str(d).strip().lower() for d in cfg["dayoff_weekdays"]
                                  if str(d).strip().lower() in _WEEKDAYS]
    if cfg.get("dayoff_chance") is not None:
        try:
            out["dayoff_chance"] = max(0.0, min(1.0, float(cfg["dayoff_chance"])))
        except (TypeError, ValueError):
            pass
    if isinstance(cfg.get("pause_windows"), list):
        pw = []
        for w in cfg["pause_windows"]:
            if not isinstance(w, dict):
                continue
            start = str(w.get("start", "")).strip()
            end = str(w.get("end", "")).strip()
            if not start or not end:
                continue
            try:
                jit = int(w.get("jitter_minutes", 0) or 0)
            except (TypeError, ValueError):
                jit = 0
            pw.append({"start": start, "end": end, "jitter_minutes": jit,
                       "label": str(w.get("label") or "pause")})
        out["pause_windows"] = pw
    if isinstance(cfg.get("weekend"), dict):
        we = {}
        for k in _SCALARS:
            if cfg["weekend"].get(k) is not None:
                try:
                    we[k] = int(cfg["weekend"][k])
                except (TypeError, ValueError):
                    pass
        out["weekend"] = we
    return out


def _q(s) -> str:
    """Quote a string for TOML."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_toml(cfg: dict | None) -> str:
    """Render the config as a [schedule] TOML block the worker loader accepts.
    Bare keys first, then [schedule.weekend], then [[schedule.pause_windows]]
    (valid TOML ordering)."""
    c = merge_defaults(cfg)
    L = ["[schedule]"]
    L.append("enabled = " + ("true" if c["enabled"] else "false"))
    L.append("timezone = " + _q(c["timezone"]))
    for k in _SCALARS:
        L.append(f"{k} = {int(c[k])}")
    days = ", ".join(_q(d) for d in c["dayoff_weekdays"])
    L.append(f"dayoff_weekdays = [{days}]")
    L.append(f"dayoff_chance = {float(c['dayoff_chance'])}")
    if c["weekend"]:
        L.append("")
        L.append("[schedule.weekend]")
        for k, v in c["weekend"].items():
            L.append(f"{k} = {int(v)}")
    for w in c["pause_windows"]:
        L.append("")
        L.append("[[schedule.pause_windows]]")
        L.append("start = " + _q(w["start"]))
        L.append("end = " + _q(w["end"]))
        L.append(f"jitter_minutes = {int(w['jitter_minutes'])}")
        L.append("label = " + _q(w["label"]))
    return "\n".join(L) + "\n"
