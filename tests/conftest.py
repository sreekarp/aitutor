"""Shared test fixtures and helpers."""
import pytest

from app import curriculum, db, grading
from app.config import POINTS_TO_MASTER, Settings
from app.llm import BadOutputError
from app.models import BoardCommand, Check, ConceptState, Curriculum, Progress, Segment


@pytest.fixture(scope="session")
def cur() -> Curriculum:
    """The real lesson files, loaded and validated once per test run.

    Every test shares this object: copy it first (cur.model_copy(deep=True)) if a test changes it."""
    return curriculum.load()


def fresh_progress(cur: Curriculum) -> Progress:
    """Progress for a brand-new student: every concept not started. Use: from conftest import fresh_progress"""
    return {concept_id: ConceptState(concept_id=concept_id) for concept_id in cur.order}


def check_of(cur: Curriculum, check_id: str) -> Check:
    """A check from the real lesson files, by id. Use: from conftest import check_of"""
    return next(c for concept in cur.concepts.values() for c in concept.checks if c.id == check_id)


@pytest.fixture
def db_engine():
    """A fresh, empty in-memory database for each test."""
    engine = db.connect("sqlite://")
    yield engine
    engine.dispose()


class FakeLLM:
    """Stands in for llm.Client: chat_json's replies come from a queue per role, no network.

    A queued dict is the model's JSON and goes through `validate` like the real client's: a rejected
    output gets one retry with the next queued reply. A queued exception (e.g. ProviderError) is recorded
    as a provider error and raised as it is. Use: from conftest import FakeLLM"""

    def __init__(self, **replies: list):
        self.replies = {role: list(queue) for role, queue in replies.items()}
        self.asked: list[tuple[str, list[dict]]] = []          # (role, messages) for every call made
        self.models: list = []                                   # the `models` passed with every call (None: live)

    def chat_json(self, role, messages, schema_name, schema, validate, *, deadline, calls, models=None):
        self.models.append(models)
        for attempt in (1, 2):
            self.asked.append((role, messages))
            reply = self.replies[role].pop(0)
            failed = isinstance(reply, Exception)
            calls.append({"role": role, "model": "fake", "ms": 0, "outcome": "provider_error" if failed else "ok",
                          "retried": attempt == 2, "backup_used": False, "tokens": None, "cost": None})
            if failed:
                raise reply
            try:
                return validate(reply)
            except ValueError as err:
                calls[-1]["outcome"] = "bad_output"
                if attempt == 2:
                    raise BadOutputError(str(err)) from None


# ---------- whole-turn tests ----------
# (use: from conftest import new_student, progress_of, said_with_working, new_question)
def new_student(db_engine, cur, mastered=None):
    """Asha, with the concepts before factorisation mastered (or the ones given), so factorisation is current."""
    with db_engine.begin() as conn:
        student_id = db.get_or_create_student(conn, "asha", "Asha")["id"]
        progress = db.load_progress(conn, student_id, cur)
        for concept_id in cur.order[:cur.order.index("factorisation")] if mastered is None else mastered:
            progress[concept_id].status, progress[concept_id].points = "mastered", POINTS_TO_MASTER
        db.save_progress(conn, student_id, progress, cur.version)
    return student_id


def progress_of(db_engine, cur, student_id):
    """The student's progress, as saved."""
    with db_engine.connect() as conn:
        return db.load_progress(conn, student_id, cur)


def said_with_working(check):
    """The right answer, said first (scripted mode reads values before "because"), then the working."""
    return " and ".join(str(value) for value in grading.answer_key(check).values) + " because I factorised it"


def new_question(check, formulas) -> Segment:
    """How a NEW question is asked: its own segment clears the board first, then writes the question."""
    asked = curriculum.ask_segment(check, formulas)
    return Segment(say=asked.say, board=[BoardCommand(op="clear"), *asked.board])


# ---------- web tests ----------
def make_settings(**changes) -> Settings:
    """Settings for web tests: scripted mode, in-memory database. Change fields by name, e.g. https_only=True."""
    values = dict(openrouter_api_key="", access_code="letmein", session_secret="test-secret",
                  database_url="sqlite://", llm_mode="scripted", max_turns_per_day=2000, tts_voice="sarah",
                  stt_language="en", https_only=False)
    return Settings(**{**values, **changes})


def sign_in(client, username: str = "asha", first_name: str = "Asha"):
    """Sign in through the real route; the TestClient keeps the cookie for later requests."""
    return client.post("/api/login", json={"username": username, "first_name": first_name, "access_code": "letmein"})
