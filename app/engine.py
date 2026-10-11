"""The teaching rules (spec §4): which move comes next, and why. Pure functions: no I/O, inputs never changed."""
from __future__ import annotations

import copy

from app.config import MAX_HINTS, POINTS_TO_MASTER
from app.curriculum import next_check_id, next_step_id
from app.models import Concept, ConceptState, Curriculum, Decision, Grade, Pending, Progress, Understood

# How R8's reason starts, by reply type.
REPEAT_CAUSE = {"next": "That reply didn't answer the question", "say_again": "You asked to hear it again",
                "off_topic": "That reply was off-topic", "unclear": "That reply wasn't clear"}


def current_concept(progress: Progress, cur: Curriculum) -> str | None:
    """The first concept being taught or checked, else the first not started, else None (spec §4.5)."""
    for cid in cur.order:
        if progress[cid].status in ("teaching", "checking"):
            return cid
    for cid in cur.order:
        if progress[cid].status == "not_started":
            return cid
    return None


def decide(progress: Progress, cur: Curriculum, understood: Understood,
           grade: Grade | None) -> tuple[Decision, Progress]:
    """Pick exactly one rule for this reply (spec §4.3, §4.4); return it with the new progress."""
    after = copy.deepcopy(progress)               # all changes happen on this copy
    cid = current_concept(after, cur)
    state = after[cid] if cid is not None else None
    asked = state.pending.check_id if state and state.pending else None
    if understood.safety:
        decision = _make("R10", "safety", state, "Safety comes first: the reply may need an adult's help, "
                         "so the lesson waits. Progress is kept.")
    elif state is None:
        decision = end_decision()
    elif state.pending is None:
        decision = _teaching(state, cur.concepts[cid], understood)
    else:
        decision = _checking(after, cur, state, understood, grade)
    # the facts behind the decision, for the decisions table (the turn row keeps the full reply and grade)
    decision.evidence.update(check_id=asked, type=understood.type, verdict=grade.verdict if grade else None)
    return decision, after


def end_decision() -> Decision:
    """END: no concept is left to teach; the same reply answers every later turn."""
    return Decision(rule="END", action="end", concept_id=None,
                    reason="Every concept is mastered or marked for review, so this session's lessons are done.")


# ---------- the two columns of the §4.3 table ----------
def _teaching(state: ConceptState, concept: Concept, understood: Understood) -> Decision:
    kind, total = understood.type, len(concept.steps)
    if kind in ("next", "answer"):
        step_id = next_step_id(concept, state.step_id)
        if step_id is None:
            return _ask_next(state, concept, "R0", "start_check",
                             f"Teaching: all {total} steps done, so a question checks what you learned.")
        state.status, state.step_id = "teaching", step_id      # also starts a concept not started yet
        number = [step.id for step in concept.steps].index(step_id) + 1
        return _make("R0", "next_step", state, f"Teaching: you're ready, so step {number} of {total}.",
                     play_step_id=step_id)
    if kind == "claims_understanding":
        return _ask_next(state, concept, "R6", "start_check",
                         "You said you get it, so the remaining steps are skipped and a question checks it.")
    if kind == "question":
        return _make("R7", "explain", state, "You asked a question, so it is answered. Same step.")
    if kind == "dont_know":
        return _make("R7", "explain", state, "You said you don't follow, so this step is explained again.")
    return _make("R8", "repeat", state, f"{REPEAT_CAUSE[kind]}, so the last reply is said again. Nothing changes.")


def _checking(after: Progress, cur: Curriculum, state: ConceptState, understood: Understood,
              grade: Grade | None) -> Decision:
    kind, check_id = understood.type, state.pending.check_id
    if kind == "answer":
        return _answer(after, cur, state, understood, grade)
    if kind == "question":
        return _make("R7", "explain", state, "You asked a question, so it is answered first. Same question.",
                     reask=True)
    if kind == "dont_know":
        return _hint(after, cur, state, "You asked for help.", wrong_values=False)
    if kind == "claims_understanding":
        return _make("R6", "show_me", state, "Points need an answer with working, so: show me. Same question.",
                     ask_check_id=check_id)
    return _make("R8", "repeat", state, f"{REPEAT_CAUSE[kind]}, so the question is asked again. Nothing changes.",
                 ask_check_id=check_id)


