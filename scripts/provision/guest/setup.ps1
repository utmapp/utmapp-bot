# setup.ps1 - runs on a dev or test VM over SSH, one step at a time, for
# provision-windows-dev.sh and provision-windows-test.sh.
#
# usage: setup.ps1 -Step <step> [options]
#
# Both:
#   system   UAC, firewall, Defender, SmartScreen, long paths, Explorer, power
#            and the Windows policies of the golden "Windows Development" VM
#   defender turns Microsoft Defender off through a Safe Mode boot; prints
#            "DEFENDER REBOOT" when the host must restart the VM, else
#            "DEFENDER OFF"
#   verify   -Role dev|test: checks what that role needs; prints "VERIFY OK"
#            or "VERIFY FAILED" and fails on the latter
#
# Dev VMs (the build machines and kernel debuggers):
#   kdnet    -Mac MAC -Address IP: static address on the NIC with that MAC,
#            the second one, on the kdnet network
#   tools    7-Zip, Git, CMake, Python, WinFsp and SSHFS-Win
#   sshfs    RSA key for SSHFS-Win (prints "GUEST_PUBKEY <key>" for the host)
#            and the persistent Z: mapping of the host's ~/Documents
#   vs       Visual Studio 2022 Community with the components in vs2022.vsconfig
#   python   meson, mako, packaging and pyyaml; after vs, because pyyaml has no
#            wheel for every Python and architecture and is then compiled
#   wdk      Windows Driver Kit
#
# Test VMs (run the driver under test, debugged over kdnet):
#   gpu      removes the guest tools' VirtIO GPU drivers (viogpudo, viogpu3d)
#            so the GPU is free for the driver under test, and keeps Windows
#            Update from bringing them back; prints "GPU REBOOT" when the host
#            must restart the VM and run it again, else "GPU OFF"
#   debug    -HostIp IP -Port N -Key K: test signing on, kdnet on the e1000
#            NIC (no Windows driver; only kdnet's kd_02_8086.dll drives it)
#            towards the debugger at IP:N.  Needs Secure Boot off.  Rerun
#            after the restart to keep the kdnet adapter's default route off
#            the shared network.
#   content  [-Mark]: VC++ and DirectX (June 2010) runtimes, GravityMark, Unigine Heaven, power and
#            Game DVR settings for benchmarking; with -Mark also 3DMark from
#            downloads\3dmark (registered with downloads\3dmark.key, which is
#            deleted, and seeded with downloads\3dmark\dlc if present).
#            Prints "CONTENT OK" with what is installed.
#
# Every step is idempotent: it skips what is already installed, so a failed
# provisioning run can be resumed from the failed step.

