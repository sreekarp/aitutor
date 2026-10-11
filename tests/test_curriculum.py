"""Tests for app/curriculum.py and the lesson files (spec §3.1-§3.3)."""
import re

import pytest
import yaml
from conftest import fresh_progress
from sympy import Eq, Rational

from app import curriculum, grading, mathcheck
from app.models import BoardCommand, Check, Concept, Curriculum, Mistake, Segment, Step

STANDARD_FORM_LATEX = r"ax^{2} + bx + c = 0, \quad a \neq 0"


def tiny_concept() -> Concept:
    """A small valid concept. Each validation test breaks one thing in a fresh copy."""
    return Concept(
        id="tiny", title="Tiny", ncert="none",
        steps=[
            Step(id="s1", segments=[Segment(say="If two brackets multiply to zero, one of them is zero.", board=[
                BoardCommand(op="write", id="t1", latex="(x-4)(x+1) = 0", check="Eq((x-4)*(x+1), 0)"),
                BoardCommand(op="highlight", target="t1", part="(x-4)"),
                BoardCommand(op="write", id="t2", formula="standard_form"),
                BoardCommand(op="write", id="t3", text="Zero product rule"),
            ])]),
            Step(id="s2", segments=[Segment(say="Next.")]),
        ],
        checks=[
            Check(id="c1", say="Solve x squared minus 5x plus 6 equals zero.", label="Solve:",
                  equation="Eq(x**2 - 5*x + 6, 0)", kind="roots", hints=["One.", "Two.", "Three."],
                  traps={"sign_flip": "-2, -3"}),
            Check(id="c2", say="What is b?", label="What is b?", equation="Eq(2*x**2 - 7*x + 5, 0)",
                  kind="coefficient", which="b", hints=["One.", "Two.", "Three."]),
            Check(id="c3", say="A story question.", label="A story.", equation="Eq(x*(x+3), 40)",
                  kind="roots", keep="positive", show_equation=False, hints=["One.", "Two.", "Three."]),
        ],
        mistakes=[Mistake(id="sign_flip", looks_like="roots with the wrong signs", fix="Check each sign.")],
        strategies=["worked_example"],
        reteach_step="s1",
    )


def tiny_curriculum(concept: Concept) -> Curriculum:
    return Curriculum(version="test", order=["tiny"], formulas={"standard_form": STANDARD_FORM_LATEX},
                      concepts={"tiny": concept})


def board(concept: Concept) -> list[BoardCommand]:
    """The board commands of the tiny concept's first step."""
    return concept.steps[0].segments[0].board


def highlight(part: str) -> BoardCommand:
    """A highlight on the tiny concept's formula line."""
    return BoardCommand(op="highlight", target="t2", part=part)


BROKEN = [  # (what is broken, how to break it, words the problem must contain)
    ("duplicate step id", lambda c: c.steps.append(c.steps[0]), "step id s1 is used twice"),
    ("duplicate check id", lambda c: c.checks.append(c.checks[0]), "check id c1 is used twice"),
    ("duplicate mistake id", lambda c: c.mistakes.append(c.mistakes[0]), "mistake id sign_flip is used twice"),
    ("trap names no mistake", lambda c: setattr(c.checks[0], "traps", {"typo": "-2, -3"}),
     "unknown mistake typo"),
    ("two hints", lambda c: setattr(c.checks[0], "hints", ["One.", "Two."]), "exactly 3 hints"),
    ("wrong board maths", lambda c: setattr(board(c)[0], "latex", "(x-4)(x+2) = 0"), "line t1"),
    ("unreadable board maths", lambda c: setattr(board(c)[0], "latex", r"\frac{"), "line t1"),
    ("maths without a check", lambda c: setattr(board(c)[0], "check", None), "needs a check"),
    ("two kinds in one write", lambda c: setattr(board(c)[3], "formula", "standard_form"), "exactly one of"),
    ("unknown formula", lambda c: setattr(board(c)[2], "formula", "nope"), "unknown formula nope"),
    ("highlight part absent", lambda c: setattr(board(c)[1], "part", "(x-5)"), "exactly once"),
    ("highlight target unknown", lambda c: setattr(board(c)[1], "target", "zz"), "isn't written earlier"),
    ("highlight splits a power", lambda c: board(c).append(highlight("^{2}")), "split a power"),
    ("highlight cuts a command", lambda c: board(c).append(highlight("eq 0")), "cuts into a command"),
    ("highlight unbalanced", lambda c: board(c).append(highlight("2} + bx")), "unbalanced"),
    ("reteach step missing", lambda c: setattr(c, "reteach_step", "nope"), "reteach_step nope"),
    ("no worked_example", lambda c: setattr(c, "strategies", ["compare_non_example"]), "worked_example"),
    ("answer key fails", lambda c: setattr(c.checks[0], "equation", "Eq(x**2"), "answer key"),
    ("trap grades correct", lambda c: setattr(c.checks[0], "traps", {"sign_flip": "3, 2"}), "grades correct"),
    ("trap on a yes/no question", lambda c: setattr(c.checks[0], "kind", "is_quadratic"), "can't have them"),
    ("coefficients share a factor", lambda c: setattr(c.checks[1], "equation", "Eq(2*x**2 + 4*x + 6, 0)"),
     "share a factor"),
    ("LaTeX in speech", lambda c: setattr(c.steps[0].segments[0], "say", "Look at x^2."), "LaTeX characters"),
    ("LaTeX in a hint", lambda c: setattr(c.checks[0], "hints", ["One.", "Two.", r"Use \frac."]),
     "LaTeX characters"),
    ("LaTeX in a fix", lambda c: setattr(c.mistakes[0], "fix", "Watch x_1."), "LaTeX characters"),
    ("too many words", lambda c: setattr(c.checks[0], "say", "word " * 61), "over 60 words"),
    ("file id differs", lambda c: setattr(c, "id", "other"), "the file's id is other"),
]


