"""A character's page, reached from Voice Studio."""

from playwright.sync_api import expect


def open_amy(site, page):
    page.goto(f"{site.url}/voice-studio/{site.book_id}")
    page.locator(".char-name a", has_text="Amy").first.click()
    page.wait_for_selector(".excerpt")


def test_voice_studio_name_opens_the_characters_sentences(site, page, perf):
    open_amy(site, page)
    expect(page.locator("h1")).to_contain_text("Amy")
    assert page.locator(".excerpt").count() >= 1
    expect(page.locator("#summary")).to_contain_text("spoken by Amy")
    link = page.locator(".excerpt a", has_text="Open in reader").first
    assert "segment=" in link.get_attribute("href")


def test_changing_the_speaker_keeps_the_card_so_it_can_be_put_back(site, page, perf):
    open_amy(site, page)
    card = page.locator(".excerpt").first
    anchor = card.locator(".hit").first.get_attribute("data-anchor")
    card.locator("[data-speaker]").select_option("Carol")
    expect(card.locator("[data-moved]")).to_have_text("Now spoken by Carol")
    expect(card.locator(".hit.moved")).to_have_count(card.locator(".hit").count())

    page.goto(f"{site.url}/voice-studio/{site.book_id}")
    page.locator(".char-name a", has_text="Carol").first.click()
    expect(page.locator(f'.hit[data-anchor="{anchor}"]')).to_have_count(1)

    page.goto(f"{site.url}/voice-studio/{site.book_id}")
    page.locator(".char-name a", has_text="Amy").first.click()
    page.wait_for_selector(".excerpt")
    expect(page.locator(f'.hit[data-anchor="{anchor}"]')).to_have_count(0)
