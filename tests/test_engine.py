"""Tests for app/engine.py, the teaching rules (spec §4).

The engine runs on a tiny curriculum built in this file, so these tests don't change when the
lesson files do. Every decision goes through turn() or check_decision(), which also check two
promises each time: decide() never changes its input, and a reason never contains an answer key.
"""
from __future__ import annotations

import copy
import re
from fractions import Fraction

import pytest

from app import engine
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
        id="alpha", title="Alpha practice", ncert="test", reteach_step="a_intro",
        steps=[_step("a_intro"), _step("a_method"), _step("a_check")],
        checks=[_check("a1", "Eq(x**2 - 18*x + 77, 0)", traps={"sign_flip": "-7, -11"}),
                _check("a2", "Eq(x**2 - 4*x - 77, 0)", traps={"sign_flip": "-11, 7"}),
                _check("a3", "Eq(x**2 - 17*x + 72, 0)", traps={"sign_flip": "-8, -9"}),
                _check("a4", "Eq(5 + 2*x**2, 13*x)", kind="coefficient", which="b",
                       traps={"sign_when_moving": "13"})],
        mistakes=[_known("sign_flip"), _known("sign_when_moving")],
        strategies=["worked_example", "substitute_back", "compare_non_example"])
    beta = Concept(
        id="beta", title="Beta practice", ncert="test", reteach_step="b_what",
        steps=[_step("b_what"), _step("b_cases")],
        checks=[_check("b1", "Eq(x**2 + 9*x + 7, 0)", kind="discriminant", traps={"sign_in_D": "109"}),
                _check("b2", "Eq(x**2 + 8*x + 20, 0)", kind="nature"),
                _check("b3", "Eq(x*(x + 7), x**2 + 9)", kind="is_quadratic")],
        mistakes=[_known("sign_in_D")],
        strategies=["worked_example", "compare_non_example"])
    gamma = Concept(
        id="gamma", title="Gamma practice", ncert="test", reteach_step="g_plan",
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


# ---------- the current concept (spec §4.5) ----------
def test_current_concept_prefers_work_in_progress():
    assert engine.current_concept(progress_with(), CUR) == "alpha"
    assert engine.current_concept(progress_with(mastered("alpha"), asking("g1", cid="gamma")), CUR) == "gamma"
    assert engine.current_concept(progress_with(mastered("alpha")), CUR) == "beta"
    done = progress_with(mastered("alpha"), mastered("beta"), ConceptState("gamma", status="needs_review"))
    assert engine.current_concept(done, CUR) is None


# ---------- teaching (spec §4.3, first column) ----------
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
    # a later pass carries on after the last question asked (§4.2)
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


def test_unclear_while_teaching_repeats_last_reply():
    progress = progress_with(teaching_at("a_method"))
    for kind in ("say_again", "off_topic", "unclear"):
        decision, after = turn(progress, said(kind))
        assert (decision.rule, decision.action, decision.play_step_id, decision.ask_check_id) == \
            ("R8", "repeat", None, None)
        assert after == progress


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
    assert after["beta"].status == "teaching" and after["beta"].step_id == "b_what"


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
