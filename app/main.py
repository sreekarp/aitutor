"""The web app: the sign-in cookie, the JSON routes the page calls, and the page's own files (spec §8.3, §10)."""
from __future__ import annotations

import logging
import secrets
import sys

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware

from app import curriculum, db, page, voice
from app.config import WEB_DIR, Settings, load_settings
from app.llm import Client
from app.models import Curriculum
from app.tutor import NoSession, Tutor

COOKIE_MAX_AGE = 30 * 24 * 60 * 60   # the sign-in cookie lasts 30 days (in seconds)
MAX_AUDIO_BYTES = 2 * 1024 * 1024    # 2 MB: a 30-second recording is under 1 MB, and speech-to-text is paid


class LoginForm(BaseModel):
    """What the sign-in card sends. Spaces around each value are removed first."""
    model_config = ConfigDict(str_strip_whitespace=True)
    username: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_.-]+$")
    first_name: str = Field(min_length=1, max_length=30)
    access_code: str


class TextTurn(BaseModel):
    """A reply typed in the text box, plus the last voice reply's stop-click-to-first-audio time (spec §8.5)."""
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
        request.session["student_id"] = student["id"]   # only ever read from the signed cookie (spec §10)
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

    @app.post("/api/turn")
    def text_turn(body: TextTurn, student_id: str = Depends(signed_in)) -> dict:
        try:
            result = tutor.turn(student_id, body.text, prev_first_audio_ms=body.prev_first_audio_ms)
        except NoSession:
            raise HTTPException(status_code=409, detail="Press Start first.") from None
        return page.payload(tutor, student_id, result)

    @app.post("/api/turn/audio")
    def audio_turn(audio: UploadFile, prev_first_audio_ms: int | None = Form(None),
                   student_id: str = Depends(signed_in)) -> dict:
        if tutor.scripted:
            raise HTTPException(status_code=503, detail="Voice needs the API key; type instead.")
        data = audio.file.read(MAX_AUDIO_BYTES + 1)   # one byte more than allowed is enough to tell
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="That recording is too long.")
        try:
            result = voice.turn_audio(tutor, student_id, data, audio.content_type or "",
                                      prev_first_audio_ms=prev_first_audio_ms)
        except NoSession:
            raise HTTPException(status_code=409, detail="Press Start first.") from None
        return page.payload(tutor, student_id, result)

    @app.get("/api/tts/{turn_id}/{index}")
    def tts(turn_id: str, index: int, student_id: str = Depends(signed_in)) -> Response:
        try:
            audio = voice.speech(tutor, student_id, turn_id, index)
        except voice.NotFound:
            raise HTTPException(status_code=404, detail="No such turn.") from None
        if audio is None:
            raise HTTPException(status_code=503, detail="No server voice right now; the browser voice reads it.")
        return Response(content=audio, media_type="audio/mpeg")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")   # last, so the /api routes win
    return app


# The tutor's JSON log lines (one per turn, spec §11) go to stdout. Other libraries stay at WARNING:
# at INFO, httpx and SQLAlchemy would add a line for every request and every SQL statement.
logging.basicConfig(format="%(message)s", stream=sys.stdout)
logging.getLogger("app").setLevel(logging.INFO)

app = create_app()   # what `uv run uvicorn app.main:app` serves
