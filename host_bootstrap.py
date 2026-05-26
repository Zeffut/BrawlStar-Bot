"""Host-side environment bootstrap.

Runs at bot startup BEFORE the main loop and the Telegram bot kick in.
Responsibilities (Windows host case):
  - Ensure BlueStacks is running. Launch it if not.
  - Wait for the BlueStacks ADB endpoint to come up.
  - Connect ADB and verify the device.
  - Ensure the target game package (Brawl Stars) is installed.
    If missing, download the .xapk from the configured URL and install
    via `adb install-multiple`.
  - Launch the game.

Each step pushes a cloud_sync event and sends an alert (if configured)
so that failures surface on the panel and in Telegram automatically.

On non-Windows hosts, the function is a quick no-op (Mac users
already have BlueStacks open, Linux users use a USB device).
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import alerts
import cloud_sync

log = logging.getLogger(__name__)

# ----------------------------------------------------------- defaults

BS_PACKAGE      = "com.supercell.brawlstars"
ADB_HOST        = "127.0.0.1:5555"
XAPK_URL        = os.environ.get(
    "BRAWLSTARS_XAPK_URL",
    "https://brawlpanel.zeffut.fr/brawlstars.xapk",
)
# Path resolution — try common BlueStacks install dirs.
_BS_CANDIDATES = [
    r"C:\Program Files\BlueStacks_nxt\HD-Player.exe",
    r"C:\Program Files (x86)\BlueStacks_nxt\HD-Player.exe",
    r"C:\Program Files\BlueStacks\HD-Player.exe",
]
_ADB_CANDIDATES = [
    r"C:\platform-tools\adb.exe",
    shutil.which("adb"),
]


def _find_bluestacks() -> str | None:
    for p in _BS_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _find_adb() -> str | None:
    for p in _ADB_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def _report(event: str, msg: str, level: str = "info") -> None:
    """Log + cloud event + optional Telegram alert."""
    getattr(log, level)(msg)
    try:
        cloud_sync.event(event, {"msg": msg})
    except Exception:
        log.exception("cloud_sync.event failed")
    # Re-use the cycle_started_no_ocr template for free-form alerts; the
    # bootstrap doesn't have its own alert event type yet so we just log
    # to cloud + console for now.


def _alert(text: str) -> None:
    """Send a one-off Telegram message via the alerts plumbing.

    We don't go through alerts.format_alert because that requires a
    pre-defined template. Instead, send directly via the bot token if
    we can read it from cfg.
    """
    try:
        import tomllib
        cfg_path = Path(__file__).resolve().parent / "cfg" / "telegram.toml"
        with cfg_path.open("rb") as f:
            cfg = tomllib.load(f)
        import requests
        requests.post(
            f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
            data={"chat_id": cfg["chat_id"], "text": f"[BOOT] {text}"},
            timeout=5,
        )
    except Exception:
        log.exception("alert post failed")


# ------------------------------------------------------------- steps


def _is_process_running(name: str) -> bool:
    """Cheap psutil-free process check via tasklist (Windows)."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", errors="replace")
        return name.lower() in out.lower()
    except Exception:
        return False


def _wait_port(host: str, port: int, timeout_s: float = 180) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(2)
    return False


