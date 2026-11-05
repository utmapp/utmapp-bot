@echo off
rem ===========================================================================
rem  specialize.cmd - runs as SYSTEM in the specialize pass of Windows Setup.
rem
rem  usage: specialize.cmd <media-dir>
rem
rem  Copies the configuration media to C:\utmapp-bot\media for firstlogon.ps1
rem  and applies the settings that have to be in place before the first logon,
rem  such as the firewall, which must be off for the host to reach SSH.  The
rem  Defender policies keep it quiet until setup-dev.ps1 -Step defender turns
rem  its services off: their registry keys cannot be written while Defender
rem  runs, not even here.
rem ===========================================================================

if not exist C:\utmapp-bot mkdir C:\utmapp-bot
call :main "%~1" > C:\utmapp-bot\specialize.log 2>&1
exit /b 0

:main
echo === Copying %~1 to C:\utmapp-bot\media
robocopy "%~1" C:\utmapp-bot\media /E /NFL /NDL /NJH /NP

echo === UAC
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v EnableLUA /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v ConsentPromptBehaviorAdmin /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v PromptOnSecureDesktop /t REG_DWORD /d 0 /f

echo === Microsoft Defender
set DP=HKLM\SOFTWARE\Policies\Microsoft\Windows Defender
reg add "%DP%" /v DisableAntiSpyware /t REG_DWORD /d 1 /f
reg add "%DP%" /v DisableRoutinelyTakingAction /t REG_DWORD /d 1 /f
reg add "%DP%" /v ServiceKeepAlive /t REG_DWORD /d 0 /f
reg add "%DP%\Real-Time Protection" /v DisableRealtimeMonitoring /t REG_DWORD /d 1 /f
reg add "%DP%\Real-Time Protection" /v DisableBehaviorMonitoring /t REG_DWORD /d 1 /f
reg add "%DP%\Real-Time Protection" /v DisableOnAccessProtection /t REG_DWORD /d 1 /f
reg add "%DP%\Real-Time Protection" /v DisableScanOnRealtimeEnable /t REG_DWORD /d 1 /f
reg add "%DP%\Real-Time Protection" /v DisableIOAVProtection /t REG_DWORD /d 1 /f
reg add "%DP%\Spynet" /v SpynetReporting /t REG_DWORD /d 0 /f
reg add "%DP%\Spynet" /v SubmitSamplesConsent /t REG_DWORD /d 2 /f
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows Defender Security Center\Notifications" /v DisableNotifications /t REG_DWORD /d 1 /f
reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" /v SecurityHealth /f

echo === SmartScreen, Smart App Control, apps from anywhere
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\System" /v EnableSmartScreen /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer" /v SmartScreenEnabled /t REG_SZ /d Off /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer" /v AicEnabled /t REG_SZ /d Anywhere /f
reg add "HKLM\SOFTWARE\Policies\Microsoft\Edge" /v SmartScreenEnabled /t REG_DWORD /d 0 /f
reg add "HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy" /v VerifiedAndReputablePolicyState /t REG_DWORD /d 0 /f

echo === No automatic device encryption
rem BitLocker bound to the TPM would demand the recovery key once a test VM's
rem Secure Boot state changes.
reg add "HKLM\SYSTEM\CurrentControlSet\Control\BitLocker" /v PreventDeviceEncryption /t REG_DWORD /d 1 /f

echo === Firewall
for %%p in (DomainProfile StandardProfile PublicProfile) do (
  reg add "HKLM\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\%%p" /v EnableFirewall /t REG_DWORD /d 0 /f
)

echo === Done
goto :eof
