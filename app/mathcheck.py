"""Checks the maths on the whiteboard before a student sees it (spec §6.3 rule 3).

Your prototype, proposal/equation_check.py, hardened after two reviews: it never runs the LLM's code,
never crashes or hangs, and refuses any line it can't fully check. A board line is a dict with "latex"
(what the student sees), "check" (the same maths in SymPy syntax) and "relation": "equiv" (the default:
same roots as the line before), "select" (keeps some of those roots) or "new" (a fresh start).
"""
from __future__ import annotations

import re

from sympy import (Add, Eq, Expr, FiniteSet, Float, Integer, Mul, Number, Or, Pow, Rational, S,
                   Symbol, preorder_traversal, simplify, solveset, sqrt)
from sympy import latex as to_latex
from sympy.core.function import AppliedUndef
from sympy.core.parameters import evaluate
from sympy.core.relational import Relational
from sympy.parsing.latex import parse_latex
from sympy.parsing.sympy_parser import parse_expr, rationalize, standard_transformations

x = Symbol("x")
# Eq stays unevaluated: otherwise sympy turns Eq(4, -4) into plain False before we can inspect it.
ALLOWED_NAMES = {"x": x, "Eq": lambda a, b: Eq(a, b, evaluate=False), "Or": Or,
                 "sqrt": sqrt, "Rational": Rational}
# What sympy's generated code may call; nothing else (no builtins) is reachable.
GLOBALS = {"Integer": Integer, "Float": Float, "Symbol": Symbol, "Rational": Rational,
           "Add": Add, "Mul": Mul, "Pow": Pow, "__builtins__": {}}
TOKEN = re.compile(r"\s*(\d+\.?\d*|[A-Za-z_]+|\*\*|[-+*/(),])")    # no "=": equations use Eq
# Safety limits, so "9**9**9" or "x*x*...*x" can't hang the server: at most 200 characters and
# 4 powers, every exponent a number up to 10, and x never raised above the power 10 overall.
MAX_LENGTH, MAX_POWERS, MAX_EXPONENT = 200, 4, 10
ALLOWED_NODES = (Symbol, Number, Add, Mul, Pow, Eq, Or)             # no factorials, sums or functions
OR_WORD = re.compile(r"\\text\{\s*or\s*\}|,")                        # "x = 2 \text{ or } x = 3"
SPACING = re.compile(r"\\left|\\right|\\[,;:!]|\\q?quad")            # strict mode rejects a leading \left(
SQUEEZE = re.compile(r"(?<=[^A-Za-z])\s+|\s+(?=[^A-Za-z])")          # "(x - 2) (x + 3)" -> "(x-2)(x+3)"
# Load the LaTeX parser now. This fails at startup if antlr is missing or the wrong version, and its
# first call must not happen inside evaluate(False) (sympy.physics.units fails to import there).
parse_latex("x = 1")


class CheckError(Exception):
    """A check string or board line that must not be shown; the message says why."""


# ---------- reading maths safely ----------

def parse_check(text: str):
    """Parse SymPy-syntax text (from a lesson or the LLM) without running arbitrary code."""
    text = text.strip()
    if not text or len(text) > MAX_LENGTH or text.count("**") > MAX_POWERS:
        raise CheckError("empty, too long, or too many powers")
    pos = 0
    while pos < len(text):                      # parse_expr uses eval, so every token is whitelisted first
        m = TOKEN.match(text, pos)
        if not m:
            raise CheckError(f"unexpected character {text[pos:].strip()[0]!r}")
        tok = m.group(1)
        if (tok[0].isalpha() or tok[0] == "_") and tok not in ALLOWED_NAMES:
            raise CheckError(f"name not allowed: {tok}")
        pos = m.end()
    try:
        # evaluate=False keeps "Eq(2*1**2 - 3*1 + 1, 0)" as an equation instead of collapsing it to True;
        # rationalize turns decimals into exact fractions while parsing (12.5 becomes 25/2).
        expr = parse_expr(text, local_dict=dict(ALLOWED_NAMES), global_dict=dict(GLOBALS),
                          transformations=standard_transformations + (rationalize,), evaluate=False)
    except Exception as err:
        raise CheckError(f"could not read {text!r} ({type(err).__name__})") from None
    if not isinstance(expr, (Expr, Eq, Or)):                          # e.g. "1, 2" parses to a tuple
        raise CheckError("expected one expression or equation")
    problem = _limit_problem(expr)
    if problem:
        raise CheckError(problem)
    return expr


