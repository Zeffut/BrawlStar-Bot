# Set up auto-login for the Dev account (no manual login at boot).
# Run AS ADMINISTRATOR.
$k = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty $k AutoAdminLogon "1"
Set-ItemProperty $k DefaultUserName "Dev"
Set-ItemProperty $k DefaultPassword "9464"
Write-Host "Auto-login configured. Rebooting in 5s..."
Start-Sleep 5
Restart-Computer -Force
