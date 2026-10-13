"""Every scripted line the tutor says: greetings, praise, re-asks, the safety reply and the END summary, plus
the hints, explanations and reteaches used when there is no API key or the LLM's reply fails."""
from __future__ import annotations

from sympy import latex

from app import grading
from app.config import MAX_HINTS
from app.curriculum import get_step
from app.models import (WORD_KINDS, BoardCommand, Check, Concept, Curriculum, Decision, Grade, Progress,
                        Segment, Step)

# ---------- scripted texts ----------
# The "sorry" before the tutor asks again: no transcript came, a spoken reply nobody could place, or a typed one.
DIDNT_CATCH = "Sorry, I didn't catch that. Say it again, or type it."
MISHEARD = "Sorry, I may have misheard you. Say it again, or type it."
DIDNT_FOLLOW = "Sorry, I didn't follow that."
APOLOGIES = (DIDNT_CATCH, MISHEARD, DIDNT_FOLLOW)
SORRY = "Sorry, let's try that again."                 # after an error on our side: nothing changed
# Said at once while the AI writes the reply (wait_key picks which), so the wait isn't silent.
WAIT = {"example": "Sure, let me think of one.", "explain": "Good question. Let me think.",
        "think": "Hmm, let me think."}
SAFETY_REPLY = ("Thank you for telling me. If something is worrying you, please talk to a parent, a teacher or "
                "another adult you trust. And please don't share personal details like your phone number or "
                "address here.")
FIXED = {"start_check": "Let's see if you've got it.", "show_me": "Great, then show me.",
         "safety": SAFETY_REPLY, "limit": "That's all for today. Let's continue tomorrow!",
         "lesson_changed": "The lesson has been updated, so here's a new question."}
PRAISE = ("Right", "Well done", "That's right", "Nice work")
YES_NO_KINDS = ("is_quadratic", "is_root", "has_real_roots")
CLARIFY = {"roots": "Say just the numbers, or type them.", "coefficient": "Say just the number.",
           "discriminant": "Say just the number.",
           "nature": "Two distinct real roots, two equal real roots, or no real roots?"}
FOLLOW_UP = {"missing": "That's part of it, but a root is missing. Have another look.",
             "extra": "Nearly. One of those values doesn't fit the question. Think about which ones make sense."}
# While teaching: the line before the step is said again, for an explanation and for a solved example without the AI.
TEACHING_LEAD = {"explain": "Good question — let's go over it again.", "example": "Let's go over that step again."}
REASK = {"roots": "So, what are the roots?", "discriminant": "So, what is the discriminant?",
         "nature": "So, what is the nature of the roots?", "is_quadratic": "So, is it a quadratic equation?",
         "is_root": "So, is it a root?", "has_real_roots": "So, is it possible?"}
REVIEW_LATER = "This one needs more practice, so we'll come back to this next time."   # R9, after the worked answer
# The cue that ends a teaching turn, by where the student is (compose.cue_for picks which): at the topic start, on
# a step, on the last step, after a solved example. Each place always gets the same phrasing (cue_line).
CUES = {"offer": ("Shall I teach it? Or say 'test me' if you already know it.",
                  "Shall I teach you this one? Or say 'test me' if you already know it."),
        "step": ("Did that make sense? Shall we go on?", "Does that make sense so far? Shall we go on?",
                 "Is that clear? Shall we go on?"),
        "last_step": ("Ready for a question?", "Ready to try a question?", "Shall we try a question now?"),
        "example": ("Shall we go on, or shall I do another example?",
                    "Shall we go on, or would you like another example?")}


def wait_key(action: str) -> str:
    """Which WAIT line goes with an action the AI writes the reply for."""
    return action if action in ("example", "explain") else "think"


