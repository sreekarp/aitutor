"""Tests for app/fallback.py: the keyword fast paths and scripted understanding."""
from conftest import check_of, make_settings, new_student

from app import db, fallback, grading
from app.tutor import Tutor


# ---------- helpers ----------
def understand(cur, text, check_id="fa1", *, phase="checking", awaiting_working=False):
    return fallback.understand(text, phase=phase, check=check_of(cur, check_id), awaiting_working=awaiting_working)


# ---------- fast paths ----------
def test_keyword_whole_reply_only():
    for text in ("ok", "OK!", "ok next", "Haan theek hai", "go on", "accha"):
        assert fallback.keyword(text, "teaching") == "next", text
    for text in ("say again", "Repeat please", "phir se bolo"):
        assert fallback.keyword(text, "teaching") == fallback.keyword(text, "checking") == "say_again"
    for text in ("yesterday", "change", "ok, so why is it zero?", "repeat why that works", "did India win yesterday?"):
        assert fallback.keyword(text, "teaching") is None, text
    assert fallback.keyword("ok", "checking") is None                     # while checking, "ok" isn't "next"


def test_keyword_reads_got_it_test_me_not_sure_and_move_on():
    """Whole replies, read with no model call. "got it" goes on while teaching (while a question is open UNDERSTAND
    reads it), and "no" and "nahi" mean not sure only while teaching: while a question is open they are answers."""
    for text in ("got it", "Got it.", "Understood!", "samajh gaya", "I get it", "I get it.", "Makes sense"):
        assert (fallback.keyword(text, "teaching"), fallback.keyword(text, "checking")) == \
            ("claims_understanding", None), text
    for text in ("carry on", "Carry on.", "teach me", "Yes quiz me", "yes, quiz me"):
        assert (fallback.keyword(text, "teaching"), fallback.keyword(text, "checking")) == ("next", None), text
    for text in ("not sure", "I'm not sure", "didn't get it", "Didn’t get it", "don't understand",
                 "another example", "explain again", "confused"):
        assert fallback.keyword(text, "teaching") == fallback.keyword(text, "checking") == "not_sure", text
    for text in ("no", "No.", "nahi"):
        assert (fallback.keyword(text, "teaching"), fallback.keyword(text, "checking")) == ("not_sure", None), text
    for text in ("move on", "Move on!", "no thanks", "stop here", "next topic"):
        assert fallback.keyword(text, "teaching") == fallback.keyword(text, "checking") == "move_on", text
    for text in ("test me", "Test me first", "quiz me", "I already know this", "I already know it.",
                 "skip to the questions"):
        assert fallback.keyword(text, "teaching") == fallback.keyword(text, "checking") == "test_me", text
    for text in ("no, it's 3", "not sure, maybe 2 and 3", "move on to factorisation", "test me on roots",
                 "that makes sense, why is it zero?"):
        assert fallback.keyword(text, "teaching") is None, text           # longer replies go to UNDERSTAND


def test_yes_and_no_while_a_question_is_open_are_answers_never_go_on_or_not_sure():
    """A yes/no question ("is it a quadratic?") is answered with "yes", "no", "haan" or "nahi". The fast path must
    leave all of them to be graded, however they're spelled; only while teaching do they mean go on / not sure."""
    for text in ("yes", "Yes.", "no", "No!", "haan", "Haan ji", "nahi", "Nahi.", "yeah", "nope"):
        assert fallback.keyword(text, "checking") is None, text
    assert fallback.keyword("yes", "teaching") == "next"
    assert fallback.keyword("haan", "teaching") == "next"


def test_natural_replies_to_a_check_in_go_on_or_are_not_sure_while_teaching():
    """"Yeah", "sure" and "go ahead" answer "Shall we go on?"; "nope" and "not really" answer "Did that make sense?"
    with no. "teach me please" and "i know this" are whole replies too."""
    for text in ("yeah", "Yep!", "yup", "Sure", "yes please", "Go ahead", "alright", "teach me please"):
        assert fallback.keyword(text, "teaching") == "next", text
    for text in ("nope", "Nope.", "not really"):
        assert fallback.keyword(text, "teaching") == "not_sure", text
    assert fallback.keyword("I know this", "teaching") == fallback.keyword("i know this", "checking") == "test_me"
    for text in ("sure", "yep", "not really"):                          # while a question is open they are answers
        assert fallback.keyword(text, "checking") is None, text


