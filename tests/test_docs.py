"""README.md and DESIGN.md match what's built: every command and reply button is in the README, the eval scenario
count is the real one, and DESIGN.md shows the real tables."""
import re

from conftest import fresh_progress

from app import compose, db
from app.config import ROOT
from app.models import Pending
from evals import layer2


def read(name: str) -> str:
    """The text of a file in the repo root."""
    return (ROOT / name).read_text(encoding="utf-8")


def test_readme_lists_every_command():
    readme = read("README.md")
    for command in ("uv sync", "uv run uvicorn app.main:app", "uv run python -m app.cli", "uv run pytest",
                    "uv run python -m evals", "uv run python -m sims",
                    "uv run python -m evals --prompt-respond respond.broken --only begs_for_answer"):
        assert command in readme, command


def test_readme_names_every_reply_button(cur):
    """README's walk through a lesson names, in bold, each button the page can show, as the page labels it."""
    concept = cur.concepts[cur.order[0]]
    progress = fresh_progress(cur)
    state = progress[concept.id]
    first, last = concept.steps[0].id, concept.steps[-1].id
    labels = set()
    for status, step_id, pending, last_action in [
            ("teaching", None, None, None),                             # a topic's start: the offer
            ("teaching", first, None, None),                            # on a step
            ("teaching", first, None, "example"),                       # after a solved example
            ("teaching", last, None, None),                             # the last step
            ("checking", last, Pending(concept.checks[0].id), None)]:   # a question open
        state.status, state.step_id, state.pending = status, step_id, pending
        labels |= {choice.label for choice in compose.cue_for(cur, progress, last_action)[1]}
    readme = read("README.md")
    assert len(labels) == 7 and sorted(label for label in labels if f"**{label}**" not in readme) == []


def test_docs_give_the_real_number_of_eval_scenarios():
    """Wherever the docs say "N tutor scenarios" or "N eval scenarios", N is the scenario file's count."""
    count = str(len(layer2.load_scenarios()))
    for name in ("README.md", "DESIGN.md"):
        said = re.findall(r"(\d+) (?:tutor|eval) scenarios", read(name))
        assert said and set(said) == {count}, (name, said)


def test_design_has_the_six_sections():
    assert re.findall(r"^## (.+)$", read("DESIGN.md"), re.M) == [
        "1. Data model", "2. Iterability", "3. Observability", "4. Latency", "5. Metrics", "6. What's ugly"]


def test_design_data_model_is_the_real_one():
    tables = dict(re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", read("DESIGN.md"), re.S))
    assert list(tables) == ["students", "sessions", "progress", "turns", "decisions"]
    for name, body in tables.items():
        documented = re.findall(r"^  ([a-z_]+) ", body, re.M)     # a column line starts with its name
        assert documented == [column.name for column in db.metadata.tables[name].columns], name
