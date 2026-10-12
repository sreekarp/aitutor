"""Tests for app/engine.py and app/sessions.py, the teaching rules.

The engine runs on a tiny curriculum built in this file, so these tests don't change when the
lesson files do. Every decision goes through turn() or check_decision(), which also check two
promises each time: decide() never changes its input, and a reason never contains an answer key.
"""
from __future__ import annotations

import copy
import re
from dataclasses import replace
from fractions import Fraction

import pytest

from app import engine, sessions
from app.models import (ACTIONS, Check, Concept, ConceptState, Curriculum, Decision, Grade, Mistake, Pending,
                        Progress, Segment, Step, Understood)


# ---------- a tiny curriculum: 3 concepts, 2-3 steps and 3-4 checks each ----------
def _step(step_id: str) -> Step:
    return Step(id=step_id, segments=[Segment(say=f"This is step {step_id}.")])


def _check(check_id: str, equation: str, kind: str = "roots", **fields) -> Check:
    return Check(id=check_id, say="Answer this question.", label="Solve:", equation=equation, kind=kind,
                 hints=["Hint one.", "Hint two.", "Hint three."], **fields)


def _known(mistake_id: str) -> Mistake:
    return Mistake(id=mistake_id, looks_like=f"the {mistake_id} slip", fix="Check each step again.")


def tiny_curriculum() -> Curriculum:
    """Three small concepts: enough to reach every rule."""
    alpha = Concept(
        id="alpha", title="Alpha practice", ncert="test", intro="Test intro.", reteach_step="a_intro",
        steps=[_step("a_intro"), _step("a_method"), _step("a_check")],
        checks=[_check("a1", "Eq(x**2 - 18*x + 77, 0)", traps={"sign_flip": "-7, -11"}),
                _check("a2", "Eq(x**2 - 4*x - 77, 0)", traps={"sign_flip": "-11, 7"}),
                _check("a3", "Eq(x**2 - 17*x + 72, 0)", traps={"sign_flip": "-8, -9"}),
                _check("a4", "Eq(5 + 2*x**2, 13*x)", kind="coefficient", which="b",
                       traps={"sign_when_moving": "13"})],
        mistakes=[_known("sign_flip"), _known("sign_when_moving")],
        strategies=["worked_example", "substitute_back", "compare_non_example"])
    beta = Concept(
        id="beta", title="Beta practice", ncert="test", intro="Test intro.", reteach_step="b_what",
        steps=[_step("b_what"), _step("b_cases")],
        checks=[_check("b1", "Eq(x**2 + 9*x + 7, 0)", kind="discriminant", traps={"sign_in_D": "109"}),
                _check("b2", "Eq(x**2 + 8*x + 20, 0)", kind="nature"),
                _check("b3", "Eq(x*(x + 7), x**2 + 9)", kind="is_quadratic")],
        mistakes=[_known("sign_in_D")],
        strategies=["worked_example", "compare_non_example"])
    gamma = Concept(
        id="gamma", title="Gamma practice", ncert="test", intro="Test intro.", reteach_step="g_plan",
        steps=[_step("g_plan"), _step("g_example")],
        checks=[_check("g1", "Eq(x**2 - 64, 0)", keep="positive", show_equation=False,
                       traps={"kept_impossible_root": "8, -8"}),
                _check("g2", "Eq(x**2 + 2*x - 63, 0)", kind="is_root", candidate="-9"),
                _check("g3", "Eq(x**2 - 10*x + 30, 0)", kind="has_real_roots")],
        mistakes=[_known("kept_impossible_root")],
        strategies=["worked_example", "compare_non_example"])
    return Curriculum(version="test", order=["alpha", "beta", "gamma"], formulas={},
                      concepts={"alpha": alpha, "beta": beta, "gamma": gamma})


CUR = tiny_curriculum()

# SymPy's answers to the tiny checks: a1 {7, 11}, a2 {-7, 11}, a3 {8, 9}, a4 b = -13, b1 D = 53,
# b2 none, b3 no, g1 8 (all roots 8, -8), g2 yes, g3 no. No reason may contain any of them.
KEY_NUMBERS = {Fraction(n) for n in (7, 11, -7, 8, 9, -13, 53, -8)}
KEY_WORDS = {"none", "no", "yes"}


# ---------- small builders ----------
def progress_with(*states: ConceptState) -> Progress:
    """Progress for CUR: the given concept states, every other concept not started."""
    progress = {cid: ConceptState(cid) for cid in CUR.order}
    for state in states:
        progress[state.concept_id] = state
    return progress


def teaching_at(step_id: str, cid: str = "alpha", last_check_id: str | None = None) -> ConceptState:
    return ConceptState(cid, status="teaching", step_id=step_id, last_check_id=last_check_id)


def asking(check_id: str, cid: str = "alpha", points: float = 0.0, reteach_count: int = 0,
           tried: tuple = (), **pending) -> ConceptState:
    """A concept in checking, waiting for an answer to check_id (Pending fields as keywords)."""
    return ConceptState(cid, status="checking", points=points, pending=Pending(check_id, **pending),
                        last_check_id=check_id, reteach_count=reteach_count, strategies_tried=list(tried))


def mastered(cid: str) -> ConceptState:
    return ConceptState(cid, status="mastered", points=2.0)


