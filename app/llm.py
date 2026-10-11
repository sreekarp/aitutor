"""OpenRouter client: chat that returns JSON, speech-to-text and text-to-speech (spec §7.2, §7.5).

Two failure rules. A provider problem (timeout, connection error, HTTP error, an error body or an
empty reply) tries the role's backup model once. A bad output (not JSON, or `validate` rejects it)
asks the same model once more, listing the problems. Never more than 2 calls. Every call gets the
smaller of its own time limit and the time left before the turn's deadline. A model with 3 provider
problems in a row is skipped for 60 s (its "breaker" is open).
"""
from __future__ import annotations

import base64
import json
import logging
import time

import httpx

from app.config import (BREAKER_COOLDOWN_S, BREAKER_FAILURES, LLM_TIMEOUT_S, MIN_AUDIO_BYTES, MODELS,
                        OPENROUTER_URL, STT_TIMEOUT_S, TTS_INSTRUCTIONS, TTS_TIMEOUT_S)

log = logging.getLogger(__name__)

AUDIO_FORMATS = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mpeg": "mp3"}
RETRY_MESSAGE = "Your last reply had these problems: {problems}. Reply again with valid JSON only."


class ProviderError(Exception):
    """The provider failed, no time was left, or every model of the role is skipped."""


class BadOutputError(Exception):
    """The model answered, but its output failed validation twice."""


