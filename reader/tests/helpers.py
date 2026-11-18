"""Small helpers shared by the tests."""


def spoken(segs):
    """[(text, speaker)] for the spoken (dialogue) segments."""
    return [(s["text"], s["character_name"]) for s in segs if s["is_dialogue"]]


def by_text(segs, text):
    """The segment with exactly this text."""
    return next(s for s in segs if s["text"] == text)
