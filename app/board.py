"""The whiteboard: board state and checks on every LLM reply before it is shown."""
from __future__ import annotations

import re
from dataclasses import dataclass

from sympy import Eq, Or, sstr

from app import examples, grading, mathcheck
from app.config import BOARD_MAX_LINES, MAX_SEGMENTS, SAY_MAX_WORDS
from app.curriculum import LATEX_CHARS, SPOKEN_SYMBOLS, highlight_problem
from app.fallback import in_words, spoken_maths
from app.models import PINNED_IDS, AnswerKey, BoardCommand, BoardLine, Segment

# Numbers in speech, after spoken_maths(): "2", "-1/2", "2 + sqrt(3)", "1 ± sqrt(2)".
NUMBERS = re.compile(r"(?:sqrt\(\d+\)|[-+*/.±0-9 ])+")
# Saying the answer outright: "x =", "x is", "x equals", "x equal to", "x must be", "x has to be" (and "D is" for
# a discriminant, "k is" for find k), "the root is", "the roots are", "the answer is", followed by the numbers
# that come straight after.
ANSWER_PHRASE = re.compile(r"(?:\b[xdk]\s*(?:=|\bis\b|\bequals?(?:\s+to)?\b|\bmust\s+be\b|\bhas\s+to\s+be\b)"
                           r"|\broots?\s+(?:is|are)\b|\banswer\s+is\b)((?:sqrt\(\d+\)|[-+*/.±0-9 ])*)")
STEP_LABEL = re.compile(r"\bstep\s+\w+")       # "Step two": a label, not one of the answer's values
# Rule 7: the replies whose own example may never be one of this concept's questions, as their problems name them.
NO_LESSON_QUESTIONS = {"reteach": "a reteach", "example": "a solved example"}


@dataclass
class ReplyContext:
    action: str
    board: list[BoardLine]
    anchor: str | None              # pending question's equation (SymPy syntax), or None
    key: AnswerKey | None           # pending question's key
    next_key: AnswerKey | None      # for a reteach: the next question's key
    student_answer: str             # values the student already said (never flagged)
    check_equations: list[str]      # every topic's question equations ("never use as examples")
    formulas: dict[str, str]


# ---------- board state ----------
def apply(board: list[BoardLine], segments: list[Segment], formulas: dict[str, str]) -> list[BoardLine]:
    """The board after these segments' commands; the input list is not changed. Highlights change nothing."""
    lines = list(board)
    for segment in segments:
        for cmd in segment.board:
            if cmd.op == "clear":
                lines = []
            elif cmd.op == "write":
                lines = _write(lines, cmd, formulas)
    return lines


def _write(lines: list[BoardLine], cmd: BoardCommand, formulas: dict[str, str]) -> list[BoardLine]:
    """Replace the line with this id in place, or add it at the end and keep at most 8 lines."""
    latex = formulas.get(cmd.formula) if cmd.formula else cmd.latex      # a formula keeps its name and LaTeX
    new = BoardLine(id=cmd.id, latex=latex, formula=cmd.formula, text=cmd.text, check=cmd.check)
    ids = [line.id for line in lines]
    if cmd.id in ids:
        at = ids.index(cmd.id)
        return lines[:at] + [new] + lines[at + 1:]
    lines = lines + [new]
    while len(lines) > BOARD_MAX_LINES:            # drop the oldest line, but never the question's two lines
        oldest = next(i for i, line in enumerate(lines) if line.id not in PINNED_IDS)
        lines = lines[:oldest] + lines[oldest + 1:]
    return lines


def line_ids(commands: list[BoardCommand]) -> list[str]:
    """The ids of the lines these commands write or highlight, in order, each once."""
    ids = [cmd.id if cmd.op == "write" else cmd.target for cmd in commands if cmd.op in ("write", "highlight")]
    return list(dict.fromkeys(ids))


def replayed(segments: list[Segment], on_board: list[BoardLine]) -> list[Segment]:
    """The segments said again: a line still on the board is pointed at (highlighted) while its part is said, a line
    that is gone (cleared, or pushed off) is written again, a highlight of a gone line is dropped, and nothing is
    cleared."""
    present, again = {line.id for line in on_board}, []
    for segment in segments:
        commands = []
        for cmd in segment.board:
            if cmd.op == "write":
                commands.append(BoardCommand(op="highlight", target=cmd.id) if cmd.id in present else cmd)
                present.add(cmd.id)
            elif cmd.op == "highlight" and cmd.target in present:
                commands.append(cmd)
        again.append(Segment(say=segment.say, board=commands))
    return again