def after_probe() -> Progress:
    """Right after R2 asked 'how did you get that?' about a right answer to a1."""
    return progress_with(asking("a1", awaiting_working=True, last_answer="7, 11"))


def said(kind: str, answer: str = "", working: bool = False, mistake: str | None = None,
         safety: bool = False) -> Understood:
    return Understood(type=kind, answer=answer, showed_working=working, mistake=mistake, safety=safety)


def graded(verdict: str, student: str, detail: str = "", trap: str | None = None,
           wrong_values: bool = False) -> Grade:
    return Grade(verdict=verdict, student=student, detail=detail, trap=trap, wrong_values=wrong_values)


WRONG = graded("wrong", "12, 13", detail="not_a_root", wrong_values=True)   # not roots at all, no trap


# ---------- checks run on every decision ----------
def assert_no_key(reason: str) -> None:
    """Fail if the reason contains an answer key: numbers compared by value, words as whole words."""
    numbers = {Fraction(token) for token in re.findall(r"-?\d+(?:\.\d+)?(?:/\d+)?", reason)}
    words = set(re.findall(r"[a-z]+", reason.lower()))
    assert not numbers & KEY_NUMBERS, f"answer key in reason: {reason}"
    assert not words & KEY_WORDS, f"answer key in reason: {reason}"


def check_decision(decision: Decision) -> Decision:
    """A known action, and a reason with no answer key in it."""
    assert decision.action in ACTIONS and decision.reason
    assert_no_key(decision.reason)
    return decision


def turn(progress: Progress, understood: Understood, grade: Grade | None = None,
         cur: Curriculum = CUR) -> tuple[Decision, Progress]:
    """engine.decide(), plus: the input progress is unchanged and the decision passes check_decision()."""
    before = copy.deepcopy(progress)
    decision, after = engine.decide(progress, cur, understood, grade)
    assert progress == before, "decide() changed its input"
    return check_decision(decision), after


# ---------- the current concept ----------
def test_current_concept_prefers_work_in_progress():
    assert engine.current_concept(progress_with(), CUR) == "alpha"
    assert engine.current_concept(progress_with(mastered("alpha"), asking("g1", cid="gamma")), CUR) == "gamma"
    assert engine.current_concept(progress_with(mastered("alpha")), CUR) == "beta"
    done = progress_with(mastered("alpha"), mastered("beta"), ConceptState("gamma", status="needs_review"))
    assert engine.current_concept(done, CUR) is None


# ---------- teaching ----------
def test_teaching_ok_plays_next_step():
    decision, after = turn(progress_with(teaching_at("a_intro")), said("next"))
    assert (decision.rule, decision.action, decision.play_step_id) == ("R0", "next_step", "a_method")
    assert after["alpha"].step_id == "a_method" and after["alpha"].status == "teaching"
    decision, after = turn(after, said("answer", "12"))            # an answer-like reply moves on too
    assert (decision.rule, decision.play_step_id) == ("R0", "a_check")
    decision, after = turn(progress_with(), said("next"))          # a concept not started begins at step 1
    assert (decision.play_step_id, after["alpha"].status) == ("a_intro", "teaching")


def test_last_step_starts_checking_with_fresh_pending():
    decision, after = turn(progress_with(teaching_at("a_check")), said("next"))
    assert (decision.rule, decision.action, decision.ask_check_id) == ("R0", "start_check", "a1")
    state = after["alpha"]
    assert state.status == "checking" and state.pending == Pending("a1") and state.last_check_id == "a1"
    # a later pass carries on after the last question asked
    decision, after = turn(progress_with(teaching_at("a_check", last_check_id="a2")), said("next"))
    assert decision.ask_check_id == "a3" and after["alpha"].pending == Pending("a3")


def test_claims_understanding_teaching_and_checking():
    decision, after = turn(progress_with(teaching_at("a_intro")), said("claims_understanding"))
    assert (decision.rule, decision.action, decision.ask_check_id) == ("R6", "start_check", "a1")
    assert after["alpha"].status == "checking" and after["alpha"].points == 0
    progress = progress_with(asking("a2", hint_level=1))
    decision, after = turn(progress, said("claims_understanding"))
    assert (decision.rule, decision.action, decision.ask_check_id) == ("R6", "show_me", "a2")
    assert after == progress                                       # no points, same question


def test_question_explains_and_keeps_state():
    progress = progress_with(teaching_at("a_method"))
    for kind in ("question", "dont_know"):
        decision, after = turn(progress, said(kind))
        assert (decision.rule, decision.action, decision.reask) == ("R7", "explain", False)
        assert after == progress
    progress = progress_with(asking("a1", hint_level=2))
    decision, after = turn(progress, said("question"))
    assert (decision.rule, decision.action, decision.reask) == ("R7", "explain", True)
    assert after == progress


def test_teaching_answer_with_known_mistake_is_explained():
    # "I think the roots are -4 and 1" while teaching: the slip is explained before moving on
    progress = progress_with(teaching_at("a_method"))
    decision, after = turn(progress, said("answer", "-4, 1", mistake="sign_flip"))
    assert (decision.rule, decision.action, decision.mistake) == ("R7", "explain", "sign_flip")
    assert after == progress
    decision, _ = turn(progress, said("answer", "12", mistake="not_listed"))    # an unknown id moves on as before
    assert (decision.rule, decision.action) == ("R0", "next_step")


