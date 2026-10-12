"""Scripted-mode tests of app/tutor.py and app/page.py: whole turns, real lesson files, an in-memory database.
The whole turns about how a session or topic starts are in test_topic_starts.py."""
import json
from dataclasses import replace

import pytest
from conftest import new_question, new_student, progress_of, said_with_working

from app import curriculum, db, engine, grading, page
from app.config import MAX_INPUT_CHARS, Settings
from app.models import BoardCommand, Pending, Segment
from app.tutor import DIDNT_CATCH, NoSession, Tutor

SETTINGS = Settings(openrouter_api_key="", access_code="test", session_secret="test", database_url="sqlite://",
                    llm_mode="scripted", max_turns_per_day=1000, tts_voice="sarah", stt_language="en",
                    https_only=False)
PHONE = "2 and 3, my number is 9876543210"         # a safety keyword (personal details), mixed with an answer


@pytest.fixture
def tutor(cur, db_engine):
    return Tutor(cur, db_engine, SETTINGS)


def to_first_question(tutor, cur, student_id):
    """Start, then "ok" through every factorisation step; returns the turn that asked the first question."""
    tutor.start(student_id)
    for _ in cur.concepts["factorisation"].steps:
        result = tutor.turn(student_id, "ok")
    return result


def test_full_pass_through_factorisation_scripted(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    concept = cur.concepts["factorisation"]
    assert tutor.start(student_id).decision.rule == "S0"
    assert progress_of(db_engine, cur, student_id)["factorisation"].step_id == concept.steps[0].id
    for step in concept.steps[1:]:
        result = tutor.turn(student_id, "ok")
        assert (result.decision.rule, result.decision.action) == ("R0", "next_step")
        assert progress_of(db_engine, cur, student_id)["factorisation"].step_id == step.id

    result = tutor.turn(student_id, "ok")                       # after the last step: the first question
    state = progress_of(db_engine, cur, student_id)["factorisation"]
    assert (result.decision.rule, result.decision.action) == ("R0", "start_check")
    assert state.status == "checking" and state.pending == Pending(concept.checks[0].id)
    assert result.segments[-1] == new_question(concept.checks[0], cur.formulas)

    first = concept.checks[0]
    result = tutor.turn(student_id, said_with_working(first))
    assert (result.decision.rule, result.decision.action) == ("R1", "credit")
    state = progress_of(db_engine, cur, student_id)["factorisation"]
    assert state.points == 1.0 and state.pending == Pending(curriculum.next_check_id(concept, first.id))

    second = curriculum.get_check(concept, state.pending.check_id)
    result = tutor.turn(student_id, said_with_working(second))
    assert (result.decision.rule, result.decision.action) == ("R1", "mastered")
    progress = progress_of(db_engine, cur, student_id)
    assert progress["factorisation"].status == "mastered" and progress["factorisation"].pending is None
    following = cur.order[cur.order.index("factorisation") + 1]
    assert result.decision.resume_concept_id == following
    assert progress[following].status == "teaching"
    assert progress[following].step_id == cur.concepts[following].steps[0].id
    says = [segment.say for segment in result.segments]
    assert says[0].endswith("! You've mastered solving by factorisation.")
    assert says[1] == f"Now let's move on to the quadratic formula. {cur.concepts[following].intro}"
    assert says[2:] == [segment.say for segment in cur.concepts[following].steps[0].segments]
    assert says[2] == "Here is the formula. Read a, b and c from the standard form and put them in."   # no repeat
    assert "q-eq" not in [line.id for line in result.board]     # the board was cleared for the new concept
    assert result.calls == []


def test_wrong_trap_answer_reteaches_and_asks_next(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    to_first_question(tutor, cur, student_id)
    concept = cur.concepts["factorisation"]
    check = concept.checks[0]
    mistake_id, trap = next(iter(check.traps.items()))
    result = tutor.turn(student_id, trap)
    decision = result.decision
    assert (decision.rule, decision.action, decision.mistake) == ("R3", "reteach", mistake_id)
    assert decision.strategy == "replay"                         # scripted: no LLM strategy is used up
    next_id = curriculum.next_check_id(concept, check.id)
    state = progress_of(db_engine, cur, student_id)["factorisation"]
    assert decision.ask_check_id == next_id and state.pending == Pending(next_id)
    assert state.reteach_count == 1 and state.strategies_tried == []
    says = [segment.say for segment in result.segments]
    fix = next(mistake.fix for mistake in concept.mistakes if mistake.id == mistake_id)
    assert any(fix in say for say in says)
    assert all(segment.say in says for segment in curriculum.get_step(concept, concept.reteach_step).segments)
    assert result.segments[-1] == new_question(curriculum.get_check(concept, next_id), cur.formulas)
    assert grading.key_text(check) not in decision.reason


def test_asking_for_the_steps_gets_the_next_hint_for_half_a_point(tutor, db_engine, cur):
    """Asking for the steps of the open question is asking for help: the next hint, one step at a time, and a right
    answer after it earns half a point, the same as after "help me solve this"."""
    student_id = new_student(db_engine, cur)
    to_first_question(tutor, cur, student_id)
    fa1 = cur.concepts["factorisation"].checks[0]
    for text, level in (("Explain step by step.", 1), ("show me how", 2)):
        result = tutor.turn(student_id, text)
        assert (result.decision.rule, result.decision.action, result.decision.hint_level) == ("R4", "hint", level)
        assert [segment.say for segment in result.segments] == [
            f"Here's a hint. {fa1.hints[level - 1]}", "Try that step, then tell me the roots."]
    last = tutor.turn(student_id, "how do I solve this?")
    assert [segment.say for segment in last.segments] == [f"Here's a hint. {fa1.hints[2]}", "So, what are the roots?"]
    solved = tutor.turn(student_id, said_with_working(fa1))
    assert solved.decision.rule == "R1" and progress_of(db_engine, cur, student_id)["factorisation"].points == 0.5


def test_ok_while_teaching_uses_no_llm(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    result = tutor.turn(student_id, "ok")
    assert result.decision.rule == "R0"
    assert result.calls == []
    with db_engine.connect() as conn:
        assert db.get_turn(conn, result.turn_id)["calls"] == []


def test_turn_without_session_raises(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    with pytest.raises(NoSession):
        tutor.turn(student_id, "ok")


def test_turn_long_input_truncated(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    result = tutor.turn(student_id, "I think the answer is " * 100)          # a 2,200-character paste
    assert len(result.transcript) == MAX_INPUT_CHARS
    with db_engine.connect() as conn:
        assert len(db.get_turn(conn, result.turn_id)["student_text"]) == MAX_INPUT_CHARS
    for text in ("हाँ", "🤔🤔", ""):                            # non-Latin, emoji only, empty: no crash
        assert tutor.turn(student_id, text).decision.rule == "R8"


def test_turn_after_check_removed(db_engine, cur):
    student_id = new_student(db_engine, cur)
    to_first_question(Tutor(cur, db_engine, SETTINGS), cur, student_id)
    edited = cur.model_copy(deep=True)                           # never edit the shared fixture
    concept = edited.concepts["factorisation"]
    removed = concept.checks.pop(0)                              # the pending question is gone
    result = Tutor(edited, db_engine, SETTINGS).turn(student_id, "2 and 3")
    assert (result.decision.rule, result.decision.action) == ("R8", "lesson_changed")
    assert "curriculum_changed" in result.issues and result.grade is None
    new_id = curriculum.next_check_id(concept, removed.id)       # the removed id is unknown -> check 1
    assert progress_of(db_engine, edited, student_id)["factorisation"].pending == Pending(new_id)
    assert result.segments[-1] == new_question(curriculum.get_check(concept, new_id), edited.formulas)


def test_limit_reached(db_engine, cur):
    student_id = new_student(db_engine, cur)
    tutor = Tutor(cur, db_engine, replace(SETTINGS, max_turns_per_day=2))
    tutor.start(student_id)
    tutor.turn(student_id, "ok")
    before = progress_of(db_engine, cur, student_id)
    result = tutor.turn(student_id, "ok")                        # turn 3 of a 2-turn day
    assert result.decision.rule == "LIMIT"
    assert progress_of(db_engine, cur, student_id) == before
    with db_engine.connect() as conn:
        assert db.turns_today(conn) == 3                         # the LIMIT reply is saved as a turn too


def test_internal_error_leaves_progress(tutor, db_engine, cur, monkeypatch):
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    before = progress_of(db_engine, cur, student_id)

    def broken(*args, **kwargs):
        raise RuntimeError("bug in the rules")

    monkeypatch.setattr(engine, "decide", broken)
    result = tutor.turn(student_id, "ok")
    assert "Sorry, let's try that again" in result.segments[0].say
    assert "internal_error" in result.issues
    assert progress_of(db_engine, cur, student_id) == before


def test_stt_failed_turn_asks_to_type(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    result = tutor.turn(student_id, "", input_mode="voice", stt_failed=True)
    assert [segment.say for segment in result.segments] == [DIDNT_CATCH]
    assert (result.decision.rule, result.understood.type, result.input_hint) == ("R8", "unclear", "type")

    to_first_question(tutor, cur, student_id)
    result = tutor.turn(student_id, "", input_mode="voice", stt_failed=True)
    check = cur.concepts["factorisation"].checks[0]
    assert result.segments[0].say == DIDNT_CATCH
    assert result.segments[-1] == curriculum.ask_segment(check, cur.formulas)    # while checking: the question again
    assert result.input_hint == "type"
    assert progress_of(db_engine, cur, student_id)["factorisation"].pending == Pending(check.id)


def test_say_again_after_didnt_catch_replays_the_step(tutor, db_engine, cur):
    """While teaching, "say again" right after a lone "didn't catch that" replays the step, not the apology."""
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)                                      # greeting, intro, step 1
    sorry = tutor.turn(student_id, "", input_mode="voice", stt_failed=True)
    assert [segment.say for segment in sorry.segments] == [DIDNT_CATCH]
    again = tutor.turn(student_id, "say again")
    step_1 = cur.concepts["factorisation"].steps[0].segments
    assert [segment.say for segment in again.segments] == [segment.say for segment in step_1]


def test_payload_shape(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    assert page.state(tutor, student_id)["board"] == [] and page.state(tutor, student_id)["why"] == []
    tutor.start(student_id)
    teaching = {row["concept_id"]: row for row in page.state(tutor, student_id)["progress"]}["factorisation"]
    assert (teaching["status"], teaching["step_index"], teaching["current"]) == ("teaching", 0, True)
    for _ in cur.concepts["factorisation"].steps:
        tutor.turn(student_id, "ok")
    check = cur.concepts["factorisation"].checks[0]
    result = tutor.turn(student_id, check.traps["sign_flip"])   # a wrong answer: a reteach
    data = page.payload(tutor, student_id, result)

    assert set(data) == {"turn_id", "transcript", "segments", "decision", "progress", "why", "board", "input_hint"}
    assert (data["turn_id"], data["transcript"], data["input_hint"]) == (result.turn_id, check.traps["sign_flip"], None)
    assert data["decision"] == {"rule": "R3", "action": "reteach", "reason": result.decision.reason}
    assert data["why"][0] == data["decision"] and len(data["why"]) == 5     # newest first, the last 5
    assert all(set(why) == {"rule", "action", "reason"} for why in data["why"])
    assert [segment["say"] for segment in data["segments"]] == [segment.say for segment in result.segments]
    for segment in data["segments"]:
        assert set(segment) == {"say", "board"}
        assert all(set(cmd) == {"op", "id", "latex", "formula", "text", "target", "part"} for cmd in segment["board"])
    assert data["board"] == [{"id": line.id, "latex": line.latex, "formula": line.formula, "text": line.text}
                             for line in result.board]
    assert [row["concept_id"] for row in data["progress"]] == cur.order
    rows = {row["concept_id"]: row for row in data["progress"]}
    assert rows["factorisation"] == {"concept_id": "factorisation", "title": cur.concepts["factorisation"].title,
                                     "status": "checking", "step_index": None, "step_count": 4, "points": 0.0,
                                     "hint_level": 0, "reteach_count": 1, "current": True}
    assert rows["roots"]["status"] == "mastered" and not rows["roots"]["current"]

    text = json.dumps(data)                                      # no answer keys anywhere
    assert '"evidence"' not in text and '"check"' not in text
    asked = curriculum.get_check(cur.concepts["factorisation"], result.decision.ask_check_id)
    assert grading.key_text(check) not in text and grading.key_text(asked) not in text

    formula = Segment(say="Here is the formula.", board=[BoardCommand(op="write", id="f1", formula="discriminant")])
    cmd = page.payload(tutor, student_id, replace(result, segments=[formula]))["segments"][0]["board"][0]
    assert (cmd["formula"], cmd["latex"]) == ("discriminant", cur.formulas["discriminant"])   # LaTeX filled in


def test_safety_turn_keeps_pending_and_flags_review(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    to_first_question(tutor, cur, student_id)
    before = progress_of(db_engine, cur, student_id)
    for text in (PHONE, "my dad gets drunk and beats me", "my dad... beats me",    # a person, then "beats me"
                 "I was raped, help", "I'm getting beaten at home, please help"):
        result = tutor.turn(student_id, text)
        assert result.decision.rule == "R10" and result.grade is None, text
        assert "safety_review" in result.issues, text
        assert progress_of(db_engine, cur, student_id) == before, text


def test_a_missed_disclosure_never_gets_a_maths_hint(tutor, db_engine, cur):
    """The keywords miss "He gets drunk. Beats me." With "I don't know" or "help" after it, the question is asked
    again: no hint, and no hint level used."""
    student_id = new_student(db_engine, cur)
    to_first_question(tutor, cur, student_id)
    for text in ("He gets drunk. Beats me. I don't know", "He gets drunk. Beats me. Please help"):
        result = tutor.turn(student_id, text)
        assert (result.decision.rule, result.decision.action) == ("R8", "repeat"), text
    assert progress_of(db_engine, cur, student_id)["factorisation"].pending.hint_level == 0


def test_my_number_is_seven_is_graded_not_flagged(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur, mastered=[])
    with db_engine.begin() as conn:                              # standard form's question sf2 ("what is b?") is open
        progress = db.load_progress(conn, student_id, cur)
        state = progress["standard_form"]
        state.status, state.step_id = "checking", cur.concepts["standard_form"].steps[-1].id
        state.pending, state.last_check_id = Pending("sf2"), "sf2"
        db.save_progress(conn, student_id, progress, cur.version)
    tutor.start(student_id)
    result = tutor.turn(student_id, "My number is seven.")       # b is -7: 7 is the sign_when_moving trap
    assert result.decision.rule == "R3" and "safety_review" not in result.issues
    assert result.grade.trap == "sign_when_moving"


def test_end_when_all_done_but_safety_first(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur, mastered=cur.order)
    first = tutor.start(student_id)
    assert first.decision.rule == "S0" and first.decision.resume_concept_id is None
    assert [segment.say for segment in first.segments] == [                   # greeting, then the END summary
        "Welcome back!", "That's everything for now, Asha! You've mastered standard form and recognising a "
        "quadratic, roots and checking by substitution, solving by factorisation, the quadratic formula, "
        "the discriminant and the nature of roots and word problems."]
    result = tutor.turn(student_id, "ok")
    assert result.decision.rule == "END"
    assert [segment.say for segment in result.segments] == [segment.say for segment in first.segments[1:]]
    assert tutor.turn(student_id, PHONE).decision.rule == "R10"
