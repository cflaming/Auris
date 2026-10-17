"""
Text enrichment engine.

Splits chapter text into TTS-ready segments, attributes dialogue to characters,
injects OmniVoice non-verbal tags, adjusts speed for scene tone, and avoids
common sentence-boundary mistakes such as "Mr." or "Dr." being treated as a
full stop.
"""

import re

# Bump whenever the enrichment output changes in a way that requires
# existing stored segments to be discarded and rebuilt.
ENRICHMENT_VERSION = 1

_DOT = "<prd>"
_ELLIPSIS = "<ell>"
_SPLIT = "<split>"
_QUOTE_CLASS = r'["\u201c\u201d]'
_QUOTE_CONTENT_CLASS = r'"\u201c\u201d'
_NAME_PATTERN = r"[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,2}"

# Matches section-break dinkuses: * * *, ***, ---, ◆, ✦, ★, #, lone em-dash, etc.
_DINKUS_RE = re.compile(
    r"^\s*(?:\*\s*){2,}\*\s*$"       # * * * or *** or * * * * *
    r"|^\s*[-—]{3,}\s*$"             # --- or ———
    r"|^\s*[◆◇✦✧★☆•·#]\s*$"         # single decorative glyph
    r"|^\s*(?:[◆◇✦✧★☆•·#]\s*){2,}$", # repeated glyphs
    re.MULTILINE,
)

_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?:chapter|ch\.?)\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty(?:\s*-\s*\w+)?)"
    r"|part\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"|prologue|epilogue|foreword|preface|introduction|afterword|appendix|interlude"
    r")\b.*$",
    re.IGNORECASE,
)

_SHORT_ALL_CAPS_RE = re.compile(r"^[A-Z0-9][A-Z0-9 '&,:;.-]{1,80}$")
_QUESTION_RE = re.compile(r'\?\s*["\u201d]?\s*$')
_SURPRISE_RE = re.compile(r'!\s*["\u201d]?\s*$')
_SHOCKED_QUESTION_END_RE = re.compile(r'\?!\s*["\u201d]?\s*$')

# \u2500\u2500 Question context refiners \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
_QUESTION_HINT_RE = re.compile(
    r"\b(asked|wondered|queried|questioned|inquired|demanded|challenged)\b",
    re.IGNORECASE,
)
# Skeptical / rhetorical: raised eyebrow, disbelief, sarcasm
_SKEPTIC_CONTEXT_RE = re.compile(
    r"\b(scoff|scoffed|sneer|sneered|skeptic|sarcast|disdain|"
    r"smirk|smirked|raised.*eyebrow|narrowed.*eyes|rolled.*eyes|"
    r"doubt|doubted|disbelief|incredulous|dismissive)\b",
    re.IGNORECASE,
)
# Shocked / disbelieving questions
_SHOCK_CONTEXT_RE = re.compile(
    r"\b(shock|shocked|horrified|frozen|stunned|stagger|recoil|"
    r"jaw dropped|speechless|aghast|pale|couldn't believe|"
    r"taken aback|dumbstruck|wide.eyed)\b",
    re.IGNORECASE,
)
# Wondering / curious questions
_WONDER_CONTEXT_RE = re.compile(
    r"\b(wonder|curious|pondered|puzzle|puzzled|contemplat|mused|"
    r"tilted.*head|furrowed.*brow|peered|squinted|speculated|mulled)\b",
    re.IGNORECASE,
)

# \u2500\u2500 Surprise context refiners \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
_SURPRISE_HINT_RE = re.compile(
    r"\b(gasp|gasped|gasping|exclaim|exclaimed|cried out|startled|shouted|yelled|"
    r"yelped|screamed|shrieked|recoiled|flinched|jumped back)\b",
    re.IGNORECASE,
)
# Strong shock \u2014 jaw-dropping, screaming, impossible
_STRONG_SURPRISE_RE = re.compile(
    r"\b(gasp|gasped|shriek|shrieked|scream|screamed|"
    r"jaw dropped|impossible|unbelievable|recoil|recoiled|"
    r"stunned|flinch|flinched|couldn't believe|speechless|aghast|"
    r"mind went blank|froze in place|blood ran cold)\b",
    re.IGNORECASE,
)
# Excited / triumphant surprise
_EXCITED_SURPRISE_RE = re.compile(
    r"\b(finally|at last|triumph|triumphant|victory|succeed|succeeded|"
    r"incredible|amazing|wonderful|brilliant|breakthrough|"
    r"beamed|cheered|lit up|leaped for joy|eyes shone)\b",
    re.IGNORECASE,
)
# Mild realization / dawning understanding
_MILD_REALIZATION_RE = re.compile(
    r"\b(realiz|reali[sz]ed|dawned|suddenly understood|remembered|"
    r"occurred to|it hit|clicked|made sense|recognition|it struck)\b",
    re.IGNORECASE,
)

