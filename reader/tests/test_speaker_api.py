"""Speaker corrections and character management through the HTTP API."""

from core.database import get_conn
import pytest

from tests.demo_story import AMBIGUOUS_REPEAT, CHAPTER_ONE
from tests.helpers import all_by_text, by_text, spoken

NO_DEAN = [("Amy", "female"), ("Carol", "female")]   # Dean not detected yet


def test_segments_expose_anchor_and_overridden(make_book, segments):
    segs = segments(make_book())
    assert all("anchor" in s and s["overridden"] is False for s in segs)
    assert [s["anchor"] for s in segs if s["text"] == ""] == [None]


def test_correct_one_sentence(make_book, segments, correct):
    book = make_book()
    before = segments(book)
    target = by_text(before, '"Then when?"')                 # detected as Amy

    res = correct(book, [target["anchor"]], "Dean")
    assert res.status_code == 200 and res.json["updated"] == 1

    after = segments(book)
    fixed = by_text(after, '"Then when?"')
    assert fixed["character_name"] == "Dean" and fixed["overridden"]
    assert [s["character_name"] for s in after if s is not fixed and s["text"] != '"Then when?"'] == \
           [s["character_name"] for s in before if s["text"] != '"Then when?"']
    assert sum(s["overridden"] for s in after) == 1


@pytest.fixture
def repeats(make_book, segments, correct):
    """A book whose only chapter repeats one line twice; yields helpers around its two copies."""
    book = make_book(chapters=(AMBIGUOUS_REPEAT,))

    class Repeats:
        def lines(self):
            return all_by_text(segments(book), '"No,"')

        def state(self):
            return [(s["character_name"], s["overridden"]) for s in self.lines()]

        def correct_second(self, character="Amy", reset=False):
            return correct(book, [self.lines()[1]["anchor"]], character, reset=reset)

    repeats = Repeats()
    repeats.book = book
    return repeats


def test_repeated_identical_lines_are_corrected_independently(repeats):
    assert repeats.state() == [("Dean", False), ("Dean", False)]
    assert repeats.correct_second().status_code == 200
    assert repeats.state() == [("Dean", False), ("Amy", True)]


def test_a_correction_to_one_repeat_survives_a_rebuild(repeats, client):
    def segment_ids():
        with get_conn() as conn:
            return {r[0] for r in conn.execute("SELECT id FROM tts_segments WHERE book_id=?", (repeats.book.id,))}

    repeats.correct_second()
    assert repeats.state() == [("Dean", False), ("Amy", True)]     # reading it builds the segments
    before = segment_ids()
    assert before

    # Adding a character re-segments the book.
    res = client.post(f"/api/books/{repeats.book.id}/characters", json={"name": "Evelyn", "gender": "female"})
    assert res.status_code == 201
    assert repeats.state() == [("Dean", False), ("Amy", True)]
    assert segment_ids() and not before & segment_ids()          # rows were rebuilt, not reused


def test_resetting_one_repeat_leaves_the_other_alone(repeats):
    repeats.correct_second()
    repeats.correct_second(reset=True)
    assert repeats.state() == [("Dean", False), ("Dean", False)]


def test_correct_a_whole_quote_at_once(make_book, segments, correct):
    book = make_book()
    halves = ['"After the shift change,"', '"Not a minute sooner."']        # Carol's reply, split by "she replied"
    before = segments(book)
    correct(book, [by_text(before, t)["anchor"] for t in halves], "Dean")

    after = segments(book)
    assert [by_text(after, t)["character_name"] for t in halves] == ["Dean", "Dean"]
    assert by_text(after, '"That is far too early."')["character_name"] == "Carol"   # neighbour untouched


def test_correct_to_narrator(make_book, segments, correct):
    book = make_book()
    target = by_text(segments(book), '"Then when?"')
    correct(book, [target["anchor"]], None)
    fixed = by_text(segments(book), '"Then when?"')
    assert fixed["character_name"] is None and fixed["overridden"]


def test_reset_restores_the_detected_speaker(make_book, segments, correct):
    book = make_book()
    original = spoken(segments(book))
    anchors = [s["anchor"] for s in segments(book) if s["is_dialogue"]]

    correct(book, anchors, "Dean")
    assert {name for _, name in spoken(segments(book))} == {"Dean"}

    assert correct(book, anchors, reset=True).status_code == 200
    restored = segments(book)
    assert spoken(restored) == original and not any(s["overridden"] for s in restored)


