# BrawlStar-Bot — Windows VM boot-check script
#
# Runs at every login after BlueStacks startup. Verifies the stack
# is healthy and either auto-fixes or alerts via Telegram if it can't.
#
# Checklist (in order):
#   1. BlueStacks process is running (if not → start it)
#   2. ADB server is up + can connect to BlueStacks (127.0.0.1:5555)
#   3. Brawl Stars APK is installed in BlueStacks
#   4. Launch Brawl Stars
#   5. Start the Python bot (telegram_main.py)
#
# Steps that need manual intervention (first-time Supercell ID login,
# APK install) trigger a Telegram alert so you know to hop on VNC.

$ErrorActionPreference = "Continue"
$REPO       = "C:\Users\Dev\BrawlStar-Bot"
$ADB        = "C:\platform-tools\adb.exe"
$BSTACKS    = "C:\Program Files\BlueStacks_nxt\HD-Player.exe"
$ADB_HOST   = "127.0.0.1:5555"
$BS_PACKAGE = "com.supercell.brawlstars"
$LOG        = "$REPO\logs\bootcheck.log"

New-Item -ItemType Directory -Force -Path (Split-Path $LOG) | Out-Null
function Log($m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m"
    Write-Host $line
    Add-Content -Path $LOG -Value $line
}

function TelegramAlert($text) {
    try {
        $cfg = (Get-Content "$REPO\cfg\telegram.toml" | ConvertFrom-StringData -ErrorAction SilentlyContinue)
        # The TOML has values like bot_token = "xxx" — strip quotes manually
        $token = (Get-Content "$REPO\cfg\telegram.toml" | Select-String 'bot_token').ToString().Split('=')[1].Trim().Trim('"')
        $chat  = (Get-Content "$REPO\cfg\telegram.toml" | Select-String 'chat_id').ToString().Split('=')[1].Trim()
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post `
            -Body @{ chat_id = $chat; text = "🤖 BOOTCHECK [HP-VM]: $text" } -TimeoutSec 5 | Out-Null
    } catch {
        Log "TelegramAlert failed: $_"
    }
}

# ---- 1. BlueStacks running? --------------------------------------
Log "=== bootcheck start ==="
$bs = Get-Process HD-Player -ErrorAction SilentlyContinue
if (-not $bs) {
    Log "BlueStacks not running — starting"
    Start-Process $BSTACKS
    Start-Sleep -Seconds 30
} else {
    Log "BlueStacks already running (PID $($bs.Id))"
}

# ---- 2. Wait for ADB port (up to 3 min) --------------------------
Log "Waiting for BlueStacks ADB on $ADB_HOST"
$adbReady = $false
for ($i = 0; $i -lt 36; $i++) {
    try {
        $sock = New-Object Net.Sockets.TcpClient
        $sock.Connect("127.0.0.1", 5555)
        if ($sock.Connected) { $adbReady = $true; $sock.Close(); break }
    } catch {}
    Start-Sleep -Seconds 5
}
if (-not $adbReady) {
    Log "ADB port never opened"
    TelegramAlert "ADB port 5555 not opening — BlueStacks may be stuck"
    exit 1
}
Log "ADB port up"

& $ADB connect $ADB_HOST | Out-Null
Start-Sleep 2
$devices = & $ADB devices | Out-String
Log "adb devices:`n$devices"
if ($devices -notmatch "$ADB_HOST\s+device") {
    Log "ADB device not authorized/online"
    TelegramAlert "ADB cannot connect to BlueStacks"
    exit 1
}

# ---- 3. Brawl Stars installed? -----------------------------------
$pkgs = & $ADB -s $ADB_HOST shell pm list packages 2>&1
if ($pkgs -notmatch $BS_PACKAGE) {
    Log "Brawl Stars NOT installed — manual install required"
    TelegramAlert "Brawl Stars n'est pas installé sur BlueStacks. Connecte-toi en VNC pour l'installer (APKMirror ou Aurora Store, voir Notion)."
    exit 2
}
Log "Brawl Stars installed"

# ---- 4. Launch Brawl Stars ---------------------------------------
$running = & $ADB -s $ADB_HOST shell pidof $BS_PACKAGE 2>&1
if (-not $running -or $running -match "Error") {
    Log "Launching Brawl Stars"
    & $ADB -s $ADB_HOST shell monkey -p $BS_PACKAGE -c android.intent.category.LAUNCHER 1 | Out-Null
    Start-Sleep -Seconds 8
} else {
    Log "Brawl Stars already running (PID $running)"
}

# ---- 5. Start the Python bot -------------------------------------
$botRunning = Get-Process -Name python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "*\BrawlStar-Bot\venv\*" }
if (-not $botRunning) {
    Log "Starting telegram_main.py"
    $py = "$REPO\venv\Scripts\python.exe"
    Start-Process -FilePath $py -ArgumentList "telegram_main.py" `
        -WorkingDirectory $REPO -WindowStyle Hidden
} else {
    Log "Bot already running (PID $($botRunning.Id))"
}

Log "=== bootcheck done ==="
TelegramAlert "Bootcheck OK — BlueStacks + BS + bot tous opérationnels"
