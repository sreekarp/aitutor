"""app/tutor.py and app/llm_steps.py with the LLM: a fake client plays UNDERSTAND and RESPOND."""
import pytest
from conftest import FakeLLM, new_question, new_student

from app import curriculum, db, replies
from app.config import POINTS_TO_MASTER, Settings
from app.llm import ProviderError
from app.replies import REVIEW_LATER, SAFETY_REPLY
from app.tutor import MISHEARD, Tutor

SETTINGS = Settings(openrouter_api_key="test-key", access_code="test", session_secret="test",
                    database_url="sqlite://", llm_mode="auto", max_turns_per_day=1000, tts_voice="sarah",
                    stt_language="en", https_only=False)
HINT = {"segments": [{"say": "Look at the middle term and the number on its own.", "board": []}]}


def understood(kind: str, answer: str = "", working: bool = False, safety: bool = False) -> dict:
    """UNDERSTAND's JSON, as strict mode returns it."""
    return {"type": kind, "answer": answer, "showed_working": working, "mistake": None, "safety": safety}


def write(line_id: str, latex: str, check: str, relation: str = "equiv") -> dict:
    """One board write command, as RESPOND returns it."""
    return {"op": "write", "id": line_id, "latex": latex, "check": check, "relation": relation}


# RESPOND's worked answer to fa1 for R9 (needs review): it must end on the answer.
WORKED = {"segments": [{"say": "This one is tricky, and that's fine. Here it is step by step.", "board": [
    write("w1", "(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)"),
    write("w2", r"x = 2 \text{ or } x = 3", "Or(Eq(x, 2), Eq(x, 3))")]}]}


def at_first_question(cur, db_engine, fake: FakeLLM) -> tuple[Tutor, str]:
    """Asha starts factorisation (earlier concepts mastered) and says "ok" to step 1 and on through its steps:
    question fa1."""
    tutor = Tutor(cur, db_engine, SETTINGS, llm=fake)
    with db_engine.begin() as conn:
        student_id = db.get_or_create_student(conn, "asha", "Asha")["id"]
        progress = db.load_progress(conn, student_id, cur)
        for concept_id in ("standard_form", "roots"):
            progress[concept_id].status, progress[concept_id].points = "mastered", POINTS_TO_MASTER
        db.save_progress(conn, student_id, progress, cur.version)
    tutor.start(student_id)
    for _ in range(len(cur.concepts["factorisation"].steps) + 1):
        tutor.turn(student_id, "ok")
    assert fake.asked == []                       # "ok" while teaching is a fast path: no LLM call
    return tutor, student_id


def roles(fake: FakeLLM) -> list[str]:
    return [role for role, _ in fake.asked]


def factorisation_state(db_engine, cur, student_id):
    with db_engine.connect() as conn:
        return db.load_progress(conn, student_id, cur)["factorisation"]


def test_llm_turn_uses_two_calls_for_wrong_answer(cur, db_engine):
    fake = FakeLLM(understand=[understood("answer", "1, 6")], respond=[HINT])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "one and six")
    assert roles(fake) == ["understand", "respond"]
    assert (result.decision.rule, result.decision.action) == ("R4", "hint")
    assert result.segments[0].say == HINT["segments"][0]["say"]
    assert result.segments[-1].say == "Try that step, then tell me the roots."   # the re-ask after hint 1
    assert [call["role"] for call in result.calls] == ["understand", "respond"] and result.issues == []
    facts = fake.asked[1][1][1]["content"]
    assert "HINT LEVEL: 1 of 3" in facts and cur.concepts["factorisation"].checks[0].hints[0] in facts


def test_llm_turn_uses_one_call_for_correct_answer(cur, db_engine):
    fake = FakeLLM(understand=[understood("answer", "2, 3", working=True)])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "2 and 3, I split the middle term")
    assert roles(fake) == ["understand"]
    assert (result.decision.rule, result.decision.action) == ("R1", "credit")
    assert len(result.calls) == 1 and result.issues == []
    assert factorisation_state(db_engine, cur, student_id).points == 1.0


def test_understand_failure_falls_back_to_scripted(cur, db_engine):
    fake = FakeLLM(understand=[ProviderError("timeout")], respond=[HINT])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "I don't know")
    assert roles(fake) == ["understand"]                                   # RESPOND isn't tried this turn
    assert result.understood.type == "dont_know"                           # read by fallback.understand
    assert (result.decision.rule, result.decision.action) == ("R4", "hint")
    assert result.segments[0].say.startswith("Here's a hint.")             # the scripted hint
    assert result.issues == ["understand_fallback"]


