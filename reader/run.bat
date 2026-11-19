@echo off
:: Auris - Windows launcher (after `setup.bat`, see the README)
:: The TTS model loads in the background; the app opens immediately.

cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo uv is not installed. Get it from https://docs.astral.sh/uv/ and run: setup.bat
    exit /b 1
)
if not exist ".venv" (
    echo No environment yet. Run: setup.bat
    exit /b 1
)

echo Starting Auris...
echo.
echo Model status will appear in the top-right corner of the app.
echo (Model loads in the background; you can browse and read while it loads.)
echo.
echo Press Ctrl+C to stop.

:: --no-sync: keep whichever PyTorch build was chosen at setup time instead of re-syncing to the default
uv run --no-sync app.py %*
