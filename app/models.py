"""Every shared data shape. Other modules import their types from here."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------- lesson files (curriculum/ch4/*.yaml) ----------
Kind = Literal["roots", "is_quadratic", "is_root", "coefficient", "discriminant", "nature", "has_real_roots"]
WORD_KINDS = ("is_quadratic", "is_root", "nature", "has_real_roots")
Strategy = Literal["worked_example", "substitute_back", "compare_non_example"]


class BoardCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["write", "highlight", "clear"]
    id: str | None = None          # write: the line id
    latex: str | None = None       # write: maths shown to the student
    check: str | None = None       # write: the same maths in SymPy syntax (x, Eq, Or, sqrt, Rational)
    relation: Literal["equiv", "new", "select"] = "equiv"
    formula: str | None = None     # write: a formula name from index.yaml
    text: str | None = None        # write: a plain line of words
    target: str | None = None      # highlight: the line id
    part: str | None = None        # highlight: an exact LaTeX snippet of that line


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    say: str
    board: list[BoardCommand] = Field(default_factory=list)


class Reply(BaseModel):
    """What RESPOND must return."""
    model_config = ConfigDict(extra="forbid")
    segments: list[Segment] = Field(min_length=1, max_length=3)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    segments: list[Segment]


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    say: str                                     # the spoken question
    label: str                                   # text line written above the question
    equation: str                                # SymPy syntax, e.g. "Eq(x**2 - 5*x + 6, 0)"
    kind: Kind
    hints: list[str]                             # exactly 3
    traps: dict[str, str] = Field(default_factory=dict)   # mistake id -> a specific wrong answer
    keep: Literal["positive"] | None = None      # roots: keep only positive roots
    candidate: str | None = None                 # is_root: the value to substitute
    which: Literal["a", "b", "c"] | None = None  # coefficient: which one
    show_equation: bool = True                   # word problems: False


class Mistake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    looks_like: str
    fix: str


class Concept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    ncert: str
    steps: list[Step]
    checks: list[Check]
    mistakes: list[Mistake]
    strategies: list[Strategy]
    reteach_step: str


class Curriculum(BaseModel):
    version: str
    order: list[str]                 # concept ids in teaching order
    formulas: dict[str, str]         # formula name -> LaTeX
    concepts: dict[str, Concept]     # concept id -> concept


# ---------- one reply, as understood ----------
ReplyType = Literal["next", "answer", "question", "dont_know", "claims_understanding",
                    "off_topic", "unclear", "say_again"]


class Understood(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ReplyType
    answer: str = ""                 # "" means no answer was given
    showed_working: bool = False
    mistake: str | None = None       # a suggestion; only used as §5.4 allows
    safety: bool = False             # distress or personal information


# ---------- answer keys and grades ----------
Verdict = Literal["correct", "wrong", "partial", "unsure"]


@dataclass
class AnswerKey:
    kind: str
    values: list | None = None       # numeric kinds: the key ([] = no real roots); coefficient/discriminant: [one value]
    all_roots: list | None = None    # roots kind: every real root before `keep`
    word: str | None = None          # word kinds: "yes" / "no" / "distinct" / "equal" / "none"


@dataclass
class Grade:
    verdict: Verdict
    student: str                     # the student's answer text, e.g. "-2, 3" or "yes"
    detail: str = ""                 # "missing" | "extra" | "not_a_root" | "empty" | "" (why partial or wrong)
    trap: str | None = None          # mistake id when the answer equals one of the check's traps
    wrong_values: bool = False       # the answer contained values that are not roots (substitute_back can use them)


# ---------- progress ----------
Status = Literal["not_started", "teaching", "checking", "mastered", "needs_review"]


@dataclass
class Pending:
    check_id: str
    hint_level: int = 0
    awaiting_working: bool = False
    followup_used: bool = False
    last_answer: str = ""


@dataclass
class ConceptState:
    concept_id: str
    status: Status = "not_started"
    step_id: str | None = None
    points: float = 0.0
    pending: Pending | None = None
    last_check_id: str | None = None
    reteach_count: int = 0
    strategies_tried: list[str] = field(default_factory=list)


Progress = dict[str, ConceptState]   # concept id -> state, one entry per concept in Curriculum.order


# ---------- decisions ----------
ACTIONS = ("next_step", "start_check", "credit", "mastered", "probe", "reteach", "hint", "follow_up",
           "clarify", "explain", "show_me", "repeat", "needs_review", "safety", "greet", "resume",
           "end", "limit", "lesson_changed")
LLM_ACTIONS = frozenset({"hint", "follow_up", "explain", "reteach", "needs_review"})


@dataclass
class Decision:
    rule: str                        # "R0".."R10", "S0", "S1", "END", "LIMIT"
    action: str                      # one of ACTIONS
    concept_id: str | None           # the concept this turn was about (None for END and LIMIT)
    reason: str                      # for the Why? panel; never contains an answer key
    evidence: dict = field(default_factory=dict)   # saved in the DB only; may contain keys
    hint_level: int = 0
    reteach_count: int = 0
    strategy: str | None = None      # reteach strategy, or "replay" when the reteach was scripted
    mistake: str | None = None       # the mistake this move targets
    play_step_id: str | None = None        # play this step of concept_id after the words
    ask_check_id: str | None = None        # ask this question of concept_id after the words
    resume_concept_id: str | None = None   # resume this concept after the words (§4.5)
    clear_board: bool = False              # clear the board before resuming / playing
    reask: bool = False                    # add the short scripted re-ask after the words


# ---------- board ----------
PINNED_IDS = ("q-label", "q-eq")


@dataclass
class BoardLine:
    id: str
    latex: str | None = None
    formula: str | None = None
    text: str | None = None
    check: str | None = None


# ---------- one finished turn ----------
@dataclass
class TurnResult:
    turn_id: str
    session_id: str
    transcript: str
    understood: Understood | None
    grade: Grade | None
    decision: Decision
    segments: list[Segment]
    board: list[BoardLine]
    issues: list[str]
    latency_ms: dict
    calls: list[dict]
    input_hint: str | None = None    # "type" when the tutor suggests typing, else None


# ---------- one turn in progress (tutor.py builds it, llm_steps.py reads and adds to it) ----------
@dataclass
class TurnContext:
    """Everything one turn needs; built by Tutor.turn() before understanding."""
    student: dict
    session: dict
    concept: Concept | None            # current concept (None at END)
    phase: str                         # "teaching" | "checking"
    before: Progress                   # progress before decide()
    check: Check | None                # the pending question before decide() (None while teaching)
    text: str                          # the student's words, cut to MAX_INPUT_CHARS
    last_speech: list[Segment]         # what the tutor said last turn, without its board commands
    previous_reason: str               # reason of the previous turn's decision ("" if none)
    board: list[BoardLine]             # the session board before this turn
    deadline: float                    # time.monotonic() + TURN_DEADLINE_S, set at turn start
    calls: list[dict]                  # model calls made so far this turn
    issues: list[str]
    latency: dict
    scripted: bool = False             # no LLM for the rest of this turn (scripted mode, or UNDERSTAND failed)
