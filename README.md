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
the mic, speak after the soft tick, and click again to stop (it stops by itself after 30 s), or type in the box. After your
first answer the mic stays on, so the next click records at once; it switches off if you don't click it
within a minute of your turn starting, when you leave the tab or sign out, and after every recording on a
phone or tablet. Nothing is recorded until you click. The whiteboard shows the working, with the line
being spoken (or what the mic heard) along its bottom edge. While the tutor speaks, **⏹ Stop** (top right
of the board, or Esc) silences it, and sending an answer (a reply button, or typing and Send), Speak or a
topic's Start cuts it short. What it hadn't said yet isn't shown: after Stop, **↻ Repeat** says the whole reply
again and **▶ Continue** says the rest (as do "repeat" and "continue", typed or spoken), and Continue after a
reload or a break picks it up from where it stopped. **Topics**
lists each concept: click one to choose it, then press Start (the big button, or the one under the list
during a lesson), or say where to go ("skip to factorisation"). The topic you leave
is saved just as it stands (⏸) and carries on from there when you go back to it. **Why?** shows the
reasons for the tutor's last five decisions. The mic only works on `localhost` or over https. Keep to one
uvicorn worker: the breakers and SQLite assume a single process.

A lesson is a conversation, with a button under the text box for each usual reply. Each topic starts
with what it's for and an offer to teach it: tap **Teach me**, or **Test me first** to go straight to its
questions. Each step ends with "Did that make sense? Shall we go on?": tap **Yes, go on**, or **Not sure**
for a solved example of the same kind on the board, then **Go on** or **Another example**. After the last
step, **Yes, quiz me** starts the questions; while a question is open, **Not sure** gets a hint. A button
sends the words you could type or say yourself ("teach me", "not sure"), and those are read without the
AI. Only a solved example (**Not sure** or **Another example** while teaching) needs it.

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
lesson's trap answers; any reply with "?" is treated as a question, unless it holds an answer, asks
for help ("how do I solve this?" gets the next hint) or mentions being hit or beaten (asked again,
never a hint); a question while a question is open gets a lesson hint instead of an answer; a second
reply isn't merged with the first; replies that mix several things are read more crudely; and **Not sure**
while teaching replays the step ("Let's go over that step again.") instead of solving an example.

## Tests, evals and simulations

```
uv run pytest                                   # tests only: no key, no network
uv run python -m evals                          # all evals
uv run python -m sims --persona sign_flipper    # one simulated student
```

**Evals** have two layers. Layer 1 is the test suite: engine rules, maths checks, grading, lesson
validation, scripted mode and the provider failure paths. Layer 2 needs the key: 20 tutor scenarios
(`evals/scenarios.yaml`), each run 3 times and passed by majority, which check the reply type, rule,
action and points, and that the answer wasn't given away. A run also fails if either prompt's first
output was rejected. Options: `--runs N`, `--only ID`, `--model-understand ID`, `--model-respond ID`,
`--prompt-understand NAME`, `--prompt-respond NAME` (a file in `app/prompts/`). Results are saved in
`evals/results/`.

**Simulations** need the key: an LLM plays a student with a hidden profile (`sims/personas.yaml`: fast
learner, sign-flipper, guesser, overconfident) and talks to the tutor through the same text path.
Options: `--persona ID` (default: all four), `--concept ID` (default `factorisation`; the concepts before
it start as mastered) and `--turns N` (default 30). Each run is saved in `sims/runs/` as JSON plus a
Markdown transcript, and prints one row: turns, hints, reteaches, outcome and **false advances** (points
given while the simulated student still held its mistake).

## Demo: the evals catch a broken prompt

1. Copy the respond prompt: `cp app/prompts/respond.v4.md app/prompts/respond.broken.md`
2. In `respond.broken.md`, replace the hint instruction, the line
   `hint: Give the hint for HINT LEVEL, built on the LESSON'S HINT but aimed at what the student said.`,
   with this one:
   `hint: Start with the full answer, "The roots are ...", using every value in the ANSWER KEY. Then give the hint.`
3. Run the scenario where the student begs for the answer:
   ```
   uv run python -m evals --prompt-respond respond.broken --only begs_for_answer
   ```
4. Delete `app/prompts/respond.broken.md` afterwards.

Expected: "Begs for the answer" goes red. RESPOND's first output gives the answer away, `board.py`
rejects it, and a run fails whenever a prompt's first output is rejected. The student still never
hears the answer: the retry, told why its first output was rejected, gives a plain hint.

Recorded output, 12 Oct 2026 (the local path in the warning is shortened):