def test_unclear_while_teaching_repeats_last_reply():
    progress = progress_with(teaching_at("a_method"))
    for kind in ("say_again", "off_topic", "unclear"):
        decision, after = turn(progress, said(kind))
        assert (decision.rule, decision.action, decision.play_step_id, decision.ask_check_id) == \
            ("R8", "repeat", None, None)
        # True whether the last reply is said again or, right after a topic start, just the step
        assert decision.reason == f"{engine.REPEAT_CAUSE[kind]}, so it is said again. Nothing changes.", kind
        assert after == progress


def test_a_concept_not_started_opens_at_its_start():
    """activate(): a topic opens at its start, step_id None (its intro is said; step 1 waits for a reply).
    A paused concept carries on just where it stopped."""
    after = progress_with(ConceptState("beta", status="paused", step_id="b_cases"))
    engine.activate(after, CUR, "alpha")
    engine.activate(after, CUR, "beta")
    assert after["alpha"] == ConceptState("alpha", status="teaching")
    assert after["beta"] == ConceptState("beta", status="teaching", step_id="b_cases")


def test_at_the_topic_start_a_reply_begins_step_1():
    """At the topic start any reply begins step 1, which also answers a question. "I get it" goes straight to a
    question (R6). "Say again", and a reply that couldn't be read (a failed recording too), say the start again."""
    start = progress_with(ConceptState("alpha", status="teaching"))
    for reply in (said("next"), said("answer", "12", mistake="sign_flip"), said("question"), said("dont_know"),
                  said("off_topic")):
        decision, after = turn(start, reply)
        assert (decision.rule, decision.action, decision.play_step_id) == ("R0", "next_step", "a_intro"), reply
        assert after["alpha"] == ConceptState("alpha", status="teaching", step_id="a_intro"), reply
    decision, after = turn(start, said("claims_understanding"))
    assert (decision.rule, decision.action, decision.ask_check_id) == ("R6", "start_check", "a1")
    for kind in ("say_again", "unclear"):
        decision, after = turn(start, said(kind))
        assert (decision.rule, decision.action, after) == ("R8", "repeat", start), kind


# ---------- checking: right answers and the "how did you get that?" probe ----------
def test_correct_with_working_gives_point_and_next_question():
    decision, after = turn(progress_with(asking("a1")), said("answer", "7, 11", working=True),
                           graded("correct", "7, 11"))
    assert (decision.rule, decision.action, decision.ask_check_id) == ("R1", "credit", "a2")
    assert decision.evidence == {"check_id": "a1", "type": "answer", "verdict": "correct"}
    state = after["alpha"]
    assert state.points == 1.0 and state.pending == Pending("a2") and state.last_check_id == "a2"


def test_half_point_after_hint():
    progress = progress_with(asking("a1", points=1.0, hint_level=1, followup_used=True, last_answer="11"))
    decision, after = turn(progress, said("answer", "7, 11", working=True), graded("correct", "7, 11"))
    assert (decision.rule, decision.action) == ("R1", "credit")
    assert after["alpha"].points == 1.5 and "0.5" in decision.reason
    assert after["alpha"].pending == Pending("a2")                 # the next question starts fresh


def test_mastery_resumes_next_concept_with_clear():
    decision, after = turn(progress_with(asking("a2", points=1.0)), said("answer", "11, -7", working=True),
                           graded("correct", "11, -7"))
    assert (decision.rule, decision.action, decision.concept_id) == ("R1", "mastered", "alpha")
    assert (decision.resume_concept_id, decision.clear_board, decision.ask_check_id) == ("beta", True, None)
    assert after["alpha"].status == "mastered" and after["alpha"].pending is None and after["alpha"].points == 2.0
    assert after["beta"] == ConceptState("beta", status="teaching")             # at its start: step_id None


def test_mastering_last_concept_resumes_none():
    progress = progress_with(mastered("alpha"), mastered("beta"), asking("g1", cid="gamma", points=1.5))
    decision, after = turn(progress, said("answer", "8", working=True), graded("correct", "8"))
    assert (decision.action, decision.resume_concept_id, decision.clear_board) == ("mastered", None, True)
    assert after["gamma"].status == "mastered" and engine.current_concept(after, CUR) is None


def test_correct_without_working_probes():
    decision, after = turn(progress_with(asking("a1")), said("answer", "7, 11"), graded("correct", "7, 11"))
    assert (decision.rule, decision.action, decision.ask_check_id, decision.reask) == ("R2", "probe", None, False)
    assert after["alpha"].pending == Pending("a1", awaiting_working=True, last_answer="7, 11")
    assert after["alpha"].points == 0


def test_probe_then_working_only_reply_credits():
    decision, after = turn(after_probe(), said("answer", "", working=True))
    assert (decision.rule, decision.action) == ("R1", "credit")
    assert after["alpha"].points == 1.0 and after["alpha"].pending == Pending("a2")


