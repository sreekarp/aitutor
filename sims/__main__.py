"""Simulated students (spec §13): an LLM plays a student with a hidden profile and talks to the tutor
through the same text path a real student uses. Needs OPENROUTER_API_KEY. Each run is saved to sims/runs/.

    uv run python -m sims [--persona sign_flipper] [--concept quadratic_formula] [--turns 40]
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import curriculum, db
from app.config import MODELS, POINTS_TO_MASTER, PROMPT_VERSIONS, ROOT, Settings, load_settings
from app.llm import BadOutputError, Client, ProviderError
from app.models import ConceptState, Curriculum, TurnResult
from app.tutor import Tutor

PERSONAS_FILE = ROOT / "sims" / "personas.yaml"
RUNS_DIR = ROOT / "sims" / "runs"
PERSONA_FIELDS = ("id", "name", "first_name", "tests", "profile", "mistakes")
STUDENT_DEADLINE_S = 20.0    # one student reply: at most 2 model calls of up to 8 s each
HISTORY_LINES = 12           # the student model sees this many recent lines of the conversation
STUDENT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["say", "still_has_mistake"],
                  "properties": {"say": {"type": "string"}, "still_has_mistake": {"type": "boolean"}}}
HEADER = f"{'persona':<14} {'concept':<18} {'turns':>5} {'hints':>5} {'reteaches':>9}  {'outcome':<12} false advances"


# ---------- personas ----------
def load_personas(path: Path = PERSONAS_FILE) -> dict[str, dict]:
    """Read personas.yaml into {persona id: persona}. Raises ValueError if a persona is incomplete."""
    personas = {}
    for persona in yaml.safe_load(path.read_text(encoding="utf-8")):
        missing = [name for name in PERSONA_FIELDS if name not in persona]
        if missing:
            raise ValueError(f"persona {persona.get('id')} is missing {', '.join(missing)}")
        if not isinstance(persona["mistakes"], list):
            raise ValueError(f"persona {persona['id']}: mistakes must be a list")
        personas[persona["id"]] = persona
    return personas


# ---------- the student model ----------
def check_reply(data) -> dict:
    """chat_json's validate: the reply must be {"say": some words, "still_has_mistake": true or false}."""
    if not isinstance(data, dict) or not isinstance(data.get("say"), str) or not data["say"].strip():
        raise ValueError('the reply must be a JSON object whose "say" is non-empty text')
    if not isinstance(data.get("still_has_mistake"), bool):
        raise ValueError('"still_has_mistake" must be true or false')
    return {"say": data["say"].strip(), "still_has_mistake": data["still_has_mistake"]}


def transcript_lines(turns: list[dict]) -> list[str]:
    """The conversation so far as "Tutor: ..." and "You: ..." lines."""
    lines = []
    for turn in turns:
        if turn["student"] is not None:
            lines.append(f"You: {turn['student']}")
        lines.append("Tutor: " + " ".join(turn["tutor"]))
    return lines


def board_text(result: TurnResult) -> str:
    """The whiteboard as plain lines (LaTeX or words) that the student model can read."""
    lines = [line.text or line.latex or line.formula or "" for line in result.board]
    return "\n".join(lines) or "(empty)"


def student_messages(persona: dict, lines: list[str], board: str) -> list[dict]:
    """The student model's prompt: hidden profile and mistakes, the recent conversation, the board."""
    mistakes = "\n".join(f"- {mistake}" for mistake in persona["mistakes"]) or "- none"
    system = (
        "You are role-playing a Class 10 student in India who is learning quadratic equations from a tutor.\n"
        f"Your hidden profile (never mention it):\n{persona['profile'].strip()}\n"
        f"Mistakes you hold at the start:\n{mistakes}\n"
        "Reply as this student would speak: one or two short sentences, no LaTeX (say \"x squared\"). "
        "Keep a mistake until the tutor's explanation would really change your mind. "
        "Set still_has_mistake to true while you still hold any of your mistakes, otherwise false.\n"
        'Reply with JSON only: {"say": "...", "still_has_mistake": true or false}'
    )
    user = ("The lesson so far:\n" + "\n".join(lines[-HISTORY_LINES:])
            + f"\n\nThe whiteboard now:\n{board}\n\nWhat do you say next?")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def ask_student(student_llm, persona: dict, record: dict, board: str) -> dict:
    """One reply from the student model. Raises ProviderError or BadOutputError when it fails."""
    messages = student_messages(persona, transcript_lines(record["turns"]), board)
    return student_llm.chat_json("student", messages, "student_reply", STUDENT_SCHEMA, check_reply,
                                 deadline=time.monotonic() + STUDENT_DEADLINE_S, calls=record["student_calls"])


