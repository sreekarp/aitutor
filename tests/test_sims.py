"""Simulations. The tutor runs in scripted mode; a FakeLLM plays the student model."""
import dataclasses
import json
import re

import pytest

from app.llm import BadOutputError, ProviderError
from conftest import FakeLLM, make_settings
from sims import __main__ as sims
from sims.__main__ import (check_reply, load_personas, save_run, simulate, student_messages, summary_row,
                           transcript_lines)

SCRIPTED = make_settings()
PERSONAS = load_personas()

# The roots concept: rt1 "is -2 a root of x² + x - 2 = 0?" -> yes; rt2 "is -1 a root of
# x² - 3x + 2 = 0?" -> no; rt3 "is 3/2 a root of 2x² - x - 3 = 0?" -> yes. Scripted mode reads the
# answer from the words before "because", and "because" counts as working.
RT1_RIGHT = "yes, because 4 - 2 - 2 = 0"
RT1_WRONG = "no, because -2 squared is -4"
RT2_RIGHT = "no, because 1 + 3 + 2 = 6"
RT3_RIGHT = "yes, because 9/2 - 3/2 - 3 = 0"


def said(text: str, still_has_mistake: bool = False) -> dict:
    """One reply from the student model."""
    return {"say": text, "still_has_mistake": still_has_mistake}


def student(*replies) -> FakeLLM:
    """A fake student model that gives these replies in order; an exception in the list is raised."""
    return FakeLLM(student=list(replies))


def test_persona_file_valid():
    assert list(PERSONAS) == ["fast_learner", "sign_flipper", "guesser", "overconfident"]
    assert PERSONAS["fast_learner"]["mistakes"] == []
    for persona in PERSONAS.values():
        assert persona["name"] and persona["first_name"] and persona["tests"] and persona["profile"].strip()
        if persona["id"] != "fast_learner":
            assert persona["mistakes"], persona["id"]


def test_load_personas_rejects_a_missing_field(tmp_path):
    path = tmp_path / "personas.yaml"
    path.write_text("- id: lazy\n  name: Lazy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lazy is missing"):
        load_personas(path)


def test_sim_stops_on_mastery(cur):
    fake = student(said("test me"), said(RT1_RIGHT), said(RT2_RIGHT), said("never sent"))
    record = simulate(PERSONAS["fast_learner"], cur, SCRIPTED, fake, None, concept_id="roots")
    assert record["summary"] == {"turns": 3, "hints": 0, "reteaches": 0, "false_advances": 0,
                                 "outcome": "mastered"}
    assert len(fake.asked) == 3                   # stopped as soon as roots was mastered
    assert record["final_progress"]["roots"]["status"] == "mastered"
    assert record["final_progress"]["standard_form"]["status"] == "mastered"   # seeded: it comes first
    assert record["error"] is None


def test_sim_run_counts_false_advances(cur):
    fake = student(
        said("test me", True),        # R6: no points
        said(RT1_WRONG, True),        # wrong -> R4 hint 1
        said(RT1_RIGHT, True),        # R1 (half a point) while still holding the mistake: a false advance
        said(RT2_RIGHT, False),       # R1, mistake gone
        said(RT3_RIGHT, False),       # R1 -> mastered
    )
    record = simulate(PERSONAS["sign_flipper"], cur, SCRIPTED, fake, None, concept_id="roots")
    assert record["summary"] == {"turns": 5, "hints": 1, "reteaches": 0, "false_advances": 1,
                                 "outcome": "mastered"}
    assert [t["decision"]["rule"] for t in record["turns"]] == ["S0", "R6", "R4", "R1", "R1", "R1"]


def test_persona_without_mistakes_never_counts_false_advances(cur):
    fake = student(said("test me", True), said(RT1_RIGHT, True), said(RT2_RIGHT, True))
    record = simulate(PERSONAS["fast_learner"], cur, SCRIPTED, fake, None, concept_id="roots")
    assert record["summary"]["false_advances"] == 0


