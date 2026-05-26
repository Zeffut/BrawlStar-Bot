# BrawlStar-Bot — Windows VM provisioning script
#
# Run AS ADMINISTRATOR in PowerShell on the VM. It installs:
#   - OpenSSH Server (so the Mac can SSH in via the HP→VM tunnel)
#   - Git, Python 3.12 (via winget)
#   - ADB platform-tools + scrcpy (manual download)
#   - The bot repo + venv + dependencies
#   - cfg/telegram.toml + cfg/cloud.toml (you fill in the secrets after)
#
# BlueStacks must still be installed manually (GUI installer; can't be
# silenced reliably). Once Windows + this script are done, install
# BlueStacks via the official .exe.

$ErrorActionPreference = "Stop"
$REPO_URL    = "https://github.com/Zeffut/BrawlStar-Bot.git"
$REPO_DIR    = "C:\BrawlStar-Bot"
$ADB_DIR     = "C:\platform-tools"
$SCRCPY_DIR  = "C:\scrcpy"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------------------------------------------------------------- SSH
Step "Enable OpenSSH Server"
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction SilentlyContinue
Start-Service sshd
Set-Service -Name sshd -StartupType "Automatic"
New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server" -Enabled True `
    -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -ErrorAction SilentlyContinue
Write-Host "SSH listening on port 22"

# ----------------------------------------------------------- Git + Py
Step "Install Git + Python 3.12 via winget"
winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements

# Refresh PATH so the just-installed binaries are visible in this session
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path","User")

# -------------------------------------------------------------- ADB
Step "Install ADB platform-tools"
if (-not (Test-Path "$ADB_DIR\adb.exe")) {
    Invoke-WebRequest -Uri "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" `
        -OutFile "$env:TEMP\pt.zip"
    Expand-Archive -Path "$env:TEMP\pt.zip" -DestinationPath "C:\" -Force
    Remove-Item "$env:TEMP\pt.zip"
}
# Add to system PATH (idempotent)
$mp = [Environment]::GetEnvironmentVariable("Path","Machine")
if ($mp -notlike "*$ADB_DIR*") {
    [Environment]::SetEnvironmentVariable("Path", "$mp;$ADB_DIR", "Machine")
}

# ------------------------------------------------------------ scrcpy
Step "Install scrcpy"
if (-not (Test-Path "$SCRCPY_DIR\scrcpy.exe")) {
    $latest = Invoke-RestMethod -Uri "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
    $asset = $latest.assets | Where-Object { $_.name -like "scrcpy-win64-*.zip" } | Select-Object -First 1
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "$env:TEMP\scrcpy.zip"
    Expand-Archive -Path "$env:TEMP\scrcpy.zip" -DestinationPath "$env:TEMP\scrcpy-extract" -Force
    $inner = Get-ChildItem "$env:TEMP\scrcpy-extract" -Directory | Select-Object -First 1
    Move-Item $inner.FullName $SCRCPY_DIR
    Remove-Item "$env:TEMP\scrcpy.zip", "$env:TEMP\scrcpy-extract" -Recurse -ErrorAction SilentlyContinue
}
$mp = [Environment]::GetEnvironmentVariable("Path","Machine")
if ($mp -notlike "*$SCRCPY_DIR*") {
    [Environment]::SetEnvironmentVariable("Path", "$mp;$SCRCPY_DIR", "Machine")
}

# Refresh PATH again
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path","User")

# ----------------------------------------------------------- Clone
Step "Clone bot repo"
if (-not (Test-Path $REPO_DIR)) {
    git clone $REPO_URL $REPO_DIR
} else {
    Push-Location $REPO_DIR
    git pull
    Pop-Location
}

# -------------------------------------------------------------- venv
Step "Create venv + install dependencies"
Push-Location $REPO_DIR
python -m venv venv
& ".\venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\venv\Scripts\python.exe" -m pip install -r requirements.txt
Pop-Location

# --------------------------------------------------------- cfg files
Step "Drop config templates"
$telegramCfg = @"
bot_token = "REPLACE_ME"
chat_id = 0
poll_timeout_s = 25
"@
$cloudCfg = @"
enabled = true
url = "https://brawlpanel.zeffut.fr"
token = "REPLACE_ME"
instance_id = "hp-vm-win11"
name = "HP VM (Windows)"
"@
$telegramPath = "$REPO_DIR\cfg\telegram.toml"
$cloudPath    = "$REPO_DIR\cfg\cloud.toml"
if (-not (Test-Path $telegramPath)) { $telegramCfg | Out-File -Encoding utf8 $telegramPath }
$cloudCfg | Out-File -Encoding utf8 $cloudPath

Write-Host "`n=== ALL DONE ===" -ForegroundColor Green
Write-Host "Next steps (manual):"
Write-Host "  1. Edit $cloudPath  → paste the real CLOUD_AUTH_TOKEN"
Write-Host "  2. Edit $telegramPath  → paste your bot_token + chat_id (or skip if you don't want Telegram)"
Write-Host "  3. Install BlueStacks manually (download .exe from bluestacks.com)"
Write-Host "  4. Reboot or log out so PATH changes propagate to new shells"
Write-Host "  5. To start the bot:"
Write-Host "       cd $REPO_DIR"
Write-Host "       .\venv\Scripts\python.exe telegram_main.py"