# \u2500\u2500 Emotion word patterns \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
_LAUGHTER_RE = re.compile(
    r"\b(laugh|laughs|laughed|laughing|chuckl|giggl|"
    r"grinned|grinning|amused|teased|joked|snickered|cackled|"
    r"burst out laughing|couldn't help laughing)\b",
    re.IGNORECASE,
)
_SIGH_RE = re.compile(
    r"\b(sigh|sighs|sighed|sighing|exhale|exhaled|"
    r"breathed out|let out a (?:long |weary |heavy |deep )?breath|"
    r"heaved a sigh|resigned(?:ly)?)\b",
    re.IGNORECASE,
)
_DISSATISFACTION_RE = re.compile(
    r"\b(grumbl|mutter|growl|snapp|barked|hiss|scowl|gritted|"
    r"glared|glaring|frowned|frowning|stormed|huffed|fumed|"
    r"seethed|snarled|glowered|bristled|sneered at|"
    r"slammed|threw.*down|shook.*head in disgust)\b",
    re.IGNORECASE,
)
_CONFIRMATION_RE = re.compile(
    r"\b(nod|nodded|nodding|agreed|confirm|confirmed|affirm|affirmed|assented|"
    r"concurred|gave a nod|tilted his head in agreement|tilted her head in agreement)\b",
    re.IGNORECASE,
)
_WHISPER_RE = re.compile(
    r"\b(whisper|whispered|breathed|murmured|under his breath|under her breath|"
    r"barely audible|in a low voice|hissed softly)\b",
    re.IGNORECASE,
)

_TAG_RULES = [
    (_LAUGHTER_RE,       "[laughter]"),
    (_SIGH_RE,           "[sigh]"),
    (_DISSATISFACTION_RE,"[dissatisfaction-hnn]"),
    (_CONFIRMATION_RE,   "[confirmation-en]"),
]
_ATTRIBUTION_VERBS = (
    "said|replied|asked|whispered|shouted|cried|muttered|exclaimed|called|added|"
    "continued|laughed|sighed|groaned|snapped|retorted|insisted|demanded|pleaded|"
    "began|noted|observed|remarked|growled|yelled|murmured|gasped|stammered|shrieked"
)
_ATTRIBUTION_SENTENCE_RE = re.compile(
    rf"^(?:{_NAME_PATTERN}|he|she|they)\s+(?:{_ATTRIBUTION_VERBS})(?:\s+\w+){{0,4}}[.!?]?$",
    re.IGNORECASE,
)

_STANDALONE_QUOTE_RE = re.compile(rf'{_QUOTE_CLASS}([^"\u201c\u201d]{{2,}}){_QUOTE_CLASS}')

_ACTION_WORDS = re.compile(
    r"\b(ran|rushed|sprinted|struck|fell|crashed|burst|grabbed|pulled|pushed|"
    r"slammed|exploded|screamed|fired|attacked|fled|chased|leaped|jumped|"
    r"stabbed|shot|hit|smashed|broke|shattered)\b",
    re.IGNORECASE,
)
_SLOW_WORDS = re.compile(
    r"\b(slowly|gently|quietly|softly|carefully|tenderly|silently|"
    r"solemnly|mournfully|peacefully|dreamily)\b",
    re.IGNORECASE,
)

_ABBREVIATION_WORDS = (
    "Mr", "Mrs", "Ms", "Dr", "Prof", "Sr", "Jr", "St", "Mt", "Lt", "Capt",
    "Col", "Gen", "Sgt", "Rev", "Hon", "Pres", "Gov", "Sen", "Rep", "Supt",
    "Det", "No", "Nos", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug",
    "Sep", "Sept", "Oct", "Nov", "Dec", "etc", "vs",
)
_ABBREVIATION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(item) for item in _ABBREVIATION_WORDS) + r")\.",
    re.IGNORECASE,
)
_MULTI_DOT_TOKEN_RE = re.compile(
    r"\b(?:e\.g\.|i\.e\.|a\.m\.|p\.m\.|u\.s\.a?\.|u\.k\.|u\.n\.|ph\.d\.)",
    re.IGNORECASE,
)
_INITIALISM_RE = re.compile(r"\b(?:[A-Z]\.){2,}")
_NAME_INITIAL_RE = re.compile(r"\b[A-Z]\.(?=\s+[A-Z][a-z])")


def _scene_speed(text: str) -> float:
    action = len(_ACTION_WORDS.findall(text))
    slow = len(_SLOW_WORDS.findall(text))
    if action >= 3:
        return 1.15
    if slow >= 2:
        return 0.9
    return 1.0


def _title_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()


