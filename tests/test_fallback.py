"""Tests for app/fallback.py: scripted understanding and every scripted reply."""
import re

from conftest import fresh_progress

from app import fallback, grading, mathcheck
from app.models import ACTIONS, Check, ConceptState, Decision, Grade, Pending, Segment

LATEX_CHARS = set("\\^{}$_")
EXPECTED_SAFETY_TEXT = ("Thank you for telling me. If something is worrying you, please talk to a parent, a teacher or "
                    "another adult you trust. And please don't share personal details like your phone number or "
                    "address here.")


# ---------- helpers ----------
def check_of(cur, check_id):
    """A check from the real lesson files, by id."""
    return next(c for concept in cur.concepts.values() for c in concept.checks if c.id == check_id)


def understand(cur, text, check_id="fa1", *, phase="checking", awaiting_working=False):
    return fallback.understand(text, phase=phase, check=check_of(cur, check_id), awaiting_working=awaiting_working)


def spoken(cur, action, check=None, cid=None, progress=None, grade=None, first_name="Asha", **fields):
    """The `say` lines of fallback.words() for one action."""
    decision = Decision(rule="R0", action=action, concept_id=cid, reason="test", **fields)
    segments = fallback.words(decision, cur=cur, progress=progress or fresh_progress(cur), check=check,
                              grade=grade, first_name=first_name)
    return [segment.say for segment in segments]


# ---------- fast paths ----------
def test_keyword_whole_reply_only():
    for text in ("ok", "OK!", "ok next", "Haan theek hai", "go on", "accha"):
        assert fallback.keyword(text, "teaching") == "next", text
    for text in ("say again", "Repeat please", "phir se bolo"):
        assert fallback.keyword(text, "teaching") == fallback.keyword(text, "checking") == "say_again"
    for text in ("yesterday", "change", "ok, so why is it zero?", "repeat why that works", "did India win yesterday?"):
        assert fallback.keyword(text, "teaching") is None, text
    assert fallback.keyword("ok", "checking") is None                     # while checking, "ok" isn't "next"


# ---------- scripted understanding ----------
def test_understand_order_and_negations(cur):
    teaching = {"I don't get it": "dont_know", "I haven't understood": "dont_know", "samajh nahi aaya": "dont_know",
                "just tell me": "dont_know", "I get it": "claims_understanding", "samajh gaya": "claims_understanding",
                "Why is it zero?": "question", "how does that work": "question", "the weather is nice": "unclear"}
    for text, kind in teaching.items():
        assert understand(cur, text, phase="teaching").type == kind, text
    said = understand(cur, "I get it, it's 2 and 3")                      # an answer beats "I get it"
    assert (said.type, said.answer) == ("answer", "2, 3")
    assert understand(cur, "is it 2 and 3?").type == "answer"             # ... and beats "?"
    assert understand(cur, "I think it's 2 and 3").type == "answer"       # whole words: "think" isn't "hint"
    assert understand(cur, "I know it is yes", "rt1").answer == "yes"     # ... and "know" isn't "no"
    assert understand(cur, "I don't know, maybe 2").type == "dont_know"   # dont_know is checked first
    assert understand(cur, "2 and 3", phase="teaching").type == "unclear"  # answers are read only while checking


def test_my_number_is_an_answer_and_beats_me_is_unclear(cur):
    said = understand(cur, "My number is seven.", "sf2")                  # graded like any other answer
    assert (said.type, said.answer) == ("answer", "7")
    # Asked again, never hinted: a disclosure the safety check misses can end in "beats me" too.
    assert understand(cur, "Beats me.", "sf2").type == "unclear"


