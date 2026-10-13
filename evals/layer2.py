"""Layer 2 evals: tutor scenarios against the real LLM, each run several times, scored by majority.

A run fails if its expectations aren't met, or if the first output of UNDERSTAND or RESPOND was rejected
(read from the turn's issues; this wins over provider trouble). A run hit by provider trouble (a provider
error, a backup model or a skipped model in its calls) is run again, up to twice, then it is "not scored".
A scenario passes with a majority of passes among its scored runs; under 2 scored runs it is "not scored".
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx
import yaml

from app import board, curriculum, db, grading
from app.config import MODELS, POINTS_TO_MASTER, PROMPT_VERSIONS, PROMPTS_DIR, ROOT, Settings, load_settings
from app.llm import Client
from app.models import PINNED_IDS, AnswerKey, ConceptState, Curriculum, Pending, TurnResult
from app.tutor import Tutor

SCENARIOS_FILE = Path(__file__).with_name("scenarios.yaml")
RESULTS_DIR = ROOT / "evals" / "results"
RERUNS = 2                                   # extra tries for a run hit by provider trouble
PENDING_FIELDS = ("hint_level", "awaiting_working", "followup_used", "last_answer")
SETUP_KEYS = {"concept", "status", "step", "check", "points", "reteach_count", *PENDING_FIELDS}
EXPECT_KEYS = {"type", "rule", "action", "mistake", "points", "unchanged", "checked_line", "no_reveal",
               "tutor_says", "issue", "points_at"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The evals command's flags. No argv means no flags."""
    parser = argparse.ArgumentParser(prog="python -m evals", description="Layer 1 tests, then Layer 2 scenarios.")
    parser.add_argument("--runs", type=int, default=3, help="runs per scenario (at least 2; default 3)")
    parser.add_argument("--only", metavar="ID", help="run just this scenario")
    parser.add_argument("--model-understand", metavar="ID", help="use only this model for UNDERSTAND")
    parser.add_argument("--model-respond", metavar="ID", help="use only this model for RESPOND")
    parser.add_argument("--prompt-understand", metavar="NAME", help="use app/prompts/NAME.md for UNDERSTAND")
    parser.add_argument("--prompt-respond", metavar="NAME", help="use app/prompts/NAME.md for RESPOND")
    args = parser.parse_args(argv or [])
    if args.runs < 2:
        parser.error("--runs must be at least 2: a scenario needs 2 scored runs")
    if args.only and args.only not in [scenario["id"] for scenario in load_scenarios()]:
        parser.error(f"no scenario has the id {args.only!r}")
    for name in (args.prompt_understand, args.prompt_respond):
        if name and not (PROMPTS_DIR / f"{name}.md").exists():
            parser.error(f"no prompt file app/prompts/{name}.md")
    return args


