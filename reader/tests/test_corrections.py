"""The speaker-corrections record: what is captured, the list page's data, resetting, backfill."""

import sqlite3

import pytest

from core.database import get_conn
from tests.helpers import by_text

THEN_WHEN = '"Then when?"'                          # detected as Amy; paragraph: '"Then when?" Amy asked.'


def edit_sentence(book):
    """Change the text under a correction made on THEN_WHEN, so its anchor no longer exists."""
    with get_conn() as conn:
        conn.execute("UPDATE chapters SET content=replace(content, 'Then when?', 'Whenever?') WHERE id=?",
                     (book.chapters[0],))


def stored(book):
    """The corrections table rows for a book, as dicts."""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM segment_speakers WHERE book_id=?", (book.id,)).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture
def corrections(client):
    return lambda book: client.get(f"/api/books/{book.id}/corrections").json


def test_a_correction_captures_what_detection_said_and_the_surrounding_text(make_book, segments, correct):
    book = make_book()
    correct(book, [by_text(segments(book), THEN_WHEN)["anchor"]], "Dean")

    (row,) = stored(book)
    assert row["character_name"] == "Dean" and row["detected_speaker"] == "Amy"
    assert row["text"] == THEN_WHEN
    assert row["prev_text"] == '"That is far too early."' and row["next_text"] == "Amy asked."
    assert row["paragraph_text"] == '"Then when?" Amy asked.'
    assert row["created_at"] and row["updated_at"]


def test_narration_detected_as_the_narrator_is_recorded_with_no_speaker(make_book, segments, correct):
    book = make_book()
    correct(book, [by_text(segments(book), "Carol shook her head.")["anchor"]], "Dean")

    (row,) = stored(book)
    assert row["detected_speaker"] is None and row["text"] == "Carol shook her head."   # captured: narrator


def test_correcting_again_keeps_the_original_detection_and_date(make_book, segments, correct):
    book = make_book()
    anchor = by_text(segments(book), THEN_WHEN)["anchor"]
    correct(book, [anchor], "Dean")
    first = stored(book)[0]
    with get_conn() as conn:        # plant values that re-deriving could not reproduce
        conn.execute("UPDATE segment_speakers SET detected_speaker='Original', "
                     "created_at='2020-01-01 00:00:00', updated_at='2020-01-01 00:00:00'")

    correct(book, [anchor], "Carol")
    (row,) = stored(book)
    assert row["character_name"] == "Carol"
    assert row["detected_speaker"] == "Original"               # what was captured the first time, not re-derived
    assert row["created_at"] == "2020-01-01 00:00:00" and row["updated_at"] != "2020-01-01 00:00:00"
    assert row["text"] == first["text"] and row["paragraph_text"] == first["paragraph_text"]


def test_resetting_removes_the_record(make_book, segments, correct):
    book = make_book()
    anchor = by_text(segments(book), THEN_WHEN)["anchor"]
    correct(book, [anchor], "Dean")
    correct(book, [anchor], reset=True)
    assert stored(book) == []


# ── The list ──────────────────────────────────────────────────────────────────

def test_the_list_is_empty_when_nothing_is_corrected(make_book, corrections):
    assert corrections(make_book()) == []


def test_the_list_shows_detected_and_corrected_speakers_in_reading_order(make_book, segments, correct, corrections):
    book = make_book()
    ch1 = segments(book, 0)
    correct(book, [by_text(ch1, '"We should leave before dawn,"')["anchor"]], "Dean")
    correct(book, [by_text(ch1, THEN_WHEN)["anchor"]], None)
    correct(book, [by_text(segments(book, 1), '"Ready,"')["anchor"]], "Dean", chapter_index=1)

    items = corrections(book)
    assert [(c["chapter_title"], c["text"], c["detected_speaker"], c["corrected_speaker"], c["status"]) for c in items] == [
        ("Chapter 1", '"We should leave before dawn,"', "Amy", "Dean", "active"),
        ("Chapter 1", THEN_WHEN, "Amy", None, "active"),                    # corrected to the narrator
        ("Chapter 2", '"Ready,"', "Amy", "Dean", "active"),
    ]
    assert all(c["segment_index"] is not None and c["context_captured"] for c in items)


def test_a_correction_to_a_deleted_character_is_listed_as_ignored(make_book, segments, correct, corrections):
    book = make_book()
    correct(book, [by_text(segments(book), THEN_WHEN)["anchor"]], "Dean")
    with get_conn() as conn:
        conn.execute("DELETE FROM characters WHERE book_id=? AND name='Dean'", (book.id,))
    assert [c["status"] for c in corrections(book)] == ["ignored"]


