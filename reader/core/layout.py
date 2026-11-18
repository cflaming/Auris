"""Where things live under a data folder. Pure functions with no import-time state, so the dev
server can use them before it points AURIS_DATA_DIR at its scratch copy (core/paths.py, by contrast,
reads the variable once at import)."""

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
FILES = ("database", "settings")      # the layout() entries that are files; the rest are folders


def resolve_root(env=None) -> Path:
    """The data folder: AURIS_DATA_DIR if set, else next to the app."""
    env = os.environ if env is None else env
    return Path(env.get("AURIS_DATA_DIR") or APP_DIR).expanduser().resolve()


def layout(root: Path) -> dict[str, Path]:
    """Every place the app keeps data, under `root`."""
    return {
        "database": root / "data" / "reader.db",
        "settings": root / "data" / "settings.json",
        "audio_cache": root / "audio_cache",
        "uploads": root / "uploads",
        "exports": root / "exports",
    }
