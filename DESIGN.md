# DESIGN.md — AI tutor for quadratic equations (NCERT Class 10)

> Rewritten after the build: it describes what is in this repo. Anything designed but not built is
> marked *(designed)*. Numbers are measured unless marked *(estimate)*.

The core rule behind every section below: **plain code decides every teaching move, the LLM
understands what the student said and helps them fix their mistake in words, and SymPy checks every
piece of maths on the board before a student sees it.** State lives in the database and the engine,
never in an LLM conversation, which is what makes the rest of this document possible.

---

## 1. Data model

SQLite, through SQLAlchemy Core with a `DATABASE_URL` setting, so the same five tables run on
Postgres with no code change (add `psycopg[binary]` and use a `postgresql+psycopg://` URL). JSON
columns are text in SQLite and `JSON` in Postgres. Ids are `uuid4` hex strings; times are UTC.

```sql
CREATE TABLE students (
  id          TEXT PRIMARY KEY,
  username    TEXT NOT NULL UNIQUE,   -- sign-in name, e.g. "riya7"
  first_name  TEXT NOT NULL,          -- used in greetings; no surname, email or age: minors
  created_at  DATETIME
);

CREATE TABLE sessions (                -- a new one at Start after a 30-minute gap
  id                  TEXT PRIMARY KEY,
  student_id          TEXT REFERENCES students(id),
  session_no          INTEGER,        -- 1, 2, 3 ... per student
  curriculum_version  TEXT,           -- e.g. "2026.10.1", as at session start
  prompt_versions     JSON,           -- {"understand": "understand.v1", "respond": "respond.v1"}
  models              JSON,           -- role -> models from config.py, or {"mode": "scripted"}
  board               JSON,           -- the whiteboard now, so a reload or Continue redraws it
  started_at          DATETIME,
  UNIQUE (student_id, session_no)
);

CREATE TABLE progress (                -- one row per student per concept: the current state
  student_id          TEXT,
  concept_id          TEXT,           -- stable ids: standard_form, roots, factorisation, ...
  status              TEXT,           -- not_started | teaching | checking | mastered | needs_review
  step_id             TEXT,           -- the teaching step shown, by id, e.g. "split_middle"
  points              FLOAT,          -- +1 per right answer with working, +0.5 after a hint; mastered at 2
  pending             JSON,           -- the open question: check_id, hint_level, awaiting_working,
                                      -- followup_used, last_answer. Set exactly while status = checking
  last_check_id       TEXT,           -- the last question asked; the next one follows it in file order
  reteach_count       INTEGER,        -- at most 3 per pass through a concept
  strategies_tried    JSON,           -- ["worked_example", "substitute_back"]
  curriculum_version  TEXT,           -- the lesson version when this row was last written
  updated_at          DATETIME,
  PRIMARY KEY (student_id, concept_id)
);

CREATE TABLE turns (                   -- append-only: one row per reply, and per Start / Continue
  id            TEXT PRIMARY KEY,
  session_id    TEXT REFERENCES sessions(id),
  turn_no       INTEGER,              -- 0, 1, 2 ... within the session (0 is its Start)
  concept_id    TEXT,
  phase         TEXT,                 -- teaching | checking
  input         TEXT,                 -- voice | text | start
  student_text  TEXT,                 -- the transcript; no audio is stored
  understood    JSON,                 -- reply type, answer, showed_working, suspected mistake, safety
  grade         JSON,                 -- SymPy's verdict (correct | wrong | partial | unsure), trap
  tutor_output  JSON,                 -- the segments spoken and drawn (speech + board commands)
  versions      JSON,                 -- what this turn used: curriculum, understand and respond prompts
  latency_ms    JSON,                 -- per stage, plus the browser's prev_first_audio_ms, and for
                                      -- voice turns its mic_open_ms and recording_ms
  calls         JSON,                 -- per model call: role, model, ms, outcome, retried,
                                      -- backup_used, tokens, cost
  issues        JSON,                 -- rejected outputs and why, fallbacks, lesson changes, safety
  created_at    DATETIME
);

CREATE TABLE decisions (               -- append-only: the "why" behind every move, one per turn
  id             TEXT PRIMARY KEY,
  turn_id        TEXT REFERENCES turns(id),
  student_id     TEXT,
  concept_id     TEXT,
  rule           TEXT,                -- R0..R10, S0, S1, END, LIMIT
  action         TEXT,                -- next_step | credit | hint | reteach | needs_review | ...
  reason         TEXT,                -- built from a template in code, never by an LLM
  evidence       JSON,                -- what the rule saw, answer key included: never sent to the browser
  hint_level     INTEGER,
  reteach_count  INTEGER,
  strategy       TEXT,                -- worked_example | substitute_back | compare_non_example | replay
  created_at     DATETIME
);
```

