"""Loads the lesson files and checks every one of them at startup.

If anything is wrong the app refuses to start, so a slip in a lesson file never reaches a student.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError
from sympy import Poly, expand

from app import grading, mathcheck
from app.config import CURRICULUM_DIR, MAX_HINTS, SAY_MAX_WORDS
from app.models import PINNED_IDS, WORD_KINDS, BoardCommand, Check, Concept, Curriculum, Segment, Step

LATEX_CHARS = "\\^{}$_"               # never allowed in spoken text
# Nor "=" or a minus sign before a number, a root or a letter: a voice reads "x = -2" and "x-2" as "x equals 2"
# and "x 2". Any dash counts ("–2" too), except inside a word ("self-check", "two-step"). Say "equals", "minus".
SPOKEN_SYMBOLS = re.compile(r"=|(?<![A-Za-z]{2})[-‐‑‒–−﹣－]\s*(?:[\d.√½¼¾]|root\b|sqrt\b|[a-z]\b)")
COMMAND = re.compile(r"\\[A-Za-z]+")  # a LaTeX command name, e.g. \frac
ALIAS = re.compile(r"[a-z0-9]+(?: [a-z0-9]+)*")   # a topic's alias: lower-case words, as a reply is read
LABEL_ID, EQUATION_ID = PINNED_IDS    # "q-label" and "q-eq": the question's two pinned board lines


class CurriculumError(Exception):
    """The lesson files have problems. The message lists every one, one per line."""


# ---------- loading ----------

def load(directory: Path = CURRICULUM_DIR) -> Curriculum:
    """Read index.yaml and every lesson it lists, then validate everything. Raises CurriculumError."""
    try:
        mathcheck.self_test()
    except RuntimeError as err:
        raise CurriculumError(f"the maths checker failed its self-test: {err}") from None
    index = _read_yaml(directory / "index.yaml")
    concepts, problems = {}, []
    for concept_id in index.get("order", []):
        try:
            data = _read_yaml(directory / f"{concept_id}.yaml")
            concepts[concept_id] = _build(Concept, data, concept_id)
        except CurriculumError as err:
            problems.append(str(err))
    if problems:
        raise CurriculumError("\n".join(problems))
    cur = _build(Curriculum, {**index, "concepts": concepts}, "index.yaml")
    problems = validate(cur)
    if problems:
        raise CurriculumError("\n".join(problems))
    return cur


def _read_yaml(path: Path) -> dict:
    """One YAML file as a dict. A missing file or broken YAML becomes a CurriculumError."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise CurriculumError(f"{path.name}: {err}") from None
    if not isinstance(data, dict):
        raise CurriculumError(f"{path.name}: expected keys and values at the top level")
    return data


def _build(model, data: dict, where: str):
    """Turn YAML data into a pydantic model; its field errors become one readable CurriculumError."""
    try:
        return model.model_validate(data)
    except ValidationError as err:
        fields = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors())
        raise CurriculumError(f"{where}: {fields}") from None


# ---------- validation at startup ----------

def validate(cur: Curriculum) -> list[str]:
    """Every problem in the curriculum, one readable line each. An empty list means all is well."""
    problems = []
    if len(set(cur.order)) != len(cur.order):
        problems.append("index.yaml: a concept is listed twice in order")
    for concept_id in cur.order:
        concept = cur.concepts.get(concept_id)
        if concept is None:
            problems.append(f"{concept_id}: listed in order, but there is no lesson for it")
            continue
        if concept.id != concept_id:
            problems.append(f"{concept_id}: the file's id is {concept.id}")
        problems += [f"{concept_id}: {p}" for p in _concept_problems(concept, cur.formulas)]
    aliases = [alias for concept in cur.concepts.values() for alias in concept.aliases]
    problems += [f"alias {alias!r} is used twice" for alias in sorted(set(aliases)) if aliases.count(alias) > 1]
    return problems


