"""Cues and choices: every teaching turn ends on a short question ("Did that make sense? Shall we go on?") and
offers buttons that send words the fast path reads, so no reply is a dead end and no button needs a model call."""
from pathlib import Path

import pytest
from conftest import FakeLLM, make_settings, new_student, progress_of, sign_in
from fastapi.testclient import TestClient

from app import compose, curriculum, fallback, page, replies
from app.main import create_app
from app.models import Choice, ConceptState, Decision, Pending, Segment, TurnContext
from app.tutor import DIDNT_CATCH, Tutor

ROOT = Path(__file__).resolve().parents[1]
CUE_LINES = {line for lines in replies.CUES.values() for line in lines}
OFFER = [Choice("Teach me", "teach me"), Choice("Test me first", "test me")]
NOT_SURE = [Choice("Not sure", "not sure")]


@pytest.fixture
def tutor(cur, db_engine):
    return Tutor(cur, db_engine, make_settings())


def progress_at(cur, concept_id: str, **state) -> dict:
    """Progress with every concept before concept_id mastered, and concept_id in this state."""
    progress = {cid: ConceptState(cid) for cid in cur.order}
    for earlier in cur.order[:cur.order.index(concept_id)]:
        progress[earlier].status = "mastered"
    progress[concept_id] = ConceptState(concept_id, **state)
    return progress


def test_cue_line_keeps_one_phrasing_per_place(cur):
    steps = cur.concepts["factorisation"].steps
    assert all(2 <= len(lines) <= 3 for lines in replies.CUES.values())
    assert replies.CUES["offer"][0] == "Shall I teach it? Or say 'test me' if you already know it."
    assert replies.CUES["step"][0] == "Did that make sense? Shall we go on?"
    assert replies.CUES["last_step"][0] == "Ready for a question?"
    assert replies.CUES["example"][0] == "Shall we go on, or shall I do another example?"
    assert all("shall we go on" in line.lower() for line in replies.CUES["step"])
    assert {replies.cue_line("step", step.id) for step in steps} <= set(replies.CUES["step"])
    assert replies.cue_line("step", steps[0].id) == replies.CUES["step"][sum(map(ord, steps[0].id)) % 3]  # by id
    assert not [line for line in CUE_LINES if set(line) & set(curriculum.LATEX_CHARS + "=")]   # plain spoken words


def test_cue_for_follows_where_the_turn_ends(cur):
    concept = cur.concepts["factorisation"]
    first, last = concept.steps[0].id, concept.steps[-1].id
    cases = [(dict(status="teaching"), "greet", "offer", OFFER),
             (dict(status="teaching", step_id=first), "next_step", "step",
              [Choice("Yes, go on", "yes go on"), Choice("Not sure", "not sure")]),
             (dict(status="teaching", step_id=last), "next_step", "last_step",
              [Choice("Yes, quiz me", "yes quiz me"), Choice("Not sure", "not sure")]),
             (dict(status="teaching", step_id=first), "example", "example",
              [Choice("Go on", "go on"), Choice("Another example", "another example")])]
    for state, action, kind, choices in cases:
        cue, got = compose.cue_for(cur, progress_at(cur, "factorisation", **state), action)
        assert cue.say == replies.cue_line(kind, state.get("step_id") or "factorisation") and cue.board == [], kind
        assert got == choices, kind
    checking = progress_at(cur, "factorisation", status="checking", pending=Pending("fa1"), last_check_id="fa1")
    assert compose.cue_for(cur, checking, "start_check") == (None, NOT_SURE)        # the question is the cue
    done = {cid: ConceptState(cid, status="mastered") for cid in cur.order}
    assert compose.cue_for(cur, done, "mastered") == (None, [])


def test_every_choice_is_read_without_a_model_call(cur):
    steps = cur.concepts["factorisation"].steps
    reads = {"teach me": "next", "test me": "test_me", "yes go on": "next", "yes quiz me": "next",
             "go on": "next", "another example": "not_sure", "not sure": "not_sure"}
    places = [(dict(status="teaching"), "greet"), (dict(status="teaching", step_id=steps[0].id), "next_step"),
              (dict(status="teaching", step_id=steps[-1].id), "next_step"),
              (dict(status="teaching", step_id=steps[0].id), "example")]
    for state, action in places:
        for choice in compose.cue_for(cur, progress_at(cur, "factorisation", **state), action)[1]:
            assert fallback.keyword(choice.say, "teaching") == reads[choice.say], choice
    assert fallback.keyword("not sure", "checking") == "not_sure"