# ---------- scripted replies ----------
def words(decision: Decision, *, cur: Curriculum, progress: Progress, check: Check | None,
          grade: Grade | None, first_name: str) -> list[Segment]:
    """Scripted words for a decision's action. Compose adds any step, question or re-ask afterwards."""
    concept = cur.concepts.get(decision.concept_id or "")
    action = decision.action
    if first_name.islower() or first_name.isupper():         # typed "sreekar" or "SREEKAR": say "Sreekar"
        first_name = first_name.title()
    if action in FIXED:
        return _say(FIXED[action])
    if action == "next_step":
        return []                                            # the step's own segments do the talking
    if action in ("greet", "resume"):
        return _say(_greeting(decision, cur, progress, first_name))
    if action in ("credit", "mastered"):
        return _say(_praise(decision, check, concept))
    if action == "probe":
        return _say("Right — how do you know?" if check.kind in YES_NO_KINDS else "Right — how did you get that?")
    if action == "repeat" and decision.evidence.get("type") == "move_on":
        return _say("Let's get this one first.")            # compose asks the open question again
    if action == "repeat":
        return _say("Let me ask that again." if check else "Let me say that again.")
    if action == "clarify":
        return _say(CLARIFY.get(check.kind, "Just say yes or no."))
    if action == "hint":
        return _say(f"Here's a hint. {check.hints[decision.hint_level - 1]}")
    if action == "follow_up":
        return _say(FOLLOW_UP.get(grade.detail if grade else "", "Nearly. Have another look at your answer."))
    if action == "explain" and check:                        # R7 adds no hint level; compose re-asks the question
        if decision.hint_level == 0:
            return _say("Good question. Here's where to start.") + _say(check.hints[0])
        return _say("Good question. Let's look at the last hint again.") + _say(check.hints[decision.hint_level - 1])
    if action in TEACHING_LEAD:                              # while teaching: the step again (for an example, the
        steps = _segments(get_step(concept, progress[concept.id].step_id))     # AI would have solved a new one)
        return _say(TEACHING_LEAD[action]) + steps
    if action == "reteach":
        fix = next((m.fix for m in concept.mistakes if m.id == decision.mistake), "")
        lead = _say(f"Let's look at it another way. {fix}".strip())
        return lead + _segments(get_step(concept, concept.reteach_step))
    if action == "needs_review":                             # if no topic follows, END's summary says we come back
        worked = Segment(say=f"{check.hints[-1]} Here is the answer.", board=[answer_line(check)])
        return [worked, Segment(say=REVIEW_LATER)] if decision.resume_concept_id else [worked]
    if action == "end":
        return _summary(cur, progress, first_name)
    if action == "jump":                                     # the topic left keeps its place; opening() follows
        left = cur.concepts.get(decision.evidence.get("from") or "")
        return _say(f"Sure. {left.title} is saved just where you left it.") if left and left is not concept else []
    raise ValueError(f"no scripted words for action {action!r}")


def reask(check: Check, awaiting_working: bool, hint_level: int = 0) -> Segment:
    """The short re-ask after a hint, follow-up or explanation. hint_level is the hint just given (0 for none):
    after hint 1 or 2 the student tries that step first, one step at a time; after the last hint, the question."""
    if awaiting_working:
        return Segment(say="So, how did you get your answer?")
    if 0 < hint_level < MAX_HINTS:
        what = "the roots" if check.kind == "roots" and check.show_equation else "the answer"
        return Segment(say=f"Try that step, then tell me {what}.")
    if check.kind == "coefficient":
        return Segment(say=f"So, what is {check.which}?")
    if check.kind == "roots" and not check.show_equation:
        return Segment(say="So, what is your answer?")       # word problems ask for one quantity
    return Segment(say=REASK[check.kind])


def cue_line(kind: str, key: str) -> str:
    """One phrasing of a cue, picked by a step or topic id: the same place always says the same words (so its audio
    is cached), and the steps of a topic don't all end alike."""
    lines = CUES[kind]
    return lines[sum(map(ord, key)) % len(lines)]


