"""
Kokoro TTS engine (82M params, runs fast on CPU/MPS, no model path setup).

Drop-in replacement for TTSEngine: same public interface (status, load_async,
reload, generate, generate_batch, generate_preview). Kokoro has no voice design
or cloning, so the OmniVoice-style `instruct` string (gender, age, pitch,
accent) is mapped deterministically to one of Kokoro's preset voices.
Reference-audio cloning is not supported and is ignored.
"""

import functools
import hashlib
import logging
import re

import numpy as np

from core.base_engine import SAMPLE_RATE, BaseEngine
from core.instruct import parse_instruct

log = logging.getLogger(__name__)

KOKORO_REPO = "hexgrad/Kokoro-82M"

# gender -> tone ("low" | "mid" | "high") -> candidate voice ids
_AMERICAN = {
    "male": {"low": ["am_michael", "am_onyx"], "mid": ["am_adam", "am_eric", "am_liam"],
             "high": ["am_puck", "am_fenix"]},
    "female": {"low": ["af_nicole", "af_nova"], "mid": ["af_heart", "af_bella", "af_sarah"],
               "high": ["af_sky", "af_jessica", "af_kore"]},
}
_BRITISH = {
    "male": {"low": ["bm_george", "bm_lewis"], "mid": ["bm_daniel"],
             "high": ["bm_fable"]},
    "female": {"low": ["bf_emma"], "mid": ["bf_isabella"],
               "high": ["bf_isabella", "bf_lily"]},
}

# Kokoro pads every clip with ~0.4s of leading and ~0.5s of trailing silence, which
# adds up to a long gap between segments. Trim to the speech and keep a short pad.
_SILENCE_THRESHOLD = 0.004   # |amplitude| below this counts as silence
_LEAD_PAD_SEC = 0.04
_TAIL_PAD_SEC = 0.15
_CACHE_VERSION = 2           # bump when output processing changes (invalidates cached WAVs)

_TAG_RE = re.compile(r"\[[A-Za-z][A-Za-z\- ]*\]")  # OmniVoice expression tags, e.g. [laughter]


def trim_silence(audio: np.ndarray) -> np.ndarray:
    loud = np.flatnonzero(np.abs(audio) > _SILENCE_THRESHOLD)
    if loud.size == 0:
        return audio
    start = max(0, loud[0] - int(_LEAD_PAD_SEC * SAMPLE_RATE))
    end = min(len(audio), loud[-1] + 1 + int(_TAIL_PAD_SEC * SAMPLE_RATE))
    return audio[start:end]


@functools.lru_cache(maxsize=256)
def pick_voice(instruct: str | None) -> str:
    parts = parse_instruct(instruct or "")
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


class KokoroEngine(BaseEngine):
    engine_name = "kokoro"
    cache_tag = f"kokoro:{_CACHE_VERSION}"

    def __init__(self):
        super().__init__()
        self._pipelines: dict = {}

    def _loading_status(self) -> dict:
        return {"state": "loading", "message": "Loading Kokoro…"}

    def _idle_status(self) -> dict:
        return {"state": "not_loaded", "model_path": KOKORO_REPO, "model_exists": True}

    def _unload(self):
        self._pipelines = {}

    def _pipeline(self, lang: str):
        return self._pipelines[lang]

    def _load(self):
        with self._lock:
            if self._ready:
                return
            self._loading = True
            self._error = None
        try:
            from kokoro import KModel, KPipeline
            kmodel = KModel(repo_id=KOKORO_REPO).eval()  # downloads weights on first run
            # American ("a") and British ("b") differ only in G2P; share one model.
            self._pipelines = {
                lang: KPipeline(lang_code=lang, repo_id=KOKORO_REPO, model=kmodel)
                for lang in ("a", "b")
            }
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
        payload = f"kokoro{_CACHE_VERSION}|{voice}|{text}|{speed:.2f}"
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
        return trim_silence(np.concatenate(chunks)) if chunks else np.zeros(0, dtype=np.float32)
