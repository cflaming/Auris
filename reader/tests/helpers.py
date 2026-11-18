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
