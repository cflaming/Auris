"""Schema creation and migrations."""

import logging
import sqlite3

import pytest

import core.database as db

EXPECTED_COLUMNS = {
    "id", "book_id", "chapter_id", "segment_index", "text", "enriched_text", "character_name",
    "instruct", "speed", "is_dialogue", "paragraph", "span", "anchor", "overridden",
    "audio_path", "duration_sec", "cache_key", "generation_sec", "engine_tag",
}

LEGACY_TTS_SEGMENTS = """
CREATE TABLE tts_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL, text TEXT NOT NULL, enriched_text TEXT NOT NULL,
    character_name TEXT, instruct TEXT, speed REAL DEFAULT 1.0, is_dialogue INTEGER DEFAULT 0,
    audio_path TEXT, duration_sec REAL, cache_key TEXT UNIQUE
)"""


def columns(path):
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(tts_segments)")}


def add_segment(conn, index, text="a", cache_key=None):
    conn.execute(
        "INSERT INTO tts_segments (book_id, chapter_id, segment_index, text, enriched_text, cache_key) "
        "VALUES (1, 1, ?, ?, ?, ?)", (index, text, text, cache_key))


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A newly created database holding one book with one chapter."""
    path = str(tmp_path / "reader.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO books (id, title, file_path, file_type) VALUES (1, 'B', 'b.txt', 'txt')")
        conn.execute("INSERT INTO chapters (id, book_id, title, order_num, content) VALUES (1, 1, 'C', 1, 'x')")
    return path


@pytest.fixture
def legacy_db(fresh_db):
    """The same database, but with the old tts_segments table (UNIQUE cache_key, 13 columns)."""
    with sqlite3.connect(fresh_db) as conn:
        conn.execute("DROP TABLE tts_segments")
        conn.execute(LEGACY_TTS_SEGMENTS)
    return fresh_db


def test_fresh_database_has_every_column_and_allows_shared_cache_keys(fresh_db, caplog):
    with caplog.at_level(logging.WARNING):
        db.init_db()                                           # idempotent
    assert columns(fresh_db) == EXPECTED_COLUMNS
    with sqlite3.connect(fresh_db) as conn:                    # identical sentences share a key
        add_segment(conn, 0, '"No."', "samekey")
        add_segment(conn, 1, '"No."', "samekey")
    assert "migration failed" not in caplog.text


def test_legacy_unique_cache_key_is_migrated_without_losing_rows(legacy_db, caplog):
    with sqlite3.connect(legacy_db) as conn:
        conn.execute(
            "INSERT INTO tts_segments (id, book_id, chapter_id, segment_index, text, enriched_text, "
            "character_name, speed, cache_key) VALUES (7, 1, 1, 0, 'Hi.', 'Hi.', 'Amy', 1.2, 'k1')")

    with caplog.at_level(logging.WARNING):
        db.init_db()

    assert "migration failed" not in caplog.text
    assert columns(legacy_db) == EXPECTED_COLUMNS
    with sqlite3.connect(legacy_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tts_segments").fetchone()
        assert (row["id"], row["text"], row["character_name"], row["speed"], row["cache_key"]) == (7, "Hi.", "Amy", 1.2, "k1")
        assert (row["span"], row["overridden"], row["anchor"], row["engine_tag"]) == (0, 0, None, None)
        add_segment(conn, 1, "Hi.", "k1")                      # the constraint is really gone


def test_cascade_delete_still_works_after_migration(legacy_db):
    with sqlite3.connect(legacy_db) as conn:
        add_segment(conn, 0)
    db.init_db()
    with db.get_conn() as conn:                                # get_conn enables foreign keys
        conn.execute("DELETE FROM books WHERE id=1")
        assert conn.execute("SELECT COUNT(*) FROM tts_segments").fetchone()[0] == 0


def test_old_correction_tables_gain_the_context_columns(fresh_db):
    with sqlite3.connect(fresh_db) as conn:                    # the table as it was before context was captured
        conn.execute("DROP TABLE segment_speakers")
        conn.execute("CREATE TABLE segment_speakers (book_id INTEGER NOT NULL, chapter_id INTEGER NOT NULL, "
                     "anchor TEXT NOT NULL, character_name TEXT, PRIMARY KEY (book_id, chapter_id, anchor))")
        conn.execute("INSERT INTO segment_speakers VALUES (1, 1, 'abc:0', 'Amy')")
    db.init_db()
    with sqlite3.connect(fresh_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM segment_speakers").fetchone()
    assert {"detected_speaker", "text", "prev_text", "next_text", "paragraph_text", "created_at", "updated_at"} <= set(row.keys())
    assert (row["anchor"], row["character_name"], row["text"]) == ("abc:0", "Amy", None)   # data kept, context empty
