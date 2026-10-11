"""Voice: a recording becomes the same turn as a typed reply through speech-to-text, and each
segment of a reply becomes MP3 through text-to-speech, cached on disk. In scripted mode the routes answer 503 first."""
from __future__ import annotations

import hashlib
import json
import logging
import time

from app import db
from app.config import MIN_AUDIO_BYTES, MODELS, TTS_CACHE_DIR, TTS_TIMEOUT_S, TURN_DEADLINE_S
from app.llm import ProviderError
from app.models import TurnResult
from app.tutor import Tutor

log = logging.getLogger(__name__)


class NotFound(Exception):
    """No such turn, or no such segment in it, for this student (the web route answers 404)."""


def turn_audio(tutor: Tutor, student_id: str, audio: bytes, mime: str, *,
               prev_first_audio_ms: int | None = None, mic_open_ms: int | None = None,
               recording_ms: int | None = None) -> TurnResult:
    """Speech-to-text, then the same turn as a typed reply. No transcript: "Say it again, or type it"."""
    _, over_limit = tutor.open_session(student_id)   # before Start, or past the daily limit: no paid call
    started, calls, text = time.monotonic(), [], ""
    # A click with no speech isn't sent: it would fail and count towards the speech-to-text breaker.
    if len(audio) >= MIN_AUDIO_BYTES and not over_limit:
        try:                                  # each attempt gets STT_TIMEOUT_S, all inside the turn's deadline
            text = tutor.llm.transcribe(audio, mime, tutor.settings.stt_language,
                                        deadline=started + TURN_DEADLINE_S, calls=calls)
        except ProviderError:
            text = ""                         # failed or empty: R8 asks the student to say it again or type
    stt_ms = round((time.monotonic() - started) * 1000)
    # The browser's mic times (Speak click -> recording started, then started -> Stop) are saved next to "stt"
    pre_latency = {"mic_open_ms": mic_open_ms, "recording_ms": recording_ms, "stt": stt_ms}
    return tutor.turn(student_id, text, input_mode="voice", prev_first_audio_ms=prev_first_audio_ms,
                      stt_failed=not text and not over_limit, pre_calls=calls, pre_latency=pre_latency)


def speech(tutor: Tutor, student_id: str, turn_id: str, index: int) -> bytes | None:
    """MP3 of segment `index` of the student's own turn, cached on disk. None: the browser voice reads it."""
    with tutor.db_engine.connect() as conn:
        turn = db.get_turn(conn, turn_id) if db.turn_owner(conn, turn_id) == student_id else None
    segments = turn["tutor_output"]["segments"] if turn else []
    if not 0 <= index < len(segments):        # someone else's turn, no such turn, or no such segment
        raise NotFound(f"turn {turn_id} has no segment {index} for this student")
    text, voice = segments[index]["say"], tutor.settings.tts_voice
    if tutor.scripted or not text.strip():
        return None
    key = f"{MODELS['tts'][0]['id']}|{voice}|{text}"
    cache_file = TTS_CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()}.mp3"
    if cache_file.exists():
        return cache_file.read_bytes()
    started, calls = time.monotonic(), []
    try:
        audio = tutor.llm.speak(text, voice, deadline=started + TTS_TIMEOUT_S, calls=calls)
    except ProviderError:
        audio = None                          # not cached, so the next request tries again
    ms = round((time.monotonic() - started) * 1000)
    log.info(json.dumps({"tts_turn_id": turn_id, "segment": index, "ms": ms, "calls": calls}))
    if audio:                                 # speak() only returns complete audio (an audio type, over 1 KB)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(audio)
    return audio
