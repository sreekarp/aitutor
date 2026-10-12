"""Tests for app/grading.py: answer keys, answer parsing and grading."""
import time

import pytest
from sympy import Rational, sqrt

from app import grading
from app.mathcheck import CheckError
from app.models import WORD_KINDS, Check


def make(check_id, kind, equation, **fields):
    """A Check with placeholder words: only the maths matters in these tests."""
    return Check(id=check_id, say="(question)", label="(label)", equation=equation, kind=kind,
                 hints=["(hint 1)", "(hint 2)", "(hint 3)"], **fields)


def same_values(got, expected):
    """Same numbers in the same order, compared by value."""
    return len(got) == len(expected) and all(grading.same_value(a, b) for a, b in zip(got, expected))


def numbers(text):
    return grading.parse_answer(text, "roots")


# ---------- reading answers ----------

def test_by_value():
    pairs = [("2/6", Rational(1, 3)), ("sqrt(2/3)", sqrt(6) / 3), ("0.5", Rational(1, 2)),
             ("2/(-6)", Rational(-1, 3)), ("(4 + sqrt(12))/2", 2 + sqrt(3)), ("-12.5", Rational(-25, 2)),
             ("1/(2 - sqrt(3))", 2 + sqrt(3))]          # == says no to this one; simplify says yes
    for text, value in pairs:
        [number] = numbers(text)
        assert grading.same_value(number, value), text
    assert not grading.same_value(numbers("0.333")[0], Rational(1, 3))   # close is not equal


def test_parse_answer_odd_formats():
    # Review Focus 1: each of these is read correctly...
    readable = {
        "x=2,x=3": [2, 3], "x = 2 or x = 3": [2, 3], "2 & 3": [2, 3], "2 and 3.": [2, 3],
        "2; 3": [2, 3], "  2, 3   ": [2, 3], "2, 3,": [2, 3], "X = 5": [5],
        "\u22122": [-2], "3 × 2": [6], "5^2": [25],          # a unicode minus sign, ×, ^
        "√2": [sqrt(2)], "2√3": [2 * sqrt(3)], "√(2/3)": [sqrt(6) / 3], "2, √3.": [2, sqrt(3)],
        "1 ± √2": [1 + sqrt(2), 1 - sqrt(2)],
        "x = 2 +/- sqrt(3)": [2 + sqrt(3), 2 - sqrt(3)],
        "2 +- sqrt(3)": [2 + sqrt(3), 2 - sqrt(3)],
        "none": [], "No real roots.": [], "no solution": [], "x = none": [],
    }
    for text, expected in readable.items():
        got = numbers(text)
        assert got is not None and same_values(got, expected), text
    # ...and each of these can't be read (None, so it grades "unsure"): never a crash or a hang.
    unreadable = ["", "   ", ",", "banana", "2x = 6", "x", "x**2", "sqrt(-1)", "1/0", "0/0", "9**9**9",
                  "2**100", "9^9^9", "__import__('os')", "Eq(x, 2)", "1" * 61, ",".join(["1"] * 11),
                  "½", "हाँ", "😀"]
    for text in unreadable:
        start = time.perf_counter()
        assert numbers(text) is None, text
        assert time.perf_counter() - start < 1, text


def test_word_kinds_and_synonyms():
    for kind in ("is_quadratic", "is_root", "has_real_roots"):
        for text in ("yes", "Yes.", "haan", "Han", "yeah"):
            assert grading.parse_answer(text, kind) == "yes", (kind, text)
        for text in ("no", "nahi", "nahin", "not possible", "Not possible!"):
            assert grading.parse_answer(text, kind) == "no", (kind, text)
    nature = {
        "distinct": ["distinct", "different", "two distinct real roots", "Two distinct real roots."],
        "equal": ["equal", "same", "repeated", "coincident", "two equal real roots"],
        "none": ["none", "no real roots", "no solution", "not real", "imaginary"],
    }
    for word, spellings in nature.items():
        for text in spellings:
            assert grading.parse_answer(text, "nature") == word, text
    # A word that doesn't fit the question can't be read, so it grades "unsure", never "wrong".
    assert grading.parse_answer("yes", "nature") is None
    assert grading.parse_answer("equal", "is_root") is None
    assert grading.parse_answer("maybe", "is_quadratic") is None
    assert grading.parse_answer("", "nature") is None


