"""Web routes in scripted mode: sign-in, the cookie, sessions and text turns.

Importing app.main also builds the module-level app from your settings (it creates data/tutor.db if missing).
Each test below builds its own app instead, with an in-memory database."""
from conftest import make_settings, sign_in
from fastapi.testclient import TestClient

from app.main import create_app

PAYLOAD_KEYS = {"turn_id", "transcript", "segments", "decision", "progress", "why", "board", "input_hint"}


def make_client(cur, **changes) -> TestClient:
    """A browser stand-in talking to a fresh app with its own in-memory database."""
    return TestClient(create_app(make_settings(**changes), cur=cur))


def cookie_flags(response) -> list[str]:
    """The Set-Cookie attributes after the value, lower-cased: ["path=/", "max-age=2592000", "httponly", ...]."""
    parts = response.headers["set-cookie"].split(";")[1:]
    return [part.strip().lower() for part in parts]


def test_health_check_needs_no_sign_in(cur):
    """The host's health check and an uptime pinger call /healthz: no sign-in needed."""
    r = make_client(cur).get("/healthz")
    assert (r.status_code, r.json()) == (200, {"ok": True})


def test_login_wrong_code_403(cur):
    client = make_client(cur)
    r = client.post("/api/login", json={"username": "asha", "first_name": "Asha", "access_code": "wrong"})
    assert r.status_code == 403
    assert "set-cookie" not in r.headers
    assert client.get("/api/state").status_code == 401


def test_login_rejects_bad_username_or_name(cur):
    client = make_client(cur)
    for username, first_name in [("a b", "Asha"), ("", "Asha"), ("x" * 31, "Asha"), ("asha", "   ")]:
        r = client.post("/api/login", json={"username": username, "first_name": first_name, "access_code": "letmein"})
        assert r.status_code == 422, username


def test_login_sets_cookie_and_state_works(cur):
    client = make_client(cur)
    r = sign_in(client)
    assert r.status_code == 200
    assert r.json() == {"first_name": "Asha"}
    flags = cookie_flags(r)
    assert "httponly" in flags                 # page scripts can't read the cookie
    assert "samesite=lax" in flags
    assert "max-age=2592000" in flags          # 30 days
    assert "secure" not in flags               # https_only is off in tests
    state = client.get("/api/state")
    assert state.status_code == 200
    body = state.json()
    assert [row["concept_id"] for row in body["progress"]] == cur.order
    assert body["board"] == []
    assert body["why"] == []


def test_cookie_is_secure_when_https_only(cur):
    r = sign_in(make_client(cur, https_only=True))
    assert "secure" in cookie_flags(r)


def test_routes_require_sign_in(cur):
    client = make_client(cur)
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/logout").status_code == 401
    assert client.post("/api/session").status_code == 401
    assert client.post("/api/turn", json={"text": "ok"}).status_code == 401


def test_cookie_for_unknown_student_is_401(cur):
    first = make_client(cur)
    sign_in(first)
    reset = make_client(cur)          # same secret, empty database: like a redeploy that wiped the data
    reset.cookies = first.cookies
    assert reset.get("/api/state").status_code == 401


def test_session_then_turn_round_trip(cur):
    client = make_client(cur)
    sign_in(client)
    start = client.post("/api/session")
    assert start.status_code == 200
    body = start.json()
    assert PAYLOAD_KEYS <= set(body)
    assert set(body["decision"]) == {"rule", "action", "reason"}   # never the evidence (it can hold the key)
    assert body["decision"]["rule"] == "S0"
    assert any("Asha" in segment["say"] for segment in body["segments"])
    turn = client.post("/api/turn", json={"text": "ok", "prev_first_audio_ms": 900})
    assert turn.status_code == 200
    assert turn.json()["transcript"] == "ok"
    assert turn.json()["decision"]["rule"] == "R0"
    assert client.get("/api/state").json()["why"][0]["rule"] == "R0"   # newest decision first
    assert client.post("/api/logout").json() == {"ok": True}
    assert client.get("/api/state").status_code == 401


def test_jump_picks_a_topic(cur):
    client = make_client(cur)
    sign_in(client)
    picked = client.post("/api/jump", json={"concept_id": "discriminant"})
    assert picked.status_code == 200 and PAYLOAD_KEYS <= set(picked.json())
    assert picked.json()["decision"]["rule"] == "S2"
    rows = {row["concept_id"]: row for row in picked.json()["progress"]}
    assert rows["discriminant"]["current"] and rows["discriminant"]["status"] == "teaching"
    assert client.post("/api/jump", json={"concept_id": "calculus"}).status_code == 404


def test_turn_without_session_409(cur):
    client = make_client(cur)
    sign_in(client)
    r = client.post("/api/turn", json={"text": "ok"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Press Start first."


def test_existing_username_keeps_first_name(cur):
    client = make_client(cur)
    sign_in(client, "asha", "Asha")
    again = sign_in(client, "asha", "Someone Else")
    assert again.json() == {"first_name": "Asha"}


def test_index_served(cur):
    r = make_client(cur).get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>AI Tutor</title>" in r.text