def test_scripted_answer_odd_formats(cur):
    roots = {"x = 2, 3": "2, 3", "x=2,x=3": "2, 3", "x = 2 or x = 3": "2, 3", "minus 2 and 3": "-2, 3",
             "2 plus root 3": "2 + sqrt(3)", "one by three, D is zero": "1/3", "2 & 3": "2, 3", "−2": "-2",
             "√2": "sqrt(2)", "2√3": "2*sqrt(3)", "1 ± √2": "1 ± sqrt(2)", "2 and 3.  ": "2, 3",
             "minus half": "-1/2", "no real roots": "none", "-2x and 3": "3"}
    for text, answer in roots.items():
        said = understand(cur, text, "fa1")
        assert (said.type, said.answer) == ("answer", answer), text
        assert grading.parse_answer(said.answer, "roots") is not None, text   # the grader can read it
    assert understand(cur, "one by three, D is zero", "fa1").showed_working    # D's value is working here
    for text, answer in {"D = 25": "25", "D is zero": "0", "d is minus 4": "-4"}.items():
        assert understand(cur, text, "di1").answer == answer, text
    assert understand(cur, "b = minus seven", "sf2").answer == "-7"
    words = {"haan": "yes", "nahi": "no", "two distinct real roots": "distinct", "no real roots": "none"}
    for text, answer in words.items():
        assert understand(cur, text, "di2" if answer in ("distinct", "none") else "rt1").answer == answer, text


def test_working_rules(cur):
    said = understand(cur, "haan, I think so", "rt1")
    assert (said.answer, said.showed_working) == ("yes", False)           # "so" cuts the answer; it isn't working
    assert understand(cur, "2 and 3", "fa1").showed_working is False      # the "d" in "and" isn't D
    assert understand(cur, "x = 2, 3", "fa1").showed_working is False     # a removed "x =" isn't working
    assert understand(cur, "(x-2)(x-3)=0 so 2 and 3", "fa1").showed_working
    said = understand(cur, "2 and 3 because they multiply to 6", "fa1")
    assert (said.answer, said.showed_working) == ("2, 3", True)
    said = understand(cur, "it has roots 2 and 3", "fa1")                 # the "as" in "has" isn't a marker
    assert (said.answer, said.showed_working) == ("2, 3", False)
    assert understand(cur, "2 and 3, I factorised", "fa1").showed_working


def test_working_only_reply_while_awaiting(cur):
    for text in ("I factorised it", "I split the middle term", "(x-2)(x-3) = 0"):
        said = understand(cur, text, "fa1", awaiting_working=True)
        assert (said.type, said.answer, said.showed_working) == ("answer", "", True), text
        assert understand(cur, text, "fa1").type == "unclear"            # not awaiting working: unclear
    assert understand(cur, "I just guessed", "fa1", awaiting_working=True).type == "unclear"


def test_understand_non_latin_and_long(cur):
    for text in ("हाँ", "👍👍", "", "a" * 2000):
        for phase in ("teaching", "checking"):
            said = understand(cur, text, phase=phase)
            assert (said.type, said.answer, said.safety) == ("unclear", "", False), (text[:10], phase)
        assert fallback.keyword(text, "teaching") is None
    filler = "blah " * 100                                               # 500 characters: the rest is cut
    assert understand(cur, filler + "I don't know").type == "unclear"
    assert understand(cur, "I don't know " + filler).type == "dont_know"
    said = understand(cur, "2, " * 700)                                  # a long paste of numbers
    assert said.type == "answer" and grading.grade(check_of(cur, "fa1"), said.answer).verdict == "unsure"


# ---------- scripted replies ----------
def test_safety_reply_words(cur):
    assert spoken(cur, "safety") == [EXPECTED_SAFETY_TEXT]                    # the exact safety words


