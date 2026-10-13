"""app/prompts.py: the prompt files, the strict JSON schemas, the per-turn messages and the output readers."""
import json
from typing import get_args

import pytest
from sympy import Poly, expand

from app import board, curriculum, grading, mathcheck, prompts
from app.config import PROMPT_VERSIONS, ROOT
from app.models import BoardCommand, BoardLine, Decision, Kind, Reply, ReplyType, Segment, Understood

NO_REVEAL = "- Never give away the answer to the PENDING QUESTION"
DEMO_LINE = "hint: Give the hint for HINT LEVEL, built on the LESSON'S HINT but aimed at what the student said."


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
    assert messages[0]["content"] == prompts.load_prompt(PROMPT_VERSIONS["understand"])
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
    assert messages[0] == {"role": "system", "content": prompts.load_prompt(PROMPT_VERSIONS["respond"])}
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


def test_the_broken_prompt_demo_edits_a_line_of_the_configured_respond_prompt():
    """README's demo swaps RESPOND's hint instruction for one that gives the answer away. That line must be in the
    configured respond prompt exactly once, and README must copy that version and quote the line."""
    respond = PROMPT_VERSIONS["respond"]
    assert prompts.load_prompt(respond).splitlines().count(DEMO_LINE) == 1
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"cp app/prompts/{respond}.md app/prompts/respond.broken.md" in readme and DEMO_LINE in readme


@pytest.mark.parametrize("name", ["respond.v2", "respond.v3", "respond.v4", "respond.v5"])
def test_later_respond_prompts_keep_the_no_reveal_rule(name):
    """Every respond version keeps v1's no-reveal rule word for word."""
    v1, later = prompts.load_prompt("respond.v1").splitlines(), prompts.load_prompt(name).splitlines()
    (start_1, end_1), (start_2, end_2) = _no_reveal_rule(v1), _no_reveal_rule(later)
    assert later[start_2:end_2] == v1[start_1:end_1]


def _a_times_c(equation: str):
    """a × c of a question's equation (left side minus right side), or None when it isn't a quadratic."""
    eq = mathcheck.parse_check(equation)
    poly = Poly(expand((eq.lhs - eq.rhs).doit()), mathcheck.x)
    return poly.coeff_monomial(mathcheck.x**2) * poly.coeff_monomial(1) if poly.degree() == 2 else None


@pytest.mark.parametrize("name", ["respond.v2", "respond.v3", "respond.v4", "respond.v5"])
def test_labelled_step_example_passes_the_reply_rules(cur, name):
    """The two-line example (a words line, then its maths line) is a reply board.py accepts. Its product
    is no lesson question's a × c, so a model copying it into a hint can't mix it up with the question's numbers."""
    lines = prompts.load_prompt(name).splitlines()
    example = [json.loads(line) for line in lines if line.strip().startswith('{"op": "write", "id": "s')]
    assert [(cmd["id"], cmd["text"], cmd["latex"], cmd["check"]) for cmd in example] == [
        ("s1", "Step one: multiply a by c", None, None), ("s2", None, "4(-5) = -20", "Eq(4*(-5), -20)")]
    assert -20 not in {_a_times_c(check.equation) for concept in cur.concepts.values() for check in concept.checks}
    concept = cur.concepts["factorisation"]
    check, next_check = curriculum.get_check(concept, "fa2"), curriculum.get_check(concept, "fa3")
    reply = {"segments": [{"say": "Step one: multiply a by c.", "board": example}]}
    for action in ("hint", "follow_up", "explain", "reteach", "example"):
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


@pytest.mark.parametrize("name", ["understand.v3", "understand.v4"])
def test_understand_knows_every_reply_type_and_that_got_it_goes_on(name):
    """understand.v3 and v4 list every reply type once. While teaching, "got it" means go on and only an explicit
    "test me" skips the steps; a bare "no" or "nahi" is not sure only while teaching."""
    lines = prompts.load_prompt(name).splitlines()
    assert sorted(line[2:line.index(":")] for line in lines if line.startswith("- ")) == sorted(get_args(ReplyType))
    text = " ".join(" ".join(lines).split())                               # line breaks don't matter
    assert ('While PHASE is teaching, "got it", "understood" and "yes" mean go on: next or claims_understanding. '
            'Only an explicit request like "test me" or "I already know this" is test_me.') in text
    not_sure = text[text.index("- not_sure:"):text.index("- claims_understanding:")]
    for phrase in ('"not sure"', '"another example"', 'While PHASE is teaching, a bare "no" or "nahi" is not_sure'):
        assert phrase in not_sure, phrase