def test_understand_rejected_output_is_retried_and_recorded(cur, db_engine):
    fake = FakeLLM(understand=[understood("answer", "two and three", working=True),
                               understood("answer", "2, 3", working=True)])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "two and three, I factorised")
    assert roles(fake) == ["understand", "understand"]
    assert result.issues == ["understand_rejected"]                        # first output failed the answer parser
    assert (result.decision.rule, result.decision.action) == ("R1", "credit")


def test_respond_rejected_then_scripted_records_issues(cur, db_engine):
    wrong_maths = {"segments": [{"say": "Start with x^2.", "board": [write("h1", "(x-2)(x+3) = 0",
                                                                              "Eq((x-2)*(x+3), 0)")]}]}
    reveals = {"segments": [{"say": "The roots are 2 and 3.", "board": []}]}
    fake = FakeLLM(understand=[understood("dont_know")], respond=[wrong_maths, reveals])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "pata nahi yaar")
    assert roles(fake) == ["understand", "respond", "respond"]
    rejected = [issue for issue in result.issues if issue.startswith("respond_rejected:")]
    assert "respond_rejected:segment 1: say has LaTeX characters; say the maths in words" in rejected
    assert any("line h1: not equivalent" in issue for issue in rejected)   # checked against the question
    assert any("gives away the answer" in issue for issue in rejected)     # the retry was rejected too
    assert result.issues[-1] == "respond_fallback"
    assert result.segments[0].say.startswith("Here's a hint.")             # the scripted hint is shown
    assert "x^2" not in " ".join(segment.say for segment in result.segments)


def test_a_question_whose_reply_is_rejected_twice_gets_a_lesson_hint(cur, db_engine):
    """R7 while a question is open: when RESPOND is rejected twice, the scripted words say where to start (hint 1),
    or after a hint that last hint again, then the question. R7 adds no hint level, so it stays free."""
    wrong = {"segments": [{"say": "Look at this.", "board": [write("e1", "3(-2) = 6", "Eq(3*(-2), 6)")]}]}
    fake = FakeLLM(understand=[understood("question"), understood("dont_know"), understood("question")],
                   respond=[wrong, wrong, HINT, wrong, wrong])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    fa1 = cur.concepts["factorisation"].checks[0]
    first = tutor.turn(student_id, "why do we split the middle term?")
    assert (first.decision.rule, first.issues[-1]) == ("R7", "respond_fallback")
    assert [segment.say for segment in first.segments] == [
        "Good question. Here's where to start.", fa1.hints[0], "So, what are the roots?"]
    tutor.turn(student_id, "I'm stuck")                                    # hint 1, written by the AI
    again = tutor.turn(student_id, "but why split it?")
    assert [segment.say for segment in again.segments] == [
        "Good question. Let's look at the last hint again.", fa1.hints[0], "So, what are the roots?"]
    assert factorisation_state(db_engine, cur, student_id).pending.hint_level == 1


def test_reteach_fallback_is_replay(cur, db_engine):
    fake = FakeLLM(understand=[understood("answer", "-2, -3")], respond=[ProviderError("HTTP 500")])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "minus 2 and minus 3")
    decision = result.decision
    assert (decision.rule, decision.action, decision.mistake) == ("R3", "reteach", "sign_flip")
    assert decision.strategy == "replay" and result.issues == ["respond_fallback"]
    state = factorisation_state(db_engine, cur, student_id)
    assert state.reteach_count == 1 and state.strategies_tried == []       # no LLM strategy was used up
    concept = cur.concepts["factorisation"]
    replayed = curriculum.get_step(concept, concept.reteach_step).segments
    assert all(segment in result.segments for segment in replayed)


@pytest.mark.parametrize("llm_mistake", ["sign_flip", None])
def test_answer_corrected_by_understand_is_graded_as_said(cur, db_engine, llm_mistake):
    # Seen with the real model: the student says the sign-flipped roots, and UNDERSTAND writes down the right
    # ones (naming the mistake, or not). The student's own words are graded instead: the trap, not a point.
    fixed = {**understood("answer", "2, 3"), "mistake": llm_mistake}
    fake = FakeLLM(understand=[fixed], respond=[ProviderError("HTTP 500")])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "minus 2 and minus 3")
    assert (result.decision.rule, result.decision.mistake) == ("R3", "sign_flip")
    assert result.grade.verdict == "wrong" and result.understood.answer == "-2, -3"
    assert "answer_rewritten" in result.issues
    assert factorisation_state(db_engine, cur, student_id).points == 0


def test_right_answer_with_a_named_mistake_is_still_right(cur, db_engine):
    # UNDERSTAND may name a mistake in the working; the student's own words are right too, so nothing changes.
    right = {**understood("answer", "2, 3"), "mistake": "sign_flip"}
    fake = FakeLLM(understand=[right])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "2 and 3")
    assert (result.decision.rule, result.grade.verdict) == ("R2", "correct")
    assert "answer_rewritten" not in result.issues


