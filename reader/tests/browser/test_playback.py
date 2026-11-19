"""Playback and the background buffer, measured by what the page asks the server for."""

from tests.browser.helpers import start_playing


def test_resuming_mid_chapter_loads_from_that_sentence_not_the_chapter_start(site, reader):
    page = reader(segment=60)
    start_playing(page)
    s = site.stats()
    texts = page.evaluate("chapter.segments.map(s => s.text)")
    # (some lines repeat, so only lines that exist nowhere later tell)
    only_earlier = set(texts[:60]) - set(texts[60:])
    # Regression: a fresh stream used to begin at sentence 0, so resuming at 335 generated 0..335 first.
    assert not set(s["encoded_texts"]) & only_earlier
    assert s["encodes"] < 20, f"resume generated {s['encodes']} sentences"
    assert page.evaluate("Math.min(...Object.keys(_segStart).map(Number))") >= 60


def test_while_paused_the_buffer_generates_ten_ahead_then_goes_quiet(site, reader):
    page = reader(segment=5)
    page.wait_for_function("Array.from({length: 11}, (_, i) => chapter.cache.has(5 + i)).every(Boolean)", timeout=20000)
    page.wait_for_timeout(1500)
    before = site.stats()["requests POST /api/tts/generate"]
    page.wait_for_timeout(2000)
    after = site.stats()["requests POST /api/tts/generate"]
    # the cursor sentence plus ten after it, then nothing more
    assert after == before == 11


def test_moving_the_cursor_while_paused_extends_the_buffer_by_only_what_is_new(site, reader):
    page = reader(segment=5)
    page.wait_for_function("chapter.cache.has(15)", timeout=20000)
    page.wait_for_timeout(500)
    generates = lambda: site.stats()["requests POST /api/tts/generate"]
    before = generates()
    page.evaluate("setCurrentSegment(8, {highlight: true, save: false})")
    page.wait_for_function("chapter.cache.has(18)", timeout=20000)
    page.wait_for_timeout(800)
    assert generates() - before == 3                             # sentences 16, 17, 18
