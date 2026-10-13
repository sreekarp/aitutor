"""After a safety reply (R10) a page reload draws the same buttons as the live turn did, whatever came next."""
import pytest
from conftest import make_settings, new_student

from app import curriculum, db, page
from app.tutor import Tutor

PII = "my number is 9876543210"
FAIL = "<failed recording>"


def start(*first_replies: str):
    """A scripted-mode tutor with a new student who has been through Start and these replies."""
    tutor = Tutor(curriculum.load(), db.connect("sqlite://"), make_settings())
    student_id = new_student(tutor.db_engine, tutor.cur)
    tutor.start(student_id)
    for text in first_replies:
        tutor.turn(student_id, text)
    return tutor, student_id


def say(tutor: Tutor, student_id: str, text: str) -> tuple[str, list[dict], list[dict]]:
    """One turn: its rule, the buttons the live turn shows (as the page gets them), and the buttons a reload draws
    straight after it (GET /api/state)."""
    if text == FAIL:
        result = tutor.turn(student_id, "", input_mode="voice", stt_failed=True)
    else:
        result = tutor.turn(student_id, text)
    live = page.payload(tutor, student_id, result)["choices"]
    return result.decision.rule, live, page.state(tutor, student_id)["choices"]


def play(replies: list[str], teach_first: bool):
    """Play the replies from the topic start (or from step 1 when teach_first): (text, rule, live, reloaded) each."""
    tutor, student_id = start("teach me") if teach_first else start()
    return [(text, *say(tutor, student_id, text)) for text in replies]


@pytest.mark.parametrize("teach_first", [True, False], ids=["on a step", "at the topic start"])
@pytest.mark.parametrize("replies", [[PII, FAIL], [PII, FAIL, FAIL], [PII, FAIL, "say again"]],
                         ids=["R10+fail", "R10+fail+fail", "R10+fail+say again"])
def test_reload_after_a_safety_reply_and_a_failed_recording_matches_the_live_buttons(replies, teach_first):
    seen = play(replies, teach_first)
    assert seen[0][1:] == ("R10", [], [])                               # R10 itself: no buttons
    for text, _, live, reloaded in seen[1:]:
        assert live, text                                              # the step's or the offer's buttons
        assert reloaded == live, text


@pytest.mark.parametrize("replies", [[PII, "say again"], [PII, FAIL], [PII, "banana"]],
                         ids=["say again", "fail", "unclear"])
def test_reload_after_a_safety_reply_with_a_question_open_keeps_not_sure(replies):
    tutor, student_id = start("teach me", "test me")                    # a question is open
    assert say(tutor, student_id, PII) == ("R10", [], [])
    for text in replies[1:]:
        _, live, reloaded = say(tutor, student_id, text)
        assert [choice["label"] for choice in live] == ["Not sure"], text
        assert reloaded == live, text


@pytest.mark.parametrize("teach_first", [True, False], ids=["on a step", "at the topic start"])
def test_a_replay_of_the_safety_reply_has_no_buttons_live_or_after_a_reload(teach_first):
    seen = play([PII, "sorry", "say again"], teach_first)
    for text, rule, live, reloaded in seen:
        assert (live, reloaded) == ([], []), (text, rule)
