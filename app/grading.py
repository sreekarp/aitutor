"""Answer keys, reading answers and grading, all done by SymPy."""
from __future__ import annotations

import re

from sympy import Eq, Expr, Poly, S, expand, simplify, solveset, sstr

from app.mathcheck import k, parse_check, x
from app.models import WORD_KINDS, AnswerKey, Check, Grade

# ---------- reading an answer ----------

MAX_VALUE_CHARS = 60     # one value, e.g. "2 + sqrt(3)"
MAX_VALUES = 10          # an answer with more values than this isn't read
# Typed symbols -> SymPy syntax ("\u2212" is the unicode minus, "\u2013" an en dash); ± spellings -> "±".
SYMBOLS = {"\u2212": "-", "\u2013": "-", "×": "*", "÷": "/", "^": "**", "+/-": "±", "+-": "±"}
SEPARATORS = re.compile(r",|;|&|\band\b|\bor\b")    # what can stand between two values
NONE_PHRASES = ("none", "no real roots", "no real root", "no roots", "no solution", "no real solution",
                "not real", "imaginary")
YES_NO_WORDS = {"yes": ("yes", "yeah", "yep", "haan", "han", "possible"),
                "no": ("no", "nope", "nahi", "nahin", "not possible", "impossible", "no real roots")}
NATURE_WORDS = {"distinct": ("distinct", "different", "two distinct real roots", "two distinct roots"),
                "equal": ("equal", "same", "repeated", "coincident", "two equal real roots", "two equal roots"),
                "none": NONE_PHRASES}


def parse_answer(text: str, kind: str):
    """Numeric kinds -> list of SymPy numbers ([] for "none"); word kinds -> the word; None if unreadable."""
    if kind in WORD_KINDS:
        return _read_word(text, kind)
    text = _tidy(text)               # lower case, no outer spaces or final full stop
    text = _plain_symbols(text)      # minus signs, × ÷ ^ and the spellings of ±
    text = _square_roots(text)       # √2 -> sqrt(2), 2√3 -> 2*sqrt(3)
    text = _drop_x_equals(text)      # "x = 2, x = 3" -> "2, 3" (and "k = 4" -> "4")
    if _words_only(text) in NONE_PHRASES:
        return []
    values = []
    for part in _split_values(text):         # "2 and 3" -> "2", "3"
        for value in _plus_minus(part):      # "1 ± sqrt(2)" -> "1 + sqrt(2)", "1 - sqrt(2)"
            number = _read_number(value)
            if number is None:
                return None
            values.append(number)
    return values if 0 < len(values) <= MAX_VALUES else None


def same_value(a, b) -> bool:
    """True when two numbers are equal by value: 2/6 = 1/3, sqrt(2/3) = sqrt(6)/3."""
    return simplify(a - b) == 0


def _tidy(text):
    return text.strip().lower().rstrip(".!?").strip()


def _plain_symbols(text):
    for symbol, plain in SYMBOLS.items():
        text = text.replace(symbol, plain)
    return text


def _square_roots(text):
    text = re.sub(r"(\d)\s*√", r"\1*√", text)                  # 2√3 -> 2*√3
    text = re.sub(r"√\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", text)    # √3 -> sqrt(3)
    return text.replace("√", "sqrt")                             # √(2/3) -> sqrt(2/3)


def _drop_x_equals(text):
    return re.sub(r"(?<![\w.])[xk]\s*=\s*", "", text)     # not the "x =" inside "2x = 6"


def _words_only(text):
    """Letters and single spaces only: 'No real roots!' -> 'no real roots'."""
    return " ".join(re.sub(r"[^a-z]+", " ", text.lower()).split())


def _split_values(text):
    return [part.strip() for part in SEPARATORS.split(text) if part.strip()]


def _plus_minus(value):
    return [value.replace("±", "+"), value.replace("±", "-")] if "±" in value else [value]


def _read_number(text):
    """One value as a real SymPy number, or None. parse_check refuses powers above 10."""
    if len(text) > MAX_VALUE_CHARS:
        return None
    try:
        value = parse_check(text).doit()
    except Exception:                # any parse or maths error just means "can't read it"
        return None
    if isinstance(value, Expr) and not value.free_symbols and value.is_real and value.is_finite:
        return value
    return None


