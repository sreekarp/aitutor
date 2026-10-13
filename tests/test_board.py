"""Tests for app/board.py: board state and the checks on every LLM reply."""
from app import board, curriculum, examples, grading
from app.models import BoardCommand, BoardLine, Check, Segment

FA1 = "Eq(x**2 - 5*x + 6, 0)"                      # roots 2 and 3
QUESTION = [BoardLine("q-label", text="Solve by factorisation:"),
            BoardLine("q-eq", latex="x^{2}-5x+6=0", check=FA1)]


def roots_check(equation: str = FA1, **fields) -> Check:
    return Check(id="t1", say="Solve it.", label="Solve:", equation=equation, kind="roots",
                 hints=["One.", "Two.", "Three."], **fields)


def write(line_id: str, latex: str | None = None, check: str | None = None, **fields) -> BoardCommand:
    return BoardCommand(op="write", id=line_id, latex=latex, check=check, **fields)


def highlight(target: str, part: str | None = None) -> BoardCommand:
    return BoardCommand(op="highlight", target=target, part=part)


def context(action: str = "hint", **fields) -> board.ReplyContext:
    """A reply about the pending question x^2 - 5x + 6 = 0, unless fields say otherwise."""
    values = dict(action=action, board=list(QUESTION), anchor=FA1, key=grading.answer_key(roots_check()),
                  next_key=None, student_answer="", check_equations=[FA1, "Eq(3*x**2 - 5*x - 2, 0)"],
                  formulas={"standard_form": r"ax^{2} + bx + c = 0"})
    return board.ReplyContext(**(values | fields))


def problems(*board_commands: BoardCommand, say: str = "Look at the board.", **fields) -> list[str]:
    """validate_reply() for a one-segment reply."""
    return board.validate_reply([Segment(say=say, board=list(board_commands))], context(**fields))


# ---------- board state ----------
def test_apply_replace_in_place_and_pins():
    start = QUESTION + [BoardLine("r1", latex="x = 1", check="Eq(x, 1)")]
    segments = [Segment(say="One.", board=[write("r1", "x = 2", "Eq(x, 2)"), write("r2", text="Roots")]),
                Segment(say="Two.", board=[highlight("r1", "2"), write("q-eq", "x^{2}=9", "Eq(x**2, 9)"),
                                           write("f1", formula="standard_form")])]
    after = board.apply(start, segments, {"standard_form": "ax^{2}+bx+c=0"})
    assert [line.id for line in after] == ["q-label", "q-eq", "r1", "r2", "f1"]   # rewrites stay in place
    assert after[1] == BoardLine("q-eq", latex="x^{2}=9", check="Eq(x**2, 9)")
    assert after[2] == BoardLine("r1", latex="x = 2", check="Eq(x, 2)")
    assert after[4] == BoardLine("f1", latex="ax^{2}+bx+c=0", formula="standard_form")
    assert start[2].latex == "x = 1"                                       # the input board is unchanged
    assert board.apply(after, [Segment(say="Next.", board=[BoardCommand(op="clear")])], {}) == []
    assert board.apply(after, [Segment(say="Look.", board=[highlight("r2")])], {}) == after


def test_apply_cap_keeps_question_lines():
    start = [BoardLine("r0", text="Earlier")] + QUESTION + [BoardLine(f"r{n}", text="Step") for n in range(1, 6)]
    assert len(start) == 8
    after = board.apply(start, [Segment(say="More.", board=[write(f"n{n}", text="New") for n in range(3)])], {})
    assert [line.id for line in after] == ["q-label", "q-eq", "r3", "r4", "r5", "n0", "n1", "n2"]


# ---------- checking a reply ----------
def test_say_rules():
    for say in (r"So \frac{1}{2}.", "x^2 is nine.", "Use {a, b}.", "It costs $5.", "Call it x_1."):
        assert any("LaTeX" in p for p in problems(say=say)), say
    for say in ("So x = -2.", "It is -3.", "Try −1 now.", "Put –2 in place of x.", "So x-2 is zero.",
                "Now 5-3 gives 2.", "Take 3x-5.", "It's -√3.", "It's - root 3.", "So -b over 2a.", "Try -.5."):
        assert any("'=' or a minus sign" in p for p in problems(say=say)), say
    for say in ("x squared minus 5x plus 6 equals zero.", "It's a well-known step: 3x squared.", "Self-check it.",
                "Nice work — 2 and 3 are right.", "Look at the x-axis.", "A two-step method."):
        assert not any("minus sign" in p for p in problems(say=say)), say
    assert problems(say="word " * 60) == []
    assert any("over 60 words" in p for p in problems(say="word " * 61))
    assert board.validate_reply([], context()) != []                       # 1 to 4 segments (3 asked for)
    assert board.validate_reply([Segment(say="Hi.")] * 4, context()) == []
    assert any("1 to 4 segments" in p for p in board.validate_reply([Segment(say="Hi.")] * 5, context()))


