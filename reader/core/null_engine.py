"""An engine that never loads a model or generates audio, for where TTS must not run (the dev
server, the tests). It has the surface the app uses on EngineManager."""


class NullEngine:
    def __init__(self, engine_name="null", cache_tag="null:1", state="loading", message=""):
        self.engine_name, self.cache_tag = engine_name, cache_tag
        self.state, self.message = state, message

    def load_async(self): pass
    def status(self): return {"state": self.state, "message": self.message}
    def apply_settings(self, updates): pass
    def set_engine(self, name): return False
    def close(self): pass