def _is_short_all_caps_heading(text: str) -> bool:
    words = text.split()
    return len(words) <= 10 and bool(_SHORT_ALL_CAPS_RE.match(text))


def _is_heading_paragraph(text: str, chapter_title: str | None = None) -> bool:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return False
    if chapter_title and _title_key(cleaned) == _title_key(chapter_title):
        return True
    if _SECTION_HEADING_RE.match(cleaned):
        return True
    if _is_short_all_caps_heading(cleaned) and not re.search(r"[.!?]", cleaned):
        return True
    return False


def _line_starts_new_paragraph(previous_line: str, current_line: str) -> bool:
    prev = previous_line.rstrip()
    curr = current_line.lstrip()
    if not prev:
        return True
    if _is_heading_paragraph(curr):
        return True
    return bool(re.search(r'[.!?]["\u201d]?\s*$', prev))


def _split_paragraphs(text: str, chapter_title: str | None = None) -> list[str]:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n+", raw)
    paragraphs: list[str] = []

    for block in blocks:
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in block.splitlines()
            if re.sub(r"\s+", " ", line).strip()
        ]
        if not lines:
            continue

        buffer: list[str] = []
        for line in lines:
            if _is_heading_paragraph(line, chapter_title):
                if buffer:
                    paragraphs.append(" ".join(buffer).strip())
                    buffer = []
                paragraphs.append(line)
                continue
            if buffer and _line_starts_new_paragraph(buffer[-1], line):
                paragraphs.append(" ".join(buffer).strip())
                buffer = [line]
            else:
                buffer.append(line)
        if buffer:
            paragraphs.append(" ".join(buffer).strip())

    cleaned: list[str] = []
    for idx, paragraph in enumerate(paragraphs):
        if _is_heading_paragraph(paragraph, chapter_title):
            continue
        if idx < 2 and _is_short_all_caps_heading(paragraph):
            continue
        cleaned.append(paragraph)

    return cleaned


def _protect_sentence_boundaries(text: str) -> str:
    protected = text.replace("...", _ELLIPSIS)
    protected = re.sub(r"(?<=\d)\.(?=\d)", _DOT, protected)
    protected = _MULTI_DOT_TOKEN_RE.sub(lambda match: match.group(0).replace(".", _DOT), protected)
    protected = _INITIALISM_RE.sub(lambda match: match.group(0).replace(".", _DOT), protected)
    protected = _NAME_INITIAL_RE.sub(lambda match: match.group(0).replace(".", _DOT), protected)
    protected = _ABBREVIATION_RE.sub(lambda match: match.group(0).replace(".", _DOT), protected)
    return protected


def _restore_sentence_boundaries(text: str) -> str:
    return text.replace(_DOT, ".").replace(_ELLIPSIS, "...")


def _split_paragraph_sentences(paragraph: str) -> list[str]:
    protected = _protect_sentence_boundaries(paragraph)
    protected = re.sub(
        r'([.!?]["\u201d]?)\s+(?=(?:["\u201c]?[A-Z0-9]))',
        rf"\1{_SPLIT}",
        protected,
    )
    parts = [_restore_sentence_boundaries(part).strip() for part in protected.split(_SPLIT)]
    return [part for part in parts if part]


def _has_dialogue(text: str) -> bool:
    return bool(_STANDALONE_QUOTE_RE.search(text))


def _should_merge_sentences(buffer: str, sentence: str) -> bool:
    if not buffer or not sentence:
        return False
    if _has_dialogue(buffer) and _ATTRIBUTION_SENTENCE_RE.match(sentence):
        return True
    if _ATTRIBUTION_SENTENCE_RE.match(buffer) and _has_dialogue(sentence):
        return True
    if _QUESTION_RE.search(buffer) or _SURPRISE_RE.search(buffer):
        return False
    if _has_dialogue(buffer) != _has_dialogue(sentence):
        return False
    combined_words = len((buffer + " " + sentence).split())
    if combined_words > 30:
        return False
    if len(buffer.split()) < 8 or len(sentence.split()) < 4:
        return True
    return not _has_dialogue(buffer) and not _has_dialogue(sentence) and combined_words <= 22


def _segment_text(text: str) -> list[str]:
    """Split a run of text into sentences, merging short neighbours into
    TTS-sized segments."""
    segments: list[str] = []
    buffer = ""
    for sentence in _split_paragraph_sentences(text):
        if not buffer:
            buffer = sentence
        elif _should_merge_sentences(buffer, sentence):
            buffer = f"{buffer} {sentence}".strip()
        else:
            segments.append(buffer)
            buffer = sentence
    if buffer:
        segments.append(buffer)
    return segments