def _read_word(text, kind):
    phrase = _words_only(text)
    choices = NATURE_WORDS if kind == "nature" else YES_NO_WORDS
    for word, spellings in choices.items():
        if phrase in spellings:
            return word
    return None


# ---------- answer keys ----------

KEY_WORDS = {
    "is_quadratic": {"yes": "Yes, it is a quadratic equation", "no": "No, it is not a quadratic equation"},
    "has_real_roots": {"yes": "Yes, it is possible: the equation has real roots",
                       "no": "No, it is not possible: the equation has no real roots"},
    "nature": {"distinct": "Two distinct real roots", "equal": "Two equal real roots", "none": "No real roots"},
}


def answer_key(check: Check) -> AnswerKey:
    """The check's right answer, computed by SymPy. Raises ValueError or CheckError if its maths is broken."""
    expr = _left_minus_right(check.equation)
    kind = check.kind
    if (k in expr.free_symbols) != (kind == "find_k"):
        raise ValueError(f"{check.id}: only a find_k question has k in it, and it must")
    if kind == "roots":
        all_roots = _real_roots(expr)
        values = [r for r in all_roots if r.is_positive] if check.keep == "positive" else list(all_roots)
        return AnswerKey(kind=kind, values=values, all_roots=all_roots)
    if kind == "is_quadratic":
        return AnswerKey(kind=kind, word="yes" if Poly(expr, x).degree() == 2 else "no")
    if kind == "is_root":
        return AnswerKey(kind=kind, word="yes" if simplify(expr.subs(x, _candidate(check))) == 0 else "no")
    a, b, c = _abc(expr)
    if kind == "coefficient":
        if check.which is None:
            raise ValueError(f"{check.id}: a coefficient question needs `which`")
        if a < 0:                    # rearrange so the x² term is positive, as NCERT does
            a, b, c = -a, -b, -c
        return AnswerKey(kind=kind, values=[{"a": a, "b": b, "c": c}[check.which]])
    d = b**2 - 4 * a * c             # D of the equation as written
    if kind == "discriminant":
        return AnswerKey(kind=kind, values=[d])
    if kind == "nature":             # NCERT's three cases
        return AnswerKey(kind=kind, word="distinct" if d > 0 else "equal" if d == 0 else "none")
    if kind == "has_real_roots":
        return AnswerKey(kind=kind, word="yes" if d >= 0 else "no")
    if kind == "find_k":             # the k that make D zero, but not one that makes a zero: then it's no quadratic
        all_values = _real_roots(expand(d), k)
        values = [value for value in all_values if simplify(a.subs(k, value)) != 0]     # by value: 1 ± √2 too
        if not values:
            raise ValueError(f"{check.id}: no value of k gives two equal roots")
        return AnswerKey(kind=kind, values=values, all_roots=all_values)
    raise ValueError(f"{check.id}: unknown kind {kind!r}")


def key_text(check: Check) -> str:
    """The key as words for a scripted line: "x = 2 or x = 3", "k = 4", "b = -7", "No real roots"."""
    key = answer_key(check)
    if check.kind in ("roots", "find_k"):
        letter = "k" if check.kind == "find_k" else "x"
        return " or ".join(f"{letter} = {_show(value)}" for value in key.values) or "No real roots"
    if check.kind == "coefficient":
        return f"{check.which} = {_show(key.values[0])}"
    if check.kind == "discriminant":
        return f"D = {_show(key.values[0])}"
    if check.kind == "is_root":
        value = _show(_candidate(check))
        return f"Yes, x = {value} is a root" if key.word == "yes" else f"No, x = {value} is not a root"
    return KEY_WORDS[check.kind][key.word]


def key_speech(check: Check) -> str:
    """A word answer as the tutor says it (key_text is for the board): "Yes, minus 2 is a root"."""
    if check.kind == "is_root":
        value = say_number(_candidate(check))
        return f"Yes, {value} is a root" if answer_key(check).word == "yes" else f"No, {value} is not a root"
    return KEY_WORDS[check.kind][answer_key(check).word]


