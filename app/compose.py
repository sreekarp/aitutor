"""The scripted parts of a reply after its words: a step, a question, the next concept, a re-ask, then the cue
that ends a teaching turn ("Shall we go on?") and the buttons that answer it. Pure functions."""
from __future__ import annotations

from app import curriculum, engine, replies
from app.models import BoardCommand, Choice, Curriculum, Decision, Progress, Segment, TurnContext

RESUMING = ("greet", "resume", "mastered", "needs_review", "jump")    # actions that go on to the current concept


def compose(cur: Curriculum, ctx: TurnContext, decision: Decision, after: Progress,
            with_cue: bool = True) -> list[Segment]:
    """The scripted parts after the words: a step, a question, the next concept, a re-ask, then the cue (unless
    with_cue is False: a lone "didn't catch that" asks for the reply itself, so it ends without one)."""
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
    cue, _ = cue_for(cur, after, shown_action(ctx, decision))
    if with_cue and cue and decision.rule not in NO_CUE and not _replays_a_cue(ctx, decision):
        parts.append(cue)                             # the turn ends on a cue: never a dead end
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


# ---------- the cue and the buttons ----------
NO_CUE = ("R10", "LIMIT", "END")          # safety, the daily limit and the end: no cue line and no buttons
NOT_SURE = Choice("Not sure", "not sure")
CHOICES = {"offer": (Choice("Teach me", "teach me"), Choice("Test me first", "test me")),
           "step": (Choice("Yes, go on", "yes go on"), NOT_SURE),
           "last_step": (Choice("Yes, quiz me", "yes quiz me"), NOT_SURE),
           "example": (Choice("Go on", "go on"), Choice("Another example", "another example"))}


def cue_for(cur: Curriculum, progress: Progress, last_action: str | None) -> tuple[Segment | None, list[Choice]]:
    """The cue line and the buttons for where a turn ends, in the current concept. A question open: no cue line (the
    question is the cue), just "Not sure". Every topic done: nothing."""
    cid = engine.current_concept(progress, cur)
    if cid is None:
        return None, []
    concept, state = cur.concepts[cid], progress[cid]
    if last_action == "safety":                        # a replayed safety reply (shown_action): no cue, no buttons
        return None, []
    if state.pending:
        return None, [NOT_SURE]
    if state.step_id is None:
        kind = "offer"                                 # the topic start: teach it, or test me first
    elif last_action == "example":
        kind = "example"
    elif state.step_id == concept.steps[-1].id:
        kind = "last_step"
    else:
        kind = "step"
    return Segment(say=replies.cue_line(kind, state.step_id or cid)), list(CHOICES[kind])


def shown_action(ctx: TurnContext, decision: Decision, apology: bool = False) -> str:
    """The action whose cue the student last heard: a replay (R8) of a solved example keeps the example's cue and
    buttons, so they still match what is said. A replay of the safety reply (R10) has neither, just as R10 itself;
    but a lone "didn't catch that" (apology) replays nothing, so it keeps the step's buttons."""
    said = ctx.last_speech
    replay = decision.action == "repeat" and ctx.phase == "teaching" and bool(said)
    if replay and not apology and said[-1].say == replies.SAFETY_REPLY:
        return "safety"
    return "example" if replay and said[-1].say in replies.CUES["example"] else decision.action


def _replays_a_cue(ctx: TurnContext, decision: Decision) -> bool:
    """R8 while teaching says the last turn's speech again (llm_steps.respond), which already ends on its cue.
    (A lone "didn't catch that" takes its place and asks for the reply itself, so it gets no cue either.)"""
    said = ctx.last_speech
    cues = {line for lines in replies.CUES.values() for line in lines}
    return decision.action == "repeat" and ctx.phase == "teaching" and bool(said) and said[-1].say in cues