def test_probe_then_guess_gives_hint_not_credit():
    decision, after = turn(after_probe(), said("answer", ""))       # "I just guessed"
    assert (decision.rule, decision.action, decision.hint_level, decision.reask) == ("R4", "hint", 1, True)
    pending = after["alpha"].pending
    assert after["alpha"].points == 0 and pending.hint_level == 1 and pending.awaiting_working


def test_repeated_right_answer_gets_r2_again():
    progress = after_probe()
    decision, after = turn(progress, said("answer", "7, 11"), graded("correct", "7, 11"))
    assert (decision.rule, decision.action) == ("R2", "probe")
    assert after == progress                                       # no hint, no lost points


def test_probe_then_wrong_answer_clears_awaiting_and_no_credit_later():
    decision, after = turn(after_probe(), said("answer", "12, 13"), WRONG)
    assert (decision.rule, decision.action) == ("R4", "hint")
    assert after["alpha"].pending.awaiting_working is False
    # explaining the working now earns nothing: the standing answer is wrong
    decision, after = turn(after, said("answer", "", working=True))
    assert (decision.rule, decision.action) == ("R5", "clarify")
    assert after["alpha"].points == 0


# ---------- checking: hints, follow-ups and re-asks (R4, R5, R8) ----------
def test_hint_ladder():
    progress = progress_with(asking("a1"))
    replies = [(said("answer", "12, 13"), WRONG), (said("dont_know"), None), (said("answer", "12, 13"), WRONG)]
    for level, (understood, grade) in enumerate(replies, start=1):
        decision, progress = turn(progress, understood, grade)
        assert (decision.rule, decision.action, decision.hint_level, decision.reask) == ("R4", "hint", level, True)
        assert progress["alpha"].pending.check_id == "a1"           # still the same question
    assert progress["alpha"].points == 0


def test_partial_then_partial_without_mistake_hints():
    partial = graded("partial", "11", detail="missing")
    decision, after = turn(progress_with(asking("a1")), said("answer", "11"), partial)
    assert (decision.rule, decision.action, decision.reask, decision.mistake) == ("R5", "follow_up", True, None)
    assert after["alpha"].pending.followup_used
    decision, after = turn(after, said("answer", "11"), partial)
    assert (decision.rule, decision.action, decision.hint_level) == ("R4", "hint", 1)


def test_unsure_then_unsure_repeats_never_hints():
    unsure = graded("unsure", "eleventy")
    decision, after = turn(progress_with(asking("a1")), said("answer", "eleventy"), unsure)
    assert (decision.rule, decision.action, decision.reask) == ("R5", "clarify", False)
    assert after["alpha"].pending.followup_used
    for _ in range(2):
        decision, after = turn(after, said("answer", "eleventy"), unsure)
        assert (decision.rule, decision.action, decision.ask_check_id) == ("R8", "repeat", "a1")
        assert after["alpha"].pending.hint_level == 0 and after["alpha"].reteach_count == 0


def test_ok_while_checking_reasks():
    progress = progress_with(asking("a2", hint_level=1))
    for kind in ("next", "say_again", "off_topic", "unclear"):
        decision, after = turn(progress, said(kind))
        assert (decision.rule, decision.action, decision.ask_check_id) == ("R8", "repeat", "a2")
        assert after == progress


# ---------- safety and the end ----------
def test_safety_first_even_after_end():
    progress = progress_with(asking("a1", hint_level=1))
    decision, after = turn(progress, said("answer", "7, 11", working=True, safety=True),
                           graded("correct", "7, 11"))
    assert (decision.rule, decision.action, decision.concept_id) == ("R10", "safety", "alpha")
    assert after == progress                                       # not graded; pending kept
    done = progress_with(mastered("alpha"), mastered("beta"), mastered("gamma"))
    decision, after = turn(done, said("unclear", safety=True))
    assert (decision.rule, decision.concept_id) == ("R10", None) and after == done


def test_end_when_no_concept_left():
    done = progress_with(mastered("alpha"), ConceptState("beta", status="needs_review"), mastered("gamma"))
    for understood in (said("next"), said("answer", "12"), said("question")):
        decision, after = turn(done, understood)
        assert (decision.rule, decision.action, decision.concept_id) == ("END", "end", None)
        assert after == done


# ---------- promises kept by every decision ----------
def test_reasons_never_contain_key():
    """turn() scans every reason in this module; here the student even says the keys."""
    cases = [
        (asking("a1"), said("answer", "7, 11"), graded("correct", "7, 11")),
        (asking("a4"), said("answer", "-13", working=True), graded("correct", "-13")),
        (asking("a1"), said("answer", "11"), graded("partial", "11", detail="missing")),
        (asking("b1", cid="beta"), said("answer", "53", working=True), graded("correct", "53")),
        (asking("b2", cid="beta"), said("answer", "none"), graded("correct", "none")),
        (asking("b3", cid="beta", hint_level=1), said("answer", "no", working=True), graded("correct", "no")),
        (asking("g1", cid="gamma"), said("answer", "8, -8"),
         graded("partial", "8, -8", detail="extra", trap="kept_impossible_root")),
        (asking("g2", cid="gamma"), said("answer", "yes", working=True), graded("correct", "yes")),
        (asking("g3", cid="gamma"), said("answer", "no"), graded("correct", "no")),
    ]
    for state, understood, grade in cases:
        decision, _ = turn(progress_with(state), understood, grade)
        assert decision.rule in ("R1", "R2", "R5")
    with pytest.raises(AssertionError):                            # the scan itself works
        assert_no_key("The answer is 7 and 11.")
    with pytest.raises(AssertionError):
        assert_no_key("It has none.")


