"""Checks the maths on the whiteboard before a student sees it (reply rule 3 in board.py).

It grew out of an equation_check.py prototype, hardened after two reviews: it never runs the LLM's code,
never crashes or hangs, and refuses any line it can't fully check. A board line is a dict with "latex"
(what the student sees), "check" (the same maths in SymPy syntax) and "relation": "equiv" (the default:
same roots as the line before), "select" (keeps some of those roots) or "new" (a fresh start).
Reading the maths safely is mathread.py's job; its names are used from here too (mathcheck.parse_check).
"""
from __future__ import annotations

from sympy import Eq, FiniteSet, Or, Poly, S, expand, simplify, solveset

from app.mathread import CheckError, k, parse_check, parse_display, question_latex, root_of, unknown, x

__all__ = ["CheckError", "check_line", "check_steps", "k", "parse_check", "parse_display", "problems",
           "question_latex", "same_maths", "self_test", "solutions", "x"]


def solutions(expr):
    """Complex solution set of an equation or an 'or' line (x = a or x - b = 0), in x, or in k for a line with
    only k in it; None for a bare expression."""
    letter = unknown(expr)
    if isinstance(expr, Or):
        values = [root_of(part, letter) for part in expr.args]
        if None in values:
            raise CheckError("an 'or' line joins first-degree equations: Or(Eq(x, a), Eq(x, b)) or "
                             "Or(Eq(x - a, 0), Eq(x - b, 0))")
        return FiniteSet(*values)
    if isinstance(expr, Eq):
        # Complex roots, not just real ones: otherwise any two equations with no real roots look the same.
        return solveset(expr.lhs - expr.rhs, letter, S.Complexes)
    return None


def _equal(a, b):
    """True when a and b have the same value (for every x, when they contain x)."""
    return simplify(a - b) == 0


def same_maths(a, b) -> bool | None:
    """Do two parsed statements say the same thing? None when one of them couldn't be read."""
    # Root claims compare as sets; arithmetic side by side, by value; other equations may swap sides
    # or be negated; a bare expression must be exactly equal, never negated.
    if a is None or b is None:
        return None
    if a.free_symbols != b.free_symbols:
        return False                                                  # "x = 6" and "k = 6" aren't the same
    if isinstance(a, Or) or isinstance(b, Or):
        return solutions(a) == solutions(b)
    if isinstance(a, Eq) and isinstance(b, Eq):
        if not (a.free_symbols or b.free_symbols):
            return _equal(a.lhs, b.lhs) and _equal(a.rhs, b.rhs)
        da, db = a.lhs - a.rhs, b.lhs - b.rhs
        return _equal(da, db) or _equal(da, -db)
    if isinstance(a, Eq) or isinstance(b, Eq):
        return False                                                  # an equation vs a bare expression
    return _equal(a, b)


def check_line(line: dict, prev):
    """(parsed line, note) for one board line; raises CheckError when it must not be shown."""
    # The note says why the line wasn't compared with the one before: "arithmetic", "new" or "".
    if not line.get("latex") or not line.get("check"):
        raise CheckError("a maths line needs both latex and check")
    expr = parse_check(line["check"])

    # 1. The LaTeX the student sees must say the same thing as the checked form.
    shown = parse_display(line["latex"])
    if shown is None:
        raise CheckError(f"can't read the LaTeX {line['latex']!r}")
    if not same_maths(shown, expr):
        raise CheckError(f"LaTeX shows {shown}, but the checked form is {expr}")

    # 2. Pure arithmetic (no x) must simply be true, e.g. a substitution check. It is never chained.
    if not expr.free_symbols:
        if isinstance(expr, Eq) and not _equal(expr.lhs, expr.rhs):
            raise CheckError(f"arithmetic is false: left side is {simplify(expr.lhs)}, not {expr.rhs}")
        return expr, "arithmetic"

    # 3. Compare with the previous line.
    relation = line.get("relation") or "equiv"
    if relation == "new":
        return expr, "new"
    if prev is None:
        return expr, ""
    if not isinstance(expr, (Eq, Or)):                                # a bare expression
        _check_expression(expr, prev)
        return expr, ""
    if not isinstance(prev, (Eq, Or)):                                # an equation after a bare expression E
        prev = Eq(prev, 0, evaluate=False)                            # is compared with E = 0
    if expr.free_symbols == prev.free_symbols == {x, k}:              # a find-k question's equation, rearranged
        if not _same_for_every_k(prev, expr):
            raise CheckError("not the same equation as the previous line, for every k")
        return expr, ""
    before, after = _compared(prev, expr)
    if relation == "select":
        if not after.is_subset(before):
            raise CheckError(f"{after} is not among the previous roots {before}")
    elif before != after:
        detail = [f"{word} {roots}" for word, roots in (("loses", before - after), ("adds", after - before))
                  if roots != S.EmptySet]
        raise CheckError("not equivalent to the previous line: " + ", ".join(detail))
    return expr, ""


