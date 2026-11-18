"""Where Auris keeps its data, read once when this module is first imported.

By default everything lives next to the app (reader/data, audio_cache, uploads, exports).
Set AURIS_DATA_DIR to move all of it under another folder with the same layout; the dev server
(dev_server.py) does this to run against a scratch copy of your data.

The variable must therefore be set before any core module is imported. That ordering is
deliberate and a little fragile, which is why dev_server.py refuses to start unless every path
below, and the ones the modules actually use, turn out to be inside its scratch folder.
"""

from core.layout import APP_DIR, layout, resolve_root

DATA_ROOT = resolve_root()
LOCATIONS = layout(DATA_ROOT)          # all of the above, by name

DB_PATH = LOCATIONS["database"]
SETTINGS_FILE = LOCATIONS["settings"]
AUDIO_CACHE_DIR = LOCATIONS["audio_cache"]
UPLOAD_DIR = LOCATIONS["uploads"]
EXPORTS_DIR = LOCATIONS["exports"]
