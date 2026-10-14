"""The example equations RESPOND may use for a new example (a solved example, a reteach, an explanation), from
index.yaml: checked at startup, and chosen each turn so that none gives an answer away. RESPOND used to pick its
own, and often picked one of the lessons' questions, which the board's rule 7 then rejected."""
from __future__ import annotations

import functools

from sympy import Eq, Or, Poly, PolynomialError, Rational, expand

from app import mathcheck
from app.models import AnswerKey, Curriculum, Example


def for_turn(cur: Curriculum, keys: list[AnswerKey | None]) -> list[str]:
    """The example equations RESPOND may use this turn, one line each with its roots: none sharing a root with
    these answer keys (the question being asked, and the next one; for a find-k question, its values of k, since
    "x equals 4" sounds like "k is 4"), and none with no real roots when one of those answers is "no real roots"."""
    answers = [key for key in keys if key and key.kind in ("roots", "find_k")]
    taken = [complex(value) for key in answers for value in key.all_roots or key.values or []]
    no_roots = any(not key.values for key in answers)
    d_values = [key.values[0] for key in keys if key and key.kind == "discriminant" and key.values]
    lines = []
    for example in cur.examples:
        roots = _numbers(tuple(example.roots))
        if any(abs(root - value) < 1e-9 for root in roots for value in taken) or (no_roots and not roots):
            continue
        if any(discriminant(example) == value for value in d_values):          # its D is the answer asked for
            continue
        said = {0: "no real roots", 1: f"one repeated root {', '.join(example.roots)}"}
        lines.append(f"- {example.latex}: " + said.get(len(roots), "roots " + ", ".join(example.roots)))
    return lines


@functools.lru_cache
def _numbers(roots: tuple[str, ...]) -> list[complex]:
    """An example's roots as numbers (worked out once)."""
    return [complex(mathcheck.parse_check(root)) for root in roots]


def discriminant(example: Example):
    """b squared minus 4ac of an example equation (worked out once)."""
    return _discriminant(example.check)


@functools.lru_cache
def _discriminant(check: str):
    equation = mathcheck.parse_check(check)
    a, b, c = Poly(equation.lhs - equation.rhs, mathcheck.x).all_coeffs()
    return b * b - 4 * a * c


# ---------- rule 7: never a lesson question ----------
def is_a_question(check: str, equations: list[str]) -> bool:
    """Rule 7 (board.py), and the startup check: is this line one of these questions, rearranged, factorised or
    solved? Every topic's questions are compared, so the quick look (_could_be_same) goes first: same_maths is slow
    (a second or more per reply)."""
    try:
        line = mathcheck.parse_check(check)
        return any(_could_be_same(line, question) and mathcheck.same_maths(line, question)
                   for question in map(mathcheck.parse_check, equations))
    except mathcheck.CheckError:
        return False                               # an unreadable check is already reported by rule 3


SAMPLE_X = (Rational(13, 7), Rational(-29, 11))     # x values to compare two equations at (roots of no lesson's)


def _could_be_same(a, b) -> bool:
    """A quick look before mathcheck.same_maths, which is slow: False only when a and b certainly differ. Two
    equations in x differ when, at a sample x, their sides (left minus right) differ either way round; a root claim
    ("x = 2 or x = 5") and an equation differ when a claimed root doesn't solve the equation. Anything else: True,
    and same_maths decides. A real match is never ruled out."""
    if a.free_symbols != b.free_symbols:                        # x and k lines: same_maths says no too
        return False
    if isinstance(a, Or) != isinstance(b, Or):                  # a root claim and an equation
        claim, other = (a, b) if isinstance(a, Or) else (b, a)
        if not (isinstance(other, Eq) and other.free_symbols):
            return True
        try:
            roots = mathcheck.solutions(claim)                            # a claim's own values: nothing is solved
        except mathcheck.CheckError:
            return True
        values = [_at(other.lhs - other.rhs, root) for root in roots]
        return all(value is None or abs(value) < 1e-9 for value in values)
    if not (isinstance(a, Eq) and isinstance(b, Eq) and a.free_symbols and b.free_symbols):
        return True
    for sample in SAMPLE_X:
        u, v = _at(a.lhs - a.rhs, sample), _at(b.lhs - b.rhs, sample)
        if u is None or v is None:
            return True                                         # not a number there: can't tell
        tolerance = 1e-9 * max(1.0, abs(u), abs(v))
        if abs(u - v) > tolerance and abs(u + v) > tolerance:
            return False
    return True


def _at(expr, value) -> complex | None:
    """expr at x = value, as a number; None when it isn't one there."""
    try:
        return complex(expr.subs(mathcheck.x, value))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ---------- the startup check ----------
def problems(cur: Curriculum) -> list[str]:
    """Every problem with the example equations, one readable line each ([] means all is well)."""
    questions, equations = [], [check.equation for concept in cur.concepts.values() for check in concept.checks]
    for check in (check for concept in cur.concepts.values() for check in concept.checks):
        try:
            question = mathcheck.parse_check(check.equation)
        except mathcheck.CheckError:
            continue                                 # reported with its lesson
        if isinstance(question, Eq) and question.free_symbols:
            questions.append((check.id, question))
    found = [problem for example in cur.examples for problem in _problems(example, questions)]
    return found + [f"example {example.latex!r}: it is a lesson question" for example in cur.examples
                    if not found and is_a_question(example.check, equations)]


def _problems(example: Example, questions: list) -> list[str]:
    """An example equation is a quadratic whose LaTeX says the same as its check, with exactly the real roots listed;
    and no lesson question has all its roots (solving it would give that answer away)."""
    name = f"example {example.latex!r}"
    try:
        equation, roots = mathcheck.parse_check(example.check), [mathcheck.parse_check(r) for r in example.roots]
    except mathcheck.CheckError as err:
        return [f"{name}: {err}"]
    if not mathcheck.same_maths(mathcheck.parse_display(example.latex), equation):
        return [f"{name}: the LaTeX doesn't say the same as the check"]
    try:
        poly = Poly(equation.lhs - equation.rhs, mathcheck.x)
    except (AttributeError, PolynomialError):
        return [f"{name}: not a quadratic equation"]
    if poly.degree() != 2:
        return [f"{name}: not a quadratic equation"]
    a, b, c = poly.all_coeffs()
    expected = 2 if b * b - 4 * a * c > 0 else 1 if b * b - 4 * a * c == 0 else 0
    exact = all(expand((equation.lhs - equation.rhs).subs(mathcheck.x, root).doit()) == 0 for root in roots)
    if len(set(roots)) != expected or len(roots) != expected or not exact:
        return [f"{name}: its real roots aren't exactly {example.roots}"]
    clash = [check_id for check_id, question in questions if roots and all(_solves(question, r) for r in roots)]
    return [f"{name}: a lesson question ({', '.join(clash)}) has these roots"] if clash else []


def _solves(equation, root) -> bool:
    """Does x = root make the equation true (to within rounding)?"""
    try:
        return abs(complex((equation.lhs - equation.rhs).subs(mathcheck.x, root))) < 1e-9
    except (TypeError, ValueError):
        return False