def test_llm_text_lines_no_digits():
    """A words line has no maths: no "=", no x next to a number, no decimal or fraction. Small whole numbers are
    written as words for it ("minus 6" -> "minus six"), as the prompt asks, instead of rejecting the reply."""
    assert problems(write("t1", text="Two roots, both positive")) == []
    cmd = write("t1", text="Step 1: find two numbers that multiply to minus 6 and add to minus 15")
    assert problems(cmd) == [] and cmd.text == ("Step one: find two numbers that multiply to minus six and add to "
                                                 "minus fifteen")
    for text in ("x = two", "a = 1", "Half is 0.5", "Use 1/2", "Try 3x", "It is 100"):
        assert any("text line" in p for p in problems(write("t1", text=text))), text
    assert any("exactly one of" in p for p in problems(write("t1", "x = 2", "Eq(x, 2)", text="Also")))
    assert any("needs a check" in p for p in problems(write("t1", "x^{2} = 4")))
    assert any("clear isn't allowed" in p for p in problems(BoardCommand(op="clear")))


def test_a_label_beside_maths_reports_both_word_rules():
    """A "Step 1 =" label on a maths line breaks rule 1 (one kind per line) and rule 4 (no "=" in words). Both come
    back at once, each saying how to fix it, because the single retry sends exactly these lines to the model."""
    found = problems(write("e1", "2(-3) = -6", "Eq(2*(-3), -6)", text="Step 1 = a times c"), action="explain")
    assert found == ["line e1: write exactly one of latex, formula or text; words and maths go on separate lines",
                     "line e1: the text line 'Step 1 = a times c' has digits or '='; write numbers in a words line "
                     "as words, "
                     "e.g. 'Step one', and put maths on a checked line"]


def test_wrong_factorisation_rejected_with_anchor():
    found = problems(write("h1", "(x-2)(x+3) = 0", "Eq((x-2)*(x+3), 0)"))
    assert any("h1" in p and "loses {3}, adds {-3}" in p for p in found), found
    assert problems(write("h1", "(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)")) == []
    # a display that doesn't match its check is caught too
    assert problems(write("h1", "(x-2)(x-3) = 0", "Eq((x-2)*(x+3), 0)")) != []


def test_word_problem_anchor_not_displayed():
    equation = "Eq(x*(x + 3), 40)"                                         # the breadth: 5 (and -8)
    hidden = dict(board=[BoardLine("q-label", text="A hall's length is 3 more than its breadth.")],
                  anchor=equation, key=grading.answer_key(roots_check(equation, keep="positive")))
    assert problems(write("h1", "x(x+3) = 40", "Eq(x*(x+3), 40)"), **hidden) == []
    wrong = problems(write("h1", "x(x-3) = 40", "Eq(x*(x-3), 40)"), **hidden)
    assert any("h1" in p for p in wrong), wrong                             # compared with the hidden question


def test_hint_cannot_use_new():
    example = write("e1", "x^{2}-7x+12=0", "Eq(x**2 - 7*x + 12, 0)", relation="new")
    for action in ("hint", "follow_up"):
        assert any("relation new" in p for p in problems(example, action=action)), action
    for action in ("explain", "reteach"):
        assert problems(example, action=action, next_key=grading.answer_key(roots_check())) == [], action


