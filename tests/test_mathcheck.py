"""mathcheck: your equation_check.py prototype, hardened. Every board line with numbers goes through it."""
import time

import pytest
from sympy import Eq, Or, Rational, simplify, sqrt

from app import mathcheck
from app.mathcheck import CheckError

x = mathcheck.x


# ---------- reading maths safely ----------

def test_question_latex_reads_as_written():
    assert mathcheck.question_latex("Eq((x-2)*(x+3), 0)") == "(x-2)(x+3)=0"
    assert mathcheck.question_latex("Eq(5 + 2*x**2, 7*x)") == "5+2x^{2}=7x"
    assert mathcheck.question_latex("Eq(x*(x-3), 10)") == "x(x-3)=10"
    assert mathcheck.question_latex("Eq((x+1)**2, 3*(x-2))") == "(x+1)^{2}=3(x-2)"


def test_parse_check_keeps_equations_exact():
    false_sum = mathcheck.parse_check("Eq(12.5*2, 24)")
    assert isinstance(false_sum, Eq)                    # not collapsed to False
    assert simplify(false_sum.lhs) == 25                # 12.5 was read as exactly 25/2
    claim = mathcheck.parse_check("Or(Eq(x, 12), Eq(x, -12.5))")
    assert {simplify(part.rhs) for part in claim.args} == {12, Rational(-25, 2)}
    for bad in ["x = 2", "x^2", "__import__('os')", "1, 2", "Eq(x, 9**9**9)",
                "Eq(((x**10)**10)**10, 1)", "Eq(" + "*".join(["x"] * 30) + ", 1)"]:
        with pytest.raises(CheckError):
            mathcheck.parse_check(bad)


def test_parse_display_reads_board_latex():
    shown = mathcheck.parse_display("x(x+1) + 8 = (x+2)(x-2)")
    assert simplify(shown.lhs - shown.rhs - (x + 12)) == 0     # x(x+1) means x times (x+1)
    claim = mathcheck.parse_display(r"x = 12 \text{ or } x = -12.5")
    assert isinstance(claim, Or)
    assert {simplify(part.rhs) for part in claim.args} == {12, Rational(-25, 2)}
    plus_minus = mathcheck.parse_display(r"x = 1 \pm \sqrt{2}")
    assert {simplify(part.rhs) for part in plus_minus.args} == {1 + sqrt(2), 1 - sqrt(2)}
    assert isinstance(mathcheck.parse_display("(-2)^2 = -4"), Eq)    # kept as an equation, not False
    for unreadable in [r"x = 2 \text{ (a double root)}", "x^2 + y = 0", "1 < 2", "x = 1 = 2",
                       r"x = 9^{9^{9}}", "x = 10000000!", r"\sin x = 0", ""]:
        assert mathcheck.parse_display(unreadable) is None, unreadable


# ---------- checking lines and steps ----------

def L(latex, check, relation="equiv", id="l1"):
    """One board line, the way board.py and the lesson files hand it to mathcheck."""
    return {"id": id, "latex": latex, "check": check, "relation": relation}


def report(lines, anchor=None):
    """(status, note) for each line check_steps looked at."""
    return [(status, note) for _, status, _, note in mathcheck.check_steps(lines, anchor)]


def statuses(lines, anchor=None):
    return [status for status, _ in report(lines, anchor)]


# Your prototype's seven cases, now with the hardened checker's exact messages.
CASE_A = [L(r"x(x+1) + 8 = (x+2)(x-2)", "Eq(x*(x+1) + 8, (x+2)*(x-2))"),
          L(r"x^2 + x + 8 = x^2 - 4", "Eq(x**2 + x + 8, x**2 - 4)"),
          L(r"x + 12 = 0", "Eq(x + 12, 0)")]
CASE_B = [L(r"x^2 + x + 8 = x^2 - 4", "Eq(x**2 + x + 8, x**2 - 4)"),
          L(r"x + 4 = 0", "Eq(x + 4, 0)")]
CASE_C = [L(r"2x^2 + 14x - 120 = 0", "Eq(2*x**2 + 14*x - 120, 0)"),
          L(r"x^2 + 7x - 60 = 0", "Eq(x**2 + 7*x - 60, 0)"),
          L(r"(x+12)(x-5) = 0", "Eq((x + 12)*(x - 5), 0)"),
          L(r"x = 5 \text{ or } x = -12", "Or(Eq(x, 5), Eq(x, -12))"),
          L(r"x = 5", "Eq(x, 5)", "select")]
