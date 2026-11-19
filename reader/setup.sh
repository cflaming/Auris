#!/usr/bin/env bash
# Auris - Linux / macOS setup: creates .venv from uv.lock with the right PyTorch build.
#
# Usage: bash setup.sh                  first run: picks the build for this machine and remembers it
#        bash setup.sh                  later runs: re-syncs with the remembered build (e.g. after a git pull)
#        bash setup.sh --extra cpu      choose explicitly (cpu | cu128 | default) and remember that
# Anything after the choice is passed to `uv sync` (for example --dry-run).
#
# The choice lives in .auris-extra. Delete it to have the machine detected again.

set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. Get it from https://docs.astral.sh/uv/"
    exit 1
fi

CHOICE=""
if [ "$1" = "--extra" ]; then
    CHOICE="$2"
    shift 2
elif [ -f .auris-extra ]; then
    CHOICE="$(cat .auris-extra)"
    echo "Using the remembered PyTorch build: $CHOICE (bash setup.sh --extra <cpu|cu128|default> to change)"
fi

if [ -z "$CHOICE" ]; then
    if [ "$(uname -s)" = "Darwin" ]; then
        CHOICE="default"                               # PyPI's macOS build (Apple Silicon: MPS)
    elif command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
        CHOICE="cu128"
    else
        CHOICE="cpu"
    fi
    echo "Detected this machine: PyTorch build $CHOICE"
fi

case "$CHOICE" in
    cpu|cu128) EXTRA=(--extra "$CHOICE") ;;
    default)   EXTRA=() ;;
    *) echo "Unknown PyTorch build '$CHOICE' (use cpu, cu128 or default)"; exit 1 ;;
esac

uv sync "${EXTRA[@]}" "$@"
case " $* " in *" --dry-run "*) ;; *) echo "$CHOICE" > .auris-extra ;; esac     # a dry run changes nothing, remembering included
echo ""
echo "Done. Start Auris with: bash run.sh"
