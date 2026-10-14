"""The 6 tables and small read/write helpers. SQLAlchemy Core: plain tables, no ORM classes.

Every helper takes an open connection, so the caller decides what goes into one transaction."""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (JSON, Column, DateTime, Engine, Float, ForeignKey, Integer, MetaData, Table, Text,
                        UniqueConstraint, create_engine, func, inspect, select, text)
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool

from app.models import BoardLine, ConceptState, Curriculum, Decision, Pending, Progress

metadata = MetaData()

students_table = Table(
    "students", metadata,
    Column("id", Text, primary_key=True),
    Column("username", Text, unique=True, nullable=False),
    Column("first_name", Text, nullable=False),
    Column("created_at", DateTime(timezone=True)),
)

sessions_table = Table(
    "sessions", metadata,
    Column("id", Text, primary_key=True),
    Column("student_id", Text, ForeignKey("students.id")),
    Column("session_no", Integer),
    Column("curriculum_version", Text),
    Column("prompt_versions", JSON),
    Column("models", JSON),
    Column("board", JSON),                       # list of BoardLine dicts
    Column("started_at", DateTime(timezone=True)),
    UniqueConstraint("student_id", "session_no"),
)

progress_table = Table(
    "progress", metadata,
    Column("student_id", Text, primary_key=True),
    Column("concept_id", Text, primary_key=True),
    Column("status", Text),
    Column("step_id", Text),
    Column("points", Float),
    Column("pending", JSON),                     # Pending as a dict, or null
    Column("last_check_id", Text),
    Column("reteach_count", Integer),
    Column("strategies_tried", JSON),
    Column("checks_seen", JSON),                 # question ids asked in this pass
    Column("curriculum_version", Text),
    Column("updated_at", DateTime(timezone=True)),
)

turns_table = Table(
    "turns", metadata,
    Column("id", Text, primary_key=True),
    Column("session_id", Text, ForeignKey("sessions.id")),
    Column("turn_no", Integer),
    Column("concept_id", Text),
    Column("phase", Text),
    Column("input", Text),                       # voice | text | start
    Column("student_text", Text),
    Column("understood", JSON),
    Column("grade", JSON),
    Column("tutor_output", JSON),                # {"segments": [...], "board_before": [...], "choices": [...]}
    Column("versions", JSON),
    Column("latency_ms", JSON),
    Column("calls", JSON),
    Column("issues", JSON),
    Column("created_at", DateTime(timezone=True)),
)

decisions_table = Table(
    "decisions", metadata,
    Column("id", Text, primary_key=True),
    Column("turn_id", Text, ForeignKey("turns.id")),
    Column("student_id", Text),
    Column("concept_id", Text),
    Column("rule", Text),
    Column("action", Text),
    Column("reason", Text),
    Column("evidence", JSON),                    # may hold more detail: never sent to the browser
    Column("hint_level", Integer),
    Column("reteach_count", Integer),
    Column("strategy", Text),
    Column("created_at", DateTime(timezone=True)),
)

stops_table = Table(                             # a reply the student stopped part-way (Stop, Esc, or answering)
    "stops", metadata,
    Column("turn_id", Text, ForeignKey("turns.id"), primary_key=True),
    Column("student_id", Text),
    Column("heard", Integer),                    # how many of its segments had been said in full
    Column("shown", Integer),                    # how many were on screen (the one being said too)
    Column("created_at", DateTime(timezone=True)),
)


def with_driver(url: str) -> str:
    """A Postgres URL as Neon or Render give it ("postgresql://…" or "postgres://…") with the installed driver,
    psycopg 3, named: "postgresql+psycopg://…". Any other URL is returned unchanged."""
    return re.sub(r"^postgres(?:ql)?://", "postgresql+psycopg://", url)


def connect(url: str) -> Engine:
    """Open the database: create the SQLite file's folder if missing, then any missing tables and columns."""
    url = with_driver(url)
    parsed = make_url(url)
    options = {}
    if parsed.get_backend_name() == "sqlite":
        if parsed.database in (None, "", ":memory:"):
            # One shared in-memory connection, usable from FastAPI's worker threads.
            options = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        else:
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    else:
        options = {"pool_pre_ping": True}   # a hosted database (Neon) closes idle connections: test before reuse
    engine = create_engine(url, **options)
    metadata.create_all(engine)
    add_columns(engine)
    return engine


