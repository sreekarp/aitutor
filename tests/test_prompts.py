"""app/prompts.py: the prompt files, the strict JSON schemas, the per-turn messages and the output readers."""
import json
from typing import get_args

import pytest
from sympy import Poly, expand

from app import board, curriculum, grading, mathcheck, prompts
from app.config import PROMPT_VERSIONS
from app.models import BoardCommand, BoardLine, Decision, Kind, Reply, ReplyType, Segment, Understood

NO_REVEAL = "- Never give away the answer to the PENDING QUESTION"


def _objects(schema):
    """Every object schema inside `schema`, however deeply nested."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _objects(value)


def _line(text: str, start: str) -> str:
    """The value of the line in `text` that starts with `start`."""
    return next(line for line in text.splitlines() if line.startswith(start))[len(start):]


def _no_reveal_rule(lines: list[str]) -> tuple[int, int]:
    """Where the no-reveal rule is in a respond prompt's lines: its first line, and the line after its last."""
    start = next(i for i, line in enumerate(lines) if line.startswith(NO_REVEAL))
    end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith("  "))
    return start, end


def test_prompt_files_exist_for_the_configured_versions():
    for name in PROMPT_VERSIONS.values():
        assert len(prompts.load_prompt(name)) > 500


def test_understand_prompt_contains_context(cur):
    concept = cur.concepts["factorisation"]
    check = curriculum.get_check(concept, "fa6")
    messages = prompts.understand_messages(concept=concept, phase="checking", check=check, previous_answer="3",
                                           tutor_last_line="So, what are the roots?", student_text="0 too")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == prompts.load_prompt("understand.v1")
    facts = messages[1]["content"]
    for expected in (f"CONCEPT: {concept.title}", "PHASE: checking", f"PENDING QUESTION: {check.say}",
                     "QUESTION KIND: roots", "ANSWER FORMAT: the roots in SymPy syntax",
                     f"WORKING FOR THIS KIND: {prompts.working_hint('roots')}",
                     "STUDENT'S PREVIOUS ANSWER TO THIS QUESTION: 3",
                     "TUTOR'S LAST LINE: So, what are the roots?"):
        assert expected in facts
    for mistake in concept.mistakes:
        assert f"- {mistake.id}: {mistake.looks_like}" in facts
    assert facts.splitlines()[-1] == "STUDENT SAID: 0 too"            # the student's words always come last
    # a word problem asks for every value offered for the asked quantity
    word_problem = curriculum.get_check(cur.concepts["word_problems"], "wp4")
    facts = prompts.understand_messages(concept=cur.concepts["word_problems"], phase="checking",
                                        check=word_problem, previous_answer="", tutor_last_line="",
                                        student_text="7 or minus 4")[1]["content"]
    assert "ignore values of other quantities" in facts and "PREVIOUS ANSWER TO THIS QUESTION: none" in facts
    # while teaching there is no pending question
    facts = prompts.understand_messages(concept=concept, phase="teaching", check=None, previous_answer="",
                                        tutor_last_line="", student_text="why?")[1]["content"]
    assert "PENDING QUESTION: none (teaching)" in facts


def test_respond_prompt_gives_answer_keys_and_lists_check_equations(cur):
    concept = cur.concepts["factorisation"]
    check, next_check = concept.checks[0], concept.checks[1]
    mistake = concept.mistakes[0]
    decision = Decision(rule="R3", action="reteach", concept_id=concept.id, strategy="worked_example",
                        reason="Matches known mistake sign_flip. Reteach 1 of 3 using worked_example.",
                        mistake=mistake.id, ask_check_id=next_check.id)
    lines = [BoardLine(id="q-label", text="Solve:"), BoardLine(id="q-eq", latex="x^{2}-5x+6=0")]
    messages = prompts.respond_messages(
        decision=decision, concept=concept, step=None, check=check, key=grading.answer_key(check),
        next_check=next_check, next_key=grading.answer_key(next_check),
        understood=Understood(type="answer", answer="-2, -3"), grade=grading.grade(check, "-2, -3"),
        mistake=mistake, hint_text=None, board=lines, check_equations=[c.equation for c in concept.checks],
        formulas=cur.formulas, previous_reason="A new session.", student_text="minus 2 and minus 3")
    assert messages[0] == {"role": "system", "content": prompts.load_prompt("respond.v1")}
    facts = messages[1]["content"]
    key_values = {str(v) for v in grading.answer_key(check).values}
    assert set(_line(facts, "ANSWER KEY: ").split(", ")) == key_values
    next_values = {str(v) for v in grading.answer_key(next_check).values}
    assert set(_line(facts, "NEXT ANSWER KEY: ").split(", ")) == next_values
    assert _line(facts, "NEXT QUESTION (asked right after your reply): ") == next_check.say
    never = _line(facts, "NEVER USE AS EXAMPLES: ")
    assert all(c.equation in never for c in concept.checks)
    for expected in ("ACTION: reteach", f"ENGINE'S REASON: {decision.reason}",
                     "LAST DECISION'S REASON: A new session.",
                     'UNDERSTOOD AS: answer, answer "-2, -3", working shown: no', "SYMPY'S VERDICT: wrong",
                     f"MISTAKE: {mistake.id}", mistake.fix, "STRATEGY: worked_example: solve a similar",
                     "- q-label: text Solve:", "- q-eq: latex x^{2}-5x+6=0"):
        assert expected in facts
    assert all(name in _line(facts, "FORMULAS (place them by name): ") for name in cur.formulas)
    assert facts.splitlines()[-1] == "STUDENT SAID: minus 2 and minus 3"