def _concept_problems(concept: Concept, formulas: dict[str, str]) -> list[str]:
    """The startup checks for one lesson file."""
    problems = []
    for kind, ids in (("step", [s.id for s in concept.steps]), ("check", [c.id for c in concept.checks]),
                      ("mistake", [m.id for m in concept.mistakes])):
        problems += [f"{kind} id {i} is used twice" for i in sorted(set(ids)) if ids.count(i) > 1]
    if not concept.steps or not concept.checks:
        problems.append("needs at least one step and one check")
    if get_step(concept, concept.reteach_step) is None:
        problems.append(f"reteach_step {concept.reteach_step} is not one of the steps")
    if "worked_example" not in concept.strategies:
        problems.append("strategies must include worked_example (the one that always fits)")
    for step in concept.steps:
        problems += [f"step {step.id}: {p}" for p in _step_problems(step, formulas)]
    mistake_ids = {m.id for m in concept.mistakes}
    for check in concept.checks:
        problems += [f"check {check.id}: {p}" for p in _check_problems(check, mistake_ids)]
    for mistake in concept.mistakes:
        problems += [f"mistake {mistake.id}: {p}" for p in _speech_problems(mistake.fix)]
    problems += [f"alias {alias!r} must be lower-case words, like 'nature of roots'"
                 for alias in concept.aliases if not ALIAS.fullmatch(alias)]
    spoken = (("intro", concept.intro), ("title", concept.title))       # the title is said in greetings and moves
    return problems + [f"{name}: {p}" for name, text in spoken for p in _speech_problems(text)]


def _step_problems(step: Step, formulas: dict[str, str]) -> list[str]:
    """Speech, line shapes, highlights, and a strict SymPy check of every maths line in one step."""
    problems, latex_of, maths = [], {}, []
    for segment in step.segments:
        problems += _speech_problems(segment.say)
        for cmd in segment.board:
            if cmd.op == "highlight":
                problems += _highlight_problems(cmd, latex_of)
            elif cmd.op == "write":
                problems += _write_problems(cmd, formulas)
                # what a highlight can point into: a formula's LaTeX, the line's LaTeX, or "" for text
                latex_of[cmd.id] = formulas.get(cmd.formula, "") if cmd.formula else (cmd.latex or "")
                if cmd.latex and cmd.check:
                    maths.append(cmd.model_dump())
    return problems + mathcheck.problems(maths)   # display included: unreadable maths fails


def _write_problems(cmd: BoardCommand, formulas: dict[str, str]) -> list[str]:
    """A lesson write needs an id and exactly one of: latex (with its check), formula or text."""
    if not cmd.id:
        return ["a write needs an id"]
    if [cmd.latex, cmd.formula, cmd.text].count(None) != 2:
        return [f"line {cmd.id}: write exactly one of latex, formula or text"]
    if cmd.latex and not cmd.check:
        return [f"line {cmd.id}: maths needs a check, so SymPy can test it"]
    if cmd.formula and cmd.formula not in formulas:
        return [f"line {cmd.id}: unknown formula {cmd.formula}"]
    return []


def _highlight_problems(cmd: BoardCommand, latex_of: dict[str, str]) -> list[str]:
    """A lesson highlight points at a line written earlier in its step, and passes reply rule 5."""
    if cmd.target not in latex_of:
        return [f"highlight target {cmd.target} isn't written earlier in this step"]
    problem = highlight_problem(latex_of[cmd.target], cmd.part)
    return [f"highlight on {cmd.target}: {problem}"] if problem else []


def highlight_problem(latex: str, part: str | None) -> str | None:
    """Reply rule 5 (board.py): why `part` can't be highlighted inside `latex`, or None when it can.

    No part means the whole line, which is always fine."""
    if not part:
        return None
    if latex.count(part) != 1:
        return f"part {part!r} must appear exactly once in the line"
    start = latex.index(part)
    end = start + len(part)
    if part[0] in "^_\\" or part[-1] in "^_\\" or latex[start - 1:start] in ("^", "_"):
        return f"part {part!r} would split a power, a subscript or a command"
    if any(c.start() < start < c.end() or c.start() < end < c.end() for c in COMMAND.finditer(latex)):
        return f"part {part!r} cuts into a command name"
    depth = 0
    for char in part:
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth < 0:
            break
    if depth != 0 or part.count("\\left") != part.count("\\right"):
        return f"part {part!r} has unbalanced braces or \\left / \\right"
    return None


