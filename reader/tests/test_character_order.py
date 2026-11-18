"""Voice Studio's character order: merged characters stay with who they are merged into, and groups
are ordered by their combined frequency."""

import pytest

from core.database import get_conn


@pytest.fixture
def cast(make_book, client):
    """cast({"Amy": 60, "Dean": 40, ...}, merged={"Dee": "Dean"}) -> a function listing names in display order."""
    def build(frequencies, merged=None):
        book = make_book([(name, "unknown") for name in frequencies])
        with get_conn() as conn:
            for name, freq in frequencies.items():
                conn.execute("UPDATE characters SET frequency=? WHERE book_id=? AND name=?", (freq, book.id, name))
            for alias, target in (merged or {}).items():
                conn.execute(
                    "UPDATE characters SET alias_of=(SELECT id FROM characters WHERE book_id=? AND name=?) "
                    "WHERE book_id=? AND name=?", (book.id, target, book.id, alias))
        book.listing = lambda: client.get(f"/api/books/{book.id}/characters").json
        book.names = lambda: [c["name"] for c in book.listing()]
        return book
    return build


def test_without_any_merges_the_order_is_by_frequency(cast):
    assert cast({"Carol": 30, "Amy": 60, "Dean": 40}).names() == ["Amy", "Dean", "Carol"]


def test_ties_go_to_the_alphabetically_first_name(cast):
    assert cast({"Zed": 50, "Amy": 50, "Mia": 50}).names() == ["Amy", "Mia", "Zed"]


def test_a_merged_character_stays_right_behind_the_one_it_is_merged_into(cast):
    # Dee alone (30) would sit below Carol (35); merged into Dean (group total 120, still below Amy) it stays beside him.
    book = cast({"Amy": 150, "Dean": 90, "Carol": 35, "Dee": 30}, merged={"Dee": "Dean"})
    assert book.names() == ["Amy", "Dean", "Dee", "Carol"]


def test_a_group_moves_up_when_its_combined_total_passes_another(cast):
    # Dean (40) + Dee (30) = 70 beats Amy (60), although neither of them alone does.
    book = cast({"Amy": 60, "Carol": 50, "Dean": 40, "Dee": 30}, merged={"Dee": "Dean"})
    assert book.names() == ["Dean", "Dee", "Amy", "Carol"]


def test_a_group_whose_total_falls_short_stays_below(cast):
    # 40 + 10 = 50 is still less than Amy's 60.
    book = cast({"Amy": 60, "Dean": 40, "Dee": 10, "Carol": 45}, merged={"Dee": "Dean"})
    assert book.names() == ["Amy", "Dean", "Dee", "Carol"]            # 50 beats Carol's 45, not Amy's 60


def test_the_one_merged_into_always_comes_first_even_if_the_other_is_more_frequent(cast):
    book = cast({"Amy": 20, "Dean": 5, "Dee": 90}, merged={"Dee": "Dean"})
    assert book.names() == ["Dean", "Dee", "Amy"]                     # group total 95 beats Amy; Dean heads it


def test_several_merged_characters_follow_in_frequency_order(cast):
    book = cast({"Dean": 10, "Dee": 5, "Deanna": 50, "Dino": 20}, merged={"Dee": "Dean", "Deanna": "Dean", "Dino": "Dean"})
    assert book.names() == ["Dean", "Deanna", "Dino", "Dee"]


def test_each_entry_carries_its_groups_combined_total(cast):
    book = cast({"Amy": 60, "Dean": 40, "Dee": 30}, merged={"Dee": "Dean"})
    assert {c["name"]: c["group_total"] for c in book.listing()} == {"Dean": 70, "Dee": 70, "Amy": 60}


def test_a_merge_into_a_missing_character_leaves_it_on_its_own(cast):
    book = cast({"Amy": 60, "Dean": 40})
    with get_conn() as conn:
        conn.execute("UPDATE characters SET alias_of=999999 WHERE book_id=? AND name='Dean'", (book.id,))
    assert book.names() == ["Amy", "Dean"] and {c["name"]: c["group_total"] for c in book.listing()}["Dean"] == 40


def test_a_chain_of_merges_does_not_form_a_group(cast):
    # The app keeps merges one level deep; if data ever has a chain, nothing is dragged into a wrong group.
    book = cast({"Amy": 60, "Bea": 50, "Cat": 40}, merged={"Bea": "Cat", "Amy": "Bea"})
    assert sorted(book.names()) == ["Amy", "Bea", "Cat"]              # every character is still listed exactly once
    assert {c["name"]: c["group_total"] for c in book.listing()}["Amy"] == 60       # Amy, merged into a merged one, stands alone


def test_merging_through_the_api_reorders_the_list(cast, client):
    book = cast({"Amy": 60, "Carol": 50, "Dean": 40, "Dee": 30})
    assert book.names() == ["Amy", "Carol", "Dean", "Dee"]
    ids = {c["name"]: c["id"] for c in book.listing()}

    assert client.put(f"/api/books/{book.id}/characters/{ids['Dee']}", json={"alias_of": ids["Dean"]}).status_code == 200
    assert book.names() == ["Dean", "Dee", "Amy", "Carol"]

    client.put(f"/api/books/{book.id}/characters/{ids['Dee']}", json={"alias_of": None})          # unmerge
    assert book.names() == ["Amy", "Carol", "Dean", "Dee"]
