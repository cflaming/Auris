"""Voice-instruct string parsing shared by TTS engines.

An instruct string is a comma-separated list such as
"male, elderly, low pitch, british accent".
"""

VOICE_GENDERS = {"male", "female"}
VOICE_AGES = {"child", "teenager", "young adult", "middle-aged", "elderly"}
VOICE_PITCHES = {
    "very low pitch",
    "low pitch",
    "moderate pitch",
    "high pitch",
    "very high pitch",
}
VOICE_STYLES = {"whisper"}


def parse_instruct(instruct: str) -> dict:
    parsed = {
        "gender": None,
        "age": None,
        "pitch": None,
        "accent": None,
        "styles": [],
        "extras": [],
    }

    for raw in str(instruct or "").split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item in VOICE_GENDERS:
            parsed["gender"] = item
        elif item in VOICE_AGES:
            parsed["age"] = item
        elif item in VOICE_PITCHES:
            parsed["pitch"] = item
        elif item in VOICE_STYLES:
            parsed["styles"].append(item)
        elif item.endswith("accent"):
            parsed["accent"] = item
        else:
            parsed["extras"].append(item)

    return parsed


def format_instruct(parts: dict) -> str | None:
    items = []
    for key in ("gender", "age", "pitch", "accent"):
        if parts.get(key):
            items.append(parts[key])
    items.extend(parts.get("styles", []))
    items.extend(parts.get("extras", []))
    return ", ".join(items) if items else None

