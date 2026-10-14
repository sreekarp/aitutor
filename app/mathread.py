"""Reads maths safely, for mathcheck.py: SymPy syntax from a lesson or the LLM, and the LaTeX a student sees.

It never runs the LLM's code and never hangs: every name is checked before parsing, and anything too big to work
with is refused. There are two letters: x, and k, the unknown number in a "find k" question.
"""
from __future__ import annotations

import re

from sympy import (Add, Eq, Expr, Float, Integer, Mul, Number, Or, Poly, Pow, Rational, Symbol, preorder_traversal,
                   simplify, sqrt)
from sympy import latex as to_latex
from sympy.core.function import AppliedUndef
from sympy.core.parameters import evaluate
from sympy.core.relational import Relational
from sympy.parsing.latex import parse_latex
from sympy.parsing.sympy_parser import parse_expr, rationalize, standard_transformations

x, k = Symbol("x"), Symbol("k")
LETTERS = {"x": x, "k": k}
# Eq stays unevaluated: otherwise sympy turns Eq(4, -4) into plain False before we can inspect it.
ALLOWED_NAMES = {**LETTERS, "Eq": lambda a, b: Eq(a, b, evaluate=False), "Or": Or,
                 "sqrt": sqrt, "Rational": Rational}
# What sympy's generated code may call; nothing else (no builtins) is reachable.
GLOBALS = {"Integer": Integer, "Float": Float, "Symbol": Symbol, "Rational": Rational,
           "Add": Add, "Mul": Mul, "Pow": Pow, "__builtins__": {}}
TOKEN = re.compile(r"\s*(\d+\.?\d*|[A-Za-z_]+|\*\*|[-+*/(),])")    # no "=": equations use Eq
# Safety limits, so "9**9**9" or "x*x*...*x" can't hang the server: at most 200 characters and
# 4 powers, every exponent a number up to 10, and x and k never raised above the power 10 overall.
MAX_LENGTH, MAX_POWERS, MAX_EXPONENT = 200, 4, 10
ALLOWED_NODES = (Symbol, Number, Add, Mul, Pow, Eq, Or)             # no factorials, sums or functions
OR_WORD = re.compile(r"\\text\{\s*or\s*\}|,")                        # "x = 2 \text{ or } x = 3"
SPACING = re.compile(r"\\left|\\right|\\[,;:!]|\\q?quad")            # strict mode rejects a leading \left(
SQUEEZE = re.compile(r"(?<=[^A-Za-z])\s+|\s+(?=[^A-Za-z])|(?<=\bk)\s+(?=x)")   # "(x - 2) (x + 3)" -> "(x-2)(x+3)"
# Load the LaTeX parser now. This fails at startup if antlr is missing or the wrong version, and its
# first call must not happen inside evaluate(False) (sympy.physics.units fails to import there).
parse_latex("x = 1")


class CheckError(Exception):
    """A check string or board line that must not be shown; the message says why."""


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
    """How high a power of x (or k) expr can reach, worked out without multiplying anything out."""
    if expr in (x, k):
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
            shown = shown.replace(lambda e: isinstance(e, AppliedUndef) and e.func.__name__ in LETTERS,
                                  lambda e: LETTERS[e.func.__name__] * e.args[0])
    except ImportError:
        raise                                                         # a wrong antlr must not switch checks off
    except Exception:
        return None
    if not isinstance(shown, (Eq, Expr)) or shown.free_symbols - {x, k}:
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
    claims = [_parse_one(p) for p in parts]                           # x = a or x = b, or x - a = 0 or x - b = 0
    if None in claims:
        return None
    claim = Or(*claims)
    return claim if all(root_of(c, unknown(claim)) is not None for c in claims) else None


def question_latex(equation: str) -> str:
    """The question as LaTeX that reads exactly as written: "(x-2)(x+3)=0", "5+2x^{2}=7x"."""
    tex = to_latex(parse_check(equation), order="none").replace("\\left", "").replace("\\right", "")
    return SQUEEZE.sub("", tex)


def unknown(expr):
    """The letter a line is solved for: k in a line with k but no x ("k^2 = 36"), otherwise x."""
    return k if expr.free_symbols == {k} else x


def root_of(part, letter=x):
    """The one root of a first-degree equation in this letter ("x = 3", "x - 2 = 0", "2k + 1 = 0"), else None."""
    if not isinstance(part, Eq):
        return None
    side = part.lhs - part.rhs
    if not side.is_polynomial(letter) or Poly(side, letter).degree() != 1:
        return None
    slope, start = Poly(side, letter).all_coeffs()
    root = simplify(-start / slope)                                   # evaluate "-1/3" before comparing sets
    return None if root.free_symbols else root                        # "x = k" isn't a value of x