def test_llm_reteach_is_checked_against_the_next_question(cur, db_engine):
    concept = cur.concepts["factorisation"]
    next_check = concept.checks[1]                                          # 3x^2 - 5x - 2 = 0: -1/3 and 2
    gives_next_away = {"segments": [{"say": "The next one gives minus 1 by 3 and 2.", "board": []}]}
    worked = {"segments": [
        {"say": "Let's solve a different one together.",
         "board": [write("r1", "x^2 - 7x + 12 = 0", "Eq(x**2 - 7*x + 12, 0)", "new")]},
        {"say": "Minus 3 and minus 4 multiply to 12 and add to minus 7.",
         "board": [write("r2", "(x-3)(x-4) = 0", "Eq((x-3)*(x-4), 0)")]},
        {"say": "Each bracket can be zero, so we get 3 or 4.",
         "board": [write("r3", r"x = 3 \text{ or } x = 4", "Or(Eq(x, 3), Eq(x, 4))")]}]}
    fake = FakeLLM(understand=[understood("answer", "-2, -3")], respond=[gives_next_away, worked])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    result = tutor.turn(student_id, "minus 2 and minus 3")
    assert result.issues == ["respond_rejected:gives away the answer: says every value of it"]
    assert result.decision.strategy == "worked_example"
    assert factorisation_state(db_engine, cur, student_id).strategies_tried == ["worked_example"]
    assert [segment.say for segment in result.segments[:3]] == [s["say"] for s in worked["segments"]]
    assert result.segments[-1] == new_question(next_check, cur.formulas)
    assert "NEXT ANSWER KEY: -1/3, 2" in fake.asked[1][1][1]["content"]


def test_llm_needs_review_shows_worked_answer_then_moves_on(cur, db_engine):
    fake = FakeLLM(understand=[understood("answer", "-2, -3")], respond=[WORKED])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    with db_engine.begin() as conn:                                        # three reteaches already happened
        progress = db.load_progress(conn, student_id, cur)
        progress["factorisation"].reteach_count = 3
        db.save_progress(conn, student_id, progress, cur.version)
    result = tutor.turn(student_id, "minus 2 and minus 3")
    assert (result.decision.rule, result.decision.action) == ("R9", "needs_review")
    assert [segment.say for segment in result.segments[:2]] == [WORKED["segments"][0]["say"], REVIEW_LATER]
    assert result.issues == [] and result.decision.resume_concept_id == "quadratic_formula"
    assert factorisation_state(db_engine, cur, student_id).status == "needs_review"


def test_llm_needs_review_on_the_last_topic_says_we_come_back_once(cur, db_engine):
    """Nothing is left to teach: after the LLM's worked answer, only the END summary says what comes back."""
    fake = FakeLLM(understand=[understood("answer", "-2, -3")], respond=[WORKED])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    with db_engine.begin() as conn:                     # three reteaches already happened; the rest is mastered
        progress = db.load_progress(conn, student_id, cur)
        progress["factorisation"].reteach_count = 3
        for concept_id in cur.order[cur.order.index("factorisation") + 1:]:
            progress[concept_id].status, progress[concept_id].points = "mastered", POINTS_TO_MASTER
        db.save_progress(conn, student_id, progress, cur.version)
    result = tutor.turn(student_id, "minus 2 and minus 3")
    assert (result.decision.action, result.decision.resume_concept_id) == ("needs_review", None)
    says = [segment.say for segment in result.segments]
    assert says[0] == WORKED["segments"][0]["say"] and REVIEW_LATER not in says and result.issues == []
    assert says[-1] == "Next session we'll come back to solving by factorisation."


def test_after_a_topic_start_understand_still_reads_the_real_last_line(cur, db_engine):
    """Right after Start, UNDERSTAND gets the tutor's real last line (the offer: step 1 waits), and a reply it finds
    unclear says the intro and the offer again: no step is played yet."""
    fake = FakeLLM(understand=[understood("unclear")])
    tutor = Tutor(cur, db_engine, SETTINGS, llm=fake)
    with db_engine.begin() as conn:
        student_id = db.get_or_create_student(conn, "asha", "Asha")["id"]
    first = tutor.start(student_id)                       # standard form: greeting, intro, the offer
    result = tutor.turn(student_id, "hmm, which one")
    assert roles(fake) == ["understand"]
    offer = replies.cue_line("offer", "standard_form")
    assert first.segments[-1].say == offer
    assert f"TUTOR'S LAST LINE: {offer}" in fake.asked[0][1][1]["content"]
    assert (result.decision.rule, result.decision.action) == ("R8", "repeat")
    assert [segment.say for segment in result.segments] == [cur.concepts["standard_form"].intro, offer]


