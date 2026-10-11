"""The code layout rule: every code file has one job and stays small, about 250 lines (300 at most)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_LINES = 300


def test_code_files_stay_small():
    files = [*ROOT.glob("app/*.py"), *ROOT.glob("evals/*.py"), *ROOT.glob("sims/*.py"), *ROOT.glob("web/*.js")]
    sizes = {path.relative_to(ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines()) for path in files}
    assert {name: lines for name, lines in sizes.items() if lines > MAX_LINES} == {}
