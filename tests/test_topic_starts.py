"""Whole turns as a session or topic starts, in scripted mode: the greeting or move-on line, the topic's intro,
then its step. "Say again" right after a topic starts replays just the step: not the greeting, intro or praise."""
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_settings, new_student, progress_of, said_with_working

from app import curriculum, db
from app.config import SESSION_GAP_MIN
from app.models import ConceptState, Pending
from app.tutor import Tutor

RIGHT_B = "minus 7 because I moved every term"     # the answer to sf2 ("what is b?"), with working


@pytest.fixture
def tutor(cur, db_engine):
    return Tutor(cur, db_engine, make_settings())


def sf2_open(db_engine, cur, *, mastered=(), points=0.0, hint_level=0, reteach_count=0):
    """A student whose open question is standard form's sf2, with these points, hints used and reteaches.
    The concepts in `mastered` are mastered; the others are not started."""
    student_id = new_student(db_engine, cur, mastered=list(mastered))
    with db_engine.begin() as conn:
        progress = db.load_progress(conn, student_id, cur)
        progress["standard_form"] = ConceptState("standard_form", status="checking", points=points,
                                                 pending=Pending("sf2", hint_level=hint_level),
                                                 last_check_id="sf2", reteach_count=reteach_count)
        db.save_progress(conn, student_id, progress, cur.version)
    return student_id


def test_session_start_greets_then_opens_the_concept(tutor, db_engine, cur):
    """A new session from step 1: greeting, intro, step 1. Continue (S1) within 30 minutes, and a new session
    part-way through, say "Welcome back" and carry on with the step: no intro."""
    student_id = new_student(db_engine, cur)
    concept = cur.concepts["factorisation"]
    first = tutor.start(student_id)
    assert [segment.say for segment in first.segments] == [
        "Hi Asha! Let's start today with solving by factorisation.", concept.intro,
        *[segment.say for segment in concept.steps[0].segments]]
    tutor.turn(student_id, "ok")                                 # now on step 2
    step_2 = [segment.say for segment in concept.steps[1].segments]
    carry_on = "Let's carry on with solving by factorisation from where we left off."
    again = tutor.start(student_id)                              # Continue within 30 minutes: S1
    assert [segment.say for segment in again.segments] == [f"Welcome back! {carry_on}", *step_2]
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=SESSION_GAP_MIN + 1)
    with db_engine.begin() as conn:
        conn.execute(db.turns_table.update().values(created_at=long_ago))
    later = tutor.start(student_id)                              # a new session part-way through: S0
    assert later.decision.rule == "S0"
    assert [segment.say for segment in later.segments] == [f"Welcome back, Asha! {carry_on}", *step_2]


