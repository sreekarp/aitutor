"""uv run python -m evals (spec §12): Layer 1 runs the tests; Layer 2 runs tutor scenarios with the real LLM.

Flags (Layer 2): --runs N, --only ID, --model-understand ID, --model-respond ID, --prompt-respond NAME.
"""
import subprocess
import sys

from app.config import ROOT, load_settings
from evals import layer2


def main(argv: list[str] | None = None) -> int:
    """Run Layer 1 (pytest), then Layer 2 when an OpenRouter key is set; returns 1 if either failed."""
    args = layer2.parse_args(argv)                  # bad flags stop here, before anything runs
    print("Layer 1: engine, maths, grading, lessons and scripted mode (pytest)", flush=True)
    code = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT).returncode
    settings = load_settings()
    if not settings.openrouter_api_key:
        print("Layer 2 needs OPENROUTER_API_KEY")
        return code
    return layer2.run(args, settings=settings) or code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