# ---------- answer keys: every check in the lesson files, in the order asked ----------

EXPECTED_KEYS = [
    # 1. standard_form
    (make("sf1", "is_quadratic", "Eq((x + 3)*(x - 3), x**2 + 2*x)"), "no"),
    (make("sf2", "coefficient", "Eq(5 + 2*x**2, 7*x)", which="b", traps={"sign_when_moving": "7"}), [-7]),
    (make("sf3", "is_quadratic", "Eq((x - 1)**3, x**3 + x)"), "yes"),
    (make("sf4", "coefficient", "Eq(x*(x - 3), 10)", which="c", traps={"sign_when_moving": "10"}), [-10]),
    (make("sf5", "is_quadratic", "Eq((x + 1)**2, 3*(x - 2))"), "yes"),
    (make("sf6", "is_quadratic", "Eq(x*(2*x - 1), 2*x**2 + 3)"), "no"),
    # 2. roots
    (make("rt1", "is_root", "Eq(x**2 + x - 2, 0)", candidate="-2"), "yes"),
    (make("rt2", "is_root", "Eq(x**2 - 3*x + 2, 0)", candidate="-1"), "no"),
    (make("rt3", "is_root", "Eq(2*x**2 - x - 3, 0)", candidate="3/2"), "yes"),
    (make("rt4", "is_root", "Eq(x**2 + x + 6, 0)", candidate="-2"), "no"),
    (make("rt5", "is_root", "Eq(x**2 + 3*x - 10, 0)", candidate="-5"), "yes"),
    # 3. factorisation
    (make("fa1", "roots", "Eq(x**2 - 5*x + 6, 0)", traps={"sign_flip": "-2, -3"}), [2, 3]),
    (make("fa2", "roots", "Eq(3*x**2 - 5*x - 2, 0)", traps={"sign_flip": "1/3, -2"}), [Rational(-1, 3), 2]),
    (make("fa3", "roots", "Eq((x - 2)*(x + 3), 0)", traps={"sign_flip": "-2, 3"}), [-3, 2]),
    (make("fa4", "roots", "Eq(x**2 + x - 12, 0)", traps={"sign_flip": "4, -3"}), [-4, 3]),
    (make("fa5", "roots", "Eq(x**2 - 5*x, 14)", traps={"not_zero_first": "14, 19"}), [-2, 7]),
    (make("fa6", "roots", "Eq(x**2, 3*x)", traps={"divided_by_x": "3"}), [0, 3]),
    (make("fa7", "roots", "Eq(2*x**2 - 7*x + 3, 0)", traps={"sign_flip": "-1/2, -3"}), [Rational(1, 2), 3]),
    # 4. quadratic_formula
    (make("qf1", "roots", "Eq(2*x**2 - 3*x - 2, 0)",
          traps={"minus_b": "1/2, -2", "divide_by_2_not_2a": "4, -1", "sign_in_D": "none"}), [Rational(-1, 2), 2]),
    (make("qf2", "roots", "Eq(x**2 - 4*x + 1, 0)", traps={"minus_b": "-2 + sqrt(3), -2 - sqrt(3)"}),
     [2 - sqrt(3), 2 + sqrt(3)]),
    (make("qf3", "roots", "Eq(x**2 + 2*x - 15, 0)", traps={"minus_b": "-3, 5"}), [-5, 3]),
    (make("qf4", "roots", "Eq(x**2 - 7*x + 10, 0)", traps={"minus_b": "-2, -5"}), [2, 5]),
    (make("qf5", "roots", "Eq(3*x**2 + 5*x - 2, 0)", traps={"minus_b": "2, -1/3", "sign_in_D": "-2/3, -1"}),
     [-2, Rational(1, 3)]),
    # 5. discriminant
    (make("di1", "discriminant", "Eq(x**2 + 3*x - 4, 0)", traps={"sign_in_D": "-7"}), [25]),
    (make("di2", "nature", "Eq(4*x**2 - 12*x + 9, 0)"), "equal"),
    (make("di3", "nature", "Eq(3*x**2 - 2*x + 4, 0)"), "none"),
    (make("di4", "discriminant", "Eq(x**2 + 4*x + 5, 0)", traps={"sign_in_D": "36"}), [-4]),
    (make("di5", "nature", "Eq(x**2 - 5*x + 3, 0)"), "distinct"),
    (make("di6", "roots", "Eq(9*x**2 - 6*x + 1, 0)", traps={"mixed_cases": "none"}), [Rational(1, 3)]),
    # 6. word_problems: the board shows only the story (show_equation False); one positive quantity asked
    (make("wp1", "roots", "Eq(x*(x + 3), 40)", keep="positive", show_equation=False,
          traps={"kept_impossible_root": "5, -8"}), [5]),
    (make("wp2", "has_real_roots", "Eq(x*(8 - x), 20)", show_equation=False), "no"),
    (make("wp3", "roots", "Eq(x*(x + 1), 132)", keep="positive", show_equation=False,
          traps={"kept_impossible_root": "11, -12"}), [11]),
    (make("wp4", "roots", "Eq(x*(x - 3), 28)", keep="positive", show_equation=False,
          traps={"kept_impossible_root": "7, -4"}), [7]),
    (make("wp5", "roots", "Eq(x**2 + (x + 2)**2, 100)", keep="positive", show_equation=False,
          traps={"kept_impossible_root": "6, -8"}), [6]),
]
CHECKS = {check.id: check for check, _ in EXPECTED_KEYS}
ALL_ROOTS = {"wp1": [-8, 5], "wp3": [-12, 11], "wp4": [-4, 7], "wp5": [-8, 6]}   # before keep: positive