def test_a_correction_naming_a_deleted_character_is_ignored(make_book, segments, correct):
    book = make_book()
    target = by_text(segments(book), '"Then when?"')
    correct(book, [target["anchor"]], "Dean")
    with get_conn() as conn:
        conn.execute("DELETE FROM characters WHERE book_id=? AND name='Dean'", (book.id,))
        conn.execute("DELETE FROM tts_segments WHERE book_id=?", (book.id,))       # force a rebuild

    fixed = by_text(segments(book), '"Then when?"')
    assert fixed["character_name"] == "Amy" and not fixed["overridden"]


def test_rejects_bad_requests(make_book, segments, correct, client):
    book = make_book()
    anchor = by_text(segments(book), '"Then when?"')["anchor"]
    url = f"/api/books/{book.id}/chapters/{book.chapters[0]}/speaker"

    assert client.put(url, json={"anchors": "nope", "character": None}).status_code == 400
    assert correct(book, ["not-an-anchor:0"], "Dean").status_code == 404
    assert correct(book, [anchor], "Nobody At All").status_code == 400
    assert not any(s["overridden"] for s in segments(book))


def test_corrections_are_per_chapter(make_book, segments, correct):
    book = make_book()
    ready = by_text(segments(book, 1), '"Ready,"')            # Amy, chapter two
    correct(book, [ready["anchor"]], "Dean", chapter_index=1)

    assert by_text(segments(book, 1), '"Ready,"')["character_name"] == "Dean"
    assert not any(s["overridden"] for s in segments(book, 0))


def test_a_correction_invalidates_stored_audio(make_book, segments, correct, app_module, tmp_path):
    book = make_book()
    segs = segments(book)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    with get_conn() as conn:
        conn.execute(
            "UPDATE tts_segments SET audio_path=?, duration_sec=1.0, engine_tag=? WHERE book_id=? AND segment_index=1",
            (str(wav), app_module.tts.cache_tag, book.id),
        )
    assert segments(book)[1]["has_audio"]

    correct(book, [segs[1]["anchor"]], "Dean")
    assert not segments(book)[1]["has_audio"]


def test_segments_list_the_audio_url_only_when_audio_is_current(make_book, segments, app_module, tmp_path):
    book = make_book()
    segments(book)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    with get_conn() as conn:
        conn.execute(
            "UPDATE tts_segments SET audio_path=?, duration_sec=1.0, cache_key='abc123', engine_tag=? "
            "WHERE book_id=? AND segment_index=1", (str(wav), app_module.tts.cache_tag, book.id))
    segs = segments(book)
    assert segs[1]["audio_url"] == "/api/audio/abc123"
    assert segs[0]["audio_url"] is None                       # no audio yet


def test_a_missing_audio_file_does_not_count_as_cached(make_book, segments, app_module, tmp_path):
    book = make_book()
    segments(book)
    with get_conn() as conn:
        conn.execute(
            "UPDATE tts_segments SET audio_path=?, duration_sec=1.0, engine_tag=? WHERE book_id=? AND segment_index=1",
            (str(tmp_path / "deleted.wav"), app_module.tts.cache_tag, book.id),
        )
    assert not segments(book)[1]["has_audio"]          # the reader would otherwise skip regenerating it


def test_audio_from_another_engine_does_not_count_as_cached(make_book, segments, app_module, tmp_path):
    book = make_book()
    segments(book)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"x")
    with get_conn() as conn:
        conn.execute(
            "UPDATE tts_segments SET audio_path=?, duration_sec=1.0, engine_tag='other:9' WHERE book_id=? AND segment_index=1",
            (str(wav), book.id),
        )
    assert not segments(book)[1]["has_audio"]


# ── Adding a character the detector missed ────────────────────────────────────

def test_adding_a_character_redetects_speakers_and_counts_mentions(make_book, segments, client):
    book = make_book(NO_DEAN)
    assert "Dean" not in {n for _, n in spoken(segments(book))}

    res = client.post(f"/api/books/{book.id}/characters", json={"name": "Dean", "gender": "male"})
    assert res.status_code == 201
    assert res.json["frequency"] == CHAPTER_ONE.count("Dean")         # mentions, not 0
    assert res.json["instruct"] and res.json["color_hex"]

    after = spoken(segments(book))
    assert ('"No."', "Dean") in after and ('"No. We stay put."', "Dean") in after

    listed = {c["name"]: c for c in client.get(f"/api/books/{book.id}/characters").json}
    assert listed["Dean"]["frequency"] == res.json["frequency"] > 0