def test_respond_prompt_for_hint_and_needs_review(cur):
    concept = cur.concepts["factorisation"]
    check = concept.checks[0]
    common = dict(concept=concept, step=None, check=check, key=grading.answer_key(check), next_check=None,
                  next_key=None, understood=Understood(type="dont_know"), grade=None, mistake=None, board=[],
                  check_equations=[check.equation], formulas=cur.formulas, previous_reason="")
    hint = Decision(rule="R4", action="hint", concept_id=concept.id, reason="Stuck. Hint 2 of 3.", hint_level=2)
    facts = prompts.respond_messages(decision=hint, hint_text=check.hints[1], **common)[1]["content"]
    assert "HINT LEVEL: 2 of 3" in facts and f"LESSON'S HINT: {check.hints[1]}" in facts
    assert "ANSWER KEY: " in facts and "- (empty)" in facts
    review = Decision(rule="R9", action="needs_review", concept_id=concept.id, reason="Three reteaches.")
    facts = prompts.respond_messages(decision=review, hint_text=None, **common)[1]["content"]
    assert "ANSWER KEY: " in facts


@pytest.mark.parametrize("name", ["respond.v1", "respond.v2"])
def test_the_broken_prompt_demo_removes_every_reveal_instruction(cur, name):
    """README's demo deletes one four-line rule from the respond prompt (v1 now, v2 once it is switched on).
    Nothing else may forbid giving the answer away, neither another part of the file nor the facts the code
    adds, or the demo could never go red."""
    lines = prompts.load_prompt(name).splitlines()
    start, end = _no_reveal_rule(lines)
    assert end - start == 4                               # the README says "(four lines)"
    broken = "\n".join(lines[:start] + lines[end:])
    concept = cur.concepts["factorisation"]
    check, next_check = concept.checks[0], concept.checks[1]
    for action in ("hint", "follow_up", "explain", "reteach"):
        decision = Decision(rule="R4", action=action, concept_id=concept.id, reason="A reason.", hint_level=1,
                            ask_check_id=next_check.id if action == "reteach" else None)
        facts = prompts.respond_messages(
            decision=decision, concept=concept, step=None, check=check, key=grading.answer_key(check),
            next_check=next_check if action == "reteach" else None,
            next_key=grading.answer_key(next_check) if action == "reteach" else None,
            understood=Understood(type="dont_know"), grade=None, mistake=None, hint_text=check.hints[0],
            board=[], check_equations=[check.equation], formulas=cur.formulas, previous_reason="",
            student_text="just tell me the answer")[1]["content"]
        for phrase in ("give away", "give no answer", "never reveal", "secret"):
            assert phrase not in (broken + "\n" + facts).lower(), (action, phrase)


def test_v2_drafts_exist_and_keep_the_no_reveal_rule():
    """The v2 prompts are drafts: config stays on v1 until a Layer 2 run passes with them. respond.v2 keeps
    v1's no-reveal rule word for word, so the README demo works the same on either version."""
    for name in ("understand.v2", "respond.v2"):
        assert len(prompts.load_prompt(name)) > 500
    v1, v2 = prompts.load_prompt("respond.v1").splitlines(), prompts.load_prompt("respond.v2").splitlines()
    (start_1, end_1), (start_2, end_2) = _no_reveal_rule(v1), _no_reveal_rule(v2)
    assert v2[start_2:end_2] == v1[start_1:end_1]


def _a_times_c(equation: str):
    """a × c of a question's equation (left side minus right side), or None when it isn't a quadratic."""
    eq = mathcheck.parse_check(equation)
    poly = Poly(expand((eq.lhs - eq.rhs).doit()), mathcheck.x)
    return poly.coeff_monomial(mathcheck.x**2) * poly.coeff_monomial(1) if poly.degree() == 2 else None


