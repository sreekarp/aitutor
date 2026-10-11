"""app/llm.py against a fake OpenRouter (httpx.MockTransport) and a fake clock. No network."""
import base64
import json

import httpx
import pytest

from app.config import MODELS, TTS_INSTRUCTIONS
from app.llm import BadOutputError, Client, ProviderError

PRIMARY = MODELS["understand"][0]["id"]
BACKUP = MODELS["understand"][1]["id"]
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}
MESSAGES = [{"role": "system", "content": "Read the reply."}, {"role": "user", "content": "2 and 3"}]
GOOD = '{"answer": "2, 3"}'
MP3 = b"\xff\xfb" + b"\x00" * 2000


class Clock:
    """A fake time.monotonic that only moves when a test moves it."""
    t = 1000.0

    def __call__(self):
        return self.t


def chat_reply(content):
    """A 200 chat response whose message content is `content`."""
    return httpx.Response(200, json={
        "model": PRIMARY, "usage": {"total_tokens": 120, "cost": 0.0003},
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})


def error(status):
    """An OpenRouter error response with this HTTP status."""
    return httpx.Response(status, json={"error": {"code": status, "message": "provider trouble"}})


def make_client(handler, clock=None):
    """A Client whose requests go to handler(request). Returns (client, every request sent)."""
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return Client("test-key", http=http, now=clock or Clock()), seen


def sent(request) -> dict:
    return json.loads(request.content)


def ask(client, calls, validate=lambda data: data, seconds_left=15.0):
    return client.chat_json("understand", MESSAGES, "understand", SCHEMA, validate,
                            deadline=client.now() + seconds_left, calls=calls)


def primary_fails_with(problem):
    """A handler where the primary model gets problem() (which may raise) and the backup answers fine."""
    def handler(request):
        return problem() if sent(request)["model"] == PRIMARY else chat_reply(GOOD)
    return handler


def raises(err):
    """A problem() that raises `err`, like a timeout or a dropped connection."""
    def problem():
        raise err
    return problem


def backup_answers(client, seen) -> list[dict]:
    """Ask once; check the primary failed as a provider problem and the backup answered."""
    calls = []
    assert ask(client, calls) == {"answer": "2, 3"}
    assert [sent(r)["model"] for r in seen] == [PRIMARY, BACKUP]
    assert [c["outcome"] for c in calls] == ["provider_error", "ok"]
    return calls


def test_chat_ok_records_call_and_cost():
    client, seen = make_client(lambda request: chat_reply(GOOD))
    calls = []
    assert ask(client, calls) == {"answer": "2, 3"}
    assert len(seen) == 1
    assert calls == [{"role": "understand", "model": PRIMARY, "ms": 0, "outcome": "ok", "retried": False,
                      "backup_used": False, "tokens": 120, "cost": 0.0003}]


@pytest.mark.parametrize("err", [httpx.ReadTimeout("timed out"), httpx.ConnectError("connection refused")],
                         ids=["timeout", "connection_error"])
def test_timeout_goes_to_backup(err):
    client, seen = make_client(primary_fails_with(raises(err)))
    calls = backup_answers(client, seen)
    assert sent(seen[1])["reasoning"] == {"effort": "none"}       # the backup's own params
    assert [c["backup_used"] for c in calls] == [False, True]


@pytest.mark.parametrize("status", [400, 429, 500, 503])
def test_http_500_goes_to_backup(status):
    client, seen = make_client(primary_fails_with(lambda: error(status)))
    backup_answers(client, seen)


@pytest.mark.parametrize("body", [
    {"error": {"code": 502, "message": "Provider returned error"}},                   # error with HTTP 200
    {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},             # empty content
    {"choices": [{"finish_reason": "length", "message": {"content": None}}]},         # reasoning ate the budget
    {"choices": []},                                                                  # no reply at all
])
def test_200_with_error_body_is_provider_problem(body):
    client, seen = make_client(primary_fails_with(lambda: httpx.Response(200, json=body)))
    backup_answers(client, seen)