def test_corrections_stay_when_a_character_is_added(make_book, segments, correct, client):
    book = make_book(NO_DEAN)
    target = by_text(segments(book), '"Then when?"')
    correct(book, [target["anchor"]], "Carol")

    client.post(f"/api/books/{book.id}/characters", json={"name": "Dean", "gender": "male"})
    fixed = by_text(segments(book), '"Then when?"')
    assert fixed["character_name"] == "Carol" and fixed["overridden"]


def test_assign_a_sentence_to_the_new_character(make_book, segments, correct, client):
    book = make_book(NO_DEAN)
    client.post(f"/api/books/{book.id}/characters", json={"name": "Evelyn", "gender": "female"})
    target = by_text(segments(book), '"Then when?"')

    assert correct(book, [target["anchor"]], "Evelyn").status_code == 200
    assert by_text(segments(book), '"Then when?"')["character_name"] == "Evelyn"


def test_add_character_validation(make_book, client):
    book = make_book()
    url = f"/api/books/{book.id}/characters"
    assert client.post(url, json={"name": "  "}).status_code == 400
    assert client.post(url, json={"name": "X" * 61}).status_code == 400
    assert client.post(url, json={"name": "Zed", "gender": "robot"}).status_code == 400
    assert client.post(url, json={"name": "Amy"}).status_code == 409            # already exists
    assert client.post("/api/books/999/characters", json={"name": "Zed"}).status_code == 404


def test_hand_added_characters_at_zero_are_backfilled_on_startup(make_book, client, app_module):
    book = make_book()
    with get_conn() as conn:
        conn.execute("UPDATE characters SET frequency=0 WHERE book_id=? AND name='Dean'", (book.id,))
    app_module._backfill_character_frequencies()
    with get_conn() as conn:
        freq = conn.execute("SELECT frequency FROM characters WHERE book_id=? AND name='Dean'", (book.id,)).fetchone()[0]
    assert freq == CHAPTER_ONE.count("Dean") > 0


# ── Deleting a character you added by hand ───────────────────────────────────

def _char(client, book, name):
    return next(c for c in client.get(f"/api/books/{book.id}/characters").json if c["name"] == name)


def test_a_hand_added_character_can_be_deleted_and_detection_forgets_them(make_book, segments, client):
    book = make_book(NO_DEAN)
    client.post(f"/api/books/{book.id}/characters", json={"name": "Dean", "gender": "male"})
    dean = _char(client, book, "Dean")
    assert dean["manual"] == 1 and dean["added_at"] and dean["deletable"]
    assert "Dean" in {n for _, n in spoken(segments(book))}

    assert client.delete(f"/api/books/{book.id}/characters/{dean['id']}").status_code == 200
    assert "Dean" not in {c["name"] for c in client.get(f"/api/books/{book.id}/characters").json}
    assert "Dean" not in {n for _, n in spoken(segments(book))}


def test_detected_characters_cannot_be_deleted(make_book, client):
    book = make_book()
    amy = _char(client, book, "Amy")
    assert amy["manual"] == 0 and not amy["deletable"]
    assert client.delete(f"/api/books/{book.id}/characters/{amy['id']}").status_code == 409
    assert _char(client, book, "Amy")


def test_a_character_with_hand_assigned_sentences_cannot_be_deleted(make_book, segments, correct, client):
    book = make_book(NO_DEAN)
    client.post(f"/api/books/{book.id}/characters", json={"name": "Evelyn", "gender": "female"})
    target = by_text(segments(book), '"Then when?"')
    correct(book, [target["anchor"]], "Evelyn")
    evelyn = _char(client, book, "Evelyn")

    assert not evelyn["deletable"]
    res = client.delete(f"/api/books/{book.id}/characters/{evelyn['id']}")
    assert res.status_code == 409 and "reset" in res.json["error"]
    assert by_text(segments(book), '"Then when?"')["character_name"] == "Evelyn"


def test_a_character_others_are_merged_into_cannot_be_deleted(make_book, client):
    book = make_book(NO_DEAN)
    client.post(f"/api/books/{book.id}/characters", json={"name": "Dean", "gender": "male"})
    dean, amy = _char(client, book, "Dean"), _char(client, book, "Amy")
    client.put(f"/api/books/{book.id}/characters/{amy['id']}", json={"alias_of": dean["id"]})

    assert not _char(client, book, "Dean")["deletable"]
    assert client.delete(f"/api/books/{book.id}/characters/{dean['id']}").status_code == 409


def test_deleting_an_unknown_character_is_a_404(make_book, client):
    book = make_book()
    assert client.delete(f"/api/books/{book.id}/characters/99999").status_code == 404