# ── Dialogue spans and speaker attribution ───────────────────────────────────
#
# Each paragraph is split into quoted and narration spans. Quoted spans are
# voiced by the paragraph's speaker (fiction convention: one speaker per
# paragraph); narration, including attributions like "she said", goes to the
# narrator. The speaker comes from the strongest clue available: an explicit
# speech verb next to a quote, an action beat next to it, a continued quote,
# or turn-taking in a two-person exchange.

_SPEECH_VERB_RE = re.compile(
    r"\b(?:"
    r"sa(?:id|ys|ying)|ask(?:ed|s|ing)|repl(?:ied|ies|ying)|answer(?:ed|s|ing)|"
    r"respond(?:ed|s|ing)|whisper(?:ed|s|ing)|shout(?:ed|s|ing)|yell(?:ed|s|ing)|"
    r"call(?:ed|s|ing)|cr(?:ied|ies|ying)|mutter(?:ed|s|ing)|mumbl(?:ed|es|ing)|"
    r"murmur(?:ed|s|ing)|exclaim(?:ed|s|ing)|add(?:ed|s|ing)|continu(?:ed|es|ing)|"
    r"laugh(?:ed|s|ing)|chuckl(?:ed|es|ing)|giggl(?:ed|es|ing)|sigh(?:ed|s|ing)|"
    r"groan(?:ed|s|ing)|snap(?:ped|s|ping)|retort(?:ed|s|ing)|insist(?:ed|s|ing)|"
    r"demand(?:ed|s|ing)|plead(?:ed|s|ing)|beg(?:ged|s|ging)|not(?:ed|es|ing)|"
    r"observ(?:ed|es|ing)|remark(?:ed|s|ing)|growl(?:ed|s|ing)|gasp(?:ed|s|ing)|"
    r"stammer(?:ed|s|ing)|stutter(?:ed|s|ing)|shriek(?:ed|s|ing)|scream(?:ed|s|ing)|"
    r"breath(?:ed|es|ing)|hiss(?:ed|es|ing)|bark(?:ed|s|ing)|snort(?:ed|s|ing)|"
    r"scoff(?:ed|s|ing)|huff(?:ed|s|ing)|grumbl(?:ed|es|ing)|grunt(?:ed|s|ing)|"
    r"teas(?:ed|es|ing)|jok(?:ed|es|ing)|interrupt(?:ed|s|ing)|protest(?:ed|s|ing)|"
    r"correct(?:ed|s|ing)|suggest(?:ed|s|ing)|offer(?:ed|s|ing)|admit(?:ted|s|ting)|"
    r"agree(?:d|s|ing)|explain(?:ed|s|ing)|announc(?:ed|es|ing)|declar(?:ed|es|ing)|"
    r"warn(?:ed|s|ing)|promis(?:ed|es|ing)|conced(?:ed|es|ing)|counter(?:ed|s|ing)|"
    r"chim(?:ed|es|ing)|pip(?:ed|es|ing) up|spoke|speaks|speaking|told|tells|telling|"
    r"quip(?:ped|s|ping)|drawl(?:ed|s|ing)|purr(?:ed|s|ing)|sniff(?:ed|s|ing)|"
    r"sob(?:bed|s|bing)|wonder(?:ed|s|ing)|mus(?:ed|es|ing)|prompt(?:ed|s|ing)|"
    r"confirm(?:ed|s|ing)|greet(?:ed|s|ing)|inquir(?:ed|es|ing)|enquir(?:ed|es|ing)|"
    r"question(?:ed|s|ing)|stat(?:ed|es|ing)|order(?:ed|s|ing)|command(?:ed|s|ing)"
    r")\b",
    re.IGNORECASE,
)
_SUBJECT_PRONOUN_RE = re.compile(r"\b(?:(?P<he>[Hh]e)|(?P<she>[Ss]he)|(?P<i>I))\b(?!['’])")
_TITLE_WORDS = {
    "mr", "mrs", "ms", "miss", "mister", "missus", "dr", "doctor", "lady", "lord",
    "sir", "madam", "captain", "professor", "aunt", "uncle",
}
# Optional title directly before a name, e.g. "Mister Hebert".
_TITLE_PREFIX_RE = re.compile(
    r"(?:\b(?:" + "|".join(sorted(_TITLE_WORDS, key=len, reverse=True)) + r")\.?\s+)?$",
    re.IGNORECASE,
)
# Capitalised words that start clauses but are not names.
_NOT_NAMES = {
    "The", "A", "An", "But", "And", "Then", "So", "Still", "Yet", "Now", "Well",
    "This", "That", "It", "They", "We", "You", "Everyone", "Someone", "Nobody",
}
# A clause opening with an unnamed noun phrase: "The man stood up, saying".
_UNNAMED_SUBJECT_RE = re.compile(r"^\s*(?:The|A|An|One of)\b")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
# Coordinating joins start a clause with its own subject ("..., but Carol was");
# subordinate ones (when/while/as) do not name who speaks.
_CLAUSE_JOIN_RE = re.compile(r"(?:,|\b(?:but|and|so|yet))\s*$", re.IGNORECASE)
_SCENE_BREAK_RE = re.compile(r"^\s*(?:[-*#~=_]\s*){3,}")
# Consecutive quote-free paragraphs after which an exchange is over.
_CONVERSATION_GAP = 3
# How many paragraphs back "he"/"she" may refer to a named character.
_PRONOUN_WINDOW = 8