# ---------- checking an LLM reply ----------
def validate_reply(segments: list[Segment], ctx: ReplyContext) -> list[str]:
    """The 8 rules an LLM reply must pass: 1 its shape, 2 speech without LaTeX and at most 60 words, 3 every
    maths line passes mathcheck, 4 word lines without digits or "=", 5 highlights of lines on the board (a part
    KaTeX can't mark on its own becomes the whole line), 6 no answer
    given away, 7 no lesson question as the example in a reteach or a solved example (in an explanation, none
    but the open one), 8 no reused line id. Each problem is one readable line; [] means the reply can be shown."""
    problems = []
    if not 1 <= len(segments) <= MAX_SEGMENTS:            # 3 asked for, a 4th accepted
        problems.append(f"a reply needs 1 to {MAX_SEGMENTS} segments, not {len(segments)}")
    latex_of = {line.id: line.latex or "" for line in ctx.board}      # what a highlight can point into
    maths = []
    for number, segment in enumerate(segments, start=1):
        problems += [f"segment {number}: {p}" for p in _speech_problems(segment.say)]
        for cmd in segment.board:
            if cmd.op == "write" and cmd.latex and not cmd.check and cmd.relation != "new" and (
                    ctx.anchor or any("x" in line["check"] for line in maths)):
                cmd.check = _branches(cmd.latex)     # a ± line is proved through its two branches, against the
            problems += _command_problems(cmd, ctx, latex_of)    # line above or the open question
            if cmd.op == "write" and cmd.id:
                latex_of[cmd.id] = ctx.formulas.get(cmd.formula, "") if cmd.formula else (cmd.latex or "")
                if cmd.latex and cmd.check:
                    maths.append(cmd.model_dump())
    problems += mathcheck.problems(maths, anchor=ctx.anchor)          # rule 3, anchored even if not shown
    return problems + _answer_problems(segments, maths, ctx)


def _speech_problems(say: str) -> list[str]:
    """Rule 2: speech has no LaTeX characters, no "=" or minus sign before a number, and at most 60 words."""
    problems = []
    if any(char in say for char in LATEX_CHARS):
        problems.append("say has LaTeX characters; say the maths in words")
    if SPOKEN_SYMBOLS.search(say):
        problems.append("say has '=' or a minus sign before a number; say 'equals' and 'minus' in words")
    if len(say.split()) > SAY_MAX_WORDS:
        problems.append(f"say is over {SAY_MAX_WORDS} words")
    return problems


def _command_problems(cmd: BoardCommand, ctx: ReplyContext, latex_of: dict[str, str]) -> list[str]:
    """Rules 1, 4, 5, 7 and 8 for one board command."""
    if cmd.op == "clear":
        return ["clear isn't allowed; the engine clears the board"]
    if cmd.op == "highlight":
        if cmd.target not in latex_of:
            return [f"highlight target {cmd.target} isn't on the board"]
        if highlight_problem(latex_of[cmd.target], cmd.part):
            cmd.part = None          # a part KaTeX can't mark on its own: the whole line lights up instead
        return []
    line = f"line {cmd.id}"
    if not cmd.id:
        return ["a write needs an id"]
    if cmd.id in latex_of:
        return [f"{line}: that id is already on the board; use a new id"]
    shape = []                                     # rules 1 and 4 together, so the one retry can fix both
    if [cmd.latex, cmd.formula, cmd.text].count(None) != 2:
        shape.append(f"{line}: write exactly one of latex, formula or text; words and maths go on separate lines")
    if cmd.text and "=" not in cmd.text:            # small whole numbers become words, as the prompt asks
        cmd.text = in_words(cmd.text) or cmd.text
    if cmd.text and re.search(r"[\d=]", cmd.text):
        shape.append(f"{line}: the text line {cmd.text!r} has digits or '='; write numbers in a words line as words, "
                     "e.g. 'Step one', and put maths on a checked line")
    if shape:
        return shape
    if cmd.latex and not cmd.check:
        return [f"{line}: maths needs a check, so SymPy can test it"]
    if cmd.formula and cmd.formula not in ctx.formulas:
        return [f"{line}: unknown formula {cmd.formula}"]
    if cmd.relation == "new" and ctx.action in ("hint", "follow_up"):
        return [f"{line}: hints and follow-ups can't use relation new; stay with the question"]
    if ctx.action in NO_LESSON_QUESTIONS and cmd.check and examples.is_a_question(cmd.check, ctx.check_equations):
        return [f"{line}: {NO_LESSON_QUESTIONS[ctx.action]} can't use a lesson question as its example"]
    if ctx.action == "explain" and cmd.check:      # the open question's own equation is allowed
        others = [equation for equation in ctx.check_equations if equation != ctx.anchor]
        if examples.is_a_question(cmd.check, others):
            return [f"{line}: an explanation can't use another lesson question as its example"]
    return []


