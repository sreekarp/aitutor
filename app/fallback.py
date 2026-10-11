"""Scripted mode: keyword lists, scripted understanding and every scripted reply.

Used when there is no API key, in tests, and whenever the LLM fails. All matching is by whole words,
so the "d" in "and" is never the letter D.
"""
from __future__ import annotations

import re

from sympy import latex

from app import grading
from app.config import MAX_INPUT_CHARS
from app.curriculum import get_step
from app.models import (WORD_KINDS, BoardCommand, Check, Concept, Curriculum, Decision, Grade, Progress,
                        Segment, Step, Understood)


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
DONT_KNOW = phrases("dont know", "no idea", "just tell me", "hint", "help", "dont get it", "didnt get it",
                    "dont understand", "didnt understand", "not understood", "havent understood", "pata nahi",
                    "nahi pata", "samajh nahi", "beats me")
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

# ---------- scripted texts ----------
SAFETY_REPLY = ("Thank you for telling me. If something is worrying you, please talk to a parent, a teacher or "
                "another adult you trust. And please don't share personal details like your phone number or "
                "address here.")
FIXED = {"start_check": "Let's see if you've got it.", "show_me": "Great, then show me.",
         "safety": SAFETY_REPLY, "limit": "That's all for today. Let's continue tomorrow!",
         "lesson_changed": "The lesson has been updated, so here's a new question."}
PRAISE = ("Right", "Well done", "That's right", "Nice work")
YES_NO_KINDS = ("is_quadratic", "is_root", "has_real_roots")
CLARIFY = {"roots": "Say just the numbers, or type them.", "coefficient": "Say just the number.",
           "discriminant": "Say just the number.",
           "nature": "Two distinct real roots, two equal real roots, or no real roots?"}
FOLLOW_UP = {"missing": "That's part of it, but a root is missing. Have another look.",
             "extra": "Nearly. One of those values doesn't fit the question. Think about which ones make sense."}
REASK = {"roots": "So, what are the roots?", "discriminant": "So, what is the discriminant?",
         "nature": "So, what is the nature of the roots?", "is_quadratic": "So, is it a quadratic equation?",
         "is_root": "So, is it a root?", "has_real_roots": "So, is it possible?"}
REVIEW_LATER = "This one needs more practice, so we'll come back to this next time."   # R9, after the worked answer


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


# ---------- scripted replies ----------
def words(decision: Decision, *, cur: Curriculum, progress: Progress, check: Check | None,
          grade: Grade | None, first_name: str) -> list[Segment]:
    """Scripted words for a decision's action. Compose adds any step, question or re-ask afterwards."""
    concept = cur.concepts.get(decision.concept_id or "")
    action = decision.action
    if first_name.islower() or first_name.isupper():         # typed "sreekar" or "SREEKAR": say "Sreekar"
        first_name = first_name.title()
    if action in FIXED:
        return _say(FIXED[action])
    if action == "next_step":
        return []                                            # the step's own segments do the talking
    if action in ("greet", "resume"):
        return _say(_greeting(decision, cur, progress, first_name))
    if action in ("credit", "mastered"):
        return _say(_praise(action, check, concept))
    if action == "probe":
        return _say("Right — how do you know?" if check.kind in YES_NO_KINDS else "Right — how did you get that?")
    if action == "repeat":
        return _say("Let me ask that again." if check else "Let me say that again.")
    if action == "clarify":
        return _say(CLARIFY.get(check.kind, "Just say yes or no."))
    if action == "hint":
        return _say(f"Here's a hint. {check.hints[decision.hint_level - 1]}")
    if action == "follow_up":
        return _say(FOLLOW_UP.get(grade.detail if grade else "", "Nearly. Have another look at your answer."))
    if action == "explain":                                  # checking: compose re-asks the question
        steps = [] if check else _segments(get_step(concept, progress[concept.id].step_id))
        return _say("Good question — let's go over it again.") + steps
    if action == "reteach":
        fix = next((m.fix for m in concept.mistakes if m.id == decision.mistake), "")
        lead = _say(f"Let's look at it another way. {fix}".strip())
        return lead + _segments(get_step(concept, concept.reteach_step))
    if action == "needs_review":
        return [Segment(say=f"{check.hints[-1]} Here is the answer.", board=[answer_line(check)]),
                Segment(say=REVIEW_LATER)]
    if action == "end":
        return _summary(cur, progress, first_name)
    raise ValueError(f"no scripted words for action {action!r}")


