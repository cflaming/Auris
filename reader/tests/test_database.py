"""Schema creation and migrations."""

import logging
import sqlite3

import core.database as db

EXPECTED_COLUMNS = {
    "id", "book_id", "chapter_id", "segment_index", "text", "enriched_text", "character_name",
    "instruct", "speed", "is_dialogue", "paragraph", "span", "anchor", "overridden",
    "audio_path", "duration_sec", "cache_key", "generation_sec", "engine_tag",
}


def columns(path, table="tts_segments"):
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def table_sql(path, table="tts_segments"):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]


def seed_chapter(path):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO books (id, title, file_path, file_type) VALUES (1, 'B', 'b.txt', 'txt')")
        conn.execute("INSERT INTO chapters (id, book_id, title, order_num, content) VALUES (1, 1, 'C', 1, 'x')")


def test_fresh_database_has_every_column_and_no_unique_cache_key(tmp_path, monkeypatch, caplog):
    path = str(tmp_path / "fresh.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    with caplog.at_level(logging.WARNING):
        db.init_db()
        db.init_db()                                           # idempotent
    assert columns(path) == EXPECTED_COLUMNS
    assert "UNIQUE" not in table_sql(path).upper()
    assert "migration failed" not in caplog.text


def test_identical_sentences_can_share_a_cache_key(tmp_path, monkeypatch):
    path = str(tmp_path / "fresh.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    seed_chapter(path)
    with sqlite3.connect(path) as conn:
        for i in (0, 1):
            conn.execute(
                "INSERT INTO tts_segments (book_id, chapter_id, segment_index, text, enriched_text, cache_key) "
                "VALUES (1, 1, ?, '\"No.\"', '\"No.\"', 'samekey')", (i,))
        assert conn.execute("SELECT COUNT(*) FROM tts_segments WHERE cache_key='samekey'").fetchone()[0] == 2


LEGACY_TTS_SEGMENTS = """
CREATE TABLE tts_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL, text TEXT NOT NULL, enriched_text TEXT NOT NULL,
    character_name TEXT, instruct TEXT, speed REAL DEFAULT 1.0, is_dialogue INTEGER DEFAULT 0,
    audio_path TEXT, duration_sec REAL, cache_key TEXT UNIQUE
)"""


def test_legacy_unique_cache_key_is_migrated_without_losing_rows(tmp_path, monkeypatch, caplog):
    path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    seed_chapter(path)
    with sqlite3.connect(path) as conn:                        # swap in the old-style table
        conn.execute("DROP TABLE tts_segments")
        conn.execute(LEGACY_TTS_SEGMENTS)
        conn.execute(
            "INSERT INTO tts_segments (id, book_id, chapter_id, segment_index, text, enriched_text, "
            "character_name, speed, cache_key) VALUES (7, 1, 1, 0, 'Hi.', 'Hi.', 'Amy', 1.2, 'k1')")

    with caplog.at_level(logging.WARNING):
        db.init_db()

    assert "migration failed" not in caplog.text
    assert columns(path) == EXPECTED_COLUMNS
    assert "UNIQUE" not in table_sql(path).upper()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tts_segments").fetchone()
        assert (row["id"], row["text"], row["character_name"], row["speed"], row["cache_key"]) == (7, "Hi.", "Amy", 1.2, "k1")
        assert (row["span"], row["overridden"], row["anchor"], row["engine_tag"]) == (0, 0, None, None)
        conn.execute(                                          # the constraint is really gone
            "INSERT INTO tts_segments (book_id, chapter_id, segment_index, text, enriched_text, cache_key) "
            "VALUES (1, 1, 1, 'Hi.', 'Hi.', 'k1')")


def test_cascade_delete_still_works_after_migration(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    seed_chapter(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE tts_segments")
        conn.execute(LEGACY_TTS_SEGMENTS)
        conn.execute("INSERT INTO tts_segments (book_id, chapter_id, segment_index, text, enriched_text) "
                     "VALUES (1, 1, 0, 'a', 'a')")
    db.init_db()
    with db.get_conn() as conn:                                # get_conn enables foreign keys
        conn.execute("DELETE FROM books WHERE id=1")
        assert conn.execute("SELECT COUNT(*) FROM tts_segments").fetchone()[0] == 0
