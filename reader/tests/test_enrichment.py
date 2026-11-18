"""Speaker detection and corrections in core.enrichment — pure functions, no app."""

from core import enrichment
from core.characters import generate_voice_profile
from tests.demo_story import CHAPTER_ONE, CHAPTER_TWO, CHARACTERS
from tests.helpers import by_text, spoken

REPEATED_NO = 'Dean frowned. "No," Dean said.\n\nCarol laughed. "No," Carol said.'
WE_SHOULD_LEAVE = '"We should leave before dawn,"'          # Amy
AFTER_THE_SHIFT = '"After the shift change,"'              # Carol ("she replied")


def character_map(characters=CHARACTERS, aliases=None):
    cmap = {
        name: {**generate_voice_profile(name, gender), "name": name}
        for name, gender in characters
    }
    for alias, target in (aliases or {}).items():
        cmap[alias] = {**cmap[target], "name": alias, "canonical": target}
    return cmap


CMAP = character_map()


def enrich(text=CHAPTER_ONE, overrides=None, cmap=None, **kwargs):
    return enrichment.enrich_chapter(text, cmap or CMAP, speaker_overrides=overrides, **kwargs)


def test_detects_speakers_in_the_demo_story():
    assert spoken(enrich()) == [
        (WE_SHOULD_LEAVE, "Amy"),
        ('"The patrol changes at six."', "Amy"),
        ('"That is far too early."', "Carol"),
        ('"Then when?"', "Amy"),
        (AFTER_THE_SHIFT, "Carol"),                      # pronoun "she replied": Carol, not Amy
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
    segs = [s for s in enrich(REPEATED_NO) if s["text"] == '"No,"']
    assert len(segs) == 2 and segs[0]["anchor"] != segs[1]["anchor"]
    assert [s["character_name"] for s in segs] == ["Dean", "Carol"]


def test_correcting_one_repeated_sentence_leaves_the_other_alone():
    first = next(s for s in enrich(REPEATED_NO) if s["text"] == '"No,"')
    fixed = [s for s in enrich(REPEATED_NO, {first["anchor"]: "Amy"}) if s["text"] == '"No,"']
    assert [s["character_name"] for s in fixed] == ["Amy", "Carol"]


def test_override_changes_speaker_and_voice_only_for_that_sentence():
    before = enrich()
    anchor = by_text(before, WE_SHOULD_LEAVE)["anchor"]
    after = enrich(overrides={anchor: "Dean"})

    fixed = by_text(after, WE_SHOULD_LEAVE)
    assert fixed["character_name"] == "Dean" and fixed["overridden"]
    assert fixed["instruct"] == CMAP["Dean"]["instruct"]
    others = [(a, b) for a, b in zip(before, after) if a["anchor"] != anchor]
    assert all(a["character_name"] == b["character_name"] and not b["overridden"] for a, b in others)


def test_override_to_narrator():
    anchor = by_text(enrich(), WE_SHOULD_LEAVE)["anchor"]
    fixed = by_text(enrich(overrides={anchor: None}), WE_SHOULD_LEAVE)
    narration = next(s for s in enrich() if not s["is_dialogue"])
    assert fixed["character_name"] is None and fixed["overridden"]
    assert fixed["instruct"] == narration["instruct"]          # narrator voice


def test_override_naming_a_missing_character_is_ignored():
    anchor = by_text(enrich(), WE_SHOULD_LEAVE)["anchor"]
    ignored = by_text(enrich(overrides={anchor: "Ghost"}), WE_SHOULD_LEAVE)
    assert ignored["character_name"] == "Amy" and not ignored["overridden"]


def test_override_to_a_merged_alias_uses_the_canonical_character():
    cmap = character_map(aliases={"Mandy": "Amy"})
    anchor = by_text(enrich(cmap=cmap), AFTER_THE_SHIFT)["anchor"]      # detected as Carol
    fixed = by_text(enrich(cmap=cmap, overrides={anchor: "Mandy"}), AFTER_THE_SHIFT)
    assert fixed["character_name"] == "Amy"


def test_single_narrator_mode_ignores_overrides():
    anchor = by_text(enrich(), WE_SHOULD_LEAVE)["anchor"]
    segs = enrich(overrides={anchor: "Dean"}, single_narrator_mode=True)
    assert all(s["character_name"] is None and not s["overridden"] for s in segs)


def test_adding_a_character_changes_detection():
    without_dean = enrich(cmap=character_map([("Amy", "female"), ("Carol", "female")]))
    no_lines = {'"No."', '"No. We stay put."'}
    speakers = lambda segs: [s["character_name"] for s in segs if s["text"] in no_lines]
    assert "Dean" not in speakers(without_dean)
    assert speakers(enrich()) == ["Dean", "Dean"]