# ---------- one run ----------
def seed_student(engine, cur: Curriculum, persona: dict, concept_id: str) -> str:
    """Create the student; every concept before `concept_id` starts as mastered. Returns the student id."""
    with engine.begin() as conn:
        student = db.get_or_create_student(conn, f"sim_{persona['id']}", persona["first_name"])
        progress = db.load_progress(conn, student["id"], cur)
        for earlier in cur.order[: cur.order.index(concept_id)]:
            progress[earlier] = ConceptState(earlier, status="mastered", points=POINTS_TO_MASTER)
        db.save_progress(conn, student["id"], progress, cur.version)
    return student["id"]


def add_turn(record: dict, reply: dict | None, result: TurnResult) -> None:
    """Save one exchange in the run record: the student's reply (None for the greeting) and the tutor's turn."""
    decision = result.decision
    record["turns"].append({
        "student": reply["say"] if reply else None,
        "still_has_mistake": reply["still_has_mistake"] if reply else None,
        "tutor": [segment.say for segment in result.segments],
        "decision": {"rule": decision.rule, "action": decision.action, "reason": decision.reason,
                     "strategy": decision.strategy, "mistake": decision.mistake},
        "understood": result.understood.model_dump() if result.understood else None,
        "grade": asdict(result.grade) if result.grade else None,
        "issues": result.issues, "calls": result.calls,
    })


def summarise(turns: list[dict], outcome: str) -> dict:
    """The summary row's numbers, counted from the saved turns (True counts as 1 in sum)."""
    replies = [turn for turn in turns if turn["student"] is not None]
    return {"turns": len(replies), "outcome": outcome,
            "hints": sum(turn["decision"]["action"] == "hint" for turn in replies),
            "reteaches": sum(turn["decision"]["action"] == "reteach" for turn in replies),
            "false_advances": sum(turn["decision"]["rule"] == "R1" and turn["still_has_mistake"] for turn in replies)}


def talk(record: dict, persona: dict, tutor: Tutor, student_llm, student_id: str, max_turns: int) -> str:
    """The conversation. Returns the outcome: mastered, needs_review, turn_limit or error."""
    result = tutor.start(student_id)
    add_turn(record, None, result)
    turns, failures = 0, 0
    while turns < max_turns:
        try:
            reply = ask_student(student_llm, persona, record, board_text(result))
        except (ProviderError, BadOutputError) as error:
            failures += 1
            if failures == 2:
                record["error"] = f"the student model failed twice in a row: {error}"
                return "error"
            continue
        failures = 0
        if not persona["mistakes"]:
            reply["still_has_mistake"] = False    # nothing to hold, so no false advance is possible
        result = tutor.turn(student_id, reply["say"])
        add_turn(record, reply, result)
        turns += 1
        decision = result.decision
        if decision.concept_id == record["concept"] and decision.action in ("mastered", "needs_review"):
            return decision.action
    return "turn_limit"


