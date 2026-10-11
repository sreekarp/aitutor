"""The two LLM prompts (spec §5.2, §6.1): their files, strict JSON schemas, the per-turn messages, and the
checks on what comes back.

The prompt files hold the instructions; this module only adds the facts of the turn. Prompt files are read
on every call, so an edited prompt applies from the next turn.
"""
from __future__ import annotations

from typing import get_args

from pydantic import ValidationError

from app import grading
from app.board import ReplyContext, validate_reply
from app.config import MAX_HINTS, PROMPT_VERSIONS, PROMPTS_DIR
from app.models import (AnswerKey, BoardLine, Check, Concept, Decision, Grade, Mistake, Reply, ReplyType, Segment,
                        Step, Understood)

# ---------- JSON schemas (OpenRouter strict mode: every property required, no extra properties) ----------
_TEXT_OR_NULL = {"type": ["string", "null"]}

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(get_args(ReplyType))},
        "answer": {"type": "string"},
        "showed_working": {"type": "boolean"},
        "mistake": _TEXT_OR_NULL,
        "safety": {"type": "boolean"},
    },
    "required": ["type", "answer", "showed_working", "mistake", "safety"],
    "additionalProperties": False,
}

_BOARD_COMMAND = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["write", "highlight"]},     # never "clear": only the engine clears
        "id": _TEXT_OR_NULL, "latex": _TEXT_OR_NULL, "check": _TEXT_OR_NULL,
        "relation": {"type": "string", "enum": ["equiv", "new", "select"]},
        "formula": _TEXT_OR_NULL, "text": _TEXT_OR_NULL, "target": _TEXT_OR_NULL, "part": _TEXT_OR_NULL,
    },
    "required": ["op", "id", "latex", "check", "relation", "formula", "text", "target", "part"],
    "additionalProperties": False,
}

RESPOND_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"say": {"type": "string"}, "board": {"type": "array", "items": _BOARD_COMMAND}},
                "required": ["say", "board"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["segments"],
    "additionalProperties": False,
}

# ---------- words the prompts use ----------
WORKING = {
    "is_root": "the arithmetic of putting the value into the equation",
    "roots": ("a factorised form, a split of the middle term, a step of the quadratic formula, the value of D, "
              "or (in a word problem) the equation they formed"),
    "discriminant": "the value or the sign of D",
    "nature": "the value or the sign of D",
    "has_real_roots": "the equation they formed, or the value or the sign of D",
    "is_quadratic": "what is left after simplifying (for example, that the x squared terms cancel)",
    "coefficient": "the equation rearranged into standard form",
}
ANSWER_FORMATS = {
    "roots": ('the roots in SymPy syntax, separated by commas ("4, -1", "1/2, -5", "1 + sqrt(2), 1 - sqrt(2)"); '
              'a ± answer is written as two values; "none" if they say there are no real roots'),
    "is_quadratic": '"yes" or "no"',
    "is_root": '"yes" or "no"',
    "has_real_roots": '"yes" or "no"',
    "nature": '"distinct", "equal" or "none"',
    "coefficient": 'one number in SymPy syntax, e.g. "-3"',
    "discriminant": 'one number in SymPy syntax, e.g. "-8"',
}
WORD_PROBLEM_FORMAT = ('every value they offer for the quantity asked for, in SymPy syntax, separated by commas '
                       '("6 or minus 9" -> "6, -9"); ignore values of other quantities')
STRATEGIES = {
    "worked_example": "solve a similar but different equation on the board, step by step",
    "substitute_back": ("put the student's own values back into the equation on the board and show they "
                        "don't give zero"),
    "compare_non_example": "write the correct version on the board, and say the incorrect version in words only",
}
DETAILS = {"missing": "a root is missing", "extra": "a value that can't be the answer was kept",
           "not_a_root": "some values are not roots", "empty": "no values given"}
NATURE_WORDS = {"distinct": "two distinct real roots", "equal": "two equal real roots", "none": "no real roots"}


def load_prompt(name: str) -> str:
    """The text of app/prompts/<name>.md, e.g. load_prompt("respond.v1")."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def working_hint(kind: str) -> str:
    """What counts as showing working for this kind of question (spec §5.2)."""
    return WORKING[kind]


# ---------- the messages for one turn ----------
def understand_messages(*, concept: Concept, phase: str, check: Check | None, previous_answer: str,
                        tutor_last_line: str, student_text: str) -> list[dict]:
    """The system prompt plus this turn's facts for UNDERSTAND."""
    lines = [f"CONCEPT: {concept.title}", f"PHASE: {phase}"]
    if check is None:
        lines.append("PENDING QUESTION: none (teaching)")
    else:
        lines += [f"PENDING QUESTION: {check.say}",
                  f"QUESTION KIND: {check.kind}",
                  f"ANSWER FORMAT: {_answer_format(check)}",
                  f"WORKING FOR THIS KIND: {working_hint(check.kind)}",
                  f"STUDENT'S PREVIOUS ANSWER TO THIS QUESTION: {previous_answer or 'none'}"]
    lines.append("KNOWN MISTAKES:")
    lines += [f"- {mistake.id}: {mistake.looks_like}" for mistake in concept.mistakes]
    lines += [f"TUTOR'S LAST LINE: {tutor_last_line or 'none'}", f"STUDENT SAID: {student_text}"]
    return _messages("understand", lines)


