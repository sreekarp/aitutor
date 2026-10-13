"""While the AI writes the tutor's reply (a solved example, an explanation, a hint), the page gets a short line to say
first, in the tutor's voice, so the wait doesn't feel empty: the turn's JSON comes as lines, {"wait": ..., "say":
...} and then the turn. A reply the tutor answers without the AI gets no such line."""
import json

from conftest import FakeLLM, make_settings, sign_in
from fastapi.testclient import TestClient

from app import replies
from app.main import create_app

NOT_SURE = {"type": "not_sure", "answer": "", "showed_working": False, "mistake": None, "safety": False}
# RESPOND's solved example: a new equation, not one of any topic's questions.
EXAMPLE = {"segments": [{"say": "Here's another one. For x squared minus 9x plus 20, find two numbers that multiply "
                                "to 20 and add to minus 9.",
                         "board": [{"op": "write", "id": "e1", "latex": "x^2 - 9x + 20 = 0",
                                    "check": "Eq(x**2 - 9*x + 20, 0)", "relation": "new"}]}]}


def lines_of(response) -> list[dict]:
    """The JSON lines of a turn's reply."""
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def at_step_one(cur, fake) -> TestClient:
    """A signed-in student on the first topic's step 1, on an app whose AI is the fake ("teach me" needs no AI)."""
    client = TestClient(create_app(make_settings(openrouter_api_key="test-key", llm_mode="auto"), llm=fake, cur=cur))
    sign_in(client)
    client.post("/api/session")
    assert lines_of(client.post("/api/turn", json={"text": "teach me"}))[-1]["decision"]["action"] == "next_step"
    return client


def test_while_the_ai_writes_an_example_the_page_hears_a_line_to_say_first(cur):
    client = at_step_one(cur, FakeLLM(understand=[NOT_SURE], respond=[EXAMPLE]))
    wait, turn = lines_of(client.post("/api/turn", json={"text": "I'm not sure I follow that"}))
    assert wait == {"wait": "example", "say": replies.WAIT["example"]} == {"wait": "example",
                                                                            "say": "Sure, let me think of one."}
    assert turn["decision"]["action"] == "example" and "wait" not in turn


def test_a_reply_answered_without_the_ai_gets_no_line_to_say_first(cur):
    client = at_step_one(cur, FakeLLM())
    (turn,) = lines_of(client.post("/api/turn", json={"text": "yes go on"}))
    assert turn["decision"]["action"] == "next_step"


def test_the_waiting_lines(cur):
    """One per kind of wait, each spoken in the tutor's voice (scripted mode has none: 503); no other text."""
    assert replies.WAIT == {"example": "Sure, let me think of one.", "explain": "Good question. Let me think.",
                            "think": "Hmm, let me think."}
    assert [replies.wait_key(action) for action in ("example", "explain", "hint", "reteach")] == [
        "example", "explain", "think", "think"]
    client = TestClient(create_app(make_settings(), cur=cur))
    sign_in(client)
    assert client.get("/api/tts/wait/example").status_code == 503
    assert client.get("/api/tts/wait/anything-else").status_code == 404
