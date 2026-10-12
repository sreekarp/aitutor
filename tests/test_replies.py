"""Tests for app/replies.py: every scripted line the tutor says."""
import re

from conftest import check_of, fresh_progress

from app import curriculum, grading, mathcheck, replies
from app.models import ACTIONS, Check, ConceptState, Decision, Grade, Pending, Segment

LATEX_CHARS = set("\\^{}$_")
EXPECTED_SAFETY_TEXT = ("Thank you for telling me. If something is worrying you, please talk to a parent, a teacher or "
                    "another adult you trust. And please don't share personal details like your phone number or "
                    "address here.")


# ---------- helpers ----------
def spoken(cur, action, check=None, cid=None, progress=None, grade=None, first_name="Asha", **fields):
    """The `say` lines of replies.words() for one action."""
    decision = Decision(rule="R0", action=action, concept_id=cid, reason="test", **fields)
    segments = replies.words(decision, cur=cur, progress=progress or fresh_progress(cur), check=check,
                             grade=grade, first_name=first_name)
    return [segment.say for segment in segments]


# ---------- scripted replies ----------
def test_safety_reply_words(cur):
    assert spoken(cur, "safety") == [EXPECTED_SAFETY_TEXT]                    # the exact safety words


def test_templates_have_no_latex_and_no_keys(cur):
    """Every action's words are plain short speech; while a question is open they hold no numbers at all."""
    progress = fresh_progress(cur)
    progress["roots"].status, progress["discriminant"].status = "mastered", "needs_review"
    open_question = {"probe", "show_me", "repeat", "clarify", "follow_up", "lesson_changed"}   # hint, explain: hints
    grades = (Grade("partial", "", detail="missing"), Grade("partial", "", detail="extra"))
    for cid in cur.order:
        concept = cur.concepts[cid]
        for check in concept.checks:
            for action in ACTIONS:
                fields = {"hint_level": 1, "mistake": concept.mistakes[0].id, "resume_concept_id": cid}
                says = [say for grade in grades
                        for say in spoken(cur, action, check, cid, progress, grade, **fields)]
                says += [replies.reask(check, waiting).say for waiting in (False, True)]
                for say in says:
                    assert not set(say) & LATEX_CHARS and len(say.split()) <= 60, (check.id, action, say)
                    assert not curriculum.SPOKEN_SYMBOLS.search(say), (check.id, action, say)
                    assert action not in open_question or not re.search(r"\d", say), (check.id, action, say)
        progress[cid].step_id = concept.steps[0].id                   # while teaching: replays and repeats
        for action in ("explain", "repeat", "next_step"):
            for say in spoken(cur, action, None, cid, progress):
                assert not set(say) & LATEX_CHARS and len(say.split()) <= 60, (cid, action, say)


def test_hint_follow_up_clarify_explain_and_reteach(cur):
    fa1, concept = check_of(cur, "fa1"), cur.concepts["factorisation"]
    assert spoken(cur, "hint", fa1, "factorisation", hint_level=2) == [f"Here's a hint. {fa1.hints[1]}"]
    missing = spoken(cur, "follow_up", fa1, "factorisation", grade=Grade("partial", "2", detail="missing"))
    assert "missing" in missing[0]
    assert spoken(cur, "clarify", fa1) == ["Say just the numbers, or type them."]
    assert spoken(cur, "clarify", check_of(cur, "rt1")) == ["Just say yes or no."]
    progress = fresh_progress(cur)
    progress["factorisation"].step_id = "split_middle"                  # while teaching, the step is replayed
    explained = spoken(cur, "explain", None, "factorisation", progress)
    assert explained[1:] == [s.say for s in concept.steps[1].segments]
    fix = next(m.fix for m in concept.mistakes if m.id == "sign_flip")
    retaught = spoken(cur, "reteach", fa1, "factorisation", mistake="sign_flip")
    assert retaught[0] == f"Let's look at it another way. {fix}"
    assert retaught[1:] == [s.say for s in concept.steps[0].segments]   # reteach_step zero_product, replayed
    assert spoken(cur, "probe", check_of(cur, "rt1")) == ["Right — how do you know?"]
    assert spoken(cur, "probe", fa1) == ["Right — how did you get that?"]