def load_scenarios(path: Path = SCENARIOS_FILE) -> list[dict]:
    """The scenarios in evals/scenarios.yaml, in file order."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run(args: argparse.Namespace | None = None, *, settings: Settings | None = None, cur: Curriculum | None = None,
        make_client=None, results_dir: Path = RESULTS_DIR) -> int:
    """Run the scenarios, print one line each and save the results; returns 1 if any scenario failed."""
    args = args or parse_args()
    settings = replace(settings or load_settings(), llm_mode="auto")      # always the real LLM path
    cur = cur or curriculum.load()
    scenarios = [s for s in load_scenarios() if args.only in (None, s["id"])]
    models, prompts = _overrides(args)
    # patch.dict puts the old MODELS and PROMPT_VERSIONS entries back when the block ends
    with httpx.Client() as http, mock.patch.dict(MODELS, models), mock.patch.dict(PROMPT_VERSIONS, prompts):
        new_client = make_client or (lambda: Client(settings.openrouter_api_key, http=http))
        used = {"models": {role: [m["id"] for m in MODELS[role]] for role in ("understand", "respond")},
                "prompts": dict(PROMPT_VERSIONS)}
        models_text = "; ".join(f"{role} {', '.join(ids)}" for role, ids in used["models"].items())
        print(f"Layer 2: {len(scenarios)} scenario(s) x {args.runs} runs. Models: {models_text}. "
              f"Prompts: {', '.join(used['prompts'].values())}.", flush=True)
        results = []
        for scenario in scenarios:
            results.append(run_scenario(scenario, args.runs, cur, settings, new_client))
            print(_summary(results[-1]), flush=True)
    path = _save({"time": datetime.now(timezone.utc).isoformat(), **used, "runs": args.runs,
                  "scenarios": results}, results_dir)
    print(f"Results saved to {path}")
    return 1 if any(result["verdict"] == "fail" for result in results) else 0


def run_scenario(scenario: dict, runs: int, cur: Curriculum, settings: Settings, make_client) -> dict:
    """Run one scenario `runs` times. A run hit by provider trouble is tried again, up to RERUNS more times."""
    results = []
    for _ in range(runs):
        for attempt in range(1, RERUNS + 2):
            result = run_once(scenario, cur, settings, make_client())     # a new client resets the breakers
            if result["outcome"] != "not scored":
                break
        results.append({**result, "attempts": attempt})
    return {"id": scenario["id"], "title": scenario["title"],
            "verdict": verdict([result["outcome"] for result in results]), "runs": results}


class Recording:
    """The model client, keeping every output our checks rejected, so a failed run shows what the AI wrote."""

    def __init__(self, client):
        self.client, self.rejected = client, []

    def __getattr__(self, name):                     # everything else is the client's own
        return getattr(self.client, name)

    def chat_json(self, role, messages, schema_name, schema, validate, **options):
        def keep(data):
            try:
                return validate(data)
            except ValueError:
                self.rejected.append({"role": role, "output": data})
                raise
        return self.client.chat_json(role, messages, schema_name, schema, keep, **options)


def run_once(scenario: dict, cur: Curriculum, settings: Settings, client) -> dict:
    """One run in a fresh in-memory database: the setup, Start, then the student's reply. Outcome and why."""
    setup, client = scenario["setup"], Recording(client)
    engine = db.connect("sqlite://")
    try:
        tutor = Tutor(cur, engine, settings, llm=client)
        student_id = setup_student(tutor, setup)
        before = _state(engine, cur, student_id, setup["concept"])
        result = tutor.turn(student_id, scenario["student_says"])
        after = _state(engine, cur, student_id, setup["concept"])
    finally:
        engine.dispose()
    check = curriculum.get_check(cur.concepts[setup["concept"]], setup.get("check"))
    problems = check_result(scenario["expect"], result, before, after, grading.answer_key(check) if check else None)
    rejected = [issue for issue in result.issues
                if issue == "understand_rejected" or issue.startswith("respond_rejected:")]
    if rejected:                                  # a rejected first output fails the run, provider trouble or not
        outcome = "fail"
    elif provider_trouble(result.calls):
        outcome = "not scored"
    else:
        outcome = "fail" if problems else "pass"
    return {"outcome": outcome, "problems": rejected + problems, "rule": result.decision.rule,
            "action": result.decision.action, "said": [segment.say for segment in result.segments],
            "issues": result.issues, "calls": result.calls, "rejected_outputs": client.rejected}


def setup_student(tutor: Tutor, setup: dict) -> str:
    """A new student in the setup's state (earlier concepts mastered), who has just pressed Start (S0)."""
    cur, concept_id = tutor.cur, setup["concept"]
    pending = None
    if setup["status"] == "checking":
        pending = Pending(setup["check"], **{name: setup[name] for name in PENDING_FIELDS if name in setup})
    state = ConceptState(concept_id, status=setup["status"], step_id=setup.get("step"),
                         points=float(setup.get("points", 0)), pending=pending, last_check_id=setup.get("check"),
                         reteach_count=setup.get("reteach_count", 0))
    with tutor.db_engine.begin() as conn:
        student_id = db.get_or_create_student(conn, "eval", "Asha")["id"]
        progress = db.load_progress(conn, student_id, cur)
        for earlier in cur.order[:cur.order.index(concept_id)]:          # so this concept is the current one
            progress[earlier].status, progress[earlier].points = "mastered", POINTS_TO_MASTER
        progress[concept_id] = state
        db.save_progress(conn, student_id, progress, cur.version)
    tutor.start(student_id)          # like a real session: the question is asked again, or the step replayed
    return student_id


