"""
Common interface and shared plumbing for TTS engines.

Engines implement `_load`, `_resolve_item` and `_synthesize_audio` (plus
`cache_key`); everything else — lifecycle state, WAV caching, single-item
generation and a sequential `generate_batch` — lives here. Engines that can
batch natively override `generate_batch`.
"""

import gc
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

AUDIO_CACHE_DIR = str(Path(__file__).resolve().parent.parent / "audio_cache")
SAMPLE_RATE = 24_000
_SILENCE_FALLBACK_FRAMES = int(0.3 * SAMPLE_RATE)


class BaseEngine:
    engine_name = ""
    # Identifies the audio this engine produces. Segment audio recorded under a
    # different tag is stale (other engine, or changed output processing).
    cache_tag = ""

    def __init__(self):
        self._lock = threading.Lock()
        self._loading = False
        self._ready = False
        self._error: str | None = None
        self._closed = False
        os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

    # ── lifecycle ────────────────────────────────────────────────────────
    def status(self) -> dict:
        if self._error:
            return {"state": "error", "message": self._error}
        if self._ready:
            return {"state": "ready"}
        if self._loading:
            return self._loading_status()
        return self._idle_status()

    def _loading_status(self) -> dict:
        return {"state": "loading"}

    def _idle_status(self) -> dict:
        return {"state": "not_loaded"}

    def load_async(self):
        if self._ready or self._loading:
            return
        if self._closed:
            return
        threading.Thread(target=self._run_load, daemon=True).start()

    def _run_load(self):
        self._load()
        if self._closed:  # closed while loading: don't keep the model alive
            self.close()

    def reload(self):
        self._teardown()
        self._error = None
        self.load_async()

    def _teardown(self):
        with self._lock:
            self._ready = False
            self._unload()

    def close(self):
        """Permanently shut this engine down and free its model.

        Takes the lock, so it waits for any in-flight synthesis call.
        """
        self._closed = True
        self._teardown()
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass

    def _load(self):
        raise NotImplementedError

    def _unload(self):
        """Drop loaded model state (called with self._lock held)."""

    def apply_settings(self, updates: dict):
        """React to changed app settings. No-op by default."""

    # ── engine-specific hooks ────────────────────────────────────────────
    @staticmethod
    def cache_key(text, instruct, ref_audio, speed, ref_text=None) -> str:
        raise NotImplementedError

    def _resolve_item(self, text, instruct, ref_audio, speed, ref_text=None, vd_cache=None) -> tuple:
        """Return (eff_instruct, eff_ref_audio, eff_ref_text, cache_key, cache_path)."""
        raise NotImplementedError

    def _synthesize_audio(self, text, instruct=None, ref_audio=None, ref_text=None,
                          speed=1.0, num_step=32) -> np.ndarray:
        raise NotImplementedError

    # ── shared helpers ───────────────────────────────────────────────────
    @staticmethod
    def cache_path(key: str) -> str:
        return os.path.join(AUDIO_CACHE_DIR, f"{key}.wav")

    @staticmethod
    def _normalise_audio(audio) -> np.ndarray:
        if not isinstance(audio, np.ndarray):
            audio = np.array(audio)
        if audio.ndim > 1:
            audio = audio.mean(axis=0)
        return audio


    @staticmethod
    def _cached_duration(path: str) -> float | None:
        """Duration in seconds of a valid cached WAV, or None if missing.
        Zero-length files are deleted (and None returned) so they get re-synthesized.
        Reads only the header."""
        try:
            info = sf.info(path)
        except (RuntimeError, OSError):
            return None
        if info.frames == 0:
            log.warning("Deleting zero-length cached WAV at %s, re-synthesizing", path)
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        return info.frames / info.samplerate

    def _synthesize_one(
        self, text: str, instruct: str | None, ref_audio: str | None,
        ref_text: str | None, speed: float, num_step: int,
    ) -> np.ndarray:
        """Synthesise one item; returns a silence fallback if text is empty or model output is zero-length."""
        if not text or not text.strip():
            log.warning("Empty text passed to synthesis — using silence fallback")
            return np.zeros(_SILENCE_FALLBACK_FRAMES, dtype=np.float32)
        audio = self._synthesize_audio(
            text=text, instruct=instruct, ref_audio=ref_audio,
            ref_text=ref_text, speed=speed, num_step=num_step,
        )
        if len(audio) == 0:
            log.warning("Zero-length audio for %r — using silence fallback", text[:50])
            audio = np.zeros(_SILENCE_FALLBACK_FRAMES, dtype=np.float32)
        return audio

    def generate(
        self,
        text: str,
        instruct: str | None = None,
        ref_audio: str | None = None,
        ref_text: str | None = None,
        speed: float = 1.0,
        num_step: int = 32,
    ) -> dict:
        """
        Returns:
            {
                audio_path: str,
                duration_sec: float,
                cache_hit: bool,
                cache_key: str,
            }
        """
        effective_instruct, effective_ref_audio, effective_ref_text, key, path = self._resolve_item(
            text, instruct, ref_audio, speed, ref_text=ref_text
        )

        cached_sec = self._cached_duration(path)
        if cached_sec is not None:
            return {
                "audio_path": path,
                "duration_sec": cached_sec,
                "cache_hit": True,
                "cache_key": key,
                "generation_sec": 0.0,
            }

        t0 = time.time()
        audio = self._synthesize_one(
            text=text,
            instruct=effective_instruct,
            ref_audio=effective_ref_audio,
            ref_text=effective_ref_text,
            speed=speed,
            num_step=num_step,
        )
        sf.write(path, audio, SAMPLE_RATE)

        return {
            "audio_path": path,
            "duration_sec": len(audio) / SAMPLE_RATE,
            "cache_hit": False,
            "cache_key": key,
            "generation_sec": round(time.time() - t0, 2),
        }

    def generate_preview(
        self,
        instruct: str,
        sample_text: str,
        ref_audio: str | None = None,
        ref_text: str | None = None,
    ) -> dict:
        return self.generate(
            text=sample_text,
            instruct=instruct,
            ref_audio=ref_audio,
            ref_text=ref_text,
            speed=1.0,
            num_step=24,
        )

    def generate_batch(self, items: list[dict], batch_duration: float = 60.0,
                       num_step: int = 32, progress_cb=None) -> list[dict | None]:
        """Generate each item in order. Failed items get None."""
        progress_cb = progress_cb or (lambda: None)
        results: list[dict | None] = []
        for item in items:
            try:
                results.append(self.generate(
                    text=item["text"], instruct=item.get("instruct"),
                    ref_audio=item.get("ref_audio"), ref_text=item.get("ref_text"),
                    speed=float(item.get("speed") or 1.0), num_step=num_step))
            except Exception as exc:
                log.warning("Synthesis failed: %s", exc)
                results.append(None)
            progress_cb()
        return results
