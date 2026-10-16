@echo off
:: Auris - Windows setup wrapper
:: Usage: double-click or run from Command Prompt
::
:: Creates .venv with uv when available (directly or through mise), so no
:: separately installed Python is needed. AURIS_PYTHON selects the version.

setlocal
cd /d "%~dp0"

if not defined AURIS_PYTHON set "AURIS_PYTHON=3.11"

set "UV="
where uv >nul 2>&1
if not errorlevel 1 set "UV=uv"
if not defined UV (
    where mise >nul 2>&1
    if not errorlevel 1 (
        mise which uv >nul 2>&1
        if not errorlevel 1 set "UV=mise exec -- uv"
    )
)

if defined UV goto :uv_venv

set "BOOTSTRAP="
where python >nul 2>&1
if not errorlevel 1 set "BOOTSTRAP=python"
if not defined BOOTSTRAP (
    where py >nul 2>&1
    if not errorlevel 1 set "BOOTSTRAP=py -3"
)

if not defined BOOTSTRAP (
    echo Python not found. Install uv from https://docs.astral.sh/uv/ or Python 3.10+ from https://python.org
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv...
    call %BOOTSTRAP% -m venv .venv
    if errorlevel 1 goto :venv_failed
)
goto :run_setup

:uv_venv
if exist ".venv\Scripts\python.exe" (
    set "HAVE_PY="
    for /f "delims=" %%v in ('".venv\Scripts\python.exe" -c "import sys; print('%%d.%%d' %% sys.version_info[:2])" 2^>nul') do set "HAVE_PY=%%v"
    call :check_version
)
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv with uv ^(Python %AURIS_PYTHON%^)...
    call %UV% venv --seed --python %AURIS_PYTHON% .venv
    if errorlevel 1 goto :venv_failed
)

:run_setup
".venv\Scripts\python.exe" setup.py
if errorlevel 1 (
    echo.
    echo Setup failed. See output above for details.
    pause
    exit /b 1
)

pause
exit /b 0

:check_version
if not "%HAVE_PY%"=="%AURIS_PYTHON%" (
    echo Existing .venv uses Python %HAVE_PY%, wanted %AURIS_PYTHON%; recreating...
    rmdir /s /q .venv
)
exit /b 0

:venv_failed
echo Failed to create virtual environment.
pause
exit /b 1
