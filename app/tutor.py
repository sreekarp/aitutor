"""One student turn, end to end (spec §2.2): fast paths, understand, grade, decide, words, compose, save."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine

from app import board, curriculum, db, engine, fallback, grading, prompts
from app.config import MAX_INPUT_CHARS, MODELS, PROMPT_VERSIONS, SESSION_GAP_MIN, TURN_DEADLINE_S, Settings
from app.llm import BadOutputError, ProviderError
from app.models import (LLM_ACTIONS, BoardCommand, BoardLine, Check, Concept, ConceptState, Curriculum, Decision,
                        Grade, Pending, Progress, Segment, TurnResult, Understood)

log = logging.getLogger(__name__)

DIDNT_CATCH = "Sorry, I didn't catch that. Say it again, or type it."
SORRY = "Sorry, let's try that again."
RESUMING = ("greet", "resume", "mastered", "needs_review")    # actions that go on to the current concept
LOG_FIELDS = ("session_id", "student_id", "versions", "phase", "understood", "grade", "latency_ms", "calls", "issues")


class NoSession(Exception):
    """The student hasn't pressed Start yet, so there is no session to add a turn to."""


@dataclass
class TurnContext:
    """Everything one turn needs; built by turn() before understanding."""
    student: dict
    session: dict
    concept: Concept | None            # current concept (None at END)
    phase: str                         # "teaching" | "checking"
    before: Progress                   # progress before decide()
    check: Check | None                # the pending question before decide() (None while teaching)
    text: str                          # the student's words, cut to MAX_INPUT_CHARS
    tutor_last_line: str               # last `say` of the previous turn ("" if none)
    previous_reason: str               # reason of the previous turn's decision ("" if none)
    board: list[BoardLine]             # the session board before this turn
    deadline: float                    # time.monotonic() + TURN_DEADLINE_S, set at turn start
    calls: list[dict]                  # model calls made so far this turn
    issues: list[str]
    latency: dict
    scripted: bool = False             # no LLM for the rest of this turn (scripted mode, or UNDERSTAND failed)


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
            ctx, _ = self._context(student_id, session, "", started, [], {})
            decision, after = engine.start(ctx.before, self.cur, new_session=new_session)
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
        with self.db_engine.connect() as conn:
            session = db.latest_session(conn, student_id)
            over_limit = db.turns_today(conn) >= self.settings.max_turns_per_day
        if session is None:
            raise NoSession(student_id)
        latency = {**pre_latency, "prev_first_audio_ms": prev_first_audio_ms}
        try:
            ctx, replaced = self._context(student_id, session, text, started, list(pre_calls or []), latency)
            return self._run(ctx, replaced, over_limit, input_mode, stt_failed)
        except Exception:
            log.exception("turn failed for student %s", student_id)
            return self._sorry(session, text)

    def _run(self, ctx: TurnContext, replaced: bool, over_limit: bool, input_mode: str,
             stt_failed: bool) -> TurnResult:
        """The order of checks (spec §4.4): LIMIT, safety, curriculum change, fast paths, understanding."""
        understood = grade = None
        if over_limit:
            decision, after = engine.limit_decision(), ctx.before
        elif replaced and not fallback.is_safety(ctx.text):
            decision, after = engine.lesson_changed(ctx.before, self.cur), ctx.before
        else:
            understood = _timed(ctx, "understand", self._read, ctx, stt_failed)
            if ctx.check and understood.answer and not understood.safety:    # grade answers while checking
                grade = _timed(ctx, "grade", grading.grade, ctx.check, understood.answer)
            decision, after = _timed(ctx, "decide", engine.decide, ctx.before, self.cur, understood, grade)
        if stt_failed and decision.action == "repeat":
            words = [Segment(say=DIDNT_CATCH)]               # while checking, compose asks the question again
        else:
            words, decision, after = _timed(ctx, "respond", self._words, ctx, decision, after,
                                            understood or Understood(type="unclear"), grade)
        hint = _input_hint(decision, understood, stt_failed)
        return self._finish(ctx, input_mode, understood, grade, decision, after, words, hint)

    def _context(self, student_id: str, session: dict, text: str, started: float, calls: list,
                 latency: dict) -> tuple[TurnContext, bool]:
        """Read what the turn needs; reconcile() moves the student if the lesson files changed (spec §4.4)."""
        with self.db_engine.connect() as conn:
            student = db.get_student(conn, student_id)
            saved = db.load_progress(conn, student_id, self.cur)
            board_now = db.session_board(conn, session["id"])
            last = db.last_turn(conn, session["id"])
        before, issues, replaced = engine.reconcile(saved, self.cur)
        concept_id = engine.current_concept(before, self.cur)
        concept = self.cur.concepts[concept_id] if concept_id else None
        pending = before[concept_id].pending if concept_id else None
        said = [segment["say"] for segment in last["tutor_output"]["segments"] if segment["say"]] if last else []
        ctx = TurnContext(
            student=student, session=session, concept=concept, phase="checking" if pending else "teaching",
            before=before, check=curriculum.get_check(concept, pending.check_id) if pending else None,
            text=text, tutor_last_line=said[-1] if said else "", previous_reason=last["reason"] if last else "",
            board=board_now, deadline=started + TURN_DEADLINE_S, calls=calls, issues=list(issues),
            latency=latency, scripted=self.scripted)
        return ctx, replaced

    def _read(self, ctx: TurnContext, stt_failed: bool) -> Understood:
        """The fast paths first (spec §5.1, no LLM), then understanding."""
        if fallback.is_safety(ctx.text):
            return Understood(type="unclear", safety=True)
        if stt_failed or not ctx.text or ctx.concept is None:       # nothing to read, or nothing left to teach
            return Understood(type="unclear")
        fast = fallback.keyword(ctx.text, ctx.phase)
        return Understood(type=fast) if fast else self._understand(ctx)

    # ---------- the two seams: the LLM first, the scripted version when it fails (spec §7.2, §7.3) ----------
    def _understand(self, ctx: TurnContext) -> Understood:
        """What kind of reply this is, and the answer in it: UNDERSTAND, or scripted if the LLM fails."""
        pending = ctx.before[ctx.concept.id].pending
        if not ctx.scripted:
            try:
                return self._llm_understand(ctx, pending)
            except (ProviderError, BadOutputError):
                ctx.issues.append("understand_fallback")
                ctx.scripted = True                       # the rest of this turn is scripted too
        return fallback.understand(ctx.text, phase=ctx.phase, check=ctx.check,
                                   awaiting_working=bool(pending and pending.awaiting_working))

    def _words(self, ctx: TurnContext, decision: Decision, after: Progress, understood: Understood,
               grade: Grade | None) -> tuple[list[Segment], Decision, Progress]:
        """Words for the decision's action only; compose adds steps, questions and re-asks afterwards."""
        if decision.action in LLM_ACTIONS and not ctx.scripted:
            try:
                return self._llm_words(ctx, decision, understood, grade), decision, after
            except (ProviderError, BadOutputError):
                ctx.issues.append("respond_fallback")
        if decision.action == "reteach":                  # a scripted reteach replays the lesson's reteach step
            decision, after = engine.replay_instead(decision, ctx.before, after)
        if decision.action == "repeat" and ctx.phase == "teaching":
            speech = self._last_speech(ctx.session["id"])
            if speech:
                return speech, decision, after
        words = fallback.words(decision, cur=self.cur, progress=after, check=ctx.check, grade=grade,
                               first_name=ctx.student["first_name"])
        return words, decision, after

    def _llm_understand(self, ctx: TurnContext, pending: Pending | None) -> Understood:
        """UNDERSTAND (spec §5.2). A rejected output is recorded as an issue; the client asks once more."""
        messages = prompts.understand_messages(
            concept=ctx.concept, phase=ctx.phase, check=ctx.check,
            previous_answer=pending.last_answer if pending else "", tutor_last_line=ctx.tutor_last_line,
            student_text=ctx.text)

        def validate(data: dict) -> Understood:
            understood, problems = prompts.read_understood(data, ctx.check)
            if problems:
                ctx.issues.append("understand_rejected")
                raise ValueError("; ".join(problems))
            return understood

        return self.llm.chat_json("understand", messages, "understood", prompts.UNDERSTAND_SCHEMA, validate,
                                  deadline=ctx.deadline, calls=ctx.calls)

    def _llm_words(self, ctx: TurnContext, decision: Decision, understood: Understood,
                   grade: Grade | None) -> list[Segment]:
        """RESPOND (spec §6.1). The reply is checked (§6.3) against the question pending before decide()."""
        concept, check = ctx.concept, ctx.check
        next_check = curriculum.get_check(concept, decision.ask_check_id) if decision.action == "reteach" else None
        key = grading.answer_key(check) if check else None
        next_key = grading.answer_key(next_check) if next_check else None
        equations = [question.equation for question in concept.checks]
        reply_ctx = board.ReplyContext(action=decision.action, board=ctx.board,
                                       anchor=check.equation if check else None, key=key, next_key=next_key,
                                       student_answer=understood.answer, check_equations=equations,
                                       formulas=self.cur.formulas)
        messages = prompts.respond_messages(
            decision=decision, concept=concept,
            step=None if check else curriculum.get_step(concept, ctx.before[concept.id].step_id),
            check=check, key=key, next_check=next_check, next_key=next_key, understood=understood, grade=grade,
            mistake=next((m for m in concept.mistakes if m.id == decision.mistake), None),
            hint_text=check.hints[decision.hint_level - 1] if decision.action == "hint" else None,
            board=ctx.board, check_equations=equations, formulas=self.cur.formulas,
            previous_reason=ctx.previous_reason, student_text=ctx.text)

        def validate(data: dict) -> list[Segment]:
            segments, problems = prompts.read_reply(data, reply_ctx)
            if problems:
                ctx.issues.extend(f"respond_rejected:{problem}" for problem in problems)
                raise ValueError("; ".join(problems))
            return segments

        words = self.llm.chat_json("respond", messages, "reply", prompts.RESPOND_SCHEMA, validate,
                                   deadline=ctx.deadline, calls=ctx.calls)
        if decision.action == "needs_review":            # spec §4.7: the worked answer, then this scripted line
            words = words + [Segment(say=fallback.REVIEW_LATER)]
        return words

    def _last_speech(self, session_id: str) -> list[Segment]:
        """R8 while teaching: the previous reply's speech again, without its board commands."""
        with self.db_engine.connect() as conn:
            last = db.last_turn(conn, session_id)
        segments = last["tutor_output"]["segments"] if last else []
        return [Segment(say=segment["say"]) for segment in segments if segment["say"]]

    # ---------- compose and save ----------
    def _compose(self, ctx: TurnContext, decision: Decision, after: Progress) -> list[Segment]:
        """The scripted parts after the words (spec §4.7): a step, a question, the next concept, a re-ask."""
        concept = self.cur.concepts.get(decision.concept_id or "")
        parts: list[Segment] = []
        if decision.play_step_id:
            parts += curriculum.get_step(concept, decision.play_step_id).segments
        if decision.ask_check_id:
            check = curriculum.get_check(concept, decision.ask_check_id)
            parts.append(curriculum.ask_segment(check, self.cur.formulas))
        if decision.resume_concept_id:
            parts += self._resume(decision.resume_concept_id, after)
        elif decision.action in RESUMING:                 # moving on, but nothing is left: the END summary
            parts += fallback.words(engine.end_decision(), cur=self.cur, progress=after, check=None, grade=None,
                                    first_name=ctx.student["first_name"])
        pending = after[concept.id].pending if concept else None
        if decision.reask and pending:
            parts.append(fallback.reask(curriculum.get_check(concept, pending.check_id), pending.awaiting_working))
        if decision.clear_board and parts:                # wipe the board just before the new part plays
            parts[0] = Segment(say=parts[0].say, board=[BoardCommand(op="clear"), *parts[0].board])
        return parts

    def _resume(self, concept_id: str, after: Progress) -> list[Segment]:
        """Pick a concept up where it stands (spec §4.5): its pending question, else its step (step 1 if none)."""
        concept, state = self.cur.concepts[concept_id], after[concept_id]
        if state.pending:
            check = curriculum.get_check(concept, state.pending.check_id)
            return [curriculum.ask_segment(check, self.cur.formulas)]
        return list((curriculum.get_step(concept, state.step_id) or concept.steps[0]).segments)

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

    # ---------- what the page gets ----------
    def state(self, student_id: str) -> dict:
        """GET /api/state: progress per concept, the last 5 decisions (rule, action, reason) and the board."""
        with self.db_engine.connect() as conn:
            progress = db.load_progress(conn, student_id, self.cur)
            session = db.latest_session(conn, student_id)
            lines = db.session_board(conn, session["id"]) if session else []
            recent = db.recent_decisions(conn, student_id)
        current = engine.current_concept(progress, self.cur)
        rows = [_progress_row(self.cur.concepts[cid], progress[cid], cid == current) for cid in self.cur.order]
        why = [{"rule": d["rule"], "action": d["action"], "reason": d["reason"]} for d in recent]
        shown = [{"id": line.id, "latex": line.latex, "formula": line.formula, "text": line.text} for line in lines]
        return {"progress": rows, "why": why, "board": shown}

    def payload(self, student_id: str, result: TurnResult) -> dict:
        """The turn JSON the browser gets: never the decision's evidence, nor any SymPy check text."""
        decision = result.decision
        segments = [{"say": segment.say, "board": [_command_json(cmd, self.cur.formulas) for cmd in segment.board]}
                    for segment in result.segments]
        return {"turn_id": result.turn_id, "transcript": result.transcript, "segments": segments,
                "decision": {"rule": decision.rule, "action": decision.action, "reason": decision.reason},
                **self.state(student_id), "input_hint": result.input_hint}