CASE_D = [L(r"x^2 = 3x", "Eq(x**2, 3*x)"),
          L(r"x = 3", "Eq(x, 3)")]
CASE_E = [L(r"2(1)^2 - 3(1) + 1 = 0", "Eq(2*1**2 - 3*1 + 1, 0)", "new"),
          L(r"(-4)^2 - 4 \cdot 2 \cdot 3 = -8", "Eq((-4)**2 - 4*2*3, -8)", "new"),
          L(r"(-2)^2 = -4", "Eq((-2)**2, -4)", "new")]
CASE_F = [L(r"x^2 + x + 8 = x^2 - 4", "Eq(x**2 + x + 8, x**2 - 4)"),
          L(r"x + 13 = 0", "Eq(x + 12, 0)")]
CASE_G = [L(r"x = 1", "__import__('os').system('ls')")]


def test_prototype_case_a_passes():
    """A. NCERT Ex 2(ii): looks quadratic, isn't."""
    assert report(CASE_A) == [("ok", ""), ("ok", ""), ("ok", "")]


def test_prototype_case_b_catches_the_slip():
    """B. The same, with an arithmetic slip."""
    assert report(CASE_B) == [
        ("ok", ""), ("FAIL", "not equivalent to the previous line: loses {-12}, adds {-4}")]


def test_prototype_case_c_passes():
    """C. NCERT Ex 8: scale, factorise, reject the negative root."""
    assert report(CASE_C) == [("ok", "")] * 5


def test_prototype_case_d_catches_dividing_by_x():
    """D. Dividing by x (the misconception, written by the tutor) loses the root 0."""
    assert report(CASE_D) == [("ok", ""), ("FAIL", "not equivalent to the previous line: loses {0}")]


def test_prototype_case_e_catches_false_arithmetic():
    """E. Substitution and discriminant arithmetic: true lines pass, (-2)^2 = -4 doesn't."""
    assert report(CASE_E) == [("ok", "arithmetic"), ("ok", "arithmetic"),
                              ("FAIL", "arithmetic is false: left side is 4, not -4")]


def test_prototype_case_f_catches_wrong_display():
    """F. The board shows x + 13, but the check says x + 12."""
    assert report(CASE_F) == [
        ("ok", ""), ("FAIL", "LaTeX shows Eq(x + 13, 0), but the checked form is Eq(x + 12, 0)")]


def test_prototype_case_g_rejects_code():
    """G. Code in the check field is never run."""
    assert report(CASE_G) == [("FAIL", "name not allowed: __import__")]


def test_self_test_catches_wrong_antlr(monkeypatch):
    mathcheck.self_test()                                       # the real parser passes
    monkeypatch.setattr(mathcheck, "parse_display", lambda latex: None)   # what a wrong antlr looks like
    with pytest.raises(RuntimeError):
        mathcheck.self_test()


def test_unreadable_display_fails():
    """Strict display: SymPy must be able to read what the student sees, even when the check is fine."""
    [(status, note)] = report([L(r"x = 2 \text{ (a double root)}", "Eq(x, 2)")])
    assert status == "FAIL" and note.startswith("can't read the LaTeX")


def test_arithmetic_display_side_by_side():
    substitution = "Eq((-2-2)*(-2+3), -4)"
    assert statuses([L("(-2-2)(-2+3) = -4", substitution)]) == ["ok"]
    assert statuses([L("(-2-2)(-2+3) = -6", substitution)]) == ["FAIL"]
    assert statuses([L("(-3-2)(-3+3) = 0", substitution)]) == ["FAIL"]    # true, but not the checked line
    assert statuses([L("1 + 1 = 2", "Eq((-4)**2 - 4*2*3, -8)")]) == ["FAIL"]
    assert statuses([L("0.1 + 0.2 = 0.3", "Eq(0.1 + 0.2, 0.3)")]) == ["ok"]  # decimals are exact


def test_root_claim_display_compared():
    check = "Or(Eq(x, 5), Eq(x, -12))"
    assert statuses([L(r"x = 5 \text{ or } x = -12", check)]) == ["ok"]
    assert statuses([L(r"x = -12 \text{ or } x = 5", check)]) == ["ok"]     # order doesn't matter
    assert statuses([L(r"x = -5 \text{ or } x = 12", check)]) == ["FAIL"]   # swapped signs


