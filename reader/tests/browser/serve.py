"""A throwaway Auris server for the browser tests: its own data folder, a tone-generating TTS engine
(real WAVs, real caching, real ffmpeg conversion, but no model), a seeded book, and request counting.

    python tests/browser/serve.py --data-dir DIR --port N

Extra endpoints, test-only (they exist only in this script, not in the app):
    GET  /__stats    requests per route, tone-engine encodes, fmp4 conversions
    POST /__reset    wipe everything and seed the book again (returns its ids)
"""

import argparse
import hashlib
import os
import sys
import threading
from collections import Counter
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]


def build_story(paragraphs: int = 60) -> str:
    """Long enough that "the next ten sentences" and "the rest of the chapter" are different things."""
    speakers = ["Amy", "Carol", "Dean"]
    out = []
    for n in range(paragraphs):
        who = speakers[n % 3]
        out.append(f'Step {n} was taken in the grey light. "Line {n} is mine," {who} said. The rain kept falling.')
    return "\n\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()

    os.environ["AURIS_DATA_DIR"] = args.data_dir          # before anything from core/ or app is imported
    sys.path.insert(0, str(APP_DIR))
    import numpy as np
    import app as app_mod
    from core import database as db
    from core.base_engine import AUDIO_CACHE_DIR, BaseEngine, SAMPLE_RATE
    from core.database import get_conn
    from flask import jsonify, request
    from tests.demo_story import CHARACTERS
    from tests.helpers import seed_book

    class ToneEngine(BaseEngine):
        engine_name = "tone"

        def __init__(self):
            super().__init__()
            self._ready = True
            self.encoded_texts = []

        @staticmethod
        def cache_key(text, instruct, ref_audio, speed, ref_text=None):
            return hashlib.md5(repr((text, instruct, ref_audio, speed)).encode()).hexdigest()

        def _resolve_item(self, text, instruct, ref_audio, speed, ref_text=None, vd_cache=None):
            key = self.cache_key(text, instruct, ref_audio, speed)
            return instruct, ref_audio, ref_text, key, self.cache_path(key)

        def _synthesize_audio(self, text, instruct=None, ref_audio=None, ref_text=None, speed=1.0, num_step=32):
            self.encoded_texts.append(text)
            seconds = max(0.5, len(text) * 0.03)
            t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
            return (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    engine = ToneEngine()
    app_mod.tts = engine
    db.init_db()

    def clear_audio():
        for f in Path(AUDIO_CACHE_DIR).glob("*"):
            if f.is_file():
                f.unlink()

    def seed() -> dict:
        with get_conn() as conn:
            conn.execute("DELETE FROM books")
        second = 'A short second chapter. "Hello," Amy said.'
        book_id, chapter_ids = seed_book(CHARACTERS, (build_story(), second), title="Browser test book")
        return {"book_id": book_id, "chapter_ids": chapter_ids}

    clear_audio()
    ids = seed()
    stats = Counter()
    stats_lock = threading.Lock()

    @app_mod.app.after_request
    def _tally(resp):
        if not request.path.startswith("/__"):
            route = request.url_rule.rule if request.url_rule else "(none)"
            with stats_lock:
                stats[f"requests {request.method} {route}"] += 1
        return resp

    @app_mod.app.get("/__stats")
    def _stats():
        with stats_lock:
            data = dict(stats)
        data["encodes"] = len(engine.encoded_texts)
        data["fmp4_conversions"] = len(list(Path(AUDIO_CACHE_DIR).glob("*.fmp4")))
        data["encoded_texts"] = list(engine.encoded_texts)
        return jsonify(data)

    @app_mod.app.post("/__reset")
    def _reset():
        nonlocal ids
        clear_audio()
        with stats_lock:
            stats.clear()
        engine.encoded_texts.clear()
        ids = seed()
        return jsonify(ids)

    @app_mod.app.get("/__ids")
    def _ids():
        return jsonify(ids)

    app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