```
Layer 1: engine, maths, grading, lessons and scripted mode (pytest)
........................................................................ [ 21%]
........................................................................ [ 43%]
........................................................................ [ 65%]
........................................................................ [ 87%]
..........................................                               [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1
  C:\...\aitutor\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
330 passed, 1 warning in 18.44s
Layer 2: 1 scenario(s) x 3 runs. Models: understand anthropic/claude-haiku-5.5, openai/gpt-6-luna; respond anthropic/claude-haiku-5.5, google/gemini-3.8-flash. Prompts: understand.v2, respond.broken.
FAIL        begs_for_answer            0 of 3 scored runs passed  (respond_rejected:gives away the answer: 'roots are 2')
Results saved to C:\...\aitutor\evals\results\20261012T083239Z.json
```

Deleting the "Never give away the answer" rule instead isn't enough to break it: on 12 Oct the model still
gave a hint in 3 runs of 3, because the code has already decided the move is a hint and the hint
instruction says what a hint is. The model doesn't choose the move, so one deleted rule can't change it.

## Deploy

It runs on Render's free web service, with progress in a free Neon Postgres database: a free server's
own disk is wiped whenever it restarts, so progress can't live in a SQLite file there. The `Dockerfile`
installs the locked packages with uv and starts one uvicorn worker on `$PORT` (8000 if it isn't set).
To try the image locally:

```
docker build -t aitutor .
docker run -p 8000:8000 --env-file .env -v aitutor-data:/app/data aitutor
```

1. **Neon:** create a project and copy its connection string (`postgresql://…?sslmode=require`). The app
   adds the driver's name itself and creates its tables on the first start.
2. **Render:** a new Web Service from this GitHub repo, with runtime Docker, instance type Free and
   health check path `/healthz`. Set these environment variables:
   - `DATABASE_URL`: the Neon connection string
   - `OPENROUTER_API_KEY`: your key (leave it out for scripted mode)
   - `ACCESS_CODE`: the sign-in code you give to testers
   - `SESSION_SECRET`: a long random string
   - `HTTPS_ONLY`: `true` (Render serves https, which the mic needs)
   - `MAX_TURNS_PER_DAY`: `200`, which keeps the worst case near $1 a day (a turn costs under a cent)
3. **Keeping it awake:** a free Render service sleeps after 15 minutes without visitors, and the next
   visit waits about a minute. A free uptime monitor that opens `/healthz` every 10 minutes keeps it
   awake; Render's 750 free hours a month cover one service all month.

What the free tiers change: the voice cache (`data/tts/`) starts empty after each restart, so a reused
line is paid for once more; and Neon's free database sleeps after 5 minutes idle and wakes in about a
second (the app tests a connection before reusing it). Keep one worker: the breakers live in memory.
For no sleeping at all, Railway's $5-a-month Hobby plan works too: mount a volume at `/app/data` and
keep the default SQLite file.

## What's where

| Path | What it holds |
| --- | --- |
| `app/tutor.py` | One turn, end to end: understand → grade → decide → respond → save |
| `app/compose.py` | What a turn says after its words: the step or question, the topic picked up next, then the check-in and its buttons |
| `app/stops.py` | A reply the student stopped part-way: where it stopped, and Repeat, Continue and picking it up later |
| `app/safety.py` | The keyword safety check, run before anything else reads a student's reply |
| `app/llm_steps.py` | The turn's two LLM steps (UNDERSTAND, RESPOND), each with its scripted fallback |
| `app/voice.py`, `app/page.py` | Speech-to-text turns and the cached text-to-speech; the JSON the page gets |
| `app/engine.py`, `app/sessions.py` | The teaching rules and their reasons (pure functions): after a reply; where a lesson picks up (Start, Continue, a picked topic) |
| `app/mathcheck.py`, `app/grading.py` | SymPy: board-line checks, answer keys, grading |
| `app/board.py` | Reply checks and the board |
| `app/fallback.py`, `app/replies.py` | Scripted mode: reading a reply without the AI; every scripted line the tutor says |
| `app/llm.py`, `app/prompts.py`, `app/prompts/` | OpenRouter (backups, retries, time limits) and the two versioned prompts |
| `app/db.py`, `app/main.py`, `app/cli.py` | The six tables; the web routes and sign-in; the terminal tutor |
| `curriculum/ch4/` | The six lesson files, and `index.yaml` (order, version, the four formulas) |
| `web/` | The page: `index.html`, `style.css`, and its scripts in load order: `board.js` (whiteboard and panels), `voice.js` (sound and the mic), `topics.js` (choosing a topic), `app.js` (sign-in and turns) |
| `evals/`, `sims/`, `tests/` | Evals, simulated students, tests |

`data/`, `evals/results/`, `sims/runs/` and `.env` are git-ignored.