def test_highlight_part_rules():
    """A highlight must point at a line on the board. A part KaTeX can't mark on its own lights the whole line
    instead of rejecting the reply: it is only a visual detail (models write "-3x" for "- 3x")."""
    lines = [BoardLine("r1", latex="(x-2)(x-3)=0"), BoardLine("r2", latex=r"x = 2 \text{ or } x = 3"),
             BoardLine("r3", latex=r"\left(x - 2\right)^{2} = 0")]
    on_board = dict(board=QUESTION + lines)
    fuzzy = [highlight("r1", "(x+5)"),                                      # absent
             highlight("r1", "x-"),                                         # twice
             highlight("q-eq", "x^"),                                       # splits a power
             highlight("r3", r"\left(x - 2"),                               # a \left without its \right
             highlight("r2", r"text{ or }")]                                # inside \text
    for cmd in fuzzy:
        assert problems(cmd, **on_board) == [] and cmd.part is None, cmd.target
    assert len(problems(highlight("r9"), **on_board)) == 1                 # no such line
    good = [highlight("r1", "(x-2)"), highlight("q-eq"), highlight("r2", "x = 3"),
            write("h1", "(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)"), highlight("h1", "(x-3)")]
    assert problems(*good, **on_board) == []                              # incl. a line written in the reply


def test_reteach_cannot_use_check_equation():
    reteach = dict(action="reteach", next_key=grading.answer_key(roots_check("Eq(3*x**2 - 5*x - 2, 0)")))
    for latex, check in (("x^{2}-5x+6=0", "Eq(x**2 - 5*x + 6, 0)"), ("x^{2}+6=5x", "Eq(x**2 + 6, 5*x)"),
                         ("(x-2)(x-3)=0", "Eq((x-2)*(x-3), 0)")):
        found = problems(write("e1", latex, check, relation="new"), **reteach)
        assert any("a lesson question as its example" in p for p in found), latex
    assert problems(write("e1", "x^{2}-7x+12=0", "Eq(x**2 - 7*x + 12, 0)", relation="new"), **reteach) == []


def test_an_explanation_cannot_use_another_question_as_its_example(cur):
    """Rule 7 for explanations too: while fa2 is open, solving fa7 (a later question) as the example would give
    fa7's answer away. The open question's own equation stays allowed."""
    concept = cur.concepts["factorisation"]
    fa2 = curriculum.get_check(concept, "fa2")                          # 3x^2 - 5x - 2 = 0 is open
    open_fa2 = dict(action="explain", board=[], anchor=fa2.equation, key=grading.answer_key(fa2),
                    check_equations=[question.equation for question in concept.checks])
    solves_fa7 = [write("e1", "2x^2 - 7x + 3 = 0", "Eq(2*x**2 - 7*x + 3, 0)", relation="new"),
                  write("e2", "(2x - 1)(x - 3) = 0", "Eq((2*x - 1)*(x - 3), 0)"),
                  write("e3", r"x = 3 \text{ or } x = \frac{1}{2}", "Or(Eq(x, 3), Eq(x, 1/2))")]
    assert problems(*solves_fa7, say="Here is a similar one.", **open_fa2) == [
        f"line {line_id}: an explanation can't use another lesson question as its example"
        for line_id in ("e1", "e2", "e3")]
    own = [write("e1", "3x^2 - 6x + x - 2 = 0", "Eq(3*x**2 - 6*x + x - 2, 0)"),
           write("e2", "3x(x - 2) + 1(x - 2) = 0", "Eq(3*x*(x - 2) + 1*(x - 2), 0)")]
    assert problems(*own, say="Split the middle term, then take out a common factor.", **open_fa2) == []


def test_a_solved_example_starts_new_but_never_on_a_lesson_question():
    """The example action ("not sure" about a step): no question is open, so there is no anchor and no answer key,
    and the example may say its own roots. Its equation starts with relation new and may never be one of this
    concept's questions, rearranged, factorised or solved."""
    teaching = dict(action="example", board=[], anchor=None, key=None)
    solved = [write("e0", text="Another example"),
              write("e1", "x^{2}-9x+20=0", "Eq(x**2 - 9*x + 20, 0)", relation="new"),
              write("e2", "(x-4)(x-5)=0", "Eq((x-4)*(x-5), 0)"),
              write("e3", r"x = 4 \text{ or } x = 5", "Or(Eq(x, 4), Eq(x, 5))")]
    assert problems(*solved, say="Here's another one. Its roots are 4 and 5.", **teaching) == []
    for latex, check in (("x^{2}-5x+6=0", "Eq(x**2 - 5*x + 6, 0)"), ("(x-2)(x-3)=0", "Eq((x-2)*(x-3), 0)"),
                         ("3x^{2}-5x-2=0", "Eq(3*x**2 - 5*x - 2, 0)")):
        assert problems(write("e1", latex, check, relation="new"), **teaching) == [
            "line e1: a solved example can't use a lesson question as its example"], latex


