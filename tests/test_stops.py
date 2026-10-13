"""Stopping the tutor part-way (Stop, Esc, or answering before it finishes): the server keeps where the student
stopped. "repeat" plays the whole reply again, "continue" the part not heard yet, and coming back later picks it up."""
from datetime import timedelta

import pytest
from conftest import check_of, make_settings, new_student, said_with_working, sign_in
from fastapi.testclient import TestClient

from app import board, curriculum, db, fallback, page, stops
from app.main import create_app
from app.replies import MISHEARD, SAFETY_REPLY
from app.tutor import Tutor


@pytest.fixture
def tutor(cur, db_engine):
    return Tutor(cur, db_engine, make_settings())


def says(result):
    return [segment.say for segment in result.segments]


def on_step_one(tutor, db_engine, cur):
    """A new student starts, then "teach me": step 1 of factorisation (three segments), then its cue."""
    student_id = new_student(db_engine, cur)
    started = tutor.start(student_id)
    return student_id, started, tutor.turn(student_id, "teach me")


def stop(tutor, student_id, result, heard, shown=None):
    """The page reports a stop: `heard` segments said in full, `shown` on screen (by default the same)."""
    assert stops.record(tutor.db_engine, tutor.cur, student_id, result.turn_id, heard,
                        heard if shown is None else shown)


def at_first_question(tutor, db_engine, cur):
    """A new student taught factorisation step by step up to its first question."""
    student_id = new_student(db_engine, cur)
    result = tutor.start(student_id)
    while not result.decision.ask_check_id:
        result = tutor.turn(student_id, "yes go on")
    return student_id, result


def credited(tutor, db_engine, cur):
    """The first question answered right with working: "Right! ..." then a new question, on a clean board."""
    student_id, asked = at_first_question(tutor, db_engine, cur)
    right = tutor.turn(student_id, said_with_working(check_of(cur, asked.decision.ask_check_id)))
    assert right.decision.rule == "R1" and right.decision.ask_check_id and len(right.segments) == 2
    return student_id, asked, right


def test_repeat_after_a_stop_plays_the_whole_reply_again_with_its_board(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 1)
    again = tutor.turn(student_id, "repeat")
    assert (again.decision.rule, again.decision.evidence["replay"]) == ("R8", "all")
    assert again.segments == step.segments          # the same words and board commands, its cue once
    assert again.choices == step.choices


def test_continue_after_a_stop_plays_the_rest(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 1)
    rest = tutor.turn(student_id, "continue")
    assert (rest.decision.rule, rest.decision.evidence["replay"]) == ("R8", "rest")
    assert rest.segments == step.segments[1:]
    assert rest.choices == step.choices


def test_without_a_stop_continue_still_goes_on(tutor, db_engine, cur):
    student_id, _, _ = on_step_one(tutor, db_engine, cur)
    assert tutor.turn(student_id, "continue").decision.action == "next_step"


def test_a_stop_after_the_last_segment_is_no_stop(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, len(step.segments))
    assert tutor.turn(student_id, "continue").decision.action == "next_step"


def test_any_other_reply_after_a_stop_moves_on(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 1)
    assert tutor.turn(student_id, "yes go on").decision.action == "next_step"


def test_the_board_goes_back_to_what_was_shown(tutor, db_engine, cur):
    """The lines of the part not heard never appeared, so a reload must not show them either."""
    student_id, started, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 1)
    with db_engine.connect() as conn:
        saved = db.session_board(conn, step.session_id)
    assert saved == board.apply(started.board, step.segments[:1], cur.formulas)
    assert len(saved) < len(step.board)


