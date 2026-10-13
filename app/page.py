"""What the page gets: the state it draws after sign-in or a reload, and each turn's JSON. Neither ever
carries a decision's evidence or any SymPy check text."""
from __future__ import annotations

from app import compose, db, engine, replies
from app.models import BoardCommand, Concept, ConceptState, Progress, TurnResult
from app.tutor import Tutor


def state(tutor: Tutor, student_id: str) -> dict:
    """GET /api/state: progress per concept, the last 5 decisions (rule, action, reason), the board and the
    buttons for the next reply."""
    with tutor.db_engine.connect() as conn:
        progress = db.load_progress(conn, student_id, tutor.cur)
        session = db.latest_session(conn, student_id)
        lines = db.session_board(conn, session["id"]) if session else []
        recent = db.recent_decisions(conn, student_id)
        last = db.last_turn(conn, session["id"]) if session else None
    segments = last["tutor_output"]["segments"] if last else []
    safety_said = bool(segments) and segments[-1]["say"] == replies.SAFETY_REPLY    # R10, or "say again" after it
    current = engine.current_concept(progress, tutor.cur)
    rows = [_progress_row(tutor.cur.concepts[cid], progress[cid], cid == current) for cid in tutor.cur.order]
    why = [{"rule": d["rule"], "action": d["action"], "reason": d["reason"]} for d in recent]
    shown = [{"id": line.id, "latex": line.latex, "formula": line.formula, "text": line.text} for line in lines]
    choices = _saved_choices(tutor, progress, recent, safety_said)
    return {"progress": rows, "why": why, "board": shown, "choices": choices}


def payload(tutor: Tutor, student_id: str, result: TurnResult) -> dict:
    """The turn JSON the browser gets: never the decision's evidence, nor any SymPy check text."""
    decision = result.decision
    segments = [{"say": segment.say, "board": [_command_json(cmd, tutor.cur.formulas) for cmd in segment.board]}
                for segment in result.segments]
    return {"turn_id": result.turn_id, "transcript": result.transcript, "segments": segments,
            "decision": {"rule": decision.rule, "action": decision.action, "reason": decision.reason},
            **state(tutor, student_id), "input_hint": result.input_hint,
            "choices": [{"label": choice.label, "say": choice.say} for choice in result.choices]}


def _saved_choices(tutor: Tutor, progress: Progress, recent: list[dict], safety_said: bool = False) -> list[dict]:
    """The buttons after a reload: rebuilt from the saved progress and the newest decisions, as the turn chose them
    (a "say again" shows the buttons of the cue it replays, so the newest decision that isn't one decides; after R10
    that is R10, whose own turn had no buttons, but a later turn's cue gets them as usual)."""
    if not recent or recent[0]["rule"] in compose.NO_CUE or safety_said:
        return []              # before the first Start, or after safety (or its replay), the limit or the end
    action = next((decision["action"] for decision in recent if decision["action"] != "repeat"), recent[0]["action"])
    _, choices = compose.cue_for(tutor.cur, progress, action)
    return [{"label": choice.label, "say": choice.say} for choice in choices]


def _progress_row(concept: Concept, concept_state: ConceptState, current: bool) -> dict:
    """One concept for the progress panel; step_index counts from 0 and is only set while on a step (teaching,
    or paused on a step: a paused concept with an open question has none). at_start: teaching or paused at the
    topic start, before step 1."""
    step_ids = [step.id for step in concept.steps]
    on_step = concept_state.status in ("teaching", "paused") and not concept_state.pending
    teaching = on_step and concept_state.step_id in step_ids
    return {"concept_id": concept.id, "title": concept.title, "status": concept_state.status,
            "step_index": step_ids.index(concept_state.step_id) if teaching else None, "step_count": len(step_ids),
            "at_start": on_step and concept_state.step_id is None,
            "points": concept_state.points,
            "hint_level": concept_state.pending.hint_level if concept_state.pending else 0,
            "reteach_count": concept_state.reteach_count, "current": current}


def _command_json(cmd: BoardCommand, formulas: dict[str, str]) -> dict:
    """A board command for the browser: formula writes carry their LaTeX; the SymPy check stays on the server."""
    return {"op": cmd.op, "id": cmd.id, "latex": formulas.get(cmd.formula) if cmd.formula else cmd.latex,
            "formula": cmd.formula, "text": cmd.text, "target": cmd.target, "part": cmd.part}