def test_root_claim_fraction_and_decimal():
    a_not_one = [L("2x^2 - 5x + 3 = 0", "Eq(2*x**2 - 5*x + 3, 0)"),
                 L("(2x-3)(x-1) = 0", "Eq((2*x-3)*(x-1), 0)"),
                 L(r"x = \frac{3}{2} \text{ or } x = 1", "Or(Eq(x, 3/2), Eq(x, 1))")]
    assert statuses(a_not_one) == ["ok"] * 3
    prayer_hall = [L("2x^2 + x - 300 = 0", "Eq(2*x**2 + x - 300, 0)"),
                   L("(x-12)(2x+25) = 0", "Eq((x-12)*(2*x+25), 0)"),
                   L(r"x = 12 \text{ or } x = -12.5", "Or(Eq(x, 12), Eq(x, -12.5))"),
                   L("x = 12", "Eq(x, 12)", "select")]
    assert statuses(prayer_hall) == ["ok"] * 4
    assert statuses([L(r"x = 12 \text{ or } x = -12.5", "Or(Eq(x, 12), Eq(x, -25/2))")]) == ["ok"]
    assert statuses([L(r"x = 0.4 \text{ or } x = 3", "Or(Eq(x, 1/2), Eq(x, 3))")]) == ["FAIL"]
    assert report([L(r"12.5 \cdot 2 = 24", "Eq(12.5*2, 24)")]) == [
        ("FAIL", "arithmetic is false: left side is 25, not 24")]


def test_expression_after_equation():
    q1 = L("x^2 - 5x + 6 = 0", "Eq(x**2 - 5*x + 6, 0)")
    q5 = L("x^2 - 5x = 14", "Eq(x**2 - 5*x, 14)")
    assert statuses([q1, L("(x-2)(x-3)", "(x-2)*(x-3)")]) == ["ok", "ok"]
    assert statuses([q1, L("(x-2)(x+3)", "(x-2)*(x+3)")]) == ["ok", "FAIL"]
    assert statuses([q5, L("x^2 - 5x - 14", "x**2 - 5*x - 14")]) == ["ok", "ok"]
    assert statuses([q5, L("-x^2 + 5x + 14", "-x**2 + 5*x + 14")]) == ["ok", "FAIL"]  # the negation
    discriminant = [L("x^2 + 3x - 4 = 0", "Eq(x**2 + 3*x - 4, 0)"),
                    L(r"3^2 - 4 \cdot 1 \cdot (-4)", "3**2 - 4*1*(-4)")]
    assert report(discriminant) == [("ok", ""), ("ok", "arithmetic")]          # no x: never chained


def test_equation_after_expression():
    q1 = L("x^2 - 5x + 6 = 0", "Eq(x**2 - 5*x + 6, 0)")
    hint = [q1, L("(x-2)(x-3)", "(x-2)*(x-3)"), L("(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)"),
            L(r"x = 2 \text{ or } x = 3", "Or(Eq(x, 2), Eq(x, 3))")]
    assert statuses(hint) == ["ok"] * 4
    split = [L("x^2 - 7x + 12", "x**2 - 7*x + 12"), L("x^2 - 3x - 4x + 12", "x**2 - 3*x - 4*x + 12"),
             L("x(x-3) - 4(x-3)", "x*(x-3) - 4*(x-3)"), L("(x-3)(x-4)", "(x-3)*(x-4)"),
             L("(x-3)(x-4) = 0", "Eq((x-3)*(x-4), 0)")]
    assert statuses(split) == ["ok"] * 5
    wrong = [L("(x-3)(x-4)", "(x-3)*(x-4)"), L("(x-3)(x+4) = 0", "Eq((x-3)*(x+4), 0)")]
    assert report(wrong) == [("ok", ""), ("FAIL", "not equivalent to the previous line: loses {4}, adds {-4}")]


def test_no_real_roots_steps_are_not_all_equivalent():
    question = L("(x+1)^2 = 3(x-2)", "Eq((x+1)**2, 3*(x-2))")
    assert statuses([question, L("x^2 + 1 = 3x - 6", "Eq(x**2 + 1, 3*x - 6)")]) == ["ok", "FAIL"]
    assert statuses([question, L("x^2 - x + 7 = 0", "Eq(x**2 - x + 7, 0)")]) == ["ok", "ok"]
    wrong_sign = [L("3x^2 - 2x + 4 = 0", "Eq(3*x**2 - 2*x + 4, 0)"),
                  L("3x^2 + 2x + 4 = 0", "Eq(3*x**2 + 2*x + 4, 0)")]
    assert statuses(wrong_sign) == ["ok", "FAIL"]