def test_llm_cannot_reuse_id():
    for line_id in ("q-eq", "q-label"):
        assert any("already on the board" in p for p in problems(write(line_id, text="Again"))), line_id
    twice = problems(write("t1", text="First"), write("t1", text="Second"))
    assert len(twice) == 1 and "already on the board" in twice[0]
    assert problems(write("t1", text="First"), write("t2", text="Second")) == []


def test_reveals_answer_cases():
    key = grading.answer_key(roots_check())                                # x = 2 or x = 3
    def says(text, student="", commands=()):
        return board.reveals_answer([Segment(say=text, board=list(commands))], key, student)
    assert says("It's 2 and 3.")
    assert says("So x equals two.")
    assert says("The roots are 2 and 3.")
    assert says("Look.", commands=[write("r1", r"x = 2 \text{ or } x = 3", "Or(Eq(x, 2), Eq(x, 3))")])
    assert says("Find two numbers that multiply to 6 and add to minus 5.") is None
    assert says("Yes, x = 3 works. Now find the other root.", student="3") is None
    assert says("Look.", commands=[write("r1", "(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)")]) is None
    brackets = write("r1", r"x - 2 = 0 \text{ or } x - 3 = 0", "Or(Eq(x - 2, 0), Eq(x - 3, 0))")
    assert says("Look.", commands=[brackets])                              # each bracket = 0 states the roots
    assert says("It's 2 and 3.", student="2, 3") is None                 # nothing left to give away
    surds = grading.answer_key(roots_check("Eq(x**2 - 4*x + 1, 0)"))       # 2 - sqrt(3), 2 + sqrt(3)
    assert board.reveals_answer([Segment(say="So x is 2 plus root 3.")], surds, "")
    halves = grading.answer_key(roots_check("Eq(2*x**2 - 3*x - 2, 0)"))    # -1/2, 2
    assert board.reveals_answer([Segment(say="Then x is minus half.")], halves, "")
    nature = grading.answer_key(Check(id="n1", say="Nature?", label="Nature:", equation=FA1, kind="nature",
                                      hints=["One.", "Two.", "Three."]))
    assert board.reveals_answer([Segment(say="It has two distinct real roots.")], nature, "") is None
    found = problems(say="It's 2 and 3.")                                  # validate_reply runs it too
    assert any("gives away the answer" in p for p in found)
    # a reteach is checked against the next question's whole answer set, not the question just closed
    reteach = dict(action="reteach", next_key=grading.answer_key(roots_check("Eq(x**2 - 7*x + 10, 0)")),
                   student_answer="2")
    assert problems(say="Earlier the roots were 2 and 3.", **reteach) == []
    assert any("gives away the answer" in p for p in problems(say="Next one: it's 2 and 5.", **reteach))


def test_needs_review_must_end_on_key():
    review = dict(action="needs_review")
    working = write("w1", "(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)")
    claim = write("w2", r"x = 2 \text{ or } x = 3", "Or(Eq(x, 2), Eq(x, 3))")
    assert problems(working, claim, say="The roots are 2 and 3.", **review) == []   # showing it is the point
    assert any("must end with a root claim" in p for p in problems(working, **review))
    equation = "Eq(x*(x + 3), 40)"                                          # keep the positive root only
    hall = dict(review, anchor=equation, key=grading.answer_key(roots_check(equation, keep="positive")))
    both = write("w1", r"x = 5 \text{ or } x = -8", "Or(Eq(x, 5), Eq(x, -8))")
    kept = write("w2", "x = 5", "Eq(x, 5)", relation="select")
    assert problems(both, kept, **hall) == []
    assert any("must end with a root claim" in p for p in problems(both, **hall))


def test_the_lesson_question_rule_is_cheap_with_every_topics_questions(cur, monkeypatch):
    """Rule 7 compares each line with every topic's questions (34): the slow symbolic check runs only for a
    question the line could be, so a reply is checked in well under a second even on a slow server."""
    calls = []
    real = examples.mathcheck.same_maths
    monkeypatch.setattr(examples.mathcheck, "same_maths", lambda a, b: calls.append(1) or real(a, b))
    every = [c.equation for concept in cur.concepts.values() for c in concept.checks]
    assert examples.is_a_question("Eq(2*x**2 - 5*x + 3, 0)", every) is False
    assert calls == []                                                 # none of the 34 could be it
    assert examples.is_a_question("Eq(x**2 + 10, 7*x)", every) is True        # qf4, rearranged