def test_decide_does_not_mutate_input():
    progress = progress_with(asking("a1", points=1.5))
    snapshot = copy.deepcopy(progress)
    decision, after = engine.decide(progress, CUR, said("answer", "7, 11", working=True),
                                    graded("correct", "7, 11"))
    assert decision.action == "mastered" and after["alpha"].status == "mastered"
    assert progress == snapshot and progress["alpha"].status == "checking"
    assert after["alpha"] is not progress["alpha"]
    assert CUR == tiny_curriculum()                                # the curriculum is untouched too


# ---------- reteaching and needs review (R3, R4, R9) ----------
TRAP = graded("wrong", "-7, -11", detail="not_a_root", trap="sign_flip", wrong_values=True)   # a1's trap


def test_known_mistake_reteaches_with_first_untried_strategy():
    decision, after = turn(progress_with(asking("a1")), said("answer", "-7, -11"), TRAP)
    assert (decision.rule, decision.action, decision.mistake) == ("R3", "reteach", "sign_flip")
    assert (decision.strategy, decision.reteach_count, decision.ask_check_id) == ("worked_example", 1, "a2")
    state = after["alpha"]
    assert state.pending == Pending("a2") and state.last_check_id == "a2"       # the next question, fresh
    assert state.strategies_tried == ["worked_example"] and state.reteach_count == 1
    # the next reteach in this pass takes the first strategy not tried yet
    progress = progress_with(asking("a2", reteach_count=1, tried=("worked_example",)))
    decision, after = turn(progress, said("answer", "-11, 7"),
                           graded("wrong", "-11, 7", trap="sign_flip", wrong_values=True))
    assert (decision.strategy, decision.reteach_count) == ("substitute_back", 2)
    assert after["alpha"].strategies_tried == ["worked_example", "substitute_back"]


def test_llm_mistake_only_used_when_listed_and_wrong_or_partial():
    decision, _ = turn(progress_with(asking("a1")), said("answer", "12, 13", mistake="sign_flip"), WRONG)
    assert (decision.rule, decision.action, decision.mistake) == ("R3", "reteach", "sign_flip")
    decision, _ = turn(progress_with(asking("a1")), said("answer", "12, 13", mistake="made_up"), WRONG)
    assert (decision.rule, decision.action, decision.mistake) == ("R4", "hint", None)     # not in the list
    decision, _ = turn(progress_with(asking("a1")), said("answer", "-7, -11", mistake="sign_when_moving"), TRAP)
    assert decision.mistake == "sign_flip"                                                # a trap is certain
    decision, _ = turn(progress_with(asking("a1", followup_used=True)), said("answer", "11", mistake="sign_flip"),
                       graded("partial", "11", detail="missing"))
    assert (decision.rule, decision.mistake) == ("R3", "sign_flip")                       # a 2nd partial answer
    decision, _ = turn(progress_with(asking("a1")), said("answer", "7, 11", working=True, mistake="sign_flip"),
                       graded("correct", "7, 11"))
    assert (decision.rule, decision.mistake) == ("R1", None)                              # right: ignored
    decision, _ = turn(progress_with(asking("a1", followup_used=True)), said("answer", "?", mistake="sign_flip"),
                       graded("unsure", "?"))
    assert (decision.rule, decision.mistake) == ("R8", None)                              # unreadable: ignored


def test_hints_exhausted_reteach_without_mistake():
    decision, after = turn(progress_with(asking("a1", hint_level=3)), said("answer", "12, 13"), WRONG)
    assert (decision.rule, decision.action, decision.mistake) == ("R4", "reteach", None)
    assert (decision.strategy, decision.ask_check_id) == ("worked_example", "a2")
    assert after["alpha"].pending == Pending("a2") and after["alpha"].reteach_count == 1


def test_substitute_back_skipped_after_dont_know():
    progress = progress_with(asking("a1", hint_level=3, reteach_count=1, tried=("worked_example",)))
    decision, _ = turn(progress, said("dont_know"))
    assert (decision.rule, decision.action, decision.strategy) == ("R4", "reteach", "compare_non_example")
    decision, _ = turn(progress, said("answer", "12, 13"), WRONG)          # wrong values can be put back in
    assert decision.strategy == "substitute_back"


def test_strategies_wrap_to_first_eligible():
    alpha = CUR.concepts["alpha"]
    every = asking("a1", tried=("worked_example", "substitute_back", "compare_non_example"))
    assert engine.pick_strategy(alpha, every, wrong_values=True) == "worked_example"
    reordered = alpha.model_copy(update={"strategies": ["substitute_back", "compare_non_example", "worked_example"]})
    assert engine.pick_strategy(reordered, every, wrong_values=False) == "compare_non_example"   # first eligible
    two = asking("a1", tried=("worked_example", "compare_non_example"))
    assert engine.pick_strategy(alpha, two, wrong_values=True) == "substitute_back"
    assert engine.pick_strategy(alpha, two, wrong_values=False) == "worked_example"
    # through decide(): a strategy used again isn't listed twice
    progress = progress_with(asking("a1", hint_level=3, reteach_count=2,
                                    tried=("worked_example", "compare_non_example")))
    decision, after = turn(progress, said("dont_know"))
    assert (decision.strategy, decision.reteach_count) == ("worked_example", 3)
    assert after["alpha"].strategies_tried == ["worked_example", "compare_non_example"]
    assert "not tried yet" not in decision.reason