def test_answer_keys_all_34_checks():
    assert len(CHECKS) == 34
    for check, expected in EXPECTED_KEYS:
        key = grading.answer_key(check)
        assert key.kind == check.kind, check.id
        if check.kind in WORD_KINDS:
            assert key.word == expected and key.values is None, check.id
        else:
            assert same_values(key.values, expected), check.id
        if check.kind == "roots":
            assert same_values(key.all_roots, ALL_ROOTS.get(check.id, expected)), check.id


def test_coefficient_positive_x_squared():
    assert same_values(grading.answer_key(CHECKS["sf2"]).values, [-7])   # 5 + 2x² = 7x -> 2x² - 7x + 5 = 0
    rearranged = {
        ("Eq(-3*x**2 + 4*x + 2, 0)", "b"): -4,     # times -1: 3x² - 4x - 2 = 0
        ("Eq(4*x - 3*x**2 + 2, 0)", "a"): 3,
        ("Eq(10, x*(x - 3))", "c"): -10,            # the x² term starts on the right
    }
    for (equation, which), value in rearranged.items():
        key = grading.answer_key(make("co", "coefficient", equation, which=which))
        assert same_values(key.values, [value]), equation


def test_discriminant_as_written():
    cases = {
        "Eq(x**2 + 3*x - 4, 0)": 25,
        "Eq(2*x**2 + 6*x - 8, 0)": 100,     # as written: not divided by 2 first (that would give 25)
        "Eq(-x**2 - 3*x + 4, 0)": 25,       # multiplying through by -1 doesn't change D
        "Eq(x**2 + 4*x + 5, 0)": -4,
    }
    for equation, d in cases.items():
        assert same_values(grading.answer_key(make("d", "discriminant", equation)).values, [d]), equation