def opening(concept: Concept, action: str, from_top: bool) -> list[Segment]:
    """Said as a concept is picked up, before its step or question. From the top (its start, or step 1): its
    intro (not on Continue). Right after R1 or R9 ended another concept, or a picked topic (S2): first that we move
    on to it, or go back to it if already started."""
    title, moved_on = _lower_first(concept.title), action in ("mastered", "needs_review", "jump")
    if not from_top:                                         # no "Now" here: some steps start with it
        return _say(f"Let's get back to {title}, where we left off.") if moved_on else []
    if action == "resume":
        return []                                            # Continue (S1) only replays the step
    return _say(f"Now let's move on to {title}. {concept.intro}" if moved_on else concept.intro)


def answer_line(check: Check) -> BoardCommand:
    """Scripted R9's board line: a checked root claim built from SymPy's key, or a text line in words."""
    key = grading.answer_key(check)
    if check.kind != "roots" or not key.values:
        return BoardCommand(op="write", id="answer", text=grading.key_text(check))
    shown = r" \text{ or } ".join(f"x = {latex(value)}" for value in key.values)
    claims = [f"Eq(x, {value})" for value in key.values]
    checked = claims[0] if len(claims) == 1 else f"Or({', '.join(claims)})"
    return BoardCommand(op="write", id="answer", latex=shown, check=checked,
                        relation="select" if check.keep else "equiv")


def _say(text: str) -> list[Segment]:
    return [Segment(say=text)]


def _segments(step: Step | None) -> list[Segment]:
    return list(step.segments) if step else []


def _lower_first(text: str) -> str:
    """For the middle of a sentence: "Solving by factorisation" -> "solving by factorisation"."""
    return text[:1].lower() + text[1:]


def _praise(decision: Decision, check: Check, concept: Concept) -> str:
    """Praise that rotates by check id; word answers are restated in NCERT words ("Right — no real roots.")."""
    praise = PRAISE[sum(map(ord, check.id)) % len(PRAISE)]
    text = f"{praise} — {_lower_first(grading.key_speech(check))}." if check.kind in WORD_KINDS else f"{praise}!"
    if decision.action == "mastered" and decision.resume_concept_id:   # not the very last: END's summary says it
        text += f" You've mastered {_lower_first(concept.title)}."
    return text


def _greeting(decision: Decision, cur: Curriculum, progress: Progress, first_name: str) -> str:
    """S0 and S1: start the concept, go over it again, or carry on where we left off. S0 says the name."""
    cid = decision.resume_concept_id
    if cid is None:
        return "Welcome back!"                               # nothing left: compose adds the summary, with the name
    state, title = progress[cid], _lower_first(cur.concepts[cid].title)
    if decision.action == "resume":                          # S1: no name, so its audio is cached for everyone
        return f"Welcome back! Let's carry on with {title} from where we left off."
    if state.pending or state.step_id not in (None, cur.concepts[cid].steps[0].id):   # S0 part-way through
        return f"Welcome back, {first_name}! Let's carry on with {title} from where we left off."
    if state.status == "teaching" and state.last_check_id:   # S0 reset it from needs review (it keeps last_check_id)
        return f"Hi {first_name}! Last time, {title} needed more practice, so let's go over it again."
    return f"Hi {first_name}! Let's start today with {title}."


def _summary(cur: Curriculum, progress: Progress, first_name: str) -> list[Segment]:
    """END: what's mastered, and what needs review next session."""
    done = [_lower_first(cur.concepts[c].title) for c in cur.order if progress[c].status == "mastered"]
    review = [_lower_first(cur.concepts[c].title) for c in cur.order if progress[c].status == "needs_review"]
    first = f"That's everything for now, {first_name}!" + (f" You've mastered {_join(done)}." if done else "")
    rest = [Segment(say=f"Next session we'll come back to {_join(review)}.")] if review else []
    return [Segment(say=first)] + rest


def _join(items: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