def _compared(prev, expr):
    """The two solution sets to compare: of two lines in x, or two lines in k. A line in k right after an equation
    in x and k (a find-k question) is compared with that equation's D = 0, the condition for two equal roots."""
    if prev.free_symbols == {x, k} and expr.free_symbols == {k}:
        return solutions(_equal_roots_condition(prev)), solutions(expr)
    if prev.free_symbols != expr.free_symbols:
        raise CheckError("a line in k can't follow a line in x, or the other way round: start it with "
                         "relation 'new'")
    return solutions(prev), solutions(expr)


def _equal_roots_condition(equation):
    """D = 0 for a quadratic in x whose numbers use k, as an equation in k: b² - 4ac = 0."""
    side = expand((equation.lhs - equation.rhs).doit()) if isinstance(equation, Eq) else None
    if side is None or not side.is_polynomial(x) or Poly(side, x).degree() != 2:
        raise CheckError("a line in k after an equation in x is its D = 0, so that equation must be a quadratic")
    a, b, c = Poly(side, x).all_coeffs()
    d = expand(b**2 - 4 * a * c)
    if k not in d.free_symbols:                                       # e.g. x² + 2kx + k² = 0: D is 0 for every k
        raise CheckError(f"this equation's D is {d} whatever k is, so no line in k follows from it")
    return Eq(d, 0, evaluate=False)


def _same_for_every_k(a, b) -> bool:
    """Two equations in x and k say the same thing: one's left side minus right side is a fixed number times the
    other's, so they agree whatever k is."""
    if not (isinstance(a, Eq) and isinstance(b, Eq)):
        return False
    below = simplify(b.lhs - b.rhs)
    if below == 0:                                                    # "kx = kx" is no rearrangement of anything
        return False
    ratio = simplify((a.lhs - a.rhs) / below)
    return bool(ratio.is_number and ratio.is_finite and ratio != 0)


def _check_expression(expr, prev):
    """A bare expression must equal the expression before it, or an equation's left side minus its right."""
    if isinstance(prev, Or):
        raise CheckError("an expression can't follow a root claim; use relation 'new'")
    if isinstance(prev, Eq):
        if not _equal(prev.lhs - prev.rhs, expr):
            raise CheckError(f"{expr} is not the left side minus the right side of {prev}")
    elif not _equal(prev, expr):
        raise CheckError(f"{expr} is not equal to {prev}")


def check_steps(lines: list[dict], anchor: str | None = None) -> list[tuple[int, str, str, str]]:
    """(index, "ok"|"FAIL", latex, note) per line; stops at the first FAIL. anchor = SymPy text of the
    pending question's equation: the first line is compared with it (unless relation is "new")."""
    prev, report = (parse_check(anchor) if anchor else None), []
    for i, line in enumerate(lines, 1):
        try:
            expr, note = check_line(line, prev)
            report.append((i, "ok", line["latex"], note))
            if expr.free_symbols:  # arithmetic lines don't become the reference for the next step
                prev = expr
        except Exception as err:  # any failure means "don't show it"
            why = str(err) if isinstance(err, CheckError) else f"internal error: {type(err).__name__}"
            report.append((i, "FAIL", line.get("latex") or "", why))
            break  # nothing after a wrong line can be trusted
    return report


def problems(lines: list[dict], anchor: str | None = None) -> list[str]:
    """The FAIL notes from check_steps, as "line <id>: <why>". Empty list = all lines fine."""
    return [f"line {lines[i - 1].get('id', i)}: {why}"
            for i, status, _, why in check_steps(lines, anchor) if status == "FAIL"]


def self_test() -> None:
    """Raise RuntimeError unless case F (board x + 13, check x + 12) fails on its display."""
    # A wrong antlr install reads no LaTeX at all, which would quietly switch the display check off.
    case_f = [{"latex": "x^2 + x + 8 = x^2 - 4", "check": "Eq(x**2 + x + 8, x**2 - 4)"},
              {"latex": "x + 13 = 0", "check": "Eq(x + 12, 0)"}]
    report = check_steps(case_f)
    if [r[1] for r in report] != ["ok", "FAIL"] or not report[1][3].startswith("LaTeX shows"):
        raise RuntimeError("mathcheck self-test failed (is antlr4-python3-runtime 4.11.1 installed?): "
                           f"{report}")
