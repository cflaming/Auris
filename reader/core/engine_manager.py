"""
EngineManager — owns the active TTS engine and swaps it safely.

App code talks to a single long-lived `EngineManager`; every other attribute
(status, generate, generate_batch, load_async, ...) is forwarded to whichever
engine is current, so call sites never rebind or hold a stale engine.
Switching engines returns immediately; in the background the old engine waits
for its in-flight synthesis call, unloads its model, and only then does the
new engine start loading. A call already dispatched to the old engine (e.g. a
running generate_batch) can still fail once the old engine is closed.
"""

import importlib
import threading

from core import settings as app_settings

# name -> (module, class); imported lazily so unused engines cost nothing.
ENGINES = {
    "kokoro": ("core.kokoro_engine", "KokoroEngine"),
    "omnivoice": ("core.tts_engine", "TTSEngine"),
}


def _create(name: str):
    module, cls = ENGINES[name]
    return getattr(importlib.import_module(module), cls)()


class EngineManager:
    def __init__(self):
        self._swap_lock = threading.Lock()
        name = app_settings.get("tts_engine")
        if name not in ENGINES:
            name = app_settings.DEFAULTS["tts_engine"]
        self._engine = _create(name)

    def set_engine(self, name: str) -> bool:
        """Switch to `name` and start loading it. Returns False if already active."""
        if name not in ENGINES:
            raise ValueError(f"Unknown TTS engine: {name!r}")
        with self._swap_lock:
            if name == self._engine.engine_name:
                return False
            old, new = self._engine, _create(name)
            self._engine = new
            threading.Thread(target=self._retire, args=(old, new), daemon=True).start()
            return True

    @staticmethod
    def _retire(old, new):
        old.close()  # waits for in-flight synthesis, then frees the model
        new.load_async()

    def __getattr__(self, attr):
        # Only reached for attributes not defined on the manager itself.
        return getattr(self._engine, attr)
