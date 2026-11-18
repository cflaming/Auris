"""Where Auris keeps its data.

By default everything lives next to the app (reader/data, audio_cache, uploads, exports).
Set AURIS_DATA_DIR to move all of it under another folder with the same layout; the dev
server (dev_server.py) uses this to run against a scratch copy of your data. The variable
must be set before any core module is imported, since these are read at import time.
"""

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("AURIS_DATA_DIR") or APP_DIR).expanduser().resolve()

DB_PATH = DATA_ROOT / "data" / "reader.db"
SETTINGS_FILE = DATA_ROOT / "data" / "settings.json"
AUDIO_CACHE_DIR = DATA_ROOT / "audio_cache"
UPLOAD_DIR = DATA_ROOT / "uploads"
EXPORTS_DIR = DATA_ROOT / "exports"
