"""The scripted parts of a reply that come after its words: a step, a question, the concept picked up next, the
END summary, a short re-ask. Pure functions, like engine.py: no I/O, inputs never changed."""
from __future__ import annotations

from app import curriculum, engine, replies
from app.models import BoardCommand, Curriculum, Decision, Progress, Segment, TurnContext

RESUMING = ("greet", "resume", "mastered", "needs_review", "jump")    # actions that go on to the current concept


def compose(cur: Curriculum, ctx: TurnContext, decision: Decision, after: Progress) -> list[Segment]:
    """The scripted parts after the words: a step, a question, the next concept, a re-ask."""
    concept = cur.concepts.get(decision.concept_id or "")
    parts: list[Segment] = []
    if decision.play_step_id:
        parts += curriculum.get_step(concept, decision.play_step_id).segments
    if decision.ask_check_id:
        check = curriculum.get_check(concept, decision.ask_check_id)
        parts.append(curriculum.ask_segment(check, cur.formulas))
    if decision.resume_concept_id:
        parts += resume(cur, decision.resume_concept_id, after, decision.action)
    elif decision.action in RESUMING:                 # moving on, but nothing is left: the END summary
        parts += replies.words(engine.end_decision(), cur=cur, progress=after, check=None, grade=None,
                               first_name=ctx.student["first_name"])
    pending = after[concept.id].pending if concept else None
    if decision.reask and pending:
        hint_level = decision.hint_level if decision.action == "hint" else 0     # the hint just given, if any
        asked = curriculum.get_check(concept, pending.check_id)
        parts.append(replies.reask(asked, pending.awaiting_working, hint_level))
    if decision.clear_board and parts:                # wipe the board just before the new part plays
        parts[0] = Segment(say=parts[0].say, board=[BoardCommand(op="clear"), *parts[0].board])
    return parts


def resume(cur: Curriculum, concept_id: str, after: Progress, action: str) -> list[Segment]:
    """Pick a concept up where it stands: its opening (replies.opening), then its pending question, else its
    step. At the topic start (step_id None) just the opening: step 1 waits for the student's reply."""
    concept, state = cur.concepts[concept_id], after[concept_id]
    step = curriculum.get_step(concept, state.step_id)              # None at the topic start
    from_top = not state.pending and (step is None or step.id == concept.steps[0].id)
    opening = replies.opening(concept, action, from_top=from_top)
    if state.pending:
        check = curriculum.get_check(concept, state.pending.check_id)
        return opening + [curriculum.ask_segment(check, cur.formulas)]
    return opening + (list(step.segments) if step else [])
