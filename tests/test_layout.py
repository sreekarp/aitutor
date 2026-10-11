"""The code layout rules: every code file has one job and stays small, about 250 lines (300 at most), and
nothing points a reader at notes that aren't in this repo."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_LINES = 300
# Section marks, build-task numbers and the private folders of design notes and source material.
OUTSIDE = re.compile(r"§|\bTasks? \d+|proposal/|_notes/|your prototype", re.IGNORECASE)


def test_code_files_stay_small():
    files = [*ROOT.glob("app/*.py"), *ROOT.glob("evals/*.py"), *ROOT.glob("sims/*.py"), *ROOT.glob("web/*.js")]
    sizes = {path.relative_to(ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines()) for path in files}
    assert {name: lines for name, lines in sizes.items() if lines > MAX_LINES} == {}


def test_nothing_points_at_notes_outside_the_repo():
    patterns = ("app/**/*.py", "app/prompts/*.md", "evals/*", "sims/*", "web/*", "tests/*.py", "curriculum/**/*.yaml",
                "README.md", "DESIGN.md", ".env.example", "Dockerfile", "pyproject.toml")
    files = [path for pattern in patterns for path in ROOT.glob(pattern)
             if path.is_file() and path.name != "test_layout.py"]       # this file names what it looks for
    hits = [f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()[:80]}" for path in files
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if OUTSIDE.search(line)]
    assert hits == []
