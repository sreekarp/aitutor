"""app/tutor.py with the LLM (spec §5.2, §6.1, §7.2): a fake client plays UNDERSTAND and RESPOND."""
from conftest import FakeLLM

from app import curriculum, db
from app.config import POINTS_TO_MASTER, Settings
from app.fallback import REVIEW_LATER
from app.llm import ProviderError
from app.tutor import Tutor

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


def at_first_question(cur, db_engine, fake: FakeLLM) -> tuple[Tutor, str]:
    """Asha starts factorisation (earlier concepts mastered) and says "ok" through its steps: question fa1."""
    tutor = Tutor(cur, db_engine, SETTINGS, llm=fake)
    with db_engine.begin() as conn:
        student_id = db.get_or_create_student(conn, "asha", "Asha")["id"]
        progress = db.load_progress(conn, student_id, cur)
        for concept_id in ("standard_form", "roots"):
            progress[concept_id].status, progress[concept_id].points = "mastered", POINTS_TO_MASTER
        db.save_progress(conn, student_id, progress, cur.version)
    tutor.start(student_id)
    for _ in cur.concepts["factorisation"].steps:
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
    assert result.segments[-1].say == "So, what are the roots?"           # the engine's re-ask follows
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
    assert result.segments[-1] == curriculum.ask_segment(next_check, cur.formulas)
    assert "NEXT SECRET ANSWER (never reveal it): -1/3, 2" in fake.asked[1][1][1]["content"]


def test_llm_needs_review_shows_worked_answer_then_moves_on(cur, db_engine):
    worked = {"segments": [{"say": "This one is tricky, and that's fine. Here it is step by step.", "board": [
        write("w1", "(x-2)(x-3) = 0", "Eq((x-2)*(x-3), 0)"),
        write("w2", r"x = 2 \text{ or } x = 3", "Or(Eq(x, 2), Eq(x, 3))")]}]}
    fake = FakeLLM(understand=[understood("answer", "-2, -3")], respond=[worked])
    tutor, student_id = at_first_question(cur, db_engine, fake)
    with db_engine.begin() as conn:                                        # three reteaches already happened
        progress = db.load_progress(conn, student_id, cur)
        progress["factorisation"].reteach_count = 3
        db.save_progress(conn, student_id, progress, cur.version)
    result = tutor.turn(student_id, "minus 2 and minus 3")
    assert (result.decision.rule, result.decision.action) == ("R9", "needs_review")
    assert [segment.say for segment in result.segments[:2]] == [worked["segments"][0]["say"], REVIEW_LATER]
    assert result.issues == [] and result.decision.resume_concept_id == "quadratic_formula"
    assert factorisation_state(db_engine, cur, student_id).status == "needs_review"