def _split_quote_spans(paragraph: str) -> tuple[list[tuple[bool, str]], bool]:
    """Split a paragraph into (is_quote, text) spans, quote marks included in
    the quoted spans. Straight quotes toggle; curly quotes open and close.
    Also returns whether the paragraph ends inside an open quote, which in
    fiction means the same speaker continues in the next paragraph."""
    spans: list[tuple[bool, str]] = []
    buf: list[str] = []
    inside = False
    carry = ""

    def flush(is_quote: bool):
        nonlocal carry
        text = (carry + "".join(buf)).strip()
        buf.clear()
        carry = ""
        if not text:
            return
        if not re.search(r"[A-Za-z0-9]", text):
            # Bare punctuation between quotes: keep it with its neighbour.
            if spans:
                prev_quote, prev_text = spans[-1]
                spans[-1] = (prev_quote, f"{prev_text}{text}" if text[0] in ",.;:!?" else f"{prev_text} {text}")
            else:
                carry = text + " "
            return
        spans.append((is_quote, text))

    for ch in paragraph:
        if ch == "“" or (ch == '"' and not inside):
            if not inside:
                flush(False)
                inside = True
            buf.append(ch)
        elif ch == "”" or (ch == '"' and inside):
            buf.append(ch)
            if inside:
                flush(True)
                inside = False
        else:
            buf.append(ch)
    ended_open = inside and bool("".join(buf).strip())
    flush(inside)
    return spans, ended_open


def _name_matcher(character_map: dict) -> tuple[re.Pattern | None, dict[str, str]]:
    """Regex over character names (plus title-less aliases such as "Militia"
    for "Miss Militia") and the alias -> canonical name map. A character
    merged into another (its dict has "canonical") resolves to that one."""
    def canonical(name: str) -> str:
        target = (character_map.get(name) or {}).get("canonical")
        return target if target in character_map else name

    aliases = {name: canonical(name) for name in character_map}
    candidates: dict[str, set[str]] = {}
    for name in character_map:
        words = name.split()
        if len(words) > 1 and words[0].lower().rstrip(".") in _TITLE_WORDS:
            candidates.setdefault(" ".join(words[1:]), set()).add(canonical(name))
    for alias, names in candidates.items():
        if len(names) == 1 and alias not in aliases:
            aliases[alias] = next(iter(names))
    if not aliases:
        return None, {}
    alternation = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
    # Skip possessives: "Taylor's father said" is not Taylor speaking.
    return re.compile(rf"\b(?:{alternation})\b(?!['’]s\b)"), aliases


def _subjects(clause: str, names_re, aliases) -> list[tuple[int, str, str]]:
    """(position, kind, value) for names and subject pronouns in a clause.
    kind is "name" (value = canonical name), "pronoun" (value = gender) or
    "first_person"."""
    found = []
    if names_re:
        for m in names_re.finditer(clause):
            found.append((m.start(), "name", aliases[m.group(0)]))
    for m in _SUBJECT_PRONOUN_RE.finditer(clause):
        if m.group("i"):
            found.append((m.start(), "first_person", ""))
        else:
            found.append((m.start(), "pronoun", "male" if m.group("he") else "female"))
    return sorted(found)


def _at_clause_start(clause: str, pos: int) -> bool:
    """True when the word at pos starts a (sub)clause, i.e. is likely its
    subject: at the start, after an intro phrase ("Cutting through the
    pleasantries, Carol"), or after a conjunction ("..., but Carol")."""
    before = _TITLE_PREFIX_RE.sub("", clause[:pos])
    return not before.strip() or bool(_CLAUSE_JOIN_RE.search(before))


