#!/usr/bin/env bash
# Auris - Linux / macOS setup wrapper
# Usage: bash setup.sh
#
# Creates .venv with uv when available (directly or through mise), so no
# separately installed Python is needed. AURIS_PYTHON selects the version.

set -e
cd "$(dirname "$0")"

WANT_PY="${AURIS_PYTHON:-3.11}"

UV=()
if command -v uv >/dev/null 2>&1; then
    UV=(uv)
elif command -v mise >/dev/null 2>&1 && mise which uv >/dev/null 2>&1; then
    UV=(mise exec -- uv)
fi

if [ ${#UV[@]} -gt 0 ]; then
    if [ -x ".venv/bin/python" ]; then
        HAVE_PY=$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
        if [ "$HAVE_PY" != "$WANT_PY" ]; then
            echo "Existing .venv uses Python ${HAVE_PY:-unknown}, wanted $WANT_PY; recreating..."
            rm -rf .venv
        fi
    fi
    if [ ! -x ".venv/bin/python" ]; then
        echo "Creating virtual environment in .venv with uv (Python $WANT_PY)..."
        "${UV[@]}" venv --seed --python "$WANT_PY" .venv
    fi
else
    PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)
    if [ -z "$PYTHON" ]; then
        echo "Python not found. Install uv (https://docs.astral.sh/uv/) or Python 3.10+ first."
        exit 1
    fi
    if [ ! -x ".venv/bin/python" ]; then
        echo "Creating virtual environment in .venv with $PYTHON..."
        "$PYTHON" -m venv .venv
    fi
fi

".venv/bin/python" setup.py
