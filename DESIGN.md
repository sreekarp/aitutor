# DESIGN.md — AI tutor for quadratic equations (NCERT Class 10)

> Rewritten after the build: it describes what is in this repo. Anything designed but not built is
> marked *(designed)*. Numbers are measured unless marked *(estimate)*.

The core rule behind every section below: **plain code decides every teaching move, the LLM
understands what the student said and helps them fix their mistake in words, and SymPy checks every
piece of maths on the board before a student sees it.** State lives in the database and the engine,
never in an LLM conversation, which is what makes the rest of this document possible.

**How a lesson goes.** The rules are in `engine.py` (after a reply) and `sessions.py` (Start, Continue,
a new topic); every turn saves its rule and reason, and the page's "Why?" panel shows them. While
teaching, every reply ends on a check-in question, and every reply comes with buttons for the usual
answers. A button sends the words the student could type or say, and the keyword rules read them, so
reading a tap needs no model call (only a solved example calls RESPOND).

| Where the student is | They say, or tap | What happens |
| --- | --- | --- |
| A topic's start: its intro, then "Shall I teach it? Or say 'test me' if you already know it." | "test me", "quiz me", "I already know this" (**Test me first**) | R6: the questions start now |
| | Anything else: "teach me", "ok", "no", a question (**Teach me**) | R0: step 1. An unclear reply gets R8: "Sorry, I may have misheard you..." (spoken) or "Sorry, I didn't follow that." (typed), then just the offer again. A failed recording gets only "Sorry, I didn't catch that..." (with the offer's buttons) |
| A step, ending "Did that make sense? Shall we go on?" (the last step: "Ready for a question?") | "yes", "yeah", "sure", "go ahead", "ok", "got it", "understood", "move on" (**Yes, go on**, **Yes, quiz me**) | R0: the next step, or after the last one the first question |
| | "not sure", "no", "nope", "not really", "didn't get it", "another example" (**Not sure**) | R7 `example`: RESPOND solves a similar equation on the board (SymPy checks each line; never a question from any topic, which would give a later answer away), then "Shall we go on, or shall I do another example?" (**Go on**, **Another example**). Scripted: "Let's go over that step again." and the step is replayed |
| | "test me", "skip to the questions" | R6: the questions start now; the other steps are skipped |
| | A question, "I don't know" | R7: explained; same step |
| A question is open (**Not sure**) | An answer | SymPy grades it: R1 credit, R2 "how did you get that?", R3 reteach, R4 hint or R5 follow-up |
| | "not sure", "I don't know", "hint" | R4: the next hint |
| | "got it", "test me" | R6: "show me", because points need an answer with working |
| | "move on" | R8: "Let's get this one first", and the question again |
| Anywhere | "Can we skip to the factorization topic?", "move on to the discriminant" | S2: that topic, as if it was picked in Topics |
| | "say again", "pardon?", "can you repeat that?" | R8: the last reply again, a hint or an explanation included. While teaching it ends on its check-in (after a "sorry", the whole step); while a question is open, on the question (after a greeting or a "sorry", just the question) |
| Right after the student stopped the tutor part-way | "repeat", "say again" (**↻ Repeat**); "continue", "carry on", "resume" (**▶ Continue**) | R8: the stopped reply again, from its start or from the line it stopped on, with its board and its buttons. Any other reply moves on as usual, and the part not said is dropped |

"Yes", "yeah", "sure", "ok", "haan", "no", "nope" and "not really" (and "that makes sense") are read this way only
while teaching: while a question is open they are answers, because several questions are yes or no. (The full list
is under "Skip work" below.) A spoken topic switch has to start the reply: only a
few polite lead-in words (ok, so, please, can we, I want to, let's) may come before a move word (skip, go, move, jump,
switch, change, take me), then "to" and a topic's name from the `aliases` in its lesson file (unique,
checked at startup), at the end of the reply; only a few filler words (topic, chapter, lesson, please, now...;
`TOPIC_TAIL` in `fallback.py`) may follow the name. Free text before the move word ("I'm stuck, skip to
roots") goes to UNDERSTAND as usual. The switch is read after the safety check and before the keyword rules
and UNDERSTAND, so "move on to the discriminant" switches topics instead of moving on. A safety reply, the
day's limit and the end of the lessons get no check-in and no buttons, and "say again" after a safety reply
says it again as it was, also with none. A lone "Sorry, I didn't catch that" asks for the reply itself, so it ends
on no check-in, but it shows the buttons of where the lesson stands (the step's, the offer's, or **Not sure** with a
question open). Right after a safety reply the student had no buttons, so these are the first ones they get back, and
a reload draws the same. On the page, a click on a topic only chooses it, and a Start button opens it (the big button
before a lesson, one under the topic list during it). While the tutor speaks every control works: **Stop** (or Esc)
silences it, and sending an answer (a reply button, or typing and Send), Speak or a topic's Start cuts it short.
While each part is said, the board lines it writes or points at are lit; a replay ("say again") points at the
lines still on the board instead of writing them again, and draws again any that are gone.
The part not yet said is never shown: the page tells the server how many segments were said and shown (kept in
`stops`; closing or reloading the page mid-reply counts too), and the saved board goes back to what was on screen.
Stop (or Speak) adds **↻ Repeat** and **▶ Continue** before the reply's own buttons. If the open question was in the
part never shown, any other reply hears the rest first, since a question can't be answered unseen (a safety reply
and a topic asked for still come first). Start or Continue later picks the reply up: within 30 minutes (S1) from
the line it stopped on, in a new session (S0) from its start, on the board it began on. (Not when the stopped reply
was a Start itself, the safety reply, the daily limit or the end, or when the new session changed progress: then the
lesson resumes as usual.) Only while the page waits for the server (or
records) are the controls off, so a turn is never sent twice, and the next turn waits for the stop report, so
"continue" always finds it. *(Designed)* Fairer passing: two clean
answers in a row
to master a topic, a **Move on** button that passes it with a review question next session, and a
"test me" slip sending the student to step 1.