def _check_problems(check: Check, mistake_ids: set[str]) -> list[str]:
    """Speech, hints, traps and the SymPy answer key of one question."""
    problems = [p for text in (check.say, *check.hints) for p in _speech_problems(text)]
    if len(check.hints) != MAX_HINTS:
        problems.append(f"needs exactly {MAX_HINTS} hints, has {len(check.hints)}")
    problems += [f"trap names unknown mistake {m}" for m in check.traps if m not in mistake_ids]
    if check.traps and check.kind in WORD_KINDS:
        problems.append("traps are wrong numbers, so a yes/no or nature question can't have them")
    try:
        grading.answer_key(check)
    except Exception as err:   # whatever went wrong, this question could never be marked
        return problems + [f"answer key can't be computed: {err}"]
    for trap in check.traps.values():
        verdict = grading.grade(check, trap).verdict
        if verdict not in ("wrong", "partial"):
            problems.append(f"trap {trap!r} grades {verdict}; a trap must grade wrong or partial")
    if check.kind == "coefficient" and _common_factor(check.equation) != 1:
        problems.append("the coefficients share a factor, so a, b and c have more than one right answer")
    if check.kind in WORD_KINDS:                     # the praise says the answer aloud (grading.key_speech)
        try:
            problems += [f"praise: {p}" for p in _speech_problems(grading.key_speech(check))]
        except ValueError as err:
            problems.append(f"the praise can't say the answer: {err}")
    return problems


def _common_factor(equation: str):
    """The largest whole number dividing every coefficient of (left - right): 2 for 2x² + 4x + 6 = 0."""
    eq = mathcheck.parse_check(equation)
    return abs(Poly(expand((eq.lhs - eq.rhs).doit()), mathcheck.x).content())


def _speech_problems(text: str) -> list[str]:
    """Spoken text is plain words: no LaTeX characters and at most SAY_MAX_WORDS words."""
    problems = []
    if any(char in text for char in LATEX_CHARS):
        problems.append(f"spoken text has LaTeX characters: {text[:40]!r}")
    if SPOKEN_SYMBOLS.search(text):
        problems.append(f"spoken text has '=' or a minus sign; say 'equals' and 'minus': {text[:40]!r}")
    if len(text.split()) > SAY_MAX_WORDS:
        problems.append(f"spoken text is over {SAY_MAX_WORDS} words: {text[:40]!r}")
    return problems


# ---------- helpers for the engine and the tutor ----------

def get_step(concept: Concept, step_id: str | None) -> Step | None:
    """The step with this id, or None."""
    return next((step for step in concept.steps if step.id == step_id), None)


def next_step_id(concept: Concept, step_id: str | None) -> str | None:
    """The step after step_id. None or an unknown id gives the first step; the last step gives None."""
    ids = [step.id for step in concept.steps]
    if step_id not in ids:
        return ids[0]
    position = ids.index(step_id) + 1
    return ids[position] if position < len(ids) else None


def get_check(concept: Concept, check_id: str | None) -> Check | None:
    """The check with this id, or None."""
    return next((check for check in concept.checks if check.id == check_id), None)


def next_check_id(concept: Concept, last_check_id: str | None) -> str:
    """The check after last_check_id in file order, wrapping round. None or an unknown id gives check 1."""
    ids = [check.id for check in concept.checks]
    if last_check_id not in ids:
        return ids[0]
    return ids[(ids.index(last_check_id) + 1) % len(ids)]


def ask_segment(check: Check, formulas: dict[str, str]) -> Segment:
    """The spoken question with its pinned board lines: the label, then the equation (not for word problems).

    `formulas` isn't needed yet (no question places a formula line); it stays in the agreed signature."""
    board = [BoardCommand(op="write", id=LABEL_ID, text=check.label)]
    if check.show_equation:
        board.append(BoardCommand(op="write", id=EQUATION_ID, latex=mathcheck.question_latex(check.equation),
                                  check=check.equation, relation="new"))   # a question starts a fresh chain
    return Segment(say=check.say, board=board)