def test_bad_json_retries_same_model_with_problems():
    replies = iter([chat_reply("Sure! The answer is 2 and 3."), chat_reply(GOOD)])
    client, seen = make_client(lambda request: next(replies))
    calls = []
    assert ask(client, calls) == {"answer": "2, 3"}
    assert [sent(r)["model"] for r in seen] == [PRIMARY, PRIMARY]
    retry = sent(seen[1])["messages"]
    assert retry[:2] == MESSAGES
    assert retry[2] == {"role": "assistant", "content": "Sure! The answer is 2 and 3."}
    assert retry[3] == {"role": "user", "content": "Your last reply had these problems: not valid JSON "
                                                   "(Expecting value). Reply again with valid JSON only."}
    assert [(c["outcome"], c["retried"]) for c in calls] == [("bad_output", False), ("ok", True)]


def test_validate_failure_retries_then_raises():
    client, seen = make_client(lambda request: chat_reply('{"answer": "two and three"}'))

    def validate(data):
        raise ValueError("answer 'two and three' is not a roots answer")

    calls = []
    with pytest.raises(BadOutputError, match="not a roots answer"):
        ask(client, calls, validate)
    assert [sent(r)["model"] for r in seen] == [PRIMARY, PRIMARY]
    assert "answer 'two and three' is not a roots answer" in sent(seen[1])["messages"][-1]["content"]
    assert [c["outcome"] for c in calls] == ["bad_output", "bad_output"]


def test_at_most_two_calls():
    # provider problem, then the backup's output is bad: no third call
    client, seen = make_client(lambda request: error(500) if sent(request)["model"] == PRIMARY
                               else chat_reply("not json"))
    with pytest.raises(BadOutputError):
        ask(client, [])
    assert len(seen) == 2
    # bad output, then the retry hits a provider problem: no backup after a retry
    replies = iter([chat_reply("not json"), error(500)])
    client, seen = make_client(lambda request: next(replies))
    with pytest.raises(ProviderError):
        ask(client, [])
    assert [sent(r)["model"] for r in seen] == [PRIMARY, PRIMARY]
    # both models down
    client, seen = make_client(lambda request: error(500))
    with pytest.raises(ProviderError):
        ask(client, [])
    assert len(seen) == 2


def test_deadline_respected():
    # each attempt gets the smaller of the 8 s limit and the time left
    client, seen = make_client(lambda request: chat_reply(GOOD))
    ask(client, [], seconds_left=3.0)
    assert seen[0].extensions["timeout"]["read"] == 3.0
    # no time left: no request at all
    client, seen = make_client(lambda request: chat_reply(GOOD))
    calls = []
    with pytest.raises(ProviderError, match="deadline"):
        ask(client, calls, seconds_left=0.0)
    assert seen == []
    assert {c["outcome"] for c in calls} == {"skipped"}
    # the primary uses up the time, so the backup is never called
    clock = Clock()

    def slow_failure(request):
        clock.t += 9.0
        return error(500)

    client, seen = make_client(slow_failure, clock)
    with pytest.raises(ProviderError, match="deadline"):
        ask(client, [], seconds_left=9.0)
    assert len(seen) == 1


def test_breaker_skips_model_after_three_failures_and_recovers():
    clock = Clock()
    client, seen = make_client(primary_fails_with(lambda: error(500)), clock)
    for _ in range(3):
        ask(client, [])
    assert len(seen) == 6                          # each time: primary fails, backup answers
    calls = []
    ask(client, calls)                             # the breaker is open: straight to the backup
    assert len(seen) == 7 and sent(seen[-1])["model"] == BACKUP
    assert [(c["model"], c["outcome"]) for c in calls] == [(PRIMARY, "skipped"), (BACKUP, "ok")]
    clock.t += 61                                  # 60 s later the primary is tried again
    ask(client, [])
    assert sent(seen[7])["model"] == PRIMARY
    # when every model of a role is skipped, nothing is sent
    client, seen = make_client(lambda request: error(500), clock)
    for _ in range(3):
        with pytest.raises(ProviderError):
            ask(client, [])
    with pytest.raises(ProviderError, match="skipped"):
        ask(client, [])
    assert len(seen) == 6