---

## 1. Data model

SQLite, through SQLAlchemy Core with a `DATABASE_URL` setting, so the same six tables run on
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
  status              TEXT,           -- not_started | teaching | checking | paused | mastered | needs_review
                                      -- (paused: the student picked another topic; everything else is kept)
  step_id             TEXT,           -- the teaching step shown, by id, e.g. "split_middle"; empty at
                                      -- a topic's start, before step 1 (its intro and the offer)
  points              FLOAT,          -- +1 per right answer with working, +0.5 after a hint; mastered at 2
  pending             JSON,           -- the open question: check_id, hint_level, awaiting_working,
                                      -- followup_used, last_answer. Set while status = checking, and
                                      -- kept while a topic left mid-question is paused
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
  tutor_output  JSON,                 -- the segments spoken and drawn (speech + board commands), the
                                      -- board they began on, and the buttons after them
  versions      JSON,                 -- what this turn used: curriculum, understand and respond prompts
  latency_ms    JSON,                 -- per stage, plus the browser's prev_first_audio_ms, and for
                                      -- voice turns its mic_open_ms and recording_ms
  calls         JSON,                 -- per model call: role, model, ms, outcome, retried,
                                      -- backup_used, tokens, cost; speech-to-text also keeps
                                      -- seconds, the clip's length, when OpenRouter sends it
  issues        JSON,                 -- rejected outputs and why, fallbacks, lesson changes, safety
  created_at    DATETIME
);

CREATE TABLE decisions (               -- append-only: the "why" behind every move, one per turn
  id             TEXT PRIMARY KEY,
  turn_id        TEXT REFERENCES turns(id),
  student_id     TEXT,
  concept_id     TEXT,
  rule           TEXT,                -- R0..R10, S0, S1, S2 (a topic picked or asked for), END, LIMIT
  action         TEXT,                -- next_step | example | credit | hint | reteach | needs_review | ...
  reason         TEXT,                -- built from a template in code, never by an LLM
  evidence       JSON,                -- what the rule saw, answer key included: never sent to the browser
  hint_level     INTEGER,
  reteach_count  INTEGER,
  strategy       TEXT,                -- worked_example | substitute_back | compare_non_example | replay
  created_at     DATETIME
);

