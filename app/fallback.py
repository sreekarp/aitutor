"""Scripted mode's reading of a reply: keyword lists, spoken maths and scripted understanding. (What the
tutor says back is in replies.py.)

Used when there is no API key, in tests, and whenever the LLM fails. All matching is by whole words,
so the "d" in "and" is never the letter D.
"""
from __future__ import annotations

import re

from app import grading
from app.config import MAX_INPUT_CHARS
from app.models import WORD_KINDS, Check, Understood


def phrases(*phrases: str) -> re.Pattern:
    """A regex that finds any of the phrases as whole words, longest first ("self harm" also finds "self-harm")."""
    words = sorted((r"\W+".join(map(re.escape, p.split())) for p in phrases), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(words) + r")\b")


def _whole_reply(*phrases: str) -> re.Pattern:
    """A regex for a reply made only of these phrases, like "ok next" or "haan theek hai"."""
    one = "|".join(sorted((r"\s+".join(map(re.escape, p.split())) for p in phrases), key=len, reverse=True))
    return re.compile(rf"(?:{one})(?:\s+(?:{one}))*")


# ---------- word lists ----------
NEXT = _whole_reply("ok", "okay", "next", "go on", "carry on", "continue", "yes", "haan", "han", "haan ji",
                    "theek hai", "thik hai", "accha", "yeah", "yep", "yup", "sure", "yes please", "go ahead", "alright",
                    "teach me", "teach me please", "yes quiz me")       # the last three: the choice buttons, or close
SAY_AGAIN = _whole_reply("say again", "say that again", "say it again", "can you say that again", "repeat",
                         "repeat please", "please repeat", "repeat that", "repeat it", "can you repeat that",
                         "could you repeat that", "what did you say", "phir se", "phir se bolo", "dobara", "once more",
                         "pardon", "sorry", "what", "come again")
# Hit, beat, slap, kick, punch, smack, thrash, hurt or touch, in any form, then "me" or "us" ("beats me", "slapped
# us"), or the student saying it the other way round (I_WAS_HIT). Someone may be hurting the student and the safety
# check missed it ("He gets drunk. Beats me. I don't know"), so such a reply is unclear: the question is asked
# again, and it never gets a maths hint.
HURT_ME = re.compile(r"\b(?:hit|beat|slap|kick|punch|smack|thrash|hurt|touch)\w*\s+(?:me|us)\b")
# The student saying it the other way round: I was, I am, I'm, I get, I got, I keep, I have been, I've been or I
# been, perhaps then being or getting, then hit, beaten, beat up, slapped, kicked, punched, smacked, thrashed, hurt,
# touched or abused ("I was beaten", "I'm getting hit at home", "I keep getting beaten"; "I'm" and "I've" arrive as
# im and ive). Not when the maths did it ("I got beaten by this question", "hit with a tough sum"), and "I'm beat"
# means tired. safety.py uses it too.
BY_THE_MATHS = (r" (?:by|with)(?: (?:this|that|the|these|those|a|an|such a|so many))?"
                r"(?: (?:hard|tough|tricky|difficult))? (?:question|sum|problem|equation|quadratic)s?\b")
I_WAS_HIT = re.compile(r"\b(?:i (?:was|am|get|got|keep|have been|been)|im|ive been)(?: being| getting)? (?:hit"
                       r"|beaten|beat up|slapped|kicked|punched|smacked|thrashed|hurt|touched|abused)\b"
                       rf"(?!{BY_THE_MATHS})")
DONT_KNOW = phrases("dont know", "no idea", "just tell me", "hint", "help", "dont get it", "didnt get it",
                    "dont understand", "didnt understand", "not understood", "havent understood", "pata nahi",
                    "nahi pata", "samajh nahi")
