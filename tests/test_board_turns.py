"""Whole turns: what the whiteboard shows after each kind of move. Every new question starts on a clean board;
a hint, explanation, follow-up, probe, show-me or re-ask of the same question keeps the board and its working."""
from conftest import FakeLLM, make_settings, new_student

from app import curriculum, db
from app.tutor import Tutor

LLM = make_settings(openrouter_api_key="test-key", llm_mode="auto")     # a FakeLLM plays the model: no network
SPLIT = {"op": "write", "id": "h1", "latex": "x^2 - 2x - 3x + 6 = 0", "check": "Eq(x**2 - 2*x - 3*x + 6, 0)",
         "relation": "equiv"}                                           # a hint line for fa1 (x^2 - 5x + 6 = 0)
HINT = {"segments": [{"say": "Split the middle term.", "board": [SPLIT]}]}
NO_LINES = {"segments": [{"say": "Look at the signs again.", "board": []}]}    # a reply that writes nothing


def understood(kind: str, answer: str = "", working: bool = False) -> dict:
    """UNDERSTAND's JSON, as strict mode returns it."""
    return {"type": kind, "answer": answer, "showed_working": working, "mistake": None, "safety": False}


def write(line_id: str, latex: str, check: str, relation: str = "equiv") -> dict:
    """One board write command, as RESPOND returns it."""
    return {"op": "write", "id": line_id, "latex": latex, "check": check, "relation": relation}


def ids(result) -> list[str]:
    return [line.id for line in result.board]


def with_a_hint_line(cur, db_engine, *understand, respond=()):
    """Factorisation's first question fa1 is asked, then a hint writes h1 under it.
    Returns (tutor, student, the board after the hint)."""
    fake = FakeLLM(understand=[understood("dont_know"), *understand], respond=[HINT, *respond])
    tutor = Tutor(cur, db_engine, LLM, llm=fake)
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    for _ in range(len(cur.concepts["factorisation"].steps) + 1):
        tutor.turn(student_id, "ok")                                    # step 1, ...; the last "ok" asks fa1
    hinted = tutor.turn(student_id, "no idea")
    assert hinted.board[-1].id == "h1"
    return tutor, student_id, hinted.board


def test_first_question_starts_a_clean_board(cur, db_engine):
    """The lesson's last example leaves the board when the first question is asked."""
    tutor = Tutor(cur, db_engine, make_settings())
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    for _ in range(len(cur.concepts["factorisation"].steps) + 1):      # step 1, ..., then the first question
        result = tutor.turn(student_id, "ok")
    assert result.decision.action == "start_check" and ids(result) == ["q-label", "q-eq"]


def test_right_answer_the_next_question_starts_a_clean_board(cur, db_engine):
    """The reported bug: fa1's working line stayed under the next question."""
    tutor, student_id, _ = with_a_hint_line(cur, db_engine, understood("answer", "2, 3", working=True))
    result = tutor.turn(student_id, "2 and 3, I split the middle term")
    assert (result.decision.rule, result.decision.action) == ("R1", "credit")
    assert result.segments[0].board == []                               # "Right!" plays over the old board
    fa2 = curriculum.get_check(cur.concepts["factorisation"], "fa2")
    assert ids(result) == ["q-label", "q-eq"] and result.board[1].check == fa2.equation
    with db_engine.connect() as conn:                                   # a reload draws the same board
        assert db.session_board(conn, result.session_id) == result.board


def test_same_question_keeps_the_board(cur, db_engine):
    """A re-ask (R8), show me (R6), an explanation (R7), the next hint (R4), a probe (R2), a follow-up (R5) and
    Continue (S1) are all about the same question: the board stays exactly as it was."""
    tutor, student_id, board = with_a_hint_line(
        cur, db_engine, understood("unclear"), understood("claims_understanding"), understood("question"),
        understood("dont_know"), understood("answer", "2, 3"), understood("answer", "2"),
        respond=[NO_LINES, NO_LINES, NO_LINES])
    for text, rule in (("hmm", "R8"), ("I get it", "R6"), ("why split it?", "R7"), ("still stuck", "R4"),
                       ("2 and 3", "R2"), ("just 2", "R5")):
        result = tutor.turn(student_id, text)
        assert (result.decision.rule, result.board) == (rule, board), text
    result = tutor.start(student_id)                                    # back within 30 minutes
    assert (result.decision.rule, result.board) == ("S1", board)


