"""Where a lesson picks up: Start or Continue (S0, S1), a topic the student picks (S2), lesson files that
changed between turns, and the daily turn limit. Pure functions, like engine.py: no I/O, inputs never changed."""
from __future__ import annotations

import copy

from app.config import SESSION_GAP_MIN
from app.curriculum import get_check, get_step, next_check_id, next_step_id
from app.engine import activate, current_concept, make_decision, resume_next
from app.models import ConceptState, Curriculum, Decision, Pending, Progress


def start(progress: Progress, cur: Curriculum, *, new_session: bool,
          replaced: bool = False) -> tuple[Decision, Progress]:
    """S0 (a new session) or S1 (carry on): resume the current concept where it stands. S1 clears the board
    only when the lesson files replaced the open question (replaced): that question is a new one."""
    after = copy.deepcopy(progress)
    if new_session:                                  # S0: every concept marked for review starts again
        for cid in cur.order:
            if after[cid].status == "needs_review":  # points, reteaches, strategies reset; last question kept
                after[cid] = ConceptState(cid, status="teaching", step_id=next_step_id(cur.concepts[cid], None),
                                          last_check_id=after[cid].last_check_id)
    cid = resume_next(after, cur)
    state = after[cid] if cid else None
    where = "we carry on where you left off" if cid else "every concept is mastered or marked for review"
    if new_session:
        return make_decision("S0", "greet", state, f"New session: concepts marked for review start again from step 1, "
                             f"and {where}.", resume_concept_id=cid, clear_board=True), after
    return make_decision("S1", "resume", state, f"You're back within {SESSION_GAP_MIN} minutes, so {where}.",
                         resume_concept_id=cid, clear_board=replaced), after


def jump(progress: Progress, cur: Curriculum, concept_id: str) -> tuple[Decision, Progress]:
    """S2: the student picked a concept. Every other active concept is paused just as it stands; the picked
    one carries on where it stopped, or starts again from step 1 if it was mastered or marked for review."""
    after = copy.deepcopy(progress)
    left = current_concept(after, cur)
    if left and after[left].status not in ("teaching", "checking"):
        left = None                                  # nothing was under way (e.g. a new student)
    for cid in cur.order:
        if cid != concept_id and after[cid].status in ("teaching", "checking"):
            after[cid].status = "paused"
    picked, title = after[concept_id], cur.concepts[concept_id].title
    again = picked.status in ("mastered", "needs_review")
    if again:                                        # a fresh pass; the next question follows the last one asked
        after[concept_id] = ConceptState(concept_id, last_check_id=picked.last_check_id)
    activate(after, cur, concept_id)
    reason = f"You picked {title}" + (", so it starts again from step 1." if again else ".")
    if left and left != concept_id:
        reason += f" {cur.concepts[left].title} is paused just as it stands."
    return make_decision("S2", "jump", after[concept_id], reason, resume_concept_id=concept_id, clear_board=True,
                         evidence={"from": left}), after


def reconcile(progress: Progress, cur: Curriculum) -> tuple[Progress, list[str], bool]:
    """Move students off removed steps and questions. Returns (progress, issues, question replaced)."""
    after, changed, replaced = copy.deepcopy(progress), False, False      # points are always kept
    current = current_concept(after, cur)
    for cid in cur.order:
        state, concept = after[cid], cur.concepts[cid]
        if state.status in ("teaching", "paused") and not state.pending and get_step(concept, state.step_id) is None:
            state.step_id, changed = next_step_id(concept, None), True       # back to the first step
        if state.pending and get_check(concept, state.pending.check_id) is None:
            check_id = next_check_id(concept, state.last_check_id)          # check 1 if that id is gone too
            state.pending, state.last_check_id = Pending(check_id), check_id
            changed, replaced = True, replaced or cid == current
    return after, ["curriculum_changed"] if changed else [], replaced


def lesson_changed(progress: Progress, cur: Curriculum) -> Decision:
    """R8 after reconcile() replaced the current question: this reply isn't marked; the new question is asked."""
    state = progress[current_concept(progress, cur)]
    return make_decision("R8", "lesson_changed", state, "The lesson changed, so your reply isn't marked and a new "
                         "question is asked. Points are kept.", ask_check_id=state.pending.check_id, clear_board=True)


def limit_decision() -> Decision:
    """LIMIT: today's turn limit is reached; nothing else changes."""
    return Decision(rule="LIMIT", action="limit", concept_id=None,
                    reason="Today's turn limit is reached, so the lesson continues tomorrow.")