def test_fourth_reteach_is_needs_review():
    progress = progress_with(asking("a1", points=1.5, reteach_count=3,
                                    tried=("worked_example", "substitute_back", "compare_non_example")))
    decision, after = turn(progress, said("answer", "-7, -11"), TRAP)
    assert (decision.rule, decision.action, decision.concept_id) == ("R9", "needs_review", "alpha")
    assert (decision.resume_concept_id, decision.clear_board, decision.ask_check_id) == ("beta", True, None)
    assert (decision.strategy, decision.mistake) == (None, "sign_flip")
    state = after["alpha"]
    assert (state.status, state.pending, state.points, state.reteach_count) == ("needs_review", None, 1.5, 3)
    assert after["beta"] == ConceptState("beta", status="teaching")             # at its start: step_id None
    # the same gate after hint 3 (the R4 route); on the last concept nothing is left to resume
    progress = progress_with(mastered("alpha"), mastered("beta"),
                             asking("g1", cid="gamma", hint_level=3, reteach_count=3))
    decision, after = turn(progress, said("dont_know"))
    assert (decision.rule, decision.resume_concept_id) == ("R9", None)
    assert engine.current_concept(after, CUR) is None


# ---------- session start (S0, S1) ----------
def needs_review(cid: str, step_id: str, last_check_id: str) -> ConceptState:
    """A concept whose 3 reteaches ran out partway through a pass."""
    return ConceptState(cid, status="needs_review", step_id=step_id, points=1.5, last_check_id=last_check_id,
                        reteach_count=3, strategies_tried=["worked_example", "compare_non_example"])


def test_s0_resets_every_needs_review_and_keeps_last_check():
    progress = progress_with(mastered("alpha"), needs_review("beta", "b_cases", "b2"),
                             needs_review("gamma", "g_example", "g1"))
    before = copy.deepcopy(progress)
    decision, after = sessions.start(progress, CUR, new_session=True)
    check_decision(decision)
    assert (decision.rule, decision.action, decision.concept_id) == ("S0", "greet", "beta")
    assert (decision.resume_concept_id, decision.clear_board) == ("beta", True)
    assert after["beta"] == ConceptState("beta", status="teaching", step_id="b_what", last_check_id="b2")
    assert after["gamma"] == ConceptState("gamma", status="teaching", step_id="g_plan", last_check_id="g1")
    assert after["alpha"] == before["alpha"] and progress == before         # input unchanged
    # a brand-new student opens the first concept at its start (step_id None); a review above restarts at step 1
    decision, after = sessions.start(progress_with(), CUR, new_session=True)
    assert decision.resume_concept_id == "alpha"
    assert after["alpha"] == ConceptState("alpha", status="teaching")


def test_s1_continues_without_clear():
    progress = progress_with(mastered("alpha"), needs_review("beta", "b_cases", "b2"),
                             asking("g2", cid="gamma", points=1.0, hint_level=2))
    decision, after = sessions.start(progress, CUR, new_session=False)
    check_decision(decision)
    assert (decision.rule, decision.action, decision.resume_concept_id, decision.clear_board) == \
        ("S1", "resume", "gamma", False)
    assert decision.hint_level == 2 and after == progress     # nothing reset, not even the review


def test_s0_with_everything_final_resumes_none():
    done = progress_with(mastered("alpha"), mastered("beta"), mastered("gamma"))
    decision, after = sessions.start(done, CUR, new_session=True)
    check_decision(decision)
    assert (decision.rule, decision.concept_id, decision.resume_concept_id) == ("S0", None, None)
    assert after == done
    final = progress_with(mastered("alpha"), needs_review("beta", "b_cases", "b2"), mastered("gamma"))
    decision, after = sessions.start(final, CUR, new_session=False)          # S1 never resets a review
    assert (decision.rule, decision.resume_concept_id) == ("S1", None) and after == final


# ---------- the student picks a concept (S2) ----------
def jump(progress: Progress, concept_id: str) -> tuple[Decision, Progress]:
    """sessions.jump(), plus: the input progress is unchanged and the decision passes check_decision()."""
    before = copy.deepcopy(progress)
    decision, after = sessions.jump(progress, CUR, concept_id)
    assert progress == before, "jump() changed its input"
    return check_decision(decision), after


def test_jump_pauses_the_current_concept_and_starts_the_picked_one():
    progress = progress_with(asking("a2", points=1.0, hint_level=2))
    decision, after = jump(progress, "gamma")
    assert (decision.rule, decision.action, decision.concept_id) == ("S2", "jump", "gamma")
    assert (decision.resume_concept_id, decision.clear_board, decision.evidence["from"]) == ("gamma", True, "alpha")
    assert after["alpha"] == ConceptState("alpha", status="paused", points=1.0, pending=Pending("a2", hint_level=2),
                                          last_check_id="a2")             # everything kept, just paused
    assert after["gamma"] == ConceptState("gamma", status="teaching")           # at its start: step_id None
    assert engine.current_concept(after, CUR) == "gamma"


