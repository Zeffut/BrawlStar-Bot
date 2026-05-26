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
    """Launch BlueStacks if not running. Uses a one-shot scheduled task
    so the GUI lands in the user's Console session (subprocess.Popen
    from a process in session 0 would put it in services session 0,
    invisible to VNC)."""
    if _is_process_running("HD-Player.exe"):
        log.info("BlueStacks already running")
        return True
    log.info("BlueStacks not running — scheduling one-shot launch task")
    # Schedule via PowerShell — Register-ScheduledTask runs as Dev,
    # LogonType=Interactive guarantees user session.
    ps_cmd = (
        '$t = Register-ScheduledTask -TaskName "BotLaunchBS" '
        '-Action (New-ScheduledTaskAction -Execute "' + bs_path + '") '
        '-Trigger (New-ScheduledTaskTrigger -At (Get-Date).AddSeconds(3) -Once) '
        '-Principal (New-ScheduledTaskPrincipal -UserId "Dev" '
        '-RunLevel Limited -LogonType Interactive) -Force; '
        'Start-Sleep 8; '
        'Unregister-ScheduledTask -TaskName "BotLaunchBS" -Confirm:$false'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, timeout=30,
        )
    except Exception:
        log.exception("scheduled-task launch failed")
        return False
    # Wait up to 90 s for HD-Player to actually appear.
    for i in range(18):
        time.sleep(5)
        if _is_process_running("HD-Player.exe"):
            log.info("BlueStacks running (after %d s)", (i + 1) * 5)
            return True
    log.error("BlueStacks never appeared after scheduled-task launch")
    return False


def _ensure_adb(adb: str) -> bool:
    code, out = _adb(adb, "connect", ADB_HOST, timeout=10)
    log.info("adb connect → %s", out.strip())
    code, out = _adb(adb, "devices", timeout=5)
    log.info("adb devices →\n%s", out)
    return ADB_HOST in out and "device" in out


def _is_package_installed(adb: str, package: str) -> bool:
    """Retry a few times — `pm list packages` can fail during BS boot."""
    for attempt in range(5):
        code, out = _adb(adb, "-s", ADB_HOST, "shell", "pm", "list", "packages", timeout=15)
        if code == 0 and "package:" in out:
            return package in out
        log.debug("pm list packages attempt %d failed (code=%d), retrying", attempt + 1, code)
        time.sleep(5)
    log.warning("pm list packages never succeeded; assuming package IS installed to skip auto-install")
    return True  # safer: don't auto-reinstall when state is unknown


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


def _launch_brawlstars(bs_path: str, adb: str) -> bool:
    """Launch the game via `adb shell am start` (the BlueStacks
    --cmd launchApp variant tends to open the App Center instead of
    actually switching to the Android instance)."""
    log.info("launching Brawl Stars via adb am start")
    code, out = _adb(adb, "-s", ADB_HOST, "shell", "am", "start",
                     "-n", f"{BS_PACKAGE}/.GameApp", timeout=30)
    log.info("am start → %s", out.strip())
    return code == 0 and "Error" not in out


# --------------------------------------------------------------- public


