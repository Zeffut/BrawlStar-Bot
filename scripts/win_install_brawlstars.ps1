# Download and install Brawl Stars into BlueStacks via ADB.
# Reads the .xapk (split-APK bundle) from our VPS, unzips, then
# `adb install-multiple` to push every split at once.

$ErrorActionPreference = "Stop"
$XAPK_URL = "https://brawlpanel.zeffut.fr/brawlstars.xapk"
$ADB      = "C:\platform-tools\adb.exe"
$ADB_HOST = "127.0.0.1:5555"
$WORK     = "C:\Users\Dev\bs-install"
$XAPK     = "$WORK\brawlstars.xapk"
$EXTRACT  = "$WORK\extract"

New-Item -ItemType Directory -Force -Path $WORK | Out-Null

Write-Host "[1/4] Downloading $XAPK_URL ..."
if (-not (Test-Path $XAPK) -or (Get-Item $XAPK).Length -lt 1000000000) {
    Remove-Item $XAPK -ErrorAction SilentlyContinue
    # Stream download with progress (~1.6 GB)
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $XAPK_URL -OutFile $XAPK -UseBasicParsing
}
Write-Host ("  done ({0:N0} bytes)" -f (Get-Item $XAPK).Length)

Write-Host "[2/4] Extracting splits..."
if (Test-Path $EXTRACT) { Remove-Item -Recurse -Force $EXTRACT }
New-Item -ItemType Directory -Force -Path $EXTRACT | Out-Null
# .xapk == zip; use .NET ZipFile directly (Expand-Archive rejects the
# .xapk extension even though the file IS a zip).
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($XAPK, $EXTRACT)

$splits = Get-ChildItem -Path $EXTRACT -Filter "*.apk"
Write-Host "  found $($splits.Count) split APKs"

Write-Host "[3/4] adb install-multiple..."
& $ADB connect $ADB_HOST | Out-Null
Start-Sleep -Seconds 1
$args = @("-s", $ADB_HOST, "install-multiple", "-r", "-d") + ($splits.FullName)
$out = & $ADB @args 2>&1
Write-Host $out

if ($LASTEXITCODE -ne 0) {
    Write-Host "INSTALL FAILED" -ForegroundColor Red
    exit 1
}

Write-Host "[4/4] Cleanup..."
Remove-Item -Recurse -Force $EXTRACT
# Keep the .xapk for the next install (re-runs are idempotent)
Write-Host "OK" -ForegroundColor Green