def test_start_twice_is_one_session(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    first = tutor.start(student_id)
    tutor.turn(student_id, "ok")                                 # now on step 2
    again = tutor.start(student_id)                              # a second click, or a page reload
    assert again.decision.rule == "S1" and again.session_id == first.session_id
    with db_engine.connect() as conn:
        assert db.latest_session(conn, student_id)["session_no"] == 1
    step = cur.concepts["factorisation"].steps[1]
    assert progress_of(db_engine, cur, student_id)["factorisation"].step_id == step.id
    says = [segment.say for segment in again.segments]
    assert says[-len(step.segments):] == [segment.say for segment in step.segments]   # the current step again
    ids = [line.id for line in again.board]
    assert len(ids) == len(set(ids))                             # replayed lines replace themselves


def test_new_session_after_gap_runs_s0(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    first = tutor.start(student_id)
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=SESSION_GAP_MIN + 1)
    with db_engine.begin() as conn:
        conn.execute(db.turns_table.update().values(created_at=long_ago))
    again = tutor.start(student_id)
    assert again.decision.rule == "S0" and again.session_id != first.session_id
    with db_engine.connect() as conn:
        assert db.latest_session(conn, student_id)["session_no"] == 2
    assert "Asha" in " ".join(segment.say for segment in again.segments)    # greeted by first name


def test_review_pass_goes_back_to_the_open_question(tutor, db_engine, cur):
    """Mastering the concept under review goes back to the next concept's open question, and says so first."""
    student_id = new_student(db_engine, cur, mastered=[])
    with db_engine.begin() as conn:            # standard form is being reviewed; roots still has a question open
        progress = db.load_progress(conn, student_id, cur)
        progress["standard_form"] = ConceptState("standard_form", status="checking", points=1.0,
                                                 pending=Pending("sf2"), last_check_id="sf2")
        progress["roots"] = ConceptState("roots", status="checking", pending=Pending("rt2"), last_check_id="rt2")
        db.save_progress(conn, student_id, progress, cur.version)
    tutor.start(student_id)
    result = tutor.turn(student_id, said_with_working(curriculum.get_check(cur.concepts["standard_form"], "sf2")))
    assert (result.decision.rule, result.decision.action) == ("R1", "mastered")
    says = [segment.say for segment in result.segments]
    assert says[0].endswith("! You've mastered standard form and recognising a quadratic.")
    assert says[1] == "Let's get back to roots and checking by substitution, where we left off."
    rt2 = curriculum.get_check(cur.concepts["roots"], "rt2")
    assert result.segments[2:] == [curriculum.ask_segment(rt2, cur.formulas)]     # then its question again


def test_say_again_after_a_topic_start_replays_just_the_step(tutor, db_engine, cur):
    """Start, then "say again": step 1's speech, without its board commands; not the greeting and intro again.
    The same after Continue, and for a reply the tutor can't read, like "hi"."""
    student_id = new_student(db_engine, cur)
    steps = cur.concepts["factorisation"].steps
    first = tutor.start(student_id)                              # greeting, intro, step 1
    again = tutor.turn(student_id, "say again")
    assert again.decision.rule == "R8"
    assert [segment.say for segment in again.segments] == [segment.say for segment in steps[0].segments]
    assert all(segment.board == [] for segment in again.segments) and again.board == first.board
    tutor.turn(student_id, "ok")                                 # step 2
    tutor.start(student_id)                                      # Continue: "Welcome back! …", then step 2
    unclear = tutor.turn(student_id, "hi")
    assert unclear.decision.rule == "R8"
    assert [segment.say for segment in unclear.segments] == [segment.say for segment in steps[1].segments]


@pytest.mark.parametrize("state, reply, action", [
    ({"points": 1.0}, RIGHT_B, "mastered"),                                   # R1: the second point
    ({"hint_level": 3, "reteach_count": 3}, "I don't know", "needs_review"),  # R9: no hints or reteaches left
], ids=["mastered", "needs_review"])
def test_say_again_after_moving_on_replays_just_the_new_step(tutor, db_engine, cur, state, reply, action):
    """Standard form ends and roots begins. "Say again" then gives just roots' step 1: not the praise or the
    worked answer, and not the move-on line and roots' intro."""
    student_id = sf2_open(db_engine, cur, **state)
    tutor.start(student_id)
    moved_on = tutor.turn(student_id, reply)
    assert (moved_on.decision.action, moved_on.decision.resume_concept_id) == (action, "roots")
    again = tutor.turn(student_id, "say again")
    assert again.decision.rule == "R8"
    step_1 = cur.concepts["roots"].steps[0].segments
    assert [segment.say for segment in again.segments] == [segment.say for segment in step_1]


def test_say_again_after_any_other_reply_replays_that_reply(tutor, db_engine, cur):
    """Not right after a topic start, the last reply is said again as it was: a step, or an answered question."""
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    for text in ("ok", "why is that?"):           # R0: step 2. R7: "Good question …", then step 2 again
        reply = tutor.turn(student_id, text)
        again = tutor.turn(student_id, "say again")
        assert again.decision.rule == "R8", text
        assert [segment.say for segment in again.segments] == [segment.say for segment in reply.segments], text


def test_the_last_mastery_says_mastered_once(tutor, db_engine, cur):
    """Nothing is left to teach, so the praise leaves "You've mastered …" to the END summary right after it."""
    student_id = sf2_open(db_engine, cur, mastered=cur.order[1:], points=1.0)     # all but standard form
    tutor.start(student_id)
    result = tutor.turn(student_id, RIGHT_B)
    assert (result.decision.action, result.decision.resume_concept_id) == ("mastered", None)
    assert [segment.say for segment in result.segments] == [
        "Nice work!", "That's everything for now, Asha! You've mastered standard form and recognising a quadratic, "
        "roots and checking by substitution, solving by factorisation, the quadratic formula, the discriminant and "
        "the nature of roots and word problems."]


def test_the_last_needs_review_says_we_come_back_once(tutor, db_engine, cur):
    """Nothing is left to teach, so after the worked answer only the END summary says what comes back next session."""
    student_id = sf2_open(db_engine, cur, mastered=cur.order[1:], hint_level=3, reteach_count=3)
    tutor.start(student_id)
    result = tutor.turn(student_id, "I don't know")              # no hints or reteaches left: R9
    assert (result.decision.action, result.decision.resume_concept_id) == ("needs_review", None)
    sf2 = curriculum.get_check(cur.concepts["standard_form"], "sf2")
    assert [segment.say for segment in result.segments] == [
        f"{sf2.hints[-1]} Here is the answer.",
        "That's everything for now, Asha! You've mastered roots and checking by substitution, solving by "
        "factorisation, the quadratic formula, the discriminant and the nature of roots and word problems.",
        "Next session we'll come back to standard form and recognising a quadratic."]


def test_picking_a_topic_pauses_this_one_and_comes_back_to_it(tutor, db_engine, cur):
    """S2: the open question waits, with its hint, while another topic runs; picking it again asks it again."""
    student_id = sf2_open(db_engine, cur, points=1.0, hint_level=1)
    tutor.start(student_id)
    picked = tutor.jump(student_id, "discriminant")
    concept = cur.concepts["discriminant"]
    assert (picked.decision.rule, picked.decision.action) == ("S2", "jump")
    assert [segment.say for segment in picked.segments] == [
        "Sure. Standard form and recognising a quadratic is saved just where you left it.",
        f"Now let's move on to the discriminant and the nature of roots. {concept.intro}",
        *[segment.say for segment in concept.steps[0].segments]]
    assert picked.segments[1].board[0].op == "clear"            # wiped as the picked topic opens
    saved = progress_of(db_engine, cur, student_id)
    assert saved["standard_form"] == ConceptState("standard_form", status="paused", points=1.0,
                                                  pending=Pending("sf2", hint_level=1), last_check_id="sf2")
    assert saved["discriminant"].status == "teaching"
    back = tutor.jump(student_id, "standard_form")
    sf2 = curriculum.get_check(cur.concepts["standard_form"], "sf2")
    assert back.segments[1].say == "Let's get back to standard form and recognising a quadratic, where we left off."
    assert back.segments[-1].say == sf2.say
    assert progress_of(db_engine, cur, student_id)["standard_form"].status == "checking"


def test_picking_a_topic_before_start_opens_a_session(tutor, db_engine, cur):
    student_id = new_student(db_engine, cur)
    picked = tutor.jump(student_id, "word_problems")
    assert picked.decision.rule == "S2" and picked.decision.concept_id == "word_problems"
    assert tutor.turn(student_id, "ok").decision.concept_id == "word_problems"       # turns now work