param(
    [Parameter(Mandatory)] [ValidateSet('system', 'defender', 'kdnet', 'tools', 'sshfs', 'vs', 'python', 'wdk', 'gpu', 'debug', 'verify', 'content')] [string] $Step,
    [string] $Downloads = 'C:\utmapp-bot\downloads',
    [string] $HostUser,
    [string] $Mac,
    [string] $Address,
    [string] $HostIp,
    [int] $Port,
    [string] $Key,
    [ValidateSet('dev', 'test')] [string] $Role,
    [string] $KdnetAddress,
    [switch] $Mark
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Set-Reg([string] $Path, [string] $Name, $Value, [string] $Type = 'DWord') {
    if (-not (Test-Path $Path)) { New-Item -Path $Path -Force | Out-Null }
    New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType $Type -Force | Out-Null
}

# Run an installer and fail unless it exits with one of $OkCodes (3010 means
# success, reboot required).
function Install([string] $File, [string[]] $Arguments, [int[]] $OkCodes = @(0, 3010)) {
    $path = Join-Path $Downloads $File
    if (-not (Test-Path $path)) { throw "installer not found: $path" }
    Write-Host "--- $File $Arguments"
    if ($path -like '*.msi') {
        $Arguments = @('/i', "`"$path`"", '/qn', '/norestart') + $Arguments
        $path = 'msiexec.exe'
    }
    $p = Start-Process $path -ArgumentList $Arguments -Wait -PassThru -NoNewWindow
    if ($OkCodes -notcontains $p.ExitCode) { throw "$File failed with exit code $($p.ExitCode)" }
}

function Test-Installed([string] $DisplayNamePattern) {
    $keys = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
            'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    [bool](Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like $DisplayNamePattern })
}

$Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python315-arm64\python.exe'
$VsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$DefenderServices = 'WinDefend', 'WdBoot', 'WdFilter', 'WdNisDrv', 'WdNisSvc', 'Sense', 'MDCoreSvc'
$Debuggers = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Debuggers\arm64'
$KdnicDescription = 'Microsoft Kernel Debug Network Adapter*'

# A network interface's on-link default route (the kdnet network's DHCP hands
# one out) must not compete with the shared network's.
function Set-LowRoutePriority {
    Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | Where-Object NextHop -eq '0.0.0.0' |
        ForEach-Object { Set-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -InterfaceMetric 500 }
}

# The e1000 the test VM's kdnet uses: no Windows driver, so it is identified
# by its PCI IDs (82540EM).
function Get-KdnetPciDevice {
    Get-PnpDevice -PresentOnly | Where-Object InstanceId -like 'PCI\VEN_8086&DEV_100E*' | Select-Object -First 1
}

# The VirtIO GPU (virtio-ramfb-gl), whatever driver it has or has not.
function Get-VirtioGpu {
    Get-PnpDevice -PresentOnly | Where-Object InstanceId -like 'PCI\VEN_1AF4&DEV_1050*'
}

# The guest tools' VirtIO GPU drivers: service name -> INF.
$GpuDrivers = [ordered]@{ VioGpuDod = 'viogpudo.inf'; VioGpu3D = 'viogpu3d.inf' }
$BasicDisplay = 'HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\BasicDisplay'

function Get-GpuPackages {
    Get-WindowsDriver -Online | Where-Object { @($GpuDrivers.Values) -contains (Split-Path $_.OriginalFileName -Leaf) }
}

# What the service control manager says about a (driver) service: Absent,
# Running, Stopped, or Pending (marked for delete, or a record whose registry
# key is gone; only a restart clears those).  Get-Service does not list
# kernel drivers, so ask sc.exe.
function Get-ServiceState([string] $Name) {
    $out = & sc.exe query $Name
    $code = $LASTEXITCODE
    $key = Test-Path "HKLM:\SYSTEM\CurrentControlSet\Services\$Name"
    switch ($code) {
        0 { if (-not $key) { return 'Pending' }; if ($out -match 'RUNNING') { return 'Running' }; return 'Stopped' }
        1060 { if ($key) { return 'Stopped' }; return 'Absent' }   # not known to the SCM
        default { return 'Pending' }                               # 1072 marked for delete, 2 key gone
    }
}

switch ($Step) {

'system' {
    Write-Host '=== UAC'
    $sys = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
    Set-Reg $sys EnableLUA 0
    Set-Reg $sys ConsentPromptBehaviorAdmin 0
    Set-Reg $sys PromptOnSecureDesktop 0

    Write-Host '=== Firewall'
    Set-NetFirewallProfile -All -Enabled False

    Write-Host '=== Network'
    # The host-only (kdnet) network hands out an on-link default route with
    # the same metric as the shared network's; keep internet traffic on the
    # shared network.
    Set-LowRoutePriority

    Write-Host '=== SmartScreen, Smart App Control, apps from anywhere'
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' EnableSmartScreen 0
    Set-Reg 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer' SmartScreenEnabled 'Off' String
    Set-Reg 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer' AicEnabled 'Anywhere' String
    Set-Reg 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' VerifiedAndReputablePolicyState 0

    Write-Host '=== Microsoft Defender'
    # Turned off in the specialize pass; if tamper protection got there first
    # these fail, and verify reports Defender as still running.
    try {
        Set-MpPreference -DisableRealtimeMonitoring $true -DisableBehaviorMonitoring $true `
            -DisableIOAVProtection $true -MAPSReporting Disabled -SubmitSamplesConsent NeverSend `
            -ExclusionPath 'C:\', 'Z:\' -ErrorAction Stop
    } catch {
        Write-Host "Set-MpPreference: $($_.Exception.Message)"
    }

    Write-Host '=== File system'
    Set-Reg 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' LongPathsEnabled 1

    Write-Host '=== Explorer'
    $adv = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced'
    Set-Reg $adv HideFileExt 0
    Set-Reg $adv Hidden 1
    Set-Reg $adv ShowCopilotButton 0
    Set-Reg $adv Start_TrackProgs 0
    Set-Reg $adv Start_IrisRecommendations 0
    Set-Reg $adv Start_AccountNotifications 0
    Set-Reg $adv ShowSyncProviderNotifications 0

    Write-Host '=== Policies'
    # Copilot, Recall and other AI features, consumer content, Game DVR and
    # Edge's first-run noise; as on the golden VM.
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot' TurnOffWindowsCopilot 1
    $ai = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsAI'
    Set-Reg $ai DisableAIDataAnalysis 1
    Set-Reg $ai AllowRecallEnablement 0
    Set-Reg $ai TurnOffSavingSnapshots 1
    Set-Reg $ai DisableClickToDo 1
    Set-Reg 'HKLM:\SOFTWARE\Policies\WindowsNotepad' DisableAIFeatures 1
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableConsumerAccountStateContent 1
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableWindowsConsumerFeatures 1
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\GameDVR' AllowGameDVR 0
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search' AllowCortana 0
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' PublishUserActivities 0
    $edge = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
    foreach ($n in 'HubsSidebarEnabled', 'NewTabPageContentEnabled', 'ShowRecommendationsEnabled',
                   'DefaultBrowserSettingEnabled', 'EdgeShoppingAssistantEnabled', 'UserFeedbackAllowed',
                   'PersonalizationReportingEnabled', 'SmartScreenEnabled') {
        Set-Reg $edge $n 0
    }
    Set-Reg $edge HideFirstRunExperience 1
    # Do not restart for updates while someone is logged on: long builds run
    # in the autologon session.
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoRebootWithLoggedOnUsers 1

    Write-Host '=== Power'
    powercfg.exe /hibernate off
    powercfg.exe /change standby-timeout-ac 0
    powercfg.exe /change monitor-timeout-ac 0
}

'defender' {
    # Defender's WdFilter driver denies every write to its service keys while
    # it runs, even from the specialize pass and as SYSTEM.  It does not run in
    # Safe Mode, so schedule a Safe Mode boot that disables the services: a
    # RunOnce entry starting with "*" also runs in Safe Mode.  The host then
    # restarts the VM; the script leaves Safe Mode and restarts it again.
    $off = $true
    foreach ($s in $DefenderServices) {
        $start = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$s" -ErrorAction SilentlyContinue).Start
        if ($null -ne $start -and $start -ne 4) { $off = $false }
    }
    if ($off) { Write-Host 'DEFENDER OFF'; break }
    $script = 'C:\utmapp-bot\defender-off.cmd'
    $lines = @('@echo off', '(')
    foreach ($s in $DefenderServices) {
        $lines += "  reg add `"HKLM\SYSTEM\CurrentControlSet\Services\$s`" /v Start /t REG_DWORD /d 4 /f"
    }
    $lines += '  bcdedit /deletevalue {current} safeboot', ') > C:\utmapp-bot\defender-off.log 2>&1', 'shutdown /r /t 5'
    Set-Content -Path $script -Value $lines -Encoding ascii
    Set-Reg 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce' '*DefenderOff' "cmd.exe /c $script" String
    bcdedit.exe /set '{current}' safeboot minimal | Out-Null
    if ($LASTEXITCODE) { throw "bcdedit failed with exit code $LASTEXITCODE" }
    Write-Host 'DEFENDER REBOOT'
}

'kdnet' {
    if (-not $Mac -or -not $Address) { throw '-Mac and -Address are required' }
    $nic = Get-NetAdapter | Where-Object { ($_.MacAddress -replace '-', ':') -eq $Mac.ToUpper() }
    if (-not $nic) { throw "no network adapter with MAC address $Mac" }
    if ($nic.Name -ne 'kdnet') { Rename-NetAdapter -Name $nic.Name -NewName kdnet; $nic = Get-NetAdapter -Name kdnet }
    $ip = Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
    if ($ip.IPAddress -eq $Address -and $ip.PrefixOrigin -eq 'Manual') {
        Write-Host "kdnet: $Address already set"
    } else {
        Write-Host "--- kdnet: $Address/24 on $($nic.InterfaceDescription) ($Mac)"
        Set-NetIPInterface -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -Dhcp Disabled
        Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Remove-NetIPAddress -Confirm:$false
        Get-NetRoute -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false
        # No gateway: the kdnet network only connects the VMs and the host.
        New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress $Address -PrefixLength 24 | Out-Null
    }
    Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 | Format-Table InterfaceAlias, IPAddress, PrefixLength, PrefixOrigin -AutoSize | Out-String | Write-Host
}

'gpu' {
    # The guest tools install VirtIO GPU drivers - viogpudo (display only)
    # and, in the 3D guest tools, viogpu3d - with everything else; the test VM
    # gets the driver under test instead.  Prints "GPU REBOOT" when the host
    # must restart the VM and run the step again, "GPU OFF" when done.
    #
    # The order matters (the guest tools' Uninstall-VirtioGpu.cmd explains
    # each point; this does the same without its prompts):
    #  - viogpudo hangs the machine when a loaded instance is removed or
    #    disabled, so a loaded driver is only set not to start, and its
    #    package is deleted after a restart, when nothing is loaded.
    #  - Older installers set DisableBasicDisplayFallback: with no VirtIO GPU
    #    driver the screen would stay black.  Removed first.
    #  - The services go through the SCM (sc delete), never by deleting their
    #    keys: a record the SCM still holds makes the next install of the
    #    driver bind a NULL driver.  A deferred delete needs one more restart.
    $v = Get-ItemProperty $BasicDisplay -Name DisableBasicDisplayFallback -ErrorAction SilentlyContinue
    if ($v) {
        Write-Host '--- re-enabling the basic display fallback'
        Remove-ItemProperty $BasicDisplay -Name DisableBasicDisplayFallback -Force
        $k = Get-Item $BasicDisplay -ErrorAction SilentlyContinue
        if ($k -and $k.ValueCount -eq 0 -and $k.SubKeyCount -eq 0) { Remove-Item $BasicDisplay -Force }
    }
    # Windows Update offers drivers for the VirtIO GPU too.  Not a device ID
    # block: that would also stop the driver under test from installing.
    Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' ExcludeWUDriversInQualityUpdate 1
    Set-Reg 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\DriverSearching' SearchOrderConfig 0

    $loaded = @($GpuDrivers.Keys | Where-Object { (Get-ServiceState $_) -eq 'Running' })
    if ($loaded) {
        foreach ($svc in $loaded) {
            Write-Host "--- $svc is loaded: not starting it from the next boot"
            Set-Reg "HKLM:\SYSTEM\CurrentControlSet\Services\$svc" Start 4
        }
        Write-Host 'GPU REBOOT'
        break
    }
    foreach ($d in @(Get-GpuPackages)) {
        Write-Host "--- pnputil /delete-driver $($d.Driver) ($(Split-Path $d.OriginalFileName -Leaf) $($d.Version))"
        & pnputil.exe /delete-driver $d.Driver /uninstall
        if (0, 3010 -notcontains $LASTEXITCODE) {
            & pnputil.exe /delete-driver $d.Driver /uninstall /force
            if (0, 3010 -notcontains $LASTEXITCODE) { throw "pnputil failed with exit code $LASTEXITCODE" }
        }
    }
    if (@(Get-GpuPackages).Count) { throw 'a VirtIO GPU driver package is still in the driver store' }
    $pending = @()
    foreach ($svc in $GpuDrivers.Keys) {
        $state = Get-ServiceState $svc
        if ($state -eq 'Absent') { continue }
        if ($state -ne 'Pending') {
            Write-Host "--- sc delete $svc"
            & sc.exe delete $svc | Out-Null
            if ($LASTEXITCODE -eq 1060) {
                # Never known to the SCM: no record to strand.
                Remove-Item "HKLM:\SYSTEM\CurrentControlSet\Services\$svc" -Recurse -Force -ErrorAction SilentlyContinue
            }
            $state = Get-ServiceState $svc
        }
        if ($state -ne 'Absent') { Write-Host "--- $svc is $state until the next restart"; $pending += $svc }
    }
    Get-VirtioGpu | Format-Table Status, Class, FriendlyName, InstanceId -AutoSize | Out-String | Write-Host
    if ($pending) { Write-Host 'GPU REBOOT'; break }
    Write-Host 'GPU OFF'
}

'debug' {
    if (Confirm-SecureBootUEFI) { throw 'Secure Boot is on; test signing and kernel debugging cannot be turned on' }
    function Bcd { & bcdedit.exe @args; if ($LASTEXITCODE) { throw "bcdedit $args failed with exit code $LASTEXITCODE" } }
    if (-not $HostIp) {
        # no kdnet network (a UTM without vmnet): test signing only
        Bcd /set '{current}' testsigning on
        break
    }
    if (-not $Port -or -not $Key) { throw '-Port and -Key are required with -HostIp' }
    $pci = Get-KdnetPciDevice
    if (-not $pci) { throw 'no e1000 (PCI\VEN_8086&DEV_100E) to debug over' }
    $bus = (Get-PnpDeviceProperty -InstanceId $pci.InstanceId -KeyName DEVPKEY_Device_BusNumber).Data
    $addr = (Get-PnpDeviceProperty -InstanceId $pci.InstanceId -KeyName DEVPKEY_Device_Address).Data
    # Without busparams kdnet may take the first NIC it has a driver for -
    # kd_02_1af4.dll drives the shared virtio NIC too.
    $busparams = "$bus.$($addr -shr 16).$($addr -band 0xFFFF)"
    Write-Host "--- e1000 $($pci.InstanceId) at $busparams"
    Bcd /set '{current}' testsigning on
    # /noumex: user-mode exceptions do not break into the kernel debugger.
    # Without it any user-mode breakpoint stops the whole VM until a debugger
    # attaches - the guest tools' spice-webdavd.exe hits one at every boot.
    Bcd /dbgsettings net "hostip:$HostIp" "port:$Port" "key:$Key" /noumex
    Bcd /set '{dbgsettings}' busparams $busparams
    Bcd /debug '{current}' on
    & bcdedit.exe /dbgsettings
    Set-LowRoutePriority
    Get-NetAdapter -InterfaceDescription $KdnicDescription -ErrorAction SilentlyContinue |
        Format-Table Name, InterfaceDescription, Status -AutoSize | Out-String | Write-Host
}

'tools' {
    if (-not (Test-Path "$env:ProgramFiles\7-Zip\7z.exe")) {
        Install '7z2605-arm64.exe' '/S'
    }
    if (-not (Test-Path "$env:ProgramFiles\Git\cmd\git.exe")) {
        Install 'Git-2.56.0.4-arm64.exe' '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES'
    }
    if (-not (Test-Path "$env:ProgramFiles\CMake\bin\cmake.exe")) {
        Install 'cmake-4.4.5-windows-arm64.msi' 'ADD_CMAKE_TO_PATH=System'
    }
    if (-not (Test-Path $Python)) {
        # Per user, on PATH, as on the golden VM.
        Install 'python-3.15.2-arm64.exe' '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1'
    }
    if (-not (Test-Installed 'WinFsp*')) {
        Install 'winfsp-2.2.26286.msi' @()
    }
    if (-not (Test-Installed 'SSHFS-Win*')) {
        Install 'sshfs-win-3.7.21011-x64.msi' @()
    }
}

'sshfs' {
    if (-not $HostUser) { throw '-HostUser is required' }
    # SSHFS-Win's key-based share (\\sshfs.k\) uses ~/.ssh/id_rsa.
    $ssh = Join-Path $env:USERPROFILE '.ssh'
    $key = Join-Path $ssh 'id_rsa'
    New-Item -ItemType Directory -Force $ssh | Out-Null
    if (-not (Test-Path $key)) {
        Write-Host '--- ssh-keygen -t rsa'
        $comment = "$env:USERNAME@$env:COMPUTERNAME".ToLower()
        # Windows PowerShell drops an empty argument, so -N gets '""'.
        & ssh-keygen.exe -q -t rsa -b 3072 -N '""' -C $comment -f $key
        if ($LASTEXITCODE) { throw "ssh-keygen failed with exit code $LASTEXITCODE" }
    }
    # The host is the gateway of the shared network.
    $hostIp = (Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Where-Object NextHop -ne '0.0.0.0' |
        Sort-Object RouteMetric | Select-Object -First 1).NextHop
    if (-not $hostIp) { throw 'no default gateway' }
    # Persistent mapping, restored at the next interactive logon.  "net use"
    # cannot connect an SSHFS-Win share from an SSH session (system error 64).
    $net = 'HKCU:\Network\Z'
    Set-Reg $net RemotePath "\\sshfs.k\$HostUser@$hostIp\Documents" String
    Set-Reg $net UserName '' String
    Set-Reg $net ProviderName 'Windows File System Proxy' String
    Set-Reg $net ProviderType 0x20737046
    Set-Reg $net ConnectionType 1
    Set-Reg $net ConnectFlags 0
    Write-Host "HOST_IP $hostIp"
    Write-Host "GUEST_PUBKEY $((Get-Content "$key.pub" -Raw).Trim())"
}

'vs' {
    $vs = if (Test-Path $VsWhere) { & $VsWhere -products * -requires Microsoft.VisualStudio.Workload.NativeDesktop -property installationPath }
    $config = Join-Path $PSScriptRoot 'vs2022.vsconfig'
    if ($vs) {
        # Already installed: add any component that is missing.
        $installer = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\setup.exe'
        Write-Host "--- modifying $vs"
        $p = Start-Process $installer -ArgumentList 'modify', '--installPath', "`"$vs`"", '--config', "`"$config`"",
            '--quiet', '--norestart', '--nocache' -Wait -PassThru -NoNewWindow
        if (0, 3010 -notcontains $p.ExitCode) { throw "Visual Studio modify failed with exit code $($p.ExitCode)" }
    } else {
        Install 'vs_community.exe' '--quiet', '--wait', '--norestart', '--nocache', '--config', "`"$config`""
    }
}

'python' {
    # pip prints warnings on stderr, which Windows PowerShell would turn into
    # errors; its exit code decides.
    $ErrorActionPreference = 'Continue'
    Write-Host '--- pip install meson mako packaging pyyaml'
    & $Python -m pip install --disable-pip-version-check --upgrade meson mako packaging pyyaml
    if ($LASTEXITCODE) { throw "pip install failed with exit code $LASTEXITCODE" }
}

'wdk' {
    if (-not (Test-Installed 'Windows Driver Kit - Windows 10.0.26100*')) {
        Install 'wdksetup.exe' '/quiet', '/norestart', '/ceip', 'off'
    }
}

'verify' {
    if (-not $Role) { throw '-Role is required' }
    $ErrorActionPreference = 'Continue'
    $failed = 0
    function Check([string] $What, [bool] $Ok, [string] $Detail = '') {
        if ($Ok) { Write-Host "  ok    $What $Detail" } else { Write-Host "  FAIL  $What $Detail"; $script:failed++ }
    }
    function Note([string] $What, [string] $Detail) { Write-Host "  note  $What $Detail" }

  if ($Role -eq 'dev') {
    Write-Host '=== Build tools (as build-mesa and build-drivers.cmd look for them)'
    foreach ($t in 'git.exe', 'curl.exe', 'tar.exe', 'cmake.exe', 'python.exe', 'pip.exe', 'meson.exe') {
        $c = Get-Command $t -ErrorAction SilentlyContinue
        Check $t ([bool]$c) $(if ($c) { $c.Source })
    }
    foreach ($m in 'packaging', 'mako', 'yaml') {
        & python.exe -c "import $m" 2>$null
        Check "python: import $m" ($LASTEXITCODE -eq 0)
    }
    Check '7-Zip' (Test-Path "$env:ProgramFiles\7-Zip\7z.exe")

    Write-Host '=== Visual Studio and WDK'
    $vs = & $VsWhere -latest -requires Microsoft.VisualStudio.Workload.NativeDesktop -property installationPath
    Check 'Visual Studio (NativeDesktop workload)' ([bool]$vs) $vs
    if ($vs) {
        $msvc = Get-ChildItem "$vs\VC\Tools\MSVC" -Directory | Sort-Object Name | Select-Object -Last 1
        foreach ($lib in 'lib\arm64', 'lib\arm64ec', 'lib\x64', 'lib\x86',
                         'lib\spectre\arm64', 'lib\spectre\arm64ec', 'lib\spectre\x64', 'lib\spectre\x86') {
            Check "MSVC $lib" (Test-Path "$($msvc.FullName)\$lib")
        }
        $missing = @()
        $installed = & $VsWhere -latest -format json -include packages | ConvertFrom-Json
        $ids = $installed[0].packages.id
        foreach ($c in (Get-Content (Join-Path $PSScriptRoot 'vs2022.vsconfig') -Raw | ConvertFrom-Json).components) {
            if ($ids -notcontains $c) { $missing += $c }
        }
        Check 'vs2022.vsconfig components' ($missing.Count -eq 0) ($missing -join ', ')
    }
    $kits = "${env:ProgramFiles(x86)}\Windows Kits\10"
    Check 'Windows SDK 10.0.26100' (Test-Path "$kits\Include\10.0.26100.0\um\windows.h")
    Check 'WDK 10.0.26100 (km headers)' (Test-Path "$kits\Include\10.0.26100.0\km\wdm.h")
    Check 'WDK ARM64 libs' (Test-Path "$kits\Lib\10.0.26100.0\km\arm64\ntoskrnl.lib")
    Check 'WDK MSBuild integration' (Test-Path "$kits\build\10.0.26100.0\WindowsDriver.Common.targets")
    Check 'signtool / inf2cat' ((Test-Path "$kits\bin\10.0.26100.0\x86\Inf2Cat.exe") -or (Test-Path "$kits\bin\10.0.26100.0\arm64\Inf2Cat.exe"))

    Write-Host '=== Host share'
    $z = Get-ChildItem Z:\ -ErrorAction SilentlyContinue
    Check 'Z: (host ~/Documents over SSHFS-Win)' ([bool]$z) "$(@($z).Count) entries"

    Write-Host '=== Kernel debugger'
    Check 'kd.exe' (Test-Path "$Debuggers\kd.exe") $Debuggers
    if ($KdnetAddress) {
        $kd = Get-NetIPAddress -InterfaceAlias kdnet -AddressFamily IPv4 -ErrorAction SilentlyContinue
        Check 'kdnet network address' ($kd.IPAddress -eq $KdnetAddress -and $kd.PrefixOrigin -eq 'Manual') "$($kd.IPAddress) ($($kd.PrefixOrigin))"
    } else { Write-Host '  skip  kdnet network (none: a UTM without vmnet)' }
  } else {
    Write-Host '=== Test signing and kernel debugging'
    $sb = $null
    try { $sb = Confirm-SecureBootUEFI } catch { }
    Check 'Secure Boot off' ($sb -eq $false) "Confirm-SecureBootUEFI: $sb"
    $opts = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control').SystemStartOptions
    Check 'booted with test signing' ($opts -match 'TESTSIGNING') $opts
    $bcd = (& bcdedit.exe /enum '{current}') -join "`n"
    Check 'BCD testsigning' ($bcd -match '(?m)^testsigning\s+Yes') ''
  if ($HostIp) {
    Check 'booted with kernel debugging' ($opts -match '\bDEBUG\b') ''
    Check 'BCD debug' ($bcd -match '(?m)^debug\s+Yes') ''
    $dbg = (& bcdedit.exe /dbgsettings) -join "`n"
    $want = @{ debugtype = 'NET'; hostip = $HostIp; port = "$Port"; key = $Key; noumex = 'Yes' }
    foreach ($k in $want.Keys) {
        $v = if ($dbg -match "(?m)^$k\s+(\S+)") { $Matches[1] } else { '' }
        Check "dbgsettings $k" ($v -eq $want[$k]) $v
    }
    $pci = Get-KdnetPciDevice
    $bp = if ($dbg -match '(?m)^busparams\s+(\S+)') { $Matches[1] } else { '' }
    Check 'dbgsettings busparams (the e1000)' ($pci -and $bp) "$bp $($pci.InstanceId)"
    $kdnic = Get-NetAdapter -InterfaceDescription $KdnicDescription -ErrorAction SilentlyContinue
    # Windows only shows the kdnet adapter as Up when kdnet started on the NIC.
    Check 'kdnet adapter (KDNIC) up' ($kdnic.Status -eq 'Up') "$($kdnic.Name) $($kdnic.Status)"
  } else { Write-Host '  skip  kernel debugging (no kdnet network: a UTM without vmnet)' }

    Write-Host '=== Drivers'
    $pkgs = @(Get-GpuPackages)
    Check 'no VirtIO GPU driver in the driver store' ($pkgs.Count -eq 0) (($pkgs | ForEach-Object { "$($_.Driver) $(Split-Path $_.OriginalFileName -Leaf)" }) -join ', ')
    foreach ($svc in $GpuDrivers.Keys) {
        $state = Get-ServiceState $svc
        Check "no $svc service" ($state -eq 'Absent') $state
    }
    $fallback = Get-ItemProperty $BasicDisplay -Name DisableBasicDisplayFallback -ErrorAction SilentlyContinue
    Check 'basic display fallback on' (-not $fallback) ''
    $gpu = Get-VirtioGpu
    $gpuSvc = if ($gpu) { (Get-PnpDeviceProperty -InstanceId $gpu.InstanceId -KeyName DEVPKEY_Device_Service -ErrorAction SilentlyContinue).Data }
    Check 'VirtIO GPU has no driver' ($gpu -and -not $gpuSvc) "$($gpu.FriendlyName) $gpuSvc"

    Check 'UTM guest tools' (Test-Installed 'UTM Guest Tools*')
    $net = Get-NetAdapter | Where-Object { $_.InterfaceDescription -like 'Red Hat VirtIO Ethernet*' -and $_.Status -eq 'Up' }
    Check 'VirtIO network up' ([bool]$net) $net.Name
    Check 'no build tools' (-not (Test-Path $VsWhere) -and -not (Test-Path "$env:ProgramFiles\Git")) ''
  }

    Write-Host '=== System'
    $sys = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
    Check 'UAC off' ($sys.EnableLUA -eq 0)
    $fw = Get-NetFirewallProfile | Where-Object Enabled
    Check 'firewall off' (-not $fw) ($fw.Name -join ', ')
    $wl = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Check 'automatic logon' ($wl.AutoAdminLogon -eq '1' -and $wl.DefaultUserName) $wl.DefaultUserName
    Check 'sshd automatic' ((Get-Service sshd).StartType -eq 'Automatic')
    Check 'apps from anywhere' ((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer').AicEnabled -eq 'Anywhere')
    $def = Get-Service WinDefend -ErrorAction SilentlyContinue
    $mp = Get-MpComputerStatus -ErrorAction SilentlyContinue
    $defOff = (-not $def) -or $def.Status -ne 'Running' -or ($mp -and -not $mp.RealTimeProtectionEnabled)
    $detail = if ($def) { "service $($def.Status)" } else { 'service absent' }
    if ($mp) { $detail += ", real-time $($mp.RealTimeProtectionEnabled), tamper protection $($mp.IsTamperProtected)" }
    Check 'Defender off' $defOff $detail
    $bl = Get-CimInstance -Namespace root\cimv2\Security\MicrosoftVolumeEncryption -ClassName Win32_EncryptableVolume -Filter "DriveLetter='C:'" -ErrorAction SilentlyContinue
    Check 'BitLocker off on C:' (-not $bl -or $bl.ProtectionStatus -eq 0) "$(if ($bl) { "protection status $($bl.ProtectionStatus)" })"

    if ($failed) { Write-Host "VERIFY FAILED ($failed)"; exit 1 }
    Write-Host 'VERIFY OK'
}

'content' {
    # 1638: a newer version of the runtime is already installed
    foreach ($a in 'arm64', 'x64', 'x86') { Install "vc_redist.$a.exe" @('/install', '/quiet', '/norestart') @(0, 1638, 3010) }

    # DirectX End-User Runtime (June 2010): D3DX, XAudio2 2.7, XInput 1.3.
    # Windows does not ship it and older engines import it (3DMark Fire
    # Strike: d3dx11_43.dll; without it its workload never starts and 3DMark
    # reports NO_RESPONSE)
    if (-not (Test-Path (Join-Path $env:SystemRoot 'System32\d3dx11_43.dll'))) {
        $dx = Join-Path $env:TEMP 'dxredist'
        Remove-Item -Recurse -Force $dx -ErrorAction SilentlyContinue
        Install 'directx_Jun2010_redist.exe' @('/Q', "/T:$dx")
        Write-Host '--- DXSETUP.exe /silent'
        $p = Start-Process (Join-Path $dx 'DXSETUP.exe') -ArgumentList '/silent' -Wait -PassThru -NoNewWindow
        if ($p.ExitCode -ne 0) { throw "DXSETUP failed with exit code $($p.ExitCode)" }
        Remove-Item -Recurse -Force $dx
    }

    $gm = Join-Path $env:ProgramFiles 'GravityMark\bin\GravityMark.exe'
    if (-not (Test-Path $gm)) { Install 'GravityMark_1.89_arm64.msi' @() }
    $heaven = Join-Path ${env:ProgramFiles(x86)} 'Unigine\Heaven Benchmark 4.0\bin\Heaven.exe'
    if (-not (Test-Path $heaven)) { Install 'Unigine_Heaven-4.0.exe' @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-') }

    # a blanked display or a sleeping VM invalidates a benchmark run; Game DVR
    # hooks D3D presentation
    & powercfg /change monitor-timeout-ac 0
    & powercfg /change standby-timeout-ac 0
    Set-Reg 'HKCU:\System\GameConfigStore' 'GameDVR_Enabled' 0
    Set-Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\GameDVR' 'AppCaptureEnabled' 0

    $markCmd = Join-Path $env:ProgramFiles 'UL\3DMark\3DMarkCmd.exe'
    if ($Mark) {
        $src = Join-Path $Downloads '3dmark'
        if (-not (Test-Path $markCmd)) {
            $msi = Join-Path $src 'data_x64.msi'
            if (Test-Path $msi) {
                # the payloads of the bundle: a 3dmark-setup.exe taken from a
                # Package Cache is only the Burn engine, without its container
                # Burn normally passes the folders its InstallDLC custom action
                # side-loads tests from and into; alone, the MSI gets the literal
                # "default" for both and fails (1603).  Side-load from an empty
                # folder: the tests are copied from dlc\ below.
                $empty = Join-Path $Downloads '3dmark-none'
                New-Item -ItemType Directory -Force $empty | Out-Null
                $dlcDst = Join-Path $env:ProgramData 'UL\3DMark\chops'   # 3DMark puts its tests in dlc\ under it
                foreach ($m in @('data_x64.msi', 'Futuremark_SystemInfo.msi')) {
                    $path = Join-Path $src $m
                    if (-not (Test-Path $path)) { continue }
                    Write-Host "--- msiexec /i $m /qn"
                    $p = Start-Process msiexec.exe -ArgumentList '/i', "`"$path`"", '/qn', '/norestart', "INSTALLLOCATION=`"$empty`"", "DLC_DESTINATION=`"$dlcDst`"" -Wait -PassThru -NoNewWindow
                    if (@(0, 3010) -notcontains $p.ExitCode) { throw "$m failed with exit code $($p.ExitCode)" }
                }
            } else {
                Write-Host '--- 3dmark-setup.exe /install /quiet /norestart'
                $p = Start-Process (Join-Path $src '3dmark-setup.exe') -ArgumentList '/install', '/quiet', '/norestart' -Wait -PassThru -NoNewWindow
                if (@(0, 3010) -notcontains $p.ExitCode) { throw "3dmark-setup.exe failed with exit code $($p.ExitCode) (see %TEMP%\3DMark_*.log)" }
            }
            if (-not (Test-Path $markCmd)) { throw '3DMark setup finished but 3DMarkCmd.exe is missing' }
        }
        # 3DMark Basic's download: each test is a packed <test>.dlc next to the
        # installer; 3DMarkCmd installs every .dlc in a folder (as the MSI's own
        # InstallDLC step does)
        if (Get-ChildItem $src -Filter '*.dlc' -File -ErrorAction SilentlyContinue) {
            Write-Host '--- 3DMarkCmd --install <the .dlc tests>'
            $p = Start-Process $markCmd -ArgumentList '--install', "`"$src`"" -Wait -PassThru -NoNewWindow
            if ($p.ExitCode -ne 0) { throw "3DMarkCmd --install failed with exit code $($p.ExitCode)" }
        }
        $dlc = Join-Path $src 'dlc'
        if (Test-Path $dlc) {
            Write-Host '--- 3DMark tests from dlc'
            $dst = Join-Path $env:ProgramData 'UL\3DMark\chops\dlc'
            New-Item -ItemType Directory -Force $dst | Out-Null
            Copy-Item -Recurse -Force (Join-Path $dlc '*') $dst
        }
        # Check every file of the tests the harness runs against the SHA-256 in
        # its manifest.  A fresh VM once got a corrupt 1.5 GB Time Spy file from
        # a good .dlc (3DMark then fails: "decompress() -3"); reinstalling that
        # one test fixed it.  So: verify, reinstall a bad test once, verify again.
        $chops = Join-Path $env:ProgramData 'UL\3DMark\chops\dlc'
        function Test-MarkTest($t) {
            $mf = Join-Path $chops "$t.manifest"
            if (-not (Test-Path $mf)) { return "no $t.manifest" }
            foreach ($f in (Get-Content -Raw $mf | ConvertFrom-Json).chopsFiles) {
                $path = Join-Path (Join-Path $chops $t) ($f.path -replace '/', '\')
                if (-not (Test-Path $path)) { return "missing $($f.path)" }
                if ((Get-FileHash $path -Algorithm SHA256).Hash -ne $f.hash.ToUpper()) { return "bad $($f.path)" }
            }
            return $null
        }
        foreach ($t in @('fire-strike-test', 'time-spy-test')) {
            $why = Test-MarkTest $t
            if (-not $why) { continue }
            $pkg = Get-ChildItem $src -Filter "*-$t-v*.dlc" -File -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $pkg) { Write-Host "WARNING: 3DMark $t not usable ($why) and no .dlc to reinstall it from"; continue }
            Write-Host "--- 3DMark $t failed its check ($why): reinstalling it"
            Remove-Item -Recurse -Force (Join-Path $chops $t), (Join-Path $chops "$t.manifest") -ErrorAction SilentlyContinue
            $one = Join-Path $Downloads "3dmark-$t"
            New-Item -ItemType Directory -Force $one | Out-Null
            Move-Item -Force $pkg.FullName $one
            $p = Start-Process $markCmd -ArgumentList '--install', "`"$one`"" -Wait -PassThru -NoNewWindow
            $why = Test-MarkTest $t
            if ($why) { throw "3DMark $t still fails its check after reinstalling ($why)" }
            Write-Host "--- 3DMark $t verified after reinstalling"
        }

        $keyFile = Join-Path $Downloads '3dmark.key'
        if (Test-Path $keyFile) {
            $k = (Get-Content -Raw $keyFile).Trim()
            Remove-Item -Force $keyFile
            Write-Host '--- 3DMarkCmd --register (key not shown)'
            # 3DMark keeps the activation per user (HKCU): this must run as the
            # autologon user, which is the SSH user
            $out = & $markCmd "--register=$k" 2>&1 | Out-String
            $k = $null
            if ($LASTEXITCODE -ne 0) { Write-Host ($out -replace '[A-Z0-9]{5}(-[A-Z0-9]{5}){3,}', '<key>'); throw "3DMark registration failed ($LASTEXITCODE)" }
        }
    }

    $have = @()
    foreach ($i in @(@('GravityMark', $gm), @('Heaven', $heaven), @('3DMark', $markCmd))) { if (Test-Path $i[1]) { $have += $i[0] } }
    $reg = Get-ItemProperty 'HKCU:\SOFTWARE\UL\3DMark' -ErrorAction SilentlyContinue
    $activated = [bool]($reg -and $reg.KeyCode)
    $tests = @(Get-ChildItem (Join-Path $env:ProgramData 'UL\3DMark\chops\dlc') -Directory -ErrorAction SilentlyContinue | ForEach-Object Name)
    Write-Host "CONTENT OK: $($have -join ', '); 3DMark activated: $activated; 3DMark tests: $($tests -join ', ')"
}
}