# ---------- answers while checking (the order of checks in §4.4) ----------
def _answer(after: Progress, cur: Curriculum, state: ConceptState, understood: Understood,
            grade: Grade | None) -> Decision:
    pending, concept = state.pending, cur.concepts[state.concept_id]
    has_answer = bool(understood.answer.strip())
    if pending.awaiting_working and not has_answer:          # a reply to "how did you get that?"
        if understood.showed_working:
            return _credit(after, cur, state, "You showed the working for your right answer.")
        return _hint(after, cur, state, "The working still wasn't shown.", wrong_values=False)
    if has_answer:                                           # any answer is graded afresh
        pending.awaiting_working = False
        pending.last_answer = understood.answer
    verdict = grade.verdict if grade and has_answer else "unsure"
    if verdict == "correct" and understood.showed_working:
        return _credit(after, cur, state, "SymPy: correct, with working.")
    if verdict == "correct":
        pending.awaiting_working = True
        return _make("R2", "probe", state,
                     "SymPy: correct, but the working wasn't shown. Points come once you say how.")
    if verdict == "wrong":
        return _wrong(after, cur, state, "SymPy: not the answer.", _mistake(concept, understood, grade),
                      grade.wrong_values)
    if not pending.followup_used:                            # partial or unsure: one follow-up first
        pending.followup_used = True
        if verdict == "unsure":
            return _make("R5", "clarify", state, "An answer couldn't be read from that reply, "
                         "so one follow-up asks for just the answer.")
        what = "a value is missing" if grade.detail == "missing" else "a value doesn't fit the question"
        return _make("R5", "follow_up", state, f"SymPy: partly right ({what}). One follow-up; not counted wrong.",
                     mistake=_mistake(concept, understood, grade), reask=True)
    if verdict == "partial":
        return _wrong(after, cur, state, "SymPy: still only partly right.", _mistake(concept, understood, grade),
                      grade.wrong_values)
    return _make("R8", "repeat", state, "Still couldn't read an answer, so the question is asked again. "
                 "An unreadable answer never leads to a hint.", ask_check_id=pending.check_id)


def _mistake(concept: Concept, understood: Understood, grade: Grade) -> str | None:
    """Spec §5.4: a trap answer is certain; otherwise the LLM's suggestion, if it's listed."""
    if grade.trap:
        return grade.trap
    listed = {mistake.id for mistake in concept.mistakes}
    if grade.verdict in ("wrong", "partial") and understood.mistake in listed:
        return understood.mistake
    return None


def _wrong(after: Progress, cur: Curriculum, state: ConceptState, cause: str, mistake: str | None,
           wrong_values: bool) -> Decision:
    """R3 (reteach) when the mistake is known, otherwise R4 (hint)."""
    if mistake:
        return _reteach(after, cur, state, "R3", f"{cause} Known mistake: {mistake}.", mistake, wrong_values)
    return _hint(after, cur, state, cause, wrong_values)


def _hint(after: Progress, cur: Curriculum, state: ConceptState, cause: str, wrong_values: bool) -> Decision:
    """R4: the next hint on the same question; once all 3 are used, a reteach with no target mistake."""
    pending = state.pending
    if pending.hint_level >= MAX_HINTS:
        return _reteach(after, cur, state, "R4", f"{cause} All {MAX_HINTS} hints used.", None, wrong_values)
    pending.hint_level += 1
    return _make("R4", "hint", state, f"{cause} Hint {pending.hint_level} of {MAX_HINTS}.", reask=True)


def _credit(after: Progress, cur: Curriculum, state: ConceptState, cause: str) -> Decision:
    """R1: +1 point (+0.5 if a hint was given); mastered at 2, otherwise the next question."""
    concept = cur.concepts[state.concept_id]
    gain = 0.5 if state.pending.hint_level > 0 else 1.0
    state.points += gain
    hint_note = " (a hint was used)" if gain < 1 else ""
    score = f"+{gain:g} point{hint_note}, now {state.points:g} of {POINTS_TO_MASTER:g}"
    if state.points < POINTS_TO_MASTER:
        return _ask_next(state, concept, "R1", "credit", f"{cause} {score}.")
    state.status, state.pending = "mastered", None
    return _make("R1", "mastered", state, f"{cause} {score}, so {concept.title} is mastered.",
                 resume_concept_id=_resume_next(after, cur), clear_board=True)


def _reteach(after: Progress, cur: Curriculum, state: ConceptState, rule: str, cause: str,
             mistake: str | None, wrong_values: bool) -> Decision:
    """R3, or R4 after the last hint: reteach, then the next question. Task 8 writes it."""
    raise NotImplementedError("reteaching is added in Task 8")


# ---------- small shared helpers ----------
def _ask_next(state: ConceptState, concept: Concept, rule: str, action: str, reason: str, **extra) -> Decision:
    """Ask the next question (spec §4.2). Asking any question starts a fresh pending."""
    check_id = next_check_id(concept, state.last_check_id)
    state.status, state.pending, state.last_check_id = "checking", Pending(check_id), check_id
    return _make(rule, action, state, reason, ask_check_id=check_id, **extra)


def _resume_next(after: Progress, cur: Curriculum) -> str | None:
    """The concept to resume (spec §4.5); one not started yet begins at its first step."""
    cid = current_concept(after, cur)
    if cid is not None and after[cid].status == "not_started":
        after[cid].status = "teaching"
        after[cid].step_id = next_step_id(cur.concepts[cid], None)
    return cid


def _make(rule: str, action: str, state: ConceptState | None, reason: str, **extra) -> Decision:
    """A Decision about this concept, carrying its current hint level and reteach count."""
    if state is None:
        return Decision(rule=rule, action=action, concept_id=None, reason=reason, **extra)
    hint_level = state.pending.hint_level if state.pending else 0
    return Decision(rule=rule, action=action, concept_id=state.concept_id, reason=reason,
                    hint_level=hint_level, reteach_count=state.reteach_count, **extra)