def test_negated_bare_expression_rejected():
    shown_negated = [L("x^2 - 7x + 12", "x**2 - 7*x + 12"), L("(3-x)(x-4)", "(x-3)*(x-4)")]
    assert statuses(shown_negated) == ["ok", "FAIL"]
    assert statuses([L("-(x-2)(x-3)", "(x-2)*(x-3)")]) == ["FAIL"]
    assert mathcheck.same_maths(mathcheck.parse_display("-(x-2)(x-3)"),
                                mathcheck.parse_check("(x-2)*(x-3)")) is False
    # Equations may still swap sides or be negated.
    assert statuses([L("0 = x^2 - 5x + 6", "Eq(x**2 - 5*x + 6, 0)")]) == ["ok"]
    assert statuses([L("-x^2 + 5x - 6 = 0", "Eq(x**2 - 5*x + 6, 0)")]) == ["ok"]


def test_anchor_catches_wrong_factorisation():
    question = "Eq(x**2 - 5*x + 6, 0)"
    wrong = [L("(x-2)(x+3) = 0", "Eq((x-2)*(x+3), 0)", id="h1")]
    assert report(wrong, anchor=question) == [
        ("FAIL", "not equivalent to the previous line: loses {3}, adds {-3}")]
    assert mathcheck.problems(wrong, anchor=question) == [
        "line h1: not equivalent to the previous line: loses {3}, adds {-3}"]
    assert mathcheck.problems([L("(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)")], anchor=question) == []
    new_example = [L("(x-2)(x+3) = 0", "Eq((x-2)*(x+3), 0)", "new")]
    assert mathcheck.problems(new_example, anchor=question) == []     # "new" isn't compared
    word_problem = "Eq(x*(x+3), 40)"                                   # never shown on the board
    assert statuses([L("x^2 + 3x + 40 = 0", "Eq(x**2 + 3*x + 40, 0)")], anchor=word_problem) == ["FAIL"]
    assert statuses([L("x^2 + 3x - 40 = 0", "Eq(x**2 + 3*x - 40, 0)"),
                     L(r"x = 5 \text{ or } x = -8", "Or(Eq(x, 5), Eq(x, -8))"),
                     L("x = 5", "Eq(x, 5)", "select")], anchor=word_problem) == ["ok"] * 3


GARBAGE_CHECKS = ["", "   ", "Eq(x)", "x == 2", "x = 2", "__import__('os')", "9**9**9", "1, 2",
                  "Eq(x, 2", "Or(Eq(x**2, 4), Eq(x, 1))", "Eq(x, sqrt(-1))", "lambda: 1", "x" * 300]


def test_garbage_checks_never_crash():
    for check in GARBAGE_CHECKS:
        start = time.perf_counter()
        found = mathcheck.problems([L(r"x = 2 \text{ or } x = -2", check, id="g1")])
        assert len(found) == 1 and found[0].startswith("line g1: "), check
        assert "internal error" not in found[0], found
        assert time.perf_counter() - start < 1, check
    start = time.perf_counter()
    found = mathcheck.problems([L(r"x = 9^{9^{9}}", "Eq(x, 1)", id="g2")])
    assert found == [r"line g2: can't read the LaTeX 'x = 9^{9^{9}}'"]
    assert time.perf_counter() - start < 1


def test_huge_powers_rejected_fast():
    tower = "((((((((9^{10})^{10})^{10})^{10})^{10})^{10})^{10})^{10})^{10}"
    huge = [L("x = 1", "Eq(x, 9**9**9)"),
            L("x = 1", "Eq(x, (((9**10)**10)**10)**10**10)"),
            L("x^{10} = 1", "Eq(((x**10)**10)**10, 1)"),
            L("x = 1", "Eq(" + "*".join(["x"] * 60) + " + x + 1, 0)"),
            L(r"x = 9^{9^{9}}", "Eq(x, 1)"),
            L(r"9^{9^{9}} = 1", "Eq(1, 1)"),
            L(r"x = 10000000!", "Eq(x, 1)"),
            L(tower + " = 1", "Eq(1, 1)"),
            L("(((x+1)^{10})^{10})^{10} = 0", "Eq((((x+2)**10)**10)**10, 0)")]
    for line in huge:
        start = time.perf_counter()
        assert mathcheck.problems([line]) != [], line
        assert time.perf_counter() - start < 1, line