# Asking for the steps of the open question is asking for help: the next hint. "step by step" asks unless the
# student says they did it or will do it themselves (MY_OWN_STEPS: "I did it step by step", "let me go step by
# step"). Checked after answers and not while "how did you get that?" waits, so answers and working still count.
# Not "explain the solution": it may mean the question just solved.
HOW_TO = phrases("show me how", "how do i solve", "how to solve")
STEP_BY_STEP = re.compile(r"\bstep\W+by\W+step\b")                     # "step-by-step" too
# "I did it", "I solved it", "I've done it", "let me go", "I'll take it", "I'm doing it" (but not "I did not").
MY_OWN_STEPS = re.compile(r"\b(?:i (?:did|solved|worked)(?! not)|ive (?:done|solved|worked)|(?:let me|ill|i will)"
                          r" (?:go|do|take|solve|work|show)|(?:im|i am) (?:going|doing|solving|working))\b")
GET_ITS = ("get it", "got it", "understood", "samajh gaya", "samajh gayi")
GET_IT = phrases(*GET_ITS)
# "i get it" and "makes sense" count only as a whole reply: inside a longer one, "nothing makes sense" or "that makes
# sense, why is it zero?" would wrongly be read as got it. "Makes sense" may have a yes before it, an "it" or "that"
# and a "now" after it ("yeah that makes sense now"); a "no" or "nothing" before it makes it a different reply.
MAKES_SENSE = re.compile(r"(?:(?:yes|yeah|yep|ok|okay|haan)\s+)?(?:(?:that|it|this|all of this)\s+)?makes sense"
                         r"(?:\s+now)?")
GET_IT_WHOLE = phrases(*GET_ITS, "i get it")
# What the student wants next: skip to the questions, or stop this topic. Read as a whole reply (keyword) and in
# longer replies (understand).
TEST_ME = phrases("test me", "test me first", "quiz me", "skip to the questions")
# "i know this" counts only as a whole reply: inside a longer one ("I know this is hard") it isn't a request.
TEST_ME_WHOLE = phrases("test me", "test me first", "quiz me", "skip to the questions", "i already know this",
                        "i already know it", "i know this")
MOVE_ON = phrases("move on", "no thanks", "stop here", "next topic")
# Not following what was just explained. As a whole reply, "no" and "nahi" count only while teaching (NOT_SURE):
# while a question is open they are answers (NOT_SURE_ASKED). In a longer reply UNSURE is read after answers, as
# "not sure, maybe 2 and 3" is an answer; there "didn't get it" and "don't understand" are DONT_KNOW, read first.
NOT_SURE_WORDS = ("not sure", "im not sure", "didnt get it", "dont understand", "another example", "explain again",
                  "confused")
NOT_SURE = _whole_reply(*NOT_SURE_WORDS, "no", "nahi", "nope", "not really")
NOT_SURE_ASKED = _whole_reply(*NOT_SURE_WORDS)
UNSURE = phrases(*NOT_SURE_WORDS)
QUESTION_START = re.compile(r"\s*(?:why|how|what|kyun|kaise)\b")
# Method words, by their start: "factor" also finds "factorise" and "factorised".
METHOD = re.compile(r"\b(?:d|formula|discriminant|(?:factor|split|bracket|substitut|put|multipl|expand|simplif"
                    r"|cancel)\w*)\b")
BECAUSE = phrases("because", "since")
MARKER = re.compile(r"\b(?:because|since|so|as)\b|=")    # the answer is read from the text before these
X_OR_D = re.compile(r"(?<![a-z])[xd](?![a-z])")           # a part that mentions x or D is working, not a value
MATHS = re.compile(r"sqrt|[-+*/().±0-9 ]")
NUMBER_WORDS = {word: str(n) for n, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())} | {"half": "1/2"}
NUMBER_WORD = re.compile(r"\b(?:" + "|".join(NUMBER_WORDS) + r")\b")
# The answer words each kind accepts, from grading's lists: "haan" -> "yes", "no solution" -> "none".
WORD_CHOICES = {"is_quadratic": grading.YES_NO_WORDS, "is_root": grading.YES_NO_WORDS,
                "has_real_roots": grading.YES_NO_WORDS, "nature": grading.NATURE_WORDS,
                "roots": {"none": grading.NONE_PHRASES}}