def test_safety_and_the_end_get_no_cue_or_choices(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    safe = tutor.turn(student_id, "my number is 9876543210")
    assert safe.decision.rule == "R10" and safe.choices == []
    assert [segment.say for segment in safe.segments] == [replies.SAFETY_REPLY]
    new_student(db_engine, cur, mastered=cur.order)              # the same Asha, now with every topic mastered
    end = tutor.turn(student_id, "ok")
    assert end.decision.rule == "END" and end.choices == []
    assert not {segment.say for segment in end.segments} & CUE_LINES


def test_no_dead_ends_in_any_topic(tutor, db_engine, cur):
    """Every real topic from its start, saying "ok" through every step: each teaching turn ends on its cue and has
    buttons; the turn that asks the first question ends on the question, with just "Not sure"."""
    student_id = new_student(db_engine, cur, mastered=[])
    for concept_id in cur.order:
        concept = cur.concepts[concept_id]
        opened = tutor.jump(student_id, concept_id)              # the topic start (the topic before is paused)
        assert opened.segments[-1].say == replies.cue_line("offer", concept_id) and opened.choices == OFFER
        for number, step in enumerate(concept.steps, 1):
            result = tutor.turn(student_id, "ok")
            kind = "last_step" if number == len(concept.steps) else "step"
            assert result.decision.play_step_id == step.id, step.id
            assert result.segments[-1].say == replies.cue_line(kind, step.id), step.id
            assert len(result.choices) == 2 and result.choices[-1] == NOT_SURE[0], step.id
        asked = tutor.turn(student_id, "ok")
        check = curriculum.get_check(concept, asked.decision.ask_check_id)
        assert asked.decision.action == "start_check" and asked.segments[-1].say == check.say, concept_id
        assert asked.choices == NOT_SURE, concept_id


def test_say_again_replays_the_cue_once(tutor, db_engine, cur):
    """"Say again" at the topic start gives the intro and the offer; after a step, the step and its cue: never the
    cue twice. A lone "didn't catch that" adds no cue (it asks for the reply again) but keeps the buttons."""
    student_id = new_student(db_engine, cur)
    concept = cur.concepts["factorisation"]
    tutor.start(student_id)
    again = tutor.turn(student_id, "say again")
    assert [segment.say for segment in again.segments] == [concept.intro, replies.cue_line("offer", "factorisation")]
    step_1 = tutor.turn(student_id, "teach me")
    cue = replies.cue_line("step", concept.steps[0].id)
    assert [segment.say for segment in step_1.segments] == [s.say for s in concept.steps[0].segments] + [cue]
    for _ in range(2):                                           # a replay of a replay: still one cue
        again = tutor.turn(student_id, "say again")
        assert [segment.say for segment in again.segments] == [segment.say for segment in step_1.segments]
    sorry = tutor.turn(student_id, "", input_mode="voice", stt_failed=True)
    assert [segment.say for segment in sorry.segments] == [DIDNT_CATCH] and sorry.choices
    after_sorry = tutor.turn(student_id, "say again")
    assert [segment.say for segment in after_sorry.segments] == [segment.say for segment in step_1.segments]


def test_say_again_after_a_lone_apology_at_a_topic_start_hears_the_start(tutor, db_engine, cur):
    """A failed recording at the topic start gets just "didn't catch that", with the offer's buttons; "say again"
    then hears the intro and the offer, not the apology."""
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    sorry = tutor.turn(student_id, "", input_mode="voice", stt_failed=True)
    assert [segment.say for segment in sorry.segments] == [DIDNT_CATCH] and sorry.choices == OFFER
    again = tutor.turn(student_id, "say again")
    intro = cur.concepts["factorisation"].intro
    assert [segment.say for segment in again.segments] == [intro, replies.cue_line("offer", "factorisation")]
    assert again.choices == OFFER
    assert progress_of(db_engine, cur, student_id)["factorisation"].step_id is None      # still at the start


def test_continue_at_a_topic_start_ends_on_the_offer(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    again = tutor.start(student_id)                              # Continue (S1), still at the topic start
    assert again.decision.rule == "S1"
    assert [segment.say for segment in again.segments] == [
        "Welcome back! Let's carry on with solving by factorisation from where we left off.",
        replies.cue_line("offer", "factorisation")]
    assert again.choices == OFFER


def test_a_choice_needs_no_model_call(cur, db_engine):
    """With the LLM on, each button the tutor offers is read by the fast path: the fake client's queues are empty."""
    fake = FakeLLM(understand=[], respond=[])
    tutor = Tutor(cur, db_engine, make_settings(openrouter_api_key="test-key", llm_mode="auto"), llm=fake)
    student_id = new_student(db_engine, cur)
    result = tutor.start(student_id)
    for _ in cur.concepts["factorisation"].steps:                # teach me, then "yes go on" up to the last step
        result = tutor.turn(student_id, result.choices[0].say)
        assert (result.decision.rule, result.decision.action) == ("R0", "next_step")
    asked = tutor.turn(student_id, result.choices[0].say)        # "yes quiz me"
    assert asked.decision.action == "start_check" and fake.asked == []
    assert progress_of(db_engine, cur, student_id)["factorisation"].status == "checking"


def test_the_page_gets_the_choices_and_rebuilds_them_on_reload(cur):
    client = TestClient(create_app(make_settings(), cur=cur))
    sign_in(client)
    assert client.get("/api/state").json()["choices"] == []      # before Start: nothing to choose
    offer = [{"label": "Teach me", "say": "teach me"}, {"label": "Test me first", "say": "test me"}]
    assert client.post("/api/session").json()["choices"] == offer
    assert client.get("/api/state").json()["choices"] == offer   # a reload draws the same buttons
    step_1 = client.post("/api/turn", json={"text": "teach me"}).json()
    assert step_1["choices"] == [{"label": "Yes, go on", "say": "yes go on"}, {"label": "Not sure", "say": "not sure"}]
    assert client.get("/api/state").json()["choices"] == step_1["choices"]


def test_say_again_keeps_the_buttons_and_so_does_a_reload(cur):
    """The buttons match the cue last spoken: a "say again" at the topic start or on a step keeps them, and a reload
    right after it draws them again."""
    client = TestClient(create_app(make_settings(), cur=cur))
    sign_in(client)
    offer = client.post("/api/session").json()["choices"]
    assert client.post("/api/turn", json={"text": "say again"}).json()["choices"] == offer
    assert client.get("/api/state").json()["choices"] == offer
    step = client.post("/api/turn", json={"text": "teach me"}).json()["choices"]
    assert client.post("/api/turn", json={"text": "say again"}).json()["choices"] == step
    assert client.get("/api/state").json()["choices"] == step


def test_a_replay_keeps_the_buttons_of_the_cue_it_replays(cur, tutor):
    """"Say again" replays speech that ends on a cue, so it shows that cue's buttons: after a solved example, "go on
    or another example". A reload reads the newest decision that wasn't a replay. (By hand: no example turn here.)"""
    step_id = cur.concepts["factorisation"].steps[0].id
    on_step = progress_at(cur, "factorisation", status="teaching", step_id=step_id)
    example_cue, step_cue = (Segment(say=replies.cue_line(kind, step_id)) for kind in ("example", "step"))
    ctx = TurnContext(student={"first_name": "Asha"}, session={}, concept=cur.concepts["factorisation"],
                      phase="teaching", before=on_step, check=None, text="say again",
                      last_speech=[Segment(say="A solved example."), example_cue], previous_reason="", board=[],
                      deadline=0.0, calls=[], issues=[], latency={})
    repeat = Decision("R8", "repeat", "factorisation", "test")
    assert compose.shown_action(ctx, repeat) == "example"
    assert compose.shown_action(ctx, Decision("R0", "next_step", "factorisation", "test")) == "next_step"
    ctx.last_speech = [step_cue]
    assert compose.shown_action(ctx, repeat) == "repeat"
    recent = [{"rule": "R8", "action": "repeat"}, {"rule": "R8", "action": "repeat"},
              {"rule": "R7", "action": "example"}]                 # newest first
    assert page._saved_choices(tutor, on_step, recent) == [{"label": "Go on", "say": "go on"},
                                                           {"label": "Another example", "say": "another example"}]


def test_buttons_drawn_while_a_turn_runs_start_off():
    """Tapping a button while the tutor still speaks does nothing. The buttons sit in the say form, so setBusy turns
    them off and on with the text box; each one is drawn off while a turn runs; a click while busy is ignored.
    (A source check: the muted browser run clicks them for real.)"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    form = html[html.index('<form id="say-form"'):html.index("</form>", html.index('<form id="say-form"'))]
    assert '<div id="choices" class="choices"></div>' in form
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    draw = js[js.index("function drawChoices("):js.index("function onChoice(")]
    click = js[js.index("function onChoice("):]
    assert "button.disabled = busy;" in draw and 'button.type = "button";' in draw
    assert "if (!button || busy) return;" in click[:click.index("\n}")]
