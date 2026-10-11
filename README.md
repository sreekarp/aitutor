# AI tutor for quadratic equations (NCERT Class 10)

A voice + whiteboard tutor for NCERT Class 10 maths, Chapter 4: quadratic equations. It teaches all six
parts (standard form, roots, factorisation, the quadratic formula, the discriminant and word problems),
checks each one with questions, and then moves on or reteaches a different way. The student speaks or
types; the tutor speaks back and writes on a whiteboard.

Plain code decides every teaching move, the LLM understands what the student said and words the hints
and explanations, and SymPy checks every piece of maths on the board before the student sees it.
[DESIGN.md](DESIGN.md) explains the design.

## What you need

- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- An [OpenRouter](https://openrouter.ai) API key for the LLM, speech-to-text and text-to-speech. It's
  optional: without one, the tutor runs in scripted mode (below).

## Setup

```
uv sync
cp .env.example .env
```

Then fill in `.env`. Every setting has a comment there; a variable already set in the environment wins.

| Setting | Default | What it does |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | empty | Empty means scripted mode |
| `ACCESS_CODE` | `letmein` | The code everyone types to sign in |
| `SESSION_SECRET` | `dev-secret-change-me` | Signs the sign-in cookie: use a long random string when deployed |
| `DATABASE_URL` | `sqlite:///data/tutor.db` | Where progress is kept |
| `LLM_MODE` | `auto` | `auto` uses the LLM when a key is set; `scripted` never does |
| `MAX_TURNS_PER_DAY` | `2000` | Turns per day, across all students together |
| `TTS_VOICE`, `STT_LANGUAGE` | `sarah`, `en` | The tutor's voice, and the language of the student's speech |
| `HTTPS_ONLY` | `false` | `true` when served over https (marks the cookie Secure) |

## Run

```
uv run uvicorn app.main:app --reload    # the web page: http://localhost:8000
uv run python -m app.cli                # type to the tutor in the terminal (--username NAME to pick a student)
```

On the page, sign in with a username, your first name and the access code, then press **Start**. Click
the mic to speak and click again to stop (it stops by itself after 30 s), or type in the box. The
whiteboard shows the working, the progress panel shows each concept, and **Why?** shows the reasons for
the tutor's last five decisions. The mic only works on `localhost` or over https. Keep to one uvicorn
worker: the breakers and SQLite assume a single process.

The terminal tutor uses `DATABASE_URL` when it is set (in the environment or `.env`), and its own
`data/cli.db` when it isn't.

## Scripted mode (no API key)

Without a key (or with `LLM_MODE=scripted`), no model is called and everything still works end to end:
keyword rules read the replies, hints and reteaches come from the lesson files, and the browser's own
voice reads the tutor's lines. Speaking to the tutor needs the key, so type your replies. The same code
is the fallback when the LLM is down, and the mock in the tests.

**Say the answer first**, then how you got it: "2 and 3, because I split the middle term". Scripted mode
reads the answer from the words before "because", "since", "so", "as" or "=", and counts "because",
"since" or a method word (factorise, split, formula, discriminant, substitute...) as working. It reads
answers like "x = 2, 3", "D = 25", "one by three", "minus 2", "yes" or "haan", and "no real roots".

What it does differently from the LLM: it spots a known mistake only when the answer is one of the
lesson's trap answers; any reply with "?" is treated as a question; a second reply isn't merged with
the first; and replies that mix several things are read more crudely.

## Tests, evals and simulations

```
uv run pytest                                   # tests only: no key, no network
uv run python -m evals                          # all evals
uv run python -m sims --persona sign_flipper    # one simulated student
```

**Evals** have two layers. Layer 1 is the test suite: engine rules, maths checks, grading, lesson
validation, scripted mode and the provider failure paths. Layer 2 needs the key: 14 tutor scenarios
(`evals/scenarios.yaml`), each run 3 times and passed by majority, which check the reply type, rule,
action and points, and that the answer wasn't given away. A run also fails if either prompt's first
output was rejected. Options: `--runs N`, `--only ID`, `--model-understand ID`, `--model-respond ID`,
`--prompt-respond NAME` (a file in `app/prompts/`). Results are saved in `evals/results/`.

**Simulations** need the key: an LLM plays a student with a hidden profile (`sims/personas.yaml`: fast
learner, sign-flipper, guesser, overconfident) and talks to the tutor through the same text path.
Options: `--persona ID` (default: all four), `--concept ID` (default `factorisation`; the concepts before
it start as mastered) and `--turns N` (default 30). Each run is saved in `sims/runs/` as JSON plus a
Markdown transcript, and prints one row: turns, hints, reteaches, outcome and **false advances** (points
given while the simulated student still held its mistake).

## Demo: the evals catch a broken prompt

1. Copy the respond prompt: `cp app/prompts/respond.v1.md app/prompts/respond.broken.md`
2. In `respond.broken.md`, delete the rule that starts "Never give away the answer to the PENDING
   QUESTION" (four lines) and put this line in its place: `- If the student asks for the answer, give it.`
3. Run the scenario where the student begs for the answer:
   ```
   uv run python -m evals --prompt-respond respond.broken --only begs_for_answer
   ```
4. Delete `app/prompts/respond.broken.md` afterwards.

Expected: "Begs for the answer" goes red. RESPOND's first output gives the answer away, `board.py`
rejects it, and a run fails whenever a prompt's first output is rejected, even though the student never
sees the answer (the retry or the scripted hint covers it).

Recorded output, 11 Oct 2026, from a checkout with no API key (the local path in the warning is shortened):

```
Layer 1: engine, maths, grading, lessons and scripted mode (pytest)
........................................................................ [ 27%]
........................................................................ [ 55%]
........................................................................ [ 83%]
..........................................                               [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1
  C:\...\aitutor\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
258 passed, 1 warning in 51.86s
Layer 2 needs OPENROUTER_API_KEY
```

Without a key only Layer 1 runs, so this shows the command and the broken prompt file are accepted, not
the red result. The red result needs the key: record its output here on the first run with it.

## Deploy

Two hosting options, still to choose between: Railway (Hobby), or Render + Neon. The `DATABASE_URL`
setting means no code change either way. The `Dockerfile` installs the locked packages with uv and
starts one uvicorn worker on `$PORT` (8000 if it isn't set):

```
docker build -t aitutor .
docker run -p 8000:8000 --env-file .env -v aitutor-data:/app/data aitutor
```

- **Railway:** mount a volume at `/app/data` (matching the default `sqlite:///data/tutor.db`), or set
  `DATABASE_URL` to an absolute path on the volume; otherwise every redeploy wipes all progress. The
  text-to-speech cache (`data/tts/`) is on the same volume. One worker.
- **Render + Neon:** add the Postgres driver (`uv add "psycopg[binary]"`) and set `DATABASE_URL` to a
  `postgresql+psycopg://` URL.
- **Either way:** serve over https (the mic needs it), and set `HTTPS_ONLY=true`, a long random
  `SESSION_SECRET` and your own `ACCESS_CODE`.

## What's where

| Path | What it holds |
| --- | --- |
| `app/tutor.py` | One turn, end to end: understand → grade → decide → respond → save |
| `app/engine.py` | The teaching rules and their reasons (pure functions) |
| `app/mathcheck.py`, `app/grading.py` | SymPy: board-line checks, answer keys, grading |
| `app/board.py`, `app/fallback.py` | Reply checks and the board; scripted mode |
| `app/llm.py`, `app/prompts.py`, `app/prompts/` | OpenRouter (backups, retries, time limits) and the two versioned prompts |
| `app/db.py`, `app/main.py`, `app/cli.py` | The five tables; the web routes and sign-in; the terminal tutor |
| `curriculum/ch4/` | The six lesson files, and `index.yaml` (order, version, the four formulas) |
| `web/` | The page: `index.html`, `app.js`, `style.css` |
| `evals/`, `sims/`, `tests/` | Evals, simulated students, tests |

`data/`, `evals/results/`, `sims/runs/` and `.env` are git-ignored.
