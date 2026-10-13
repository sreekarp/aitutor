"""Whole turns as a session or topic starts, in scripted mode: the greeting or move-on line, the topic's intro and
the offer; step 1 waits for the student's reply. "Say again" right after Continue replays just the step and its cue:
not the greeting."""
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeLLM, make_settings, new_student, progress_of, said_with_working

from app import curriculum, db, replies
from app.config import SESSION_GAP_MIN
from app.models import ConceptState, Pending
from app.tutor import DIDNT_CATCH, DIDNT_FOLLOW, MISHEARD, Tutor

WITH_LLM = make_settings(openrouter_api_key="test-key", llm_mode="auto")   # a (fake) key: replies go to the LLM
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
    """A new student: greeting, intro and the offer, and step 1 waits for a reply. Continue (S1) within 30 minutes,
    and a new session part-way through, say "Welcome back" and carry on with the step and its cue: no intro."""
    student_id = new_student(db_engine, cur)
    concept = cur.concepts["factorisation"]
    first = tutor.start(student_id)
    assert [segment.say for segment in first.segments] == [
        "Hi Asha! Let's start today with solving by factorisation.", concept.intro,
        replies.cue_line("offer", "factorisation")]
    step_1 = tutor.turn(student_id, "ok")
    assert [segment.say for segment in step_1.segments] == [
        *[segment.say for segment in concept.steps[0].segments], replies.cue_line("step", concept.steps[0].id)]
    tutor.turn(student_id, "ok")                                 # now on step 2
    step_2 = [segment.say for segment in concept.steps[1].segments] + [replies.cue_line("step", concept.steps[1].id)]
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
    for _ in range(2):                                           # step 1, then step 2
        tutor.turn(student_id, "ok")
    again = tutor.start(student_id)                              # a second click, or a page reload
    assert again.decision.rule == "S1" and again.session_id == first.session_id
    with db_engine.connect() as conn:
        assert db.latest_session(conn, student_id)["session_no"] == 1
    step = cur.concepts["factorisation"].steps[1]
    assert progress_of(db_engine, cur, student_id)["factorisation"].step_id == step.id
    says = [segment.say for segment in again.segments]
    assert says[-len(step.segments) - 1:] == [*[segment.say for segment in step.segments],   # the step again,
                                              replies.cue_line("step", step.id)]           # then its cue
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


def test_say_again_at_a_topic_start_and_after_continue(tutor, db_engine, cur):
    """Start, then "say again": the intro and the offer are said again (on a step: the step and its cue), without
    board commands, and no step is played yet. After Continue on a step, a reply the tutor can't read, like "hi",
    gets "Sorry, I didn't follow that." and just the step's cue: not the greeting."""
    student_id = new_student(db_engine, cur)
    steps = cur.concepts["factorisation"].steps
    first = tutor.start(student_id)                              # greeting, intro
    again = tutor.turn(student_id, "say again")
    assert (again.decision.rule, again.decision.action) == ("R8", "repeat")
    assert [segment.say for segment in again.segments] == [cur.concepts["factorisation"].intro,
                                                           replies.cue_line("offer", "factorisation")]
    assert progress_of(db_engine, cur, student_id)["factorisation"].step_id is None      # still at the start
    assert all(segment.board == [] for segment in again.segments) and again.board == first.board
    for _ in range(2):                                           # step 1, then step 2
        tutor.turn(student_id, "ok")
    tutor.start(student_id)                                      # Continue: "Welcome back! …", then step 2
    unclear = tutor.turn(student_id, "hi")
    assert unclear.decision.rule == "R8"
    assert [segment.say for segment in unclear.segments] == [DIDNT_FOLLOW, replies.cue_line("step", steps[1].id)]


def test_a_reply_nobody_can_read_at_a_topic_start_never_begins_step_1(tutor, db_engine, cur):
    """A spoken reply nobody can place hears "Sorry, I may have misheard you" and the offer again, and a failed
    recording gets "Sorry, I didn't catch that". Either way the topic stays at its start; only a reply that can be
    read begins step 1."""
    student_id = new_student(db_engine, cur)
    concept = cur.concepts["factorisation"]
    first = tutor.start(student_id)                              # greeting, intro, the offer
    heard = tutor.turn(student_id, "hi", input_mode="voice")     # a spoken reply the tutor can't place
    assert (heard.decision.rule, heard.decision.action) == ("R8", "repeat")
    assert [segment.say for segment in heard.segments] == [MISHEARD, first.segments[-1].say]   # not the intro again
    assert progress_of(db_engine, cur, student_id)["factorisation"].step_id is None
    sorry = tutor.turn(student_id, "", input_mode="voice", stt_failed=True)
    assert (sorry.decision.rule, sorry.decision.action, sorry.input_hint) == ("R8", "repeat", "type")
    assert [segment.say for segment in sorry.segments] == [DIDNT_CATCH]
    assert progress_of(db_engine, cur, student_id)["factorisation"].step_id is None
    step_1 = tutor.turn(student_id, "ok")
    assert [segment.say for segment in step_1.segments] == [
        *[segment.say for segment in concept.steps[0].segments], replies.cue_line("step", concept.steps[0].id)]


