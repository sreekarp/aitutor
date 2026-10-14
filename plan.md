# Plan: an AI tutor for quadratic equations

## 1. What I understood

Build a tutor for NCERT Class 10, Chapter 4 (quadratic equations) that a student talks to and that writes on a
whiteboard. It has to cover all six subconcepts. For each one it has to teach, check whether the student really
understood, and then decide: move on, or teach it again a different way. It must be able to say why it decided
that. Additionally: a regression eval suite runnable with one command, graceful handling of a failing AI
provider, simulated students, a live link, and DESIGN.md.

What should work:
- A student can sign in, learn any of the six topics by voice or by typing, and pick up where they left off.
- Every maths line on the board and every answer is checked by code, never just trusted from the AI.
- If the AI is down, the tutor still works with limited capacity (scripted words), thus less flexibly.
- A broken prompt makes the eval suite go red.

What I expected to be hardest: trusting and validating the AI. A tutor that shows wrong maths or moves a student
on too early is a waste of the student's time, so most of my focus went on the checks placed on the AI. Also,
giving the student a natural feeling instead of a mechanical one.

## 2. The core idea

**The rules decide the move; the AI only reads the student and chooses the words.**

```
student speaks ─▶ speech-to-text ─▶ UNDERSTAND (AI: what kind of reply? what answer?)
   ─▶ SymPy grades the answer ─▶ rules engine picks ONE move (and writes the reason)
   ─▶ RESPOND (AI: words and board lines, only for hints, explanations, examples, reteaches)
   ─▶ board checker (SymPy proves every line) ─▶ text-to-speech, one part at a time
```

Why: an AI that decides "the student has mastered this" can't be tested or explained, and sometimes the maths
given by an AI is wrong. Rules are predictable, testable and resumable, and each decision has a plain reason
anyone can see in the "Why?" panel. The AI is good at the part rules are bad at: understanding messy speech and
wording an explanation.

## 3. Key decisions

| Decision | What I chose | Instead of | Why |
| --- | --- | --- | --- |
| Who decides | A rules engine | The AI | Explainable, testable, can't skip ahead |
| Maths | SymPy checks every board line and answer (an idea from MobiTutor, another tutoring app) | Trusting the AI | The board must only show true maths |
| Lessons | Six YAML files, checked manually and by SymPy at startup | A generic pipeline to convert pdfs into lessons | Small enough to write well; a typo stops the app at startup, not mid-lesson |
| Passing Criteria | Two clean answers in a row on fresh questions | Points that add up regardless | "Mastered" must mean the student can do it without help |
| Progress | One row per student per topic, plus a log of every turn and decision | One JSON blob per student | Easy to query; every decision has a recorded reason |
| Versions | Each session keeps the prompts and models it started with | Always the latest config | A change can't surprise a student mid-session |
| AI failure | Backup model, one retry, deadline, then scripted words | - | The lesson never stops because a provider is down |
| Hosting | Render free + Neon Postgres, plain HTML/JS | - | Free and simple; the cost is a ~1 minute cold start |

## 4. Iterations

### Iteration 0: the proposal (before code)
- **Planned:** React app, a reply "router" model using jev, a pipeline turning the NCERT PDF into lessons, pre-recorded audio,
  PIN sign-in, an AI judge for evals.
- **Cut, after a review of the design:** react (made simple html+js), the jev router, the pdf pipeline for lessons, pre-recorded audio, PIN based signin and the AI judge. With 2–3 days, I kept what proves the teaching loop and
  dropped what only adds moving parts.

### Iteration 1: the core loop (11 Oct)
- **Built:** maths checker, grading, lessons, rules engine, scripted fallback (works with no API key), the AI
  client with backups and retries, the page with voice and whiteboard, evals, simulated students.
- **Went wrong:** the keyword safety check missed real distress messages and flagged harmless answers; fixed with
  real phrasings as tests. Files grew too big to explain, so: one job per file, 300 lines at most.

