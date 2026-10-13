"""Whole turns for a solved example while teaching: a student not sure about a step sees a similar equation solved
on the board (the example action), with the AI and without it. An example changes no progress, so it is never a
reteach."""
from conftest import FakeLLM, make_settings, new_student, progress_of, sign_in
from fastapi.testclient import TestClient

from app import curriculum, db, replies
from app.config import Settings
from app.main import create_app
from app.tutor import Tutor

SETTINGS = Settings(openrouter_api_key="test-key", access_code="test", session_secret="test",
                    database_url="sqlite://", llm_mode="auto", max_turns_per_day=1000, tts_voice="sarah",
                    stt_language="en", https_only=False)
NOT_SURE = {"type": "not_sure", "answer": "", "showed_working": False, "mistake": None, "safety": False}
REJECTED = "respond_rejected:line e1: a solved example can't use a lesson question as its example"


def write(line_id: str, latex: str, check: str, relation: str = "equiv") -> dict:
    """One board write command, as RESPOND returns it."""
    return {"op": "write", "id": line_id, "latex": latex, "check": check, "relation": relation}


# RESPOND's solved example for "split the middle term": a new equation, not one of the lesson's questions.
EXAMPLE = {"segments": [
    {"say": "Here's another one. For x squared minus 9x plus 20, find two numbers that multiply to 20 and add "
            "to minus 9.", "board": [{"op": "write", "id": "e0", "text": "Another example"},
                                     write("e1", "x^2 - 9x + 20 = 0", "Eq(x**2 - 9*x + 20, 0)", "new")]},
    {"say": "Minus 4 and minus 5 work. Split the middle term, then take out the common factor.",
     "board": [write("e2", "x^2 - 4x - 5x + 20 = 0", "Eq(x**2 - 4*x - 5*x + 20, 0)"),
               write("e3", "(x-4)(x-5) = 0", "Eq((x-4)*(x-5), 0)")]}]}
# The same move on a lesson question (fa1): rejected, as solving it would give that question's answer away.
USES_FA1 = {"segments": [{"say": "Here's one like it.", "board": [
    write("e1", "x^2 - 5x + 6 = 0", "Eq(x**2 - 5*x + 6, 0)", "new")]}]}
# Another topic's question (the quadratic formula's qf4): solved here, its answer would be known before it is asked.
USES_QF4 = {"segments": [{"say": "Here's one like it.", "board": [
    write("e1", "x^2 - 7x + 10 = 0", "Eq(x**2 - 7*x + 10, 0)", "new")]}]}


def on_split_middle(cur, db_engine, llm=None) -> tuple[Tutor, str]:
    """Asha is on factorisation's second step, split the middle term, and has just pressed Start. Without an llm
    the tutor is scripted."""
    tutor = Tutor(cur, db_engine, SETTINGS, llm=llm)
    student_id = new_student(db_engine, cur)
    with db_engine.begin() as conn:
        progress = db.load_progress(conn, student_id, cur)
        progress["factorisation"].status, progress["factorisation"].step_id = "teaching", "split_middle"
        db.save_progress(conn, student_id, progress, cur.version)
    tutor.start(student_id)
    return tutor, student_id


def says(result) -> list[str]:
    return [segment.say for segment in result.segments]


def test_not_sure_on_a_step_gets_a_solved_example_from_the_ai(cur, db_engine):
    fake = FakeLLM(understand=[NOT_SURE], respond=[EXAMPLE])
    tutor, student_id = on_split_middle(cur, db_engine, fake)
    before = progress_of(db_engine, cur, student_id)
    result = tutor.turn(student_id, "I'm not sure I follow that")
    assert [role for role, _ in fake.asked] == ["understand", "respond"]
    assert (result.decision.rule, result.decision.action) == ("R7", "example") and result.issues == []
    assert says(result)[:-1] == [segment["say"] for segment in EXAMPLE["segments"]]
    assert says(result)[-1] in replies.CUES["example"]                  # then: go on, or another example?
    assert [choice.label for choice in result.choices] == ["Go on", "Another example"]
    assert [line.id for line in result.board][-4:] == ["e0", "e1", "e2", "e3"]
    assert progress_of(db_engine, cur, student_id) == before            # the same step, and not a reteach
    facts = fake.asked[1][1][1]["content"]                              # RESPOND's facts for this turn
    step = curriculum.get_step(cur.concepts["factorisation"], "split_middle")
    assert "ACTION: example" in facts and "PENDING QUESTION" not in facts
    assert all(segment.say in facts for segment in step.segments)        # the step, part by part
    assert all(check.equation in facts for check in cur.concepts["factorisation"].checks)  # NEVER USE AS EXAMPLES