def test_tiny_concept_is_valid():
    assert curriculum.validate(tiny_curriculum(tiny_concept())) == []


@pytest.mark.parametrize("what, breaks, expected", BROKEN, ids=[case[0] for case in BROKEN])
def test_each_validation_rule_fires(what, breaks, expected):
    concept = tiny_concept()
    breaks(concept)
    problems = curriculum.validate(tiny_curriculum(concept))
    assert any(expected in problem for problem in problems), problems


def test_next_check_id_wraps_and_handles_unknown():
    concept = tiny_concept()
    assert curriculum.next_check_id(concept, None) == "c1"
    assert curriculum.next_check_id(concept, "c1") == "c2"
    assert curriculum.next_check_id(concept, "c3") == "c1"      # after the last check, wrap round
    assert curriculum.next_check_id(concept, "gone") == "c1"    # removed from the lesson file
    assert curriculum.get_check(concept, "c2").which == "b"
    assert curriculum.get_check(concept, "gone") is None


def test_next_step_id():
    concept = tiny_concept()
    assert curriculum.next_step_id(concept, None) == "s1"
    assert curriculum.next_step_id(concept, "s1") == "s2"
    assert curriculum.next_step_id(concept, "s2") is None       # last step: time to ask a question
    assert curriculum.next_step_id(concept, "gone") == "s1"
    assert curriculum.get_step(concept, "s2").segments[0].say == "Next."
    assert curriculum.get_step(concept, None) is None


def test_ask_segment_writes_pinned_lines():
    concept = tiny_concept()
    segment = curriculum.ask_segment(concept.checks[0], {})
    assert segment.say == "Solve x squared minus 5x plus 6 equals zero."
    label, equation = segment.board
    assert (label.op, label.id, label.text) == ("write", "q-label", "Solve:")
    assert (equation.op, equation.id, equation.check) == ("write", "q-eq", "Eq(x**2 - 5*x + 6, 0)")
    assert equation.latex == mathcheck.question_latex("Eq(x**2 - 5*x + 6, 0)") == "x^{2}-5x+6=0"
    story = curriculum.ask_segment(concept.checks[2], {})      # word problem: show_equation is false
    assert [(cmd.id, cmd.text) for cmd in story.board] == [("q-label", "A story.")]


def write_lessons(folder, concept_data: dict) -> None:
    """Write an index.yaml listing one lesson, plus that lesson file."""
    index = {"version": "1", "order": ["tiny"], "formulas": {"standard_form": STANDARD_FORM_LATEX}}
    (folder / "index.yaml").write_text(yaml.safe_dump(index), encoding="utf-8")
    (folder / "tiny.yaml").write_text(yaml.safe_dump(concept_data), encoding="utf-8")


