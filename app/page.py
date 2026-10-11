"""What the page gets: the state it draws after sign-in or a reload, and each turn's JSON. Neither ever
carries a decision's evidence or any SymPy check text."""
from __future__ import annotations

from app import db, engine
from app.models import BoardCommand, Concept, ConceptState, TurnResult
from app.tutor import Tutor


def state(tutor: Tutor, student_id: str) -> dict:
    """GET /api/state: progress per concept, the last 5 decisions (rule, action, reason) and the board."""
    with tutor.db_engine.connect() as conn:
        progress = db.load_progress(conn, student_id, tutor.cur)
        session = db.latest_session(conn, student_id)
        lines = db.session_board(conn, session["id"]) if session else []
        recent = db.recent_decisions(conn, student_id)
    current = engine.current_concept(progress, tutor.cur)
    rows = [_progress_row(tutor.cur.concepts[cid], progress[cid], cid == current) for cid in tutor.cur.order]
    why = [{"rule": d["rule"], "action": d["action"], "reason": d["reason"]} for d in recent]
    shown = [{"id": line.id, "latex": line.latex, "formula": line.formula, "text": line.text} for line in lines]
    return {"progress": rows, "why": why, "board": shown}


def payload(tutor: Tutor, student_id: str, result: TurnResult) -> dict:
    """The turn JSON the browser gets: never the decision's evidence, nor any SymPy check text."""
    decision = result.decision
    segments = [{"say": segment.say, "board": [_command_json(cmd, tutor.cur.formulas) for cmd in segment.board]}
                for segment in result.segments]
    return {"turn_id": result.turn_id, "transcript": result.transcript, "segments": segments,
            "decision": {"rule": decision.rule, "action": decision.action, "reason": decision.reason},
            **state(tutor, student_id), "input_hint": result.input_hint}


def _progress_row(concept: Concept, concept_state: ConceptState, current: bool) -> dict:
    """One concept for the progress panel; step_index counts from 0 and is only set while teaching."""
    step_ids = [step.id for step in concept.steps]
    teaching = concept_state.status == "teaching" and concept_state.step_id in step_ids
    return {"concept_id": concept.id, "title": concept.title, "status": concept_state.status,
            "step_index": step_ids.index(concept_state.step_id) if teaching else None, "step_count": len(step_ids),
            "points": concept_state.points,
            "hint_level": concept_state.pending.hint_level if concept_state.pending else 0,
            "reteach_count": concept_state.reteach_count, "current": current}


def _command_json(cmd: BoardCommand, formulas: dict[str, str]) -> dict:
    """A board command for the browser: formula writes carry their LaTeX; the SymPy check stays on the server."""
    return {"op": cmd.op, "id": cmd.id, "latex": formulas.get(cmd.formula) if cmd.formula else cmd.latex,
            "formula": cmd.formula, "text": cmd.text, "target": cmd.target, "part": cmd.part}