class Client:
    """Talks to OpenRouter. It holds the breakers, so make one per process (and one per eval run)."""

    def __init__(self, api_key: str, http: httpx.Client | None = None, now=time.monotonic):
        self.api_key = api_key
        self.http = http or httpx.Client()
        self.now = now                            # the clock; tests pass a fake one
        self.failures: dict[str, int] = {}        # model id -> provider problems in a row
        self.skip_until: dict[str, float] = {}    # model id -> skipped until this time

    def chat_json(self, role: str, messages: list[dict], schema_name: str, schema: dict,
                  validate, *, deadline: float, calls: list[dict]):
        """Ask the role's model for JSON and return validate(reply). Raises ProviderError or BadOutputError."""
        response_format = {"type": "json_schema",
                           "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
        models = self._models(role, calls)
        model, retried = models[0], False
        for attempt in (1, 2):
            body = {"model": model["id"], "messages": messages, "response_format": response_format,
                    "provider": {"require_parameters": True}, **model["params"]}
            try:
                content = self._post(role, model, "/chat/completions", body, LLM_TIMEOUT_S, deadline,
                                     calls, read=_chat_content, retried=retried)
            except ProviderError:
                if attempt == 2 or len(models) == 1:
                    raise
                model = models[1]                         # rule 1: the backup model, once
                continue
            try:
                return validate(_json_object(content))
            except ValueError as err:
                calls[-1]["outcome"] = "bad_output"      # the provider was fine; the reply was not
                if attempt == 2:
                    raise BadOutputError(str(err)) from None
                messages = [*messages, {"role": "assistant", "content": content},
                            {"role": "user", "content": RETRY_MESSAGE.format(problems=err)}]
                retried = True                            # rule 2: the same model once more

    def transcribe(self, audio: bytes, mime: str, language: str, *, deadline: float,
                   calls: list[dict]) -> str:
        """Speech to text. An empty transcript raises ProviderError without trying the backup."""
        body = {"input_audio": {"data": base64.b64encode(audio).decode("ascii"),
                                "format": audio_format(mime)},
                "language": language}
        text = self._first_that_works("stt", "/audio/transcriptions", body, STT_TIMEOUT_S, deadline,
                                      calls, read=_stt_text)
        if not text.strip():
            raise ProviderError("empty transcript")
        return text.strip()

    def speak(self, text: str, voice: str, *, deadline: float, calls: list[dict]) -> bytes:
        """Text to speech as mp3 bytes. Raises ProviderError; the browser voice is the backup."""
        body = {"input": text, "voice": voice, "instructions": TTS_INSTRUCTIONS, "response_format": "mp3"}
        return self._first_that_works("tts", "/audio/speech", body, TTS_TIMEOUT_S, deadline, calls,
                                      read=_mp3_bytes)

    # ---------- helpers ----------
    def _first_that_works(self, role, path, body, limit, deadline, calls, *, read):
        """Rule 1 for audio: the primary model, then on a provider problem the backup, once."""
        models = self._models(role, calls)
        for model in models:
            try:
                return self._post(role, model, path, {"model": model["id"], **body, **model["params"]},
                                  limit, deadline, calls, read=read)
            except ProviderError:
                if model is models[-1]:
                    raise

    def _post(self, role, model, path, body, limit, deadline, calls, *, read, retried=False):
        """Send one request and return read(response). Logs it in `calls`, updates the breaker, and
        raises ProviderError for any provider problem, or when no time is left (without calling)."""
        entry = _entry(role, model["id"], "skipped", retried)
        calls.append(entry)
        timeout = min(limit, deadline - self.now())
        if timeout <= 0:
            raise ProviderError("deadline")
        started = self.now()
        try:
            response = self.http.post(OPENROUTER_URL + path, json=body, timeout=timeout,
                                      headers={"Authorization": f"Bearer {self.api_key}"})
            if response.status_code != 200:
                raise ProviderError(f"HTTP {response.status_code}")
            result = read(response)
        except (httpx.HTTPError, ProviderError) as err:
            reason = str(err) or type(err).__name__
            entry.update(ms=self._ms(started), outcome="provider_error")
            self._failed(model["id"])
            log.warning("%s call to %s failed: %s", role, model["id"], reason)
            raise ProviderError(reason) from None
        entry.update(ms=self._ms(started), outcome="ok", **_usage(response))
        self.failures[model["id"]] = 0
        return result

    def _models(self, role: str, calls: list[dict]) -> list[dict]:
        """The role's models, primary first, minus those whose breaker is open (logged as skipped)."""
        usable = []
        for model in MODELS[role][:2]:
            if self.now() < self.skip_until.get(model["id"], 0.0):
                calls.append(_entry(role, model["id"], "skipped"))
            else:
                usable.append(model)
        if not usable:
            raise ProviderError(f"every {role} model is skipped (breaker open)")
        return usable

    def _failed(self, model_id: str) -> None:
        """Count a provider problem. The 3rd in a row opens the model's breaker for 60 s."""
        self.failures[model_id] = self.failures.get(model_id, 0) + 1
        if self.failures[model_id] >= BREAKER_FAILURES:
            self.failures[model_id] = 0
            self.skip_until[model_id] = self.now() + BREAKER_COOLDOWN_S
            log.warning("breaker open: %s is skipped for %.0f s", model_id, BREAKER_COOLDOWN_S)

    def _ms(self, started: float) -> int:
        return round((self.now() - started) * 1000)


def audio_format(mime: str) -> str:
    """OpenRouter's format name for a recording's MIME type ("audio/webm;codecs=opus" -> "webm")."""
    base = mime.split(";")[0].strip().lower()
    return AUDIO_FORMATS.get(base, base.split("/")[-1])


def _entry(role: str, model_id: str, outcome: str, retried: bool = False) -> dict:
    """One record for `calls`; ms, tokens and cost are filled in after the request."""
    return {"role": role, "model": model_id, "ms": 0, "outcome": outcome, "retried": retried,
            "backup_used": model_id != MODELS[role][0]["id"], "tokens": None, "cost": None}


def _json_body(response: httpx.Response) -> dict:
    """The JSON body. Not a JSON object, or an "error" inside it (even with HTTP 200), is a provider problem."""
    try:
        data = response.json()
    except ValueError:
        raise ProviderError("the response is not JSON") from None
    if not isinstance(data, dict):
        raise ProviderError("the response is not a JSON object")
    if "error" in data:
        raise ProviderError(f"error body: {data['error']}")
    return data


def _chat_content(response: httpx.Response) -> str:
    """The reply text of a chat response. No reply, or an empty one, is a provider problem."""
    data = _json_body(response)
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError("no reply in the response") from None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError(f"empty reply (finish_reason: {choice.get('finish_reason')})")
    return content


def _json_object(content: str) -> dict:
    """The model's reply as a JSON object, or ValueError saying why it isn't one."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as err:
        raise ValueError(f"not valid JSON ({err.msg})") from None
    if not isinstance(data, dict):
        raise ValueError("the reply must be one JSON object")
    return data


def _stt_text(response: httpx.Response) -> str:
    """The transcript of a speech-to-text response ("" when there is none)."""
    return str(_json_body(response).get("text") or "")


def _mp3_bytes(response: httpx.Response) -> bytes:
    """The audio of a speech response. Anything that isn't real audio is a provider problem."""
    kind = response.headers.get("content-type", "")
    if not kind.startswith("audio/") or len(response.content) < MIN_AUDIO_BYTES:
        raise ProviderError(f"not audio ({kind or 'no type'}, {len(response.content)} bytes)")
    return response.content


def _usage(response: httpx.Response) -> dict:
    """Tokens and cost from a JSON response's "usage" (audio responses have none)."""
    if "json" not in response.headers.get("content-type", ""):
        return {}
    usage = response.json().get("usage")
    if not isinstance(usage, dict):
        return {}
    return {"tokens": usage.get("total_tokens"), "cost": usage.get("cost")}
