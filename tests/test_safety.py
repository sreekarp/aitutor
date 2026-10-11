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


def test_factor_lists_are_not_phone_numbers():
    """A phone number is 7+ digits in groups of 2 or more ("98765 43210"), or said one by one ("9 8 7 6 5 4 3").
    A list that mixes 1-digit and longer numbers, like the factor pairs of 12, is maths."""
    for text in ("1 12 2 6 3 4", "1 40 2 20 4 10 5 8", "1 132 2 66 3 44 4 33 6 22 11 12"):
        assert not safety.is_safety(text), text
    for text in ("91 98765 43210", "011 2345 6789", "987-654-3210", "9876543210", "9 8 7 6 5 4 3"):
        assert safety.is_safety(text), text
    assert safety.is_safety("11-10-2026")        # a false alarm DESIGN.md owns up to: a date looks like a phone number