def add_columns(engine: Engine) -> None:
    """create_all never changes an existing table, so a column added after a table went live is added here."""
    if "checks_seen" not in {column["name"] for column in inspect(engine).get_columns("progress")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE progress ADD COLUMN checks_seen JSON"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(row) -> dict | None:
    """A result row as a plain dict. SQLite forgets time zones, so timestamps are marked UTC again."""
    if row is None:
        return None
    data = dict(row._mapping)
    for key in ("created_at", "started_at", "updated_at"):
        if isinstance(data.get(key), datetime) and data[key].tzinfo is None:
            data[key] = data[key].replace(tzinfo=timezone.utc)
    return data


# ---------- students ----------
def get_or_create_student(conn, username: str, first_name: str) -> dict:
    """The student with this username, created if new. An existing student keeps their stored first name."""
    row = conn.execute(select(students_table).where(students_table.c.username == username)).first()
    if row is not None:
        return _as_dict(row)
    student = {"id": uuid4().hex, "username": username, "first_name": first_name, "created_at": _now()}
    conn.execute(students_table.insert().values(**student))
    return student


def get_student(conn, student_id: str) -> dict | None:
    """The student row, or None."""
    return _as_dict(conn.execute(select(students_table).where(students_table.c.id == student_id)).first())


# ---------- progress ----------
def load_progress(conn, student_id: str, cur: Curriculum) -> Progress:
    """One ConceptState per concept in teaching order; a concept with no row yet is not started."""
    rows = conn.execute(select(progress_table).where(progress_table.c.student_id == student_id)).all()
    saved = {row.concept_id: row for row in rows}
    progress = {}
    for concept_id in cur.order:
        row = saved.get(concept_id)
        if row is None:
            progress[concept_id] = ConceptState(concept_id)
        else:
            progress[concept_id] = ConceptState(
                concept_id=concept_id, status=row.status, step_id=row.step_id, points=row.points,
                pending=Pending(**row.pending) if row.pending else None, last_check_id=row.last_check_id,
                reteach_count=row.reteach_count, strategies_tried=list(row.strategies_tried or []),
                checks_seen=list(row.checks_seen or []))
    return progress


def save_progress(conn, student_id: str, progress: Progress, curriculum_version: str) -> None:
    """Write every concept's state: update its row, or insert one the first time."""
    for state in progress.values():
        values = {"status": state.status, "step_id": state.step_id, "points": state.points,
                  "pending": asdict(state.pending) if state.pending else None,
                  "last_check_id": state.last_check_id, "reteach_count": state.reteach_count,
                  "strategies_tried": list(state.strategies_tried), "checks_seen": list(state.checks_seen),
                  "curriculum_version": curriculum_version,
                  "updated_at": _now()}
        same_row = (progress_table.c.student_id == student_id) & (progress_table.c.concept_id == state.concept_id)
        if conn.execute(progress_table.update().where(same_row).values(**values)).rowcount == 0:
            conn.execute(progress_table.insert().values(student_id=student_id, concept_id=state.concept_id,
                                                        **values))


# ---------- sessions and their boards ----------
def latest_session(conn, student_id: str) -> dict | None:
    """The student's newest session, or None."""
    query = (select(sessions_table).where(sessions_table.c.student_id == student_id)
             .order_by(sessions_table.c.session_no.desc()).limit(1))
    return _as_dict(conn.execute(query).first())


def create_session(conn, student_id: str, *, curriculum_version: str, prompt_versions: dict,
                   models: dict) -> dict:
    """A new session numbered one after the student's latest, with an empty board."""
    latest = latest_session(conn, student_id)
    session = {"id": uuid4().hex, "student_id": student_id,
               "session_no": latest["session_no"] + 1 if latest else 1,
               "curriculum_version": curriculum_version, "prompt_versions": prompt_versions,
               "models": models, "board": [], "started_at": _now()}
    conn.execute(sessions_table.insert().values(**session))
    return session


def set_session_board(conn, session_id: str, board: list[BoardLine]) -> None:
    """Save the session's whiteboard."""
    conn.execute(sessions_table.update().where(sessions_table.c.id == session_id)
                 .values(board=[asdict(line) for line in board]))


def session_board(conn, session_id: str) -> list[BoardLine]:
    """The session's whiteboard as BoardLines."""
    board = conn.execute(select(sessions_table.c.board).where(sessions_table.c.id == session_id)).scalar()
    return [BoardLine(**line) for line in board or []]


# ---------- turns and decisions ----------
TURN_FIELDS = ("session_id", "concept_id", "phase", "input", "student_text", "understood", "grade",
               "tutor_output", "versions", "latency_ms", "calls", "issues")


def save_turn(conn, turn: dict, decision: Decision) -> str:
    """Insert one turn and its decision; returns the new turn id. Turns are numbered 0, 1, 2… per session."""
    previous = conn.execute(select(func.max(turns_table.c.turn_no))
                            .where(turns_table.c.session_id == turn["session_id"])).scalar()
    turn_id, now = uuid4().hex, _now()
    conn.execute(turns_table.insert().values(
        id=turn_id, turn_no=0 if previous is None else previous + 1, created_at=now,
        **{name: turn[name] for name in TURN_FIELDS}))
    conn.execute(decisions_table.insert().values(
        id=uuid4().hex, turn_id=turn_id, student_id=turn["student_id"], concept_id=decision.concept_id,
        rule=decision.rule, action=decision.action, reason=decision.reason, evidence=decision.evidence,
        hint_level=decision.hint_level, reteach_count=decision.reteach_count, strategy=decision.strategy,
        created_at=now))
    return turn_id


def get_turn(conn, turn_id: str) -> dict | None:
    """One turn row, or None."""
    return _as_dict(conn.execute(select(turns_table).where(turns_table.c.id == turn_id)).first())


def last_turn(conn, session_id: str) -> dict | None:
    """The session's newest turn, plus its decision's rule, action and reason, and "heard" and "shown" when the
    student stopped it part-way (else None); None before the first turn."""
    query = (select(turns_table, decisions_table.c.rule, decisions_table.c.action, decisions_table.c.reason,
                    stops_table.c.heard, stops_table.c.shown)
             .join(decisions_table, decisions_table.c.turn_id == turns_table.c.id)
             .outerjoin(stops_table, stops_table.c.turn_id == turns_table.c.id)
             .where(turns_table.c.session_id == session_id)
             .order_by(turns_table.c.turn_no.desc()).limit(1))
    return _as_dict(conn.execute(query).first())


def save_stop(conn, turn_id: str, student_id: str, heard: int, shown: int) -> None:
    """Keep how many of a turn's segments were said, and shown, before the student stopped it (the latest wins)."""
    conn.execute(stops_table.delete().where(stops_table.c.turn_id == turn_id))
    conn.execute(stops_table.insert().values(turn_id=turn_id, student_id=student_id, heard=heard, shown=shown,
                                             created_at=_now()))


def turn_owner(conn, turn_id: str) -> str | None:
    """The id of the student whose session holds this turn, or None."""
    query = (select(sessions_table.c.student_id)
             .join(turns_table, turns_table.c.session_id == sessions_table.c.id)
             .where(turns_table.c.id == turn_id))
    return conn.execute(query).scalar()


def recent_decisions(conn, student_id: str, limit: int = 5) -> list[dict]:
    """The student's newest decisions first: rule, action, reason, created_at (never the evidence)."""
    query = (select(decisions_table.c.rule, decisions_table.c.action, decisions_table.c.reason,
                    decisions_table.c.created_at)
             .join(turns_table, turns_table.c.id == decisions_table.c.turn_id)
             .where(decisions_table.c.student_id == student_id)
             .order_by(decisions_table.c.created_at.desc(), turns_table.c.turn_no.desc())
             .limit(limit))
    return [_as_dict(row) for row in conn.execute(query)]


def turns_today(conn) -> int:
    """Turns since midnight UTC, across all students (the daily limit)."""
    midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(select(func.count()).select_from(turns_table)
                        .where(turns_table.c.created_at >= midnight)).scalar()
