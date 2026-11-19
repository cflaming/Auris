"""A character's page: the chapters they speak in, and their sentences in each."""


def char_id(client, book, name):
    return next(c["id"] for c in client.get(f"/api/books/{book.id}/characters").json if c["name"] == name)


def chapters(client, book, name):
    return client.get(f"/api/books/{book.id}/characters/{char_id(client, book, name)}/excerpts").json["chapters"]


def sentences(client, book, name, chapter_index=0):
    url = f"/api/books/{book.id}/characters/{char_id(client, book, name)}/excerpts/{book.chapters[chapter_index]}"
    return client.get(url).json


def test_unbuilt_chapters_are_listed_with_an_unknown_count_then_counted_once_built(client, make_book, segments):
    book = make_book()
    listed = chapters(client, book, "Amy")
    assert [c["id"] for c in listed] == book.chapters and {c["count"] for c in listed} == {None}
    truth = [s["text"] for s in segments(book) if s["character_name"] == "Amy"]       # builds chapter 1
    first = chapters(client, book, "Amy")[0]
    assert first["count"] == len(truth) and truth


def test_a_built_chapter_without_the_character_is_left_out(client, make_book, segments):
    book = make_book()
    for i in range(len(book.chapters)):
        segments(book, i)
    ids = {c["id"] for c in chapters(client, book, "Dean")}
    for i, chapter_id in enumerate(book.chapters):
        has = any(s["character_name"] == "Dean" for s in segments(book, i))
        assert (chapter_id in ids) == has


def hits(excerpts):
    return [x for e in excerpts for p in e["paragraphs"] for x in p if x["hit"]]


def test_lists_only_their_sentences_inside_their_paragraphs(client, make_book, segments):
    book = make_book()
    found = sentences(client, book, "Amy")
    truth = [s["text"] for s in segments(book) if s["character_name"] == "Amy"]
    assert [x["text"] for x in hits(found)] == truth and truth
    assert all(x["anchor"] for x in hits(found)) and all(e["segment_index"] is not None for e in found)
    assert all(p for e in found for p in e["paragraphs"])


def rows_for(speakers, paragraphs=None):
    return [{"text": f"S{i}.", "anchor": f"a{i}", "overridden": 0, "character_name": who,
             "paragraph": (paragraphs or range(len(speakers)))[i], "segment_index": i}
            for i, who in enumerate(speakers)]


def test_nearby_sentences_share_one_excerpt_and_far_ones_do_not(app_module):
    rows = rows_for(["Amy", None, None, "Amy"] + [None] * 12 + ["Amy"])
    groups = app_module._excerpt_groups(rows, "Amy")
    assert len(groups) == 2
    first = [x["text"] for p in groups[0]["paragraphs"] for x in p]
    assert first[0] == "S0." and "S3." in first                          # one passage through to the second hit
    assert [x["hit"] for p in groups[0]["paragraphs"] for x in p][:4] == [True, False, False, True]
    assert groups[1]["segment_index"] == 16


def test_an_excerpt_holds_at_most_a_few_hits_and_keeps_paragraphs(app_module):
    many = app_module._excerpt_groups(rows_for(["Amy"] * 12), "Amy")
    assert [sum(x["hit"] for p in g["paragraphs"] for x in p) for g in many] == [5, 5, 2]
    split = app_module._excerpt_groups(rows_for(["Amy", None, "Amy"], paragraphs=[0, 0, 1]), "Amy")
    assert [[x["text"] for x in p] for p in split[0]["paragraphs"]] == [["S0.", "S1."], ["S2."]]


def test_a_changed_speaker_moves_between_characters(client, make_book, segments, correct):
    book = make_book()
    line = next(s for s in segments(book) if s["character_name"] == "Amy")
    correct(book, [line["anchor"]], "Carol")
    assert line["anchor"] not in [x["anchor"] for x in hits(sentences(client, book, "Amy"))]
    assert {x["anchor"]: x["overridden"] for x in hits(sentences(client, book, "Carol"))}[line["anchor"]] is True


def test_a_merged_character_is_listed_under_its_target(client, make_book):
    book = make_book()
    amy, carol = char_id(client, book, "Amy"), char_id(client, book, "Carol")
    client.put(f"/api/books/{book.id}/characters/{carol}", json={"alias_of": amy})
    assert sentences(client, book, "Carol") == sentences(client, book, "Amy")


def test_unknown_character_and_pages(client, make_book):
    book = make_book()
    assert client.get(f"/api/books/{book.id}/characters/9999/excerpts").status_code == 404
    assert client.get(f"/api/books/{book.id}/characters/9999/excerpts/{book.chapters[0]}").status_code == 404
    assert client.get(f"/characters/{book.id}/9999").status_code == 404
    assert client.get(f"/characters/{book.id}/{char_id(client, book, 'Amy')}").status_code == 200
