"""A reply the student stopped part-way (Stop, Esc, or answering before it ended). The page says how many of its
segments had been said in full and how many were shown, and the saved board goes back to what was on screen. After
that, "repeat" says all of the reply again and "continue" the part not yet said (R8); Start or Continue picks it up,
from where it stopped (S1) or, in a new session, from its start (S0). Any other reply moves on, and the part not said
is dropped, unless that part held the open question: then it is said first."""
from __future__ import annotations

import re
from dataclasses import replace

from sqlalchemy import Engine

from app import board, compose, db, engine, fallback, safety
from app.models import (PINNED_IDS, BoardCommand, BoardLine, Choice, Curriculum, Decision, Progress, Segment, Stopped,
                        TurnContext)

# "continue", "carry on", "resume" or "keep going", maybe with "ok" or "yes" before or "please" after.
CONTINUE = re.compile(r"(?:(?:ok|okay|yes|yeah|please)\s+)?(?:continue|carry on|resume|keep going)(?:\s+please)?")
REASONS = {"all": "You asked to hear it again, so all of it is said again.",
           "rest": "You asked me to carry on, so the rest of it is said.",
           "unseen": "You stopped me before the question, so the rest of my reply, with the question, comes first."}


def record(db_engine: Engine, cur: Curriculum, student_id: str, turn_id: str, heard: int, shown: int) -> bool:
    """Keep where the student stopped their newest turn's reply, and put the saved board back to what was on screen
    by then. False when turn_id isn't their newest turn: a late or stray stop changes nothing."""
    with db_engine.begin() as conn:
        session = db.latest_session(conn, student_id)
        turn = db.last_turn(conn, session["id"]) if session else None
        if turn is None or turn["id"] != turn_id:
            return False
        output = turn["tutor_output"]
        if heard < len(output["segments"]):                   # else all of it was said: nothing to keep
            shown = min(max(shown, heard), len(output["segments"]))
            db.save_stop(conn, turn_id, student_id, heard, shown)
            if "board_before" in output:
                on_screen = [Segment.model_validate(segment) for segment in output["segments"][:shown]]
                lines = [BoardLine(**line) for line in output["board_before"]]
                db.set_session_board(conn, session["id"], board.apply(lines, on_screen, cur.formulas))
    return True


def find(turn: dict | None) -> Stopped | None:
    """The turn's reply if the student stopped it part-way (a row from db.last_turn), else None."""
    if turn is None or turn["heard"] is None:
        return None
    output = turn["tutor_output"]
    return Stopped(segments=[Segment.model_validate(segment) for segment in output["segments"]], heard=turn["heard"],
                   shown=turn["shown"], board_before=[BoardLine(**line) for line in output.get("board_before", [])],
                   choices=[Choice(**choice) for choice in output.get("choices", [])])


def replay_kind(text: str) -> str | None:
    """After a stop: "all" for a reply asking to hear it again, "rest" for one asking to carry on, else None."""
    clean = fallback.plain(text)
    if fallback.SAY_AGAIN.fullmatch(clean):
        return "all"
    return "rest" if CONTINUE.fullmatch(clean) else None


def which_replay(ctx: TurnContext, formulas: dict[str, str], topic_asked: bool) -> str | None:
    """Right after a stop: "all" or "rest" when the reply asks for it. Any other reply gets "unseen" when the open
    question isn't on the board yet (the part never shown would put it there), since it can't be answered unseen; but
    a reply the safety check flags and a topic asked for come first. None: no replay, the turn goes on as usual."""
    if ctx.stopped is None:
        return None
    which = replay_kind(ctx.text)
    if which or ctx.check is None or topic_asked or safety.is_safety(ctx.text):
        return which
    to_come = board.apply(ctx.board, ctx.stopped.segments[ctx.stopped.shown:], formulas)
    return "unseen" if _question_lines(to_come) != _question_lines(ctx.board) else None


def replay(ctx: TurnContext, which: str) -> tuple[Decision, list[Segment]]:
    """R8 after a stop: the stopped reply again with its board, all of it or from the segment it stopped in. Nothing
    changes."""
    stopped, state = ctx.stopped, ctx.before[ctx.concept.id] if ctx.concept else None
    words = stopped.segments if which == "all" else stopped.segments[stopped.heard:]
    decision = engine.make_decision("R8", "repeat", state, REASONS[which] + " Nothing changes.",
                                    evidence={"replay": which})
    return decision, words


def is_replay(decision: Decision) -> bool:
    """A stopped reply said again (R8), or picked up at Start or Continue: it keeps its own cue and buttons."""
    return "replay" in decision.evidence


def can_pick_up(last: dict, replaced: bool, before: Progress, after: Progress) -> bool:
    """Start or Continue picks a stopped reply up, except: a Start or Continue itself (it would greet twice), a safety
    reply, the daily limit or the end (no lesson to go back to), a question the lesson files replaced, or a new session
    that changed progress (a topic marked for review starts again). Then the lesson resumes as usual."""
    return last["input"] != "start" and last["rule"] not in compose.NO_CUE and not replaced and after == before


def pick_up(decision: Decision, greeting: list[Segment], stopped: Stopped,
            new_session: bool) -> tuple[Decision, list[Segment]]:
    """Start or Continue after a stop, in place of the usual resume: the greeting, then the stopped reply from where
    it stopped (S1); in a new session, all of it, on the board it began on (S0 starts on a clean board)."""
    if new_session:
        first, *rest = stopped.segments
        restore = [BoardCommand(op="clear"), *map(_write, stopped.board_before)]
        words, which = [Segment(say=first.say, board=restore + first.board), *rest], "all"
        reason = " You'd stopped me part-way, so that reply is said again from its start."
    else:
        words, which = stopped.segments[stopped.heard:], "rest"
        reason = " You'd stopped me part-way, so I carry on from there."
    # a "repeat" action: a later "say again" or reload treats it as the replay it is (its buttons are the reply's own)
    return replace(decision, action="repeat", reason=decision.reason + reason, resume_concept_id=None,
                   clear_board=False, evidence={**decision.evidence, "replay": which}), greeting + words


def _question_lines(lines: list[BoardLine]) -> list[BoardLine]:
    """The open question's two pinned lines on a board: its label and its equation."""
    return [line for line in lines if line.id in PINNED_IDS]


def _write(line: BoardLine) -> BoardCommand:
    """The command that writes this board line again (a formula by its name)."""
    return BoardCommand(op="write", id=line.id, latex=None if line.formula else line.latex, formula=line.formula,
                        text=line.text, check=line.check)
