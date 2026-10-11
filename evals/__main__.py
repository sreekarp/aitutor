"""uv run python -m evals (spec §12): Layer 1 runs the tests; Layer 2 runs tutor scenarios with the real LLM."""
import subprocess
import sys

from app.config import ROOT, load_settings


def main() -> int:
    """Run Layer 1 (pytest), then Layer 2 when an OpenRouter key is set; returns the exit code."""
    print("Layer 1: engine, maths, grading, lessons and scripted mode (pytest)", flush=True)
    code = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT).returncode
    if not load_settings().openrouter_api_key:
        print("Layer 2 needs OPENROUTER_API_KEY")
        return code
    print("Layer 2 is added in Task 16")
    return code


if __name__ == "__main__":
    sys.exit(main())
