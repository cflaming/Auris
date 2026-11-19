#!/usr/bin/env bash
# Auris - Linux / macOS launcher
# Usage: bash run.sh        (after `bash setup.sh`, see the README)

set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. Get it from https://docs.astral.sh/uv/ and run: bash setup.sh"
    exit 1
fi
if [ ! -d .venv ]; then
    echo "No environment yet. Run: bash setup.sh"
    exit 1
fi

echo "Starting Auris..."
echo ""
echo "Model status will appear in the top-right corner of the app."
echo "(Model loads in the background; you can browse and read while it loads.)"
echo ""
echo "Press Ctrl+C to stop."

# --no-sync: keep whichever PyTorch build was chosen at setup time instead of re-syncing to the default
uv run --no-sync app.py "$@"