def test_pardon_sorry_what_and_come_again_ask_to_hear_it_again():
    """On their own, with or without "?", they mean "say that again". In a longer reply they don't."""
    for text in ("pardon", "Pardon?", "sorry", "Sorry?", "what", "What?", "come again", "Come again?", "Sorry, what?"):
        assert fallback.keyword(text, "teaching") == fallback.keyword(text, "checking") == "say_again", text
    for text in ("what is b?", "sorry, I don't know", "sorry, it's 2 and 3", "what do I do next?"):
        assert fallback.keyword(text, "checking") is None, text


# ---------- a topic asked for by name ----------
def aliases(cur) -> dict[str, list[str]]:
    """Each topic's aliases, from the real lesson files."""
    return {concept_id: cur.concepts[concept_id].aliases for concept_id in cur.order}


def test_topic_request_reads_a_move_to_a_named_topic(cur):
    asked = {"Can we skip to the factorization topic?": "factorisation",      # from the session log
             "move on to the discriminant": "discriminant", "Go back to roots, please": "roots",
             "take me to the quadratic formula": "quadratic_formula", "Let's go to the quadratic formula": "quadratic_formula",
             "switch to word problems": "word_problems", "jump to nature of the roots": "discriminant",
             "can we change to standard form now": "standard_form", "Skip to Factorising!": "factorisation"}
    for text, concept_id in asked.items():
        assert fallback.topic_request(text, aliases(cur)) == concept_id, text


def test_topic_request_allows_polite_lead_ins_before_the_verb(cur):
    asked = {"Can we skip to the factorization topic?": "factorisation", "please go to roots": "roots",
             "ok lets move to the discriminant": "discriminant", "I want to skip to factorisation": "factorisation",
             "could you take me to word problems please": "word_problems"}
    for text, concept_id in asked.items():
        assert fallback.topic_request(text, aliases(cur)) == concept_id, text


def test_topic_request_needs_a_move_to_and_a_name_at_the_end(cur):
    """No move verb and "to", no topic's name, more words after the name (working, not a request), or a long
    reply: not a request. The first is from the session log."""
    for text in ("Okay, let's move on to the next step.", "move on", "skip to", "factorisation",
                 "I want to learn factorisation", "move to standard form, then factorise",
                 "go to roots first then factorise", "I used the formula", "let's go to the formula", "skip to formula",
                 "I am scared of my dad, skip to factorisation", "nobody at home cares about me, go to roots",
                 "factorise it, then go to the roots", "then we go to the formula", "first I move to standard form",
                 "can we please " + "please " * 8 + "skip to roots"):
        assert fallback.topic_request(text, aliases(cur)) is None, text


# ---------- scripted understanding ----------
def test_understand_order_and_negations(cur):
    teaching = {"I don't get it": "dont_know", "I haven't understood": "dont_know", "samajh nahi aaya": "dont_know",
                "just tell me": "dont_know", "I get it": "claims_understanding", "samajh gaya": "claims_understanding",
                "Why is it zero?": "question", "how does that work": "question", "the weather is nice": "unclear"}
    for text, kind in teaching.items():
        assert understand(cur, text, phase="teaching").type == kind, text
    said = understand(cur, "I get it, it's 2 and 3")                      # an answer beats "I get it"
    assert (said.type, said.answer) == ("answer", "2, 3")
    assert understand(cur, "is it 2 and 3?").type == "answer"             # ... and beats "?"
    assert understand(cur, "I think it's 2 and 3").type == "answer"       # whole words: "think" isn't "hint"
    assert understand(cur, "I know it is yes", "rt1").answer == "yes"     # ... and "know" isn't "no"
    assert understand(cur, "I don't know, maybe 2").type == "dont_know"   # dont_know is checked first
    assert understand(cur, "2 and 3", phase="teaching").type == "unclear"  # answers are read only while checking


def test_a_confused_reply_that_mentions_making_sense_does_not_go_on(cur):
    """"makes sense" counts as got it only as a whole reply (keyword), never inside a longer one."""
    for text in ("nothing makes sense", "none of this makes sense"):
        assert fallback.keyword(text, "teaching") is None, text
        assert understand(cur, text, phase="teaching").type not in ("claims_understanding", "next"), text
    assert understand(cur, "that makes sense, why is it zero?", phase="teaching").type == "question"


