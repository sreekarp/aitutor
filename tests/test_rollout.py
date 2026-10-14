"""Versions pinned to a session, and staged rollouts (app/rollout.py)."""
from conftest import FakeLLM

from app import config, db, rollout
from app.config import MODELS, PROMPT_VERSIONS, Settings
from app.tutor import Tutor

SETTINGS = Settings(openrouter_api_key="test-key", access_code="test", session_secret="test",
                    database_url="sqlite://", llm_mode="auto", max_turns_per_day=1000, tts_voice="sarah",
                    stt_language="en", https_only=False)
OLD = {"understand": "understand.v3", "respond": "respond.v6"}
NEW_MODELS = {**MODELS, "respond": [{"id": "test/new-model", "params": {}}]}


def test_a_cohort_is_stable_and_a_new_rollout_draws_a_new_share():
    students = [f"student{n}" for n in range(400)]
    first = [rollout.cohort("respond-v8", s) for s in students]
    assert first == [rollout.cohort("respond-v8", s) for s in students] and all(0 <= c < 100 for c in first)
    assert 0.05 < sum(c < 10 for c in first) / len(students) < 0.15          # about 10% under 10
    assert first != [rollout.cohort("respond-v9", s) for s in students]


def test_a_new_session_gets_the_live_versions_or_its_rollout_share(monkeypatch):
    assert rollout.choose("s1") == (PROMPT_VERSIONS, MODELS)
    trial = {"id": "trial", "percent": 100, "prompt_versions": OLD, "models": NEW_MODELS}
    monkeypatch.setattr(config, "ROLLOUT", trial)
    assert rollout.choose("s1") == (OLD, NEW_MODELS)
    monkeypatch.setattr(config, "ROLLOUT", {**trial, "percent": 0})
    assert rollout.choose("s1") == (PROMPT_VERSIONS, MODELS)


def test_a_turn_uses_its_sessions_versions_unless_retired_missing_or_scripted(monkeypatch):
    session = {"prompt_versions": OLD, "models": NEW_MODELS}
    assert rollout.pinned(session) == (OLD, NEW_MODELS)
    live = (PROMPT_VERSIONS, MODELS)
    assert rollout.pinned({"prompt_versions": OLD, "models": {"mode": "scripted"}}) == live
    assert rollout.pinned({"prompt_versions": {**OLD, "respond": "respond.v99"}, "models": NEW_MODELS}) == live
    monkeypatch.setattr(config, "RETIRED_PROMPTS", {"respond.v6"})              # a rollback
    assert rollout.pinned(session) == live


def test_a_session_keeps_its_prompts_and_models_after_the_config_changes(cur, db_engine, monkeypatch):
    """Halfway through a session the live config moves on; the session's turns still use what it started with,
    and its turns record those versions. The next session gets the new config."""
    fake = FakeLLM(understand=[{"type": "question", "answer": "", "showed_working": False, "mistake": None,
                                "safety": False}] * 2)
    tutor = Tutor(cur, db_engine, SETTINGS, llm=fake)
    with db_engine.begin() as conn:
        student_id = db.get_or_create_student(conn, "pia", "Pia")["id"]
    monkeypatch.setitem(PROMPT_VERSIONS, "understand", "understand.v3")
    tutor.start(student_id)                                  # the session saves understand.v3
    monkeypatch.setitem(PROMPT_VERSIONS, "understand", "understand.v4")   # a deploy mid-session
    fake.replies["respond"] = [{"segments": [{"say": "Good question. It means the value of x."}]}]
    result = tutor.turn(student_id, "what does a root mean, really, in this step?")
    system = fake.asked[0][1][0]["content"]
    assert system == (config.PROMPTS_DIR / "understand.v3.md").read_text(encoding="utf-8")
    assert fake.models[0] == MODELS["understand"]
    with db_engine.connect() as conn:
        assert db.get_turn(conn, result.turn_id)["versions"]["understand"] == "understand.v3"
