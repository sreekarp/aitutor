"""Voice: the audio turn route, speech-to-text failures and the text-to-speech cache (spec §7.2, §8.1-§8.2)."""
import hashlib

import pytest
from conftest import make_settings, sign_in
from fastapi.testclient import TestClient

from app import db
from app.config import MODELS
from app.llm import ProviderError
from app.main import create_app
from app.tutor import DIDNT_CATCH, Tutor

VOICE_ON = make_settings(openrouter_api_key="test-key", llm_mode="auto")   # a (fake) key switches voice on
SPEECH = b"\x1a\x45\xdf\xa3" + bytes(4000)   # starts like a webm file, and is over MIN_AUDIO_BYTES
MP3 = b"ID3" + bytes(3000)                   # what the fake text-to-speech returns


class FakeVoice:
    """Stands in for llm.Client: canned speech-to-text and text-to-speech, no network. Chat is always down."""

    def __init__(self, heard: str = "ok", stt_fails: bool = False, tts_fails: bool = False):
        self.heard, self.stt_fails, self.tts_fails = heard, stt_fails, tts_fails
        self.stt_calls: list[tuple[str, str]] = []   # (mime, language) for each call
        self.tts_calls: list[tuple[str, str]] = []   # (text, voice) for each call

    def transcribe(self, audio, mime, language, *, deadline, calls):
        self.stt_calls.append((mime, language))
        calls.append({"role": "stt", "model": "fake", "ms": 5, "outcome": "provider_error" if self.stt_fails else "ok",
                      "retried": False, "backup_used": False, "tokens": None, "cost": None})
        if self.stt_fails:
            raise ProviderError("speech-to-text is down")
        return self.heard

    def speak(self, text, voice, *, deadline, calls):
        self.tts_calls.append((text, voice))
        if self.tts_fails:
            raise ProviderError("text-to-speech is down")
        return MP3

    def chat_json(self, role, messages, schema_name, schema, validate, *, deadline, calls):
        raise ProviderError("chat is down in these tests, so the tutor uses its scripted words")


def voice_client(cur, fake: FakeVoice) -> TestClient:
    """A signed-in browser stand-in on an app with voice switched on."""
    client = TestClient(create_app(VOICE_ON, llm=fake, cur=cur))
    sign_in(client)
    return client


def upload(client: TestClient, audio: bytes = SPEECH, mime: str = "audio/webm", **form):
    """POST a recording the way the page does: multipart, with the audio as a file part."""
    return client.post("/api/turn/audio", files={"audio": ("recording", audio, mime)}, data=form)


@pytest.fixture
def tts_cache(tmp_path, monkeypatch):
    """Keep the text-to-speech cache in a temporary folder for one test."""
    monkeypatch.setattr("app.tutor.TTS_CACHE_DIR", tmp_path)
    return tmp_path


def test_turn_audio_transcribes_then_runs_turn(cur, db_engine):
    fake = FakeVoice(heard="ok")
    tutor = Tutor(cur, db_engine, VOICE_ON, fake)
    with db_engine.begin() as conn:
        student_id = db.get_or_create_student(conn, "asha", "Asha")["id"]
    tutor.start(student_id)
    result = tutor.turn_audio(student_id, SPEECH, "audio/webm;codecs=opus", prev_first_audio_ms=850)
    assert fake.stt_calls == [("audio/webm;codecs=opus", "en")]   # llm.py turns the MIME type into "webm"
    assert (result.transcript, result.decision.rule) == ("ok", "R0")   # the same turn as typing "ok"
    assert result.calls[0]["role"] == "stt"           # the speech-to-text call is part of the turn's record
    with db_engine.connect() as conn:
        saved = db.get_turn(conn, result.turn_id)
    assert saved["input"] == "voice"
    assert saved["latency_ms"]["prev_first_audio_ms"] == 850
    assert "stt" in saved["latency_ms"] and "total" in saved["latency_ms"]


def test_audio_route_runs_a_turn(cur):
    fake = FakeVoice(heard="ok")
    client = voice_client(cur, fake)
    client.post("/api/session")
    r = upload(client, mime="audio/webm;codecs=opus", prev_first_audio_ms="850")
    assert r.status_code == 200
    assert (r.json()["transcript"], r.json()["decision"]["rule"]) == ("ok", "R0")
    assert fake.stt_calls == [("audio/webm;codecs=opus", "en")]


def test_stt_failure_asks_to_type(cur):
    client = voice_client(cur, FakeVoice(stt_fails=True))
    client.post("/api/session")
    body = upload(client).json()
    assert (body["transcript"], body["decision"]["rule"], body["input_hint"]) == ("", "R8", "type")
    assert body["segments"][0]["say"] == DIDNT_CATCH


