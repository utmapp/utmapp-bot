<#
.SYNOPSIS
Verifies a test-signed viogpu3d driver package before it is handed out.

.DESCRIPTION
Fails (exit 1) unless the package directory:
  - contains the KMD, its INF and catalog, and the user-mode driver files;
  - has an INF DriverVer equal to -Version, and every Neptune UMD DLL carries
    that same file version (Windows refuses to load the UMD otherwise);
  - references every user-mode driver file from the INF;
  - has a signed viogpu3d.sys and viogpu3d.cat;
  - lists every packaged file in viogpu3d.cat.

Must run where signtool.exe is on PATH (the KMD build environment).

.PARAMETER Arm64x
Expect the ARM64X user-mode driver (forwarder plus arm64 and arm64ec views).

.PARAMETER Wow64
Expect the x86 user-mode driver for WOW64 applications.
#>
param(
    [Parameter(Mandatory)] [string] $PackageDir,
    [Parameter(Mandatory)] [string] $Version,
    [switch] $Arm64x,
    [switch] $Wow64
)

$ErrorActionPreference = 'Stop'
$problems = [System.Collections.Generic.List[string]]::new()

$umd = @('neptune_umd.dll')
if ($Arm64x) { $umd += 'neptune_umd_arm64.dll', 'neptune_umd_ec.dll' }
if ($Wow64) { $umd += 'neptune_umd_x86.dll' }
$userMode = $umd + 'vulkan_virtio.dll', 'virtio_icd.json'
$expected = @('viogpu3d.sys', 'viogpu3d.inf', 'viogpu3d.cat') + $userMode

foreach ($f in $expected) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageDir $f))) { $problems.Add("missing $f") }
}

if ($problems.Count -eq 0) {
    $inf = Get-Content -LiteralPath (Join-Path $PackageDir 'viogpu3d.inf')

    $m = $inf | Select-String '^\s*DriverVer\s*=\s*[^,]*,\s*([0-9.]+)' | Select-Object -First 1
    $driverVer = if ($m) { $m.Matches[0].Groups[1].Value } else { '<none>' }
    if ($driverVer -ne $Version) { $problems.Add("INF DriverVer is $driverVer, expected $Version") }

    foreach ($f in $userMode) {
        if (-not ($inf | Select-String -SimpleMatch $f)) { $problems.Add("INF does not reference $f") }
    }

    foreach ($f in $umd) {
        $fileVersion = (Get-Item -LiteralPath (Join-Path $PackageDir $f)).VersionInfo.FileVersion
        if ($fileVersion -ne $Version) { $problems.Add("$f file version is $fileVersion, expected $Version") }
    }

    foreach ($f in 'viogpu3d.sys', 'viogpu3d.cat') {
        $sig = Get-AuthenticodeSignature -LiteralPath (Join-Path $PackageDir $f)
        if (-not $sig.SignerCertificate) { $problems.Add("$f is not signed") }
    }

    # The test root is normally untrusted on the build VM, so signtool reports
    # a chain error even for files the catalog does cover; only a hash that is
    # missing from the catalog counts as a failure.  signtool wraps the chain
    # message across lines, hence \s+.
    $cat = Join-Path $PackageDir 'viogpu3d.cat'
    if (-not (Get-Command signtool.exe -ErrorAction SilentlyContinue)) {
        $problems.Add('signtool.exe is not on PATH; cannot check the catalog')
        $expected = @()
    }
    foreach ($f in $expected | Where-Object { $_ -ne 'viogpu3d.cat' }) {
        $out = cmd /c "signtool verify /pa /c `"$cat`" `"$(Join-Path $PackageDir $f)`" 2>&1" | Out-String
        if ($LASTEXITCODE -ne 0 -and $out -notmatch 'terminated in a root\s+certificate') {
            $problems.Add("$f is not covered by viogpu3d.cat")
        }
    }
}

if ($problems.Count -gt 0) {
    Write-Host "ERROR: package $PackageDir failed verification:"
    $problems | ForEach-Object { Write-Host "  - $_" }
    exit 1
}
Write-Host "Verified $PackageDir (version $Version, $($expected.Count) files)"
exit 0