def _branches(latex: str) -> str | None:
    """A ± line's check, its two branches: "Or(Eq(x, ...), Eq(x, ...))"; None when it isn't x = ... ± ... with
    one ±. (The prompt still says never to write ±: this is a safety net that still proves the line.)"""
    if latex.count("\\pm") != 1:
        return None
    shown = mathcheck.parse_display(latex)
    if not (isinstance(shown, Or) and all(isinstance(p, Eq) and p.lhs == mathcheck.x for p in shown.args)):
        return None
    return "Or(" + ", ".join(f"Eq(x, {sstr(part.rhs)})" for part in shown.args) + ")"


def _answer_problems(segments: list[Segment], maths: list[dict], ctx: ReplyContext) -> list[str]:
    """Rule 6; and a needs-review worked answer on a roots (or find_k) question must end on the key."""
    if ctx.action == "needs_review":                # showing the answer is the point here
        key = ctx.key
        if key and key.kind in ("roots", "find_k") and key.values:
            letters = (mathcheck.k,) if key.kind == "find_k" else (mathcheck.x,)    # "x = 4" is no value of k
            last = _claimed(maths[-1]["check"], letters) if maths else None
            if last is None or not _same_set(last, key.values):
                return ["a worked answer must end with a root claim equal to the answer"]
        return []
    if ctx.action == "reteach":                     # the next question's whole answer set
        reveal = reveals_answer(segments, ctx.next_key, "")
    else:
        reveal = reveals_answer(segments, ctx.key, ctx.student_answer)
    return [reveal] if reveal else []


# ---------- does a reply give the answer away? (rule 6) ----------
def reveals_answer(segments: list[Segment], key: AnswerKey | None, student_answer: str) -> str | None:
    """Why this reply gives the answer away, or None. Word answers (yes/no, nature) can't be checked."""
    if key is None or not key.values:
        return None
    said = (grading.parse_answer(student_answer, key.kind) if student_answer else None) or []
    secret = [value for value in key.values if not _has(said, value)]     # what the student hasn't said
    if not secret:
        return None
    words = [cmd.text for segment in segments for cmd in segment.board if cmd.op == "write" and cmd.text]
    text = STEP_LABEL.sub(" ", " ".join([segment.say for segment in segments] + words).lower())   # board words too
    speech = spoken_maths(text)
    for found in ANSWER_PHRASE.finditer(speech):
        if any(_has(secret, value) for value in _values(found.group(1))):
            return f"gives away the answer: {found.group(0).strip(' .')!r}"
    spoken = _values(speech)
    if len(secret) >= 2 and all(_has(spoken, value) for value in secret):
        return "gives away the answer: says every value of it"
    for segment in segments:
        for cmd in segment.board:
            claimed = _stated(cmd.check) if cmd.op == "write" else None
            if claimed and _same_set([v for v in claimed if not _has(said, v)], secret):
                return f"gives away the answer: line {cmd.id} claims it"
    return None


def _values(text: str) -> list:
    """Every number in a piece of speech, by value: " 2 and -1/2" -> [2, -1/2]."""
    return [value for run in NUMBERS.findall(text) for value in grading.parse_answer(run, "roots") or []]


def _claimed(check: str | None, letters: tuple = (mathcheck.x, mathcheck.k)) -> list | None:
    """The values of a root claim line ("Or(Eq(x, 2), Eq(x, 3))" -> [2, 3]) or of k ("Eq(k, 4)" -> [4]), in one of
    these letters; None for any other line."""
    try:
        expr = mathcheck.parse_check(check or "")
    except mathcheck.CheckError:
        return None
    parts = expr.args if isinstance(expr, Or) else (expr,)
    if all(isinstance(p, Eq) and p.lhs in letters and p.rhs.is_number for p in parts):
        return [p.rhs for p in parts]
    return None


def _stated(check: str | None) -> list | None:
    """The roots a line states outright: a root claim, or each bracket set to zero ("x - 2 = 0 or x - 3 = 0")."""
    try:
        expr = mathcheck.parse_check(check or "")
        return _claimed(check) or (list(mathcheck.solutions(expr)) if isinstance(expr, Or) else None)
    except mathcheck.CheckError:
        return None


def _has(values: list, value) -> bool:
    return any(grading.same_value(value, other) for other in values)


def _same_set(a: list, b: list) -> bool:
    """Equal as sets of numbers, compared by value."""
    return all(_has(b, value) for value in a) and all(_has(a, value) for value in b)
