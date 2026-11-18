"""Kokoro's pure helpers: silence trimming and voice choice. No model is loaded."""

import numpy as np

from core.base_engine import SAMPLE_RATE
from core.kokoro_engine import KokoroEngine, pick_voice, trim_silence

SR = SAMPLE_RATE


def clip(lead, speech, trail):
    return np.concatenate([np.zeros(int(lead * SR)), np.full(int(speech * SR), 0.5), np.zeros(int(trail * SR))])


def test_trim_keeps_all_speech_plus_a_short_pad():
    trimmed = trim_silence(clip(0.4, 1.0, 0.5))
    assert np.count_nonzero(trimmed) == int(1.0 * SR)          # no speech cut
    assert 1.15 < len(trimmed) / SR < 1.25                     # 1.0s + ~0.04 lead + ~0.15 tail


def test_trim_of_pure_silence_is_returned_as_is():
    silence = np.zeros(SR)
    assert trim_silence(silence) is silence


def test_pick_voice_is_deterministic_and_matches_gender_and_accent():
    assert pick_voice("female, young adult") == pick_voice("female, young adult")
    assert pick_voice("male, elderly, low pitch, british accent").startswith("bm_")
    assert pick_voice("female, middle-aged, american accent").startswith("af_")
    assert pick_voice("male, young adult, american accent").startswith("am_")
    assert pick_voice(None).startswith("af_")          # no hints: female American default


def test_cache_key_depends_on_voice_text_and_speed_not_reference_audio():
    key = KokoroEngine.cache_key
    base = key("Hello.", "male", None, 1.0)
    assert base == key("Hello.", "male", "/some/ref.wav", 1.0)     # cloning is ignored by Kokoro
    assert base != key("Hello!", "male", None, 1.0)
    assert base != key("Hello.", "female", None, 1.0)
    assert base != key("Hello.", "male", None, 1.1)

