"""The speaker-override dialog: its views, keys, focus and what applying does to the page."""

import re

from playwright.sync_api import expect

from tests.browser.helpers import apply, dialogue_index, pick, right_click_sentence


def open_dialog(page, idx=None):
    idx = dialogue_index(page) if idx is None else idx
    right_click_sentence(page, idx)
    return idx


def test_other_speaker_is_offered_right_under_narrator_and_labels_the_sentence(reader):
    page = reader()
    idx = open_dialog(page)
    options = [" ".join(t.split()) for t in page.locator("#speaker-list li[role=option]").all_inner_texts()]
    assert options[:2] == ["Narrator", "Other speaker (narrator’s voice)"]
    pick(page, "Other speaker")
    apply(page)
    page.wait_for_function("i => chapter.segments[i]?.character_name === 'Other speaker'", arg=idx)
    sentence = page.locator(f'.sentence[data-idx="{idx}"]').first
    expect(sentence).to_have_attribute("title", "Other speaker (corrected)")
    expect(page.locator("#speaker-modal")).to_be_hidden()


def test_typing_other_finds_it_and_does_not_offer_it_as_a_new_character(reader):
    page = reader()
    open_dialog(page)
    page.locator("#speaker-list").focus()
    page.keyboard.type("other speaker")
    rows = [" ".join(t.split()) for t in page.locator("#speaker-list li").all_inner_texts()]
    assert rows == ["Other speaker (narrator’s voice)"]


def test_escape_walks_back_one_view_at_a_time(reader):
    page = reader()
    open_dialog(page)
    modal = page.locator("#speaker-modal")

    page.get_by_role("button", name="Debug info").click()
    expect(page.locator("#speaker-debug")).to_be_visible()
    page.keyboard.press("Escape")                                  # debug -> back to the override view
    expect(page.locator("#speaker-debug")).to_be_hidden()
    expect(modal).to_be_visible()

    page.keyboard.press("e")                                       # (a letter on the list starts a search)
    expect(page.locator("#speaker-input")).to_be_visible()
    page.keyboard.press("Escape")                                  # leaves the search, dialog stays
    expect(page.locator("#speaker-input")).to_be_hidden()
    expect(modal).to_be_visible()

    page.keyboard.press("Escape")                                  # now the dialog's own Cancel
    expect(modal).to_be_hidden()


def test_new_character_takes_over_the_dialog_and_escape_goes_back(reader):
    page = reader()
    open_dialog(page)
    pick(page, "+ New character")
    modal = page.locator("#speaker-modal .speaker-modal")
    expect(modal).to_have_class(re.compile(r"showing-new"))
    expect(page.locator("#speaker-new-name")).to_be_focused()
    footer = page.locator(".speaker-new-footer")
    expect(footer).to_be_visible()
    box, viewport = footer.bounding_box(), page.viewport_size
    assert box["y"] + box["height"] <= viewport["height"]         # the footer is on screen, not below the fold
    page.keyboard.press("Escape")
    expect(modal).not_to_have_class(re.compile(r"showing-new"))
    expect(page.locator("#speaker-modal")).to_be_visible()


BEHIND_THE_DIALOG = """(() => {
  const a = document.activeElement;
  return a !== document.body && !document.getElementById('speaker-modal').contains(a);
})()"""


def test_tab_goes_round_the_dialog_and_never_into_the_page_behind_it(reader):
    # Past the last control focus may rest on the body (the browser's own address bar, in a real window), never on the page.
    page = reader()
    open_dialog(page)
    for direction in ("Tab", "Shift+Tab"):
        visited = set()
        for press in range(30):
            page.keyboard.press(direction)
            assert not page.evaluate(BEHIND_THE_DIALOG), f"focus reached the page behind after {press + 1} x {direction}"
            visited.add(page.evaluate("document.activeElement.textContent.trim().slice(0, 12)"))
        assert {"Apply", "Cancel", "Reset", "Debug info"} <= visited     # it does reach every button


def test_debug_info_has_a_deep_link_to_this_sentence(site, reader):
    page = reader()
    idx = open_dialog(page)
    page.get_by_role("button", name="Debug info").click()
    text = page.locator("#speaker-debug-text").input_value()
    assert f"/reader/{site.book_id}?chapter={site.chapter_id}&segment={idx}" in text
