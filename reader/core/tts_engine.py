"""
OmniVoice TTS engine wrapper.

Loads the model lazily from the configured model directory.
Caches generated audio by segment hash.
Stabilizes short voice-design generations by reusing a longer
instruction-conditioned reference clip for each instruct string.
"""

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

# Let ops without an Apple GPU (MPS) kernel run on CPU instead of raising.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

AUDIO_CACHE_DIR = str(Path(__file__).resolve().parent.parent / "audio_cache")
VOICE_REF_DIR = os.path.join(AUDIO_CACHE_DIR, "voice_refs")
SAMPLE_RATE = 24_000
VOICE_DESIGN_REF_TEXT = (
    "Hello. This is a stable voice sample for conditioning. "
    "The room is quiet, the day is calm, and every word should sound clear, "
    "natural, and easy to understand."
)
VOICE_REF_MIN_ZCR = 0.015
VOICE_REF_GEN_ATTEMPTS = 4
_SILENCE_FALLBACK_FRAMES = int(0.3 * SAMPLE_RATE)
VOICE_GENDERS = {"male", "female"}
VOICE_AGES = {"child", "teenager", "young adult", "middle-aged", "elderly"}
VOICE_PITCHES = {
    "very low pitch",
    "low pitch",
    "moderate pitch",
    "high pitch",
    "very high pitch",
}
VOICE_STYLES = {"whisper"}


def _model_path_from_settings() -> str:
    try:
        from core.settings import get

        return get("model_path") or ""
    except Exception:
        return str(Path(__file__).resolve().parent.parent.parent / "model_backup" / "OmniVoice")


def _parse_instruct(instruct: str) -> dict:
    parsed = {
        "gender": None,
        "age": None,
        "pitch": None,
        "accent": None,
        "styles": [],
        "extras": [],
    }

    for raw in str(instruct or "").split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item in VOICE_GENDERS:
            parsed["gender"] = item
        elif item in VOICE_AGES:
            parsed["age"] = item
        elif item in VOICE_PITCHES:
            parsed["pitch"] = item
        elif item in VOICE_STYLES:
            parsed["styles"].append(item)
        elif item.endswith("accent"):
            parsed["accent"] = item
        else:
            parsed["extras"].append(item)

    return parsed


def _format_instruct(parts: dict) -> str | None:
    items = []
    for key in ("gender", "age", "pitch", "accent"):
        if parts.get(key):
            items.append(parts[key])
    items.extend(parts.get("styles", []))
    items.extend(parts.get("extras", []))
    return ", ".join(items) if items else None


def _stabilize_voice_design_instruct(instruct: str | None) -> str | None:
    if not instruct:
        return instruct

    parts = _parse_instruct(instruct)
    gender = parts["gender"]
    age = parts["age"]
    pitch = parts["pitch"]
    original = _format_instruct(parts)

    # OmniVoice docs note that some attribute combinations do not work well.
    # Empirically, male teenage/child prompts often collapse into squeals or
    # repeated junk. Simplifying them to a nearby stable voice is much more
    # reliable than passing the raw prompt through.
    if gender == "male" and age in {"teenager", "child"}:
        parts["age"] = None
        if pitch in {None, "moderate pitch", "very high pitch"}:
            parts["pitch"] = "high pitch"
        elif pitch == "very low pitch":
            parts["pitch"] = "low pitch"
    elif gender == "female" and age in {"teenager", "child"}:
        if pitch == "very low pitch":
            parts["pitch"] = "moderate pitch"
        elif age == "teenager" and pitch == "low pitch":
            parts["pitch"] = "moderate pitch"

    effective = _format_instruct(parts)
    if effective != original:
        log.info("Stabilized voice instruct: '%s' -> '%s'", original, effective)
    return effective


def _audio_zcr(audio: np.ndarray) -> float:
    mono = np.asarray(audio, dtype=float).reshape(-1)
    if mono.size < 2:
        return 0.0
    return float(np.mean(np.abs(np.diff(np.signbit(mono)))))


