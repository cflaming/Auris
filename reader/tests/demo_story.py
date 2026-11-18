"""A short, deterministic story used by the tests.

Written so the attribution heuristics have clear answers (explicit "X said"
tags, turn-taking, pronouns) plus a few lines they cannot resolve.
"""

CHAPTER_ONE = """\
Amy stood at the window of the lab. Rain streaked the glass.

"We should leave before dawn," Amy said. "The patrol changes at six."

Carol shook her head. "That is far too early."

"Then when?" Amy asked.

"After the shift change," she replied. "Not a minute sooner."

Dean walked in carrying a lantern. "No." He set it down. "No. We stay put."

* * *

The room fell quiet. Nobody argued with Dean."""

CHAPTER_TWO = """\
Morning came grey and cold. Amy packed the last of the supplies.

"Ready?" Carol said.

"Ready," said Amy."""

CHARACTERS = [  # (name, gender)
    ("Amy", "female"),
    ("Carol", "female"),
    ("Dean", "male"),
]

# The same sentence with the same sentence before it, twice: only an occurrence
# counter tells the two "No," lines apart.
AMBIGUOUS_REPEAT = 'Nobody spoke. "No," Dean said.\n\nNobody spoke. "No," Dean said.'