def test_could_be_same_rules_out_different_equations_cheaply():
    """A quick look before same_maths: two equations whose sides differ at a sample point (either way round) can't
    be the same, so the slow symbolic check is skipped. It never rules out a real match, so results don't change."""
    p = board.mathcheck.parse_check
    question = p("Eq(x**2 - 7*x + 10, 0)")
    for same in ("Eq(x**2 - 7*x + 10, 0)", "Eq(x**2 + 10, 7*x)", "Eq(-x**2 + 7*x - 10, 0)", "Eq((x-2)*(x-5), 0)"):
        assert examples._could_be_same(p(same), question) and board.mathcheck.same_maths(p(same), question), same
    for other in ("Eq(x**2 - 9*x + 20, 0)", "Eq(2*x**2 - 5*x + 3, 0)", "Eq(x - 2, 0)"):
        assert not examples._could_be_same(p(other), question), other
    assert examples._could_be_same(p("Or(Eq(x, 2), Eq(x, 5))"), question)      # its roots: could be its solution
    assert not examples._could_be_same(p("Or(Eq(x, 3/2), Eq(x, 1))"), question)  # a root that doesn't solve it


def test_a_words_line_on_the_board_cannot_give_the_answer_away():
    """The board's words are read like speech for rule 6: "Roots are two and three" gives fa1's answer away."""
    key = grading.answer_key(roots_check("Eq(x**2 - 5*x + 6, 0)"))
    said = [Segment(say="Here is how to start.", board=[write("t1", text="Roots are 2 and 3")])]
    assert board.reveals_answer(said, key, "") is not None


def test_a_plus_or_minus_line_is_checked_through_its_two_branches():
    """Models teach the formula with one substituted line, x = (-5 ± ...)/2, and leave out its check. Its two
    branches are its check, so it is still proved: they must be exactly the roots of the equation above it."""
    example = write("e1", "x^{2} + 5x + 4 = 0", "Eq(x**2 + 5*x + 4, 0)", relation="new")
    right = write("e2", r"x = \frac{-5 \pm \sqrt{5^{2} - 4 \cdot 1 \cdot 4}}{2 \cdot 1}")
    assert problems(example, right, action="reteach") == [] and right.check.startswith("Or(Eq(x, ")
    wrong = write("e2", r"x = \frac{5 \pm \sqrt{5^{2} - 4 \cdot 1 \cdot 4}}{2 \cdot 1}")    # +b: the minus b slip
    assert any("not equivalent" in p for p in problems(example, wrong, action="reteach"))
    assert any("needs a check" in p for p in problems(example, write("e2", r"x = \frac{1}{2}"), action="reteach"))


def test_a_plus_or_minus_line_needs_something_above_it_to_prove_it():
    """Its branches are only proved against the line above (or the open question): with nothing above, or as a new
    example, a ± line with no check is still rejected. And only one ± per line."""
    sign_slip = write("e2", r"x = \frac{-9 \pm \sqrt{81-80}}{2}")
    assert any("needs a check" in p for p in problems(sign_slip, action="explain", anchor=None, key=None))
    new = write("e2", r"x = \frac{9 \pm \sqrt{81-80}}{2}", relation="new")
    assert any("needs a check" in p for p in problems(new, action="explain"))
    two = write("e2", r"x = 1 \pm 2 \pm 3")
    assert any("needs a check" in p for p in problems(write("e1", "x^{2} - 9x + 20 = 0", "Eq(x**2 - 9*x + 20, 0)",
                                                            relation="new"), two, action="explain"))


def test_the_board_words_give_no_number_answer_away_and_step_labels_are_fine():
    """A number up to 99 in words is read back like speech ("twenty-five" is 25), and "Step two" or "Step three"
    labels are not the answer's values."""
    d_key = grading.answer_key(Check(id="d", say="Find D.", label="D:", equation="Eq(x**2 - 7*x + 6, 0)",
                                     kind="discriminant", hints=["a", "b", "c"]))
    words = [Segment(say="Here is a hint.", board=[write("t1", text="So D is twenty-five")])]
    assert board.reveals_answer(words, d_key, "") is not None
    key = grading.answer_key(roots_check())                                  # x^2 - 5x + 6: roots 2 and 3
    steps = [Segment(say="Let's go.", board=[write("t1", text="Step two: find the factors"),
                                              write("t2", text="Step three: set each bracket to zero")])]
    assert board.reveals_answer(steps, key, "") is None