def _clause_clue(clause: str, names_re, aliases, explicit_only: bool, last_subject: bool = False):
    """Return (kind, value) naming who speaks according to one narration
    clause, or None. kind: name, pronoun, first_person or unknown (value is
    the untracked name, or "" when the verb has no recognisable subject).
    For beats, last_subject picks the clause's last subject ("Amy wished...,
    but Carol was...") instead of its first."""
    verb = _SPEECH_VERB_RE.search(clause)
    subjects = _subjects(clause, names_re, aliases)
    if verb:
        before = [s for s in subjects if s[0] < verb.start()]
        leading = [s for s in before if _at_clause_start(clause, s[0])]
        if leading:
            return leading[0][1:]
        if _UNNAMED_SUBJECT_RE.match(clause):
            return ("unknown", "")
        if before:
            return before[0][1:]
        after = [s for s in subjects if 0 <= s[0] - verb.end() <= 2]
        if after:                                   # inverted: said Carol
            return after[0][1:]
        word = re.search(r"\b([A-Z][a-z]+)\s+$", clause[:verb.start()])
        if word and word.group(1) not in _NOT_NAMES:
            return ("unknown", word.group(1))       # a name we don't track
        return ("unknown", "")
    if explicit_only:
        return None
    beats = [(kind, value) for pos, kind, value in subjects if _at_clause_start(clause, pos)]
    if not beats:
        return None
    return beats[-1] if last_subject else beats[0]


def _adjacent_clauses(spans, idx: int) -> list[tuple[str, str]]:
    """Narration clauses touching quote span idx: ("after", first clause of
    the following narration), then ("before", last clause of the preceding)."""
    out = []
    if idx + 1 < len(spans) and not spans[idx + 1][0]:
        out.append(("after", _CLAUSE_SPLIT_RE.split(spans[idx + 1][1])[0]))
    if idx > 0 and not spans[idx - 1][0]:
        out.append(("before", _CLAUSE_SPLIT_RE.split(spans[idx - 1][1])[-1]))
    return out


def _paragraph_clue(spans, names_re, aliases):
    quote_idx = [i for i, (is_quote, _) in enumerate(spans) if is_quote]
    if not quote_idx:
        return None
    for i in quote_idx:
        for _, clause in _adjacent_clauses(spans, i):
            clue = _clause_clue(clause, names_re, aliases, explicit_only=True)
            if clue:
                return clue
    # Beats around the first quote: the last two sentences leading into it,
    # nearest first, then the sentence right after it.
    first = quote_idx[0]
    beat_clauses = []
    if first > 0 and not spans[first - 1][0]:
        beat_clauses += [(c, True) for c in reversed(_CLAUSE_SPLIT_RE.split(spans[first - 1][1])[-2:])]
    if first + 1 < len(spans) and not spans[first + 1][0]:
        beat_clauses.append((_CLAUSE_SPLIT_RE.split(spans[first + 1][1])[0], False))
    for clause, leading in beat_clauses:
        clue = _clause_clue(clause, names_re, aliases, explicit_only=False, last_subject=leading)
        if clue:
            return clue
    return None


def _addressed(spans, names_re, aliases) -> str | None:
    """A character addressed by name in the paragraph's speech ("..., Taylor?"
    or "Amy, ..."), who is likely to answer next."""
    if not names_re:
        return None
    found = None
    for is_quote, text in spans:
        if not is_quote:
            continue
        for m in names_re.finditer(text):
            before = text[:m.start()].rstrip()
            after = text[m.end():m.end() + 1]
            if (before.endswith(",") or before.endswith(("\"", "\u201c"))) and after in (",", "?", "!", "."):
                found = aliases[m.group(0)]
    return found


def _gender_ok(name: str, gender: str, character_map: dict) -> bool:
    known = (character_map.get(name) or {}).get("gender") or "unknown"
    return known not in ("male", "female") or known == gender


def _attribute_paragraphs(paragraph_spans: list, character_map: dict) -> list[str | None]:
    """Return the speaker (a character_map name, or None for the narrator)
    for the quoted spans of each paragraph."""
    names_re, aliases = _name_matcher(character_map)
    speakers: list[str | None] = []
    turns: list[str] = []          # speaker keys of the current exchange
    mentions: list[tuple[int, str]] = []  # (paragraph, name) in narration, oldest first
    quiet = 0                      # consecutive paragraphs without quotes
    continued_from: str | None = None
    addressed: str | None = None   # named in the previous speech

    for para_no, (spans, ended_open) in enumerate(paragraph_spans):
        has_quote = any(is_quote for is_quote, _ in spans)
        narration = " ".join(text for is_quote, text in spans if not is_quote)
        if _SCENE_BREAK_RE.match(spans[0][1]):
            turns.clear()
            addressed = None
        local = [aliases[m.group(0)] for m in names_re.finditer(narration)] if names_re else []

        if not has_quote:
            speakers.append(None)
            quiet += 1
            if quiet >= _CONVERSATION_GAP:
                turns.clear()
                addressed = None
            mentions.extend((para_no, name) for name in local)
            continued_from = None
            continue
        quiet = 0

        last = turns[-1] if turns else None
        alt = turns[-2] if len(turns) >= 2 and turns[-2] != last and turns[-2] in character_map else None
        if addressed and addressed != last:
            alt = addressed            # "..., Taylor?" -> Taylor answers
        clue = _paragraph_clue(spans, names_re, aliases)
        if clue == ("unknown", ""):
            clue = None                # "the teen replied": someone, but who
        key = "?"
        speaker = None

        if clue is None and continued_from is not None:
            speaker, key = (continued_from if continued_from in character_map else None), continued_from
        elif clue is None:
            speaker = alt
            key = alt or "?"
        elif clue[0] == "name":
            speaker = key = clue[1]
        elif clue[0] == "first_person":
            key = "I"
        elif clue[0] == "unknown":
            key = f"?{clue[1]}"
        elif clue[0] == "pronoun":
            gender = clue[1]
            if alt and _gender_ok(alt, gender, character_map):
                speaker = alt
            else:
                recent = [n for p_no, n in mentions if para_no - p_no <= _PRONOUN_WINDOW]
                for name in reversed(recent + local):
                    if name != last and _gender_ok(name, gender, character_map):
                        speaker = name
                        break
            key = speaker or f"?{gender}"

        speakers.append(speaker)
        if not (continued_from is not None and clue is None):
            turns.append(key)
        mentions.extend((para_no, name) for name in local)
        del mentions[:-30]
        continued_from = key if ended_open else None
        addressed = _addressed(spans, names_re, aliases)

    return speakers