def test_templates_have_no_latex_and_no_keys(cur):
    """Every action's words are plain short speech; while a question is open they hold no numbers at all."""
    progress = fresh_progress(cur)
    progress["roots"].status, progress["discriminant"].status = "mastered", "needs_review"
    open_question = {"probe", "show_me", "repeat", "clarify", "follow_up", "explain", "lesson_changed"}
    grades = (Grade("partial", "", detail="missing"), Grade("partial", "", detail="extra"))
    for cid in cur.order:
        concept = cur.concepts[cid]
        for check in concept.checks:
            for action in ACTIONS:
                fields = {"hint_level": 1, "mistake": concept.mistakes[0].id, "resume_concept_id": cid}
                says = [say for grade in grades
                        for say in spoken(cur, action, check, cid, progress, grade, **fields)]
                says += [fallback.reask(check, waiting).say for waiting in (False, True)]
                for say in says:
                    assert not set(say) & LATEX_CHARS and len(say.split()) <= 60, (check.id, action, say)
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
    assert spoken(cur, "explain", fa1, "factorisation") == ["Good question — let's go over it again."]
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
    progress["standard_form"] = ConceptState("standard_form", status="teaching", step_id="hook")   # as S0 leaves it
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


def test_names_typed_in_one_case_get_capitals(cur):
    progress = fresh_progress(cur)
    progress["roots"] = ConceptState("roots", status="teaching", step_id="definition")     # as S0 leaves it
    for typed, said in (("sreekar", "Sreekar"), ("SREEKAR", "Sreekar"), ("sai kiran", "Sai Kiran"),
                        ("McDonald", "McDonald")):                                        # mixed case is kept
        greeting = spoken(cur, "greet", None, "roots", progress, first_name=typed, resume_concept_id="roots")
        assert greeting == [f"Hi {said}! Let's start today with roots and checking by substitution."], typed


def test_opening_before_a_concepts_step_or_question(cur):
    """From step 1, the intro (the greeting named the concept). Right after another concept ended (R1 mastered,
    R9 needs review): that we move on to it, or go back to it when it was already started."""
    roots = cur.concepts["roots"]
    assert fallback.opening(roots, "greet", True) == [Segment(say=roots.intro)]
    move_on = [Segment(say=f"Now let's move on to roots and checking by substitution. {roots.intro}")]
    assert fallback.opening(roots, "mastered", True) == fallback.opening(roots, "needs_review", True) == move_on
    back = [Segment(say="Let's get back to roots and checking by substitution, where we left off.")]
    assert fallback.opening(roots, "mastered", False) == fallback.opening(roots, "needs_review", False) == back
    for action, from_top in (("resume", True), ("resume", False), ("greet", False)):   # Continue, or carrying on
        assert fallback.opening(roots, action, from_top) == [], (action, from_top)


def test_answer_line_is_checked_or_text(cur):
    no_roots = Check(id="t1", say="Solve it.", label="Solve:", equation="Eq(x**2 + 1, 0)", kind="roots",
                     hints=["One.", "Two.", "Three."])
    for check in [c for concept in cur.concepts.values() for c in concept.checks] + [no_roots]:
        line, key = fallback.answer_line(check), grading.answer_key(check)
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
    shown = fallback.words(Decision("R9", "needs_review", "factorisation", "test"), cur=cur,
                           progress=fresh_progress(cur), check=fa1, grade=None, first_name="Asha")
    assert shown[0] == Segment(say=f"{fa1.hints[-1]} Here is the answer.", board=[fallback.answer_line(fa1)])
    assert "come back to this next time" in shown[1].say


def test_reask_per_kind(cur):
    expected = {"fa1": "So, what are the roots?", "wp1": "So, what is your answer?", "sf2": "So, what is b?",
                "sf4": "So, what is c?", "di1": "So, what is the discriminant?",
                "di2": "So, what is the nature of the roots?", "sf1": "So, is it a quadratic equation?",
                "rt1": "So, is it a root?", "wp2": "So, is it possible?"}
    for check_id, say in expected.items():
        check = check_of(cur, check_id)
        assert fallback.reask(check, awaiting_working=False) == Segment(say=say), check_id
        assert fallback.reask(check, awaiting_working=True).say == "So, how did you get your answer?"