def test_sim_counts_reteaches_and_stops_at_turn_limit(cur):
    fake = student(said("test me"), *[said(RT1_WRONG, True)] * 4)    # hints 1-3, then a reteach
    record = simulate(PERSONAS["sign_flipper"], cur, SCRIPTED, fake, None, concept_id="roots", max_turns=5)
    assert record["summary"] == {"turns": 5, "hints": 3, "reteaches": 1, "false_advances": 0,
                                 "outcome": "turn_limit"}


def test_sim_ends_on_student_failures(cur):
    fake = student(ProviderError("timeout"), BadOutputError("not JSON"))
    record = simulate(PERSONAS["guesser"], cur, SCRIPTED, fake, None)
    assert record["summary"]["outcome"] == "error"
    assert record["summary"]["turns"] == 0
    assert "twice in a row" in record["error"]
    assert len(record["turns"]) == 1              # only the tutor's greeting (S0)


def test_one_student_failure_is_retried(cur):
    fake = student(ProviderError("timeout"), said("ok"), ProviderError("timeout"), said("ok"),
                   ProviderError("timeout"), ProviderError("timeout"))
    record = simulate(PERSONAS["guesser"], cur, SCRIPTED, fake, None)
    assert record["summary"]["turns"] == 2
    assert record["summary"]["outcome"] == "error"


def test_check_reply_accepts_only_the_right_shape():
    assert check_reply({"say": " 2 and 3 ", "still_has_mistake": False}) == said("2 and 3")
    for bad in ({"say": "", "still_has_mistake": False}, {"say": "hi", "still_has_mistake": "no"},
                {"say": "hi"}, ["hi"]):
        with pytest.raises(ValueError):
            check_reply(bad)


def test_student_messages_show_profile_history_and_board():
    turns = [{"student": None, "tutor": ["Hi Meera! Solve x squared minus 5x plus 6 equals zero."]},
             {"student": "-2 and -3", "tutor": ["Let's check that."]}]
    messages = student_messages(PERSONAS["sign_flipper"], transcript_lines(turns), "x^{2}-5x+6=0")
    system, user = messages[0]["content"], messages[1]["content"]
    assert PERSONAS["sign_flipper"]["mistakes"][0] in system
    assert "Tutor: Hi Meera!" in user and "You: -2 and -3" in user and "x^{2}-5x+6=0" in user


def test_save_run_writes_json_and_markdown(cur, tmp_path):
    record = simulate(PERSONAS["guesser"], cur, SCRIPTED, student(said("ok")), None, max_turns=1)
    path = save_run(record, tmp_path)
    assert re.fullmatch(r"\d{8}T\d{6}Z-guesser\.json", path.name)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["summary"]["outcome"] == "turn_limit" and saved["turns"][1]["student"] == "ok"
    assert "**Student:** ok" in path.with_suffix(".md").read_text(encoding="utf-8")
    assert summary_row(record).startswith("guesser")


def test_main_needs_the_api_key(monkeypatch, capsys):
    monkeypatch.setattr(sims, "load_settings", lambda: SCRIPTED)
    assert sims.main([]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().out


def test_main_runs_one_persona_and_prints_a_row(monkeypatch, capsys, tmp_path, cur):
    keyed = dataclasses.replace(SCRIPTED, openrouter_api_key="sk-test")   # a key, but the tutor stays scripted
    monkeypatch.setattr(sims, "load_settings", lambda: keyed)
    monkeypatch.setattr(sims, "Client", lambda api_key: student(said("ok")))
    monkeypatch.setattr(sims.curriculum, "load", lambda: cur)            # skip a second startup validation
    monkeypatch.setattr(sims, "RUNS_DIR", tmp_path)
    assert sims.main(["--persona", "guesser", "--turns", "1"]) == 0
    out = capsys.readouterr().out
    assert "guesser" in out and "turn_limit" in out
    assert len(list(tmp_path.glob("*-guesser.json"))) == 1