def _span_attribution(spans, idx: int) -> tuple[str, str]:
    """For quote span idx, the adjacent narration clause that describes how it
    is said, and which side it is on ("after"/"before"/"")."""
    clauses = _adjacent_clauses(spans, idx)
    return (clauses[0][1], clauses[0][0]) if clauses else ("", "")


def _explicit_tag_text(sentence: str) -> str:
    sentence = str(sentence or "").strip()
    inner = _STANDALONE_QUOTE_RE.search(sentence)
    if not inner:
        return sentence

    quoted = inner.group(1).strip()
    prefix = sentence[:inner.start()].strip(" ,.-")
    suffix = sentence[inner.end():].strip(" ,.-")
    parts = [part for part in (quoted, prefix, suffix) if part]
    return " ".join(parts).strip()


def _should_allow_confirmation_tag(text: str, is_dialogue: bool) -> bool:
    if not is_dialogue:
        return False

    normalized = re.sub(r"[^a-zA-Z0-9\s']", " ", str(text or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False

    words = normalized.split()
    if len(words) > 4:
        return False

    allowed_phrases = {
        "yes", "yeah", "yep", "yup", "okay", "ok", "sure", "right",
        "indeed", "exactly", "correct", "of course", "certainly",
        "i know", "i see", "all right",
    }
    return normalized in allowed_phrases


def _select_expression_tag(sentence: str, context: str, is_dialogue: bool) -> str | None:
    is_question   = bool(_QUESTION_RE.search(sentence))
    is_exclamation = bool(_SURPRISE_RE.search(sentence))
    tag_text = _explicit_tag_text(sentence)
    if context and context != sentence:
        # Attribution narration ("she laughed") now lives in its own segment.
        tag_text = f"{tag_text} {context}"

    # Question tags are explicit non-verbal symbols in OmniVoice. Using them on
    # every normal question adds unwanted pre-utterance sounds, so only keep
    # them for clearly stylized dialogue questions.
    if is_dialogue and is_question:
        if _SKEPTIC_CONTEXT_RE.search(tag_text):
            return "[question-ei]"         # sceptical / rhetorical
        if _SHOCK_CONTEXT_RE.search(tag_text) or _SHOCKED_QUESTION_END_RE.search(sentence):
            return "[question-oh]"         # shocked / disbelieving
        if _WONDER_CONTEXT_RE.search(tag_text):
            return "[question-ah]"         # curious / wondering
        return None

    # Explicit emotion attribution should be sentence-local, otherwise nearby
    # narration leaks non-verbal sounds like "mm" into unrelated lines.
    if is_dialogue:
        for pattern, tag in _TAG_RULES:
            if not pattern.search(tag_text):
                continue
            if tag == "[confirmation-en]" and not _should_allow_confirmation_tag(tag_text, is_dialogue):
                continue
            return tag

    # Quiet realizations don't need an exclamation mark to warrant a tag
    if _MILD_REALIZATION_RE.search(tag_text):
        return "[surprise-ah]"

    # Surprise / exclamation — differentiated by intensity
    if is_exclamation or _SURPRISE_HINT_RE.search(tag_text):
        if _STRONG_SURPRISE_RE.search(tag_text):
            return "[surprise-wa]"         # jaw-drop / scream shock
        if _EXCITED_SURPRISE_RE.search(tag_text):
            return "[surprise-yo]"         # triumphant / elated
        if _MILD_REALIZATION_RE.search(tag_text):
            return "[surprise-ah]"         # gentle dawning realization
        return "[surprise-oh]"             # default surprise

    return None


def _inject_tags(sentence: str, context: str, is_dialogue: bool) -> tuple[str, str | None]:
    tag = _select_expression_tag(sentence, context, is_dialogue)
    if not tag:
        return sentence, None

    inner = _STANDALONE_QUOTE_RE.search(sentence)
    if inner:
        quoted = inner.group(1).strip()
        enriched_quoted = f"{tag} {quoted}".strip()
        sentence = sentence[:inner.start(1)] + enriched_quoted + sentence[inner.end(1):]
    else:
        sentence = f"{tag} {sentence}".strip()

    return sentence, tag


def _segment_speed(sentence: str, is_dialogue: bool, scene_speed: float, tag: str | None, is_whisper: bool) -> float:
    speed = scene_speed if is_dialogue else min(scene_speed, 1.05)

    if is_whisper or tag == "[sigh]":
        speed = min(speed, 0.94)
    elif tag == "[dissatisfaction-hnn]":
        speed = min(speed, 0.97)          # muttering is deliberate and slow
    elif tag == "[question-oh]":
        speed = min(speed, 0.96)          # shocked questions land harder when slower
    elif tag in {"[surprise-wa]", "[laughter]"} and is_dialogue:
        speed = max(speed, 1.03)          # shock and laughter burst out faster
    elif tag == "[surprise-yo]":
        speed = max(speed, 1.05)          # excited triumph is animated

    if "..." in sentence or " -- " in sentence or ";" in sentence or ":" in sentence:
        speed = min(speed, 0.98)

    return round(speed, 2)


def enrich_chapter(
    chapter_text: str,
    character_map: dict,
    narrator_instruct: str = "male, elderly, low pitch, british accent",
    single_narrator_mode: bool = False,
    chapter_title: str | None = None,
) -> list[dict]:
    """
    Return segment dicts used by playback and export.
    """
    cleaned_text = str(chapter_text or "").strip()
    scene_speed = _scene_speed(cleaned_text)
    paragraph_spans = [
        _split_quote_spans(paragraph)
        for paragraph in _split_paragraphs(cleaned_text, chapter_title)
    ]
    paragraph_spans = [(spans, ended_open) for spans, ended_open in paragraph_spans if spans]
    speakers = _attribute_paragraphs(paragraph_spans, character_map)

    segments = []
    last_tag: str | None = None
    for paragraph, ((spans, _), speaker) in enumerate(zip(paragraph_spans, speakers)):
        # Dinkus / section break — emit a silent ellipsis pause instead of reading it.
        raw_paragraph = " ".join(span_text for _, span_text in spans)
        if _DINKUS_RE.match(raw_paragraph.strip()):
            segments.append({
                "text": "",
                "enriched_text": "……………",
                "character_name": None,
                "instruct": narrator_instruct,
                "speed": 1.0,
                "is_dialogue": False,
                "is_whisper": False,
                "paragraph": paragraph,
                "is_section_break": True,
            })
            last_tag = None
            continue
        for span_idx, (is_quote, span_text) in enumerate(spans):
            attribution, side = _span_attribution(spans, span_idx) if is_quote else ("", "")
            parts = _segment_text(span_text)
            for part_idx, sentence in enumerate(parts):
                is_dialogue = is_quote
                character_name = speaker if is_quote and not single_narrator_mode else None
                is_whisper = is_quote and bool(_WHISPER_RE.search(sentence) or _WHISPER_RE.search(attribution))

                if character_name:
                    instruct = character_map.get(character_name, {}).get("instruct") or narrator_instruct
                else:
                    instruct = narrator_instruct
                if is_whisper and (single_narrator_mode or character_name) and "whisper" not in instruct:
                    instruct = instruct + ", whisper"

                # Only the part touching the attribution ("she laughed") sees it.
                touches = (side == "after" and part_idx == len(parts) - 1) or (side == "before" and part_idx == 0)
                context = attribution if touches else sentence
                enriched, tag = _inject_tags(sentence, context, is_dialogue)
                # Don't chain the same tag on back-to-back segments — it sounds repetitive.
                if tag is not None and tag == last_tag:
                    enriched, tag = sentence, None
                last_tag = tag
                speed = _segment_speed(sentence, is_dialogue, scene_speed, tag, is_whisper)
                segments.append(
                    {
                        "text": sentence,
                        "enriched_text": enriched,
                        "character_name": character_name,
                        "instruct": instruct,
                        "speed": speed,
                        "is_dialogue": is_dialogue,
                        "is_whisper": is_whisper,
                        "paragraph": paragraph,
                    }
                )

    return segments