def _adb(adb: str, *args, timeout: float = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [adb, *args], capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


def _ensure_bluestacks(bs_path: str) -> bool:
    if _is_process_running("HD-Player.exe"):
        log.info("BlueStacks already running")
        return True
    log.info("starting BlueStacks: %s", bs_path)
    subprocess.Popen([bs_path], creationflags=0x00000008)  # DETACHED_PROCESS
    # Give it 30 s to start the GUI, then the port wait will block until ADB is up.
    time.sleep(30)
    return _is_process_running("HD-Player.exe")


def _ensure_adb(adb: str) -> bool:
    code, out = _adb(adb, "connect", ADB_HOST, timeout=10)
    log.info("adb connect → %s", out.strip())
    code, out = _adb(adb, "devices", timeout=5)
    log.info("adb devices →\n%s", out)
    return ADB_HOST in out and "device" in out


def _is_package_installed(adb: str, package: str) -> bool:
    code, out = _adb(adb, "-s", ADB_HOST, "shell", "pm", "list", "packages", timeout=15)
    return package in out


def _download_xapk(url: str, dst: Path) -> bool:
    if dst.exists() and dst.stat().st_size > 1_000_000_000:
        log.info("xapk already cached at %s (%.1f GB)",
                 dst, dst.stat().st_size / 1e9)
        return True
    log.info("downloading %s → %s", url, dst)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp, open(dst, "wb") as out:
            shutil.copyfileobj(resp, out, length=1 << 20)
        log.info("downloaded %.1f GB", dst.stat().st_size / 1e9)
        return True
    except Exception:
        log.exception("xapk download failed")
        return False


def _install_brawlstars(adb: str) -> bool:
    work = Path(os.environ.get("TEMP", "/tmp")) / "bs-install"
    work.mkdir(parents=True, exist_ok=True)
    xapk = work / "brawlstars.xapk"
    if not _download_xapk(XAPK_URL, xapk):
        return False
    extract = work / "extract"
    if extract.exists():
        shutil.rmtree(extract)
    extract.mkdir()
    log.info("extracting xapk…")
    with zipfile.ZipFile(xapk) as zf:
        zf.extractall(extract)
    apks = sorted(extract.glob("*.apk"))
    log.info("found %d splits, installing…", len(apks))
    args = [adb, "-s", ADB_HOST, "install-multiple", "-r", "-d", "-g",
            *[str(p) for p in apks]]
    proc = subprocess.run(args, capture_output=True, text=True,
                          timeout=600, errors="replace")
    log.info("install stdout:\n%s", proc.stdout)
    log.info("install stderr:\n%s", proc.stderr)
    return proc.returncode == 0 and "Success" in (proc.stdout + proc.stderr)


def _launch_brawlstars(bs_path: str) -> bool:
    # BlueStacks native launch command — more reliable than `adb shell monkey`.
    log.info("launching Brawl Stars via HD-Player")
    proc = subprocess.Popen([
        bs_path, "--instance", "Nougat32",
        "--cmd", "launchApp", "--package", BS_PACKAGE,
    ])
    try:
        proc.wait(timeout=60)
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        return False


# --------------------------------------------------------------- public


def bootstrap_host() -> bool:
    """Run all host-side checks; return True if the host is ready for the
    bot to start playing."""
    system = platform.system()
    if system != "Windows":
        log.info("host_bootstrap: %s detected — skipping Windows-only checks", system)
        return True

    bs = _find_bluestacks()
    adb = _find_adb()
    if not bs or not adb:
        msg = f"Cannot find BlueStacks ({bs}) or adb ({adb})"
        _report("bootstrap_missing_binary", msg, "error")
        _alert(msg)
        return False

    # 1. BlueStacks process
    if not _ensure_bluestacks(bs):
        _report("bootstrap_bluestacks_fail", "BlueStacks could not be started", "error")
        _alert("BlueStacks could not be started")
        return False

    # 2. ADB endpoint
    if not _wait_port("127.0.0.1", 5555, timeout_s=180):
        _report("bootstrap_adb_port_timeout", "ADB port 5555 never opened", "error")
        _alert("ADB port 5555 never opened on BlueStacks")
        return False
    if not _ensure_adb(adb):
        _report("bootstrap_adb_connect_fail", "Could not connect ADB to BlueStacks", "error")
        _alert("Could not connect ADB to BlueStacks")
        return False

    # 3. Brawl Stars installed?
    if not _is_package_installed(adb, BS_PACKAGE):
        log.info("Brawl Stars not installed — running auto-installer")
        _report("bootstrap_brawlstars_installing", "Installing Brawl Stars APK…")
        _alert("Auto-installing Brawl Stars (~1.6 GB download)")
        if not _install_brawlstars(adb):
            _report("bootstrap_brawlstars_install_fail",
                    "Brawl Stars auto-install failed", "error")
            _alert("Brawl Stars auto-install FAILED — check logs")
            return False
        _report("bootstrap_brawlstars_installed", "Brawl Stars installed")
        _alert("Brawl Stars installed")
    else:
        log.info("Brawl Stars already installed")

    # 4. Launch
    if not _launch_brawlstars(bs):
        log.warning("Brawl Stars launch returned non-zero, continuing anyway")

    _report("bootstrap_ready", "Host bootstrap OK")
    return True
