"""Speaker detection and corrections in core.enrichment — pure functions, no app."""

from core import enrichment
from core.characters import generate_voice_profile
from tests.conftest import spoken
from tests.demo_story import CHAPTER_ONE, CHAPTER_TWO, CHARACTERS


def character_map(characters=CHARACTERS, aliases=None):
    cmap = {
        name: {**generate_voice_profile(name, gender), "name": name}
        for name, gender in characters
    }
    for alias, target in (aliases or {}).items():
        cmap[alias] = {**cmap[target], "name": alias, "canonical": target}
    return cmap


def enrich(text=CHAPTER_ONE, overrides=None, **kwargs):
    return enrichment.enrich_chapter(text, kwargs.pop("cmap", character_map()),
                                     speaker_overrides=overrides, **kwargs)


def test_detects_speakers_in_the_demo_story():
    assert spoken(enrich()) == [
        ('"We should leave before dawn,"', "Amy"),
        ('"The patrol changes at six."', "Amy"),
        ('"That is far too early."', "Carol"),
        ('"Then when?"', "Amy"),
        ('"After the shift change,"', "Carol"),   # pronoun "she replied": Carol, not Amy
        ('"Not a minute sooner."', "Carol"),
        ('"No."', "Dean"),
        ('"No. We stay put."', "Dean"),
    ]


def test_second_chapter_uses_inverted_attribution():
    assert spoken(enrich(CHAPTER_TWO)) == [('"Ready?"', "Carol"), ('"Ready,"', "Amy")]


def test_narration_has_no_speaker():
    narration = [s for s in enrich() if not s["is_dialogue"]]
    assert narration and all(s["character_name"] is None for s in narration)


def test_section_breaks_have_no_anchor_and_cannot_be_corrected():
    breaks = [s for s in enrich() if s["text"] == ""]
    assert len(breaks) == 1 and breaks[0]["anchor"] is None


def test_anchors_are_unique_and_stable():
    first, second = enrich(), enrich()
    anchors = [s["anchor"] for s in first if s["anchor"]]
    assert len(anchors) == len(set(anchors))
    assert anchors == [s["anchor"] for s in second if s["anchor"]]


def test_identical_sentences_get_distinct_anchors():
    text = 'Dean frowned. "No," Dean said.\n\nCarol laughed. "No," Carol said.'
    segs = [s for s in enrich(text) if s["text"] == '"No,"']
    assert len(segs) == 2 and segs[0]["anchor"] != segs[1]["anchor"]
    assert [s["character_name"] for s in segs] == ["Dean", "Carol"]


def test_correcting_one_repeated_sentence_leaves_the_other_alone():
    text = 'Dean frowned. "No," Dean said.\n\nCarol laughed. "No," Carol said.'
    first = next(s for s in enrich(text) if s["text"] == '"No,"')
    fixed = [s for s in enrich(text, {first["anchor"]: "Amy"}) if s["text"] == '"No,"']
    assert [s["character_name"] for s in fixed] == ["Amy", "Carol"]


def test_override_changes_speaker_and_voice_only_for_that_sentence():
    before = enrich()
    target = before[1]                                   # '"We should leave…"' (Amy)
    after = enrich(overrides={target["anchor"]: "Dean"})

    assert after[1]["character_name"] == "Dean" and after[1]["overridden"]
    assert after[1]["instruct"] == character_map()["Dean"]["instruct"]
    unchanged = [(a, b) for i, (a, b) in enumerate(zip(before, after)) if i != 1]
    assert all(a["character_name"] == b["character_name"] and not b["overridden"] for a, b in unchanged)


def test_override_to_narrator():
    target = enrich()[1]
    fixed = enrich(overrides={target["anchor"]: None})[1]
    assert fixed["character_name"] is None and fixed["overridden"]
    narration = next(s for s in enrich() if not s["is_dialogue"])
    assert fixed["instruct"] == narration["instruct"]          # narrator voice


def test_override_naming_a_missing_character_is_ignored():
    target = enrich()[1]
    ignored = enrich(overrides={target["anchor"]: "Ghost"})[1]
    assert ignored["character_name"] == "Amy" and not ignored["overridden"]


def test_override_to_a_merged_alias_uses_the_canonical_character():
    cmap = character_map(aliases={"Mandy": "Amy"})
    target = enrich(cmap=cmap)[8]                        # '"After the shift change,"' (Carol)
    fixed = enrich(cmap=cmap, overrides={target["anchor"]: "Mandy"})[8]
    assert fixed["character_name"] == "Amy"


def test_single_narrator_mode_ignores_overrides():
    target = enrich()[1]
    segs = enrich(overrides={target["anchor"]: "Dean"}, single_narrator_mode=True)
    assert all(s["character_name"] is None and not s["overridden"] for s in segs)


def test_adding_a_character_changes_detection():
    without_dean = enrich(cmap=character_map([("Amy", "female"), ("Carol", "female")]))
    with_dean = enrich()
    no_lines = {'"No."', '"No. We stay put."'}
    speakers = lambda segs: [s["character_name"] for s in segs if s["text"] in no_lines]
    assert "Dean" not in speakers(without_dean)
    assert speakers(with_dean) == ["Dean", "Dean"]
