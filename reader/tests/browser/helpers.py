"""Small page helpers shared by the browser tests."""


def start_playing(page, idx=None):
    """Play from a sentence (the cursor's if none given) the way a click on play does, and wait until audio is running."""
    if idx is not None:
        page.evaluate("i => setCurrentSegment(i, {highlight: true, save: false})", idx)
    page.click("#btn-play")
    page.wait_for_function("isPlaying && !audio.paused && audio.currentTime > 0.2", timeout=15000)


def right_click_sentence(page, idx):
    page.locator(f'.sentence[data-idx="{idx}"]').first.scroll_into_view_if_needed()
    page.locator(f'.sentence[data-idx="{idx}"]').first.click(button="right")
    page.wait_for_selector("#speaker-modal:not(.hidden)")


def dialogue_index(page, after=0):
    return page.evaluate("a => chapter.segments.findIndex((s, i) => i >= a && s.is_dialogue)", after)


def play_generation(page):
    """Changes whenever playback is (re)started from a sentence."""
    return page.evaluate("_playGen")