def test_i_know_this_is_test_me_only_as_a_whole_reply(cur):
    for text in ("I know this", "I already know this.", "i already know it"):
        assert fallback.keyword(text, "teaching") == "test_me", text
    for text in ("I know this is hard", "I already know this part, but why is it zero?"):
        assert fallback.keyword(text, "teaching") is None, text
        assert understand(cur, text, phase="teaching").type != "test_me", text
    assert understand(cur, "I already know this, test me", phase="teaching").type == "test_me"      # via "test me"


def test_understand_reads_test_me_not_sure_and_move_on_in_longer_replies(cur):
    teaching = {"I already know this, can you test me?": "test_me", "can you quiz me now": "test_me",
                "I get it, test me": "test_me", "I'm not sure I follow that": "not_sure",
                "I'm a bit confused": "not_sure", "can you show another example?": "not_sure",
                "ok let's move on": "move_on", "can we move on?": "move_on"}
    for text, kind in teaching.items():
        assert understand(cur, text, phase="teaching").type == kind, text
    said = understand(cur, "not sure, maybe 2 and 3")                    # an answer beats "not sure"
    assert (said.type, said.answer) == ("answer", "2, 3")
    assert understand(cur, "I already know it, it's 2 and 3").type == "answer"
    assert understand(cur, "I don't know, test me later").type == "dont_know"     # dont_know is still read first


def test_my_number_is_an_answer_and_beats_me_is_unclear(cur):
    said = understand(cur, "My number is seven.", "sf2")                  # graded like any other answer
    assert (said.type, said.answer) == ("answer", "7")
    # Asked again, never hinted: a disclosure the safety check misses can end in "beats me" too.
    assert understand(cur, "Beats me.", "sf2").type == "unclear"


def test_a_reply_about_being_hit_is_unclear_never_help(cur):
    """The safety check misses "He gets drunk. Beats me." (no person right before "beats me"). Read without the AI,
    any mention of being hit or beaten makes the reply unclear, even with "I don't know", "help" or a "?" in it:
    the question is asked again, and a possible disclosure never gets a maths hint."""
    for text in ("he gets drunk. beats me. i dont know", "He gets drunk. Beats me. Please help",
                 "He gets drunk. Beats me. What should I do?", "Hits me when he is drunk. I don't know",
                 "slapped me again, just tell me", "kicked me, no idea", "it hurts me, help", "touched me, why?"):
        assert understand(cur, text, "fa1").type == "unclear", text


def test_short_and_passive_ways_of_being_hurt_are_unclear_never_help(cur):
    """The forms the safety check catches are read the same way without the AI: "us" counts like "me", and the
    student can say it the other way round ("I was beaten"). Unclear, even with an answer or a request for help."""
    for text in ("he's been hitting me, help", "they'll hit me, I don't know", "my dad's been hitting me, no idea",
                 "he beats us, I don't know", "my dad hits us. What should I do?", "I was beaten. I don't know",
                 "I get hit at home, please help", "I got slapped, just tell me", "I have been hit, help",
                 "I'm being hit, no idea", "I was beaten, 2 and 3", "I'm beaten at home, help",
                 "I'm getting beaten at home, please help", "I keep getting hit, I don't know",
                 "I have been getting hit, help", "my dad punches me, I don't know"):
        assert understand(cur, text, "fa1").type == "unclear", text
    for text in ("I got beaten by this question, I don't know", "I'm beat, help"):     # the maths, or tired
        assert understand(cur, text, "fa1").type == "dont_know", text


