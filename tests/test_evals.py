"""evals/layer2.py and the evals command: the scenario file and the run rules, with a fake client (no network)."""
import json
import subprocess

import pytest
from conftest import FakeLLM

import evals.__main__ as evals_main
from app import curriculum, fallback, prompts, safety
from app.config import MODELS, PROMPT_VERSIONS, Settings
from app.llm import ProviderError
from app.models import ACTIONS
from evals import layer2

SETTINGS = Settings(openrouter_api_key="test-key", access_code="test", session_secret="test",
                    database_url="sqlite://", llm_mode="auto", max_turns_per_day=1000, tts_voice="sarah",
                    stt_language="en", https_only=False)
HINT = {"segments": [{"say": "Look at the middle term and the number on its own.", "board": []}]}
REVEALS = {"segments": [{"say": "The roots are 2 and 3.", "board": []}]}


def understood(kind: str, answer: str = "") -> dict:
    """UNDERSTAND's JSON, as strict mode returns it."""
    return {"type": kind, "answer": answer, "showed_working": False, "mistake": None, "safety": False}


def scenario(scenario_id: str) -> dict:
    return next(s for s in layer2.load_scenarios() if s["id"] == scenario_id)


def good() -> FakeLLM:
    """A client that passes "stuck_hinglish": dont_know, then a hint that gives nothing away."""
    return FakeLLM(understand=[understood("dont_know")], respond=[HINT])


def wrong_type() -> FakeLLM:
    """A client that fails "stuck_hinglish": a question, so R7 explains instead of R4 giving a hint."""
    return FakeLLM(understand=[understood("question")], respond=[HINT])


def trouble() -> FakeLLM:
    """A client whose UNDERSTAND call fails at the provider, so the turn is scripted."""
    return FakeLLM(understand=[ProviderError("HTTP 429")])


def test_scenarios_file_valid(cur):
    scenarios = layer2.load_scenarios()
    assert len(scenarios) == 20 and len({s["id"] for s in scenarios}) == 20
    for s in scenarios:
        setup, expect = s["setup"], s["expect"]
        assert set(setup) <= layer2.SETUP_KEYS and set(expect) <= layer2.EXPECT_KEYS, s["id"]
        concept = cur.concepts[setup["concept"]]
        if setup["status"] == "checking":
            assert curriculum.get_check(concept, setup["check"]) is not None, s["id"]
        else:
            assert setup["status"] == "teaching", s["id"]
            assert "step" not in setup or curriculum.get_step(concept, setup["step"]), s["id"]   # none: the topic start
        assert expect["action"] in ACTIONS and s["title"] and s["student_says"].strip(), s["id"]
        assert expect.get("mistake") in {None, *(mistake.id for mistake in concept.mistakes)}, s["id"]
        assert fallback.keyword(s["student_says"], setup["status"]) is None, s["id"]   # every reply reaches UNDERSTAND
    assert {s["setup"]["concept"] for s in scenarios} == set(cur.order)     # all six concepts
    safety_scenario = next(s for s in scenarios if s["expect"]["rule"] == "R10")
    assert not safety.is_safety(safety_scenario["student_says"])            # UNDERSTAND must catch it, not a keyword


def test_every_scenario_setup_runs(cur):
    # Every reply reaches UNDERSTAND (none is a keyword fast path), so a failed call leaves the run not scored;
    # the scripted turn that follows must not crash into the "Sorry" reply.
    for s in layer2.load_scenarios():
        run = layer2.run_once(s, cur, SETTINGS, trouble())
        assert run["outcome"] == "not scored" and "internal_error" not in run["issues"], s["id"]


def test_not_sure_while_teaching_needs_a_checked_example(cur):
    """The scenario fits the code: "not sure" on a step is R7 example, and it passes only when RESPOND solves an
    example on the board (a SymPy-checked line of its own), not when it just talks."""
    solved = {"segments": [{"say": "Here's another one. Minus 4 and minus 5 multiply to 20 and add to minus 9.",
                            "board": [{"op": "write", "id": "e1", "latex": "x^2 - 9x + 20 = 0",
                                       "check": "Eq(x**2 - 9*x + 20, 0)", "relation": "new"},
                                      {"op": "write", "id": "e2", "latex": "(x-4)(x-5) = 0",
                                       "check": "Eq((x-4)*(x-5), 0)"}]}]}
    talks = {"segments": [{"say": "Let's look at it again, slowly.", "board": []}]}
    not_sure = scenario("not_sure_while_teaching")
    run = layer2.run_once(not_sure, cur, SETTINGS, FakeLLM(understand=[understood("not_sure")], respond=[solved]))
    assert (run["outcome"], run["rule"], run["action"]) == ("pass", "R7", "example"), run["problems"]
    run = layer2.run_once(not_sure, cur, SETTINGS, FakeLLM(understand=[understood("not_sure")], respond=[talks]))
    assert run["problems"] == ["the reply wrote no checked line of its own"]