def say_number(value) -> str:
    """A number in words for speech: -2 -> "minus 2", 3/2 -> "3 over 2", 2 - sqrt(3) -> "2 minus root 3".
    A voice reads the "-" of "-2" as a dash, so "x = -2" sounds like "x equals 2"."""
    text = re.sub(r"sqrt\((\d+)\)", r"root \1", _show(value)).replace("*", " ")       # 2*sqrt(3) -> "2 root 3"
    text = re.sub(r"(\d+)/(\d+)", r"\1 over \2", text).replace(" + ", " plus ").replace(" - ", " minus ")
    text = re.sub(r"^-\s*", "minus ", text)
    if re.search(r"[*/()]|sqrt", text):
        raise ValueError(f"{value} can't be said in words")
    return text


def _left_minus_right(equation):
    """The check's equation L = R as the expanded polynomial L - R."""
    parsed = parse_check(equation)
    if not isinstance(parsed, Eq):
        raise ValueError(f"not an equation: {equation}")
    expr = expand((parsed.lhs - parsed.rhs).doit())
    if not expr.is_polynomial(x):
        raise ValueError(f"not a polynomial equation: {equation}")
    return expr


def _real_roots(expr, letter=x):
    """Every real root of expr = 0, smallest first."""
    roots = solveset(expr, letter, S.Reals)
    if not roots.is_finite_set:
        raise ValueError(f"{expr} = 0 doesn't have a list of roots")
    return sorted(roots, key=float)


def _abc(expr):
    """a, b and c of a quadratic, as written."""
    poly = Poly(expr, x)
    if poly.degree() != 2:
        raise ValueError(f"{expr} = 0 is not a quadratic equation")
    return [poly.coeff_monomial(x**power) for power in (2, 1, 0)]


def _candidate(check):
    """The number an is_root question puts in for x."""
    values = parse_answer(check.candidate or "", "roots")
    if values is None or len(values) != 1:
        raise ValueError(f"{check.id}: an is_root question needs one candidate number")
    return values[0]


def _show(value):
    """A number as plain text, number first: "2 + sqrt(3)", "-1/3"."""
    return sstr(value, order="none")


# ---------- grading ----------

def grade(check: Check, answer: str) -> Grade:
    """Grade one answer: correct, wrong, partial or unsure, plus the known mistake (trap) it matches."""
    key = answer_key(check)
    student = answer.strip()
    given = parse_answer(answer, check.kind)
    if given is None:
        return Grade(verdict="unsure", student=student)
    if check.kind in WORD_KINDS:
        verdict, detail = ("correct" if given == key.word else "wrong"), ""
    elif check.kind in ("roots", "find_k"):
        verdict, detail = _grade_roots(given, key)
    else:
        verdict, detail = _grade_one_number(given, key)
    trap = _matching_trap(check, given) if verdict in ("wrong", "partial") else None
    return Grade(verdict=verdict, student=student, detail=detail, trap=trap,
                 wrong_values=(detail == "not_a_root"))


def _grade_roots(given, key):
    """A roots (or find_k) answer, compared with the key as sets of values. Returns (verdict, detail)."""
    if _same_set(given, key.values):
        return "correct", ""
    if not given:
        return "wrong", "empty"                # said "none", but real roots exist
    if not all(_contains(key.all_roots, value) for value in given):
        return "wrong", "not_a_root"
    if all(_contains(key.values, value) for value in given):
        return "partial", "missing"            # every value is right, but one is missing
    return "partial", "extra"                  # kept a root the question rules out


def _grade_one_number(given, key):
    """Coefficient and discriminant questions: exactly one number, compared by value."""
    if len(given) != 1:
        return "unsure", ""                    # "none", or several numbers: ask for just the number
    return ("correct" if same_value(given[0], key.values[0]) else "wrong"), ""


def _matching_trap(check, given):
    """The mistake whose trap answer equals the student's answer, by value."""
    if check.kind in WORD_KINDS:
        return None                            # traps are numbers; word answers have none
    for mistake, trap_text in check.traps.items():
        trap = parse_answer(trap_text, check.kind)
        if trap is not None and _same_set(given, trap):
            return mistake
    return None


def _same_set(a, b):
    return all(_contains(b, value) for value in a) and all(_contains(a, value) for value in b)


def _contains(values, value):
    return any(same_value(value, other) for other in values)