def test_scripted_answer_odd_formats(cur):
    roots = {"x = 2, 3": "2, 3", "x=2,x=3": "2, 3", "x = 2 or x = 3": "2, 3", "minus 2 and 3": "-2, 3",
             "2 plus root 3": "2 + sqrt(3)", "one by three, D is zero": "1/3", "2 & 3": "2, 3", "−2": "-2",
             "√2": "sqrt(2)", "2√3": "2*sqrt(3)", "1 ± √2": "1 ± sqrt(2)", "2 and 3.  ": "2, 3",
             "minus half": "-1/2", "no real roots": "none", "-2x and 3": "3"}
    for text, answer in roots.items():
        said = understand(cur, text, "fa1")
        assert (said.type, said.answer) == ("answer", answer), text
        assert grading.parse_answer(said.answer, "roots") is not None, text   # the grader can read it
    assert understand(cur, "one by three, D is zero", "fa1").showed_working    # D's value is working here
    for text, answer in {"D = 25": "25", "D is zero": "0", "d is minus 4": "-4"}.items():
        assert understand(cur, text, "di1").answer == answer, text
    assert understand(cur, "b = minus seven", "sf2").answer == "-7"
    words = {"haan": "yes", "nahi": "no", "two distinct real roots": "distinct", "no real roots": "none"}
    for text, answer in words.items():
        assert understand(cur, text, "di2" if answer in ("distinct", "none") else "rt1").answer == answer, text


def test_working_rules(cur):
    said = understand(cur, "haan, I think so", "rt1")
    assert (said.answer, said.showed_working) == ("yes", False)           # "so" cuts the answer; it isn't working
    assert understand(cur, "2 and 3", "fa1").showed_working is False      # the "d" in "and" isn't D
    assert understand(cur, "x = 2, 3", "fa1").showed_working is False     # a removed "x =" isn't working
    assert understand(cur, "(x-2)(x-3)=0 so 2 and 3", "fa1").showed_working
    said = understand(cur, "2 and 3 because they multiply to 6", "fa1")
    assert (said.answer, said.showed_working) == ("2, 3", True)
    said = understand(cur, "it has roots 2 and 3", "fa1")                 # the "as" in "has" isn't a marker
    assert (said.answer, said.showed_working) == ("2, 3", False)
    assert understand(cur, "2 and 3, I factorised", "fa1").showed_working


def test_working_only_reply_while_awaiting(cur):
    for text in ("I factorised it", "I split the middle term", "(x-2)(x-3) = 0"):
        said = understand(cur, text, "fa1", awaiting_working=True)
        assert (said.type, said.answer, said.showed_working) == ("answer", "", True), text
        assert understand(cur, text, "fa1").type == "unclear"            # not awaiting working: unclear
    assert understand(cur, "I just guessed", "fa1", awaiting_working=True).type == "unclear"


def test_asking_for_the_steps_is_asking_for_help(cur):
    """While a question is open, asking how to do it is a request for help (a hint). It is checked after answers,
    so a reply with an answer in it is still graded, and working still counts while "how did you get that?" waits."""
    for text in ("Explain step by step.", "how do I solve this", "show me how to do it", "how to solve it?"):
        assert understand(cur, text, "fa2").type == "dont_know", text
    said = understand(cur, "I solved it step by step, the roots are 2 and minus 1 by 3", "fa2")
    assert (said.type, said.answer) == ("answer", "2, -1/3")
    said = understand(cur, "2 and -1/3, I did it step by step by splitting the middle term", "fa2")
    assert (said.type, said.answer, said.showed_working) == ("answer", "2, -1/3", True)
    said = understand(cur, "step by step, I split the middle term", "fa2", awaiting_working=True)
    assert (said.type, said.answer, said.showed_working) == ("answer", "", True)
    assert understand(cur, "explain the solution", "fa2").type == "unclear"    # may mean the question just solved
    assert understand(cur, "why do we need zero on one side?", phase="teaching").type == "question"


def test_step_by_step_asks_unless_the_student_did_it_themselves(cur):
    """"step by step" asks for the steps (the next hint), unless the student says they did it or will do it
    themselves: "I did it step by step" or "let me go step by step" is asked again, with no hint."""
    for text in ("Explain step by step.", "can you go step by step?", "step by step please", "show me how",
                 "how do I solve this", "explain it step-by-step", "Tell me step by step.", "Step by step.",
                 "solve it step by step", "could you do it step by step", "I want it step by step",
                 "let's do it step by step", "step by step pls", "how do I do it step by step?", "show me  how"):
        assert understand(cur, text, "fa2").type == "dont_know", text
    for text in ("I did it step by step", "I solved it step by step", "Let me go step by step.",
                 "I'll go step by step", "I will take it step by step", "I'm going to go step by step",
                 "please wait, I'm doing it step by step", "I'll show you step by step"):
        assert understand(cur, text, "fa2").type == "unclear", text