def _timed(ctx: TurnContext, stage: str, function, *args):
    """Call function(*args) and record how long it took, in ms, as ctx.latency[stage]."""
    started = time.monotonic()
    result = function(*args)
    ctx.latency[stage] = round((time.monotonic() - started) * 1000)
    return result


def _recent(turn: dict | None) -> bool:
    """True when this turn happened less than SESSION_GAP_MIN minutes ago."""
    return turn is not None and datetime.now(timezone.utc) - turn["created_at"] < timedelta(minutes=SESSION_GAP_MIN)


def _input_hint(decision: Decision, understood: Understood | None, stt_failed: bool) -> str | None:
    """"type" when the reply suggests typing: R5 clarify, R8 after an unclear reply, or a failed recording."""
    unclear = understood is not None and understood.type == "unclear" and decision.action == "repeat"
    return "type" if stt_failed or decision.action == "clarify" or unclear else None


def _progress_row(concept: Concept, state: ConceptState, current: bool) -> dict:
    """One concept for the progress panel; step_index counts from 0 and is only set while teaching."""
    step_ids = [step.id for step in concept.steps]
    teaching = state.status == "teaching" and state.step_id in step_ids
    return {"concept_id": concept.id, "title": concept.title, "status": state.status,
            "step_index": step_ids.index(state.step_id) if teaching else None, "step_count": len(step_ids),
            "points": state.points, "hint_level": state.pending.hint_level if state.pending else 0,
            "reteach_count": state.reteach_count, "current": current}


def _command_json(cmd: BoardCommand, formulas: dict[str, str]) -> dict:
    """A board command for the browser: formula writes carry their LaTeX; the SymPy check stays on the server."""
    return {"op": cmd.op, "id": cmd.id, "latex": formulas.get(cmd.formula) if cmd.formula else cmd.latex,
            "formula": cmd.formula, "text": cmd.text, "target": cmd.target, "part": cmd.part}


def _log_turn(ctx: TurnContext, turn_id: str, row: dict, decision: Decision) -> None:
    """One JSON log line per turn (spec §11), and a warning when a safety review is needed."""
    line = {"turn_id": turn_id, "rule": decision.rule, "reason": decision.reason,
            "session_versions": {"curriculum": ctx.session["curriculum_version"], **ctx.session["prompt_versions"]},
            **{key: row[key] for key in LOG_FIELDS}}
    log.info(json.dumps(line))
    if "safety_review" in row["issues"]:
        log.warning("safety review needed for turn %s", turn_id)
