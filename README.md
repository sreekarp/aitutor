# AI tutor for quadratic equations (NCERT Class 10)

**Live: https://aitutor-mar6.onrender.com** · access code **`quad-2026`**. Sign in with any username, your
first name and that code. The free server sleeps when idle, so the first visit can take about a minute.

A voice + whiteboard tutor for NCERT Class 10, Chapter 4. It teaches the six parts (standard form, roots,
factorisation, the quadratic formula, the discriminant, word problems), checks each one with questions, then
moves on or reteaches a different way, and shows why in its **Why?** panel.

Plain code decides every teaching move; the LLM reads what the student said and words the hints and
explanations; SymPy checks every piece of maths before the student sees it. [DESIGN.md](DESIGN.md) explains
the design, and [plan.md](plan.md) how it got here.

## Using it

- Press **Start**, then speak (click the mic, talk after the soft tick, click again to stop) or type.
- A topic opens with an offer: tap **Teach me**, or **Test me first** to go straight to its questions. Each step
  ends with "Did that make sense?": **Yes, go on**, or **Not sure** for a solved example, then **Go on** or
  **Another example**. After the last step, **Yes, quiz me**. While a question is open, **Not sure** gets a hint.
- A button sends the words you could say yourself ("teach me"), read without the AI: instant, never misheard.
- A topic is mastered after two clean answers in a row: a fresh question, right first time, with working, no hint.
- **⏹ Stop** (or Esc) silences the tutor; **↻ Repeat** and **▶ Continue** pick the reply up again. **Topics**
  jumps to any topic (or say "skip to factorisation"); the topic you leave is saved as it stands.
- The mic works only over https or on `localhost`.

## Run it locally

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/); an [OpenRouter](https://openrouter.ai) key is optional.

```
uv sync
cp .env.example .env                    # then fill it in: every setting has a comment
uv run uvicorn app.main:app --reload    # the page: http://localhost:8000
uv run python -m app.cli                # or type to the tutor in the terminal
```

| Setting | Default | What it does |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | empty | LLM, speech-to-text and text-to-speech; empty means scripted mode |
| `ACCESS_CODE` | `letmein` | The code everyone types to sign in |
| `SESSION_SECRET` | `dev-secret-change-me` | Signs the sign-in cookie; use a long random string when deployed |
| `DATABASE_URL` | `sqlite:///data/tutor.db` | Where progress is kept (a Postgres URL works too) |
| `LLM_MODE` | `auto` | `scripted` never calls a model |
| `MAX_TURNS_PER_DAY` | `2000` | Turns per day across all students: the cost cap |

Keep one uvicorn worker: the model breakers live in memory.

**Without a key (scripted mode)** no model is called and everything still works end to end: keyword rules read
the replies, hints and reteaches come from the lesson files, and the browser's own voice speaks. Type your replies
(speech-to-text needs the key) and say the answer first: "2 and 3, because I split the middle term". This is the
same code the tutor falls back on when the AI provider is down.

## Tests, evals and simulations

```
uv run pytest                                   # the tests: no key, no network
uv run python -m evals                          # the tests, then the scenarios with the real AI
uv run python -m sims --persona sign_flipper    # one simulated student
```

- **Evals:** 24 tutor scenarios (`evals/scenarios.yaml`), each run 3 times and passed by majority. Each checks
  the reply type, rule, action and points, and that the answer wasn't given away; a run also fails if a prompt's
  first output was rejected. Options: `--runs N`, `--only ID`, `--model-understand ID`, `--model-respond ID`,
  `--prompt-understand NAME`, `--prompt-respond NAME`. Results go to `evals/results/`.
- **Simulations:** an LLM plays a student with a hidden mistake (`sims/personas.yaml`) and counts **false
  advances**: progress given while the student still held the mistake. Options: `--persona`, `--concept`,
  `--turns`. Example transcripts are in `sims/examples/`.

## Demo: the evals catch a broken prompt

1. `cp app/prompts/respond.v7.md app/prompts/respond.broken.md`
2. In `respond.broken.md`, replace the line starting `hint: Give the hint for HINT LEVEL` with
   `hint: Start with the full answer, "The roots are ...", using every value in the ANSWER KEY. Then give the hint.`
3. Run `uv run python -m evals --prompt-respond respond.broken --only begs_for_answer`, then delete the file.

Expected: `FAIL begs_for_answer 0 of 3 scored runs passed (respond_rejected:gives away the answer: ...)`. The board
check rejects the reply, so the student still never hears the answer, but the eval goes red. Deleting the "never
give the answer" rule alone doesn't break it: the code, not the model, has already decided the move is a hint.

## Deploy

Render's free web service runs the `Dockerfile` (one uvicorn worker on `$PORT`); progress lives in a free Neon
Postgres database, because a free server's disk is wiped on every restart.

1. **Neon:** create a project and copy its connection string. The app creates its tables on the first start.
2. **Render:** a Web Service from this repo: runtime Docker, instance Free, health check path `/healthz`, and
   the variables `DATABASE_URL`, `OPENROUTER_API_KEY`, `ACCESS_CODE`, `SESSION_SECRET`, `HTTPS_ONLY=true`
   and `MAX_TURNS_PER_DAY=200` (about $1 a day at worst).
3. **Keep it awake:** a free uptime monitor opening `/healthz` every 10 minutes stops the 15-minute sleep.

## What's where

| Path | What it holds |
| --- | --- |
| `app/tutor.py` | One turn, end to end: understand → grade → decide → respond → save |
| `app/engine.py`, `app/sessions.py` | The teaching rules and their reasons (pure functions) |
| `app/llm_steps.py`, `app/prompts.py`, `app/prompts/` | The two LLM steps (UNDERSTAND, RESPOND) and their versioned prompts |
| `app/llm.py`, `app/rollout.py` | OpenRouter (backups, retries, time limits); which prompts and models a session uses |
| `app/mathread.py`, `app/mathcheck.py`, `app/grading.py`, `app/board.py` | SymPy: reading maths, checking board lines, answer keys, grading |
| `app/fallback.py`, `app/replies.py` | Scripted mode: reading replies without the AI; every scripted line |
| `app/compose.py`, `app/stops.py`, `app/examples.py` | What follows a turn's words; Stop, Repeat and Continue; the checked example equations |
| `app/safety.py` | The keyword safety check, run before anything else reads a reply |
| `app/db.py`, `app/main.py`, `app/voice.py`, `app/page.py`, `app/cli.py` | The tables; web routes and sign-in; speech; the page's JSON; the terminal tutor |
| `curriculum/ch4/` | The six lesson files and `index.yaml` |
| `web/` | The page: HTML, CSS and plain JS |
| `evals/`, `sims/`, `tests/` | Evals, simulated students, tests |