def test_the_not_sure_button_skips_understand_and_a_rejected_example_goes_over_the_step(cur, db_engine):
    """The Not sure button sends "not sure", read without a model call. An example on a lesson question is rejected
    twice, so the scripted words go over the step again, still followed by the go on / another example choice."""
    fake = FakeLLM(respond=[USES_FA1, USES_FA1])
    tutor, student_id = on_split_middle(cur, db_engine, fake)
    result = tutor.turn(student_id, "not sure")
    assert [role for role, _ in fake.asked] == ["respond", "respond"]
    assert result.issues == [REJECTED, REJECTED, "respond_fallback"]
    step = curriculum.get_step(cur.concepts["factorisation"], "split_middle")
    assert says(result)[:-1] == ["Let's go over that step again."] + [segment.say for segment in step.segments]
    assert says(result)[-1] in replies.CUES["example"]
    assert [choice.say for choice in result.choices] == ["go on", "another example"]


def test_scripted_examples_keep_the_step_until_go_on(cur, db_engine):
    """Scripted: "not sure" and "another example" go over the step again and use up no reteach; "go on" moves on."""
    tutor, student_id = on_split_middle(cur, db_engine)
    for reply in ("not sure", "another example"):
        result = tutor.turn(student_id, reply)
        assert (result.decision.rule, result.decision.action) == ("R7", "example"), reply
        assert says(result)[0] == "Let's go over that step again.", reply
        assert says(result)[-1] in replies.CUES["example"], reply
    state = progress_of(db_engine, cur, student_id)["factorisation"]
    assert (state.step_id, state.reteach_count) == ("split_middle", 0)
    result = tutor.turn(student_id, "go on")
    assert (result.decision.rule, result.decision.play_step_id) == ("R0", "a_not_one")


def test_say_again_and_a_reload_after_an_example_keep_its_cue_and_buttons(cur):
    """Scripted, through the app: "say again" after a solved example replays it with one example cue (not two) and
    the example's buttons, and a reload draws the same buttons."""
    client = TestClient(create_app(make_settings(), cur=cur))
    sign_in(client)
    client.post("/api/session")
    client.post("/api/turn", json={"text": "teach me"})
    example = client.post("/api/turn", json={"text": "not sure"}).json()
    buttons = [{"label": "Go on", "say": "go on"}, {"label": "Another example", "say": "another example"}]
    assert (example["decision"]["action"], example["choices"]) == ("example", buttons)
    again = client.post("/api/turn", json={"text": "say again"}).json()
    cues = [segment["say"] for segment in again["segments"] if segment["say"] in replies.CUES["example"]]
    assert len(cues) == 1 and again["segments"][-1]["say"] == cues[0]
    assert again["choices"] == buttons
    assert client.get("/api/state").json()["choices"] == buttons


def test_a_solved_example_never_uses_another_topics_question(cur, db_engine):
    """Every topic's questions are off limits as examples, not only this topic's: the facts list them all, and an
    example on one is rejected (twice here, so the scripted words go over the step)."""
    fake = FakeLLM(respond=[USES_QF4, USES_QF4])
    tutor, student_id = on_split_middle(cur, db_engine, fake)
    result = tutor.turn(student_id, "not sure")
    assert result.issues == [REJECTED, REJECTED, "respond_fallback"]
    facts = fake.asked[0][1][1]["content"]
    assert all(check.equation in facts for concept in cur.concepts.values() for check in concept.checks)
