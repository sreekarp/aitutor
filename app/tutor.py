"""One student turn, end to end: fast paths, understand, grade, decide, words, compose, save."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine

from app import board, curriculum, db, engine, fallback, grading, llm_steps, safety
from app.config import MAX_INPUT_CHARS, MODELS, PROMPT_VERSIONS, SESSION_GAP_MIN, TURN_DEADLINE_S, Settings
from app.models import (BoardCommand, Curriculum, Decision, Grade, Progress, Segment, TurnContext, TurnResult,
                        Understood)

log = logging.getLogger(__name__)

DIDNT_CATCH = "Sorry, I didn't catch that. Say it again, or type it."
MISHEARD = "Sorry, I may have misheard you. Say it again, or type it."   # a spoken reply nobody could place
SORRY = "Sorry, let's try that again."
RESUMING = ("greet", "resume", "mastered", "needs_review")    # actions that go on to the current concept
LOG_FIELDS = ("session_id", "student_id", "versions", "phase", "understood", "grade", "latency_ms", "calls", "issues")


class NoSession(Exception):
    """The student hasn't pressed Start yet, so there is no session to add a turn to."""


class Tutor:
    """Runs turns for every student. It keeps no student state itself: everything lives in the database."""

    def __init__(self, cur: Curriculum, engine: Engine, settings: Settings, llm=None):
        self.cur = cur
        self.db_engine = engine            # the database (the module called `engine` holds the teaching rules)
        self.settings = settings
        self.llm = llm                     # an llm.Client, or None
        self.scripted = settings.scripted or llm is None

    def start(self, student_id: str) -> TurnResult:
        """Start / Continue: S1 within SESSION_GAP_MIN minutes of the session's last turn, else a new session + S0."""
        started = time.monotonic()
        with self.db_engine.begin() as conn:
            session = db.latest_session(conn, student_id)
            new_session = session is None or not _recent(db.last_turn(conn, session["id"]))
            if new_session:
                models = {"mode": "scripted"} if self.scripted else MODELS
                session = db.create_session(conn, student_id, curriculum_version=self.cur.version,
                                            prompt_versions=PROMPT_VERSIONS, models=models)
        try:
            ctx, replaced = self._context(student_id, session, "", started, [], {})
            decision, after = engine.start(ctx.before, self.cur, new_session=new_session, replaced=replaced)
            words = fallback.words(decision, cur=self.cur, progress=after, check=None, grade=None,
                                   first_name=ctx.student["first_name"])
            return self._finish(ctx, "start", None, None, decision, after, words)
        except Exception:
            log.exception("session start failed for student %s", student_id)
            return self._sorry(session, "")

    def turn(self, student_id: str, text: str, *, input_mode: str = "text", prev_first_audio_ms: int | None = None,
             stt_failed: bool = False, pre_calls: list | None = None, pre_latency: dict | None = None) -> TurnResult:
        """One student turn. Raises NoSession before the first Start; any other error gives a scripted "Sorry"."""
        pre_latency = pre_latency or {}
        started = time.monotonic() - pre_latency.get("stt", 0) / 1000    # a voice turn began with speech-to-text
        text = text.strip()[:MAX_INPUT_CHARS]
        session, over_limit = self.open_session(student_id)
        latency = {**pre_latency, "prev_first_audio_ms": prev_first_audio_ms}
        try:
            ctx, replaced = self._context(student_id, session, text, started, list(pre_calls or []), latency)
            return self._run(ctx, replaced, over_limit, input_mode, stt_failed)
        except Exception:
            log.exception("turn failed for student %s", student_id)
            return self._sorry(session, text)

    def open_session(self, student_id: str) -> tuple[dict, bool]:
        """The student's latest session, and whether today's turn limit is used up. NoSession before the first Start."""
        with self.db_engine.connect() as conn:
            session = db.latest_session(conn, student_id)
            over_limit = db.turns_today(conn) >= self.settings.max_turns_per_day
        if session is None:
            raise NoSession(student_id)
        return session, over_limit

    def _run(self, ctx: TurnContext, replaced: bool, over_limit: bool, input_mode: str,
             stt_failed: bool) -> TurnResult:
        """The order of checks: LIMIT, safety, curriculum change, fast paths, understanding."""
        understood = grade = None
        if over_limit:
            decision, after = engine.limit_decision(), ctx.before
        elif replaced and not safety.is_safety(ctx.text):
            decision, after = engine.lesson_changed(ctx.before, self.cur), ctx.before
        else:
            understood = _timed(ctx, "understand", self._read, ctx, stt_failed)
            if ctx.check and understood.answer and not understood.safety:    # grade answers while checking
                grade = _timed(ctx, "grade", grading.grade, ctx.check, understood.answer)
            decision, after = _timed(ctx, "decide", engine.decide, ctx.before, self.cur, understood, grade)
        misheard = _misheard(ctx, input_mode, understood, decision)
        if (stt_failed or misheard) and decision.action == "repeat":
            words = [Segment(say=DIDNT_CATCH if stt_failed else MISHEARD)]   # while checking, compose asks again
        else:
            words, decision, after = _timed(ctx, "respond", llm_steps.respond, self.llm, self.cur, ctx, decision,
                                            after, understood or Understood(type="unclear"), grade)
        hint = _input_hint(decision, understood, stt_failed or misheard)
        return self._finish(ctx, input_mode, understood, grade, decision, after, words, hint)

    def _context(self, student_id: str, session: dict, text: str, started: float, calls: list,
                 latency: dict) -> tuple[TurnContext, bool]:
        """Read what the turn needs; reconcile() moves the student if the lesson files changed."""
        with self.db_engine.connect() as conn:
            student = db.get_student(conn, student_id)
            saved = db.load_progress(conn, student_id, self.cur)
            board_now = db.session_board(conn, session["id"])
            last = db.last_turn(conn, session["id"])
        before, issues, replaced = engine.reconcile(saved, self.cur)
        concept_id = engine.current_concept(before, self.cur)
        concept = self.cur.concepts[concept_id] if concept_id else None
        pending = before[concept_id].pending if concept_id else None
        said = [Segment(say=seg["say"]) for seg in last["tutor_output"]["segments"] if seg["say"]] if last else []
        step = curriculum.get_step(concept, before[concept_id].step_id) if concept and not pending else None
        apology_only = [segment.say for segment in said] in ([DIDNT_CATCH], [MISHEARD])
        if step and last and (last["action"] in RESUMING or apology_only):
            # After a topic start or move-on, "say again" replays just the step: not the greeting, intro or praise.
            # After a lone apology ("didn't catch that", "may have misheard"), the step too, not the apology.
            # Either way UNDERSTAND's "tutor's last line" is the step's last line: the line being answered.
            said = [Segment(say=segment.say) for segment in step.segments]
        ctx = TurnContext(
            student=student, session=session, concept=concept, phase="checking" if pending else "teaching",
            before=before, check=curriculum.get_check(concept, pending.check_id) if pending else None,
            text=text, last_speech=said, previous_reason=last["reason"] if last else "",
            board=board_now, deadline=started + TURN_DEADLINE_S, calls=calls, issues=list(issues),
            latency=latency, scripted=self.scripted)
        return ctx, replaced

    def _read(self, ctx: TurnContext, stt_failed: bool) -> Understood:
        """The fast paths first (no LLM), then understanding."""
        if safety.is_safety(ctx.text):
            return Understood(type="unclear", safety=True)
        if stt_failed or not ctx.text or ctx.concept is None:       # nothing to read, or nothing left to teach
            return Understood(type="unclear")
        fast = fallback.keyword(ctx.text, ctx.phase)
        return Understood(type=fast) if fast else llm_steps.understand(self.llm, ctx)

    # ---------- compose and save ----------
    def _compose(self, ctx: TurnContext, decision: Decision, after: Progress) -> list[Segment]:
        """The scripted parts after the words: a step, a question, the next concept, a re-ask."""
        concept = self.cur.concepts.get(decision.concept_id or "")
        parts: list[Segment] = []
        if decision.play_step_id:
            parts += curriculum.get_step(concept, decision.play_step_id).segments
        if decision.ask_check_id:
            check = curriculum.get_check(concept, decision.ask_check_id)
            parts.append(curriculum.ask_segment(check, self.cur.formulas))
        if decision.resume_concept_id:
            parts += self._resume(decision.resume_concept_id, after, decision.action)
        elif decision.action in RESUMING:                 # moving on, but nothing is left: the END summary
            parts += fallback.words(engine.end_decision(), cur=self.cur, progress=after, check=None, grade=None,
                                    first_name=ctx.student["first_name"])
        pending = after[concept.id].pending if concept else None
        if decision.reask and pending:
            parts.append(fallback.reask(curriculum.get_check(concept, pending.check_id), pending.awaiting_working))
        if decision.clear_board and parts:                # wipe the board just before the new part plays
            parts[0] = Segment(say=parts[0].say, board=[BoardCommand(op="clear"), *parts[0].board])
        return parts

    def _resume(self, concept_id: str, after: Progress, action: str) -> list[Segment]:
        """Pick a concept up where it stands: its opening (fallback.opening), then its pending question,
        else its step (step 1 if none)."""
        concept, state = self.cur.concepts[concept_id], after[concept_id]
        step = curriculum.get_step(concept, state.step_id) or concept.steps[0]
        opening = fallback.opening(concept, action, from_top=not state.pending and step.id == concept.steps[0].id)
        if state.pending:
            check = curriculum.get_check(concept, state.pending.check_id)
            return opening + [curriculum.ask_segment(check, self.cur.formulas)]
        return opening + list(step.segments)

    def _finish(self, ctx: TurnContext, input_mode: str, understood: Understood | None, grade: Grade | None,
                decision: Decision, after: Progress, words: list[Segment],
                input_hint: str | None = None) -> TurnResult:
        """Compose, update the board, then save progress, turn, decision and board in one transaction."""
        segments = words + self._compose(ctx, decision, after)
        new_board = board.apply(ctx.board, segments, self.cur.formulas)
        if decision.rule == "R10":
            ctx.issues.append("safety_review")
        started = ctx.deadline - TURN_DEADLINE_S       # turn() set the deadline TURN_DEADLINE_S after it began
        ctx.latency["total"] = round((time.monotonic() - started) * 1000)
        row = {"session_id": ctx.session["id"], "student_id": ctx.student["id"],
               "concept_id": ctx.concept.id if ctx.concept else None, "phase": ctx.phase, "input": input_mode,
               "student_text": ctx.text, "understood": understood.model_dump() if understood else None,
               "grade": asdict(grade) if grade else None,
               "tutor_output": {"segments": [segment.model_dump() for segment in segments]},
               "versions": {"curriculum": self.cur.version, **PROMPT_VERSIONS},
               "latency_ms": ctx.latency, "calls": ctx.calls, "issues": ctx.issues}
        with self.db_engine.begin() as conn:
            if decision.rule != "LIMIT":                  # LIMIT: nothing else changes
                db.save_progress(conn, ctx.student["id"], after, self.cur.version)
            turn_id = db.save_turn(conn, row, decision)
            db.set_session_board(conn, ctx.session["id"], new_board)
        _log_turn(ctx, turn_id, row, decision)
        return TurnResult(turn_id=turn_id, session_id=ctx.session["id"], transcript=ctx.text,
                          understood=understood, grade=grade, decision=decision, segments=segments,
                          board=new_board, issues=ctx.issues, latency_ms=ctx.latency, calls=ctx.calls,
                          input_hint=input_hint)

    def _sorry(self, session: dict, text: str) -> TurnResult:
        """After an unexpected error: a scripted apology, nothing saved, progress unchanged (the log has the error)."""
        with self.db_engine.connect() as conn:
            board_now = db.session_board(conn, session["id"])
        decision = Decision("R8", "repeat", None, "Something went wrong on our side, so nothing changed.")
        return TurnResult(turn_id="", session_id=session["id"], transcript=text, understood=None, grade=None,
                          decision=decision, segments=[Segment(say=SORRY)], board=board_now,
                          issues=["internal_error"], latency_ms={}, calls=[])


