import logging
import sqlite3
import os
from contextlib import contextmanager

from core import paths

DB_PATH = str(paths.DB_PATH)


def get_db_path():
    return os.path.abspath(DB_PATH)


@contextmanager
def get_conn():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _tts_segments_ddl(table: str) -> str:
    """Schema for the segment table. cache_key is deliberately not UNIQUE: identical
    sentences (same text and voice) in different places share a key."""
    return f"""CREATE TABLE {table} (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id       INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            chapter_id    INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
            segment_index INTEGER NOT NULL,
            text          TEXT NOT NULL,
            enriched_text TEXT NOT NULL,
            character_name TEXT,
            instruct      TEXT,
            speed         REAL DEFAULT 1.0,
            is_dialogue   INTEGER DEFAULT 0,
            paragraph     INTEGER DEFAULT 0,
            span          INTEGER DEFAULT 0,
            anchor        TEXT,
            overridden    INTEGER DEFAULT 0,
            audio_path    TEXT,
            duration_sec  REAL,
            cache_key     TEXT,
            generation_sec REAL,
            engine_tag    TEXT
        )"""


def init_db():
    os.makedirs(os.path.dirname(get_db_path()), exist_ok=True)
    with get_conn() as conn:
        conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS books (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            author      TEXT DEFAULT 'Unknown',
            file_path   TEXT NOT NULL,
            file_type   TEXT NOT NULL,
            cover_b64   TEXT,
            language    TEXT DEFAULT 'en',
            narrator_instruct TEXT,
            single_narrator_mode INTEGER DEFAULT 0,
            added_at    TEXT DEFAULT (datetime('now')),
            last_read   TEXT,
            total_chapters INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS chapters (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id             INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            title               TEXT NOT NULL,
            order_num           INTEGER NOT NULL,
            section_type        TEXT DEFAULT 'chapter',
            content             TEXT NOT NULL,
            word_count          INTEGER DEFAULT 0,
            enrichment_version  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS characters (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id       INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            name          TEXT NOT NULL,
            gender        TEXT DEFAULT 'unknown',
            frequency     INTEGER DEFAULT 1,
            instruct      TEXT,
            ref_audio_path TEXT,
            color_hex     TEXT DEFAULT '#FFFFFF',
            UNIQUE(book_id, name)
        );

        -- Manual speaker corrections. Kept apart from tts_segments, which is rebuilt
        -- whenever enrichment re-runs. anchor = hash of the sentence text plus its
        -- occurrence number in the chapter; character_name NULL means the narrator.
        CREATE TABLE IF NOT EXISTS segment_speakers (
            book_id        INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            chapter_id     INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
            anchor         TEXT NOT NULL,
            character_name TEXT,
            -- Captured when the correction is first made, so a correction is also a labelled
            -- example (what detection said, what is right, and the surrounding text).
            -- text IS NULL means the context was never captured (rows from before this existed).
            detected_speaker TEXT,
            text           TEXT,
            prev_text      TEXT,
            next_text      TEXT,
            paragraph_text TEXT,
            created_at     TEXT,
            updated_at     TEXT,
            PRIMARY KEY (book_id, chapter_id, anchor)
        );

        CREATE TABLE IF NOT EXISTS reading_progress (
            book_id    INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
            chapter_id INTEGER,
            position   INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        {_tts_segments_ddl('IF NOT EXISTS tts_segments')};

        CREATE TABLE IF NOT EXISTS bookmarks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id       INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            chapter_id    INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
            segment_index INTEGER DEFAULT 0,
            text_excerpt  TEXT,
            label         TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        """)

        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(books)").fetchall()
        }
        if "narrator_instruct" not in cols:
            conn.execute("ALTER TABLE books ADD COLUMN narrator_instruct TEXT")
        if "single_narrator_mode" not in cols:
            conn.execute(
                "ALTER TABLE books ADD COLUMN single_narrator_mode INTEGER DEFAULT 0"
            )
        if "narrator_ref_audio_path" not in cols:
            conn.execute("ALTER TABLE books ADD COLUMN narrator_ref_audio_path TEXT")
        if "expression_tags" not in cols:
            conn.execute(
                "ALTER TABLE books ADD COLUMN expression_tags INTEGER DEFAULT 1"
            )

        char_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(characters)").fetchall()
        }
        if "alias_of" not in char_cols:
            # Another character (same book) this one is merged into, e.g. a
            # hero name for a civilian name. Its lines use that voice.
            conn.execute("ALTER TABLE characters ADD COLUMN alias_of INTEGER")
        if "manual" not in char_cols:
            # Added by hand (not found by the detector). Only these can be deleted, and the list of them
            # is a record of what the detector missed.
            conn.execute("ALTER TABLE characters ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE characters ADD COLUMN added_at TEXT")

        seg_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tts_segments)").fetchall()
        }
        if "generation_sec" not in seg_cols:
            conn.execute("ALTER TABLE tts_segments ADD COLUMN generation_sec REAL")
        speaker_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(segment_speakers)").fetchall()
        }
        for col in ("detected_speaker", "text", "prev_text", "next_text", "paragraph_text",
                    "created_at", "updated_at"):
            if col not in speaker_cols:
                conn.execute(f"ALTER TABLE segment_speakers ADD COLUMN {col} TEXT")
        if "span" not in seg_cols:
            conn.execute("ALTER TABLE tts_segments ADD COLUMN span INTEGER DEFAULT 0")
        if "overridden" not in seg_cols:
            conn.execute("ALTER TABLE tts_segments ADD COLUMN overridden INTEGER DEFAULT 0")
        if "anchor" not in seg_cols:
            conn.execute("ALTER TABLE tts_segments ADD COLUMN anchor TEXT")
        if "engine_tag" not in seg_cols:
            conn.execute("ALTER TABLE tts_segments ADD COLUMN engine_tag TEXT")
        if "paragraph" not in seg_cols:
            conn.execute("ALTER TABLE tts_segments ADD COLUMN paragraph INTEGER DEFAULT 0")

        ch_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(chapters)").fetchall()
        }
        if "enrichment_version" not in ch_cols:
            conn.execute("ALTER TABLE chapters ADD COLUMN enrichment_version INTEGER DEFAULT 0")

    _drop_cache_key_unique()


def _cache_key_is_unique(conn) -> bool:
    """True if tts_segments has a UNIQUE index covering cache_key (legacy schema)."""
    for idx in conn.execute("PRAGMA index_list(tts_segments)").fetchall():
        if idx["unique"] and any(c["name"] == "cache_key"
                                 for c in conn.execute(f"PRAGMA index_info({idx['name']})")):
            return True
    return False


def _drop_cache_key_unique():
    """Older databases created tts_segments.cache_key as UNIQUE, so identical sentences
    in different chapters collided. Rebuild such a table without the constraint,
    copying columns by name so it works whatever columns the old table has."""
    conn = sqlite3.connect(get_db_path(), isolation_level=None)  # manual transaction
    conn.row_factory = sqlite3.Row
    try:
        if not _cache_key_is_unique(conn):
            return
        old_cols = [r["name"] for r in conn.execute("PRAGMA table_info(tts_segments)")]
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        try:
            conn.execute("ALTER TABLE tts_segments RENAME TO tts_segments_old")
            conn.execute(_tts_segments_ddl("tts_segments"))
            new_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tts_segments)")}
            shared = ", ".join(c for c in old_cols if c in new_cols)
            conn.execute(f"INSERT INTO tts_segments ({shared}) SELECT {shared} FROM tts_segments_old")
            conn.execute("DROP TABLE tts_segments_old")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    except Exception as exc:
        logging.getLogger(__name__).warning("cache_key UNIQUE migration failed (non-fatal): %s", exc)
    finally:
        conn.close()
