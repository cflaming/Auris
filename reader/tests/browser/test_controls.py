"""Playback-bar controls and Voice Studio."""

from playwright.sync_api import expect


def test_speed_slider_steps_to_three_times_and_its_label_never_changes_width(reader):
    page = reader()
    widths = set()
    seen = {}
    for notch in range(20):
        page.locator("#speed-slider").evaluate("(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); }", notch)
        seen[notch] = page.locator("#speed-val").inner_text()
        widths.add(round(page.locator("#speed-val").bounding_box()["width"], 1))
    assert seen[0] == "0.5×" and seen[5] == "1.0×" and seen[15] == "2.0×" and seen[19] == "3.0×"
    assert list(seen.values()) == sorted(seen.values(), key=lambda t: float(t[:-1]))      # steps only go up
    assert len(widths) == 1, f"the label's width changed between speeds: {widths}"


def test_clicking_the_speed_label_resets_to_normal_speed(reader):
    page = reader()
    page.locator("#speed-slider").evaluate("el => { el.value = 19; el.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.locator("#speed-val").click()
    expect(page.locator("#speed-val")).to_have_text("1.0×")


def test_only_hand_added_characters_with_no_assignments_can_be_deleted(site, page, perf):
    site.api(f"/api/books/{site.book_id}/characters", {"name": "Zed"})
    page.goto(f"{site.url}/voice-studio/{site.book_id}")
    page.wait_for_selector(".character-card")
    cards = page.locator(".character-card")
    named = lambda name: cards.filter(has=page.locator(".char-name", has_text=name))      # (every card lists every name in its merge menu)
    zed, amy = named("Zed"), named("Amy")
    expect(zed.get_by_role("button", name="Delete")).to_have_count(1)
    expect(amy.get_by_role("button", name="Delete")).to_have_count(0)    # detected, not hand-added

    page.once("dialog", lambda d: d.accept())
    zed.get_by_role("button", name="Delete").click()
    expect(named("Zed")).to_have_count(0)