class TTSEngine:
    def __init__(self, model_path: str = ""):
        self.model_path = model_path
        self.model = None
        self._lock = threading.Lock()
        self._loading = False
        self._downloading = False
        self._ready = False
        self._error: str | None = None
        self._voice_clone_prompts: dict = {}  # (path, mtime) -> VoiceClonePrompt
        os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)
        os.makedirs(VOICE_REF_DIR, exist_ok=True)

    def status(self) -> dict:
        resolved_path = self.model_path or _model_path_from_settings()
        if self._error:
            return {"state": "error", "message": self._error}
        if self._ready:
            return {"state": "ready"}
        if self._loading:
            info = {"state": "loading"}
            if self._downloading:
                from core.settings import download_state

                dl = download_state()
                info["downloading"] = True
                info["pct"] = dl.get("pct", 0)
                info["message"] = dl.get("message", "Downloading model…")
            return info
        return {
            "state": "not_loaded",
            "model_path": resolved_path,
            "model_exists": os.path.isdir(resolved_path),
        }

    def load_async(self):
        if self._ready or self._loading:
            return
        threading.Thread(target=self._load, daemon=True).start()

    def reload(self):
        with self._lock:
            self._ready = False
            self._error = None
            self.model = None
        self.load_async()

    def _load(self):
        with self._lock:
            if self._ready:
                return
            self._loading = True
            self._error = None

        try:
            if not self.model_path:
                self.model_path = _model_path_from_settings()

            if not os.path.isdir(self.model_path) and not self._auto_download():
                raise FileNotFoundError(
                    f"Model not found at: {self.model_path}\n"
                    "Go to Settings to set the correct path or download the model."
                )

            import torch
            from omnivoice import OmniVoice

            device = self._pick_device()
            dtype = torch.float32 if device == "cpu" else torch.float16
            log.info("Loading OmniVoice from %s on %s ...", self.model_path, device)
            self.model = OmniVoice.from_pretrained(
                self.model_path,
                device_map=device,
                dtype=dtype,
                local_files_only=True,
            )
            self._ready = True
            log.info("OmniVoice model ready.")
            # One dummy inference to trigger CUDA kernel compilation so the
            # first real request doesn't pay the JIT warmup cost.
            try:
                log.info("Warming up CUDA kernels...")
                self.model.generate(text="Hello.", num_step=4)
                log.info("Warmup complete.")
            except Exception as warm_exc:
                log.warning("Model warmup failed (non-fatal): %s", warm_exc)
        except Exception as exc:
            self._error = str(exc)
            log.error("Failed to load OmniVoice: %s", exc)
        finally:
            self._loading = False
            self._downloading = False

    def _auto_download(self) -> bool:
        """If enabled in Settings, fetch the model from HuggingFace into
        self.model_path and block (this runs on the background load thread)
        until the download finishes. Returns True once the model directory
        is present and ready to load from."""
        from core.settings import download_state, get, start_model_download

        if not get("auto_download_model", False):
            return False

        repo_id = get("model_repo", "k2-fsa/OmniVoice")
        hf_endpoint = get("hf_endpoint", "")
        log.info(
            "Model not found at %s — auto-downloading %s from HuggingFace...",
            self.model_path, repo_id,
        )
        self._downloading = True
        start_model_download(repo_id, self.model_path, hf_endpoint)

        try:
            while True:
                state = download_state()
                status = state.get("status")
                if status == "done":
                    return os.path.isdir(self.model_path)
                if status == "error":
                    self._error = f"Auto-download failed: {state.get('message') or 'unknown error'}"
                    return False
                time.sleep(1.0)
        finally:
            self._downloading = False

    @staticmethod
    def _pick_device() -> str:
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    @staticmethod
    def _ref_fingerprint(path: str) -> str:
        """Return a freshness fingerprint for a ref-audio file (mtime, or '' on error)."""
        try:
            return str(os.path.getmtime(path))
        except OSError:
            return ""

    @staticmethod
    def cache_key(
        text: str,
        instruct: str | None,
        ref_audio: str | None,
        speed: float,
        ref_text: str | None = None,
    ) -> str:
        # Character/narrator ref-audio files are re-uploaded in place at a
        # fixed path (e.g. ref_<char_id>.wav), so the path alone can't tell
        # a fresh clip apart from the one it replaced. Mixing in mtime makes
        # a re-upload produce a new key instead of silently reusing the old
        # cached generation.
        ref_fingerprint = TTSEngine._ref_fingerprint(ref_audio) if ref_audio else ""
        payload = f"{text}|{instruct}|{ref_audio}|{ref_fingerprint}|{ref_text}|{speed:.2f}"
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def cache_path(key: str) -> str:
        return os.path.join(AUDIO_CACHE_DIR, f"{key}.wav")

    @staticmethod
    def _voice_ref_key(instruct: str) -> str:
        return hashlib.md5(instruct.encode("utf-8")).hexdigest()

    def _voice_ref_path(self, instruct: str) -> str:
        return os.path.join(VOICE_REF_DIR, f"{self._voice_ref_key(instruct)}.wav")

    @staticmethod
    def _needs_voice_design_stabilization(text: str, instruct: str | None, ref_audio: str | None) -> bool:
        return bool(instruct and not ref_audio)

    def _get_voice_clone_prompt(self, ref_audio: str, ref_text: str | None):
        """Return a cached VoiceClonePrompt, building it on first use (must hold self._lock)."""
        cache_key = (ref_audio, self._ref_fingerprint(ref_audio))
        prompt = self._voice_clone_prompts.get(cache_key)
        if prompt is None:
            prompt = self.model.create_voice_clone_prompt(ref_audio, ref_text)
            self._voice_clone_prompts = {cache_key: prompt}  # evict stale entries on rebuild
        return prompt

    @staticmethod
    def _normalise_audio(audio) -> np.ndarray:
        if not isinstance(audio, np.ndarray):
            audio = np.array(audio)
        if audio.ndim > 1:
            audio = audio.mean(axis=0)
        return audio

    def _synthesize_audio(
        self,
        text: str,
        instruct: str | None = None,
        ref_audio: str | None = None,
        ref_text: str | None = None,
        speed: float = 1.0,
        num_step: int = 32,
    ) -> np.ndarray:
        with self._lock:
            if not self._ready or self.model is None:
                raise RuntimeError("Model is not loaded yet. " + (self._error or "Call load_async() first."))
            voice_clone_prompt = self._get_voice_clone_prompt(ref_audio, ref_text) if ref_audio is not None else None
            audio_arrays = self.model.generate(
                text=text,
                instruct=instruct,
                voice_clone_prompt=voice_clone_prompt,
                speed=speed,
                num_step=num_step,
            )

        audio = audio_arrays[0] if isinstance(audio_arrays, list) else audio_arrays
        return self._normalise_audio(audio)

    @staticmethod
    def _read_cached_wav(path: str) -> tuple[np.ndarray, int] | None:
        """Return (audio, sr) for a valid cached WAV, or None (deleting the file) if missing/zero-length."""
        if not os.path.exists(path):
            return None
        if sf.info(path).frames == 0:
            log.warning("Deleting zero-length cached WAV at %s, re-synthesizing", path)
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        return sf.read(path)

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

    def _resolve_item(
        self,
        text: str,
        instruct: str | None,
        ref_audio: str | None,
        speed: float,
        ref_text: str | None = None,
        vd_cache: dict | None = None,
    ) -> tuple:
        """Resolve effective params and return (eff_instruct, eff_ref_audio, eff_ref_text, cache_key, cache_path).

        vd_cache: when provided, voice-design stabilisation results are cached by instruct
        string so segments sharing a voice only pay the synthesis cost once.
        """
        eff_instruct = _stabilize_voice_design_instruct(instruct)
        eff_ref_audio, eff_ref_text = ref_audio, ref_text
        if self._needs_voice_design_stabilization(text, eff_instruct, ref_audio):
            if vd_cache is not None and eff_instruct in vd_cache:
                eff_ref_audio, eff_ref_text = vd_cache[eff_instruct]
            else:
                eff_ref_audio, eff_ref_text = self._ensure_voice_design_reference(eff_instruct)
                if vd_cache is not None:
                    vd_cache[eff_instruct] = (eff_ref_audio, eff_ref_text)
        key = self.cache_key(text, eff_instruct, eff_ref_audio, speed, ref_text=eff_ref_text)
        path = self.cache_path(key)
        return eff_instruct, eff_ref_audio, eff_ref_text, key, path

    def _ensure_voice_design_reference(self, instruct: str) -> tuple[str, str]:
        ref_path = self._voice_ref_path(instruct)
        if os.path.exists(ref_path):
            cached_audio, _ = sf.read(ref_path)
            if _audio_zcr(cached_audio) >= VOICE_REF_MIN_ZCR:
                return ref_path, VOICE_DESIGN_REF_TEXT
            log.warning("Discarding unstable cached voice reference for '%s'", instruct)

        best_audio = None
        best_zcr = -1.0

        for attempt in range(VOICE_REF_GEN_ATTEMPTS):
            audio = self._synthesize_audio(
                text=VOICE_DESIGN_REF_TEXT,
                instruct=instruct,
                speed=1.0,
                num_step=24,
            )
            zcr = _audio_zcr(audio)
            if zcr > best_zcr:
                best_audio = audio
                best_zcr = zcr
            if zcr >= VOICE_REF_MIN_ZCR:
                break
            log.warning(
                "Retrying unstable voice reference for '%s' (attempt %d/%d, zcr=%.4f)",
                instruct,
                attempt + 1,
                VOICE_REF_GEN_ATTEMPTS,
                zcr,
            )

        sf.write(ref_path, best_audio, SAMPLE_RATE)
        if best_zcr < VOICE_REF_MIN_ZCR:
            log.warning(
                "Using best-effort voice reference for '%s' despite low zcr=%.4f",
                instruct,
                best_zcr,
            )
        return ref_path, VOICE_DESIGN_REF_TEXT

    def _synthesize_audio_batch(
        self,
        texts: list[str],
        instructs: list[str | None],
        ref_audios: list[str | None],
        ref_texts: list[str | None],
        speeds: list[float],
        num_step: int = 32,
    ) -> list[np.ndarray]:
        """Batch synthesize under one GPU call. Acquires self._lock for the full batch.

        All items in the batch must share the same mode: either all have ref_audio
        (clone mode) or all have ref_audio=None (instruct/auto mode).
        """
        with self._lock:
            if not self._ready or self.model is None:
                raise RuntimeError("Model is not loaded yet. " + (self._error or "Call load_async() first."))
            vcp_list = (
                [self._get_voice_clone_prompt(r, rt) for r, rt in zip(ref_audios, ref_texts)]
                if any(r is not None for r in ref_audios)
                else None
            )
            audio_arrays = self.model.generate(
                text=texts,
                instruct=instructs if any(i is not None for i in instructs) else None,
                voice_clone_prompt=vcp_list,
                speed=speeds if any(s != 1.0 for s in speeds) else None,
                num_step=num_step,
            )

        return [self._normalise_audio(a) for a in audio_arrays]

    # Lazily initialised RuleDurationEstimator singleton (stateless after construction).
    _dur_est = None

    def generate_batch(
        self,
        items: list[dict],
        batch_duration: float = 60.0,
        num_step: int = 32,
    ) -> list[dict | None]:
        """Generate audio for multiple items using cache and true GPU batching.

        Each item: {text, instruct?, ref_audio?, speed?}.
        Returns results in the same order as items.  Failed items get None.
        """
        if TTSEngine._dur_est is None:
            from omnivoice.utils.duration import RuleDurationEstimator
            TTSEngine._dur_est = RuleDurationEstimator()
        dur_est = TTSEngine._dur_est

        results: list[dict | None] = [None] * len(items)
        uncached: list[tuple] = []

        # Phase 1: voice design stabilization + cache check.
        # _ensure_voice_design_reference calls _synthesize_audio which acquires
        # self._lock, so we must do this before any code that also holds the lock.
        # vd_cache avoids redundant synthesis for segments that share a voice instruct.
        vd_cache: dict = {}
        for i, item in enumerate(items):
            text = item["text"]
            instruct = item.get("instruct")
            ref_audio = item.get("ref_audio")
            speed = float(item.get("speed") or 1.0)

            eff_instruct, eff_ref_audio, eff_ref_text, key, path = self._resolve_item(
                text, instruct, ref_audio, speed, vd_cache=vd_cache
            )
            cached = self._read_cached_wav(path)
            if cached is not None:
                data, sr = cached
                results[i] = {"audio_path": path, "duration_sec": len(data) / sr,
                              "cache_hit": True, "cache_key": key, "generation_sec": 0.0}
                continue
            uncached.append((i, text, eff_instruct, eff_ref_audio, eff_ref_text, speed, key, path))

        if not uncached:
            return results

        # Phase 2: estimate total (ref + generated) duration per item for batch sizing.
        # ref_dur_cache avoids re-reading the same ref file header for every segment
        # belonging to the same character.
        ref_dur_cache: dict[str, float] = {}

        def _est_total(text, ref_text, ref_audio):
            ref_dur = 0.0
            if ref_audio:
                if ref_audio not in ref_dur_cache:
                    try:
                        info = sf.info(ref_audio)
                        ref_dur_cache[ref_audio] = info.frames / info.samplerate
                    except Exception:
                        ref_dur_cache[ref_audio] = 0.0
                ref_dur = ref_dur_cache[ref_audio]
            return ref_dur + dur_est.estimate_duration(
                text, ref_text or "Nice to meet you.", max(ref_dur, 0.5), low_threshold=2.0
            )

        # Phase 3: split clone (has ref_audio) vs other (no ref_audio) so that each
        # batch passed to the model is homogeneous — mixing modes crashes the model.
        clone = [u for u in uncached if u[3] is not None]
        other = [u for u in uncached if u[3] is None]

        def _cluster(group):
            with_dur = [(u, _est_total(u[1], u[4], u[3])) for u in group]
            with_dur.sort(key=lambda x: x[1], reverse=True)
            batches, cur, cur_dur = [], [], 0.0
            for u, dur in with_dur:
                if cur and cur_dur + dur > batch_duration:
                    batches.append(cur)
                    cur, cur_dur = [], 0.0
                cur.append(u)
                cur_dur += dur
            if cur:
                batches.append(cur)
            return batches

        # Phase 4: one GPU call per sub-batch, save files.
        for group in (clone, other):
            if not group:
                continue
            for batch in _cluster(group):
                t0 = time.time()
                try:
                    audios = self._synthesize_audio_batch(
                        texts=[u[1] for u in batch],
                        instructs=[u[2] for u in batch],
                        ref_audios=[u[3] for u in batch],
                        ref_texts=[u[4] for u in batch],
                        speeds=[u[5] for u in batch],
                        num_step=num_step,
                    )
                except Exception as exc:
                    log.warning("Batch synthesis failed (%d items), retrying individually: %s", len(batch), exc)
                    for u in batch:
                        orig_idx, text_u, instruct_u, ref_audio_u, ref_text_u, speed_u, key, path = u
                        t1 = time.time()
                        try:
                            audio = self._synthesize_one(text_u, instruct_u, ref_audio_u, ref_text_u, speed_u, num_step)
                            sf.write(path, audio, SAMPLE_RATE)
                            results[orig_idx] = {
                                "audio_path": path,
                                "duration_sec": len(audio) / SAMPLE_RATE,
                                "cache_hit": False,
                                "cache_key": key,
                                "generation_sec": round(time.time() - t1, 2),
                            }
                        except Exception as exc2:
                            log.warning("Individual synthesis also failed for item %d: %s", orig_idx, exc2)
                    continue
                gen_per_item = round((time.time() - t0) / len(batch), 2)
                for u, audio in zip(batch, audios):
                    orig_idx, text_u, inst_u, ref_u, rtext_u, spd_u, key, path = u
                    gen_sec = gen_per_item
                    if len(audio) == 0:
                        log.warning("Zero-length audio in batch for item %d (%r), retrying individually", orig_idx, text_u[:40])
                        t1 = time.time()
                        try:
                            audio = self._synthesize_one(text_u, inst_u, ref_u, rtext_u, spd_u, num_step)
                        except Exception as exc_retry:
                            log.warning("Individual retry also failed for item %d: %s", orig_idx, exc_retry)
                            continue
                        gen_sec = round(time.time() - t1, 2)
                    sf.write(path, audio, SAMPLE_RATE)
                    results[orig_idx] = {
                        "audio_path": path,
                        "duration_sec": len(audio) / SAMPLE_RATE,
                        "cache_hit": False,
                        "cache_key": key,
                        "generation_sec": gen_sec,
                    }

        return results

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

        cached = self._read_cached_wav(path)
        if cached is not None:
            data, sr = cached
            return {
                "audio_path": path,
                "duration_sec": len(data) / sr,
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