def test_move_on_while_a_question_is_open_asks_it_first(cur):
    """R8 after "move on": the open question comes first. Compose asks it again after these words."""
    fa1 = check_of(cur, "fa1")
    assert spoken(cur, "repeat", fa1, "factorisation", evidence={"type": "move_on"}) == ["Let's get this one first."]
    assert spoken(cur, "repeat", fa1, "factorisation", evidence={"type": "unclear"}) == ["Let me ask that again."]


def test_explain_while_a_question_is_open_says_a_lesson_hint(cur):
    """A question while a question is open, with no key or after RESPOND failed: where to start, or once a hint
    was given, that last hint again. Never a new hint: R7 adds no hint level, so it stays free."""
    fa2 = check_of(cur, "fa2")
    for level, lead, hint in ((0, "Here's where to start.", fa2.hints[0]),
                              (1, "Let's look at the last hint again.", fa2.hints[0]),
                              (2, "Let's look at the last hint again.", fa2.hints[1])):
        assert spoken(cur, "explain", fa2, "factorisation", hint_level=level) == [f"Good question. {lead}", hint]


def test_praise_greeting_and_summary(cur):
    assert spoken(cur, "credit", check_of(cur, "di3"), "discriminant")[0].endswith(" — no real roots.")
    assert "two equal real roots" in spoken(cur, "credit", check_of(cur, "di2"), "discriminant")[0]
    assert spoken(cur, "credit", check_of(cur, "fa1"), "factorisation")[0].endswith("!")   # no number restated
    praise = {spoken(cur, "credit", check_of(cur, f"fa{n}"), "factorisation")[0] for n in range(1, 6)}
    assert len(praise) > 1                                              # it rotates by check id
    mastered = spoken(cur, "mastered", check_of(cur, "fa1"), "factorisation", resume_concept_id="quadratic_formula")
    assert mastered[0].endswith("! You've mastered solving by factorisation.")
    mastered = spoken(cur, "mastered", check_of(cur, "di3"), "discriminant", resume_concept_id="word_problems")
    assert mastered == ["Right — no real roots. You've mastered the discriminant and the nature of roots."]
    # The very last mastery (nothing to resume): the END summary right after it says what's mastered.
    assert spoken(cur, "mastered", check_of(cur, "di3"), "discriminant") == ["Right — no real roots."]
    progress = fresh_progress(cur)
    progress["standard_form"] = ConceptState("standard_form", status="teaching", step_id="hook")   # on step 1
    greeting = spoken(cur, "greet", None, "standard_form", progress, resume_concept_id="standard_form")
    assert greeting == ["Hi Asha! Let's start today with standard form and recognising a quadratic."]
    progress["discriminant"] = ConceptState("discriminant", status="teaching", step_id="what_is_d")
    assert spoken(cur, "greet", None, "discriminant", progress, resume_concept_id="discriminant") == [
        "Hi Asha! Let's start today with the discriminant and the nature of roots."]
    progress["roots"] = ConceptState("roots", status="teaching", step_id="definition", last_check_id="rt3")
    assert spoken(cur, "greet", None, "roots", progress, resume_concept_id="roots") == [   # S0 started it again
        "Hi Asha! Last time, roots and checking by substitution needed more practice, so let's go over it again."]
    progress["standard_form"].status, progress["roots"].status = "mastered", "needs_review"
    end = spoken(cur, "end", progress=progress)
    assert end == ["That's everything for now, Asha! You've mastered standard form and recognising a quadratic.",
                   "Next session we'll come back to roots and checking by substitution."]