def test_jump_from_nothing_active_names_nothing_left():
    decision, after = jump(progress_with(), "gamma")                  # a new student: alpha was never started
    assert decision.evidence["from"] is None and "paused" not in decision.reason
    assert after["alpha"].status == "not_started" and after["gamma"].status == "teaching"


def test_jump_back_resumes_a_paused_concept_where_it_stopped():
    paused_question = ConceptState("alpha", status="paused", points=1.0, pending=Pending("a2", hint_level=2),
                                   last_check_id="a2")
    paused_step = ConceptState("beta", status="paused", step_id="b_cases")
    progress = progress_with(paused_question, paused_step, teaching_at("g_example", cid="gamma"))
    decision, after = jump(progress, "alpha")
    assert after["alpha"] == replace(paused_question, status="checking") and decision.hint_level == 2
    assert after["gamma"].status == "paused" and after["beta"] == paused_step
    decision, after = jump(after, "beta")
    assert after["beta"] == replace(paused_step, status="teaching") and after["alpha"].status == "paused"


def test_jump_to_a_finished_concept_starts_it_again():
    progress = progress_with(mastered("alpha"), needs_review("beta", "b_cases", "b2"), asking("g1", cid="gamma"))
    for cid, last_check in (("alpha", None), ("beta", "b2")):
        decision, after = jump(progress, cid)
        # at its start, points reset; the next question follows on from the last one asked
        assert after[cid] == ConceptState(cid, status="teaching", last_check_id=last_check)
        assert "again" in decision.reason and after["gamma"].status == "paused"


def test_jump_pauses_every_active_concept():
    # a new session puts each concept marked for review back into teaching, so more than one can be active
    progress = progress_with(teaching_at("a_intro"), teaching_at("b_what", cid="beta"))
    decision, after = jump(progress, "gamma")
    assert [after[cid].status for cid in CUR.order] == ["paused", "paused", "teaching"]
    assert engine.current_concept(after, CUR) == "gamma"


def test_paused_concepts_come_back_in_order_and_end_waits_for_them():
    progress = progress_with(ConceptState("alpha", status="paused", step_id="a_method"),
                             asking("b3", cid="beta", points=1.0), mastered("gamma"))
    decision, after = turn(progress, said("answer", "no", working=True), graded("correct", "no"))
    assert (decision.rule, decision.action, decision.resume_concept_id) == ("R1", "mastered", "alpha")
    assert after["alpha"] == ConceptState("alpha", status="teaching", step_id="a_method")
    assert engine.current_concept(progress_with(ConceptState("beta", status="paused", step_id="b_what")),
                                  CUR) == "alpha"                          # unstarted and paused, in order


# ---------- lesson files edited between turns ----------
def edited(cid: str, step: str | None = None, check: str | None = None) -> Curriculum:
    """A copy of CUR with one step or one check removed, as if the lesson file was edited."""
    cur = CUR.model_copy(deep=True)
    concept = cur.concepts[cid]
    concept.steps = [s for s in concept.steps if s.id != step]
    concept.checks = [c for c in concept.checks if c.id != check]
    return cur


def test_reconcile_missing_step_while_teaching():
    progress = progress_with(teaching_at("a_method", last_check_id="a1"))
    after, issues, replaced = sessions.reconcile(progress, edited("alpha", step="a_method"))
    assert (after["alpha"].step_id, issues, replaced) == ("a_intro", ["curriculum_changed"], False)
    assert after["alpha"].last_check_id == "a1" and progress["alpha"].step_id == "a_method"
    checking = asking("a1")
    checking.step_id = "a_method"                                  # a step only matters while teaching
    progress = progress_with(checking)
    assert sessions.reconcile(progress, edited("alpha", step="a_method")) == (progress, [], False)


def test_reconcile_leaves_a_topic_start_alone():
    """step_id None means "at the topic start", not a removed step: teaching or paused there, nothing changes."""
    progress = progress_with(ConceptState("alpha", status="teaching"), ConceptState("beta", status="paused"))
    assert sessions.reconcile(progress, CUR) == (progress, [], False)


def test_reconcile_missing_pending_check_asks_next_fresh():
    progress = progress_with(asking("a2", points=1.0, hint_level=2, followup_used=True, last_answer="12"))
    cur = edited("alpha", check="a2")
    after, issues, replaced = sessions.reconcile(progress, cur)
    assert (issues, replaced) == (["curriculum_changed"], True)
    state = after["alpha"]
    assert state.pending == Pending("a1") and state.last_check_id == "a1"        # a fresh pending
    assert state.status == "checking" and state.points == 1.0                    # points are kept
    decision = check_decision(sessions.lesson_changed(after, cur))
    assert (decision.rule, decision.action, decision.concept_id, decision.ask_check_id) == \
        ("R8", "lesson_changed", "alpha", "a1")
    assert "lesson changed" in decision.reason
    assert sessions.reconcile(after, cur) == (after, [], False)                   # a second pass finds nothing
    # another concept's question is fixed too, but this turn's reply is still marked
    progress = progress_with(teaching_at("a_method"), asking("g2", cid="gamma"))
    after, issues, replaced = sessions.reconcile(progress, edited("gamma", check="g2"))
    assert (after["gamma"].pending, issues, replaced) == (Pending("g1"), ["curriculum_changed"], False)