def test_reteach_example_is_shown_then_the_new_question_starts_clean(cur, db_engine):
    """A reteach may point at the old question and write its example; the next question then starts clean."""
    worked = {"segments": [
        {"say": "Look at your question again.", "board": [{"op": "highlight", "target": "q-eq", "part": "-5x"}]},
        {"say": "Here is a different one.", "board": [write("r1", "x^2 - 9x + 20 = 0", "Eq(x**2 - 9*x + 20, 0)",
                                                            "new"),
                                                      write("r2", "(x-4)(x-5) = 0", "Eq((x-4)*(x-5), 0)")]}]}
    tutor, student_id, _ = with_a_hint_line(cur, db_engine, understood("answer", "-2, -3"), respond=[worked])
    result = tutor.turn(student_id, "minus 2 and minus 3")              # the sign_flip trap
    assert (result.decision.rule, result.decision.action, result.issues) == ("R3", "reteach", [])
    assert [cmd.id for cmd in result.segments[1].board] == ["r1", "r2"]   # the example was on the board
    fa2 = curriculum.get_check(cur.concepts["factorisation"], "fa2")
    assert ids(result) == ["q-label", "q-eq"] and result.board[1].check == fa2.equation


def test_substitute_back_lines_leave_with_their_equation(cur, db_engine):
    """substitute_back works on the OLD equation, so its lines must not stay above the next one."""
    lines = {"segments": [{"say": "Put your values back in. Neither gives zero.", "board": [
        write("s1", r"(-2)^{2} - 5 \cdot (-2) + 6 = 20", "Eq((-2)**2 - 5*(-2) + 6, 20)"),
        write("s2", r"(-3)^{2} - 5 \cdot (-3) + 6 = 30", "Eq((-3)**2 - 5*(-3) + 6, 30)")]}]}
    tutor, student_id, _ = with_a_hint_line(cur, db_engine, understood("answer", "-2, -3"), respond=[lines])
    with db_engine.begin() as conn:                                     # worked_example was used already
        progress = db.load_progress(conn, student_id, cur)
        progress["factorisation"].reteach_count, progress["factorisation"].strategies_tried = 1, ["worked_example"]
        db.save_progress(conn, student_id, progress, cur.version)
    result = tutor.turn(student_id, "minus 2 and minus 3")
    assert (result.decision.strategy, result.issues) == ("substitute_back", [])
    assert ids(result) == ["q-label", "q-eq"]


def test_scripted_reteach_replays_its_step_then_starts_clean(cur, db_engine):
    tutor = Tutor(cur, db_engine, make_settings())
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    for _ in range(len(cur.concepts["factorisation"].steps) + 1):
        tutor.turn(student_id, "ok")
    result = tutor.turn(student_id, "minus 2 and minus 3")
    assert (result.decision.rule, result.decision.strategy) == ("R3", "replay")
    assert ids(result) == ["q-label", "q-eq"]


def test_lesson_changed_starts_a_clean_board(cur, db_engine):
    """The open question was removed from the lesson file: its hint line must not stay under the new one."""
    tutor, student_id, _ = with_a_hint_line(cur, db_engine)
    edited = cur.model_copy(deep=True)
    edited.concepts["factorisation"].checks.pop(0)                      # fa1, the open question, is gone
    result = Tutor(edited, db_engine, LLM, llm=tutor.llm).turn(student_id, "2 and 3")
    assert result.decision.action == "lesson_changed" and ids(result) == ["q-label", "q-eq"]
    assert result.board[1].check == edited.concepts["factorisation"].checks[0].equation


def test_continue_after_a_lesson_change_starts_a_clean_board(cur, db_engine):
    """Continue (S1) asks the new question when the lesson file removed the open one: a clean board too."""
    tutor, student_id, _ = with_a_hint_line(cur, db_engine)
    edited = cur.model_copy(deep=True)
    edited.concepts["factorisation"].checks.pop(0)
    result = Tutor(edited, db_engine, LLM, llm=tutor.llm).start(student_id)
    assert result.decision.rule == "S1" and ids(result) == ["q-label", "q-eq"]
    assert result.board[1].check == edited.concepts["factorisation"].checks[0].equation


def test_needs_review_answer_does_not_follow_into_the_next_concept(cur, db_engine):
    """Unchanged: R9 moves on to the next concept, which starts on a clean board."""
    answer = {"segments": [{"say": "Here it is step by step.", "board": [
        write("w1", "(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)"),
        write("w2", r"x = 2 \text{ or } x = 3", "Or(Eq(x, 2), Eq(x, 3))")]}]}
    tutor, student_id, _ = with_a_hint_line(cur, db_engine, understood("answer", "-2, -3"), respond=[answer])
    with db_engine.begin() as conn:                                     # three reteaches already happened
        progress = db.load_progress(conn, student_id, cur)
        progress["factorisation"].reteach_count = 3
        db.save_progress(conn, student_id, progress, cur.version)
    result = tutor.turn(student_id, "minus 2 and minus 3")
    assert result.decision.rule == "R9" and ids(result) == []          # the quadratic formula opens at its start