@pytest.mark.parametrize("state, reply, action", [
    ({"points": 1.0}, RIGHT_B, "mastered"),                                   # R1: the second point
    ({"hint_level": 3, "reteach_count": 3}, "I don't know", "needs_review"),  # R9: no hints or reteaches left
], ids=["mastered", "needs_review"])
def test_moving_on_opens_the_next_topic_at_its_start(tutor, db_engine, cur, state, reply, action):
    """Standard form ends and roots opens at its start: the move-on line and roots' intro, and no step yet.
    "ok" then plays roots' step 1."""
    student_id = sf2_open(db_engine, cur, **state)
    tutor.start(student_id)
    moved_on = tutor.turn(student_id, reply)
    assert (moved_on.decision.action, moved_on.decision.resume_concept_id) == (action, "roots")
    roots = cur.concepts["roots"]
    move_on = f"Now let's move on to roots and checking by substitution. {roots.intro}"
    assert move_on in [segment.say for segment in moved_on.segments]
    assert progress_of(db_engine, cur, student_id)["roots"].step_id is None
    step_1 = tutor.turn(student_id, "ok")
    assert (step_1.decision.rule, step_1.decision.play_step_id) == ("R0", roots.steps[0].id)


def test_say_again_after_any_other_reply_replays_that_reply(tutor, db_engine, cur):
    """Not right after a topic start, the last reply is said again as it was: a step, or an answered question."""
    student_id = new_student(db_engine, cur)
    tutor.start(student_id)
    for text in ("ok", "why is that?"):           # R0: step 1. R7: "Good question …", then step 1 again
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
        replies.cue_line("offer", "discriminant")]
    assert picked.segments[1].board[0].op == "clear"            # wiped as the picked topic opens
    saved = progress_of(db_engine, cur, student_id)
    assert saved["standard_form"] == ConceptState("standard_form", status="paused", points=1.0,
                                                  pending=Pending("sf2", hint_level=1), last_check_id="sf2")
    assert saved["discriminant"].status == "teaching" and saved["discriminant"].step_id is None
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


def on_roots_step_2(db_engine, cur):
    """Asha, standard form mastered, on roots' second step (not_a_root): where the logged student asked to skip."""
    student_id = new_student(db_engine, cur, mastered=["standard_form"])
    with db_engine.begin() as conn:
        progress = db.load_progress(conn, student_id, cur)
        progress["roots"] = ConceptState("roots", status="teaching", step_id="not_a_root")
        db.save_progress(conn, student_id, progress, cur.version)
    return student_id


def test_asking_for_a_topic_by_name_moves_there_without_the_llm(cur, db_engine):
    """The logged reply "Can we skip to the factorization topic?" used to be said back (R8). Now it is S2, just as
    if the topic were clicked, read without a model call: the fake LLM has no replies to give."""
    fake = FakeLLM()
    tutor = Tutor(cur, db_engine, WITH_LLM, llm=fake)
    student_id = on_roots_step_2(db_engine, cur)
    tutor.start(student_id)
    result = tutor.turn(student_id, "Can we skip to the factorization topic?")
    decision = result.decision
    assert (decision.rule, decision.action, decision.concept_id) == ("S2", "jump", "factorisation")
    assert fake.asked == [] and result.calls == [] and result.understood is None
    concept = cur.concepts["factorisation"]
    assert [segment.say for segment in result.segments[:2]] == [
        "Sure. Roots and checking by substitution is saved just where you left it.",
        f"Now let's move on to solving by factorisation. {concept.intro}"]
    assert [choice.label for choice in result.choices] == ["Teach me", "Test me first"]   # the topic-start offer
    saved = progress_of(db_engine, cur, student_id)
    assert saved["roots"] == ConceptState("roots", status="paused", step_id="not_a_root")
    assert (saved["factorisation"].status, saved["factorisation"].step_id) == ("teaching", None)


def test_asking_for_a_topic_while_a_question_is_open_pauses_the_question(cur, db_engine):
    """While checking too, and "move on to the discriminant" is a switch, not "move on": the open question waits
    with its points and hint, as when a topic is clicked."""
    fake = FakeLLM()
    tutor = Tutor(cur, db_engine, WITH_LLM, llm=fake)
    student_id = sf2_open(db_engine, cur, points=1.0, hint_level=1)
    tutor.start(student_id)
    result = tutor.turn(student_id, "move on to the discriminant")
    assert (result.decision.rule, result.decision.concept_id) == ("S2", "discriminant") and fake.asked == []
    saved = progress_of(db_engine, cur, student_id)
    assert saved["standard_form"] == ConceptState("standard_form", status="paused", points=1.0,
                                                  pending=Pending("sf2", hint_level=1), last_check_id="sf2")


@pytest.mark.parametrize("text", ["Can we go to roots?", "Okay, let's move on to the next step."],
                         ids=["current_topic", "no_topic_named"])
def test_a_reply_naming_no_other_topic_is_not_a_switch(tutor, db_engine, cur, text):
    """The topic already being taught, or no topic at all (the second is from the session log): no S2."""
    student_id = on_roots_step_2(db_engine, cur)
    tutor.start(student_id)
    assert tutor.turn(student_id, text).decision.rule != "S2"
    assert progress_of(db_engine, cur, student_id)["factorisation"].status == "not_started"


def test_safety_comes_before_a_topic_switch(tutor, db_engine, cur):
    """The reply names a topic and has personal details: the safety reply, and the lesson stays where it is."""
    student_id = on_roots_step_2(db_engine, cur)
    tutor.start(student_id)
    result = tutor.turn(student_id, "my phone number is 98765 43210 skip to factorisation")
    assert result.decision.rule == "R10" and "safety_review" in result.issues
    assert progress_of(db_engine, cur, student_id)["factorisation"].status == "not_started"