def test_load_reads_valid_files(tmp_path):
    write_lessons(tmp_path, tiny_concept().model_dump())
    cur = curriculum.load(tmp_path)
    assert cur.order == ["tiny"]
    assert cur.concepts["tiny"] == tiny_concept()


def test_load_reports_bad_fields_and_missing_files(tmp_path):
    data = tiny_concept().model_dump()
    del data["title"]
    write_lessons(tmp_path, data)
    with pytest.raises(curriculum.CurriculumError, match="title: Field required"):
        curriculum.load(tmp_path)
    (tmp_path / "index.yaml").write_text("version: '1'\norder: [ghost, phantom]\nformulas: {}\n",
                                         encoding="utf-8")
    with pytest.raises(curriculum.CurriculumError) as error:
        curriculum.load(tmp_path)
    assert "ghost.yaml" in str(error.value) and "phantom.yaml" in str(error.value)   # every problem listed


def test_load_reports_content_problems(tmp_path):
    data = tiny_concept().model_dump()
    data["checks"][0]["hints"] = ["Only one."]
    write_lessons(tmp_path, data)
    with pytest.raises(curriculum.CurriculumError, match="tiny: check c1: needs exactly 3 hints"):
        curriculum.load(tmp_path)


def test_load_stops_when_the_maths_checker_fails(monkeypatch):
    def broken_self_test():
        raise RuntimeError("case F was accepted")

    monkeypatch.setattr(mathcheck, "self_test", broken_self_test)
    with pytest.raises(curriculum.CurriculumError, match="self-test"):
        curriculum.load()


# ---------- the real lesson files ----------

def same_values(found, expected) -> bool:
    """Two lists hold the same numbers in any order, compared by value as the grader does."""
    return len(found) == len(expected) and all(
        any(grading.same_value(f, e) for f in found) for e in expected)


def assert_keys_and_traps(concept: Concept, expected: dict) -> None:
    """expected: check id -> (answer key, {mistake: (trap, verdict)}). A key is a list of numbers or a word."""
    assert [check.id for check in concept.checks] == list(expected)
    for check in concept.checks:
        key, traps = expected[check.id]
        found = grading.answer_key(check)
        if isinstance(key, str):
            assert found.word == key, check.id
        else:
            assert same_values(found.values, key), (check.id, found.values)
        assert check.traps == {mistake: trap for mistake, (trap, _) in traps.items()}, check.id
        for mistake, (trap, verdict) in traps.items():
            graded = grading.grade(check, trap)
            assert (graded.verdict, graded.trap) == (verdict, mistake), (check.id, trap)


def test_load_real_curriculum(cur):
    assert cur.version == "2026.10.1"
    assert set(cur.formulas) == {"standard_form", "quadratic_formula", "discriminant", "nature_cases"}
    concept = cur.concepts["factorisation"]
    assert concept.title == "Solving by factorisation"
    assert [step.id for step in concept.steps] == ["zero_product", "split_middle", "a_not_one", "zero_on_one_side"]
    assert sorted(m.id for m in concept.mistakes) == ["divided_by_x", "not_zero_first", "sign_flip", "wrong_split"]
    assert concept.strategies == ["worked_example", "substitute_back", "compare_non_example"]
    assert concept.reteach_step == "zero_product"
    assert curriculum.validate(cur) == []


def test_fresh_progress_starts_every_concept(cur):
    progress = fresh_progress(cur)
    assert list(progress) == cur.order
    assert all(state.status == "not_started" and state.pending is None for state in progress.values())


FACTORISATION = {   # spec §3.3 concept 3: check -> (answer key, {mistake: (trap, verdict)})
    "fa1": ([2, 3], {"sign_flip": ("-2, -3", "wrong")}),
    "fa2": ([Rational(-1, 3), 2], {"sign_flip": ("1/3, -2", "wrong")}),
    "fa3": ([2, -3], {"sign_flip": ("-2, 3", "wrong")}),
    "fa4": ([-4, 3], {"sign_flip": ("4, -3", "wrong")}),
    "fa5": ([7, -2], {"not_zero_first": ("14, 19", "wrong")}),
    "fa6": ([0, 3], {"divided_by_x": ("3", "partial")}),
    "fa7": ([Rational(1, 2), 3], {"sign_flip": ("-1/2, -3", "wrong")}),
}