def _timed(ctx: TurnContext, stage: str, function, *args):
    """Call function(*args) and record how long it took, in ms, as ctx.latency[stage]."""
    started = time.monotonic()
    result = function(*args)
    ctx.latency[stage] = round((time.monotonic() - started) * 1000)
    return result


def _recent(turn: dict | None) -> bool:
    """True when this turn happened less than SESSION_GAP_MIN minutes ago."""
    return turn is not None and datetime.now(timezone.utc) - turn["created_at"] < timedelta(minutes=SESSION_GAP_MIN)


def _misheard(ctx: TurnContext, input_mode: str, understood: Understood | None, decision: Decision) -> bool:
    """R8 while checking after a spoken reply UNDERSTAND couldn't place ("unclear" or "off_topic"): the mic may
    have misheard it, so the tutor says so before asking again. (While teaching, R8 says the last step or reply
    again instead, as before.)"""
    return (input_mode == "voice" and ctx.phase == "checking" and decision.action == "repeat"
            and understood is not None and understood.type in ("unclear", "off_topic"))


def _input_hint(decision: Decision, understood: Understood | None, not_heard: bool) -> str | None:
    """"type" when the reply suggests typing: R5 clarify, R8 after an unclear reply, or speech that wasn't heard
    (a failed recording, or a spoken reply that may have been misheard)."""
    unclear = understood is not None and understood.type == "unclear" and decision.action == "repeat"
    return "type" if not_heard or decision.action == "clarify" or unclear else None


def _log_turn(ctx: TurnContext, turn_id: str, row: dict, decision: Decision) -> None:
    """One JSON log line per turn, and a warning when a safety review is needed."""
    line = {"turn_id": turn_id, "rule": decision.rule, "reason": decision.reason,
            "session_versions": {"curriculum": ctx.session["curriculum_version"], **ctx.session["prompt_versions"]},
            **{key: row[key] for key in LOG_FIELDS}}
    log.info(json.dumps(line))
    if "safety_review" in row["issues"]:
        log.warning("safety review needed for turn %s", turn_id)