def check_result(expect: dict, result: TurnResult, before: ConceptState, after: ConceptState,
                 key: AnswerKey | None) -> list[str]:
    """The expectations this turn didn't meet, one line each ([] means all were met)."""
    got = {"type": result.understood.type if result.understood else None, "rule": result.decision.rule,
           "action": result.decision.action, "mistake": result.decision.mistake, "points": after.points}
    problems = [f"{name}: expected {expect[name]!r}, got {got[name]!r}"
                for name in got if name in expect and expect[name] != got[name]]
    commands = [cmd for segment in result.segments for cmd in segment.board]
    if expect.get("unchanged") and after != before:
        problems.append("progress changed")
    if expect.get("checked_line") and not any(cmd.op == "write" and cmd.check and cmd.id not in PINNED_IDS
                                              for cmd in commands):
        problems.append("the reply wrote no checked line of its own")
    if expect.get("no_reveal"):
        answer = result.understood.answer if result.understood else ""
        reveal = board.reveals_answer(result.segments, key, answer)
        problems += [reveal] if reveal else []
    said = " ".join(segment.say for segment in result.segments).lower()
    if "tutor_says" in expect and expect["tutor_says"].lower() not in said:
        problems.append(f"the reply doesn't say {expect['tutor_says']!r}")
    pointed = {cmd.target for cmd in commands if cmd.op == "highlight"}
    if "points_at" in expect and not pointed & set(expect["points_at"]):
        problems.append(f"the reply highlights none of {expect['points_at']} (it highlights {sorted(pointed)})")
    if "issue" in expect and expect["issue"] not in result.issues:
        problems.append(f"the turn's issues don't include {expect['issue']!r}")
    return problems


def verdict(outcomes: list[str]) -> str:
    """A majority of passes among the scored runs; "not scored" when fewer than 2 runs were scored."""
    scored = [outcome for outcome in outcomes if outcome in ("pass", "fail")]
    if len(scored) < 2:
        return "not scored"
    return "pass" if 2 * scored.count("pass") > len(scored) else "fail"


def provider_trouble(calls: list[dict]) -> bool:
    """True when a call hit a provider error, used a backup model, or was skipped (breaker or deadline)."""
    return any(call["outcome"] in ("provider_error", "skipped") or call.get("backup_used") for call in calls)


# ---------- small helpers ----------
def _state(engine, cur: Curriculum, student_id: str, concept_id: str) -> ConceptState:
    with engine.connect() as conn:
        return db.load_progress(conn, student_id, cur)[concept_id]


def _overrides(args: argparse.Namespace) -> tuple[dict, dict]:
    """The MODELS and PROMPT_VERSIONS entries the flags ask for (empty: no change)."""
    models = {role: [_model(role, model_id)] for role, model_id in
              (("understand", args.model_understand), ("respond", args.model_respond)) if model_id}
    prompts = {role: name for role, name in
               (("understand", args.prompt_understand), ("respond", args.prompt_respond)) if name}
    return models, prompts


def _model(role: str, model_id: str) -> dict:
    """One model for the role, with no backup. A model listed in config keeps its params (this role's entry
    first, then any other role's); an unlisted one gets just the role's max_tokens."""
    for models in [MODELS[role], *MODELS.values()]:
        for model in models:
            if model["id"] == model_id:
                return model
    return {"id": model_id, "params": {"max_tokens": MODELS[role][0]["params"]["max_tokens"]}}


def _summary(result: dict) -> str:
    """One printed line per scenario, e.g. "PASS        stuck_hinglish     3 of 3 scored runs passed"."""
    scored = [run for run in result["runs"] if run["outcome"] != "not scored"]
    passed = sum(run["outcome"] == "pass" for run in scored)
    problem = next((p for run in scored for p in run["problems"]), "") if result["verdict"] == "fail" else ""
    return (f"{result['verdict'].upper():<11} {result['id']:<26} {passed} of {len(scored)} scored runs passed"
            + (f"  ({problem})" if problem else ""))


def _save(report: dict, results_dir: Path) -> Path:
    """Write the report to <results_dir>/<UTC time>.json and return the file's path."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path
