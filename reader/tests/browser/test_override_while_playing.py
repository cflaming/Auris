"""Correcting a speaker while audio plays: only audio that is now out of date interrupts playback."""

import pytest

from tests.browser.helpers import apply, dialogue_index, pick, play_generation, right_click_sentence, start_playing


def correct_to_other(page, idx):
    right_click_sentence(page, idx)
    pick(page, "Other speaker")
    apply(page)
    page.wait_for_function("i => chapter.segments[i]?.character_name === 'Other speaker'", arg=idx)
    page.wait_for_timeout(300)


@pytest.fixture
def playing(reader):
    page = reader(segment=60)
    start_playing(page)
    return page


def test_correcting_a_sentence_already_read_keeps_playing(playing):
    page = playing
    gen, t = play_generation(page), page.evaluate("audio.currentTime")
    correct_to_other(page, dialogue_index(page, 10))
    assert play_generation(page) == gen                            # not restarted
    assert page.evaluate("isPlaying && !audio.paused && audio.currentTime >= " + str(t))
    assert page.locator(".sentence.playing").count() >= 1          # still highlighting what plays


def test_correcting_a_sentence_far_ahead_keeps_playing(playing):
    page = playing
    gen = play_generation(page)
    correct_to_other(page, dialogue_index(page, page.evaluate("currentSegIdx") + 30))
    assert play_generation(page) == gen
    assert page.evaluate("isPlaying && !audio.paused")


def test_correcting_a_sentence_already_loaded_ahead_restarts_playback(playing):
    page = playing
    gen = play_generation(page)
    correct_to_other(page, dialogue_index(page, page.evaluate("currentSegIdx + 1")))
    page.wait_for_function("g => _playGen !== g", arg=gen)         # the stream held the old voice for it
    page.wait_for_function("isPlaying && !audio.paused")


def test_a_correction_costs_one_refresh_not_a_stream_rebuild(site, playing):
    page = playing
    before = site.stats().get("requests GET /api/tts/segments/<int:book_id>/<int:chapter_id>", 0)
    correct_to_other(page, dialogue_index(page, 10))
    after = site.stats().get("requests GET /api/tts/segments/<int:book_id>/<int:chapter_id>", 0)
    assert after - before == 1
