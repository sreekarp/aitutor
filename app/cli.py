"""Type to the tutor in a terminal: uv run python -m app.cli [--username NAME].

The same text path as the web page. It uses its own database, data/cli.db, unless DATABASE_URL is set."""
from __future__ import annotations

import argparse
import os

from app import curriculum, db
from app.config import DATA_DIR, load_settings
from app.models import TurnResult
from app.tutor import Tutor

CLI_DATABASE = f"sqlite:///{(DATA_DIR / 'cli.db').as_posix()}"


def main(argv: list[str] | None = None) -> None:
    """Sign in as --username, press Start, then send each typed line as a turn until "quit"."""
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Type to the tutor in a terminal.")
    parser.add_argument("--username", default="student", help="who you are; a new name is a new student")
    args = parser.parse_args(argv)
    settings = load_settings()                       # also reads .env, which may set DATABASE_URL
    db_engine = db.connect(os.environ.get("DATABASE_URL") or CLI_DATABASE)
    tutor = Tutor(curriculum.load(), db_engine, settings)
    with db_engine.begin() as conn:
        student = db.get_or_create_student(conn, args.username, args.username.capitalize())
    print('Type your replies, or "quit" to stop.')
    show(tutor.start(student["id"]))
    while True:
        try:
            text = input("you: ")
        except (EOFError, KeyboardInterrupt):        # Ctrl+Z / Ctrl+D, or Ctrl+C
            break
        if text.strip().lower() == "quit":
            break
        show(tutor.turn(student["id"], text))


def show(result: TurnResult) -> None:
    """Print the tutor's words, the rule behind them, and a compact view of the whiteboard."""
    for segment in result.segments:
        if segment.say:
            print(f"tutor: {segment.say}")
    print(f"  why: {result.decision.rule} ({result.decision.action}) {result.decision.reason}")
    print("  board:" + ("" if result.board else " (empty)"))
    for line in result.board:
        print(f"    {line.id}: {line.text or line.latex}")


if __name__ == "__main__":
    main()
