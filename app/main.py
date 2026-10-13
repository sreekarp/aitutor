"""The web app: the sign-in cookie, the JSON routes the page calls, and the page's own files."""
from __future__ import annotations

import json
import logging
import queue
import secrets
import sys
import threading

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware

from app import curriculum, db, page, replies, stops, voice
from app.config import WEB_DIR, Settings, load_settings
from app.llm import Client
from app.models import Curriculum
from app.tutor import NoSession, Tutor

log = logging.getLogger(__name__)
COOKIE_MAX_AGE = 30 * 24 * 60 * 60   # the sign-in cookie lasts 30 days (in seconds)
MAX_AUDIO_BYTES = 2 * 1024 * 1024    # 2 MB: a 30-second recording is under 1 MB, and speech-to-text is paid


class LoginForm(BaseModel):
    """What the sign-in card sends. Spaces around each value are removed first."""
    model_config = ConfigDict(str_strip_whitespace=True)
    username: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_.-]+$")
    first_name: str = Field(min_length=1, max_length=30)
    access_code: str


class Jump(BaseModel):
    """A topic picked in the progress panel."""
    concept_id: str


class StopReport(BaseModel):
    """The student stopped the tutor's reply: how many of its segments had been said in full by then, and how many
    were on screen (the one being said too)."""
    heard: int = Field(ge=0)
    shown: int = Field(ge=0)


class TextTurn(BaseModel):
    """A reply typed in the text box, plus the last voice reply's stop-click-to-first-audio time."""
    text: str
    prev_first_audio_ms: int | None = None