def test_respond_v4_adds_the_example_action():
    """respond.v4 is v3 plus the example action: "not sure" on a step gets a similar but different equation
    solved on the board, starting with relation new (now allowed for it), never one of the lesson's questions."""
    v3, v4 = prompts.load_prompt("respond.v3"), prompts.load_prompt("respond.v4")
    example = " ".join(v4[v4.index("\nexample: "):v4.index("\nreteach: ")].split())
    for phrase in ("The student wasn't sure about the CURRENT STEP.", "similar but different equation",
                   "in at most 3 segments", '"relation": "new"', "NEVER USE AS EXAMPLES"):
        assert phrase in example, phrase
    assert ('"relation": "new" is only for the actions explain, reteach, example and needs_review.'
            in " ".join(v4.split()))
    assert v4.startswith(v3[:v3.index("## Rules for the board")])          # the opening and format are v3's


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


def test_understand_v4_reads_a_request_about_one_part_as_a_question():
    """understand.v4 (in use) is v3 except for two reply types: say_again repeats everything just said, and asking
    about one part, line or example of it ("repeat the last example") is a question, which RESPOND answers."""
    assert PROMPT_VERSIONS["understand"] == "understand.v4"
    v3, v4 = (" ".join(prompts.load_prompt(name).split()) for name in ("understand.v3", "understand.v4"))
    blocks = [text[text.index("- question:"):text.index("- next:")] for text in (v3, v4)]
    assert v3.replace(blocks[0], "") == v4.replace(blocks[1], "")            # nothing else changed
    question, say_again = blocks[1].split("- say_again:")
    assert '"repeat the last example"' in question and "everything" in say_again
    assert "Asking to hear the question again is say_again" in say_again


def test_respond_v5_points_at_the_lines_it_talks_about():
    """respond.v5 (in use) is v4 plus: a segment that talks about a line already on the BOARD highlights it, and
    explain answers a question about one part or example of the CURRENT STEP or YOUR LAST REPLY on its own."""
    assert PROMPT_VERSIONS["respond"] == "respond.v5"
    v4, v5 = prompts.load_prompt("respond.v4"), prompts.load_prompt("respond.v5")
    text = " ".join(v5.split())
    for phrase in ("highlight that line", "YOUR LAST REPLY", "one part, line or example", "the highest example",
                   "in one or two segments"):
        assert phrase in text, phrase
    assert v5.startswith(v4[:v4.index("## Rules for the board")])          # the opening and format are v4's


def _facts_for_a_question(cur, step, on_board, last_reply=None) -> str:
    """RESPOND's facts for "Can you repeat the last example?" on this step, with these lines on the board."""
    concept = cur.concepts["standard_form"]
    decision = Decision(rule="R7", action="explain", concept_id="standard_form", reason="You asked a question.")
    return prompts.respond_messages(
        decision=decision, concept=concept, step=step, check=None, key=None, next_check=None, next_key=None,
        understood=Understood(type="question"), grade=None, mistake=None, hint_text=None, board=on_board,
        check_equations=[], formulas=cur.formulas, previous_reason="", student_text="Can you repeat the last example?",
        last_reply=last_reply)[-1]["content"]


def test_respond_facts_give_the_step_part_by_part_with_its_examples_and_lines_and_the_last_reply(cur):
    """So that "the last example" can be found: each part of the step with the example it belongs to (a line with
    relation "new" starts the next one) and the board lines it wrote or pointed at; and what the tutor said last,
    part by part the same way."""
    step = curriculum.get_step(cur.concepts["standard_form"], "rearrange")
    on_board = board.apply([], step.segments, cur.formulas)
    facts = _facts_for_a_question(cur, step, on_board, last_reply=step.segments)
    assert f"- part 1 (example 1; lines ra1, ra2): {step.segments[0].say}" in facts
    assert f"- part 2 (example 1; lines ra3): {step.segments[1].say}" in facts
    assert f"- part 3 (example 2; lines ra4, ra5): {step.segments[2].say}" in facts
    assert "The step has 2 examples. The first example is example 1 (parts 1-2). The last example is example 2 "            "(part 3)." in facts                                         # said outright: models misread "last"
    assert "YOUR LAST REPLY, part by part:" in facts and f"(lines ra4, ra5): {step.segments[2].say}" in facts


def test_respond_facts_name_only_lines_still_on_the_board(cur):
    """A line pushed off the board can't be highlighted (the reply would be rejected), so it isn't offered."""
    step = curriculum.get_step(cur.concepts["standard_form"], "rearrange")
    on_board = [line for line in board.apply([], step.segments, cur.formulas) if line.id != "ra1"]
    facts = _facts_for_a_question(cur, step, on_board)
    assert f"- part 1 (example 1; lines ra2): {step.segments[0].say}" in facts