def _limit_problem(expr):
    """Why expr is unsafe to work with (the same limits as answers), or "" when it is fine."""
    for node in preorder_traversal(expr):
        if not isinstance(node, ALLOWED_NODES):
            return f"{type(node).__name__} is not allowed"            # e.g. 10000000!
        if isinstance(node, Pow) and not (node.exp.is_Rational and abs(node.exp) <= MAX_EXPONENT):
            return "powers must be small numbers"                      # e.g. 9**9**9
    if _degree(expr) > MAX_EXPONENT:
        return "x is raised to too high a power"                       # e.g. ((x**10)**10)**10
    return ""


def _degree(expr):
    """How high a power of x expr can reach, worked out without multiplying anything out."""
    if expr == x:
        return 1
    if isinstance(expr, Pow):
        return _degree(expr.base) * abs(expr.exp)
    if isinstance(expr, Mul):
        return sum(_degree(arg) for arg in expr.args)
    if isinstance(expr, (Add, Eq, Or)):
        return max(_degree(arg) for arg in expr.args)
    return 0


def _parse_one(latex):
    """One equation or expression from LaTeX, or None when it is unreadable or unsafe."""
    try:
        with evaluate(False):                                         # keep "(-2)^2 = -4" an equation
            shown = parse_latex(latex, strict=True)                   # strict: don't ignore trailing text
            # The LaTeX parser reads "x(x+1)" as a function call; in this chapter it means x times (x+1).
            shown = shown.replace(lambda e: isinstance(e, AppliedUndef) and e.func.__name__ == "x",
                                  lambda e: x * e.args[0])
    except ImportError:
        raise                                                         # a wrong antlr must not switch checks off
    except Exception:
        return None
    if not isinstance(shown, (Eq, Expr)) or shown.free_symbols - {x}:
        return None                                                   # e.g. \text{..} read as letters
    if isinstance(shown, Eq) and any(isinstance(s, Relational) for s in (shown.lhs, shown.rhs)):
        return None                                                   # chained "a = b = c"
    fractions = {f: Rational(str(f)) for f in shown.atoms(Float)}      # decimals: 12.5 becomes 25/2
    with evaluate(False):
        shown = shown.xreplace(fractions)
    return None if _limit_problem(shown) else shown                   # e.g. x = 9^{9^{9}}


def parse_display(latex: str):
    """Parse the LaTeX shown to the student, or None when it is unreadable or too big to check."""
    if len(latex) > MAX_LENGTH or latex.count("^") > MAX_POWERS:
        return None
    latex = SPACING.sub(" ", latex)
    parts = [p for p in OR_WORD.split(latex) if p.strip()]
    if "\\pm" in latex:                                               # "1 \pm \sqrt{2}" is two roots
        parts = [p.replace("\\pm", sign) for p in parts for sign in "+-"]
    if len(parts) <= 1:
        return _parse_one(latex)
    claims = [_parse_one(p) for p in parts]                           # a root claim, x = a or x = b
    if all(isinstance(c, Eq) and c.lhs == x and not c.rhs.free_symbols for c in claims):
        return Or(*claims)
    return None


def question_latex(equation: str) -> str:
    """The question as LaTeX that reads exactly as written: "(x-2)(x+3)=0", "5+2x^{2}=7x"."""
    tex = to_latex(parse_check(equation), order="none").replace("\\left", "").replace("\\right", "")
    return SQUEEZE.sub("", tex)


# ---------- checking lines and steps ----------

def solutions(expr):
    """Complex solution set of an equation or an 'x = a or x = b' claim; None for a bare expression."""
    if isinstance(expr, Or):
        values = []
        for part in expr.args:
            if not (isinstance(part, Eq) and part.lhs == x and not part.rhs.free_symbols):
                raise CheckError("a root claim must look like Or(Eq(x, a), Eq(x, b))")
            values.append(simplify(part.rhs))                         # evaluate "-1/3" before comparing sets
        return FiniteSet(*values)
    if isinstance(expr, Eq):
        # Complex roots, not just real ones: otherwise any two equations with no real roots look the same.
        return solveset(expr.lhs - expr.rhs, x, S.Complexes)
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
    before, after = solutions(prev), solutions(expr)
    if relation == "select":
        if not after.is_subset(before):
            raise CheckError(f"{after} is not among the previous roots {before}")
    elif before != after:
        detail = [f"{word} {roots}" for word, roots in (("loses", before - after), ("adds", after - before))
                  if roots != S.EmptySet]
        raise CheckError("not equivalent to the previous line: " + ", ".join(detail))
    return expr, ""


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