def test_tiny_recording_is_not_sent(cur):
    fake = FakeVoice()
    client = voice_client(cur, fake)
    client.post("/api/session")
    body = upload(client, audio=SPEECH[:100]).json()      # a click with no speech in it
    assert (body["decision"]["rule"], body["input_hint"]) == ("R8", "type")
    assert fake.stt_calls == []                           # no call spent, and no breaker failure counted


def test_audio_over_2_mb_is_413(cur):
    fake = FakeVoice()
    client = voice_client(cur, fake)
    client.post("/api/session")
    assert upload(client, audio=bytes(2 * 1024 * 1024 + 1)).status_code == 413
    assert fake.stt_calls == []


def test_no_speech_to_text_before_start(cur):
    fake = FakeVoice()
    client = voice_client(cur, fake)                      # signed in, but Start not pressed yet
    assert upload(client).status_code == 409
    assert fake.stt_calls == []                           # no paid call for a turn that can't happen


def test_no_speech_to_text_past_the_daily_limit(cur):
    fake = FakeVoice()
    settings = make_settings(openrouter_api_key="test-key", llm_mode="auto", max_turns_per_day=2)
    client = TestClient(create_app(settings, llm=fake, cur=cur))
    sign_in(client)
    client.post("/api/session")                           # turn 1 of a 2-turn day
    client.post("/api/turn", json={"text": "ok"})         # turn 2
    body = upload(client).json()
    assert body["decision"]["rule"] == "LIMIT"
    assert fake.stt_calls == []                           # the daily limit caps speech-to-text spending too
    assert body["input_hint"] is None                     # nothing to retype: the day's turns are used up


def test_voice_routes_require_sign_in(cur):
    client = TestClient(create_app(VOICE_ON, llm=FakeVoice(), cur=cur))
    assert upload(client).status_code == 401
    assert client.get("/api/tts/some-turn/0").status_code == 401


def test_tts_cached_after_first_call(cur, tts_cache):
    fake = FakeVoice()
    client = voice_client(cur, fake)
    start = client.post("/api/session").json()
    first = client.get(f"/api/tts/{start['turn_id']}/0")
    second = client.get(f"/api/tts/{start['turn_id']}/0")
    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "audio/mpeg"
    assert first.content == second.content == MP3
    say = start["segments"][0]["say"]
    assert fake.tts_calls == [(say, "sarah")]             # the second answer came from the cache
    name = hashlib.sha256(f"{MODELS['tts'][0]['id']}|sarah|{say}".encode()).hexdigest() + ".mp3"
    assert [path.name for path in tts_cache.iterdir()] == [name]


def test_tts_failure_is_503_and_not_cached(cur, tts_cache):
    fake = FakeVoice(tts_fails=True)
    client = voice_client(cur, fake)
    turn_id = client.post("/api/session").json()["turn_id"]
    assert client.get(f"/api/tts/{turn_id}/0").status_code == 503
    assert list(tts_cache.iterdir()) == []
    fake.tts_fails = False                     # the provider is back, and no failure was cached
    assert client.get(f"/api/tts/{turn_id}/0").status_code == 200
    assert len(fake.tts_calls) == 2


def test_tts_other_students_turn_404(cur, tts_cache):
    asha = voice_client(cur, FakeVoice())
    turn_id = asha.post("/api/session").json()["turn_id"]
    ravi = TestClient(asha.app)                # the same app and database, with its own cookie jar
    sign_in(ravi, "ravi", "Ravi")
    assert ravi.get(f"/api/tts/{turn_id}/0").status_code == 404
    assert asha.get(f"/api/tts/{turn_id}/0").status_code == 200     # the owner does get it
    assert asha.get(f"/api/tts/{turn_id}/99").status_code == 404    # no such segment
    assert asha.get(f"/api/tts/{turn_id}/-1").status_code == 404    # never Python's "last item"
    assert asha.get("/api/tts/no-such-turn/0").status_code == 404


def test_audio_route_in_scripted_mode_503(cur, tts_cache):
    client = TestClient(create_app(make_settings(), cur=cur))     # no key: scripted mode
    sign_in(client)
    turn_id = client.post("/api/session").json()["turn_id"]
    r = upload(client)
    assert r.status_code == 503
    assert r.json()["detail"] == "Voice needs the API key; type instead."
    assert client.get(f"/api/tts/{turn_id}/0").status_code == 503   # the page uses the browser voice
    assert list(tts_cache.iterdir()) == []
