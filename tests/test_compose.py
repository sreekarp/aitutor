"""compose.py with the real lesson files: the scripted parts that follow a reply's words (a question, a re-ask,
the concept picked up next, or the END summary). Pure functions: no database, no model. The whole turns in
test_tutor.py and test_topic_starts.py cover the same parts as the student hears them."""
from conftest import check_of, fresh_progress, new_question

from app import compose, curriculum
from app.models import BoardCommand, ConceptState, Decision, Pending, Segment, TurnContext

BACK_TO_SF = "Let's get back to standard form and recognising a quadratic, where we left off."


def context(progress) -> TurnContext:
    """A turn by Asha. compose reads only her first name (for the END summary)."""
    return TurnContext(student={"first_name": "Asha"}, session={}, concept=None, phase="teaching", before=progress,
                       check=None, text="", last_speech=[], previous_reason="", board=[], deadline=0.0, calls=[],
                       issues=[], latency={})


def asking(cur, concept_id: str, check_id: str, status: str = "checking", hint_level: int = 0):
    """Every concept before this one mastered, and this one with check_id open (checking, or paused)."""
    progress = fresh_progress(cur)
    for cid in cur.order[:cur.order.index(concept_id)]:
        progress[cid].status = "mastered"
    progress[concept_id] = ConceptState(concept_id, status=status, pending=Pending(check_id, hint_level=hint_level),
                                        last_check_id=check_id)
    return progress


def test_a_new_question_is_asked_on_a_clean_board(cur):
    after = asking(cur, "factorisation", "fa2")
    decision = Decision("R1", "credit", "factorisation", "test", ask_check_id="fa2", clear_board=True)
    assert compose.compose(cur, context(after), decision, after) == [new_question(check_of(cur, "fa2"), cur.formulas)]


def test_a_hint_or_an_explanation_ends_with_the_short_reask(cur):
    """After hint 1 the student tries that step first; after an explanation the question is asked again."""
    after = asking(cur, "factorisation", "fa1", hint_level=1)
    hint = Decision("R4", "hint", "factorisation", "test", hint_level=1, reask=True)
    assert compose.compose(cur, context(after), hint, after) == [Segment(say="Try that step, then tell me the roots.")]
    explain = Decision("R7", "explain", "factorisation", "test", hint_level=1, reask=True)
    assert compose.compose(cur, context(after), explain, after) == [Segment(say="So, what are the roots?")]


def test_moving_on_with_nothing_left_adds_the_end_summary(cur):
    """The last concept mastered: END's summary follows the praise, with Asha's name, on a clean board."""
    after = fresh_progress(cur)
    for state in after.values():
        state.status = "mastered"
    decision = Decision("R1", "mastered", "word_problems", "test", clear_board=True)
    parts = compose.compose(cur, context(after), decision, after)
    assert [segment.say for segment in parts] == [
        "That's everything for now, Asha! You've mastered standard form and recognising a quadratic, roots and "
        "checking by substitution, solving by factorisation, the quadratic formula, the discriminant and the nature "
        "of roots and word problems."]
    assert parts[0].board == [BoardCommand(op="clear")]


def test_a_picked_topic_opens_on_a_clean_board_and_asks_its_question_again(cur):
    after = asking(cur, "standard_form", "sf2")
    decision = Decision("S2", "jump", "standard_form", "test", resume_concept_id="standard_form", clear_board=True)
    assert compose.compose(cur, context(after), decision, after) == [
        Segment(say=BACK_TO_SF, board=[BoardCommand(op="clear")]),
        curriculum.ask_segment(check_of(cur, "sf2"), cur.formulas)]


def test_resume_picks_a_concept_up_where_it_stands(cur):
    """An open question: "let's get back to it", then the question. Continue (S1) on step 2: just step 2."""
    progress = asking(cur, "standard_form", "sf2", status="paused")
    assert compose.resume(cur, "standard_form", progress, "jump") == [
        Segment(say=BACK_TO_SF), curriculum.ask_segment(check_of(cur, "sf2"), cur.formulas)]
    step_2 = cur.concepts["factorisation"].steps[1]
    progress["factorisation"] = ConceptState("factorisation", status="teaching", step_id=step_2.id)
    assert compose.resume(cur, "factorisation", progress, "resume") == step_2.segments
