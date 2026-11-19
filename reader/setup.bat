@echo off
:: Auris - Windows setup: creates .venv from uv.lock with the right PyTorch build.
::
:: Usage: setup.bat                  first run: picks the build for this machine and remembers it
::        setup.bat                  later runs: re-syncs with the remembered build (e.g. after a git pull)
::        setup.bat --extra cpu      choose explicitly (cpu ^| cu128 ^| default) and remember that
:: Anything after the choice is passed to `uv sync` (for example --dry-run).
::
:: The choice lives in .auris-extra. Delete it to have the machine detected again.

setlocal
cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo uv is not installed. Get it from https://docs.astral.sh/uv/
    exit /b 1
)

set "CHOICE="
if "%~1"=="--extra" (
    set "CHOICE=%~2"
    shift
    shift
) else if exist ".auris-extra" (
    set /p CHOICE=<.auris-extra
    echo Using the remembered PyTorch build: %CHOICE% ^(setup.bat --extra ^<cpu^|cu128^|default^> to change^)
)

if not defined CHOICE (
    where nvidia-smi >nul 2>&1
    if errorlevel 1 (set "CHOICE=cpu") else (set "CHOICE=cu128")
    echo Detected this machine: PyTorch build %CHOICE%
)

set "EXTRA="
if "%CHOICE%"=="cpu" set "EXTRA=--extra cpu"
if "%CHOICE%"=="cu128" set "EXTRA=--extra cu128"
if not "%CHOICE%"=="cpu" if not "%CHOICE%"=="cu128" if not "%CHOICE%"=="default" (
    echo Unknown PyTorch build '%CHOICE%' ^(use cpu, cu128 or default^)
    exit /b 1
)

:: what is left after the choice goes to uv
set "REST="
:collect
if "%~1"=="" goto :sync
set "REST=%REST% %1"
shift
goto :collect

:sync
uv sync %EXTRA% %REST%
if errorlevel 1 exit /b 1
echo %REST% | find "--dry-run" >nul
if errorlevel 1 > .auris-extra echo %CHOICE%
echo.
echo Done. Start Auris with: run.bat
