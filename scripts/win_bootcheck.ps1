# BrawlStar-Bot — Windows VM boot-check
# Auto-launches BlueStacks, Brawl Stars, the Python bot. Sends a
# Telegram alert when a manual step is required.

$ErrorActionPreference = "Continue"
$REPO       = "C:\Users\Dev\BrawlStar-Bot"
$ADB        = "C:\platform-tools\adb.exe"
$BSTACKS    = "C:\Program Files\BlueStacks_nxt\HD-Player.exe"
$ADB_HOST   = "127.0.0.1:5555"
$BS_PACKAGE = "com.supercell.brawlstars"
$LOG_DIR    = "$REPO\logs"
$LOG        = "$LOG_DIR\bootcheck.log"

New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

function Log($m) {
    $line = (Get-Date -Format 'HH:mm:ss') + " $m"
    Write-Host $line
    Add-Content -Path $LOG -Value $line
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
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/sendMessage" `
            -Method Post -Body $body -TimeoutSec 5 | Out-Null
    } catch {
        Log "TelegramAlert error: $_"
    }
}

Log "=== bootcheck start ==="

# 1. BlueStacks process
$bs = Get-Process HD-Player -ErrorAction SilentlyContinue
if (-not $bs) {
    Log "Starting BlueStacks"
    Start-Process -FilePath $BSTACKS
    Start-Sleep -Seconds 30
} else {
    Log "BlueStacks already running"
}

# 2. ADB port wait
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
Log "devices:`n$devices"

# 3. Brawl Stars installed
$pkgs = (& $ADB -s $ADB_HOST shell pm list packages) -join "`n"
if ($pkgs -notmatch [regex]::Escape($BS_PACKAGE)) {
    Log "Brawl Stars not installed"
    TelegramAlert "Brawl Stars pas installe — VNC requis pour APK install"
    exit 2
}
Log "Brawl Stars installed"

# 4. Launch Brawl Stars (idempotent)
& $ADB -s $ADB_HOST shell monkey -p $BS_PACKAGE -c android.intent.category.LAUNCHER 1 | Out-Null
Log "Brawl Stars launch sent"

# 5. Python bot
$bot = Get-Process python -ErrorAction SilentlyContinue
if (-not $bot) {
    Log "Starting telegram_main.py"
    Start-Process -FilePath "$REPO\venv\Scripts\python.exe" `
        -ArgumentList "telegram_main.py" -WorkingDirectory $REPO -WindowStyle Hidden
} else {
    Log "Bot already running"
}

Log "=== bootcheck done ==="
TelegramAlert "Bootcheck OK"
