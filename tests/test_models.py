"""Settings (app/config.py) and the shared data shapes (app/models.py)."""
import json
import os
from dataclasses import asdict

import pytest
from pydantic import ValidationError

from app import config
from app.config import load_settings
from app.models import (AnswerKey, BoardLine, ConceptState, Decision, Grade, Pending, Reply,
                        Understood)

SETTING_NAMES = ["OPENROUTER_API_KEY", "ACCESS_CODE", "SESSION_SECRET", "DATABASE_URL", "LLM_MODE",
                 "MAX_TURNS_PER_DAY", "TTS_VOICE", "STT_LANGUAGE", "HTTPS_ONLY"]


@pytest.fixture
def no_settings(monkeypatch, tmp_path):
    """A throwaway copy of the environment without our settings, and a project folder with no .env.

    load_settings() writes .env values into os.environ, so each test gets its own copy to spoil."""
    environment = {k: v for k, v in os.environ.items() if k not in SETTING_NAMES}
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return tmp_path


def test_settings_defaults(no_settings):
    s = load_settings()
    assert s.openrouter_api_key == ""
    assert s.access_code == "letmein"
    assert s.session_secret == "dev-secret-change-me"
    assert s.database_url == "sqlite:///data/tutor.db"
    assert s.llm_mode == "auto"
    assert s.max_turns_per_day == 2000
    assert s.tts_voice == "sarah"
    assert s.stt_language == "en"
    assert s.https_only is False


def test_settings_scripted_without_key(no_settings):
    assert load_settings().scripted                    # no key: scripted, even in "auto"
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    assert not load_settings().scripted                # a key and "auto": the real LLM
    os.environ["LLM_MODE"] = "scripted"
    assert load_settings().scripted                    # "scripted" wins over a key


def test_env_file_values_do_not_override_environment(no_settings):
    (no_settings / ".env").write_text(
        '# a comment\n\nACCESS_CODE=from-file\nTTS_VOICE="Puck"\nMAX_TURNS_PER_DAY = 50\n',
        encoding="utf-8")
    os.environ["ACCESS_CODE"] = "from-environment"
    s = load_settings()
    assert s.access_code == "from-environment"         # the environment wins
    assert s.tts_voice == "Puck"                       # quotes are removed
    assert s.max_turns_per_day == 50                   # spaces around "=" are fine


def test_reply_rejects_four_segments():
    one = {"say": "Let's look at this line.", "board": []}
    assert len(Reply(segments=[one] * 3).segments) == 3
    with pytest.raises(ValidationError):
        Reply(segments=[one] * 4)
    with pytest.raises(ValidationError):
        Reply(segments=[])


def test_understood_rejects_unknown_type():
    understood = Understood(type="answer", answer="-2, 3")
    assert understood.showed_working is False and understood.mistake is None
    with pytest.raises(ValidationError):
        Understood(type="guess")
    with pytest.raises(ValidationError):
        Understood(type="answer", confidence=0.9)      # unknown fields are refused too


def test_models_round_trip_dataclasses():
    """asdict -> JSON -> back again, the way db.py stores progress, grades and decisions."""
    state = ConceptState("factorisation", status="checking", step_id="zero_product", points=1.5,
                         pending=Pending("fa2", hint_level=1, last_answer="-2, 3"),
                         last_check_id="fa2", reteach_count=1, strategies_tried=["worked_example"])
    data = json.loads(json.dumps(asdict(state)))
    assert ConceptState(**{**data, "pending": Pending(**data["pending"])}) == state

    fresh = ConceptState("roots")
    data = json.loads(json.dumps(asdict(fresh)))
    assert data["pending"] is None and ConceptState(**data) == fresh

    for obj in [Decision(rule="R3", action="reteach", concept_id="factorisation",
                         reason="Matches known mistake sign_flip.", evidence={"key": ["2", "3"]},
                         reteach_count=1, strategy="worked_example", mistake="sign_flip",
                         ask_check_id="fa3"),
                Grade(verdict="partial", student="3", detail="missing", trap="divided_by_x"),
                AnswerKey(kind="roots", values=[2, 3], all_roots=[2, 3]),
                BoardLine(id="q-eq", latex="x^{2}-5x+6=0")]:
        data = json.loads(json.dumps(asdict(obj)))
        assert type(obj)(**data) == obj