def test_answer_key_refuses_broken_maths():
    broken = [
        make("b1", "roots", "x**2 - 1"),                     # not an equation
        make("b2", "roots", "Eq(x, x)"),                     # every x is a root
        make("b3", "roots", "Eq(1/x, 2)"),                   # not a polynomial
        make("b4", "discriminant", "Eq(x**3, 1)"),           # not a quadratic
        make("b5", "coefficient", "Eq(2*x, 1)", which="a"),  # not a quadratic
        make("b6", "coefficient", "Eq(x**2, 1)"),            # no `which`
        make("b7", "is_root", "Eq(x**2, 1)"),                # no candidate
    ]
    for check in broken:
        with pytest.raises(ValueError):
            grading.answer_key(check)
    with pytest.raises(CheckError):
        grading.answer_key(make("b8", "roots", "__import__('os')"))


def test_key_text():
    expected = {
        "fa1": "x = 2 or x = 3", "fa2": "x = -1/3 or x = 2", "di6": "x = 1/3", "wp1": "x = 5",
        "qf2": "x = 2 - sqrt(3) or x = 2 + sqrt(3)",
        "sf2": "b = -7", "sf4": "c = -10", "di1": "D = 25", "di4": "D = -4",
        "di2": "Two equal real roots", "di3": "No real roots", "di5": "Two distinct real roots",
        "rt1": "Yes, x = -2 is a root", "rt2": "No, x = -1 is not a root", "rt3": "Yes, x = 3/2 is a root",
        "sf1": "No, it is not a quadratic equation", "sf3": "Yes, it is a quadratic equation",
        "wp2": "No, it is not possible: the equation has no real roots",
    }
    for check_id, text in expected.items():
        assert grading.key_text(CHECKS[check_id]) == text, check_id
    assert grading.key_text(make("n", "roots", "Eq(x**2 + x + 1, 0)")) == "No real roots"
    for check, _ in EXPECTED_KEYS:                     # it may be spoken, so no LaTeX characters
        assert not set(grading.key_text(check)) & set(r"\^{}$_"), check.id


def test_key_speech_says_numbers_in_words():
    # key_text is for the board ("x = -2"); a voice reads "-2" as "2", so speech says "minus 2"
    expected = {"rt1": "Yes, minus 2 is a root", "rt2": "No, minus 1 is not a root",
                "rt3": "Yes, 3 over 2 is a root", "di2": "Two equal real roots",
                "sf1": "No, it is not a quadratic equation",
                "wp2": "No, it is not possible: the equation has no real roots"}
    for check_id, text in expected.items():
        assert grading.key_speech(CHECKS[check_id]) == text, check_id
    assert grading.say_number(Rational(-1, 3)) == "minus 1 over 3"
    assert grading.say_number(2 - sqrt(3)) == "2 minus root 3"
    assert grading.say_number(2 * sqrt(3)) == "2 root 3"             # no "*" read aloud
    with pytest.raises(ValueError):                                 # can't be said in words: refused, not mangled
        grading.say_number(sqrt(2 + sqrt(3)))


# ---------- grading ----------