def test_breaker_counts_only_provider_failures_in_a_row():
    primary = iter([error(500), error(500), chat_reply(GOOD), error(500), error(500), chat_reply(GOOD)])
    client, _ = make_client(primary_fails_with(lambda: next(primary)))
    for _ in range(6):                             # the success in the middle resets the count
        calls = []
        ask(client, calls)
    assert (calls[0]["model"], calls[0]["outcome"]) == (PRIMARY, "ok")
    client, _ = make_client(lambda request: chat_reply("not json"))
    for _ in range(4):                             # bad output is not a provider problem
        calls = []
        with pytest.raises(BadOutputError):
            ask(client, calls)
    assert (calls[0]["model"], calls[0]["outcome"]) == (PRIMARY, "bad_output")


def test_request_body_has_no_temperature_and_requires_parameters():
    client, seen = make_client(lambda request: chat_reply(GOOD))
    ask(client, [])
    request, body = seen[0], sent(seen[0])
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert "temperature" not in body
    assert body["provider"] == {"require_parameters": True}
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "understand", "strict": True, "schema": SCHEMA}}
    assert body["messages"] == MESSAGES
    assert body["reasoning"] == {"enabled": False} and body["max_tokens"] == 400


@pytest.mark.parametrize("mime, audio_format", [
    ("audio/webm;codecs=opus", "webm"), ("audio/mp4", "m4a"), ("audio/ogg;codecs=opus", "ogg"),
    ("audio/wav", "wav"), ("audio/mpeg", "mp3")])
def test_transcribe_format_mapping(mime, audio_format):
    reply = {"text": " x is 2 and 3 ", "usage": {"total_tokens": 50, "cost": 0.0001}}
    client, seen = make_client(lambda request: httpx.Response(200, json=reply))
    calls = []
    assert client.transcribe(b"recording", mime, "en", deadline=client.now() + 8, calls=calls) == "x is 2 and 3"
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert sent(seen[0]) == {"model": MODELS["stt"][0]["id"], "language": "en", "input_audio": {
        "data": base64.b64encode(b"recording").decode(), "format": audio_format}}
    assert (calls[0]["role"], calls[0]["outcome"], calls[0]["cost"]) == ("stt", "ok", 0.0001)


def test_empty_transcript_is_an_error_without_backup():
    client, seen = make_client(lambda request: httpx.Response(200, json={"text": "  "}))
    with pytest.raises(ProviderError, match="empty transcript"):
        client.transcribe(b"silence", "audio/webm", "en", deadline=client.now() + 8, calls=[])
    assert len(seen) == 1
    # a provider problem, though, does try the backup speech-to-text model
    def stt_down(request):
        if sent(request)["model"] == MODELS["stt"][0]["id"]:
            return error(502)
        return httpx.Response(200, json={"text": "two and three"})

    client, seen = make_client(stt_down)
    assert client.transcribe(b"rec", "audio/webm", "en", deadline=client.now() + 8, calls=[]) == "two and three"
    assert [sent(r)["model"] for r in seen] == [m["id"] for m in MODELS["stt"]]


def test_speak_requires_mp3_and_voice():
    client, seen = make_client(lambda request: httpx.Response(
        200, content=MP3, headers={"content-type": "audio/mpeg"}))
    calls = []
    assert client.speak("Let's factorise.", "sarah", deadline=client.now() + 4, calls=calls) == MP3
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/audio/speech"
    assert sent(seen[0]) == {"model": MODELS["tts"][0]["id"], "input": "Let's factorise.", "voice": "sarah",
                             "instructions": TTS_INSTRUCTIONS, "response_format": "mp3"}
    assert seen[0].extensions["timeout"]["read"] == 4.0
    assert (calls[0]["role"], calls[0]["outcome"], calls[0]["tokens"]) == ("tts", "ok", None)


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"error": {"code": 502, "message": "down"}}),               # not audio
    httpx.Response(200, content=MP3[:100], headers={"content-type": "audio/mpeg"}),       # too small
])
def test_speak_rejects_tiny_or_non_audio(response):
    client, seen = make_client(lambda request: response)
    calls = []
    with pytest.raises(ProviderError, match="not audio"):
        client.speak("Hello.", "sarah", deadline=client.now() + 4, calls=calls)
    assert [c["outcome"] for c in calls] == ["provider_error"]
