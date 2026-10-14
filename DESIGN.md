# DESIGN.md — AI tutor for quadratic equations (NCERT Class 10)

Anything designed but not built is marked *(designed)*. Numbers are measured unless marked *(estimate)*.

**The core rule:** plain code decides every teaching move; the LLM only understands what the student said and
phrases the tutor's help; SymPy checks the maths. State lives in the database, never in an LLM conversation.
Most of what follows depends on that.

**A lesson:** a topic opens with why it's useful (a short real-life problem), then teaching steps that each end
on a check-in ("Did that make sense?"), then questions. The rules (`engine.py`, `sessions.py`: R0–R10, S0–S2)
pick one move per reply: next step, solved example, hint, reteach, credit, review. Each move has a reason, shown in the page's
"Why?" panel. Buttons and common phrases ("ok", "not sure", "test me") skip the LLM entirely.

**What "checked" means.** The headline promise is narrower than "all the maths is checked", and this is exactly it:

| Checked in code before the student sees it, or before it counts | Not checked |
| --- | --- |
| Every equation, root and number the LLM writes on the board: each line against the line above or the question (SymPy) | What the voice says, apart from "no LaTeX, no spoken '=' or minus signs" |
| Every answer key: computed by SymPy at startup, never typed in; the app won't start if a lesson fails | Whether the student's working is *right*: only whether it is there |
| The student's answer: the LLM pulls it out of the words; SymPy grades it | A mistake UNDERSTAND names while teaching (no question is open to grade) |
| No answer given away: numbers in a reply against the open question's key; no example built on any topic's question | An answer given away *in words* (yes/no, "equal roots", "no real roots") |
| | The four general formulas, written by hand; `select` lines (the kept root is checked, the reason for dropping the other isn't) |

---

## 1. Data model

SQLAlchemy Core with a `DATABASE_URL`: SQLite locally, Postgres (Neon) for the live site, same code. Ids are
`uuid4` hex; times are UTC; JSON is text in SQLite and `JSON` in Postgres.

```sql
CREATE TABLE students (
  id          TEXT PRIMARY KEY,
  username    TEXT NOT NULL UNIQUE,   -- sign-in name
  first_name  TEXT NOT NULL,          -- for greetings; no surname, email or age: the users are minors
  created_at  DATETIME
);

CREATE TABLE sessions (                -- a new one at Start after a 30-minute gap
  id                  TEXT PRIMARY KEY,
  student_id          TEXT REFERENCES students(id),
  session_no          INTEGER,
  curriculum_version  TEXT,           -- lesson files' version at session start
  prompt_versions     JSON,           -- {"understand": "understand.v4", "respond": "respond.v7"}: used by every turn
  models              JSON,           -- role -> models, also used by every turn; {"mode": "scripted"} without a key
  board               JSON,           -- the whiteboard now, so a reload or Continue redraws it
  started_at          DATETIME,
  UNIQUE (student_id, session_no)
);

CREATE TABLE progress (                -- one row per student per concept: the current state
  student_id          TEXT,
  concept_id          TEXT,           -- stable ids: standard_form, roots, factorisation, ...
  status              TEXT,           -- not_started | teaching | checking | paused | mastered | needs_review
  step_id             TEXT,           -- the teaching step, by id; empty at a topic's start
  points              FLOAT,          -- clean answers in a row (section 5); mastered at 2
  pending             JSON,           -- the open question: check_id, hint_level, awaiting_working,
                                      -- followup_used, partly_right, last_answer
  last_check_id       TEXT,           -- the last question asked; the next follows it in file order
  reteach_count       INTEGER,        -- at most 3 per pass through a concept
  strategies_tried    JSON,           -- ["worked_example", "substitute_back"]
  checks_seen         JSON,           -- questions asked in this pass: none is asked twice
  curriculum_version  TEXT,
  updated_at          DATETIME,
  PRIMARY KEY (student_id, concept_id)
);

CREATE TABLE turns (                   -- append-only: one row per reply, and per Start / Continue
  id            TEXT PRIMARY KEY,
  session_id    TEXT REFERENCES sessions(id),
  turn_no       INTEGER,
  concept_id    TEXT,
  phase         TEXT,                 -- teaching | checking
  input         TEXT,                 -- voice | text | start
  student_text  TEXT,                 -- the transcript; no audio is stored
  understood    JSON,                 -- UNDERSTAND's reading: reply type, answer, working shown, mistake, safety
  grade         JSON,                 -- SymPy's verdict and the trap answer it matched
  tutor_output  JSON,                 -- segments spoken and drawn, the board they began on, the buttons
  versions      JSON,                 -- what this turn actually used: curriculum and prompt versions
  latency_ms    JSON,                 -- per stage, plus the browser's mic and first-audio times
  calls         JSON,                 -- per model call: role, model, ms, outcome, retry, backup, tokens, cost
  issues        JSON,                 -- rejected outputs and why, fallbacks, lesson changes, safety reviews
  created_at    DATETIME
);

CREATE TABLE decisions (               -- append-only: the "why" behind every move, one per turn
  id             TEXT PRIMARY KEY,
  turn_id        TEXT REFERENCES turns(id),
  student_id     TEXT,
  concept_id     TEXT,
  rule           TEXT,                -- R0..R10, S0..S2, END, LIMIT
  action         TEXT,                -- next_step | example | credit | hint | reteach | needs_review | ...
  reason         TEXT,                -- from a template in code, never an LLM; safe to show
  evidence       JSON,                -- what the rule saw, answer key included: never sent to the browser
  hint_level     INTEGER,
  reteach_count  INTEGER,
  strategy       TEXT,                -- worked_example | substitute_back | compare_non_example | replay
  created_at     DATETIME
);

CREATE TABLE stops (                   -- a reply the student cut short (Stop, Esc, or answering)
  turn_id     TEXT PRIMARY KEY REFERENCES turns(id),
  student_id  TEXT,
  heard       INTEGER,                -- segments said in full
  shown       INTEGER,                -- segments on screen
  created_at  DATETIME
);
```

Why it looks like this:

- **History and state are separate.** `turns` and `decisions` are append-only history; `progress` is the current
  state. One transaction per turn writes progress, turn, decision and board, so a turn that fails halfway changes
  nothing.
- **One progress row per concept,** not a blob per student, so "how many students are stuck in factorisation?"
  is one query.
- **Positions are ids, not numbers.** `step_id`, `pending.check_id` and `checks_seen` name steps and questions,
  so editing a lesson file can't silently move a student (section 2).
- **Versions are stamped twice:** on the session (what it runs on, to its end) and on every turn (what that turn
  used). Every metric can be sliced by prompt, model or lesson version.
- **Reasons are safe to show; evidence isn't.** The "Why?" panel shows the reason while the question is still
  open; the answer key stays in `evidence`.
- **No audio, nothing personal beyond a first name.** Transcripts are enough for debugging and evals.

---

## 2. Iterability

The LLM holds no state, so a change can't corrupt a student. The only question is *when* a student sees it.

**Prompt or model change: a session finishes on what it started with.** Prompts are versioned files that are
never edited once used (`app/prompts/understand.v1–v4.md`, `respond.v1–v7.md` are all still there). A new
session saves its prompt versions and models (`sessions.prompt_versions`, `sessions.models`), and every turn
loads them from the session, not from the current config (`app/rollout.py`). So the student halfway through
session 3 stays on the old version until that session ends (the next Start after a 30-minute gap); session 4
gets the new one. The turn's `versions` and `calls` record what it really used. (Speech-to-text and
text-to-speech models aren't pinned: they carry no teaching behaviour.)

**Rolling a change out:**

1. Write a new prompt file, e.g. `respond.v8.md`.
2. Gate it offline: `uv run python -m evals --prompt-respond respond.v8` runs the 24 tutor scenarios, 3 runs
   each. A scenario passes on a majority of runs, and a red scenario blocks the change unless the live version
   fails it too. A run also fails if the first output was rejected, so the safety net can't hide a bad prompt.
   `uv run python -m sims` runs simulated students for false advances over long conversations.
3. Stage it: set `ROLLOUT = {"id": "respond-v8", "percent": 10, ...}` in `config.py`. A student is in the share
   when `sha256(rollout id + student id) % 100 < percent`: stable for that student, and a new rollout id draws a
   new share. Compare the two versions with a query on `turns.versions`, then go to 50% and 100%. Finally make
   it `PROMPT_VERSIONS` and remove the rollout.
4. Roll back: `percent: 0` stops new sessions getting it. In an emergency, `RETIRED_PROMPTS = {"respond.v8"}`
   moves sessions already on it to the live version on their next turn. That's safe because no state lives in
   the prompt.

How the prompts actually iterated (3 runs per scenario; one or two scenarios of difference is noise):

| Prompts | Eval scenarios passed | What changed |
| --- | --- | --- |
| understand.v1 + respond.v1 | 12 of 17 | First run with a key. Found that UNDERSTAND "corrected" a wrong answer; now the student's own words are graded |
| understand.v2 + respond.v3 | 14, then 12 of 17 | A "check before you reply" list for the board rules broken most |
| understand.v4 + respond.v5 | 19 of 22 | Check-ins, solved examples, questions about one part of a step |
| understand.v4 + respond.v6 | 21, then 20 of 22 | Example equations from a checked list; first outputs rejected 10 → 5 of 66 turns |
| understand.v4 + respond.v7 (live) | 22 of 24 | Find-k questions (NCERT Ex 4.3 Q2) |

**Curriculum change.** Six hand-written YAML lesson files (`curriculum/ch4/`) hold each topic's steps, 38
questions with hints and trap answers, known mistakes and reteach strategies. They are validated at startup (schema, ids,
every answer key, every board line, spoken text), and the app refuses to start on a broken lesson. Ids are
stable keys, and a change applies from the next turn:

| Change | What happens to the session-3 student |
| --- | --- |
| Wording, a hint, a new question | Nothing moves; the next turn uses it |
| Their step removed or renamed | Back to that concept's first step |
| Their open question removed | Their reply isn't marked; a new question is asked (R8, "the lesson changed") |
| Concepts split, merged or renamed | *(Designed)* a migration script maps progress (mastered → both mastered) |

Points are kept, and the turn records a `curriculum_changed` issue (`sessions.reconcile`). Unlike prompts,
lessons are **not** pinned to the session: only one lesson version is loaded. *(Designed)* Keep the previous
lesson version loaded, so a session finishes on it the way it does on its prompts, plus a step map (old step →
new step) to move students more gently than "back to step 1".

---

## 3. Observability

**Per turn**, one `turns` row, one `decisions` row and one JSON log line (stdout, so Render's logs) with the same
fields: session, turn and student ids; versions; phase; UNDERSTAND's reading; SymPy's verdict; the rule and
reason; milliseconds per stage; each model call's model, time, outcome, retry, backup, tokens and cost; and
issues. An issue is a rejected model output with its exact problems (bad JSON, bad maths, an answer revealed,
LaTeX in speech), a fall back to scripted words, a lesson change, a safety review or an internal error. A
safety review or a model's breaker opening also logs a warning.

**Alerts** *(designed, not built; thresholds to tune)*. Each is a query over `turns`:

| Signal | Alert when |
| --- | --- |
| Safety reviews | Any: notify a person immediately |
| Provider errors | Over 5% of calls fail in 5 minutes, or a breaker opens |
| Scripted fallbacks | Over 5% of turns in an hour |
| Rejected first outputs | Over 10% in an hour (today's baseline is about 8%) |
| Time to first audio | p95 above 3.5 s for 15 minutes |

**Finding out the tutor got worse before a student says so:**

- **Caught problems are counted, not hidden.** The safety net (one retry, then scripted words) keeps bad replies
  from students, but every catch is an issue on the turn. A rising rate shows a prompt or model drifting while
  students see nothing wrong.
- **The decision mix drifts first.** More R1 credits means the tutor got lenient; more reteaches per concept
  means it is harsh or confusing; more R8 "say that again" means speech-to-text or the phrasing is failing;
  more solved examples means a lesson step isn't landing.
- **Evals gate every change** (section 2). *(Designed)* The same suite nightly against the live config, and a
  simulation batch reporting the false advance rate.
- *(Designed)* **Shadow reading:** re-run a sample of live turns through UNDERSTAND on a stronger model; a
  rising disagreement rate means the live model drifted.
- **Slice by version.** Every turn carries its versions, so any drift traces to the change that caused it.

---

## 4. Latency

A spoken turn is: mic open → recording → speech-to-text → UNDERSTAND (skipped by the fast paths) → SymPy and
the engine → RESPOND (only for hints, explanations, examples and reteaches) → text-to-speech per segment. Each
turn records `turns.latency_ms`: `stt`, `understand`, `grade`, `decide`, `respond`, `total`, and from the
browser `mic_open_ms`, `recording_ms` and `prev_first_audio_ms`, the time from the Stop click to the first
audio of the reply.

**Measured live, 14 Oct:** one session of 18 spoken turns on the live site (Render free tier, Neon, OpenRouter),
from `turns.latency_ms`. The target is p50 ≤ 2.0 s and p95 ≤ 3.5 s from Stop to first audio.

| Stop click → first audio | Typical (p50) | Slow (p95) | Turns |
| --- | --- | --- | --- |
| All spoken turns | **4.5 s** | **13.6 s** | 18 |
| No model needed ("okay", "go on") | 2.8 s | 3.2 s | 4 |
| UNDERSTAND ran | 5.7 s | 13.6 s | 13 |
| RESPOND ran (the waiting line is spoken first) | 1.9 s | | 1 |

| Stage | Typical (p50) | Slow (p95) | Notes |
| --- | --- | --- | --- |
| Mic open | 56 ms | 59 ms | Kept warm; 555 ms the first time |
| Speech-to-text | 1.2 s | 1.9 s | 18 calls |
| UNDERSTAND (claude-haiku-5.5) | 2.8 s | 11.1 s | 4 of 13 calls over 7 s; 51 calls on the live site that afternoon: p50 2.3 s, p95 10.9 s |
| Our own code: parsing, engine, database | about 30 ms | | No-model turns: the server's time minus speech-to-text |
| Upload, text-to-speech, start of playback | 1.1 s | 2.1 s | First audio minus the server's time |

What that says:

- **Speech alone misses the target.** With no model at all, speech-to-text plus text-to-speech already take
  2.4–3.2 s.
- **UNDERSTAND's slow tail is the worst part.** It is usually 2–3 s (1.7 s typical in the 12 Oct evals), but a
  third of calls took 7–11 s that afternoon, the same hour 14 of ~70 simulation calls timed out. A 13 s wait is
  what students will remember.
- **The waiting line works.** A RESPOND turn that took the server 6 s had its first audio at 1.9 s.

**What the student perceives, and what we do about it**, most effective first:

- **Skip the models.** Buttons and listed phrases ("ok", "not sure", "say again", "skip to factorisation") use
  no model, and scripted moves (next step, next question, praise, "how did you get that?") need no RESPOND.
  So while teaching an "ok" costs nothing, a right answer one call, a wrong answer two.
- **Fill the wait.** Just before RESPOND runs (2–6 s), the server streams a short line first, and the page says
  it in the tutor's voice: "Sure, let me think of one." It saves no time; it makes the wait sound like thinking.
- **Show progress at once.** Opening the microphone… → Listening… (with a soft tick when the recording has
  really started) → Thinking… → Speaking….
- **Keep the mic warm.** After the first answer the mic stays on for up to a minute, so Speak records at once
  (`mic_open_ms` 55–60 ms instead of up to a second). Not on phones, which lower the tutor's voice while the mic is on.
- **Synthesise in parallel and cache.** Every segment's audio is requested at once, so segment 2 is made while
  segment 1 plays, and audio is cached by model, voice and text, so lesson lines play instantly after the first time.
  If a segment's audio is ~3 s late, the browser's own voice reads it, so the board never waits.
- **Keep checks cheap.** Comparing a reply's lines with all 38 questions took 2.1 s on a laptop and about 20 s on
  Render's free tier (a tenth of a CPU). A numeric pre-check (`board._could_be_same`) brought it to 0.19 s.
  Lesson learned: time CPU-bound changes on the slow host.
- **Give up on a stalled model sooner.** In that afternoon's simulation logs every slow call looked the same: the
  primary model hit the 8 s timeout, then the backup answered in 2–5 s. So UNDERSTAND now switches to its backup
  after 4 s and RESPOND after 5 s (`ROLE_TIMEOUT_S`). Re-tested live the same evening (11 spoken turns): three
  UNDERSTAND calls hit the 4 s cut, the backup answered in 1.5–2.7 s, and those turns took 5.5–6.7 s instead of
  10–13 s. One wrong answer still took 15 s: RESPOND's primary stalled for 8 s (before its cut was lowered), its
  backup ran out of the turn's 15 s, and a scripted hint was used. The primary stalled on 4 of 13 calls that evening
  and on none of 464 on 12 Oct, so this is the provider's bad day, but the tutor has to survive one.
- *(Designed)* **Next levers, by what the numbers say:** a hedged request (if the primary hasn't answered in
  ~2.5 s, also ask the backup and take whichever answers first), the real fix for a stalling provider; a short
  "Mm, let me check" when UNDERSTAND is slow,
  as the waiting line does for RESPOND; a faster UNDERSTAND model (the evals compare models with
  `--model-understand`); streaming speech-to-text; stream RESPOND; barge-in.

---

## 5. Metrics

**The number I'd look at first: retained mastery** *(designed)*: the share of mastered concepts whose warm-up
question is still answered cleanly at the start of the next session. Mastery alone can be inflated by a lenient
tutor; retained mastery can't, because the learning has to survive a break. It needs a warm-up question at
session start, which isn't built.

**Mastery has to mean something first, so the rule is now strict** (built, `engine._credit`). A concept is
mastered after **two clean answers in a row**. A clean answer is:

- **On a fresh question,** one not asked before in this pass (`progress.checks_seen`). When every question has
  been asked, the concept goes to review instead of round again.
- **Independent:** right first time, with no hint and no "partly right" follow-up on that question. A right
  answer after help moves on but scores nothing.
- **With working.** A bare right answer gets "how did you get that?" and counts only once the working comes.
  The working is judged present, not checked (see "What's ugly").

Any hint, wrong answer or reteach sets the run back to 0. Two bare guesses can't pass: a bare answer earns
nothing until the working comes, and one slip restarts the run. Two lucky guesses with made-up working still
could, because working is only judged present ("What's ugly" 1). One trade-off is deliberate: a question to the tutor
while a question is open ("why do we split the middle term?") doesn't break the run. RESPOND never gives the
answer away (checked for numbers), and students shouldn't be penalised for asking.

**Supporting numbers.** Each is a query over the tables or the eval and simulation results (no dashboard):

| Area | Metrics |
| --- | --- |
| Learning | Mastery rate per concept; questions, hints and reteaches to mastery; needs-review rate |
| Tutor quality | False advance rate (simulations); eval pass rate; rejected first outputs per 100 LLM replies; scripted fallback rate |
| Engagement | Session length; sessions per student per week; where students drop off (concept and step); next-day return |
| Experience | Stop → first audio (p50, p95); "say that again" rate; how often students stop the tutor, and where; typing instead of speaking |
| Cost | Cost per session and per mastered concept (from `calls`) |

Pairs keep each other honest: mastery with retained mastery, engagement with learning (a tutor that entertains
without teaching scores well on one, badly on the other), cost with time to first audio.

**Simulations, 14 Oct** (four LLM-played students on factorisation, `uv run python -m sims`; transcripts in
`sims/examples/`). The same run before and after the rule above:

| Student | Old rule: outcome, reteaches | New rule: turns | New rule: reteaches | New rule: outcome |
| --- | --- | --- | --- | --- |
| Fast learner | mastered, 0 | 3 | 0 | mastered (2 clean answers) |
| Guesser | mastered, 0 | 10 | 0 | mastered: both bare answers got "how did you get that?" first |
| Sign-flipper | mastered after wrong, right, wrong, wrong, right; 3 | 18 | 1 | mastered after a reteach, then 2 clean answers in a row |
| Overconfident | mastered, 1 | 12 | 3 | needs review |

What it showed:

- **The rule does what it says.** Under the old rule the sign-flipper was mastered while still flipping signs.
  Under the new one every slip restarted its run, and it was mastered only after two clean answers in a row.
- **The bigger problem is now reading, not passing.** 3 of the 4 reteaches came after a *right* answer: UNDERSTAND
  read the working as the answer ("the numbers are minus 2 and minus 3 … so x is 2 or 3" became "-2, -3").
  It sent the overconfident student to review. A stricter rule makes a misread cost more, so this is the next
  fix ("What's ugly" 1).
- **"False advances" (3) are partly the simulator's own drift:** the simulated students answered right, with
  correct working, while their script said they still held the mistake.
- OpenRouter was slow during the run: 14 of ~70 calls timed out, and 4 replies fell back to scripted words
  (cost about $0.04).

---

## 6. What's ugly

Ranked by how much they could hurt a student:

1. **Spoken maths and working aren't checked.** If the voice says "five" while the board says 12, nothing
   catches it. "Because I factorised it" counts as working even when the factorising was wrong. And UNDERSTAND
   can read working numbers as the answer: "the numbers are minus 2 and minus 3 … so x is 2 or 3" was graded as
   "-2, -3" and retaught. In the 14 Oct simulations, 3 of 4 reteaches followed a right answer misread this way.
2. **While teaching, UNDERSTAND is trusted.** A mistake it names isn't verified, because no question is open
   for SymPy to grade. In a simulation it told a correct student, twice, that their sign was wrong.
3. **The voice loop is slow:** measured live, 4.5 s typical and 13.6 s slow from Stop to first audio, against
   a 2 s target. Speech-to-text and text-to-speech alone take about 2.5 s, and UNDERSTAND has a long tail. There is no
   streaming, and no interrupting the tutor mid-sentence.
4. **Small, synthetic test sets:** 24 eval scenarios, 4 simulated students (LLMs, with their own biases and
   drift), no real students, and no Hinglish recording through speech-to-text yet.
5. **RESPOND's first output is still rejected about 1 time in 13** (5 of 66 eval turns). Students never see a bad
   line, but each rejection costs about 2 s, and reteaches, which write the most maths, fail most.
6. **Lessons aren't pinned to a session** (section 2), and with 5–8 questions per topic a student in a second
   pass sees questions from the first.
7. **The keyword safety check** (`app/safety.py`) has known false alarms ("I got hit by the ball") and misses
   ("He gets drunk. Beats me."). UNDERSTAND's safety flag is the second net; scripted mode has only the keywords.
8. **Speech-to-text mishears short replies,** especially when their start is cut off ("teach me" → "Ичми").
   Unplaceable or foreign-script replies get "Sorry, I may have misheard you"; a mishearing that reads as a real
   answer is taken at its word. A vocabulary hint fixed most cut-offs but invented "Teach me." from near-silence,
   so it was dropped.
9. **One process.** Model breakers live in memory and there's no duplicate-request guard, so the app runs one
   worker. The page never resends by itself.
10. **Sign-in is a shared access code** plus a username. Fine for a review; real students need accounts.

**Designed but not built:** alerts and dashboards, retained mastery's warm-up question, shadow reading, lesson
pinning and progress migrations, streaming and barge-in. **Cut from the proposal:** pre-recorded audio, the
PDF → curriculum pipeline, an LLM judge in evals, React.

**With two more days:** check the student's spoken working with SymPy (fixes 1 and part of 2), stream RESPOND
segment by segment, build the warm-up question so retained mastery can be measured, and put ten real students
through factorisation to see what UNDERSTAND gets wrong on real speech.
