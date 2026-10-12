"""Tests for app/safety.py: the keyword safety check, run before anything else reads a reply."""
from app import safety


def test_safety_words_and_phone_not_decimals():
    for text in ("I want to die", "sometimes I hurt myself", "self-harm", "my dad hits me",
                 "my number is nine eight seven", "call me on 9876543210", "marna chahta hoon"):
        assert safety.is_safety(text), text
    for text in ("x = 0.3333333", "1/3 is about 0.33333333", "x is 1234567.5", "123456", "2 and 3", "हाँ",
                 "12345678.9", "x = 12", "2x^2 - 3x - 5 = 0", "1/3", "D = 100 - 96 = 4", "289-240=49"):
        assert not safety.is_safety(text), text


def test_safety_answers_and_idioms_are_not_disclosures():
    """"My number is seven" is an answer and "beats me" means "I don't know": a person or a real number is needed."""
    for text in ("My number is seven.", "my number is 25", "my number is twenty five", "mera number 7 hai",
                 "Beats me.", "honestly it beats me", "this question beats me", "Oh now it hits me, it's 2 and 3"):
        assert not safety.is_safety(text), text
    for text in ("my dad beats me", "Papa beats me", "he slapped me", "my parents beat me", "Rohan hits me",
                 "my uncle touches me", "my number is 98765 43210", "98765-43210", "call me on +91 98765 43210",
                 "My number is 98765, 43210.", "my number is nine eight seven six five four three two one zero",
                 "my mobile number is 98765 43210", "(987) 654-3210", "Sir beats me", "Maa beats me",
                 "my dad gets drunk and beats me", "I feel suicidal", "I keep hurting myself", "my number is 9876"):
        assert safety.is_safety(text), text


def test_punctuation_between_the_person_and_the_verb():
    """A comma, "..." or a dash after the person still counts, and "he's" and "she's" are people too."""
    for text in ("my dad... beats me", "my dad, beats me", "my mom, hits me", "my dad - beats me", "he's hitting me",
                 "she's beating me"):
        assert safety.is_safety(text), text


def test_more_ways_to_say_someone_is_hurting_me():
    """"They're" is a person too, and are, were, has been, have been, keep or will can come between the person and
    the verb."""
    for text in ("they're hitting me", "they’re beating me", "they are hitting me", "my parents are beating me",
                 "he has been hitting me", "they keep hitting me", "they were hitting me",
                 "my brothers have been beating me", "my dad will beat me"):
        assert safety.is_safety(text), text


def test_short_and_passive_ways_to_say_someone_is_hurting_me():
    """Apostrophes are dropped, so "he's", "they've", "they'll", "he'll" and "she'll" arrive as hes, theyve, theyll,
    hell and shell; "been" can come between the person and the verb; "us" counts like "me"; and the student can say
    it the other way round: I + was, am, get, got, have been, been or being + hit, beat(en), slapped, kicked, hurt,
    touched or abused ("I'm" and "I've" arrive as im and ive)."""
    for text in ("he's been hitting me", "they've been hitting me", "they'll hit me", "he'll hit me",
                 "she'll slap me", "my dad's been hitting me", "I was beaten", "I get hit at home", "I got slapped",
                 "I have been hit", "I'm being hit", "I've been beaten", "he beats us", "my dad hits us"):
        assert safety.is_safety(text), text
    for text in ("I have hit the target", "I was beating my own record", "it beats us"):
        assert not safety.is_safety(text), text


def test_factor_lists_are_not_phone_numbers():
    """A phone number is 7+ digits in groups of 2 or more ("98765 43210"), or said one by one ("9 8 7 6 5 4 3").
    A list that mixes 1-digit and longer numbers, like the factor pairs of 12, is maths."""
    for text in ("1 12 2 6 3 4", "1 40 2 20 4 10 5 8", "1 132 2 66 3 44 4 33 6 22 11 12"):
        assert not safety.is_safety(text), text
    for text in ("91 98765 43210", "011 2345 6789", "987-654-3210", "9876543210", "9 8 7 6 5 4 3"):
        assert safety.is_safety(text), text
    assert safety.is_safety("11-10-2026")        # a false alarm DESIGN.md owns up to: a date looks like a phone number


def test_one_digit_in_front_of_a_phone_number():
    """A trunk 0 or a country code like +1, then groups of 2+ digits, is still a phone number. A factor list has
    several 1-digit numbers, so it stays maths, and so does one digit before fewer than 7 more ("1 12 13 14")."""
    for text in ("+1 987 654 3210", "0 98765 43210", "+1-987-654-3210", "call me on 0 9876543210"):
        assert safety.is_safety(text), text
    for text in ("1 12 2 6 3 4", "1 192 2 96 3 64 4 48 6 32 8 24 12 16", "1 132", "5 12 13", "1 12 13 14"):
        assert not safety.is_safety(text), text