def test_only_the_students_own_newest_turn_can_be_stopped(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    with db_engine.begin() as conn:
        other = db.get_or_create_student(conn, "ravi", "Ravi")["id"]
    assert not stops.record(tutor.db_engine, tutor.cur, other, step.turn_id, 1, 1)
    assert not stops.record(tutor.db_engine, tutor.cur, student_id, "no-such-turn", 1, 1)
    tutor.turn(student_id, "yes go on")
    assert not stops.record(tutor.db_engine, tutor.cur, student_id, step.turn_id, 1, 1)    # a late stop: too old


def test_coming_back_soon_picks_up_where_it_stopped(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 2)
    back = tutor.start(student_id)                  # within the session gap: S1
    assert back.decision.rule == "S1"
    assert says(back)[1:] == says(step)[2:]         # "Welcome back! …", then the part not heard
    assert back.choices == step.choices


def test_a_new_session_replays_the_stopped_reply_from_its_start(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 2)
    with db_engine.begin() as conn:                 # the stopped turn is from yesterday
        conn.execute(db.turns_table.update().values(created_at=db._now() - timedelta(days=1)))
    back = tutor.start(student_id)
    assert back.decision.rule == "S0"
    assert says(back)[1:] == says(step)             # the greeting, then the whole stopped reply
    assert back.segments[1].board[0].op == "clear"  # a new session starts on a clean board,
    assert back.board == step.board                 # the board the reply began on is drawn again, then the reply


def test_a_stopped_welcome_is_not_picked_up_twice(tutor, db_engine, cur):
    """Stopping "Welcome back! …" and pressing Continue again gives the usual Continue: one greeting, no echo."""
    student_id, _, _ = on_step_one(tutor, db_engine, cur)
    back = tutor.start(student_id)
    stop(tutor, student_id, back, 0)
    again = tutor.start(student_id)
    assert again.decision.action == "resume" and says(again) == says(back)


def test_say_again_while_a_question_is_open_repeats_the_last_reply(tutor, db_engine, cur):
    """Not just the question: a hint or explanation just given is said again too, and the question only once."""
    student_id = new_student(db_engine, cur)
    result = tutor.start(student_id)
    while not result.decision.ask_check_id:         # teach me, then on through the steps to the first question
        result = tutor.turn(student_id, "yes go on")
    hint = tutor.turn(student_id, "I don't know")
    assert hint.decision.action == "hint"
    again = tutor.turn(student_id, "say again")
    assert again.decision.rule == "R8" and says(again) == says(hint)


def test_more_ways_to_ask_to_hear_it_again_need_no_ai():
    for text in ("say that again", "Repeat that", "can you repeat that?", "Could you repeat that?",
                 "what did you say?", "sorry, what did you say?", "please repeat", "say it again"):
        assert fallback.keyword(text, "teaching") == fallback.keyword(text, "checking") == "say_again", text


def test_which_replies_replay_a_stopped_reply():
    for text in ("continue", "Carry on", "resume", "keep going", "please continue"):
        assert stops.replay_kind(text) == "rest", text
    for text in ("repeat", "say again", "Pardon?", "say that again"):
        assert stops.replay_kind(text) == "all", text
    for text in ("2 and 3", "continue with the next question", "yes", "not sure"):
        assert stops.replay_kind(text) is None, text


def test_the_page_reports_a_stop_and_continue_plays_the_rest(cur):
    client = TestClient(create_app(make_settings(), cur=cur))
    sign_in(client)
    client.post("/api/session")
    step = client.post("/api/turn", json={"text": "teach me"}).json()
    assert client.post(f"/api/turn/{step['turn_id']}/stopped", json={"heard": 1, "shown": 2}).json() == {"ok": True}
    rest = client.post("/api/turn", json={"text": "continue"}).json()
    assert [s["say"] for s in rest["segments"]] == [s["say"] for s in step["segments"][1:]]
    assert client.get("/api/state").json()["choices"] == rest["choices"] == step["choices"]    # a reload too
    assert client.post("/api/turn/no-such-turn/stopped", json={"heard": 1, "shown": 1}).status_code == 404
    assert client.post(f"/api/turn/{step['turn_id']}/stopped", json={"heard": -1, "shown": 0}).status_code == 422


def test_a_stop_while_a_new_question_is_read_keeps_it_on_the_board(tutor, db_engine, cur):
    """Answering over the next question (it was shown, but not said to the end) keeps it on the saved board."""
    student_id, _, right = credited(tutor, db_engine, cur)
    stop(tutor, student_id, right, heard=1, shown=2)
    with db_engine.connect() as conn:
        assert db.session_board(conn, right.session_id) == right.board


def test_any_reply_after_a_stop_before_the_question_gets_the_question_first(tutor, db_engine, cur):
    """It can't be answered unseen: "not sure" (or anything) hears the rest of the reply, the question included."""
    student_id, _, right = credited(tutor, db_engine, cur)
    stop(tutor, student_id, right, heard=0, shown=1)        # stopped during "Right!": the question never showed
    rest = tutor.turn(student_id, "not sure")
    assert (rest.decision.rule, rest.decision.evidence["replay"]) == ("R8", "unseen")
    assert rest.segments == right.segments and rest.choices == right.choices
    assert tutor.turn(student_id, "not sure").decision.action == "hint"      # now it has been shown


@pytest.mark.parametrize("text, rule", [("I want to die", "R10"), ("skip to the quadratic formula", "S2")])
def test_safety_and_a_topic_asked_for_come_before_the_unseen_question(tutor, db_engine, cur, text, rule):
    student_id, _, right = credited(tutor, db_engine, cur)
    stop(tutor, student_id, right, heard=0, shown=1)
    assert tutor.turn(student_id, text).decision.rule == rule


def test_a_stopped_safety_reply_is_not_picked_up(tutor, db_engine, cur):
    """Continue after stopping the safety reply resumes the lesson as usual; it doesn't say "Welcome back" and then
    the safety reply's end."""
    student_id, _, _ = on_step_one(tutor, db_engine, cur)
    safe = tutor.turn(student_id, "I want to die")
    assert safe.decision.rule == "R10" and safe.segments[-1].say == SAFETY_REPLY
    stop(tutor, student_id, safe, 0)
    assert tutor.start(student_id).decision.action == "resume"


def test_a_new_session_that_restarts_a_topic_for_review_resumes_as_usual(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 1)
    with db_engine.begin() as conn:                 # roots is marked for review, and the turns are from yesterday
        progress = db.load_progress(conn, student_id, cur)
        progress["roots"].status = "needs_review"
        db.save_progress(conn, student_id, progress, cur.version)
        conn.execute(db.turns_table.update().values(created_at=db._now() - timedelta(days=1)))
    back = tutor.start(student_id)
    assert (back.decision.rule, back.decision.action, back.decision.resume_concept_id) == ("S0", "greet", "roots")


def test_a_pick_up_keeps_its_buttons_after_a_reload(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 1)
    back = tutor.start(student_id)
    live = page.payload(tutor, student_id, back)["choices"]
    assert live == page.state(tutor, student_id)["choices"] == [vars(choice) for choice in step.choices]


def test_stopping_the_rest_and_continuing_again(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 1)
    rest = tutor.turn(student_id, "continue")
    stop(tutor, student_id, rest, 1)
    assert tutor.turn(student_id, "continue").segments == step.segments[2:]


def test_a_topic_picked_after_a_stop_opens_as_usual(tutor, db_engine, cur):
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    stop(tutor, student_id, step, 1)
    assert tutor.jump(student_id, "roots").decision.rule == "S2"


def test_say_again_with_a_question_open_skips_greetings_and_apologies(tutor, db_engine, cur):
    """After Continue, a picked-up reply or a recording that failed, "say again" says the question (or the reply),
    not "Welcome back" or "Sorry, I didn't catch that" again."""
    student_id, asked = at_first_question(tutor, db_engine, cur)
    question = [curriculum.ask_segment(check_of(cur, asked.decision.ask_check_id), cur.formulas).say]
    tutor.start(student_id)                                                   # S1: welcome back, then the question
    assert says(tutor.turn(student_id, "say again")) == question
    tutor.turn(student_id, "", input_mode="voice", stt_failed=True)          # "Sorry, I didn't catch that", question
    assert says(tutor.turn(student_id, "say again")) == question
    hint = tutor.turn(student_id, "I don't know")
    stop(tutor, student_id, hint, 0)
    tutor.start(student_id)                                                   # welcome back, then the hint again
    assert says(tutor.turn(student_id, "say again")) == says(hint)


def test_an_answer_over_a_question_already_on_the_board_is_graded(tutor, db_engine, cur):
    """Continue asks the open question again, which is on the board all along: an answer typed during "Welcome
    back" is marked, not swallowed by a replay."""
    student_id, asked = at_first_question(tutor, db_engine, cur)
    back = tutor.start(student_id)                          # S1: welcome back, then the same question again
    stop(tutor, student_id, back, heard=0, shown=1)
    answer = tutor.turn(student_id, said_with_working(check_of(cur, asked.decision.ask_check_id)))
    assert answer.decision.rule == "R1"


def test_a_stopped_sorry_is_never_said_again(tutor, db_engine, cur):
    """Stopping "Sorry, I may have misheard you" and asking to hear it again gets the step, not the apology; Continue
    later resumes the lesson as usual."""
    student_id, _, step = on_step_one(tutor, db_engine, cur)
    sorry = tutor.turn(student_id, "hi", input_mode="voice")
    assert says(sorry)[0] == MISHEARD
    stop(tutor, student_id, sorry, 0)
    assert says(tutor.turn(student_id, "say again")) == says(step)
    stop(tutor, student_id, tutor.turn(student_id, "hi", input_mode="voice"), 0)
    assert tutor.start(student_id).decision.action == "resume"


def test_a_reply_nobody_can_place_after_the_safety_reply_hears_it_again(tutor, db_engine, cur):
    """No "I may have misheard you" in front of the safety reply, as if the reply were a disclosure: just the safety
    reply again, with no buttons, as before."""
    student_id, _, _ = on_step_one(tutor, db_engine, cur)
    tutor.turn(student_id, "I want to die")
    heard = tutor.turn(student_id, "hi", input_mode="voice")
    assert says(heard) == [SAFETY_REPLY] and heard.choices == []


def test_buttons_after_a_sorry_and_say_again_match_a_reload(tutor, db_engine, cur):
    """After a solved example, "sorry" then "say again" keep the example's check-in and buttons, live and after a
    reload."""
    student_id, _, _ = on_step_one(tutor, db_engine, cur)
    example = tutor.turn(student_id, "not sure")
    for text in ("hi", "say again"):
        result = tutor.turn(student_id, text)
        live = page.payload(tutor, student_id, result)["choices"]
        assert live == page.state(tutor, student_id)["choices"] == [vars(choice) for choice in example.choices], text
        assert says(result)[-1] == says(example)[-1], text
