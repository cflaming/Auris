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

# A line repeated word for word, where nothing but its position tells the copies apart.
# AMBIGUOUS_REPEAT: same speaker, same preceding sentence. REPEATED_NO: different speakers.
AMBIGUOUS_REPEAT = 'Nobody spoke. "No," Dean said.\n\nNobody spoke. "No," Dean said.'
REPEATED_NO = 'Dean frowned. "No," Dean said.\n\nCarol laughed. "No," Carol said.'

# Adding "Dean" to Amy/Carol/Ben also moves a line between Narrator and Ben, not just to Dean:
# the new name changes who spoke last, which feeds the turn-taking guesses.
KNOCK_ON_STORY = '''"Yes." Carol said.

Dean frowned.

Dean nodded. "Fine."

"Maybe." Amy said.

Ben nodded. "Sure."

"Maybe."

Dean nodded. "Sure."

"Right." he said.

"Why?"'''

# "he said" goes to the most recently mentioned male character, so a new male Dean
# takes the line from Ben but a new female Dean does not.
PRONOUN_STORY = '''Ben leaned on the wall.

Dean arrived late.

"We are late," he said.'''

# "Militia" is an alias of "Miss Militia"; adding a "Mr. Militia" makes it ambiguous, so the
# alias stops matching even though the new full name never appears in the text.
ALIAS_STORY = '''"Hello," Militia said.

Amy waved. "Hi."'''