def test_welcome_back_lines(cur):
    """A new session part-way through a concept, or with a question open, says we carry on, by name.
    Continue (S1) leaves the name out, so its audio is the same for every student and stays cached."""
    carry_on = "Let's carry on with roots and checking by substitution from where we left off."
    progress = fresh_progress(cur)
    progress["roots"] = ConceptState("roots", status="teaching", step_id="not_a_root")
    new_session = spoken(cur, "greet", None, "roots", progress, resume_concept_id="roots")
    assert new_session == [f"Welcome back, Asha! {carry_on}"]
    for name in ("Asha", "Ravi"):
        again = spoken(cur, "resume", None, "roots", progress, first_name=name, resume_concept_id="roots")
        assert again == [f"Welcome back! {carry_on}"], name
    progress["roots"] = ConceptState("roots", status="checking", pending=Pending("rt1"), last_check_id="rt1")
    asking = spoken(cur, "greet", None, "roots", progress, resume_concept_id="roots")
    assert asking == new_session                                        # a question is open: the same welcome
    for action in ("greet", "resume"):                  # nothing left to teach: the END summary after it has the name
        assert spoken(cur, action, progress=progress) == ["Welcome back!"], action


def test_greeting_at_a_topic_start_starts_the_topic(cur):
    """A topic at its start (step_id None, where Start leaves a new student) is started, not carried on."""
    progress = fresh_progress(cur)
    progress["roots"] = ConceptState("roots", status="teaching")
    assert spoken(cur, "greet", None, "roots", progress, resume_concept_id="roots") == [
        "Hi Asha! Let's start today with roots and checking by substitution."]


def test_names_typed_in_one_case_get_capitals(cur):
    progress = fresh_progress(cur)
    progress["roots"] = ConceptState("roots", status="teaching", step_id="definition")     # on step 1
    for typed, said in (("sreekar", "Sreekar"), ("SREEKAR", "Sreekar"), ("sai kiran", "Sai Kiran"),
                        ("McDonald", "McDonald")):                                        # mixed case is kept
        greeting = spoken(cur, "greet", None, "roots", progress, first_name=typed, resume_concept_id="roots")
        assert greeting == [f"Hi {said}! Let's start today with roots and checking by substitution."], typed


def test_opening_before_a_concepts_step_or_question(cur):
    """From step 1, the intro (the greeting named the concept). Right after another concept ended (R1 mastered,
    R9 needs review): that we move on to it, or go back to it when it was already started."""
    roots = cur.concepts["roots"]
    assert replies.opening(roots, "greet", True) == [Segment(say=roots.intro)]
    move_on = [Segment(say=f"Now let's move on to roots and checking by substitution. {roots.intro}")]
    assert replies.opening(roots, "mastered", True) == replies.opening(roots, "needs_review", True) == move_on
    back = [Segment(say="Let's get back to roots and checking by substitution, where we left off.")]
    assert replies.opening(roots, "mastered", False) == replies.opening(roots, "needs_review", False) == back
    for action, from_top in (("resume", True), ("resume", False), ("greet", False)):   # Continue, or carrying on
        assert replies.opening(roots, action, from_top) == [], (action, from_top)
    assert replies.opening(roots, "jump", True) == move_on and replies.opening(roots, "jump", False) == back


def test_praise_says_a_word_answer_in_words(cur):
    # "x = -2" was read aloud as "x equals 2": the praise now says the number the way the question does
    rt1 = check_of(cur, "rt1")
    said = spoken(cur, "credit", rt1, "roots")[0]
    assert said.endswith(" — yes, minus 2 is a root.") and "=" not in said, said


def test_jump_says_the_topic_left_is_saved(cur):
    def said(left):
        decision = Decision(rule="S2", action="jump", concept_id="factorisation", reason="test",
                            evidence={"from": left})
        return replies.words(decision, cur=cur, progress=fresh_progress(cur), check=None, grade=None,
                             first_name="Asha")
    assert said("roots") == [Segment(say="Sure. Roots and checking by substitution is saved just where you left it.")]
    assert said(None) == said("factorisation") == []           # nothing was left, or the same topic was picked


