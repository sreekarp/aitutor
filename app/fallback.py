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
NEXT = _whole_reply("ok", "okay", "next", "go on", "continue", "yes", "haan", "han", "haan ji", "theek hai",
                    "thik hai", "accha")
SAY_AGAIN = _whole_reply("say again", "repeat", "repeat please", "phir se", "phir se bolo", "dobara", "once more")
# Not "beats me": a disclosure the safety check misses ("He gets drunk. Beats me.") must never get a hint.
DONT_KNOW = phrases("dont know", "no idea", "just tell me", "hint", "help", "dont get it", "didnt get it",
                    "dont understand", "didnt understand", "not understood", "havent understood", "pata nahi",
                    "nahi pata", "samajh nahi")
GET_IT = phrases("get it", "got it", "understood", "samajh gaya", "samajh gayi")
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


# ---------- fast paths ----------
def keyword(text: str, phase: str) -> str | None:
    """"next" or "say_again" when the WHOLE reply is listed phrases; anything longer goes to UNDERSTAND."""
    clean = " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())
    if SAY_AGAIN.fullmatch(clean):
        return "say_again"
    if phase == "teaching" and NEXT.fullmatch(clean):
        return "next"
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
    if DONT_KNOW.search(clean):
        kind = "dont_know"
    elif answer:
        kind = "answer"
    elif GET_IT.search(clean):
        kind = "claims_understanding"
    elif "?" in clean or QUESTION_START.match(clean):
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