def simulate(persona: dict, cur: Curriculum, settings: Settings, student_llm, tutor_llm, *,
             concept_id: str = "factorisation", max_turns: int = 30) -> dict:
    """One run on a fresh in-memory database. Returns the run record (save it with save_run)."""
    engine = db.connect("sqlite://")
    student_id = seed_student(engine, cur, persona, concept_id)
    tutor = Tutor(cur, engine, settings, llm=tutor_llm)
    record = {"persona": persona["id"], "concept": concept_id,
              "started_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
              "tutor_mode": "scripted" if tutor_llm is None else "llm",
              "models": {role: MODELS[role][0]["id"] for role in ("student", "understand", "respond")},
              "prompt_versions": PROMPT_VERSIONS, "curriculum_version": cur.version,
              "error": None, "turns": [], "student_calls": []}
    outcome = talk(record, persona, tutor, student_llm, student_id, max_turns)
    with engine.connect() as conn:
        progress = db.load_progress(conn, student_id, cur)
    record["final_progress"] = {cid: asdict(state) for cid, state in progress.items()}
    record["summary"] = summarise(record["turns"], outcome)
    return record


# ---------- saving and printing ----------
def transcript_md(record: dict) -> str:
    """The run as a readable Markdown transcript."""
    s = record["summary"]
    lines = [f"# {record['persona']} on {record['concept']} ({record['started_at']})", "",
             f"Outcome **{s['outcome']}** · {s['turns']} turns · {s['hints']} hints · {s['reteaches']} reteaches"
             f" · {s['false_advances']} false advances · tutor: {record['tutor_mode']}", ""]
    if record["error"]:
        lines += [f"Error: {record['error']}", ""]
    for turn in record["turns"]:
        if turn["student"] is not None:
            note = " (still has the mistake)" if turn["still_has_mistake"] else ""
            lines.append(f"**Student:**{note} {turn['student']}")
        decision = turn["decision"]
        lines += [f"**Tutor** ({decision['rule']} {decision['action']}): {' '.join(turn['tutor'])}", ""]
    return "\n".join(lines)


def save_run(record: dict, runs_dir: Path) -> Path:
    """Write <UTC time>-<persona>.json and a .md transcript next to it. Returns the JSON file's path."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{record['started_at']}-{record['persona']}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    path.with_suffix(".md").write_text(transcript_md(record), encoding="utf-8")
    return path


def summary_row(record: dict) -> str:
    """One row of the summary table, lined up with HEADER."""
    s = record["summary"]
    return (f"{record['persona']:<14} {record['concept']:<18} {s['turns']:>5} {s['hints']:>5} "
            f"{s['reteaches']:>9}  {s['outcome']:<12} {s['false_advances']}")


# ---------- command line ----------
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """--persona, --concept and --turns."""
    parser = argparse.ArgumentParser(prog="python -m sims", description="Simulated students talk to the tutor.")
    parser.add_argument("--persona", help="one persona id from sims/personas.yaml (default: all of them)")
    parser.add_argument("--concept", default="factorisation",
                        help="the concept to work on (default: factorisation); earlier ones start as mastered")
    parser.add_argument("--turns", type=int, default=30, help="the most student replies in one run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the chosen personas, save each run and print a summary row for it. Returns the exit code."""
    args = parse_args(argv)
    settings = load_settings()
    if not settings.openrouter_api_key:
        print("Simulations need OPENROUTER_API_KEY: the student is played by an LLM.")
        return 1
    cur = curriculum.load()
    personas = load_personas()
    if args.concept not in cur.order or (args.persona and args.persona not in personas):
        print(f"Unknown concept or persona. Concepts: {', '.join(cur.order)}. Personas: {', '.join(personas)}.")
        return 2
    student_llm = Client(settings.openrouter_api_key)      # one client per command: it holds the breakers
    tutor_llm = None if settings.scripted else Client(settings.openrouter_api_key)   # LLM_MODE=scripted: no LLM tutor
    print(HEADER)
    failed = False
    for persona_id in [args.persona] if args.persona else list(personas):
        record = simulate(personas[persona_id], cur, settings, student_llm, tutor_llm,
                          concept_id=args.concept, max_turns=args.turns)
        path = save_run(record, RUNS_DIR)
        print(summary_row(record), f"  -> {path.name}")
        failed = failed or record["error"] is not None
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