# A topic asked for by name: a move verb, maybe "on" or "back", then "to" ("can we skip to", "move on to", "take me
# back to"). Only words like "topic" or "please" may follow the topic's name, so working such as "move to standard
# form, then factorise" isn't a request; nor is a reply longer than TOPIC_REQUEST_WORDS words.
# The request must start the reply: only polite lead-ins ("can we", "ok lets", "i want to") may come before the verb,
# so worries or working before "go to" ("I am scared of my dad, skip to roots") are left for UNDERSTAND.
LEAD_IN = r"(?:(?:ok|okay|so|please|now|can|could|would|we|i|you|lets|let|s|us|want|wanna|like|id|d|just|to) )*"
MOVE_TO = re.compile(LEAD_IN + r"(?:skip|go|move|jump|switch|change|take me)(?: (?:on|back|over|ahead|straight))? to ")
TOPIC_TAIL = r"(?: (?:topic|chapter|lesson|section|part|please|now|instead|again))*"
TOPIC_REQUEST_WORDS = 12


# ---------- fast paths ----------
def plain(text: str) -> str:
    """Lower case without apostrophes, every other mark a single space: "Sorry, what's that?" -> "sorry whats that"."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower().replace("'", "").replace("’", "")).split())


def keyword(text: str, phase: str) -> str | None:
    """A reply type when the WHOLE reply is listed phrases ("ok next", "got it", "not sure"); anything longer goes
    to UNDERSTAND. "ok", "yes", "no" and "nahi" count only while teaching: while a question is open they're answers."""
    clean = plain(text)
    if SAY_AGAIN.fullmatch(clean):
        return "say_again"
    if phase == "teaching":
        lists = [(NEXT, "next"), (GET_IT_WHOLE, "claims_understanding"), (MAKES_SENSE, "claims_understanding"),
                 (NOT_SURE, "not_sure")]
    else:
        lists = [(NOT_SURE_ASKED, "not_sure")]
    for pattern, kind in lists + [(MOVE_ON, "move_on"), (TEST_ME_WHOLE, "test_me")]:
        if pattern.fullmatch(clean):
            return kind
    return None


def topic_request(text: str, aliases: dict[str, list[str]]) -> str | None:
    """The topic a short reply asks to go to, named by one of its aliases (concept id -> aliases, from the lesson
    files): "Can we skip to the factorization topic?" -> "factorisation". None when it names no topic."""
    clean = " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())
    found = MOVE_TO.match(clean)
    if not found or len(clean.split()) > TOPIC_REQUEST_WORDS:
        return None
    rest = clean[found.end():]                                # e.g. "the factorization topic"
    for concept_id, names in aliases.items():
        if any(re.fullmatch(rf"(?:the )?{re.escape(name)}{TOPIC_TAIL}", rest) for name in names):
            return concept_id
    return None


# ---------- scripted understanding ----------
def spoken_maths(text: str) -> str:
    """Lower-cases text and writes spoken maths as symbols: "minus half" -> "-1/2", "root 3" -> "sqrt(3)"."""
    text = text.lower().replace("'", "").replace("’", "")
    for symbol, plain in grading.SYMBOLS.items():             # unicode minus, ×, the spellings of ±
        text = text.replace(symbol, plain)
    text = NUMBER_WORD.sub(lambda m: NUMBER_WORDS[m.group(0)], text)
    text = re.sub(r"\bplus\s+(?:or\s+)?minus\b", "±", text)
    text = re.sub(r"\b(?:minus|negative)\b\s*", "-", text)
    text = re.sub(r"\bplus\b", "+", text)
    text = re.sub(r"(\d)\s+(?:by|over)\s+(-?\d)", r"\1/\2", text)              # "1 by 3" -> "1/3"
    text = re.sub(r"\b(?:square\s+)?root\s+(?:of\s+)?(?=\d)|√", "sqrt", text)
    text = re.sub(r"sqrt\s*(\d+)", r"sqrt(\1)", text)
    return re.sub(r"(\d)\s*sqrt", r"\1*sqrt", text)                             # "2 sqrt(3)" -> "2*sqrt(3)"