def test_verdict_table():
    x2_5x_6 = make("v1", "roots", "Eq(x**2 - 5*x, 6)")      # roots 6 and -1
    no_real = make("v2", "roots", "Eq(x**2 + x + 1, 0)")     # no real roots
    table = [
        # (check, answer, verdict, detail)
        (CHECKS["fa1"], "2, 3", "correct", ""),
        (CHECKS["fa1"], "3 and 2", "correct", ""),
        (CHECKS["fa2"], "-2/6, 2", "correct", ""),            # compared by value
        (CHECKS["di6"], "1/3, 1/3", "correct", ""),           # the equal root said twice
        (no_real, "none", "correct", ""),
        (CHECKS["fa1"], "none", "wrong", "empty"),            # "none", but real roots exist
        (x2_5x_6, "6, 11", "wrong", "not_a_root"),
        (no_real, "1", "wrong", "not_a_root"),
        (CHECKS["fa1"], "2", "partial", "missing"),
        (CHECKS["fa6"], "3", "partial", "missing"),           # x² = 3x divided by x loses the root 0
        (CHECKS["wp4"], "7, -4", "partial", "extra"),         # kept the impossible age -4
        (CHECKS["wp4"], "-4", "partial", "extra"),
        (CHECKS["wp4"], "7", "correct", ""),
        (CHECKS["sf2"], "-7", "correct", ""),
        (CHECKS["sf2"], "7", "wrong", ""),
        (CHECKS["sf2"], "-7, 7", "unsure", ""),               # two numbers for one coefficient
        (CHECKS["di1"], "none", "unsure", ""),                # D is always a number
        (CHECKS["rt1"], "yes", "correct", ""),
        (CHECKS["rt1"], "nahi", "wrong", ""),
        (CHECKS["di3"], "no real roots", "correct", ""),
        (CHECKS["di3"], "equal", "wrong", ""),
        (CHECKS["fa1"], "banana", "unsure", ""),
    ]
    for check, answer, verdict, detail in table:
        result = grading.grade(check, answer)
        assert (result.verdict, result.detail) == (verdict, detail), (check.id, answer)
        assert result.student == answer
    assert grading.grade(x2_5x_6, "6, 11").wrong_values is True        # 11 isn't a root: substitute it back
    assert grading.grade(CHECKS["wp4"], "7, -4").wrong_values is False   # -4 is a root, just not an age


def test_traps_match_by_value():
    qf1 = CHECKS["qf1"]                                                 # 2x² - 3x - 2 = 0: roots 2 and -1/2
    assert grading.grade(qf1, "0.5, -2").trap == "minus_b"            # trap "1/2, -2"
    assert grading.grade(qf1, "-2, 1/2").trap == "minus_b"            # in any order
    assert grading.grade(qf1, "4 and -1").trap == "divide_by_2_not_2a"
    assert grading.grade(qf1, "no real roots").trap == "sign_in_D"    # trap "none"
    assert grading.grade(qf1, "1/2, -3").trap is None                 # wrong, but no known mistake
    assert grading.grade(qf1, "2, -0.5").trap is None                 # a right answer never has a trap
    assert grading.grade(CHECKS["qf2"], "\u22122 ± √3").trap == "minus_b"   # trap "-2 + sqrt(3), -2 - sqrt(3)"
    assert grading.grade(CHECKS["fa2"], "2/6, -2").trap == "sign_flip"      # trap "1/3, -2"
    assert grading.grade(CHECKS["sf2"], "7.0").trap == "sign_when_moving"   # trap "7"


def test_every_spec_trap_is_caught_and_never_correct():
    for check, _ in EXPECTED_KEYS:
        for mistake, trap in check.traps.items():
            result = grading.grade(check, trap)
            assert result.verdict in ("wrong", "partial"), (check.id, trap)
            assert result.trap == mistake, (check.id, trap)


def test_unreadable_is_unsure():
    garbage = ["", "   ", "banana", "2x = 6", "x", "sqrt(-1)", "1/0", "9**9**9", "__import__('os')",
               "1" * 61, "हाँ", "😀", "x" * 2000]
    for check in (CHECKS["fa1"], CHECKS["sf2"], CHECKS["di1"]):
        for answer in garbage:
            result = grading.grade(check, answer)
            assert (result.verdict, result.detail, result.trap) == ("unsure", "", None), (check.id, answer)
    for check, answer in [(CHECKS["rt1"], "maybe"), (CHECKS["rt1"], "equal"), (CHECKS["di2"], "yes"), (CHECKS["wp2"], "")]:
        assert grading.grade(check, answer).verdict == "unsure", (check.id, answer)
