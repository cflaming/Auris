"""Shared fixtures.

Each test gets its own temporary database, settings file and render folder, and a
fake TTS engine, so nothing touches real data and no model is ever loaded.
"""

from types import SimpleNamespace

import pytest

from core.characters import generate_voice_profile
from core.null_engine import NullEngine
from tests.demo_story import CHAPTER_ONE, CHAPTER_TWO, CHARACTERS


def FakeEngine():
    """Stands in for EngineManager: same surface the app uses, no model."""
    return NullEngine("fake", "fake:1", state="ready")


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    import core.database as db
    import core.settings as app_settings

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "reader.db"))
    monkeypatch.setattr(app_settings, "SETTINGS_FILE", tmp_path / "settings.json")

    import app as app_mod
    renders = tmp_path / "chapter_renders"
    renders.mkdir()
    monkeypatch.setattr(app_mod, "_CHAPTER_RENDER_DIR", str(renders))
    monkeypatch.setattr(app_mod, "tts", FakeEngine())
    monkeypatch.setattr(app_mod, "_startup_complete", False)
    app_mod.app.config["TESTING"] = True
    db.init_db()
    return app_mod


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


@pytest.fixture
def make_book(app_module):
    """Insert a book with chapters and characters straight into the database."""
    from core.database import get_conn

    def _make(characters=CHARACTERS, chapters=None):
        chapters = chapters or (CHAPTER_ONE, CHAPTER_TWO)
        with get_conn() as conn:
            book_id = conn.execute(
                "INSERT INTO books (title, file_path, file_type) VALUES ('Demo', 'demo.txt', 'txt')"
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
        return SimpleNamespace(id=book_id, chapters=chapter_ids)

    return _make


@pytest.fixture
def segments(client):
    """segments(book, chapter_index=0) -> the chapter's segment list as the reader sees it."""
    def _get(book, chapter_index=0):
        return client.get(f"/api/tts/segments/{book.id}/{book.chapters[chapter_index]}").json
    return _get


@pytest.fixture
def correct(client):
    """correct(book, anchors, character, reset=False, chapter_index=0) -> response."""
    def _put(book, anchors, character=None, reset=False, chapter_index=0):
        return client.put(
            f"/api/books/{book.id}/chapters/{book.chapters[chapter_index]}/speaker",
            json={"anchors": anchors, "character": character, "reset": reset},
        )
    return _put

