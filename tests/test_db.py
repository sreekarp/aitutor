"""Tests for app/db.py: the 5 tables and their small read/write helpers."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, inspect, select

from app import db
from app.models import BoardLine, ConceptState, Decision, Pending


def make_student(conn, username="asha"):
    return db.get_or_create_student(conn, username, username.title())


def make_session(conn, student_id):
    return db.create_session(conn, student_id, curriculum_version="2026.10.1",
                             prompt_versions={"understand": "understand.v1", "respond": "respond.v1"},
                             models={"mode": "scripted"})


def make_turn(session_id, student_id):
    return {"session_id": session_id, "student_id": student_id, "concept_id": "factorisation",
            "phase": "teaching", "input": "text", "student_text": "ok", "understood": None, "grade": None,
            "tutor_output": {"segments": [{"say": "Next step.", "board": []}]},
            "versions": {"curriculum": "2026.10.1"}, "latency_ms": {"total": 5}, "calls": [], "issues": []}


def count(conn, table):
    return conn.execute(select(func.count()).select_from(table)).scalar()


def test_connect_creates_folder(tmp_path):
    path = tmp_path / "nested" / "folder" / "tutor.db"
    engine = db.connect(f"sqlite:///{path.as_posix()}")
    try:
        assert path.parent.is_dir()
        assert set(inspect(engine).get_table_names()) == {"students", "sessions", "progress", "turns", "decisions"}
    finally:
        engine.dispose()


def test_student_get_or_create_keeps_first_name(db_engine):
    with db_engine.begin() as conn:
        first = db.get_or_create_student(conn, "asha", "Asha")
        again = db.get_or_create_student(conn, "asha", "Somebody Else")
        assert again["id"] == first["id"]
        assert again["first_name"] == "Asha"
        assert db.get_student(conn, first["id"])["username"] == "asha"
        assert db.get_student(conn, "no-such-id") is None


def test_progress_round_trip(db_engine, cur):
    with db_engine.begin() as conn:
        student_id = make_student(conn)["id"]
        progress = db.load_progress(conn, student_id, cur)
        assert list(progress) == cur.order
        assert all(state == ConceptState(cid) for cid, state in progress.items())

        state = progress["factorisation"]
        state.status, state.step_id, state.points = "checking", "zero_on_one_side", 1.5
        state.pending = Pending("fa2", hint_level=1, awaiting_working=True, followup_used=True, last_answer="2")
        state.last_check_id, state.reteach_count, state.strategies_tried = "fa2", 1, ["worked_example"]
        db.save_progress(conn, student_id, progress, cur.version)
        assert db.load_progress(conn, student_id, cur) == progress

        state.points = 2.0                      # saving again updates the same rows
        db.save_progress(conn, student_id, progress, cur.version)
        assert db.load_progress(conn, student_id, cur)["factorisation"].points == 2.0
        assert count(conn, db.progress_table) == len(cur.order)

        removed = {"old_concept": ConceptState("old_concept", status="mastered")}
        db.save_progress(conn, student_id, removed, cur.version)
        assert "old_concept" not in db.load_progress(conn, student_id, cur)   # concepts not in order are ignored


def test_session_and_turn_numbers(db_engine):
    with db_engine.begin() as conn:
        student_id = make_student(conn)["id"]
        assert db.latest_session(conn, student_id) is None
        first, second = make_session(conn, student_id), make_session(conn, student_id)
        assert (first["session_no"], second["session_no"]) == (1, 2)
        assert db.latest_session(conn, student_id)["id"] == second["id"]
        assert db.latest_session(conn, student_id)["models"] == {"mode": "scripted"}
        assert db.last_turn(conn, second["id"]) is None

        turn_ids = [db.save_turn(conn, make_turn(second["id"], student_id),
                                 Decision("R0", "next_step", "factorisation", f"step {n}")) for n in range(3)]
        db.save_turn(conn, make_turn(first["id"], student_id), Decision("S0", "greet", None, "new session"))
        assert [db.get_turn(conn, turn_id)["turn_no"] for turn_id in turn_ids] == [0, 1, 2]
        last = db.last_turn(conn, second["id"])
        assert (last["id"], last["turn_no"], last["rule"], last["action"], last["reason"]) == (
            turn_ids[2], 2, "R0", "next_step", "step 2")
        assert last["tutor_output"] == {"segments": [{"say": "Next step.", "board": []}]}
        assert last["created_at"].tzinfo is not None
        assert db.last_turn(conn, first["id"])["turn_no"] == 0      # numbering is per session


def test_session_board_round_trip(db_engine):
    with db_engine.begin() as conn:
        session = make_session(conn, make_student(conn)["id"])
        assert db.session_board(conn, session["id"]) == []
        lines = [BoardLine(id="q-label", text="Solve:"),
                 BoardLine(id="q-eq", latex="x^{2}=3x", check="Eq(x**2, 3*x)"),
                 BoardLine(id="f1", latex="D = b^{2} - 4ac", formula="discriminant")]
        db.set_session_board(conn, session["id"], lines)
        assert db.session_board(conn, session["id"]) == lines


def test_save_turn_and_decision_in_one_transaction(db_engine, cur):
    with db_engine.begin() as conn:
        student_id = make_student(conn)["id"]
        session = make_session(conn, student_id)
    with pytest.raises(RuntimeError):
        with db_engine.begin() as conn:
            progress = db.load_progress(conn, student_id, cur)
            progress["factorisation"].status = "teaching"
            db.save_progress(conn, student_id, progress, cur.version)
            db.save_turn(conn, make_turn(session["id"], student_id),
                         Decision("R0", "next_step", "factorisation", "ready"))
            raise RuntimeError("crash before the turn finished")
    with db_engine.connect() as conn:
        assert count(conn, db.turns_table) == 0
        assert count(conn, db.decisions_table) == 0
        assert db.load_progress(conn, student_id, cur)["factorisation"].status == "not_started"


def test_recent_decisions_newest_first(db_engine):
    with db_engine.begin() as conn:
        student_id = make_student(conn)["id"]
        session = make_session(conn, student_id)
        for n in range(7):
            db.save_turn(conn, make_turn(session["id"], student_id),
                         Decision(f"R{n}", "hint", "factorisation", f"reason {n}", evidence={"cause": "test"}))
        recent = db.recent_decisions(conn, student_id)
        assert [d["rule"] for d in recent] == ["R6", "R5", "R4", "R3", "R2"]
        assert set(recent[0]) == {"rule", "action", "reason", "created_at"}    # never the evidence
        assert len(db.recent_decisions(conn, student_id, limit=2)) == 2


def test_turns_today_and_owner(db_engine):
    with db_engine.begin() as conn:
        asha, ravi = make_student(conn, "asha")["id"], make_student(conn, "ravi")["id"]
        asha_session, ravi_session = make_session(conn, asha), make_session(conn, ravi)
        decision = Decision("R0", "next_step", "factorisation", "ready")
        asha_turn = db.save_turn(conn, make_turn(asha_session["id"], asha), decision)
        db.save_turn(conn, make_turn(asha_session["id"], asha), decision)
        ravi_turn = db.save_turn(conn, make_turn(ravi_session["id"], ravi), decision)
        assert db.turns_today(conn) == 3                  # every student together
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        conn.execute(db.turns_table.update().where(db.turns_table.c.id == asha_turn).values(created_at=yesterday))
        assert db.turns_today(conn) == 2
        assert db.turn_owner(conn, asha_turn) == asha
        assert db.turn_owner(conn, ravi_turn) == ravi
        assert db.turn_owner(conn, "no-such-turn") is None
        assert db.get_turn(conn, "no-such-turn") is None
