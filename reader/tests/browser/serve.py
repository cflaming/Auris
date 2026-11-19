"""A throwaway Auris server for the browser tests: its own data folder, a tone-generating TTS engine
(real WAVs, real caching, real ffmpeg conversion, but no model), a seeded book, and request counting.

    python tests/browser/serve.py --data-dir DIR --port N

Extra endpoints, test-only (they exist only in this script, not in the app):
    GET  /__stats    requests per route, bytes sent, tone-engine encodes, fmp4 conversions
    POST /__reset    wipe everything and seed the book again (returns its ids)
"""

import argparse
import hashlib
import os
import sys
import threading
import time
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
    ap.add_argument("--generate-ms", type=int, default=0, help="how long each generation takes (one at a time, like a GPU)")
    args = ap.parse_args()

    os.environ["AURIS_DATA_DIR"] = args.data_dir          # before anything from core/ or app is imported
    sys.path.insert(0, str(APP_DIR))
    import numpy as np
    import app as app_mod
    from core import database as db
    from core.base_engine import AUDIO_CACHE_DIR, BaseEngine, SAMPLE_RATE
    from core.characters import generate_voice_profile
    from core.database import get_conn
    from flask import g, jsonify, request

    class ToneEngine(BaseEngine):
        engine_name = "tone"

        def __init__(self):
            super().__init__()
            self._ready = True
            self.encodes = 0
            self.encoded_texts = []
            self._gpu = threading.Lock()                  # one generation at a time, as with a real model

        @staticmethod
        def cache_key(text, instruct, ref_audio, speed, ref_text=None):
            return hashlib.md5(repr((text, instruct, ref_audio, speed)).encode()).hexdigest()

        def _resolve_item(self, text, instruct, ref_audio, speed, ref_text=None, vd_cache=None):
            key = self.cache_key(text, instruct, ref_audio, speed)
            return instruct, ref_audio, ref_text, key, self.cache_path(key)

        def _synthesize_audio(self, text, instruct=None, ref_audio=None, ref_text=None, speed=1.0, num_step=32):
            with self._gpu:
                self.encodes += 1
                self.encoded_texts.append(text)
                if args.generate_ms:
                    time.sleep(args.generate_ms / 1000)
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
            book_id = conn.execute(
                "INSERT INTO books (title, file_path, file_type) VALUES ('Browser test book', 'story.txt', 'txt')"
            ).lastrowid
            chapter_ids = []
            for n in (1, 2):
                text = build_story() if n == 1 else 'A short second chapter. "Hello," Amy said.'
                chapter_ids.append(conn.execute(
                    "INSERT INTO chapters (book_id, title, order_num, content, word_count) VALUES (?,?,?,?,?)",
                    (book_id, f"Chapter {n}", n, text, len(text.split()))).lastrowid)
            for name, gender in (("Amy", "female"), ("Carol", "female"), ("Dean", "male")):
                profile = generate_voice_profile(name, gender)
                conn.execute(
                    "INSERT INTO characters (book_id, name, gender, frequency, instruct, color_hex) VALUES (?,?,?,?,?,?)",
                    (book_id, name, gender, 1, profile["instruct"], profile["color_hex"]))
        return {"book_id": book_id, "chapter_ids": chapter_ids}

    clear_audio()
    ids = seed()
    stats = Counter()
    stats_lock = threading.Lock()

    @app_mod.app.before_request
    def _count():
        g.t0 = time.time()

    @app_mod.app.after_request
    def _tally(resp):
        if not request.path.startswith("/__"):
            route = request.url_rule.rule if request.url_rule else "(none)"
            with stats_lock:
                stats[f"requests {request.method} {route}"] += 1
                if not resp.direct_passthrough and resp.content_length:
                    stats["bytes"] += resp.content_length
        return resp

    @app_mod.app.get("/__stats")
    def _stats():
        with stats_lock:
            data = dict(stats)
        data["encodes"] = engine.encodes
        data["fmp4_conversions"] = len(list(Path(AUDIO_CACHE_DIR).glob("*.fmp4")))
        data["encoded_texts"] = list(engine.encoded_texts)
        return jsonify(data)

    @app_mod.app.post("/__reset")
    def _reset():
        nonlocal ids
        clear_audio()
        with stats_lock:
            stats.clear()
        engine.encodes, engine.encoded_texts = 0, []
        ids = seed()
        return jsonify(ids)

    @app_mod.app.get("/__ids")
    def _ids():
        return jsonify(ids)

    app_mod.app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