def understand(text: str, *, phase: str, check: Check | None, awaiting_working: bool) -> Understood:
    """Scripted understanding: the first list that matches gives the reply's type."""
    clean = spoken_maths(text[:MAX_INPUT_CHARS])
    working = _showed_working(_drop_names(clean, check))
    answer = _answer(clean, check) if phase == "checking" and check else ""
    asks = "?" in clean or bool(QUESTION_START.match(clean))
    if HURT_ME.search(clean) or I_WAS_HIT.search(clean):
        kind = "unclear"                                     # maybe a disclosure: asked again, never a hint
    elif DONT_KNOW.search(clean):
        kind = "dont_know"
    elif answer:
        kind = "answer"
    elif TEST_ME.search(clean):
        kind = "test_me"                                     # "I already know this, can you test me?"
    elif UNSURE.search(clean):
        kind = "not_sure"
    elif MOVE_ON.search(clean):
        kind = "move_on"
    elif GET_IT.search(clean) and not asks:
        kind = "claims_understanding"                        # "how did you get it?" is a question, not a claim
    elif _asks_for_steps(clean) and not awaiting_working:
        kind = "dont_know"                                   # "explain it step by step": the next hint
    elif asks:
        kind = "question"
    elif awaiting_working and working:
        kind = "answer"                                      # working only, e.g. "I factorised it"
    else:
        kind = "unclear"
    # No safety flag here: the tutor runs safety.is_safety before any understanding.
    return Understood(type=kind, answer=answer if kind == "answer" else "", showed_working=working)


def _drop_names(text: str, check: Check | None) -> str:
    """Remove "x =", "x is", "x equals" (and "D =" on D questions, "b =" when b is asked): they aren't working."""
    names = "x"
    if check and check.kind == "discriminant":
        names += "d"
    if check and check.kind == "coefficient" and check.which:
        names += check.which
    return re.sub(rf"(?<![a-z0-9])[{names}]\s*(?:=|equals\b|is\b)\s*", " ", text)


def _showed_working(text: str) -> bool:
    """A method word, "because" / "since", or an "=" that's left (so and as don't count)."""
    return bool(METHOD.search(text) or BECAUSE.search(text) or "=" in text)


def _asks_for_steps(text: str) -> bool:
    """"show me how", "how do I solve this?", or "step by step" unless the student did it or will do it themselves."""
    return bool(HOW_TO.search(text) or (STEP_BY_STEP.search(text) and not MY_OWN_STEPS.search(text)))


def _answer(text: str, check: Check) -> str:
    """The answer as UNDERSTAND writes it ("2, 3", "-1/3", "yes", "none"), or "" when no value is found."""
    text = MARKER.split(_drop_names(text, check), maxsplit=1)[0]
    if check.kind in WORD_KINDS:
        return _answer_word(text, check.kind)
    return _numbers(text) or _answer_word(text, check.kind)


def _answer_word(text: str, kind: str) -> str:
    """The first answer word this kind of question accepts: "haan" -> "yes", "no solution" -> "none"."""
    spellings = {spelling: word for word, group in WORD_CHOICES.get(kind, {}).items() for spelling in group}
    found = phrases(*spellings).search(text) if spellings else None
    return spellings[" ".join(re.findall(r"[a-z]+", found.group(0)))] if found else ""


def _numbers(text: str) -> str:
    """The value in each comma / and / or part, e.g. "-2, 3". Numbers next to x or D ("-2x") are working."""
    values = []
    for part in grading.SEPARATORS.split(text):
        value = " ".join("".join(MATHS.findall(part)).split()).rstrip(".")
        if re.search(r"[0-9]", value) and not X_OR_D.search(part):
            values.append(value)
    return ", ".join(values)
