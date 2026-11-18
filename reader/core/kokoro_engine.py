"""
Kokoro TTS engine (82M params, runs fast on CPU/MPS, no model path setup).

Drop-in replacement for TTSEngine: same public interface (status, load_async,
reload, generate, generate_batch, generate_preview). Kokoro has no voice design
or cloning, so the OmniVoice-style `instruct` string (gender, age, pitch,
accent) is mapped deterministically to one of Kokoro's preset voices.
Reference-audio cloning is not supported and is ignored.
"""

import hashlib
import logging
import re
import time

import numpy as np
import soundfile as sf

from core.tts_engine import (
    SAMPLE_RATE,
    TTSEngine,
    _parse_instruct,
)

log = logging.getLogger(__name__)

KOKORO_REPO = "hexgrad/Kokoro-82M"

# voice id -> (gender, tone) ; tone: "low" | "mid" | "high"
_AMERICAN = {
    "male": {"low": ["am_michael", "am_onyx"], "mid": ["am_adam", "am_eric", "am_liam"],
             "high": ["am_puck", "am_fenix"]},
    "female": {"low": ["af_nicole", "af_nova"], "mid": ["af_heart", "af_bella", "af_sarah"],
               "high": ["af_sky", "af_jessica", "af_kore"]},
}
_BRITISH = {
    "male": {"low": ["bm_george", "bm_lewis"], "mid": ["bm_daniel", "bm_fable"],
             "high": ["bm_fable"]},
    "female": {"low": ["bf_emma"], "mid": ["bf_emma", "bf_isabella"],
               "high": ["bf_isabella", "bf_lily"]},
}

_TAG_RE = re.compile(r"\[[A-Za-z][A-Za-z\- ]*\]")  # OmniVoice expression tags, e.g. [laughter]


def pick_voice(instruct: str | None) -> str:
    parts = _parse_instruct(instruct or "")
    gender = parts["gender"] or "female"
    british = "british" in (parts["accent"] or "") or "australian" in (parts["accent"] or "")
    pitch = parts["pitch"] or ""
    if "low" in pitch or parts["age"] == "elderly" and "high" not in pitch:
        tone = "low"
    elif "high" in pitch or parts["age"] in {"child", "teenager"}:
        tone = "high"
    else:
        tone = "mid"
    pool = (_BRITISH if british else _AMERICAN)[gender][tone]
    h = int(hashlib.md5((instruct or "").encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


class KokoroEngine(TTSEngine):
    engine_name = "kokoro"

    def __init__(self, model_path: str = ""):
        super().__init__(model_path)
        self._pipelines: dict = {}

    def status(self) -> dict:
        if self._error:
            return {"state": "error", "message": self._error}
        if self._ready:
            return {"state": "ready"}
        if self._loading:
            return {"state": "loading", "message": "Loading Kokoro…"}
        return {"state": "not_loaded", "model_path": KOKORO_REPO, "model_exists": True}

    def reload(self):
        with self._lock:
            self._ready = False
            self._error = None
            self._pipelines = {}
        self.load_async()

    def _pipeline(self, lang: str):
        pipe = self._pipelines.get(lang)
        if pipe is None:
            from kokoro import KPipeline
            pipe = KPipeline(lang_code=lang, repo_id=KOKORO_REPO)
            self._pipelines[lang] = pipe
        return pipe

    def _load(self):
        with self._lock:
            if self._ready:
                return
            self._loading = True
            self._error = None
        try:
            self._pipeline("a")  # downloads weights on first run
            self._ready = True
            log.info("Kokoro ready.")
        except Exception as exc:
            self._error = f"Kokoro failed to load: {exc}. Is the 'kokoro' package installed?"
            log.error(self._error)
        finally:
            self._loading = False

    @staticmethod
    def cache_key(text, instruct, ref_audio, speed, ref_text=None) -> str:
        voice = pick_voice(instruct)
        payload = f"kokoro|{voice}|{text}|{speed:.2f}"
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    def _resolve_item(self, text, instruct, ref_audio, speed, ref_text=None, vd_cache=None):
        key = self.cache_key(text, instruct, None, speed)
        return instruct, None, None, key, self.cache_path(key)

    def _synthesize_audio(self, text, instruct=None, ref_audio=None, ref_text=None,
                          speed=1.0, num_step=32) -> np.ndarray:
        voice = pick_voice(instruct)
        clean = _TAG_RE.sub("", text).strip()
        if not clean:
            return np.zeros(0, dtype=np.float32)
        with self._lock:
            if not self._ready:
                raise RuntimeError("Kokoro is not loaded yet. " + (self._error or ""))
            pipe = self._pipeline(voice[0])
            chunks = [np.asarray(a, dtype=np.float32).reshape(-1)
                      for _, _, a in pipe(clean, voice=voice, speed=speed) if a is not None]
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def generate_batch(self, items, batch_duration=60.0, num_step=32, progress_cb=None):
        progress_cb = progress_cb or (lambda: None)
        results = []
        for item in items:
            try:
                results.append(self.generate(
                    text=item["text"], instruct=item.get("instruct"),
                    speed=float(item.get("speed") or 1.0)))
            except Exception as exc:
                log.warning("Kokoro synthesis failed: %s", exc)
                results.append(None)
            progress_cb()
        return results