Why it looks like this:

- **`turns` and `decisions` are the history; `progress` is the current state.** A turn's writes
  (progress, turn, decision, board) happen in one transaction at the end of the turn, so a turn
  that fails halfway changes nothing.
- **One progress row per concept,** not one JSON blob per student, so "how many students are stuck
  in factorisation?" is a single query, and the progress panel reads one row per concept. The
  current concept isn't stored: it's the first concept being taught or checked, else the first not
  started.
- **Positions are ids, not numbers.** `step_id` and `pending.check_id` name the step and the
  question, so editing a lesson file can't silently move a student (section 2).
- **Versions are stamped twice:** on the session (what was configured when it started) and on
  every turn (what that turn actually used), and every model call in `calls` names its model. Any
  metric can be sliced by prompt, model or curriculum version.
- **Reasons are safe to show; evidence isn't.** The "Why?" panel shows rule and reason, which never
  contain the answer key, often while the same question is still open. The key stays in `evidence`.
- **No raw audio and no personal data beyond a first name.** The users are minors; transcripts are
  enough for debugging and evals. (The audio cache on disk holds only the tutor's own lines.)
- **Sign-in is a username, a first name and a shared access code.** The signed-in student id lives
  in a signed, HTTP-only cookie and is never taken from the request body. The access code keeps
  strangers off the OpenRouter key, and a daily turn limit across all students caps the cost.

---

## 2. Iterability

What a change does to a student halfway through session 3 depends on what changed, because the
LLM holds no state: the lesson files, the engine and the database do. Each turn rebuilds what it
needs from the database, and records the versions it used.

**Prompt or model change.** Prompts are versioned files (`app/prompts/respond.v1.md`), and
`config.py` names the version each role uses and its models (a primary and a backup). A change
takes effect on the student's next turn after the deploy, and can't corrupt anything, because no
state lives in the prompt. That turn's `versions` and `calls` show what it used.

Rollout:

1. A new prompt is a new file (`respond.v2.md`); a version in use is never edited, because prompt
   files are read on every call.
2. `uv run python -m evals --prompt-respond respond.v2` (or `--model-respond <id>`) runs the 14
   tutor scenarios against it, 3 runs each. A scenario passes with a majority of passing runs; any
   red scenario blocks the change. `uv run python -m sims` shows false advances over longer
   conversations.
3. Switch `PROMPT_VERSIONS` (or `MODELS`) in `config.py` and deploy.
4. Rollback is the same switch back. Because state isn't in the prompt, students mid-session move
   back on their next turn.

*(Designed)* Gradual rollout: send a share of students to the new version (by a hash of the
student id: 10%, then 50%, then 100%) and compare the two side by side. Every turn already records
its versions, so the comparison is a query.

**Where the curriculum comes from.** Six hand-written YAML lesson files in the repo
(`curriculum/ch4/`), plus `index.yaml` with the order, the version and the four general formulas.
A lesson file holds the concept's title, its `intro` (a sentence or two on what it's for, said before
step 1), its teaching steps, its questions with hints and trap answers, its known mistakes, and how to
reteach it.
At startup the app validates all of it: the schema, the ids, every answer key (computed by SymPy,
never typed in), every board line (checked by SymPy) and the spoken text (no LaTeX). If anything
fails, the app refuses to start, so a broken lesson never reaches a student. A new NCERT chapter is
a new folder of lesson files; a new kind of question also needs a few lines in `grading.py`.

**Curriculum change.** Concept, step and question ids are stable keys. A lesson change takes effect
from the first turn after the deploy:

| Change | Example | What happens to the session-3 student |
| --- | --- | --- |
| Content within a step or question | Better wording, a new hint | Nothing moves; the next turn uses it |
| A new question | A seventh check in factorisation | Asked when its turn in file order comes |
| A step removed or renamed | `split_middle` renamed | If they were on it: back to the concept's first step |
| The open question removed | `fa3` deleted | Their reply isn't marked; the next question is asked (R8, "the lesson changed") |
| Concepts split, merged or renamed | Split "roots" into two | *(Designed)* a migration script maps progress (mastered → both mastered; in progress → the first) |

Points are always kept, and the turn records a `curriculum_changed` issue. This is the
curriculum-change rule (`engine.reconcile`), checked at the start of every turn. *(Designed)* A step
map (old step → equivalent new step) would move students more gently than "back to the first
step", and loading several curriculum versions at once would let a session finish on the version
it started with. Neither is built: the rule above already keeps every student on valid ids.

---

## 3. Observability

Every turn writes one `turns` row, one `decisions` row and one JSON log line to stdout with the same
fields: session, turn and student ids; the session's and the turn's versions; phase; what
UNDERSTAND made of the reply; SymPy's verdict; the decision's rule and reason; milliseconds per
stage (for a voice turn, also the browser's mic-open and recording times); each model call's model,
time, outcome, retry, backup, tokens and cost; and any issues. An issue is a rejected model output
with its exact problems (not JSON, bad maths, a revealed answer, LaTeX in speech), a fall back to
scripted words, a lesson change, a safety review or an internal error. A safety review is a reply
caught by the keyword safety check (`app/safety.py`, run before anything else reads the reply) or by
UNDERSTAND's safety flag. A safety review and a model's breaker opening also write a warning line.

Alerts *(designed, not built; thresholds are targets to tune)*:

| Signal | Alert when |
| --- | --- |
| Provider errors | Over 5% of calls fail in 5 minutes, or a breaker opens |
| Scripted fallbacks | Over 5% of turns fall back to scripted words in an hour |
| Rejected outputs | Over 5% of first outputs are rejected in an hour |
| Maths errors | SymPy rejects over 2% of LLM-written board lines in an hour |
| Time to first audio | p95 above 3.5 s for 15 minutes |
| Safety reviews | Any: notify a person immediately |

**Finding out the tutor got worse before a student says so:**

- **Evals before every change:** the 14 scenarios run against a new prompt or model, and a red
  scenario blocks it. A run fails if the first output of either prompt was rejected, so the safety
  net (one retry, then scripted words) can't hide a bad prompt. *(Designed)* The same suite nightly
  against the live config, plus a simulation batch reporting the false advance rate (points given
  while the simulated student still held its hidden mistake).
- **Caught problems are counted, not hidden:** the safety net keeps bad replies away from
  students, but every catch is an issue on the turn, so a rising rate shows a prompt or model
  drifting even while students see nothing wrong.
- **Decision mix drift:** a jump in R1 credits (the tutor got lenient), in reteaches per concept
  (harsh or confusing), in R8 "say that again" or R7 questions, or in questions per lesson step all
  show up before complaints do.
- *(Designed)* **Shadow understanding:** a small share of live turns re-run offline through
  UNDERSTAND on a stronger model; a rising rate of different reply types or mistakes means the live
  model drifted. (SymPy does the grading, so there is no LLM grade to re-check.)
- **Slice by version:** because every turn carries its versions, any drift can be traced to the
  prompt, model or curriculum change that caused it.

---

## 4. Latency

A spoken turn runs speech-to-text, UNDERSTAND (unless a fast path handles the reply), SymPy
grading, the engine, RESPOND (only for hints, follow-ups, explanations, reteaches and needs review,
with its board checks), then text-to-speech per segment. Every turn records milliseconds per stage
in `turns.latency_ms` (`stt`, `understand`, `grade`, `decide`, `respond`, `total`), each
text-to-speech call logs its own time, and the browser measures stop click → first audio and sends
it with the next turn as `prev_first_audio_ms`. A voice turn's `latency_ms` also holds two times the
browser sends with the recording: `mic_open_ms`, from the Speak click until the recording has really
started (the mic is opened afresh for every recording, and a student's first-ever click also
includes the browser asking them to allow the microphone), and `recording_ms`, from then until Stop
(or the 30 s limit).

**Measured: one scripted-mode session** (no LLM), 11 Oct 2026: 55 typed turns plus the Start,
through the real web server (one uvicorn worker, a SQLite file) on a Windows 11 laptop. It covered
all six concepts: teaching steps, right, wrong and unexplained answers, hints, "how did you get
that?" probes and questions, until every concept was mastered. From `turns.latency_ms`:

| Stage | Typical (p50) | Slow (p95) | Max | Turns where it ran |
| --- | --- | --- | --- | --- |
| Understand (scripted rules) | 0 ms | 0 ms | 15 ms | 55 |
| Grade (SymPy) | 0 ms | 16 ms | 16 ms | 27 |
| Decide (engine) | 0 ms | 0 ms | 0 ms | 55 |
| Respond (scripted words) | 0 ms | 0 ms | 16 ms | 55 |
| Server total for the turn | 0 ms | 16 ms | 16 ms | 55 |
| Request → reply at the client (HTTP, JSON and the database write included) | 23 ms | 33 ms | 38 ms | 55 |

Windows' monotonic clock ticks every 15.6 ms, so each stage reads 0 or 16 ms: "0 ms" means under
one tick. The client's round trip uses a finer clock and is the real total. On Linux, as in the
Docker image, the same code records exact milliseconds.

So the tutor's own work (parsing, SymPy, the engine and the database) costs tens of milliseconds,
and in LLM mode nearly all the time is model calls. **Those numbers come from the first session run
with the API key:** speech-to-text, UNDERSTAND, RESPOND and text-to-speech per stage, and stop click →
first audio from the browser. Until then the only model timings are single calls from a check on
11 Oct, not a session: speech-to-text 1.1–2.5 s, UNDERSTAND 1.4 s (`gpt-6-luna`) to 2.9 s
(`claude-haiku-5.5`, the primary) on a new connection, and text-to-speech 0.6–1.5 s per sentence.
Added up, a spoken answer would take roughly 3–7 s from stop click to first audio *(estimate)*, and
more when RESPOND also runs: above the target of p50 ≤ 2.0 s and p95 ≤ 3.5 s. If the measurements
confirm it, the first levers are a faster UNDERSTAND model (the evals compare models with
`--model-understand`) and streaming RESPOND.

What the student perceives, and what we do about it:

- **Show progress immediately:** the status line goes Opening the microphone… → Listening… →
  Thinking… → Speaking…, and the mic and text box are disabled while a turn runs. The Speak button
  reads "⏳ Opening mic…" until the recording has really started and only then turns into Stop, so
  the student doesn't start talking before the mic can hear them. After ~17 s with no reply the page
  says "That took too long — try again or type it", and nothing is resent.
- **Board and voice together:** each segment's board commands run as that segment's audio starts.
  If its audio isn't ready ~3 s after it's due, the browser's own voice reads it, so the board is
  never more than ~3 s behind the reply.
- **Speak while synthesising:** the audio for every segment is requested at once, so segment 2 is
  being made while segment 1 plays.
- **Skip work:** "ok", "next", "haan" and "say again" never reach a model, and scripted actions (the
  next step, a new question, praise, "how did you get that?") need no RESPOND. So an "ok" uses no
  model, a right answer one (UNDERSTAND), and a wrong answer or a question two. Audio is cached on
  disk by model, voice and text, so lesson steps and scripted lines play at once after the first time.
- **Short replies:** at most three segments of at most 60 words each.
- *(Designed)* Stream the LLM's output and check segments as they arrive; let the student interrupt.

---

## 5. Metrics

**The number I'd look at first: retained mastery** *(designed)*, meaning the share of concepts
marked mastered whose warm-up question is still answered correctly at the start of the next
session. Mastery alone can be inflated by a lenient tutor; retained mastery catches that, because
the student has to show the learning stuck. It needs a warm-up question at session start, which
isn't built yet.

Supporting numbers, each a query over the five tables or the eval and simulation results (no
dashboard is built):

| Area | Metrics |
| --- | --- |
| Learning | Mastery rate per concept; questions, hints and reteaches to mastery; needs-review rate |
| Tutor quality | False advance rate (simulations); eval pass rate; rejected first outputs and maths errors per 100 LLM replies; scripted fallback rate |
| Engagement | Session length; sessions per student per week; where students drop off (concept and step); next-day return |
| Experience | Stop click → first audio (p50, p95); "say that again" rate; how often students type instead of speaking |
| Cost | Cost per session and per mastered concept (from `calls`) |

Pairs keep each other honest: mastery with retained mastery, engagement with learning (a tutor
that entertains without teaching scores well on one and badly on the other), and cost with
time to first audio (cheaper models can't be allowed to make the tutor feel slow).

---

## 6. What's ugly

Known gaps, after the build:

- **Cut from the proposal:** the Jev router and its on-screen switch, pre-recorded audio clips, the
  PDF → curriculum pipeline, PIN sign-in, React, the LLM judge in evals, and a backup TTS model (the
  browser voice is the backup). **Designed but not built:** gradual rollout, step maps and progress
  migrations, several curriculum versions at once, alerts and dashboards, shadow checks, barge-in
  and streaming, retained mastery, and a duplicate-request guard.
- **Sign-in is weak.** Anyone with the access code can sign in as any username and see that
  student's progress. Fine for a review; real students would need a parent or school account.
- **Four formulas aren't checked.** The general formulas (standard form, the quadratic formula,
  D = b² − 4ac and the three cases of D) are hand-written LaTeX: SymPy can't check letters a, b, c, D.
- **Spoken maths isn't checked.** If the voice says "five" and the board says 12, nothing catches it.
- **"Answer revealed" is only checked for numbers.** For yes/no and nature-of-roots questions, and
  for "no real roots", a reply that gives the answer away in words isn't caught.
- **Working is judged present or absent, not checked.** "Because I factorised it" counts as working
  even if the factorising was wrong.
- **One trap answer can come from two different slips:** `minus_b` and a sign flip can give the same
  wrong roots, and so can `sign_flip` and brackets with the wrong signs, so a reteach may target the
  wrong slip.
- **Questions can repeat.** A long pass through a concept with fewer than 7 questions wraps around to
  questions the student has already seen.
- **`select` and `new` board lines are trust points.** SymPy confirms a kept root was a root, not
  that the reason for dropping the other is sound.
- **Scripted mode is cruder than the LLM** (it's the fallback and the mock): it spots mistakes only
  through trap answers, so it hints where the LLM might reteach; any "?" makes a reply a question,
  even "did India win yesterday?"; a second reply isn't merged with the first ("0 too" after "3");
  and mixed replies are read roughly. Without a key, students have to say the answer first.
- **The keyword safety check has rare false alarms and misses.** `app/safety.py` reads 7 or more
  digits, in groups split by single spaces or hyphens, as a phone number when every group has 2 or
  more digits ("98765 43210", "011 2345 6789") or every group is one digit, as when a number is said
  one by one ("nine eight seven six five four three"). A list that mixes the two, like the factor
  pairs "1 12 2 6 3 4", is maths. False alarms remain: "12 13 14 15", a hyphenated date like
  "11-10-2026", 7 or more digits said one by one, "my head is hurting me", "my friend beat me to it"
  and "honestly beats me" (any word before "beats me" except it, that, this or question counts as a
  person, so that "Rohan beats me" is caught). Misses remain too: "He gets drunk. Beats me.", numbers
  said with "double" or "oh", and a Devanagari word before "beats me". With the LLM, UNDERSTAND's
  safety flag is the second net; in scripted mode "beats me" isn't read as "I don't know", so the
  tutor asks again instead of giving a hint.
- **One process only.** The breakers live in memory and SQLite wants one writer, so the app runs one
  uvicorn worker. More traffic means Postgres and shared breakers first.
- **No duplicate-request guard.** A request sent twice would apply twice. The page never resends by
  itself, so this only matters if automatic retries are added.
- **Push-to-talk only.** The student can't interrupt the tutor mid-sentence.
- **Small, synthetic test sets:** 14 eval scenarios and 4 simulated students, and the simulated
  students are LLMs with their own biases. Real student sessions are the missing check.
- **Hinglish speech is untested.** Two eval scenarios type Hinglish, but no Hinglish recording has
  been through speech-to-text, and the voice ("sarah", chosen by listening) isn't Indian English.

With two more days: measure a real voice session and act on it, stream LLM output into segments,
add barge-in, check a student's spoken working for the exact slip, and run ten real students
through concept 3 to see what UNDERSTAND gets wrong on real speech.