def test_factorisation_keys_and_traps(cur):
    concept = cur.concepts["factorisation"]
    assert_keys_and_traps(concept, FACTORISATION)
    fa3 = curriculum.get_check(concept, "fa3")
    assert curriculum.ask_segment(fa3, cur.formulas).board[1].latex == "(x-2)(x+3)=0"   # reads as written


# ---------- concepts 1 and 2 ----------

STANDARD_FORM = {   # spec §3.3 concept 1
    "sf1": ("no", {}),
    "sf2": ([-7], {"sign_when_moving": ("7", "wrong")}),
    "sf3": ("yes", {}),
    "sf4": ([-10], {"sign_when_moving": ("10", "wrong")}),
    "sf5": ("yes", {}),
    "sf6": ("no", {}),
}


def test_standard_form_keys(cur):
    concept = cur.concepts["standard_form"]
    assert [step.id for step in concept.steps] == ["hook", "definition", "rearrange", "simplify_first"]
    assert_keys_and_traps(concept, STANDARD_FORM)
    definition = curriculum.get_step(concept, "definition")
    assert any(cmd.formula == "standard_form" for segment in definition.segments for cmd in segment.board)
    for check in concept.checks:
        if check.kind == "coefficient":                      # the convention is said out loud
            assert "with the x squared term positive" in check.say, check.id
    sign = next(mistake for mistake in concept.mistakes if mistake.id == "sign_when_moving")
    assert "change the sign of each term you move" in sign.fix
    assert concept.strategies == ["worked_example", "compare_non_example"]
    assert concept.reteach_step == "simplify_first"


ROOTS = {"rt1": ("-2", "yes"), "rt2": ("-1", "no"), "rt3": ("3/2", "yes"),   # check -> (candidate, key)
         "rt4": ("-2", "no"), "rt5": ("-5", "yes")}


def test_roots_keys_three_yes_two_no(cur):
    concept = cur.concepts["roots"]
    assert [step.id for step in concept.steps] == ["definition", "not_a_root", "negative_values", "at_most_two"]
    assert_keys_and_traps(concept, {check_id: (key, {}) for check_id, (_, key) in ROOTS.items()})
    assert [check.candidate for check in concept.checks] == [candidate for candidate, _ in ROOTS.values()]
    keys = [grading.answer_key(check).word for check in concept.checks]
    assert (keys.count("yes"), keys.count("no")) == (3, 2)
    assert sorted(mistake.id for mistake in concept.mistakes) == ["negative_square", "sign_times_negative"]
    assert concept.reteach_step == "negative_values"


SPOKEN_NUMBER = re.compile(r"(minus |-)?\b(\d+(?:\.\d+)?)(?:(?: over |/)(\d+))?(?!\w)")   # "minus 12.5", "3 over 2"


def spoken_numbers(text: str) -> list:
    """Numbers written as digits, with their signs: "x is minus 5" -> [-5]. "2x" or "4ac" aren't values."""
    return [Rational(top) / int(bottom or 1) * (-1 if sign else 1)
            for sign, top, bottom in SPOKEN_NUMBER.findall(text)]


def board_numbers(cmd: BoardCommand) -> list:
    """The values a board line shows: an equation's roots, an arithmetic result, or numbers in a text line."""
    if cmd.text:
        return spoken_numbers(cmd.text)
    if not cmd.check:
        return []                                           # a formula: letters only
    expr = mathcheck.parse_check(cmd.check)
    if expr.free_symbols:
        roots = mathcheck.solutions(expr)                   # None for a bare expression
        return list(roots) if roots is not None else []
    return [expr.rhs if isinstance(expr, Eq) else expr]


def test_mistake_fixes_reveal_no_answers(cur):
    """Fixes and the reteach step are spoken just before the next question and never run through the
    "reveals the answer" check (spec §3.1), so none of their numbers may answer a check in the concept."""
    for concept in cur.concepts.values():
        answers = [value for check in concept.checks for value in grading.answer_key(check).values or []]
        found = [(f"mistake {mistake.id}", spoken_numbers(mistake.fix)) for mistake in concept.mistakes]
        for segment in curriculum.get_step(concept, concept.reteach_step).segments:
            found.append((f"step {concept.reteach_step}", spoken_numbers(segment.say)))
            found += [(f"line {cmd.id}", board_numbers(cmd)) for cmd in segment.board if cmd.op == "write"]
        clashes = [(concept.id, where, number) for where, numbers in found for number in numbers
                   if any(grading.same_value(number, answer) for answer in answers)]
        assert clashes == []