def test_a_correction_whose_sentence_is_gone_is_listed_as_missing(make_book, segments, correct, corrections):
    book = make_book()
    correct(book, [by_text(segments(book), THEN_WHEN)["anchor"]], "Dean")
    edit_sentence(book)                                        # the text changes under it
    (item,) = corrections(book)
    assert item["status"] == "missing" and item["segment_index"] is None
    assert item["text"] == THEN_WHEN                           # still shows the sentence it was made on


# ── Resetting from the list ───────────────────────────────────────────────────

def delete(client, book, chapter_index=0, **body):
    return client.delete(f"/api/books/{book.id}/corrections", json={"chapter_id": book.chapters[chapter_index], **body})


def test_delete_specific_corrections(make_book, segments, correct, client):
    book = make_book()
    ch1 = segments(book)
    a, b = (by_text(ch1, t)["anchor"] for t in ('"We should leave before dawn,"', THEN_WHEN))
    correct(book, [a], "Dean"); correct(book, [b], "Dean")

    assert delete(client, book, anchors=[a]).json == {"ok": True, "deleted": 1}
    assert [r["anchor"] for r in stored(book)] == [b]
    assert not by_text(segments(book), '"We should leave before dawn,"')["overridden"]      # applied, not just deleted


def test_delete_a_whole_chapter_only_touches_that_chapter(make_book, segments, correct, client):
    book = make_book()
    correct(book, [by_text(segments(book), THEN_WHEN)["anchor"]], "Dean")
    correct(book, [by_text(segments(book, 1), '"Ready,"')["anchor"]], "Dean", chapter_index=1)

    assert delete(client, book).json["deleted"] == 1
    assert [r["chapter_id"] for r in stored(book)] == [book.chapters[1]]


def test_corrections_whose_sentence_is_gone_can_still_be_deleted(make_book, segments, correct, client):
    book = make_book()
    anchor = by_text(segments(book), THEN_WHEN)["anchor"]
    correct(book, [anchor], "Dean")
    edit_sentence(book)
    assert correct(book, [anchor], reset=True).status_code == 404          # the speaker route can't see it any more
    assert delete(client, book, anchors=[anchor]).status_code == 200
    assert stored(book) == []


def test_deleting_invalidates_the_chapters_segments(make_book, segments, correct, client):
    book = make_book()
    correct(book, [by_text(segments(book), THEN_WHEN)["anchor"]], "Dean")
    segments(book)                                             # rebuilt with the correction applied
    delete(client, book)
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tts_segments WHERE chapter_id=?", (book.chapters[0],)).fetchone()[0] == 0


def test_delete_rejects_bad_requests(make_book, client):
    book = make_book()
    assert client.delete(f"/api/books/{book.id}/corrections", json={}).status_code == 400
    assert delete(client, book, anchors="x").status_code == 400
    assert delete(client, book).status_code == 404                         # nothing to delete
    assert delete(client, book, anchors=["nope:0"]).status_code == 404


# ── Corrections made before context was captured ──────────────────────────────

def test_old_corrections_get_their_context_filled_in_on_startup(make_book, segments, client, app_module):
    book = make_book()
    anchor = by_text(segments(book), THEN_WHEN)["anchor"]
    with get_conn() as conn:                                   # a row as it was stored before this feature
        conn.execute("INSERT INTO segment_speakers (book_id, chapter_id, anchor, character_name) VALUES (?,?,?,?)",
                     (book.id, book.chapters[0], anchor, "Dean"))
        conn.execute("INSERT INTO segment_speakers (book_id, chapter_id, anchor, character_name) VALUES (?,?,?,?)",
                     (book.id, book.chapters[0], "gone:0", "Dean"))             # its sentence no longer exists

    app_module._startup_complete = False                       # startup already ran for the first request; run it again
    client.get("/")
    rows = {r["anchor"]: r for r in stored(book)}
    assert (rows[anchor]["detected_speaker"], rows[anchor]["text"], rows[anchor]["prev_text"]) == (
        "Amy", THEN_WHEN, '"That is far too early."')
    assert rows["gone:0"]["text"] is None                       # nothing to fill in from


# ── Pages ─────────────────────────────────────────────────────────────────────

def test_the_corrections_page_and_its_link_from_voice_studio(make_book, segments, correct, client):
    book = make_book()
    page = client.get(f"/corrections/{book.id}")
    assert page.status_code == 200 and b"Speaker corrections" in page.data and b"Demo" in page.data
    assert client.get("/corrections/999").status_code == 404

    studio = lambda: client.get(f"/voice-studio/{book.id}").get_data(as_text=True)
    before = studio()
    assert f"/corrections/{book.id}" in before and "Speaker corrections (" not in before    # no count yet
    correct(book, [by_text(segments(book), THEN_WHEN)["anchor"]], "Dean")
    assert "Speaker corrections (1)" in studio()