CREATE TABLE stops (                   -- a reply the student stopped part-way (Stop, Esc, or answering)
  turn_id     TEXT PRIMARY KEY REFERENCES turns(id),
  student_id  TEXT,
  heard       INTEGER,                -- how many of its segments had been said in full
  shown       INTEGER,                -- how many were on screen (the one being said too)
  created_at  DATETIME
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
- **A stop is a row of its own,** so `turns` stays append-only, and "how often do students stop the tutor, and
  where?" is one query.
- **Every new question, new topic and new session starts on a clean board,** so a question never
  shares the board with an earlier one's working; a hint, an explanation or a re-ask of the same
  question keeps the board.
- **Versions are stamped twice:** on the session (what was configured when it started) and on
  every turn (what that turn actually used), and every model call in `calls` names its model. Any
  metric can be sliced by prompt, model or curriculum version.
- **Reasons are safe to show; evidence isn't.** The "Why?" panel shows rule and reason, which never
  contain the answer key, often while the same question is still open. The key stays in `evidence`.
- **No raw audio and no personal data beyond a first name.** The users are minors; transcripts are
  enough for debugging and evals. (The audio cache on disk holds only the tutor's own lines.)
  Nothing is recorded or sent unless Speak is pressed, and nothing is recorded in a hidden tab. The
  page keeps the mic on between answers (the browser shows its mic indicator meanwhile), and
  switches it off when Speak isn't pressed within a minute of the student's turn starting, when the
  tab is hidden or left, at sign-out, and after every recording on a phone or tablet.
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
2. `uv run python -m evals --prompt-understand understand.v2 --prompt-respond respond.v2` (or
   `--model-respond <id>`) runs the 22 tutor scenarios against the new version, 3 runs each. A
   scenario passes with a majority of passing runs; a red scenario blocks the change unless the
   version in use fails it too.
   `uv run python -m sims` shows false advances over longer conversations.
3. Switch `PROMPT_VERSIONS` (or `MODELS`) in `config.py` and deploy.
4. Rollback is the same switch back. Because state isn't in the prompt, students mid-session move
   back on their next turn.

**The first runs with the key, 12 Oct 2026** (17 scenarios × 3 runs each; results in `evals/results/`):

| Prompts | Scenarios passed | First RESPOND output rejected | Fell back to scripted words |
| --- | --- | --- | --- |
| understand.v1 + respond.v1 | 12 of 17 | 37% | 3 of 27 |
| understand.v2 + respond.v2 | 14 of 17 | 30% | 3 of 27 |
| understand.v2 + respond.v3, two runs | 14, then 12 of 17 | 30%, then 40% | 5 of 30, then 3 of 30 |

They found a real bug: UNDERSTAND wrote down the right roots when the student said wrong ones, so a
sign flip earned a point (fixed; see "What's ugly"). respond.v3 adds a short "check these before
you reply" list for the board rules broken most, and the line "x - 2 = 0 or x - 3 = 0", which
the board now accepts. With it the ± errors stopped and the sign-flip reteach passes, so config uses
understand.v2 + respond.v3. The same prompts scored 14, then 12: at 3 runs a difference of one or two
scenarios is noise, so a closer call needs `--runs 5` or more.

The check-ins and buttons ("How a lesson goes", at the top) needed new versions: `understand.v3.md` adds
three reply types (`test_me`, `not_sure`, `move_on`) and reads "got it" while teaching as "go on", and
`respond.v4.md` adds the `example` action, a solved example while teaching. Three scenarios were added for
them, and `claims_understanding` now expects the next step instead of the questions.

Questions about one part of a step ("can you repeat the last example?") needed two more:
`understand.v4.md` reads them as a question, not "say again" (which still means all of it, and "repeat
the question"), and `respond.v5.md` highlights any board line it talks about and answers about one part
or example on its own. RESPOND's facts list the step and its own last reply part by part: the example
each part belongs to (a line with relation "new" starts the next) and its lines still on the board.
Two scenarios were added: `ask_about_the_last_example` (it must highlight that example's lines) and
`repeat_the_question`. Config uses understand.v4 + respond.v5. Their first full run with the key (13 Oct) passed
19 of 22. The three failures are RESPOND formatting slips (a ± line it couldn't read, digits in a words line); the
same scenarios failed with v3 + v4 too. Two fixes came from those runs: the facts now say outright which example is
the last (left to work it out, the model explained the first one), and a highlight part KaTeX can't mark on its
own lights the whole line instead of rejecting the reply ("-3x" for "- 3x"). Then
`ask_about_the_last_example` passed 5 of 5.

Asking RESPOND to avoid every topic's questions (34) as examples, so none gives a later answer away,
made it pick one anyway: the simple textbook equations it reaches for are the questions. `respond.v6.md`
(in use) takes a new example's equation from `examples` in `index.yaml` (17 equations, checked at startup
by `app/examples.py`: the LaTeX, the roots, and no lesson question with those roots); each turn the facts
list those sharing no root with the answer being protected, with their roots. Three board rules also
stopped rejecting what is still correct: small whole numbers in a words line become words ("minus six"),
a 4th segment is accepted (3 are asked for), and a ± line with no check is checked through its two
branches (they must be exactly the roots above it). The board's words are read like speech for "gives
the answer away". The runs keep each rejected output (`rejected_outputs`). The full run then passed
21 of 22 (first outputs rejected in 5 of 66 turns, from 10), and `formula_sign`, failing all day, 4 of 5. A
review then tightened the fixes: a ± line is only accepted with a line above it (or the open question) to prove
it against; no example is offered whose discriminant is the answer; numbers in a words line that looks like maths
stay digits (rejected); "twenty-five" is read as 25. The next full run passed 20 of 22, both failures a chained
line ("x = (-5 + 3)/2 = -1", "a · c = 3 · (-2) = -6"), so v6 asks for one "=" per line; with that,
`formula_sign` passed 3 of 5 and `question_while_checking` 5 of 5.

*(Designed)* Gradual rollout: send a share of students to the new version (by a hash of the
student id: 10%, then 50%, then 100%) and compare the two side by side. Every turn already records
its versions, so the comparison is a query.

**Where the curriculum comes from.** Six hand-written YAML lesson files in the repo
(`curriculum/ch4/`), plus `index.yaml` with the order, the version, the four general formulas and the
example equations RESPOND may use.
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
curriculum-change rule (`sessions.reconcile`), checked at the start of every turn. *(Designed)* A step
map (old step → equivalent new step) would move students more gently than "back to the first
step", and loading several curriculum versions at once would let a session finish on the version
it started with. Neither is built: the rule above already keeps every student on valid ids.

---

## 3. Observability

Every turn writes one `turns` row, one `decisions` row and one JSON log line to stdout with the same
fields: session, turn and student ids; the session's and the turn's versions; phase; what
UNDERSTAND made of the reply; SymPy's verdict; the decision's rule and reason; milliseconds per
stage (for a voice turn, also the browser's mic-open and recording times); each model call's model,
time, outcome, retry, backup, tokens and cost (for speech-to-text, also the clip's length in
seconds); and any issues. An issue is a rejected model output with its exact problems (not JSON, bad
maths, a revealed answer, LaTeX in speech), a fall back to scripted words, a lesson change, a safety
review or an internal error. A safety review is a reply caught by the keyword safety check
(`app/safety.py`, run before anything else reads the reply) or by UNDERSTAND's safety flag. A safety
review and a model's breaker opening also write a warning line.

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

- **Evals before every change:** the 22 tutor scenarios run against a new prompt or model, and a red
  scenario blocks it. A run fails if the first output of either prompt was rejected, so the safety
  net (one retry, then scripted words) can't hide a bad prompt. *(Designed)* The same suite nightly
  against the live config, plus a simulation batch reporting the false advance rate (points given
  while the simulated student still held its hidden mistake).
- **Caught problems are counted, not hidden:** the safety net keeps bad replies away from
  students, but every catch is an issue on the turn, so a rising rate shows a prompt or model
  drifting even while students see nothing wrong.
- **Decision mix drift:** a jump in R1 credits (the tutor got lenient), in reteaches per concept
  (harsh or confusing), in R8 "say that again" or R7 questions, in R7 solved examples (a lesson that
  doesn't land), or in questions per lesson step all show up before complaints do.
- *(Designed)* **Shadow understanding:** a small share of live turns re-run offline through
  UNDERSTAND on a stronger model; a rising rate of different reply types or mistakes means the live
  model drifted. (SymPy does the grading, so there is no LLM grade to re-check.)
- **Slice by version:** because every turn carries its versions, any drift can be traced to the
  prompt, model or curriculum change that caused it.

---

## 4. Latency

A spoken turn runs speech-to-text, UNDERSTAND (unless a fast path handles the reply), SymPy
grading, the engine, RESPOND (only for hints, follow-ups, explanations, solved examples, reteaches and
needs review, with its board checks), then text-to-speech per segment. Every turn records milliseconds
per stage in `turns.latency_ms` (`stt`, `understand`, `grade`, `decide`, `respond`, `total`), each
text-to-speech call logs its own time, and the browser measures stop click → first audio and sends
it with the next turn as `prev_first_audio_ms`. A voice turn's `latency_ms` also holds two times the
browser sends with the recording: `mic_open_ms`, from the Speak click until the recording has really
started, and `recording_ms`, from then until Stop (or the 30 s limit, or the mic stopping by itself).
A recording that never really started (the tab was hidden first) isn't sent at all. With the mic
still on from the last answer, `mic_open_ms` is near 0 (55–60 ms measured in Chrome, which takes that
long to report that a recording has started). When the mic has to be opened (the first answer, the
first after the mic went a minute unused, and every answer on a phone or tablet) it includes that,
and a student's first-ever click also includes the browser asking them to allow the microphone.

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
and in LLM mode nearly all the time is model calls.

**Measured: model calls, 12 Oct 2026**, from the `calls` of every eval run and the four simulated
students (464 calls, all on `claude-haiku-5.5`; no provider errors, no backup model used):

| Call | Typical (p50) | Slow (p95) | Max | Calls |
| --- | --- | --- | --- | --- |
| UNDERSTAND | 1.7 s | 2.7 s | 4.0 s | 259 |
| RESPOND | 2.2 s | 3.2 s | 5.8 s | 126 |

A rejected RESPOND output costs a second call, and 30–40% of first outputs were rejected in the
evals (see "What's ugly"). Speech-to-text and text-to-speech still have only single calls from a
check on 11 Oct: 1.1–2.5 s and 0.6–1.5 s per sentence. Added up, a spoken right answer (speech-to-text,
UNDERSTAND, then the first sentence's audio) takes roughly 3.5–7 s from stop click to first audio
*(estimate from the parts; not yet measured in the browser)*, and more when RESPOND runs: above the
target of p50 ≤ 2.0 s and p95 ≤ 3.5 s. The first levers are a faster UNDERSTAND model (the evals
compare models with `--model-understand`), fewer RESPOND rejections, and streaming RESPOND.

What the student perceives, and what we do about it:

- **Fill the AI's wait:** when the tutor is about to ask the AI to write its reply (a solved example, an
  explanation, a hint: 2–6 s), the server sends the page a short line at once, before the reply, and the
  page says it in the tutor's voice: "Sure, let me think of one.", "Good question. Let me think." or "Hmm,
  let me think." (`replies.WAIT`). The turn's JSON comes as lines (the waiting line, then the turn), so
  one request carries both. Replies that need no AI (a button, a known phrase, a graded answer) get no
  such line, so it never becomes a habit. It shortens no time; it makes the wait feel like thinking. The
  page's 17 s limit starts again when the line arrives: the server is busy, and has its own deadline.
- **Keep the checks cheap:** every line RESPOND writes is compared with every topic's questions (34; an
  example on one would give a later answer away), and SymPy's comparison is slow. A quick numeric look
  first (`board._could_be_same`: the two sides at a sample x, or a claimed root put back in) skips the
  questions a line can't be. Checking a 5-line example went from 2.1 s to 0.19 s on a laptop. Render's
  free tier has a tenth of a CPU, so there it was about 20 s, past the page's limit, when first deployed.
- **Show progress immediately:** the status line goes Opening the microphone… → Listening… →
  Thinking… → Speaking…, and the mic and text box are disabled while the page waits for the server
  (while the tutor speaks they work, and cut it short). While the mic
  opens, the Speak button reads "⏳ Opening mic…", and it turns into Stop, with a soft tick, only once the
  recording has really started, so the student doesn't start talking before the mic can hear them (a
  short reply cut at its start, like "(t)each me", comes back as "Ичми" or "Mi."). A Stop click
  in the recording's first 400 ms is ignored, so a double-click on Speak doesn't end it at once.
  After ~17 s with no reply the page says "That took too long — try again or type it", and nothing
  is resent.
- **Keep the mic on:** opening the mic can take a second, so after the first answer it stays on and
  the next Speak starts recording at once. Its minute starts when the tutor has finished its reply
  and it's the student's turn again (after a typed answer too), and pressing Speak cancels it; if
  Speak isn't pressed within the minute, the mic is switched off. It is also switched off when the
  tab is hidden or left, and at sign-out, and a mic that finishes opening while the tab is hidden
  records nothing. On a touch-first device (any phone or tablet, whatever the brand: the browser reports a
  finger, not a mouse, as its main pointer) the mic is switched off after every recording, because
  phones turn the tutor's voice down while the mic is on; with a mouse or trackpad it stays on.
- **Board and voice together:** each segment's board commands run as that segment's audio starts.
  If its audio isn't ready ~3 s after it's due, the browser's own voice reads it, so the board is
  never more than ~3 s behind the reply.
- **Speak while synthesising:** the audio for every segment is requested at once, so segment 2 is
  being made while segment 1 plays.
- **Skip work:** short replies made only of listed phrases ("ok", "haan", "not sure", "test me", "say
  again", "pardon?"; "ok", "haan", "yes", "yeah", "sure", "no", "nope" and "that makes sense" only while teaching), a
  spoken topic switch ("skip to factorisation") and every button tap (each sends such a phrase) never reach a model,
  and scripted actions (the next step, a new question, praise, "how did you get that?") need no RESPOND. So, while
  teaching, an "ok" uses no model, a right answer one (UNDERSTAND), a tap on **Not sure** while teaching one
  (RESPOND's solved example), and a wrong answer or a question two. Audio
  is cached on disk by model, voice and text, so lesson steps and scripted lines play at once after
  the first time.
- **Short replies:** at most three segments of at most 60 words each (a 4th is accepted rather than rejected).
- *(Designed)* Stream the LLM's output and check segments as they arrive; let the student interrupt.

---

## 5. Metrics

**The number I'd look at first: retained mastery** *(designed)*, meaning the share of concepts
marked mastered whose warm-up question is still answered correctly at the start of the next
session. Mastery alone can be inflated by a lenient tutor; retained mastery catches that, because
the student has to show the learning stuck. It needs a warm-up question at session start, which
isn't built yet.

Supporting numbers, each a query over the six tables or the eval and simulation results (no
dashboard is built):

| Area | Metrics |
| --- | --- |
| Learning | Mastery rate per concept; questions, hints and reteaches to mastery; needs-review rate |
| Tutor quality | False advance rate (simulations); eval pass rate; rejected first outputs and maths errors per 100 LLM replies; scripted fallback rate |
| Engagement | Session length; sessions per student per week; where students drop off (concept and step); next-day return |
| Experience | Stop click → first audio (p50, p95); "say that again" rate; how often students stop the tutor, and where; how often students type instead of speaking |
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
  and streaming, retained mastery, a duplicate-request guard, and fairer passing (two clean answers in
  a row, a Move on button, review questions).
- **Passing is still the old rule.** Wrong answers never count against a student: right, wrong, wrong,
  wrong, right can be mastered (when each wrong answer is a known mistake, so each gets a fresh question; with
  unknown mistakes the same question is hinted and the last right answer earns half a point), and so are four
  right answers after hints (half a point each). A student who
  taps **Test me first** and then gets a question wrong gets hints like anyone else, not the lesson, and
  "move on" while a question is open only gets the question again.
- **Stopping has rough edges.** Repeat after stopping a Continue says only that continuation again, not the
  first reply; stopping the "Welcome back" of a picked-up reply and pressing Continue again resumes the lesson as
  usual, so the rest of that reply is lost. A stop report that reaches the server after the next turn is ignored,
  but one that lands in the very moment the next turn is being saved could put back an older board (it needs two
  tabs or a 17-second stall).
- **The check-ins are a few fixed lines, and some replies are read roughly.** Each check-in rotates
  through two or three phrasings, so a long lesson repeats them. After a solved example the check-in is
  an either-or question ("Shall we go on, or shall I do another example?"), so a bare "no" is read as not
  sure and gets another example. Solved examples while teaching have no limit, and each one is a RESPOND
  call (about 2 s). While a question is open, "not sure" gets the next hint, not a solved example. A
  spoken topic switch has to start the reply, be short and say where to ("Can we skip to the
  factorization topic?"): "can we do factorisation now?" isn't read as one, and neither is "I'm stuck,
  skip to roots" (UNDERSTAND gets that one).
- **Sign-in is weak.** Anyone with the access code can sign in as any username and see that
  student's progress. Fine for a review; real students would need a parent or school account.
- **Four formulas aren't checked.** The general formulas (standard form, the quadratic formula,
  D = b² − 4ac and the three cases of D) are hand-written LaTeX: SymPy can't check letters a, b, c, D.
- **Spoken maths isn't checked.** If the voice says "five" and the board says 12, nothing catches it.
- **"Answer revealed" is only checked for numbers.** For yes/no and nature-of-roots questions, and
  for "no real roots", a reply that gives the answer away in words isn't caught. An example can't give
  another question's answer away: a reteach can't use any of the concept's questions as its example,
  and an explanation can't use any but the open one.
- **Working is judged present or absent, not checked.** "Because I factorised it" counts as working
  even if the factorising was wrong.
- **One trap answer can come from two different slips:** `minus_b` and a sign flip can give the same
  wrong roots, and so can `sign_flip` and brackets with the wrong signs, so a reteach may target the
  wrong slip.
- **Questions can repeat.** A long pass through a concept with fewer than 7 questions wraps around to
  questions the student has already seen.
- **`select` and `new` board lines are trust points.** SymPy confirms a kept root was a root, not
  that the reason for dropping the other is sound.
- **Asking for the steps costs half a point.** While a question is open, "explain step by step",
  "how do I solve this?" or "show me how" asks for help, like "help me solve this": the next hint,
  one step at a time (after hints 1 and 2 the tutor says "Try that step, then tell me the roots."),
  and a right answer after any hint earns half a point. "Why …?" questions stay free (R7), so points
  still depend a little on how a request is worded. Scripted mode reads step requests this way:
  "step by step" asks unless the student says they did it or will do it themselves, so "I did it
  step by step", "let me go step by step" and "I'm doing it step by step" are asked again, not
  hinted. The edges are rough both ways: "I know how to solve it" and "I tried it step by step"
  still count as asking, and "I'm going mad, explain step by step" is asked again. UNDERSTAND's
  prompt v1 had no such rule; `understand.v2.md` added it on 12 Oct and v3 keeps it. One eval
  scenario (`steps_paraphrase_question`) expects "how does splitting the middle term work for this
  one?" to stay a free question, and v2 reads it as asking for help: which is right is still open.
- **Scripted mode is cruder than the LLM** (it's the fallback and the mock): it spots mistakes only
  through trap answers, so it hints where the LLM might reteach; any "?" makes a reply a question,
  even "did India win yesterday?", unless it holds an answer, asks for help ("how do I solve
  this?") or mentions being hit (see the safety check below); a question asked while a question is
  open gets a lesson hint, not an answer to it (where to start, or the last hint again, adding no
  hint level); a second reply isn't merged with the first ("0 too" after "3"); and mixed replies are
  read roughly. Without a key, students have to say the answer first.
- **The keyword safety check has rare false alarms and misses.** `app/safety.py` reads 7 or more
  digits, in groups split by single spaces or hyphens, as a phone number when every group has 2 or
  more digits ("98765 43210", "011 2345 6789"), also after one lone digit like a trunk 0 or a
  country code ("0 98765 43210", "+1 987 654 3210"), or when every group is one digit, as when a
  number is said one by one ("nine eight seven six five four three"). Any other mix, like the factor
  pairs "1 12 2 6 3 4", is maths. "Abuse", "rape" or "molest" in any form counts anywhere ("he
  keeps abusing my mom", "I was raped"). Someone hurting the student is a person ("he", "they", "my
  dad", "papa"…), up to two words like "is always", "keeps", "has been" or "will", then hit, beat,
  slap, kick, punch, smack, thrash, hurt or touch, in any form, then "me" or "us" ("he's been
  hitting me", "my dad is always beating me", "dad's been beating me", "they'll hit us"); or the
  student says it the other way round: I was, I am, I'm, I get, I got, I keep, I have been, I've
  been or I been, perhaps then being or getting, then hit, beaten, beat up, slapped, kicked,
  punched, smacked, thrashed, hurt, touched or abused ("I was beaten", "I'm getting hit at home",
  "I keep getting beaten"). That form doesn't count when the maths did it: "by" or "with", perhaps
  a word like "this", "a" or "so many" and one like "hard" or "tough", then question, sum, problem,
  equation or quadratic ("I got beaten by this question"); and "I'm beat" means tired. False alarms
  remain: "12 13 14 15", a hyphenated date like "11-10-2026", 7 or more digits said one by one, "my
  head is hurting me", "my friend beat me to it", "they're beating me at chess", "they beat us at
  cricket", "I got hurt playing cricket", "I was hit by the ball", "I'm beaten, I give up", "I got
  hit with a hard one", "what the hell, hit me with a hint" ("he'll" arrives as "hell") and
  "honestly beats me" (any word before "beats me" except it, that, this or question counts as a
  person, so that "Rohan beats me" is caught). Misses remain too: "He gets drunk. Beats me.", "he
  is still always hitting me" (three words between), "I get beat at home", "we get beaten at home",
  "being hit at home", "my dad beats my mom", "I'm scared of my dad", numbers said with "double" or
  "oh", and a Devanagari word before "beats me". With the LLM, UNDERSTAND's safety flag is the
  second net. In scripted mode, a reply with hit, beat, slap, kick, punch, smack, thrash, hurt or
  touch, in any form, followed by "me" or "us", or with the student's "I was beaten" form above,
  never gets a maths hint: the keywords give the safety reply, or, when they miss it ("He gets
  drunk. Beats me. I don't know"), the reply is read as unclear, even with an answer, a request for
  help or a "?" in it, and the tutor asks again (so "Oh, now it hits me: 2 and 3" has to be
  answered again). A miss without such words can still get a maths hint in scripted mode when it
  asks for help: "I get beat at home, help", "we get beaten at home, help", "being hit at home,
  help", "my dad beats my mom, help" or "I'm scared of my dad, help".
- **One process only.** The breakers live in memory and SQLite wants one writer, so the app runs one
  uvicorn worker. More traffic means Postgres and shared breakers first.
- **No duplicate-request guard.** A request sent twice would apply twice. The page never resends by
  itself, so this only matters if automatic retries are added.
- **Push-to-talk only.** The student can't interrupt the tutor mid-sentence.
- **The mic stays on between answers.** The browser shows its mic indicator while the tutor replies
  and for up to a minute of the student's turn after that, though nothing is recorded until Speak is
  pressed. Phones and tablets don't keep it on, so every answer there still waits for the mic to open.
- **Small, synthetic test sets:** 22 eval scenarios and 4 simulated students, and the simulated
  students are LLMs with their own biases. Real student sessions are the missing check.
- **Hinglish speech is untested.** Two eval scenarios type Hinglish, but no Hinglish recording has
  been through speech-to-text, and the voice ("sarah", chosen by listening) isn't Indian English.
- **Speech-to-text can mishear,** sometimes into words nobody said or another script, most often when
  the start of a short reply was cut off. A spoken reply UNDERSTAND can't place (unclear or off-topic),
  or one in an alphabet the lessons never use (not English, Hindi or the Greek of maths: no UNDERSTAND
  call), gets "Sorry, I may have misheard you. Say it again, or type it." and then the question, or
  while teaching just the check-in; no transcript at all gets "Sorry, I didn't catch that. Say it
  again, or type it." A mishearing that reads as a real reply, like a wrong number or "I don't know",
  is still taken at its word. A vocabulary hint for speech-to-text (the button words) was tried and
  left out: it fixed most cut-off replies, but on a clip with almost no speech it wrote "Teach me."
  anyway, and a cut-off "teach me" became "Yes, go on". A reply the student never said is worse than
  "say it again".
- **UNDERSTAND can tidy up a wrong answer.** In the first eval run with the key, the student said
  the sign-flipped roots and UNDERSTAND, though it named the sign-flip mistake, wrote down the right
  ones, so SymPy marked them right. The prompt already said "never correct it". Now a right answer is
  checked against the student's own words, read by the scripted rules (`llm_steps.grade`): when
  UNDERSTAND named a mistake, or those words are a trap answer, they are what gets graded, and the
  turn records `answer_rewritten`. A rewrite that names no mistake and matches no trap still slips
  through.
- **RESPOND's board lines are sometimes rejected.** On 12 Oct 30–40% of first outputs broke a board rule
  (most often digits in a words line); after the fixes of 13 Oct (example equations, words for small
  numbers, a 4th segment, ± lines) 5 of 66 eval turns were, and 4 fell back to scripted words. The student
  never sees a bad line, but each rejection costs about 2 s. Reteaches fail most: they write the most maths.
- **The simulated students drift.** The "overconfident" student answered both questions right with
  working, so that run tested nothing about over-confidence; false advances count only what the
  student model admits to.

With two more days: measure a real voice session and act on it, stream LLM output into segments,
add barge-in, check a student's spoken working for the exact slip, and run ten real students
through concept 3 to see what UNDERSTAND gets wrong on real speech.
