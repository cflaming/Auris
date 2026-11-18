"""
EngineManager — owns the active TTS engine and swaps it safely.

App code talks to a single long-lived `EngineManager`; every other attribute
(status, generate, generate_batch, load_async, ...) is forwarded to whichever
engine is current, so call sites never rebind or hold a stale engine.
Switching engines waits for any in-flight synthesis call, then unloads the
old model before the new one starts loading.
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
        name = app_settings.get("tts_engine", app_settings.DEFAULTS["tts_engine"])
        if name not in ENGINES:
            name = app_settings.DEFAULTS["tts_engine"]
        self._engine = _create(name)

    @property
    def engine_name(self) -> str:
        return self._engine.engine_name

    def set_engine(self, name: str) -> bool:
        """Switch to `name` and start loading it. Returns False if already active."""
        if name not in ENGINES:
            raise ValueError(f"Unknown TTS engine: {name!r}")
        with self._swap_lock:
            if name == self._engine.engine_name:
                return False
            old, self._engine = self._engine, _create(name)
            old.close()  # waits for an in-flight synthesis call, then frees the model
            self._engine.load_async()
            return True

    def __getattr__(self, attr):
        # Only reached for attributes not defined on the manager itself.
        return getattr(self._engine, attr)
