# BrawlStar-Bot Windows VM boot-check.
# Launches BlueStacks, Brawl Stars, and the Python bot at every login.
# Sends a Telegram alert if a manual step is needed.

$ErrorActionPreference = "Continue"
$REPO       = "C:\Users\Dev\BrawlStar-Bot"
$ADB        = "C:\platform-tools\adb.exe"
$BSTACKS    = "C:\Program Files\BlueStacks_nxt\HD-Player.exe"
$ADB_HOST   = "127.0.0.1:5555"
$BS_PACKAGE = "com.supercell.brawlstars"
$LOG_DIR    = "$REPO\logs"
$LOG        = "$LOG_DIR\bootcheck.log"

New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

# Single-instance mutex: if another bootcheck is running, exit silently.
$lockName = "Global\BrawlBotBootcheck"
$mutex = New-Object System.Threading.Mutex($false, $lockName)
if (-not $mutex.WaitOne(0)) {
    Write-Host "Another bootcheck instance is running — exiting"
    exit 0
}

function Log($m) {
    $line = (Get-Date -Format 'HH:mm:ss') + " $m"
    Write-Host $line
    # Open file with shared access so concurrent reads (e.g. tail) work
    [System.IO.File]::AppendAllText($LOG, $line + "`r`n")
}

function TelegramAlert($text) {
    try {
        $tomlLines = Get-Content "$REPO\cfg\telegram.toml"
        $tokenLine = $tomlLines | Select-String '^bot_token'
        $chatLine  = $tomlLines | Select-String '^chat_id'
        if (-not $tokenLine -or -not $chatLine) { return }
        $token = $tokenLine.Line.Split('=')[1].Trim().Trim('"')
        $chat  = $chatLine.Line.Split('=')[1].Trim()
        $body  = @{ chat_id = $chat; text = "[HP-VM] $text" }
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" -Method Post -Body $body -TimeoutSec 5 | Out-Null
    } catch {
        Log "TelegramAlert error: $_"
    }
}

Log "=== bootcheck start ==="

$bs = Get-Process HD-Player -ErrorAction SilentlyContinue
if (-not $bs) {
    Log "Starting BlueStacks"
    Start-Process -FilePath $BSTACKS
    Start-Sleep -Seconds 30
} else {
    Log "BlueStacks already running"
}

Log "Waiting for ADB on $ADB_HOST"
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
    Log "ADB never came up"
    TelegramAlert "ADB port 5555 not opening on BlueStacks"
    exit 1
}

& $ADB connect $ADB_HOST | Out-Null
Start-Sleep -Seconds 2
$devices = (& $ADB devices) -join "`n"
Log "adb devices output: $devices"

$pkgs = (& $ADB -s $ADB_HOST shell pm list packages) -join "`n"
$pkgPattern = [regex]::Escape($BS_PACKAGE)
if ($pkgs -notmatch $pkgPattern) {
    Log "Brawl Stars not installed - launching auto-installer"
    TelegramAlert "Auto-installing Brawl Stars (downloads ~1.6 GB)"
    $installScript = "$REPO\scripts\win_install_brawlstars.ps1"
    & powershell -ExecutionPolicy Bypass -File $installScript 2>&1 | Tee-Object -Append $LOG
    if ($LASTEXITCODE -ne 0) {
        Log "Auto-install failed (exit=$LASTEXITCODE)"
        TelegramAlert "Brawl Stars auto-install FAILED - VNC required"
        exit 2
    }
    Log "Auto-install OK"
    TelegramAlert "Brawl Stars installed automatically"
}
Log "Brawl Stars installed"

# Launch Brawl Stars via BlueStacks native command (more reliable than
# `adb shell monkey` which hangs intermittently on BS ADB transport).
$launchProc = Start-Process -FilePath $BSTACKS `
    -ArgumentList "--instance","Nougat32","--cmd","launchApp","--package",$BS_PACKAGE `
    -PassThru -WindowStyle Hidden
if (-not $launchProc.WaitForExit(30000)) {
    Log "Brawl Stars launch timed out — killing"
    $launchProc.Kill()
} else {
    Log "Brawl Stars launch sent (HD-Player launchApp)"
}

$bot = Get-Process python -ErrorAction SilentlyContinue
if (-not $bot) {
    Log "Starting telegram_main.py"
    Start-Process -FilePath "$REPO\venv\Scripts\python.exe" -ArgumentList "telegram_main.py" -WorkingDirectory $REPO -WindowStyle Hidden
} else {
    Log "Bot already running"
}

Log "=== bootcheck done ==="
TelegramAlert "Bootcheck OK"
