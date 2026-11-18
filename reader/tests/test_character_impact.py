"""Previewing what adding a character would change, before adding it."""

import pytest

from core.database import get_conn
from tests.demo_story import ALIAS_STORY, KNOCK_ON_STORY, PRONOUN_STORY
from tests.helpers import by_text

NO_DEAN = [("Amy", "female"), ("Carol", "female")]
THREE = [("Amy", "female"), ("Carol", "female"), ("Ben", "male")]


@pytest.fixture
def preview(client):
    def _preview(book, name="Dean", gender="male"):
        return client.post(f"/api/books/{book.id}/characters/preview", json={"name": name, "gender": gender})
    return _preview


@pytest.fixture
def add(client):
    def _add(book, name="Dean", gender="male"):
        return client.post(f"/api/books/{book.id}/characters", json={"name": name, "gender": gender})
    return _add


def speakers(book, segments):
    """{(chapter_id, segment_index): speaker} for the whole book as the reader would see it."""
    return {(ch, s["segment_index"]): s["character_name"]
            for i, ch in enumerate(book.chapters) for s in segments(book, i)}


def test_demo_story_adding_dean_takes_the_two_no_lines_from_the_narrator(make_book, preview):
    result = preview(make_book(NO_DEAN)).json
    assert result["total"] == result["gained"] == 2
    assert result["transitions"] == [{"from": None, "to": "Dean", "count": 2}]
    assert [(c["title"], c["changed"]) for c in result["chapters"]] == [("Chapter 1", 2)]
    assert [(c["text"], c["prev"], c["before"], c["after"]) for c in result["changes"]] == [
        ('"No."', "Dean walked in carrying a lantern.", None, "Dean"),
        ('"No. We stay put."', "He set it down.", None, "Dean")]


@pytest.mark.parametrize("name, gender, characters, chapters", [
    *[pytest.param("Dean", g, NO_DEAN, None, id=f"demo-story-{g}") for g in ("male", "female", "unknown")],
    *[pytest.param("Dean", g, THREE, (KNOCK_ON_STORY,), id=f"knock-on-{g}") for g in ("male", "female", "unknown")],
    *[pytest.param("Dean", g, THREE, (PRONOUN_STORY,), id=f"pronoun-{g}") for g in ("male", "female", "unknown")],
    pytest.param("Mr. Militia", "male", [("Miss Militia", "female"), ("Amy", "female")], (ALIAS_STORY,),
                 id="alias-collision"),
    pytest.param("Zed", "male", NO_DEAN, None, id="name-not-in-text"),
])
def test_preview_predicts_exactly_what_adding_does(make_book, segments, preview, add, name, gender,
                                                   characters, chapters):
    book = make_book(characters, chapters=chapters)
    before = speakers(book, segments)
    predicted = {(c["chapter_id"], c["segment_index"]): (c["before"], c["after"])
                 for c in preview(book, name=name, gender=gender).json["changes"]}

    assert add(book, name=name, gender=gender).status_code == 201
    after = speakers(book, segments)
    actual = {key: (before[key], after[key]) for key in before if before[key] != after[key]}
    assert predicted == actual


def test_a_stale_correction_naming_the_new_character_goes_live_even_in_a_chapter_without_the_name(
        make_book, segments, preview, add):
    book = make_book(NO_DEAN)                                  # chapter two never mentions Dean
    target = by_text(segments(book, 1), '"Ready,"')            # detected as Amy
    with get_conn() as conn:                                   # a correction left over from a deleted Dean
        conn.execute("INSERT INTO segment_speakers (book_id, chapter_id, anchor, character_name) VALUES (?,?,?,?)",
                     (book.id, book.chapters[1], target["anchor"], "Dean"))

    predicted = [(c["chapter_id"], c["text"], c["before"], c["after"]) for c in preview(book).json["changes"]]
    assert (book.chapters[1], '"Ready,"', "Amy", "Dean") in predicted

    add(book)
    assert by_text(segments(book, 1), '"Ready,"')["character_name"] == "Dean"


