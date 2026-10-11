"""Shared test fixtures and helpers. Task 4 starts this file; later tasks only append to it."""
import pytest

from app import curriculum, db
from app.llm import BadOutputError
from app.models import ConceptState, Curriculum, Progress


@pytest.fixture(scope="session")
def cur() -> Curriculum:
    """The real lesson files, loaded and validated once per test run.

    Every test shares this object: copy it first (cur.model_copy(deep=True)) if a test changes it."""
    return curriculum.load()


def fresh_progress(cur: Curriculum) -> Progress:
    """Progress for a brand-new student: every concept not started. Use: from conftest import fresh_progress"""
    return {concept_id: ConceptState(concept_id=concept_id) for concept_id in cur.order}


@pytest.fixture
def db_engine():
    """A fresh, empty in-memory database for each test (Task 11)."""
    engine = db.connect("sqlite://")
    yield engine
    engine.dispose()


class FakeLLM:
    """Stands in for llm.Client (Tasks 15-16): chat_json's replies come from a queue per role, no network.

    A queued dict is the model's JSON and goes through `validate` like the real client's: a rejected
    output gets one retry with the next queued reply. A queued exception (e.g. ProviderError) is recorded
    as a provider error and raised as it is. Use: from conftest import FakeLLM"""

    def __init__(self, **replies: list):
        self.replies = {role: list(queue) for role, queue in replies.items()}
        self.asked: list[tuple[str, list[dict]]] = []          # (role, messages) for every call made

    def chat_json(self, role, messages, schema_name, schema, validate, *, deadline, calls):
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
