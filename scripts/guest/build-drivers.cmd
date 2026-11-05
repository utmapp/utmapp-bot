@echo off
setlocal enabledelayedexpansion

rem ===========================================================================
rem  build-drivers.cmd - runs on the Windows build VM.
rem
rem  Builds the Neptune user-mode driver (Mesa, via build-mesa) and the viogpu3d
rem  kernel-mode driver package, test-signs the package and verifies it.
rem
rem  usage: build-drivers.cmd <arch> <mesa-src> <kmd-src> <workdir>
rem
rem    arch      arm64x | x64 | all
rem    mesa-src  Mesa source tree (only read; build.cmd copies it)
rem    kmd-src   kvm-guest-drivers-windows source tree (only read; mirrored)
rem    workdir   holds build-mesa\ (uploaded by the host) and kmd\ (the KMD
rem              mirror).  Keep it between runs: both builds are incremental.
rem
rem  On success prints "DRIVER_VERSION <ver>" and one "PACKAGE <TargetArch>
rem  <dir>" line per verified package for the host to collect.
rem ===========================================================================

set SCRIPT_DIR=%~dp0
set ARCH=%~1
set MESA_SRC=%~2
set KMD_SRC=%~3
set WORK=%~4
if "%WORK%" equ "" (
  echo usage: build-drivers.cmd ^<arch^> ^<mesa-src^> ^<kmd-src^> ^<workdir^>
  exit /b 2
)

rem arch -> Mesa builds and KMD TargetArch values.  There is no x86 package:
rem the WDK no longer builds 32-bit kernel drivers.  "all" still builds the x86
rem UMD and packages it into the 64-bit packages for WOW64 applications.
set WOW64=
if /i "%ARCH%" equ "arm64x" (
  set UMD_ARCHES=arm64x
  set KMD_ARCHES=ARM64
) else if /i "%ARCH%" equ "x64" (
  set UMD_ARCHES=x64
  set KMD_ARCHES=amd64
) else if /i "%ARCH%" equ "all" (
  set UMD_ARCHES=x86 x64 arm64x
  set KMD_ARCHES=amd64 ARM64
  set WOW64=1
) else (
  echo ERROR: unknown arch "%ARCH%", expected arm64x, x64 or all
  exit /b 2
)

set BM=%WORK%\build-mesa
set KMD=%WORK%\kmd

if not exist "%BM%\build.cmd" ( echo ERROR: "%BM%\build.cmd" not found & exit /b 1 )
if not exist "%MESA_SRC%\meson.build" ( echo ERROR: no Mesa source at "%MESA_SRC%" & exit /b 1 )
if not exist "%KMD_SRC%\viogpu\viogpu.sln" ( echo ERROR: no kvm-guest-drivers-windows source at "%KMD_SRC%" & exit /b 1 )