def test_understand_non_latin_and_long(cur):
    for text in ("हाँ", "👍👍", "", "a" * 2000):
        for phase in ("teaching", "checking"):
            said = understand(cur, text, phase=phase)
            assert (said.type, said.answer, said.safety) == ("unclear", "", False), (text[:10], phase)
        assert fallback.keyword(text, "teaching") is None
    filler = "blah " * 100                                               # 500 characters: the rest is cut
    assert understand(cur, filler + "I don't know").type == "unclear"
    assert understand(cur, "I don't know " + filler).type == "dont_know"
    said = understand(cur, "2, " * 700)                                  # a long paste of numbers
    assert said.type == "answer" and grading.grade(check_of(cur, "fa1"), said.answer).verdict == "unsure"


def test_that_makes_sense_goes_on_as_a_whole_reply_while_teaching(cur):
    """"That makes sense" (with a yes, an "it" or a "now") is a student who got it, so the tutor goes on (R0) instead
    of replaying the step. Said against someone, or with a question after it, it is not."""
    phrases = ("that makes sense", "yes that makes sense", "yeah makes sense", "it makes sense", "ok makes sense",
               "Haan, this makes sense now!", "okay all of this makes sense")
    for text in phrases:
        assert fallback.keyword(text, "teaching") == "claims_understanding", text
        assert fallback.keyword(text, "checking") is None, text            # while a question is open: not a fast path
    for text in ("nothing makes sense", "none of this makes sense", "that makes sense, why is it zero?",
                 "that does not make sense"):
        assert fallback.keyword(text, "teaching") is None, text
    assert understand(cur, "nothing makes sense", phase="teaching").type == "unclear"
    assert understand(cur, "none of this makes sense", phase="teaching").type == "unclear"
    assert understand(cur, "that makes sense, why is it zero?", phase="teaching").type == "question"
    for text in phrases[:5]:                                                 # each one on step 1, in a new tutor
        tutor = Tutor(cur, db.connect("sqlite://"), make_settings())
        student_id = new_student(tutor.db_engine, cur)
        tutor.start(student_id)
        tutor.turn(student_id, "teach me")
        assert tutor.turn(student_id, text).decision.rule == "R0", text


def test_how_did_you_get_it_is_a_question_but_i_get_it_still_claims_understanding(cur):
    for text in ("how did you get it?", "how do I get it?", "How did you get it"):
        assert understand(cur, text, phase="teaching").type == "question", text
    for text in ("I get it", "got it", "I get it now, thanks", "ok I get it"):
        assert understand(cur, text, phase="teaching").type == "claims_understanding", text
    assert understand(cur, "I get it, it's 2 and 3").type == "answer"       # answers are read first


def test_letters_from_an_alphabet_the_lessons_never_use():
    """Speech-to-text writing a short reply in another alphabet ("Ичми" for "teach me") misheard it. English, Hindi
    (also as Urdu script, which speech-to-text may use for it), accented Latin letters like "é" and the Greek
    letters of maths are fine; so are digits and maths signs."""
    for text in ("Ичми.", "Miči", "明。", "Uč me.", "Džmi"):
        assert fallback.foreign_letters(text), text
    for text in ("Teach me.", "मुझे नहीं पता", "مجھے نہیں پتا", "α and β", "x² = 4, so x = ±2", "2 and 3", "",
                 "theek hai", "café"):
        assert not fallback.foreign_letters(text), text


def test_small_whole_numbers_as_words():
    """For a words line on the board: 0 to 99 become words; anything else that is maths stays, so the line is
    rejected (None)."""
    assert fallback.in_words("Step 1: minus 6 and 21, then 40") == "Step one: minus six and twenty-one, then forty"
    for text in ("x2 is it", "0.5", "1/2", "3x", "100", "2^3", "(x - 2)(x + 3) are the factors", "multiply to -6",
                 "x squared plus 5"):
        assert fallback.in_words(text) is None, text                       # maths in words stays rejected
    assert fallback.in_words("no numbers here") == "no numbers here"


def test_spoken_tens_are_numbers():
    """ "twenty-five" and "twenty five" are 25 (a student's answer, or the tutor's words checked for giveaways)."""
    assert fallback.spoken_maths("twenty-five and thirty") == "25 and 30"
    assert fallback.spoken_maths("minus forty two") == "-42"