def test_answer_line_is_checked_or_text(cur):
    no_roots = Check(id="t1", say="Solve it.", label="Solve:", equation="Eq(x**2 + 1, 0)", kind="roots",
                     hints=["One.", "Two.", "Three."])
    for check in [c for concept in cur.concepts.values() for c in concept.checks] + [no_roots]:
        line, key = replies.answer_line(check), grading.answer_key(check)
        assert (line.op, line.id) == ("write", "answer")
        if check.kind == "roots" and key.values:
            assert line.text is None and line.relation == ("select" if check.keep else "equiv")
            assert mathcheck.problems([line.model_dump()], anchor=check.equation) == [], check.id
            claimed = list(mathcheck.solutions(mathcheck.parse_check(line.check)))
            assert len(claimed) == len(key.values)                       # the claim states exactly the key
            assert all(any(grading.same_value(a, b) for b in claimed) for a in key.values)
            assert line.latex.count(r"\text{ or }") == len(key.values) - 1
        else:
            assert (line.latex, line.check, line.text) == (None, None, grading.key_text(check)), check.id
    fa1 = check_of(cur, "fa1")
    moving_on = Decision("R9", "needs_review", "factorisation", "test", resume_concept_id="quadratic_formula")
    shown = replies.words(moving_on, cur=cur, progress=fresh_progress(cur), check=fa1, grade=None, first_name="Asha")
    assert shown[0] == Segment(say=f"{fa1.hints[-1]} Here is the answer.", board=[replies.answer_line(fa1)])
    assert "come back to this next time" in shown[1].say


def test_needs_review_says_we_come_back_unless_nothing_is_left(cur):
    """R9: the worked answer, then "we'll come back to this next time" as the next topic starts. After the very last
    topic, the END summary right after it says "Next session we'll come back to …", so it isn't said twice."""
    fa1 = check_of(cur, "fa1")
    worked = f"{fa1.hints[-1]} Here is the answer."
    moving_on = spoken(cur, "needs_review", fa1, "factorisation", resume_concept_id="quadratic_formula")
    assert moving_on == [worked, replies.REVIEW_LATER]
    assert spoken(cur, "needs_review", fa1, "factorisation") == [worked]          # nothing left to teach


def test_reask_per_kind(cur):
    expected = {"fa1": "So, what are the roots?", "wp1": "So, what is your answer?", "sf2": "So, what is b?",
                "sf4": "So, what is c?", "di1": "So, what is the discriminant?",
                "di2": "So, what is the nature of the roots?", "sf1": "So, is it a quadratic equation?",
                "rt1": "So, is it a root?", "wp2": "So, is it possible?"}
    for check_id, say in expected.items():
        check = check_of(cur, check_id)
        assert replies.reask(check, awaiting_working=False) == Segment(say=say), check_id
        assert replies.reask(check, awaiting_working=True).say == "So, how did you get your answer?"


def test_reask_after_hint_1_or_2_asks_for_that_step(cur):
    """One step at a time: right after hint 1 or 2 the student tries that step first. After hint 3, or words that
    weren't a hint (0), the usual re-ask; while "how did you get that?" is waiting, that question."""
    for check_id, what in (("fa2", "the roots"), ("wp1", "the answer"), ("di1", "the answer"), ("rt1", "the answer")):
        check = check_of(cur, check_id)
        for level in (1, 2):
            assert replies.reask(check, False, level) == Segment(say=f"Try that step, then tell me {what}."), check_id
        assert replies.reask(check, False, 3) == replies.reask(check, False, 0) == replies.reask(check, False)
        assert replies.reask(check, True, 1).say == "So, how did you get your answer?"