if /i "%PROCESSOR_ARCHITECTURE%" equ "ARM64" (
  set HOST_ARCH=arm64
) else if /i "%PROCESSOR_ARCHITECTURE%" equ "AMD64" (
  set HOST_ARCH=amd64
) else (
  set HOST_ARCH=x86
)

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set VS=
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -requires Microsoft.VisualStudio.Workload.NativeDesktop -property installationPath`) do set "VS=%%i"
if "%VS%" equ "" ( echo ERROR: Visual Studio installation not found & exit /b 1 )

rem *** record the source revisions ***

for /f %%i in ('git -c safe.directory^=* -C "%MESA_SRC%" rev-parse HEAD 2^>nul') do echo MESA_REVISION %%i
for /f %%i in ('git -c safe.directory^=* -C "%KMD_SRC%" rev-parse HEAD 2^>nul') do echo KMD_REVISION %%i

rem *** mirror the KMD source ***

rem The KMD is built from a local mirror: msbuild/cl writing .pdb files to an
rem SSHFS-Win share can hang the share.  Build outputs are excluded so the
rem mirror keeps them and msbuild stays incremental.
echo === Mirroring KMD source to %KMD%
robocopy "%KMD_SRC%" "%KMD%" /MIR /XD .git .vs objfre_* objchk_* Install Install_Debug /XF buildfre_*.log buildchk_*.log /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 ( echo ERROR: mirroring "%KMD_SRC%" failed & exit /b 1 )

rem *** driver version ***

rem Windows requires the UMD file version to equal the INF DriverVer, so read
rem the version the KMD will stamp and build the UMD with it.
setlocal
call :kmd_env || exit /b 1
set V=
for /f "usebackq tokens=*" %%v in (`msbuild -nologo "%KMD%\viogpu\viogpu3d\viogpu3d.vcxproj" -getProperty:STAMPINF_VERSION -p:Configuration^="Win10 Release" -p:Platform^=x64`) do set V=%%v
endlocal & set DRIVER_VERSION=%V%
echo %DRIVER_VERSION%| findstr /r /x "[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*" >nul || (
  echo ERROR: could not read the driver version from viogpu3d.vcxproj ^(got "%DRIVER_VERSION%"^)
  exit /b 1
)
echo DRIVER_VERSION %DRIVER_VERSION%

rem *** user-mode driver (Mesa) ***

set NPT_UMD_VERSION=%DRIVER_VERSION%
pushd "%BM%"
for %%a in (%UMD_ARCHES%) do (
  echo === Building Mesa UMD: %%a
  if "%%a" equ "arm64x" (
    call build_arm64x.cmd "%MESA_SRC%"
  ) else (
    call build.cmd "%MESA_SRC%" %%a
  )
  if errorlevel 1 (
    popd
    echo ERROR: Mesa %%a build failed
    exit /b 1
  )
)
popd

rem *** kernel-mode driver package ***

rem Each package dir is emptied first so no file from an earlier run with
rem different options is left behind.  Only Win10 is built: viogpu.sln maps
rem viogpu3d's Win11 configuration onto Win10, so the Win10 package serves both.
setlocal
call :kmd_env || exit /b 1
pushd "%KMD%\viogpu"
for %%k in (%KMD_ARCHES%) do (
  echo === Building KMD package: %%k
  call :kmd_prefixes %%k
  rd /s /q "Install\Win10\%%k" 1>nul 2>nul
  call ..\build\build.bat viogpu.sln Win10 %%k
  rem build.bat does not reliably return an error, so also check its output.
  if not exist "Install\Win10\%%k\viogpu3d.inf" cmd /c exit /b 1
  if errorlevel 1 (
    popd
    echo ERROR: KMD %%k build failed
    exit /b 1
  )
)

rem The build regenerates the catalog unsigned every time; sign with the
rem repo's test certificate.
echo === Test-signing
call ..\build\signAll.bat

for %%k in (%KMD_ARCHES%) do (
  call :verify %%k || ( popd & exit /b 1 )
)
popd
endlocal

echo Done^^!
exit /b 0


rem Enter the KMD build environment: Visual Studio + the installed WDK.
:kmd_env
if defined EnterpriseWDK goto :eof
call "%VS%\Common7\Tools\VsDevCmd.bat" -arch=%HOST_ARCH% -host_arch=%HOST_ARCH% -startdir=none -no_logo || exit /b 1
rem build\SetVsEnv.bat looks for an EWDK unless EnterpriseWDK is set, and the
rem WDK targets read EnterpriseWDK as a boolean: "false" keeps VS + WDK.
set EnterpriseWDK=false
goto :eof


rem Point the KMD build at the UMD for TargetArch %1.
:kmd_prefixes
set MESA_PREFIX=
set MESA_ARM64X_PREFIX=
set MESA_X86_PREFIX=
if "%1" equ "ARM64" (
  set MESA_PREFIX=%BM%\mesa-install-arm64
  set MESA_ARM64X_PREFIX=%BM%\mesa-install-arm64x
) else (
  set MESA_PREFIX=%BM%\mesa-install-x64
)
if defined WOW64 set MESA_X86_PREFIX=%BM%\mesa-install-x86
goto :eof


rem Verify the package for TargetArch %1 and report it.
:verify
set VERIFY_ARGS=
if "%1" equ "ARM64" set VERIFY_ARGS=-Arm64x
if defined WOW64 set VERIFY_ARGS=%VERIFY_ARGS% -Wow64
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%verify-package.ps1" -PackageDir "%KMD%\viogpu\Install\Win10\%1" -Version %DRIVER_VERSION% %VERIFY_ARGS% || exit /b 1
echo PACKAGE %1 %KMD%\viogpu\Install\Win10\%1
goto :eof