### Iteration 2: first runs with the real AI (12 Oct)
- **Evals:** 12/17, then 14/17 after a second prompt version.
- **Fixed:** the AI "corrected" a wrong answer before grading (now the student's own words are graded); the voice
  said "x equals 2" for "x = -2" (numbers are now always spoken in words); strict topic order felt rigid (now any
  topic can be picked, and the one you leave is saved).

### Iteration 3: a conversation, not a lecture (12–13 Oct)
- **Problem:** the tutor played step after step and only checked understanding at the end of a topic.
- **Changed:** every step ends with "Did that make sense?" and buttons; "Test me first" at a topic's start;
  "not sure" gets a solved example; say a topic's name to jump there.

### Iteration 4: going live (13 Oct)
- Private GitHub repo; Render runs the app, Neon holds the database. Using it live found the next problems.

### Iteration 5: what using it showed me (13–14 Oct)

| What I saw | What I changed |
| --- | --- |
| No way to stop the tutor mid sentence | Stop (or Esc) button; Repeat and Continue pick the reply up again |
| "Ичми" for "teach me" (the recording cut the start) | A tick when the mic starts; Sorry, I may have misheard you |
| Silence while the AI wrote an example | The tutor says "Sure, let me think of one." to make student feel not left out |
| The AI used a quiz question as its example | Every topic's questions are off limits |
| "That took too long" on the server | A quick numeric check before SymPy: 2.1 s → 0.19 s |

**Tried and dropped:** a vocabulary hint for speech-to-text. It fixed clipped words (teach me and test me are very small words and people tend to speak them quickly), but invented "Teach me."
from near-silence. A reply the student never said is worse than "say it again".

### Iteration 6: fewer rejected AI drafts, more of the chapter (14 Oct)
- The AI now picks examples from 17 checked equations, and rules that rejected correct work were fixed.
- Added "find k so the roots are equal" questions (NCERT Exercise 4.3) and story based opening for each topic.
- **Result:** evals 19/22 → 22/24; rejected first drafts 10 → 5 in 66 turns.

### Iteration 7: a review, and real numbers from the live site (14 Oct)
- **The review:** a review of my DESIGN.md against the brief found gaps in Grading, Metrics and in communication (5/10: too long, too much detail).
- **Passing made strict:** mastered after two clean answers in a row (a fresh question, right first time with working; a hint or a "partly right" nudge means it doesn't count, but asking the tutor a question is fine). Any help or wrong answer starts the count again. The simulated sign-flipper is no longer mastered
  while still flipping signs.
- **New finding:** 3 of 4 reteaches in the simulations came after a *right* answer: the AI read the working
  ("minus 2 and minus 3, so x is 2 or 3") as the answer. That's the next thing to fix.
- **Versions pinned to sessions:** a student finishes a session on the prompts and models it started with; a new
  version can go to a share of students first, and one setting rolls it back.
- **Latency measured live:** 4.5 s typical, 13.6 s worst, from Stop to hearing the tutor. The cause of the worst
  cases: the main AI model stalled and we waited 8 s before using the backup. Switching after 4–5 s brought
  the slowest re-test turn to 9.7 s (thinking the failures are due to a bad day for model/ if persisted need to switch the backup model as main one).


## 5. How I know it works

- **513 automated tests:** rules, maths checker, grading, lessons, provider failures, the web routes (with the help of Claude).
- **Evals:** 24 scenarios with the real AI, 3 runs each, one command; the latest run passed 22 of 24. A broken
  prompt turns it red.
- **Simulated students:** four personas, re-run after the passing rule changed.
- **Live measurements:** every turn saves its timings, so the latency numbers come from real sessions(but only measure the small set, will look into the simulated run latency).
- **Using it myself** on the live site, which found most of Iterations 5 and 7.

## 6. What's next

- Check the student's spoken working with SymPy, so a misread can't be graded as the answer.
- The rest of fair passing: a "Move on" button with a review question next session, and a warm-up question to
  measure whether mastery lasts.
- Latency: ask the backup model too when the main one is slow, and stream the AI's reply.
- Short spoken commands: "test me" was misheard a few times in my live tests, few time "Teach me" also(although I attributed some of it to the placement of my mic) (the buttons always work).
- Live testing on real user base (real user feedback always finds gaps the dev simulated ones ignore)

## 7. What I'd do differently

- Put it in front of real users earlier, preferably students with different math levels: the most useful fixes came from
  using the live site, not from planning.
- Save the AI's rejected drafts from day one, not just the reasons.
- Measure the real voice loop as soon as it is live: adding up the parts gave 3.5–7 s; the live worst was 13.6 s.
- Make the passing rule strict from the start: "mastered" is the number everything else is judged by.

## 8. How I worked

- **My decisions:** the rules-decide / AI-words split, the voice ("sarah", picked by ear), Render + Neon, what to
  cut, letting questions to the tutor stay free while a question is open, the "let me think" line, and acting on
  the review.
- **An AI coding assistant (Claude Code)** wrote most of the code , under my proposal and 
  direction. I reviewed the changes and kept the code to rules I can explain (one job per file, 300 lines at most).
- **Testing by hand:** I used the tutor on the live site by voice and by typing; the latency numbers are from my
  own sessions.
