<#
.SYNOPSIS
Runs on the Windows build VM: brings three local git clones to the commits the
host sent and runs build-viogpu3d's build.ps1 in them.

.DESCRIPTION
build-drivers.sh (on the host) uploads one git bundle per repository that
holds whatever the clone here may lack, and names for each the commit to build
and, when the host's checkout had uncommitted changes, a work-in-progress
commit whose tree is that working tree.

The clones live on the VM's own disk under -Work, as siblings, so build.ps1
finds them without parameters:

  <Work>\build-viogpu3d   <Work>\viogpu-wddm   <Work>\mesa

They are real git checkouts on purpose: build.ps1 stamps every binary with
`git rev-parse HEAD` (and a "+" when `git status` shows changes), and the
kernel driver builds in place, which must not happen on the SSHFS share.

A work-in-progress commit is checked out and then HEAD is reset to its parent
without touching the files, so the clone shows the same commit and the same
uncommitted changes as the host's checkout, and the stamp ends in "+".

Prints "BUILD-OUT <dir>" (the out\<Os> directory build.ps1 wrote) on success.
#>
param(
    [Parameter(Mandatory)] [string] $Work,
    # name=commit[:wip] for each repository, comma-separated
    [Parameter(Mandatory)] [string] $Revisions,
    # a file holding the arguments for build.ps1 on one line (a file, because
    # an argument that starts with "-" does not survive powershell -File)
    [string] $BuildArgsFile
)

$ErrorActionPreference = 'Stop'
$BuildArgs = ''
if ($BuildArgsFile) { $BuildArgs = ([System.IO.File]::ReadAllText($BuildArgsFile)).Trim() }
$repos = [ordered]@{}
foreach ($item in ($Revisions -split ',')) {
    $name, $spec = $item -split '=', 2
    $commit, $wip = $spec -split ':', 2
    $repos[$name] = @{ Commit = $commit; Wip = $wip }
}

function Git {
    param([string] $Dir)
    # every git call goes through here, so a failure stops the build
    & git.exe -C $Dir @args
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed in $Dir (exit $LASTEXITCODE)" }
}

foreach ($name in $repos.Keys) {
    $dir = Join-Path $Work $name
    $want = $repos[$name]
    Write-Host "=== Sync $name -> $($want.Commit.Substring(0, 10))$(if ($want.Wip) { ' + uncommitted changes' })"
    if (-not (Test-Path -LiteralPath (Join-Path $dir '.git'))) {
        # core.autocrlf=false: the tree is checked out as .gitattributes says,
        # exactly as on the host, so the clone is clean when the host's is.
        & git.exe -c core.autocrlf=false -c core.longpaths=true clone -q --no-checkout "https://github.com/utmapp/$name.git" $dir
        if ($LASTEXITCODE -ne 0) { throw "could not clone utmapp/$name" }
        Git $dir config core.autocrlf false
        Git $dir config core.longpaths true
    }
    Git $dir fetch -q origin
    $bundle = Join-Path $Work "host\$name.bundle"
    if (Test-Path -LiteralPath $bundle) {
        Git $dir fetch -q -f $bundle 'refs/utmbot/*:refs/utmbot/*'
    }
    $target = if ($want.Wip) { $want.Wip } else { $want.Commit }
    & git.exe -C $dir cat-file -e "$target^{commit}" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "$name`: commit $target is neither on GitHub nor in the bundle" }
    # Untracked files left by an earlier work-in-progress build would ride
    # along; ignored ones (build outputs) stay, so builds remain incremental.
    Git $dir clean -fdq
    Git $dir checkout -q -f --detach $target
    if ($want.Wip) { Git $dir reset -q --mixed $want.Commit }
    $status = & git.exe -C $dir status --porcelain --untracked-files=no
    Write-Host "    $(& git.exe -C $dir log -1 --format='%h %s')$(if ($status) { " (+$(@($status).Count) changed)" })"
}

$build = Join-Path $Work 'build-viogpu3d\build.ps1'
Write-Host "=== build.ps1 $BuildArgs"
# A fresh PowerShell, as build.cmd starts it: build.ps1 exits the process on
# failure, and splits its list parameters itself ("-Arch arm64x,x86").
$argList = @($BuildArgs -split '\s+' | Where-Object { $_ })
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $build @argList
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: build.ps1 failed (exit $LASTEXITCODE)"; exit $LASTEXITCODE }

# Where build.ps1 put the packages: out\<Os>, with -Debug for a checked build.
$os = 'Win10'
if ($BuildArgs -match '-Os\s+(Win1[01])') { $os = $Matches[1] }
if ($BuildArgs -match '-Configuration\s+Debug') { $os += '-Debug' }
Write-Host "BUILD-OUT $(Join-Path $Work "build-viogpu3d\out\$os")"
Write-Host "BUILD-SYMBOLS $(Join-Path $Work 'build-viogpu3d\out\symbols')"