def test_the_chosen_gender_changes_the_prediction(make_book, preview):
    book = make_book(THREE, chapters=(PRONOUN_STORY,))
    male = preview(book, gender="male").json
    female = preview(book, gender="female").json

    assert [(c["before"], c["after"]) for c in male["changes"]] == [("Ben", "Dean")]
    assert female["total"] == 0                                  # a female Dean does not get "he said"


def test_preview_changes_nothing(make_book, segments, preview, client):
    book = make_book(NO_DEAN)
    segments(book)                                              # build the segments first

    def snapshot():
        with get_conn() as conn:
            return ([r[0] for r in conn.execute("SELECT name FROM characters WHERE book_id=?", (book.id,))],
                    [r[0] for r in conn.execute("SELECT id FROM tts_segments WHERE book_id=?", (book.id,))])

    before = snapshot()
    assert preview(book).status_code == 200
    assert snapshot() == before


def test_knock_on_changes_to_other_characters_are_reported(make_book, preview):
    book = make_book(THREE, chapters=(KNOCK_ON_STORY,))
    result = preview(book).json
    pairs = {(t["from"], t["to"]): t["count"] for t in result["transitions"]}

    assert pairs[("Ben", "Dean")] == 2 and pairs[(None, "Dean")] == 1
    assert pairs[(None, "Ben")] == 1                            # a line moves without involving Dean
    assert result["gained"] == 3 and result["total"] == 4       # 3 to Dean, 1 knock-on


def test_corrections_are_not_counted(make_book, segments, correct, preview):
    book = make_book(NO_DEAN)
    no = by_text(segments(book), '"No."')
    correct(book, [no["anchor"]], "Carol")                      # the user already decided this one

    result = preview(book).json
    assert [c["text"] for c in result["changes"]] == ['"No. We stay put."']
    assert result["total"] == 1


def test_a_name_that_is_not_in_the_text_changes_nothing(make_book, preview):
    result = preview(make_book(NO_DEAN), name="Zed").json
    assert (result["total"], result["gained"], result["transitions"], result["chapters"]) == (0, 0, [], [])
    assert result["truncated"] is False


def test_long_lists_are_truncated_but_totals_stay_exact(make_book, preview, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_MAX_IMPACT_CHANGES", 1)
    result = preview(make_book(NO_DEAN)).json
    assert len(result["changes"]) == 1 and result["truncated"] is True
    assert result["total"] == 2 and result["transitions"] == [{"from": None, "to": "Dean", "count": 2}]


def test_preview_validates_like_adding(make_book, preview, add, client):
    book = make_book(NO_DEAN)
    for call in (preview, add):
        assert call(book, name="  ").status_code == 400
        assert call(book, name="Zed", gender="robot").status_code == 400
        assert call(book, name="Amy").status_code == 409
    assert client.post("/api/books/999/characters/preview", json={"name": "Zed"}).status_code == 404


# ── The shortcut: chapters that cannot be affected are not enriched at all ────

def test_an_alias_collision_changes_a_chapter_that_never_names_the_new_character(make_book, preview):
    book = make_book([("Miss Militia", "female"), ("Amy", "female")], chapters=(ALIAS_STORY,))
    result = preview(book, name="Mr. Militia", gender="male").json
    assert result["total"] == 1
    assert [(t["from"], t["to"]) for t in result["transitions"]] == [("Miss Militia", None)]


def test_chapters_that_cannot_change_are_not_enriched(make_book, preview, app_module, monkeypatch):
    book = make_book(NO_DEAN)                                  # only chapter one mentions Dean
    calls = []
    real = app_module._compute_segments_for_chapter
    monkeypatch.setattr(app_module, "_compute_segments_for_chapter",
                        lambda book_id, chapter_id, extra=(): calls.append(chapter_id) or real(book_id, chapter_id, extra))

    assert preview(book).json["total"] == 2
    assert calls == [book.chapters[0]] * 2                     # chapter one, before and after; chapter two skipped


def test_a_name_found_nowhere_enriches_nothing(make_book, preview, app_module, monkeypatch):
    book = make_book(NO_DEAN)
    monkeypatch.setattr(app_module, "_compute_segments_for_chapter",
                        lambda *a, **k: pytest.fail("should not enrich any chapter"))
    assert preview(book, name="Zed").json["total"] == 0
