"""Small helpers shared by the tests."""

import os
import subprocess
import sys

from core.layout import APP_DIR


def spoken(segs):
    """[(text, speaker)] for the spoken (dialogue) segments."""
    return [(s["text"], s["character_name"]) for s in segs if s["is_dialogue"]]


def by_text(segs, text):
    """The segment with exactly this text."""
    return next(s for s in segs if s["text"] == text)


def all_by_text(segs, text):
    """Every segment with exactly this text, in order."""
    return [s for s in segs if s["text"] == text]


def run_in_app(code, data_dir=None):
    """Run Python code in a fresh interpreter started in the app folder, with AURIS_DATA_DIR set to
    data_dir or (None) not set at all. Modules read it when imported, so this needs a new process."""
    env = {k: v for k, v in os.environ.items() if k != "AURIS_DATA_DIR"}
    if data_dir:
        env["AURIS_DATA_DIR"] = str(data_dir)
    return subprocess.run([sys.executable, "-c", code], cwd=APP_DIR, env=env, capture_output=True, text=True)


def seed_book(characters, chapters, title="Demo"):
    """Insert a book with chapters and characters straight into the database -> (book_id, [chapter ids])."""
    from core.characters import generate_voice_profile
    from core.database import get_conn

    with get_conn() as conn:
        book_id = conn.execute(
            "INSERT INTO books (title, file_path, file_type) VALUES (?, 'demo.txt', 'txt')", (title,)
        ).lastrowid
        chapter_ids = [
            conn.execute(
                "INSERT INTO chapters (book_id, title, order_num, content, word_count) VALUES (?,?,?,?,?)",
                (book_id, f"Chapter {n}", n, text, len(text.split())),
            ).lastrowid
            for n, text in enumerate(chapters, 1)
        ]
        for name, gender in characters:
            profile = generate_voice_profile(name, gender)
            conn.execute(
                "INSERT INTO characters (book_id, name, gender, frequency, instruct, color_hex) "
                "VALUES (?,?,?,?,?,?)",
                (book_id, name, gender, 1, profile["instruct"], profile["color_hex"]),
            )
    return book_id, chapter_ids
