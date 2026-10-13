"""The turn's two LLM steps, UNDERSTAND and RESPOND: the LLM first, the scripted version when it fails.
A fallback is noted in ctx.issues, and after a failed UNDERSTAND the rest of the turn is scripted. Also the grading
of UNDERSTAND's answer, which checks a right answer against the student's own words."""
from __future__ import annotations

from dataclasses import replace

from app import board, curriculum, engine, fallback, grading, prompts, replies
from app.llm import BadOutputError, ProviderError
from app.models import (LLM_ACTIONS, Curriculum, Decision, Grade, Pending, Progress, Segment, TurnContext,
                        Understood)


def understand(llm, ctx: TurnContext) -> Understood:
    """What kind of reply this is, and the answer in it: UNDERSTAND, or scripted if the LLM fails."""
    pending = ctx.before[ctx.concept.id].pending
    if not ctx.scripted:
        try:
            return _llm_understand(llm, ctx, pending)
        except (ProviderError, BadOutputError):
            ctx.issues.append("understand_fallback")
            ctx.scripted = True                       # the rest of this turn is scripted too
    return fallback.understand(ctx.text, phase=ctx.phase, check=ctx.check,
                               awaiting_working=bool(pending and pending.awaiting_working))


def respond(llm, cur: Curriculum, ctx: TurnContext, decision: Decision, after: Progress, understood: Understood,
            grade: Grade | None) -> tuple[list[Segment], Decision, Progress]:
    """Words for the decision's action only; compose adds steps, questions and re-asks afterwards."""
    if decision.action in LLM_ACTIONS and not ctx.scripted:
        try:
            return _llm_respond(llm, cur, ctx, decision, understood, grade), decision, after
        except (ProviderError, BadOutputError):
            ctx.issues.append("respond_fallback")
    if decision.action == "reteach":                  # a scripted reteach replays the lesson's reteach step
        decision, after = engine.replay_instead(decision, ctx.before, after)
    if decision.action == "repeat" and ctx.last_speech and (ctx.phase == "teaching" or understood.type == "say_again"):
        # R8 while teaching, or "say again" while asked: last_speech again (see TurnContext), so a hint or explanation
        # just given is heard again too. While asked it ends on the question (or R5's "just the answer" request, with
        # the question on the board), so the question isn't asked a second time.
        return ctx.last_speech, replace(decision, ask_check_id=None), after
    words = replies.words(decision, cur=cur, progress=after, check=ctx.check, grade=grade,
                          first_name=ctx.student["first_name"])
    return words, decision, after


def grade(ctx: TurnContext, understood: Understood) -> tuple[Understood, Grade]:
    """SymPy grades UNDERSTAND's answer. UNDERSTAND can write down the right answer when the student said a wrong
    one, so a right answer is checked against the student's own words, read by the scripted rules: when UNDERSTAND
    named a mistake, or those words are a trap answer, and they aren't right, the student's words are graded."""
    grade = grading.grade(ctx.check, understood.answer)
    if grade.verdict != "correct" or ctx.scripted:
        return understood, grade
    pending = ctx.before[ctx.concept.id].pending
    own = fallback.understand(ctx.text, phase=ctx.phase, check=ctx.check,
                              awaiting_working=pending.awaiting_working).answer
    own_grade = grading.grade(ctx.check, own) if own else grade
    if own_grade.verdict != "correct" and (understood.mistake or own_grade.trap):
        ctx.issues.append("answer_rewritten")
        return understood.model_copy(update={"answer": own}), own_grade
    return understood, grade


def _llm_understand(llm, ctx: TurnContext, pending: Pending | None) -> Understood:
    """UNDERSTAND. A rejected output is recorded as an issue; the client asks once more."""
    messages = prompts.understand_messages(
        concept=ctx.concept, phase=ctx.phase, check=ctx.check,
        previous_answer=pending.last_answer if pending else "",
        tutor_last_line=ctx.last_speech[-1].say if ctx.last_speech else "", student_text=ctx.text)

    def validate(data: dict) -> Understood:
        understood, problems = prompts.read_understood(data, ctx.check)
        if problems:
            ctx.issues.append("understand_rejected")
            raise ValueError("; ".join(problems))
        return understood

    return llm.chat_json("understand", messages, "understood", prompts.UNDERSTAND_SCHEMA, validate,
                         deadline=ctx.deadline, calls=ctx.calls)


def _llm_respond(llm, cur: Curriculum, ctx: TurnContext, decision: Decision, understood: Understood,
                 grade: Grade | None) -> list[Segment]:
    """RESPOND. Its reply is checked (board.py) against the question pending before decide()."""
    concept, check = ctx.concept, ctx.check
    next_check = curriculum.get_check(concept, decision.ask_check_id) if decision.action == "reteach" else None
    key = grading.answer_key(check) if check else None
    next_key = grading.answer_key(next_check) if next_check else None
    equations = [question.equation for question in concept.checks]
    reply_ctx = board.ReplyContext(action=decision.action, board=ctx.board,
                                   anchor=check.equation if check else None, key=key, next_key=next_key,
                                   student_answer=understood.answer, check_equations=equations,
                                   formulas=cur.formulas)
    messages = prompts.respond_messages(
        decision=decision, concept=concept,
        step=None if check else curriculum.get_step(concept, ctx.before[concept.id].step_id),
        check=check, key=key, next_check=next_check, next_key=next_key, understood=understood, grade=grade,
        mistake=next((m for m in concept.mistakes if m.id == decision.mistake), None),
        hint_text=check.hints[decision.hint_level - 1] if decision.action == "hint" else None,
        board=ctx.board, check_equations=equations, formulas=cur.formulas,
        previous_reason=ctx.previous_reason, student_text=ctx.text)

    def validate(data: dict) -> list[Segment]:
        segments, problems = prompts.read_reply(data, reply_ctx)
        if problems:
            ctx.issues.extend(f"respond_rejected:{problem}" for problem in problems)
            raise ValueError("; ".join(problems))
        return segments

    words = llm.chat_json("respond", messages, "reply", prompts.RESPOND_SCHEMA, validate,
                          deadline=ctx.deadline, calls=ctx.calls)
    # The worked answer, then "we'll come back to this next time": not after the very last topic, where END's summary
    # right after it says so.
    if decision.action == "needs_review" and decision.resume_concept_id:
        words = words + [Segment(say=replies.REVIEW_LATER)]
    return words