def create_app(settings: Settings | None = None, *, llm=None, cur: Curriculum | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings, lesson files and fake LLM client."""
    settings = settings or load_settings()
    cur = cur or curriculum.load()                  # refuses to start if a lesson file is wrong
    db_engine = db.connect(settings.database_url)
    if llm is None and not settings.scripted:
        llm = Client(settings.openrouter_api_key)   # one client, so one shared httpx connection pool
    tutor = Tutor(cur, db_engine, settings, llm)

    app = FastAPI(title="AI Tutor")
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=settings.https_only,
                       max_age=COOKIE_MAX_AGE, same_site="lax")

    def signed_in(request: Request) -> str:
        """The student id from the sign-in cookie, or 401."""
        student_id = request.session.get("student_id")
        if student_id:
            with db_engine.connect() as conn:
                if db.get_student(conn, student_id):
                    return student_id
        request.session.clear()                     # e.g. the database was reset after the cookie was made
        raise HTTPException(status_code=401, detail="Please sign in.")

    @app.post("/api/login")
    def login(form: LoginForm, request: Request) -> dict:
        # compare_digest takes as long for a wrong code as for a right one, so timing gives nothing away
        if not secrets.compare_digest(form.access_code.encode(), settings.access_code.encode()):
            raise HTTPException(status_code=403, detail="That access code isn't right.")
        with db_engine.begin() as conn:
            student = db.get_or_create_student(conn, form.username, form.first_name)
        request.session["student_id"] = student["id"]   # only ever read from the signed cookie
        return {"first_name": student["first_name"]}

    @app.post("/api/logout", dependencies=[Depends(signed_in)])
    def logout(request: Request) -> dict:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/state")
    def state(student_id: str = Depends(signed_in)) -> dict:
        return page.state(tutor, student_id)

    @app.post("/api/session")
    def start_session(student_id: str = Depends(signed_in)) -> dict:
        return page.payload(tutor, student_id, tutor.start(student_id))

    @app.post("/api/jump")
    def jump(body: Jump, student_id: str = Depends(signed_in)) -> dict:
        if body.concept_id not in tutor.cur.concepts:
            raise HTTPException(status_code=404, detail="There's no such topic.")
        return page.payload(tutor, student_id, tutor.jump(student_id, body.concept_id))

    def started(student_id: str) -> None:
        """409 before the first Start: there is no session to add a turn to."""
        try:
            tutor.open_session(student_id)
        except NoSession:
            raise HTTPException(status_code=409, detail="Press Start first.") from None

    @app.post("/api/turn")
    def text_turn(body: TextTurn, student_id: str = Depends(signed_in)) -> StreamingResponse:
        started(student_id)
        return as_lines(lambda on_wait: page.payload(tutor, student_id, tutor.turn(
            student_id, body.text, prev_first_audio_ms=body.prev_first_audio_ms, on_wait=on_wait)))

    @app.post("/api/turn/{turn_id}/stopped")
    def stopped(turn_id: str, body: StopReport, student_id: str = Depends(signed_in)) -> dict:
        if not stops.record(db_engine, cur, student_id, turn_id, body.heard, body.shown):
            raise HTTPException(status_code=404, detail="No such turn.")
        return {"ok": True}

    @app.post("/api/turn/audio")
    def audio_turn(audio: UploadFile, prev_first_audio_ms: int | None = Form(None),
                   mic_open_ms: int | None = Form(None), recording_ms: int | None = Form(None),
                   student_id: str = Depends(signed_in)) -> StreamingResponse:
        if tutor.scripted:
            raise HTTPException(status_code=503, detail="Voice needs the API key; type instead.")
        data = audio.file.read(MAX_AUDIO_BYTES + 1)   # one byte more than allowed is enough to tell
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="That recording is too long.")
        started(student_id)
        return as_lines(lambda on_wait: page.payload(tutor, student_id, voice.turn_audio(
            tutor, student_id, data, audio.content_type or "", prev_first_audio_ms=prev_first_audio_ms,
            mic_open_ms=mic_open_ms, recording_ms=recording_ms, on_wait=on_wait)))

    @app.get("/api/tts/wait/{key}", dependencies=[Depends(signed_in)])
    def wait_tts(key: str) -> Response:
        try:
            audio = voice.wait_speech(tutor, key)
        except voice.NotFound:
            raise HTTPException(status_code=404, detail="No such line.") from None
        if audio is None:
            raise HTTPException(status_code=503, detail="No server voice right now; the browser voice reads it.")
        return Response(content=audio, media_type="audio/mpeg")

    @app.get("/api/tts/{turn_id}/{index}")
    def tts(turn_id: str, index: int, student_id: str = Depends(signed_in)) -> Response:
        try:
            audio = voice.speech(tutor, student_id, turn_id, index)
        except voice.NotFound:
            raise HTTPException(status_code=404, detail="No such turn.") from None
        if audio is None:
            raise HTTPException(status_code=503, detail="No server voice right now; the browser voice reads it.")
        return Response(content=audio, media_type="audio/mpeg")

    @app.middleware("http")
    async def check_for_a_new_page(request: Request, call_next):
        """The browser asks again every time (a quick 304 when nothing changed), so after a deploy a page never
        mixes old and new files."""
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        """For the host's health check and an uptime pinger: no sign-in and no database, so it stays cheap."""
        return {"ok": True}

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")   # last, so the /api routes win
    return app


def as_lines(run_turn) -> StreamingResponse:
    """A turn's JSON as lines. run_turn(on_wait) runs the turn and returns its JSON; while it runs, on_wait(key)
    (the tutor is about to ask the AI for its reply) sends {"wait": key, "say": that line} at once, so the page can
    say it during the wait. The turn's own JSON is the last line."""
    found = queue.Queue()

    def work():
        try:
            found.put(run_turn(lambda key: found.put({"wait": key, "say": replies.WAIT[key]})))
        except Exception:                             # the turn itself never raises; this is a last resort
            log.exception("a turn failed before its reply")
            found.put({"error": "Something went wrong. Please try again."})

    def lines():
        while "wait" in (item := found.get()):
            yield json.dumps(item) + "\n"
        yield json.dumps(item) + "\n"

    threading.Thread(target=work, daemon=True).start()
    return StreamingResponse(lines(), media_type="application/x-ndjson")


# The tutor's JSON log lines (one per turn) go to stdout. Other libraries stay at WARNING:
# at INFO, httpx and SQLAlchemy would add a line for every request and every SQL statement.
logging.basicConfig(format="%(message)s", stream=sys.stdout)
logging.getLogger("app").setLevel(logging.INFO)

app = create_app()   # what `uv run uvicorn app.main:app` serves
