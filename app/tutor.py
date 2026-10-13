"""One student turn, end to end: fast paths, understand, grade, decide, words, compose, save."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine

from app import board, compose, curriculum, db, engine, fallback, llm_steps, replies, safety, sessions, stops
from app.config import MAX_INPUT_CHARS, MODELS, PROMPT_VERSIONS, SESSION_GAP_MIN, TURN_DEADLINE_S, Settings
from app.models import Curriculum, Decision, Grade, Progress, Segment, TurnContext, TurnResult, Understood
from app.replies import APOLOGIES, DIDNT_CATCH, DIDNT_FOLLOW, MISHEARD, SAFETY_REPLY, SORRY

log = logging.getLogger(__name__)

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
        """Start / Continue: S1 within SESSION_GAP_MIN minutes of the session's last turn, else a new session + S0.
        A reply the student stopped is picked up where stops.can_pick_up allows."""
        started = time.monotonic()
        session, new_session, last = self._session(student_id)
        try:
            ctx, replaced = self._context(student_id, session, "", started, [], {})
            decision, after = sessions.start(ctx.before, self.cur, new_session=new_session, replaced=replaced)
            words = self._words(ctx, decision, after)
            ctx.stopped = stops.find(last)              # in a new session, the stop is in the one before
            if ctx.stopped and stops.can_pick_up(last, replaced, ctx.before, after):
                decision, words = stops.pick_up(decision, words, ctx.stopped, new_session)
            return self._finish(ctx, "start", None, None, decision, after, words)
        except Exception:
            log.exception("session start failed for student %s", student_id)
            return self._sorry(session, "")

    def jump(self, student_id: str, concept_id: str) -> TurnResult:
        """S2: the student picked a topic, before or after Start (a session is opened if there isn't one)."""
        started = time.monotonic()
        session, _, _ = self._session(student_id)
        try:
            ctx, _ = self._context(student_id, session, "", started, [], {})
            decision, after = sessions.jump(ctx.before, self.cur, concept_id)
            return self._finish(ctx, "jump", None, None, decision, after, self._words(ctx, decision, after))
        except Exception:
            log.exception("topic jump failed for student %s", student_id)
            return self._sorry(session, "")

    def _session(self, student_id: str) -> tuple[dict, bool, dict | None]:
        """The student's session, or a new one (True) when there is none or its last turn is too old; and the last
        turn they had (None before their first)."""
        with self.db_engine.begin() as conn:
            session = db.latest_session(conn, student_id)
            last = db.last_turn(conn, session["id"]) if session else None
            new_session = not _recent(last)
            if new_session:
                models = {"mode": "scripted"} if self.scripted else MODELS
                session = db.create_session(conn, student_id, curriculum_version=self.cur.version,
                                            prompt_versions=PROMPT_VERSIONS, models=models)
        return session, new_session, last

    def _words(self, ctx: TurnContext, decision: Decision, after: Progress) -> list[Segment]:
        return replies.words(decision, cur=self.cur, progress=after, check=None, grade=None,
                             first_name=ctx.student["first_name"])

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
        """The order of checks: LIMIT, safety, curriculum change, a replay after a stop ("repeat", "continue", or any
        reply when the question wasn't shown yet), a topic asked for, fast paths, understanding."""
        understood = grade = None
        picked = _topic_asked_for(ctx, self.cur)        # another topic asked for by name, if any
        if over_limit:
            decision, after = sessions.limit_decision(), ctx.before
        elif replaced and not safety.is_safety(ctx.text):
            decision, after = sessions.lesson_changed(ctx.before, self.cur), ctx.before
        elif which := stops.which_replay(ctx, self.cur.formulas, topic_asked=picked is not None):
            decision, words = stops.replay(ctx, which)
            return self._finish(ctx, input_mode, None, None, decision, ctx.before, words)
        elif picked:                                    # S2, just as if the student clicked that topic
            decision, after = sessions.jump(ctx.before, self.cur, picked)
        else:
            unreadable = stt_failed or (input_mode == "voice" and fallback.foreign_letters(ctx.text))   # misheard
            understood = _timed(ctx, "understand", self._read, ctx, unreadable)
            if ctx.check and understood.answer and not understood.safety:    # grade answers while checking
                understood, grade = _timed(ctx, "grade", llm_steps.grade, ctx, understood)
            decision, after = _timed(ctx, "decide", engine.decide, ctx.before, self.cur, understood, grade)
        if apology := _apology(ctx, input_mode, understood, decision, stt_failed):
            # Then, while teaching, just the check-in (a lone "didn't catch that" asks for the reply itself), so
            # compose adds no second one (_replays_a_cue); while checking, compose asks the question again.
            again = ctx.last_speech[-1:] if ctx.phase == "teaching" and apology != DIDNT_CATCH else []
            words = [Segment(say=apology), *again]
        else:
            words, decision, after = _timed(ctx, "respond", llm_steps.respond, self.llm, self.cur, ctx, decision,
                                            after, understood or Understood(type="unclear"), grade)
        hint = _input_hint(decision, understood, apology in (DIDNT_CATCH, MISHEARD))
        return self._finish(ctx, input_mode, understood, grade, decision, after, words, hint)

    def _context(self, student_id: str, session: dict, text: str, started: float, calls: list,
                 latency: dict) -> tuple[TurnContext, bool]:
        """Read what the turn needs; reconcile() moves the student if the lesson files changed."""
        with self.db_engine.connect() as conn:
            student = db.get_student(conn, student_id)
            saved = db.load_progress(conn, student_id, self.cur)
            board_now = db.session_board(conn, session["id"])
            last = db.last_turn(conn, session["id"])
        before, issues, replaced = sessions.reconcile(saved, self.cur)
        concept_id = engine.current_concept(before, self.cur)
        concept = self.cur.concepts[concept_id] if concept_id else None
        pending = before[concept_id].pending if concept_id else None
        said = [Segment(say=seg["say"]) for seg in last["tutor_output"]["segments"] if seg["say"]] if last else []
        if last and last["rule"] in ("S0", "S1") and last["action"] == "repeat":
            said = said[1:]                             # a stopped reply picked up: not its "Welcome back"
        apologised = bool(said) and said[0].say in APOLOGIES
        check = curriculum.get_check(concept, pending.check_id) if pending else None
        if concept and not pending and last and (last["action"] in compose.RESUMING or apologised):
            # After a topic start or move-on, "say again" replays just the step and its cue (at the topic start: the
            # intro and the offer): not the greeting or praise. After a "sorry" (alone, or with just the check-in),
            # the whole step, not the apology. Either way UNDERSTAND's "tutor's last line" is the cue: the line
            # being answered.
            step = curriculum.get_step(concept, before[concept_id].step_id)
            # the check-in said after a "sorry" (an example's, say), else the one for where the lesson is
            cue = said[-1] if apologised and len(said) > 1 else compose.cue_for(self.cur, before, last["action"])[0]
            lesson = step.segments if step else [Segment(say=concept.intro)]
            said = [Segment(say=segment.say) for segment in lesson] + [cue]
        elif pending and last and (last["action"] in (*compose.RESUMING, "lesson_changed")
                                   or apologised):
            # With a question open, after a greeting, a topic start, a lesson change or an apology, "say again" asks
            # just the question.
            said = [Segment(say=curriculum.ask_segment(check, self.cur.formulas).say)]
        ctx = TurnContext(
            student=student, session=session, concept=concept, phase="checking" if pending else "teaching",
            before=before, check=check,
            text=text, last_speech=said, previous_reason=last["reason"] if last else "",
            board=board_now, deadline=started + TURN_DEADLINE_S, calls=calls, issues=list(issues),
            latency=latency, scripted=self.scripted, stopped=stops.find(last))
        return ctx, replaced

    def _read(self, ctx: TurnContext, unreadable: bool) -> Understood:
        """The fast paths first (no LLM), then understanding."""
        if safety.is_safety(ctx.text):
            return Understood(type="unclear", safety=True)
        if unreadable or not ctx.text or ctx.concept is None:       # nothing to read, or nothing left to teach
            return Understood(type="unclear")
        fast = fallback.keyword(ctx.text, ctx.phase)
        return Understood(type=fast) if fast else llm_steps.understand(self.llm, ctx)

    # ---------- compose and save ----------
    def _finish(self, ctx: TurnContext, input_mode: str, understood: Understood | None, grade: Grade | None,
                decision: Decision, after: Progress, words: list[Segment],
                input_hint: str | None = None) -> TurnResult:
        """Compose, update the board, then save progress, turn, decision and board in one transaction."""
        if stops.is_replay(decision):                 # a stopped reply again: it has its own cue and buttons
            segments, choices = words, ctx.stopped.choices
        else:
            lone_apology = [segment.say for segment in words] == [DIDNT_CATCH]  # asks for the reply: no cue after it
            segments = words + compose.compose(self.cur, ctx, decision, after, with_cue=not lone_apology)
            shown = compose.shown_action(ctx, decision, lone_apology)     # a replay keeps its cue's buttons
            choices = [] if decision.rule in compose.NO_CUE else compose.cue_for(self.cur, after, shown)[1]
        new_board = board.apply(ctx.board, segments, self.cur.formulas)
        if decision.rule == "R10":
            ctx.issues.append("safety_review")
        started = ctx.deadline - TURN_DEADLINE_S       # turn() set the deadline TURN_DEADLINE_S after it began
        ctx.latency["total"] = round((time.monotonic() - started) * 1000)
        row = {"session_id": ctx.session["id"], "student_id": ctx.student["id"],
               "concept_id": ctx.concept.id if ctx.concept else None, "phase": ctx.phase, "input": input_mode,
               "student_text": ctx.text, "understood": understood.model_dump() if understood else None,
               "grade": asdict(grade) if grade else None,
               "tutor_output": {"segments": [segment.model_dump() for segment in segments],     # for a stop later:
                                "board_before": [asdict(line) for line in ctx.board],         # the board it began
                                "choices": [asdict(choice) for choice in choices]},           # on, and its buttons
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
                          input_hint=input_hint, choices=choices)

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


def _topic_asked_for(ctx: TurnContext, cur: Curriculum) -> str | None:
    """Another topic the student asked for by name ("Can we skip to the factorization topic?"), read without the
    LLM; else None. Never for a reply the safety check flags: the safety reply comes first."""
    if safety.is_safety(ctx.text):
        return None
    picked = fallback.topic_request(ctx.text, {cid: cur.concepts[cid].aliases for cid in cur.order})
    return picked if picked != (ctx.concept.id if ctx.concept else None) else None


def _apology(ctx: TurnContext, input_mode: str, understood: Understood | None, decision: Decision,
             stt_failed: bool) -> str | None:
    """The "sorry" before R8 asks again, or None: DIDNT_CATCH when no transcript came, MISHEARD for a spoken reply
    UNDERSTAND couldn't place ("unclear" or "off_topic": the mic may have turned it into other words), and while
    teaching DIDNT_FOLLOW for such a typed one. (While checking, a typed one hears "Let me ask that again.") None
    after the safety reply while teaching: that is said again as it was, never as if the reply were misheard."""
    if decision.action != "repeat" or not (stt_failed or understood and understood.type in ("unclear", "off_topic")):
        return None
    if not stt_failed and ctx.phase == "teaching" and ctx.last_speech and ctx.last_speech[-1].say == SAFETY_REPLY:
        return None
    if stt_failed or input_mode == "voice":
        return DIDNT_CATCH if stt_failed else MISHEARD
    return DIDNT_FOLLOW if ctx.phase == "teaching" else None


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