def test_reconcile_unknown_last_check_falls_back_to_first():
    cur = edited("alpha", check="a3")
    after, _, replaced = sessions.reconcile(progress_with(asking("a3", hint_level=1)), cur)
    assert replaced and after["alpha"].pending == Pending("a1")       # check 1, not the one after a3's old place
    # while teaching, an unknown last question needs no fix: the first question asked is check 1
    after, issues, _ = sessions.reconcile(progress_with(teaching_at("a_check", last_check_id="a3")), cur)
    assert issues == []
    decision, after = turn(after, said("next"), cur=cur)
    assert decision.ask_check_id == "a1" and after["alpha"].pending == Pending("a1")


def test_replay_instead_undoes_strategy():
    before = progress_with(asking("a1", reteach_count=1, tried=("worked_example",)))
    decision, after = turn(before, said("answer", "-7, -11"), TRAP)
    assert decision.strategy == "substitute_back"
    snapshot = copy.deepcopy((decision, after))
    replayed, replayed_after = engine.replay_instead(decision, before, after)
    check_decision(replayed)
    assert (replayed.rule, replayed.action, replayed.strategy) == ("R3", "reteach", "replay")
    assert (replayed.reteach_count, replayed.ask_check_id, replayed.mistake) == (2, "a2", "sign_flip")
    assert "replayed" in replayed.reason and "substitute_back" not in replayed.reason
    state = replayed_after["alpha"]
    assert state.strategies_tried == ["worked_example"]                  # no strategy used up
    assert state.reteach_count == 2 and state.pending == Pending("a2")  # still a reteach; next question asked
    assert (decision, after) == snapshot                                 # inputs unchanged


def test_limit_and_end_decisions():
    for decision, rule, action in ((sessions.limit_decision(), "LIMIT", "limit"),
                                   (engine.end_decision(), "END", "end")):
        check_decision(decision)
        assert (decision.rule, decision.action, decision.concept_id) == (rule, action, None)
        assert (decision.resume_concept_id, decision.ask_check_id, decision.play_step_id) == (None, None, None)


# ---------- the board: a new question starts clean ----------
def test_only_a_new_question_clears_the_board():
    """A new question starts on a clean board; a move about the same question keeps the board and its working."""
    partial = graded("partial", "11", detail="missing")
    cases = [   # (progress, reply, grade) -> (rule, action, clears the board)
        (progress_with(teaching_at("a_check")), said("next"), None, "R0", "start_check", True),
        (progress_with(teaching_at("a_intro")), said("claims_understanding"), None, "R6", "start_check", True),
        (progress_with(asking("a1")), said("answer", "7, 11", working=True), graded("correct", "7, 11"),
         "R1", "credit", True),
        (progress_with(asking("a1")), said("answer", "-7, -11"), TRAP, "R3", "reteach", True),
        (progress_with(asking("a1", hint_level=3)), said("answer", "12, 13"), WRONG, "R4", "reteach", True),
        (progress_with(asking("a1")), said("answer", "7, 11"), graded("correct", "7, 11"), "R2", "probe", False),
        (progress_with(asking("a1")), said("dont_know"), None, "R4", "hint", False),
        (progress_with(asking("a1")), said("answer", "11"), partial, "R5", "follow_up", False),
        (progress_with(asking("a1")), said("answer", "eleventy"), graded("unsure", "eleventy"), "R5", "clarify", False),
        (progress_with(asking("a1")), said("claims_understanding"), None, "R6", "show_me", False),
        (progress_with(asking("a1")), said("question"), None, "R7", "explain", False),
        (progress_with(teaching_at("a_method")), said("question"), None, "R7", "explain", False),
        (progress_with(asking("a1")), said("unclear"), None, "R8", "repeat", False),
        (progress_with(teaching_at("a_method")), said("say_again"), None, "R8", "repeat", False),
    ]
    for progress, understood, grade, rule, action, clears in cases:
        decision, _ = turn(progress, understood, grade)
        assert (decision.rule, decision.action, decision.clear_board) == (rule, action, clears)
    lesson = edited("alpha", check="a1")                  # the open question was removed: a new one is asked
    after, _, replaced = sessions.reconcile(progress_with(asking("a1")), lesson)
    decision = check_decision(sessions.lesson_changed(after, lesson))
    assert replaced and (decision.action, decision.clear_board) == ("lesson_changed", True)


def test_s1_clears_the_board_only_when_the_lesson_replaced_the_open_question():
    lesson = edited("alpha", check="a1")
    after, _, replaced = sessions.reconcile(progress_with(asking("a1", hint_level=1)), lesson)
    decision, _ = sessions.start(after, lesson, new_session=False, replaced=replaced)
    check_decision(decision)
    assert (decision.rule, decision.resume_concept_id, decision.clear_board) == ("S1", "alpha", True)
    decision, _ = sessions.start(after, lesson, new_session=False, replaced=False)
    assert (decision.rule, decision.clear_board) == ("S1", False)