def _bootstrap_linux() -> bool:
    """Linux host (Android phone over USB)."""
    import device  # local to avoid circular import
    adb = shutil.which("adb")
    if not adb:
        log.error("adb not found in PATH")
        return False

    # 1. Wait for at least one authorized device
    log.info("waiting for an authorized ADB device…")
    serial = None
    for i in range(30):
        out = subprocess.check_output([adb, "devices"], text=True, errors="replace")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serial = parts[0]
                break
        if serial:
            break
        if "unauthorized" in out:
            log.warning("device shown as 'unauthorized' — accept the popup on the phone")
        time.sleep(2)
    if not serial:
        log.error("no authorized ADB device found")
        _alert("No authorized ADB device on the HP — phone unplugged or popup not accepted")
        return False
    log.info("using device serial=%s", serial)

    # 2. Brawl Stars installed?
    out = subprocess.check_output(
        [adb, "-s", serial, "shell", "pm", "list", "packages"],
        text=True, errors="replace", timeout=10,
    )
    if BS_PACKAGE not in out:
        log.error("Brawl Stars not installed on the connected device")
        _alert("Brawl Stars not installed on this phone — install it manually via Play Store")
        return False

    # 3. Launch Brawl Stars via `am start`
    log.info("launching Brawl Stars")
    code, output = _adb(adb, "-s", serial, "shell", "am", "start",
                        "-n", f"{BS_PACKAGE}/.GameApp", timeout=15)
    log.info("am start → %s", output.strip())
    if code != 0:
        log.warning("am start non-zero, continuing anyway")

    # 4. Dismiss every boot popup until the lobby is visible.
    # Brawl Stars boot: Supercell logo → loading → news / season pass /
    # daily streak / quests popups → lobby.
    # Use the existing close_popup.png template — same X icon on every
    # Brawl Stars popup. Falls back to BACK key if no X found.
    log.info("dismissing boot popups (≤ 90 s)…")
    try:
        from state_finder.main import get_state
        from utils import find_template_center
        from PIL import Image
        import cv2 as _cv2
        import io as _io
        TPL = _cv2.imread("state_finder/images_to_detect/close_popup.png")
        same_state_count = 0
        last_state = None
        for attempt in range(45):
            raw = subprocess.check_output(
                [adb, "-s", serial, "exec-out", "screencap", "-p"],
                timeout=10,
            )
            try:
                img = Image.open(_io.BytesIO(raw))
            except Exception:
                log.warning("screencap parse failed, retrying")
                time.sleep(2); continue
            state = get_state(img)
            log.debug("boot popup loop %d: state=%s", attempt + 1, state)
            if state == "lobby":
                log.info("LOBBY REACHED after %d attempts", attempt + 1)
                _alert("Bot ready — game in lobby")
                _report("bootstrap_ready", "Linux bootstrap OK")
                return True
            # Track stuck state.
            if state == last_state:
                same_state_count += 1
            else:
                same_state_count = 0
                last_state = state
            # In a menu/sub-screen for >=2 iterations -> safe BACK to escape.
            if state in ("shop", "brawler_selection", "popup") and same_state_count >= 2:
                log.info("stuck in state=%s for %d iters -> BACK key",
                         state, same_state_count + 1)
                subprocess.run(
                    [adb, "-s", serial, "shell", "input", "keyevent", "4"],
                    timeout=5,
                )
                time.sleep(1.5)
                continue
            # Find X button via template matching. Tap it if found.
            pos = find_template_center(img, TPL) if TPL is not None else None
            if pos:
                x, y = pos
                log.info("close-X found at (%d,%d), tapping", x, y)
                subprocess.run(
                    [adb, "-s", serial, "shell", "input", "tap", str(int(x)), str(int(y))],
                    timeout=5,
                )
            else:
                log.debug("no X visible — just waiting (safer than blind taps)")
            time.sleep(2.5)
        log.warning("could not reach lobby after 45 attempts")
        _alert("Bot started but game not in lobby — check the phone screen")
    except Exception:
        log.exception("popup dismiss loop crashed")

    _report("bootstrap_ready", "Linux bootstrap OK (lobby unreached)")
    return True


def bootstrap_host() -> bool:
    """Run all host-side checks; return True if the host is ready for the
    bot to start playing."""
    system = platform.system()
    if system == "Linux":
        return _bootstrap_linux()
    if system != "Windows":
        log.info("host_bootstrap: %s detected — skipping platform checks", system)
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
    if not _launch_brawlstars(bs, adb):
        log.warning("Brawl Stars launch returned non-zero, continuing anyway")

    _report("bootstrap_ready", "Host bootstrap OK")
    return True
