# firstlogon.ps1 - runs once, elevated, at the first (automatic) logon.
#
# Installs the UTM guest tools (VirtIO drivers, including the network driver,
# QEMU guest agent, SPICE agent), makes automatic logon permanent, installs and
# starts OpenSSH Server and authorizes the host's SSH key for administrators.
# From then on the host drives the rest of the setup over SSH.
#
# settings.json next to this script holds the account name and password.

$ErrorActionPreference = 'Stop'
$media = $PSScriptRoot
Start-Transcript -Path C:\utmapp-bot\firstlogon.log -Append | Out-Null
$settings = Get-Content (Join-Path $media 'settings.json') -Raw | ConvertFrom-Json

Write-Host '=== Automatic logon'
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $winlogon -Name AutoAdminLogon -Value '1'
Set-ItemProperty $winlogon -Name DefaultUserName -Value $settings.user
Set-ItemProperty $winlogon -Name DefaultPassword -Value $settings.password
Set-ItemProperty $winlogon -Name DefaultDomainName -Value ''
Remove-ItemProperty $winlogon -Name AutoLogonCount -ErrorAction SilentlyContinue
Set-LocalUser -Name $settings.user -PasswordNeverExpires $true
# Windows Hello-only sign-in would block password autologon.
Set-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' -Name DevicePasswordLessBuildVersion -Value 0 -Type DWord

Write-Host '=== Firewall'
Set-NetFirewallProfile -All -Enabled False

Write-Host '=== UTM guest tools'
# on the guest tools drive UTM attached (Install Windows Guest Tools)
$tools = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Get-ChildItem $_.Root -Filter 'utm-guest-tools*.exe' -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $tools) { throw 'no utm-guest-tools*.exe on any drive: UTM did not attach the guest tools' }
$p = Start-Process $tools.FullName -ArgumentList '/S' -Wait -PassThru
Write-Host "guest tools exit code $($p.ExitCode)"

Write-Host '=== Waiting for the network'
$deadline = (Get-Date).AddMinutes(15)
while (-not (Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue)) {
    if ((Get-Date) -gt $deadline) { throw 'no default route after 15 minutes' }
    Start-Sleep 5
}
Get-NetIPAddress -AddressFamily IPv4 | Format-Table InterfaceAlias, IPAddress -AutoSize | Out-String | Write-Host
# The host-only (kdnet) network hands out an on-link default route with the
# same metric as the shared network's; keep internet traffic on the shared one.
Start-Sleep 10
Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | Where-Object NextHop -eq '0.0.0.0' |
    ForEach-Object { Set-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -InterfaceMetric 500 }

Write-Host '=== OpenSSH Server'
# A Feature on Demand: it is downloaded from Windows Update, so retry while
# the network settles.
for ($i = 1; ; $i++) {
    try {
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
        if ((Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0).State -eq 'Installed') { break }
    } catch {
        Write-Host "attempt $i failed: $_"
    }
    if ($i -ge 20) { throw 'could not install OpenSSH Server' }
    Start-Sleep 30
}
$keys = 'C:\ProgramData\ssh\administrators_authorized_keys'
New-Item -ItemType Directory -Force C:\ProgramData\ssh | Out-Null
Copy-Item (Join-Path $media 'host_authorized_keys') $keys -Force
icacls.exe $keys /inheritance:r /grant 'SYSTEM:F' /grant '*S-1-5-32-544:F' | Out-Null
Set-Service sshd -StartupType Automatic
Start-Service sshd

Write-Host '=== Done'
New-Item -ItemType File -Force C:\utmapp-bot\firstlogon.done | Out-Null
Stop-Transcript | Out-Null