def respond_messages(*, decision: Decision, concept: Concept, step: Step | None, check: Check | None,
                     key: AnswerKey | None, next_check: Check | None, next_key: AnswerKey | None,
                     understood: Understood, grade: Grade | None, mistake: Mistake | None,
                     hint_text: str | None, board: list[BoardLine], check_equations: list[str],
                     formulas: dict[str, str], previous_reason: str, student_text: str = "") -> list[dict]:
    """The system prompt plus this turn's facts for RESPOND. The student's words come last (spec §6.1)."""
    lines = [f"ACTION: {decision.action}", f"ENGINE'S REASON: {decision.reason}",
             f"LAST DECISION'S REASON: {previous_reason or 'none'}", f"CONCEPT: {concept.title}"]
    if step is not None:
        lines.append("CURRENT STEP: " + " ".join(segment.say for segment in step.segments))
    if check is not None:
        secret = ("ANSWER (work it out on the board)" if decision.action == "needs_review"
                  else "SECRET ANSWER (never reveal it)")
        lines += [f"PENDING QUESTION: {check.say}", f"QUESTION EQUATION: {_equation(check)}",
                  f"{secret}: {_answer_text(key)}"]
    if next_check is not None:
        lines += [f"NEXT QUESTION (asked right after your reply): {next_check.say}",
                  f"NEXT QUESTION EQUATION: {_equation(next_check)}",
                  f"NEXT SECRET ANSWER (never reveal it): {_answer_text(next_key)}"]
    lines.append(f"UNDERSTOOD AS: {_understood_text(understood)}")
    if grade is not None:
        detail = f" ({DETAILS.get(grade.detail, grade.detail)})" if grade.detail else ""
        lines.append(f"SYMPY'S VERDICT: {grade.verdict}{detail}")
    if mistake is not None:
        lines.append(f"MISTAKE: {mistake.id}. It looks like: {mistake.looks_like}. How to fix it: {mistake.fix}")
    if decision.strategy in STRATEGIES:
        lines.append(f"STRATEGY: {decision.strategy}: {STRATEGIES[decision.strategy]}")
    if decision.action == "hint":
        lines.append(f"HINT LEVEL: {decision.hint_level} of {MAX_HINTS}")
    if hint_text:
        lines.append(f"LESSON'S HINT: {hint_text}")
    lines.append("BOARD (id: what it shows):")
    lines += [f"- {line.id}: {_shown(line)}" for line in board] or ["- (empty)"]
    lines.append("FORMULAS (place them by name): " + ", ".join(formulas))
    lines.append("NEVER USE AS EXAMPLES: " + "; ".join(check_equations))
    lines.append(f"STUDENT SAID: {student_text}")
    return _messages("respond", lines)


# ---------- checking what comes back (the tutor's `validate` for chat_json) ----------
def read_understood(data: dict, check: Check | None) -> tuple[Understood | None, list[str]]:
    """UNDERSTAND's JSON as an Understood, or the problems with it. An answer must fit the question (spec §5.2)."""
    try:
        understood = Understood.model_validate(data)
    except ValidationError as err:
        return None, _shape_problems(err)
    answer = understood.answer.strip()
    if answer and check is not None and grading.parse_answer(answer, check.kind) is None:
        return None, [f"answer {answer!r} doesn't fit the ANSWER FORMAT: {_answer_format(check)}"]
    return understood, []


def read_reply(data: dict, ctx: ReplyContext) -> tuple[list[Segment], list[str]]:
    """RESPOND's JSON as segments, and every §6.3 problem with them ([] means it can be shown)."""
    try:
        segments = Reply.model_validate(data).segments
    except ValidationError as err:
        return [], _shape_problems(err)
    return segments, validate_reply(segments, ctx)


# ---------- small helpers ----------
def _messages(role: str, lines: list[str]) -> list[dict]:
    return [{"role": "system", "content": load_prompt(PROMPT_VERSIONS[role])},
            {"role": "user", "content": "\n".join(lines)}]


def _answer_format(check: Check) -> str:
    if check.kind == "roots" and not check.show_equation:
        return WORD_PROBLEM_FORMAT
    return ANSWER_FORMATS[check.kind]


def _equation(check: Check) -> str:
    hidden = "" if check.show_equation else " (not on the board: forming it is the skill being tested)"
    return check.equation + hidden


def _answer_text(key: AnswerKey | None) -> str:
    """An answer key as plain text: "2, 3", "no real roots", "yes"."""
    if key is None:
        return "none"
    if key.word is not None:
        return NATURE_WORDS.get(key.word, key.word)
    if not key.values:
        return "no real roots"
    return ", ".join(str(value) for value in key.values)


def _understood_text(understood: Understood) -> str:
    answer = f', answer "{understood.answer}"' if understood.answer else ""
    working = "yes" if understood.showed_working else "no"
    return f"{understood.type}{answer}, working shown: {working}"


def _shown(line: BoardLine) -> str:
    if line.formula:
        return f"formula {line.formula}, latex {line.latex}"
    if line.latex is not None:
        return f"latex {line.latex}"
    return f"text {line.text}"


def _shape_problems(err: ValidationError) -> list[str]:
    """Pydantic's complaints as short lines, e.g. "segments.0.say: Field required"."""
    return [f"{'.'.join(str(part) for part in e['loc']) or 'reply'}: {e['msg']}" for e in err.errors()]
