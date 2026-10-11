"""README.md and DESIGN.md match what's built: every command is in the README, and DESIGN.md shows the real tables."""
import re

from app import db
from app.config import ROOT


def read(name: str) -> str:
    """The text of a file in the repo root."""
    return (ROOT / name).read_text(encoding="utf-8")


def test_readme_lists_every_command():
    readme = read("README.md")
    for command in ("uv sync", "uv run uvicorn app.main:app", "uv run python -m app.cli", "uv run pytest",
                    "uv run python -m evals", "uv run python -m sims",
                    "uv run python -m evals --prompt-respond respond.broken --only begs_for_answer"):
        assert command in readme, command


def test_design_has_the_six_sections():
    assert re.findall(r"^## (.+)$", read("DESIGN.md"), re.M) == [
        "1. Data model", "2. Iterability", "3. Observability", "4. Latency", "5. Metrics", "6. What's ugly"]


def test_design_data_model_is_the_real_one():
    tables = dict(re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", read("DESIGN.md"), re.S))
    assert list(tables) == ["students", "sessions", "progress", "turns", "decisions"]
    for name, body in tables.items():
        documented = re.findall(r"^  ([a-z_]+) ", body, re.M)     # a column line starts with its name
        assert documented == [column.name for column in db.metadata.tables[name].columns], name