def test_respond_v2_labelled_step_example_passes_the_reply_rules(cur):
    """respond.v2's two-line example (a words line, then its maths line) is a reply board.py accepts. Its product
    is no lesson question's a × c, so a model copying it into a hint can't mix it up with the question's numbers."""
    lines = prompts.load_prompt("respond.v2").splitlines()
    example = [json.loads(line) for line in lines if line.strip().startswith('{"op": "write", "id": "s')]
    assert [(cmd["id"], cmd["text"], cmd["latex"], cmd["check"]) for cmd in example] == [
        ("s1", "Step one: multiply a by c", None, None), ("s2", None, "4(-5) = -20", "Eq(4*(-5), -20)")]
    assert -20 not in {_a_times_c(check.equation) for concept in cur.concepts.values() for check in concept.checks}
    concept = cur.concepts["factorisation"]
    check, next_check = curriculum.get_check(concept, "fa2"), curriculum.get_check(concept, "fa3")
    reply = {"segments": [{"say": "Step one: multiply a by c.", "board": example}]}
    for action in ("hint", "follow_up", "explain", "reteach"):
        ctx = board.ReplyContext(action=action, board=[], anchor=check.equation, key=grading.answer_key(check),
                                 next_key=grading.answer_key(next_check), student_answer="",
                                 check_equations=[c.equation for c in concept.checks], formulas=cur.formulas)
        assert prompts.read_reply(reply, ctx)[1] == [], action


def test_understand_v2_reads_step_requests_as_help():
    """understand.v2: asking to be shown how to do the open question is dont_know, unless it holds an answer."""
    text = prompts.load_prompt("understand.v2")
    dont_know = text[text.index("- dont_know:"):text.index("- claims_understanding:")]
    for phrase in ("explain it step by step", "how do I solve this?", "show me how", "it is an answer"):
        assert phrase in dont_know, phrase


def test_schemas_are_strict():
    for schema in (prompts.UNDERSTAND_SCHEMA, prompts.RESPOND_SCHEMA):
        objects = list(_objects(schema))
        assert objects
        for obj in objects:
            assert obj["additionalProperties"] is False
            assert sorted(obj["required"]) == sorted(obj["properties"])
    understand = prompts.UNDERSTAND_SCHEMA["properties"]
    assert set(understand) == set(Understood.model_fields)
    assert set(understand["type"]["enum"]) == set(get_args(ReplyType))
    assert understand["mistake"]["type"] == ["string", "null"]
    segment = prompts.RESPOND_SCHEMA["properties"]["segments"]["items"]
    command = segment["properties"]["board"]["items"]["properties"]
    assert set(segment["properties"]) == set(Segment.model_fields)
    assert set(command) == set(BoardCommand.model_fields)
    assert all(command[name]["type"] == ["string", "null"]
               for name in ("id", "latex", "check", "formula", "text", "target", "part"))
    # what strict mode returns (every field, unused ones null) is a valid Reply
    reply = {"segments": [{"say": "Look at the brackets.", "board": [
        {"op": "highlight", "id": None, "latex": None, "check": None, "relation": "equiv", "formula": None,
         "text": None, "target": "q-eq", "part": None}]}]}
    assert Reply.model_validate(reply).segments[0].board[0].target == "q-eq"


def test_working_hint_covers_every_kind():
    for kind in get_args(Kind):
        assert prompts.working_hint(kind)
        assert kind in prompts.ANSWER_FORMATS


def test_read_understood_checks_the_answer_format(cur):
    check = cur.concepts["factorisation"].checks[0]
    reply = {"type": "answer", "answer": "2, 3", "showed_working": True, "mistake": None, "safety": False}
    assert prompts.read_understood(reply, check) == (Understood(**reply), [])
    understood, problems = prompts.read_understood({**reply, "answer": "two and three"}, check)
    assert understood is None and "doesn't fit the ANSWER FORMAT" in problems[0]
    assert prompts.read_understood({**reply, "answer": ""}, check)[1] == []      # working only: no answer
    assert prompts.read_understood({**reply, "answer": "4"}, None)[1] == []      # teaching: nothing to parse
    understood, problems = prompts.read_understood({**reply, "type": "maybe"}, check)
    assert understood is None and problems[0].startswith("type: ")


def test_read_reply_lists_problems(cur):
    check = cur.concepts["factorisation"].checks[0]
    ctx = board.ReplyContext(action="hint", board=[], anchor=check.equation, key=grading.answer_key(check),
                             next_key=None, student_answer="", check_equations=[check.equation],
                             formulas=cur.formulas)
    fine = {"segments": [{"say": "Look at the number on its own.", "board": []}]}
    assert prompts.read_reply(fine, ctx) == ([Segment(say="Look at the number on its own.")], [])
    segments, problems = prompts.read_reply({"segments": [{"say": "The roots are 2 and 3. Try x^2."}]}, ctx)
    assert len(segments) == 1 and len(problems) == 2           # LaTeX in speech, and the answer given away
    assert prompts.read_reply({"segments": []}, ctx)[1][0].startswith("segments: ")
