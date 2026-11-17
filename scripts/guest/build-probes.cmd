@echo off
rem build-probes.cmd <dir> - build every probe in <dir> for arm64 and x64.
rem Output: <dir>\ARM64\*.exe and <dir>\x64\*.exe.  Prints PROBES_OK on success.
rem Vulkan probes (vk*.c) need <dir>\vulkan\ (the Vulkan headers) and
rem <dir>\vulkan-1.def (the loader functions they call), which build-probes.sh
rem uploads; they link an import library made from that .def, so the build
rem needs no Vulkan SDK.  The x64 build loads the ARM64X ICD's ARM64EC view.
setlocal enabledelayedexpansion
set "DIR=%~1"
if "%DIR%" equ "" ( echo usage: build-probes.cmd ^<dir^> & exit /b 2 )

if /i "%PROCESSOR_ARCHITECTURE%" equ "ARM64" ( set HOST_ARCH=arm64 ) else ( set HOST_ARCH=amd64 )
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set VS=
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -requires Microsoft.VisualStudio.Workload.NativeDesktop -property installationPath`) do set "VS=%%i"
if "%VS%" equ "" ( echo ERROR: Visual Studio installation not found & exit /b 1 )

set FAILED=0
for %%a in (ARM64 x64) do (
  set TARGET=arm64
  if "%%a" equ "x64" set TARGET=amd64
  if not exist "%DIR%\%%a" mkdir "%DIR%\%%a"
  pushd "%DIR%\%%a"
  call "%VS%\Common7\Tools\VsDevCmd.bat" -arch=!TARGET! -host_arch=%HOST_ARCH% -startdir=none -no_logo >nul || ( popd & exit /b 1 )
  if exist "%DIR%\vulkan-1.def" lib /nologo /def:"%DIR%\vulkan-1.def" /machine:%%a /out:vulkan-1.lib >nul
  for %%f in ("%DIR%\*.c") do (
    set "LIBS=dxgi.lib d3d11.lib dxguid.lib user32.lib"
    set "NAME=%%~nf"
    set SKIP=
    if /i "!NAME:~0,2!" equ "vk" (
      set "LIBS=vulkan-1.lib"
      if not exist "%DIR%\vulkan\vulkan.h" set SKIP=1
    )
    if defined SKIP (
      echo skipped %%a\%%~nf.exe: no Vulkan headers
    ) else (
      cl /nologo /W3 /O2 /I"%DIR%" "%%f" /Fe"%%~nf.exe" /link !LIBS! >"%%~nf.build.txt" 2>&1
      if errorlevel 1 (
        echo ERROR: %%a %%~nf failed:
        type "%%~nf.build.txt"
        set FAILED=1
      ) else (
        echo built %%a\%%~nf.exe
      )
    )
  )
  del /q *.obj 2>nul
  popd
)
if %FAILED% neq 0 exit /b 1
echo PROBES_OK
exit /b 0