def test_got_it_test_me_not_sure_and_move_on_need_no_understand_call(cur, db_engine):
    """The logged reply "Got it." goes on to the next step, as "ok" does. "test me" skips to a question, "not sure"
    gets the step explained again or the question's next hint, and "move on" asks the open question again. None of
    them calls UNDERSTAND: RESPOND is called only for the explanation and the hint."""
    fake = FakeLLM(understand=[], respond=[HINT, HINT])
    tutor = Tutor(cur, db_engine, SETTINGS, llm=fake)
    student_id = new_student(db_engine, cur)
    with db_engine.begin() as conn:                       # Asha is on factorisation's second step
        progress = db.load_progress(conn, student_id, cur)
        progress["factorisation"].status, progress["factorisation"].step_id = "teaching", "split_middle"
        db.save_progress(conn, student_id, progress, cur.version)
    tutor.start(student_id)
    got_it = tutor.turn(student_id, "Got it.")
    assert (got_it.decision.rule, got_it.decision.play_step_id) == ("R0", "a_not_one")
    assert tutor.turn(student_id, "not sure").decision.rule == "R7"
    tested = tutor.turn(student_id, "test me").decision
    assert (tested.rule, tested.action, tested.ask_check_id) == ("R6", "start_check", "fa1")
    moved = tutor.turn(student_id, "move on")
    fa1 = cur.concepts["factorisation"].checks[0]
    assert moved.decision.rule == "R8" and [s.say for s in moved.segments] == ["Let's get this one first.", fa1.say]
    hinted = tutor.turn(student_id, "not sure").decision
    assert (hinted.rule, hinted.action) == ("R4", "hint")
    assert roles(fake) == ["respond", "respond"]


# ---------- spoken replies the tutor can't place: the mic may have misheard ----------
@pytest.mark.parametrize("kind, heard", [("unclear", "Кама майна."), ("off_topic", "Experience is the best teacher.")])
def test_spoken_reply_nobody_can_place_while_checking_may_be_misheard(cur, db_engine, kind, heard):
    """Speech-to-text can turn an answer into words nobody said. While a question is open, the tutor says it may
    have misheard, asks the question again and suggests typing. Nothing else changes."""
    tutor, student_id = at_first_question(cur, db_engine, FakeLLM(understand=[understood(kind)]))
    before = factorisation_state(db_engine, cur, student_id)
    result = tutor.turn(student_id, heard, input_mode="voice")
    fa1 = cur.concepts["factorisation"].checks[0]
    assert [segment.say for segment in result.segments] == [MISHEARD, fa1.say]
    assert (result.decision.rule, result.input_hint, result.transcript) == ("R8", "type", heard)
    assert factorisation_state(db_engine, cur, student_id) == before


def test_spoken_off_topic_reply_while_teaching_still_replays_the_step(cur, db_engine):
    """While teaching, a spoken reply nobody can place replays the step as before, and "say again" still works."""
    tutor = Tutor(cur, db_engine, SETTINGS, llm=FakeLLM(understand=[understood("off_topic")]))
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)                               # greeting, intro
    tutor.turn(student_id, "ok")                          # step 1 ("ok" is a fast path: no LLM call)
    step = cur.concepts["factorisation"].steps[0]
    step_1 = [segment.say for segment in step.segments] + [replies.cue_line("step", step.id)]
    heard = tutor.turn(student_id, "Experience is the best teacher.", input_mode="voice")
    again = tutor.turn(student_id, "say again")
    assert heard.decision.rule == "R8"
    assert [segment.say for segment in heard.segments] == step_1 == [segment.say for segment in again.segments]


def test_misheard_is_only_for_spoken_replies_nobody_can_place(cur, db_engine):
    """A typed reply, a spoken one UNDERSTAND can read (here Hindi), a spoken "say again" (the hint again) and a
    spoken safety reply are answered as before."""
    fake = FakeLLM(understand=[understood("off_topic"), understood("dont_know")], respond=[HINT])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    typed = tutor.turn(student_id, "can we play cricket later?")
    assert (typed.segments[0].say, typed.input_hint) == ("Let me ask that again.", None)
    hindi = tutor.turn(student_id, "मुझे नहीं पता", input_mode="voice")           # "I don't know"
    assert (hindi.decision.rule, hindi.decision.action) == ("R4", "hint")
    again = tutor.turn(student_id, "say again", input_mode="voice")
    assert [segment.say for segment in again.segments] == [segment.say for segment in hindi.segments]
    safe = tutor.turn(student_id, "I want to die", input_mode="voice")
    assert (safe.decision.rule, safe.segments[0].say, safe.input_hint) == ("R10", SAFETY_REPLY, None)
