"""Shared test fixtures and helpers. Task 4 starts this file; later tasks only append to it."""
import pytest

from app import curriculum
from app.models import ConceptState, Curriculum, Progress


@pytest.fixture(scope="session")
def cur() -> Curriculum:
    """The real lesson files, loaded and validated once per test run.

    Every test shares this object: copy it first (cur.model_copy(deep=True)) if a test changes it."""
    return curriculum.load()


def fresh_progress(cur: Curriculum) -> Progress:
    """Progress for a brand-new student: every concept not started. Use: from conftest import fresh_progress"""
    return {concept_id: ConceptState(concept_id=concept_id) for concept_id in cur.order}