def test_run_rules_majority_and_not_scored():
    assert layer2.verdict(["pass", "pass", "fail"]) == "pass"
    assert layer2.verdict(["pass", "fail", "fail"]) == "fail"
    assert layer2.verdict(["pass", "pass", "not scored"]) == "pass"
    assert layer2.verdict(["pass", "fail", "not scored"]) == "fail"          # 1 of 2 is not a majority
    assert layer2.verdict(["pass", "not scored", "not scored"]) == "not scored"
    assert layer2.verdict(["pass", "pass", "fail", "fail", "pass"]) == "pass"   # --runs 5: more than half
    assert layer2.verdict(["pass", "pass", "fail", "fail"]) == "fail"


def test_first_output_rejection_fails_run(cur):
    stuck = scenario("stuck_hinglish")
    client = FakeLLM(understand=[understood("dont_know", answer="two"), understood("dont_know")], respond=[HINT])
    run = layer2.run_once(stuck, cur, SETTINGS, client)
    assert run["outcome"] == "fail" and run["problems"] == ["understand_rejected"]   # every expectation was met
    # a rejection wins over provider trouble: the run fails instead of being run again
    client = FakeLLM(understand=[understood("dont_know")], respond=[REVEALS, ProviderError("timeout")])
    run = layer2.run_once(stuck, cur, SETTINGS, client)
    assert layer2.provider_trouble(run["calls"]) and run["outcome"] == "fail"
    assert run["problems"][0].startswith("respond_rejected:gives away the answer")


def test_provider_trouble_reruns(cur):
    clients = [trouble(), trouble(), good(), good(), trouble(), trouble(), trouble()]
    result = layer2.run_scenario(scenario("stuck_hinglish"), 3, cur, SETTINGS, lambda: clients.pop(0))
    runs = [(run["outcome"], run["attempts"]) for run in result["runs"]]
    assert runs == [("pass", 3), ("pass", 1), ("not scored", 3)]           # re-run at most twice
    assert result["verdict"] == "pass" and clients == []                    # 2 scored runs, both passed
    assert layer2.provider_trouble([{"outcome": "ok", "backup_used": True}])           # a backup is trouble too
    assert not layer2.provider_trouble([{"outcome": "bad_output", "backup_used": False}])


def test_run_saves_results_with_models_and_prompts(cur, tmp_path, capsys):
    clients = [good(), wrong_type(), good()]
    args = layer2.parse_args(["--only", "stuck_hinglish", "--model-respond", "test/model",
                              "--prompt-respond", "respond.v1"])
    code = layer2.run(args, settings=SETTINGS, cur=cur, make_client=lambda: clients.pop(0), results_dir=tmp_path)
    assert code == 0                                                        # 2 of 3 runs passed: a majority
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["models"]["respond"] == ["test/model"] and report["prompts"]["respond"] == "respond.v1"
    assert [run["outcome"] for run in report["scenarios"][0]["runs"]] == ["pass", "fail", "pass"]
    assert MODELS["respond"][0]["id"] != "test/model"                       # the override was undone
    assert "PASS" in capsys.readouterr().out
    clients = [wrong_type(), wrong_type(), good()]
    code = layer2.run(layer2.parse_args(["--only", "stuck_hinglish"]), settings=SETTINGS, cur=cur,
                      make_client=lambda: clients.pop(0), results_dir=tmp_path / "second")
    assert code == 1                                                        # a failed scenario fails the command


def test_prompt_understand_flag_tries_a_new_understand_prompt(cur, tmp_path):
    args = layer2.parse_args(["--prompt-understand", "understand.v2", "--prompt-respond", "respond.v2"])
    assert layer2._overrides(args) == ({}, {"understand": "understand.v2", "respond": "respond.v2"})
    with pytest.raises(SystemExit):                                         # there is no such file
        layer2.parse_args(["--prompt-understand", "understand.v9"])
    used, configured = [good(), good(), good()], dict(PROMPT_VERSIONS)
    clients = list(used)
    args = layer2.parse_args(["--only", "stuck_hinglish", "--prompt-understand", "understand.v1"])
    assert layer2.run(args, settings=SETTINGS, cur=cur, make_client=lambda: clients.pop(0), results_dir=tmp_path) == 0
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["prompts"] == {"understand": "understand.v1", "respond": configured["respond"]}
    system_prompts = [messages[0]["content"] for client in used for role, messages in client.asked if role == "understand"]
    assert len(system_prompts) == 3 and set(system_prompts) == {prompts.load_prompt("understand.v1")}
    assert PROMPT_VERSIONS == configured                                    # the override was undone


def test_evals_with_key_runs_layer2(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(evals_main.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, returncode=0))
    asked = []
    monkeypatch.setattr(evals_main.layer2, "run", lambda args, settings: asked.append(args.only) or 1)
    assert evals_main.main(["--only", "stuck_hinglish"]) == 1               # Layer 2's failure is the exit code
    assert asked == ["stuck_hinglish"]


def test_model_flag_keeps_listed_params():
    assert layer2._model("respond", "google/gemini-3.8-flash") == MODELS["respond"][1]            # as in config
    assert layer2._model("respond", "anthropic/claude-haiku-5.5") == MODELS["respond"][0]         # this role's entry
    assert layer2._model("understand", "google/gemini-3.8-flash") == MODELS["respond"][1]         # another role's
    unlisted = layer2._model("understand", "test/model")
    assert unlisted == {"id": "test/model", "params": {"max_tokens": MODELS["understand"][0]["params"]["max_tokens"]}}