def reask(check: Check, awaiting_working: bool) -> Segment:
    """The short re-ask after a hint, follow-up or explanation."""
    if awaiting_working:
        return Segment(say="So, how did you get your answer?")
    if check.kind == "coefficient":
        return Segment(say=f"So, what is {check.which}?")
    if check.kind == "roots" and not check.show_equation:
        return Segment(say="So, what is your answer?")       # word problems ask for one quantity
    return Segment(say=REASK[check.kind])


def opening(concept: Concept, action: str, from_top: bool) -> list[Segment]:
    """Said as a concept is picked up, before its step or question. From step 1: its intro (not on Continue).
    Right after R1 or R9 ended another concept: first that we move on to it, or go back to it if already started."""
    title, moved_on = _lower_first(concept.title), action in ("mastered", "needs_review")
    if not from_top:
        return _say(f"Now let's get back to {title}, where we left off.") if moved_on else []
    if action == "resume":
        return []                                            # Continue (S1) only replays the step
    return _say(f"Now let's move on to {title}. {concept.intro}" if moved_on else concept.intro)


def answer_line(check: Check) -> BoardCommand:
    """Scripted R9's board line: a checked root claim built from SymPy's key, or a text line in words."""
    key = grading.answer_key(check)
    if check.kind != "roots" or not key.values:
        return BoardCommand(op="write", id="answer", text=grading.key_text(check))
    shown = r" \text{ or } ".join(f"x = {latex(value)}" for value in key.values)
    claims = [f"Eq(x, {value})" for value in key.values]
    checked = claims[0] if len(claims) == 1 else f"Or({', '.join(claims)})"
    return BoardCommand(op="write", id="answer", latex=shown, check=checked,
                        relation="select" if check.keep else "equiv")


def _say(text: str) -> list[Segment]:
    return [Segment(say=text)]


def _segments(step: Step | None) -> list[Segment]:
    return list(step.segments) if step else []


def _lower_first(text: str) -> str:
    """For the middle of a sentence: "Solving by factorisation" -> "solving by factorisation"."""
    return text[:1].lower() + text[1:]


def _praise(action: str, check: Check, concept: Concept) -> str:
    """Praise that rotates by check id; word answers are restated in NCERT words ("Right — no real roots.")."""
    praise = PRAISE[sum(map(ord, check.id)) % len(PRAISE)]
    text = f"{praise} — {_lower_first(grading.key_text(check))}." if check.kind in WORD_KINDS else f"{praise}!"
    if action == "mastered":
        text += f" You've mastered {_lower_first(concept.title)}."
    return text


def _greeting(decision: Decision, cur: Curriculum, progress: Progress, first_name: str) -> str:
    """S0 and S1: start the concept, go over it again, or carry on where we left off. S0 says the name."""
    cid = decision.resume_concept_id
    if cid is None:
        return "Welcome back!"                               # nothing left: compose adds the summary, with the name
    state, title = progress[cid], _lower_first(cur.concepts[cid].title)
    if decision.action == "resume":                          # S1: no name, so its audio is cached for everyone
        return f"Welcome back! Let's carry on with {title} from where we left off."
    if state.pending or state.step_id != cur.concepts[cid].steps[0].id:      # S0 part-way through
        return f"Welcome back, {first_name}! Let's carry on with {title} from where we left off."
    if state.status == "teaching" and state.last_check_id:   # S0 reset it from needs review (it keeps last_check_id)
        return f"Hi {first_name}! Last time, {title} needed more practice, so let's go over it again."
    return f"Hi {first_name}! Let's start today with {title}."


def _summary(cur: Curriculum, progress: Progress, first_name: str) -> list[Segment]:
    """END: what's mastered, and what needs review next session."""
    done = [_lower_first(cur.concepts[c].title) for c in cur.order if progress[c].status == "mastered"]
    review = [_lower_first(cur.concepts[c].title) for c in cur.order if progress[c].status == "needs_review"]
    first = f"That's everything for now, {first_name}!" + (f" You've mastered {_join(done)}." if done else "")
    rest = [Segment(say=f"Next session we'll come back to {_join(review)}.")] if review else []
    return [Segment(say=first)] + rest


def _join(items: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
