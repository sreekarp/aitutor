"""Settings and constants. Everything tunable lives here."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURRICULUM_DIR = ROOT / "curriculum" / "ch4"
PROMPTS_DIR = ROOT / "app" / "prompts"
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
TTS_CACHE_DIR = DATA_DIR / "tts"

POINTS_TO_MASTER = 2.0          # clean answers in a row: fresh question, first try, no hint, working shown
MAX_HINTS = 3
MAX_RETEACHES = 3
BOARD_MAX_LINES = 8
SAY_MAX_WORDS = 60
MAX_SEGMENTS = 4             # RESPOND is asked for at most 3; a 4th is accepted rather than rejecting the reply
MAX_INPUT_CHARS = 500
SESSION_GAP_MIN = 30
TURN_DEADLINE_S = 15.0
STT_TIMEOUT_S = 8.0
LLM_TIMEOUT_S = 8.0
# UNDERSTAND takes 2-3 s when healthy. When its primary stalled (live, 14 Oct) the backup answered in 2-5 s, so
# waiting the full 8 s first made 10-13 s turns: it switches sooner. RESPOND writes more and keeps 8 s.
ROLE_TIMEOUT_S = {"understand": 4.0}
TTS_TIMEOUT_S = 4.0
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 60.0
MIN_AUDIO_BYTES = 1024

OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Per role: primary first, then backup. "params" are extra request-body fields for that model.
MODELS: dict[str, list[dict]] = {
    "understand": [
        {"id": "anthropic/claude-haiku-5.5", "params": {"reasoning": {"enabled": False}, "max_tokens": 400}},
        {"id": "openai/gpt-6-luna", "params": {"reasoning": {"effort": "none"}, "max_tokens": 400}},
    ],
    "respond": [
        {"id": "anthropic/claude-haiku-5.5", "params": {"reasoning": {"enabled": False}, "max_tokens": 900}},
        {"id": "google/gemini-3.8-flash", "params": {"reasoning": {"effort": "low"}, "max_tokens": 1500}},
    ],
    "student": [
        {"id": "openai/gpt-6-luna", "params": {"reasoning": {"effort": "none"}, "max_tokens": 300}},
        {"id": "deepseek/deepseek-v4.1-flash", "params": {"reasoning": {"enabled": False}, "max_tokens": 300}},
    ],
    "stt": [
        {"id": "openai/gpt-4o-mini-transcribe", "params": {}},
        {"id": "openai/whisper-large-v3-turbo", "params": {}},
    ],
    "tts": [{"id": "elevenlabs/eleven-flash-v2.5", "params": {}}],   # mp3 out; Gemini TTS is PCM-only on OpenRouter
}
TTS_INSTRUCTIONS = "Warm, patient teacher; clear, moderate pace."
PROMPT_VERSIONS = {"understand": "understand.v4", "respond": "respond.v7"}
# A staged rollout: new sessions of a stable share of students (a hash of its id and the student id, rollout.py) get
# these prompts and models instead. Every session keeps the versions it started with. None: everyone gets the above.
ROLLOUT: dict | None = None     # e.g. {"id": "respond-v8", "percent": 10, "prompt_versions": {...}, "models": MODELS}
RETIRED_PROMPTS: set[str] = set()   # a rollback: sessions pinned to one of these move to PROMPT_VERSIONS next turn


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    access_code: str
    session_secret: str
    database_url: str
    llm_mode: str            # "auto" (real LLM when a key is set) or "scripted"
    max_turns_per_day: int   # across all students together
    tts_voice: str
    stt_language: str
    https_only: bool         # mark the sign-in cookie Secure (set true when deployed)

    @property
    def scripted(self) -> bool:
        return self.llm_mode == "scripted" or not self.openrouter_api_key


def _read_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from .env into os.environ. Variables already set win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def load_settings() -> Settings:
    _read_env_file(ROOT / ".env")
    env = os.environ.get
    return Settings(
        openrouter_api_key=env("OPENROUTER_API_KEY", ""),
        access_code=env("ACCESS_CODE", "letmein"),
        session_secret=env("SESSION_SECRET", "dev-secret-change-me"),
        database_url=env("DATABASE_URL", "sqlite:///data/tutor.db"),
        llm_mode=env("LLM_MODE", "auto"),
        max_turns_per_day=int(env("MAX_TURNS_PER_DAY", "2000")),
        tts_voice=env("TTS_VOICE", "sarah"),
        stt_language=env("STT_LANGUAGE", "en"),
        https_only=env("HTTPS_ONLY", "false").lower() == "true",
    )
