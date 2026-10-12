"""The keyword safety check. It runs before anything else reads a student's reply, and looks for distress,
someone hurting the student, or personal details like a phone number. A match gets the safety reply and a
safety_review flag on the turn. With the LLM, UNDERSTAND's own safety flag is a second net.
"""
from __future__ import annotations

import re

from app.fallback import I_WAS_HIT, phrases, spoken_maths

# Distress, abuse and personal details, as whole words ("self harm" also finds "self-harm").
SAFETY = phrases("kill myself", "suicide", "want to die", "end my life", "hurt myself", "self harm", "cut myself",
                 "suicidal", "wanna die", "hurting myself", "cutting myself", "abuse", "abused", "abusive",
                 "bullied", "bully me", "bullies me", "bad touch", "scared to go home", "my address",
                 "my phone number", "my mobile number", "i live at", "marna chahta", "marna chahti")
# Someone hurting the student: "my dad beats me", "he's been hitting me", "they'll hit me", "my dad hits us".
# Apostrophes are dropped, so "he's", "she's", "they're", "they've", "they'll", "he'll" and "she'll" arrive as hes,
# shes, theyre, theyve, theyll, hell and shell. It reads the words without punctuation, so "my dad... beats me"
# counts too. ("Beats me" alone means "I don't know".) The student can also say it the other way round, "I was
# beaten": that is I_WAS_HIT, kept in fallback.py so the scripted reading can use it too (fallback.py can't import
# this file).
HURT = re.compile(r"\b(?:he|she|hes|shes|hell|shell|they|theyre|theyve|theyll|someone|somebody|my \w+|papa|mummy"
                  r"|mom|dad|mother|father|brother|sister|uncle|teacher|friend)(?: (?:always|sometimes|often|still"
                  r"|also|really|even|would|used to|keeps|keep|is|are|was|were|has been|have been|been|will))?"
                  r" (?:hit|beat|slap|kick|hurt|touch|abus)\w* (?:me|us)\b")
# Any word, then "hits me" / "beats me": "Rohan beats me", "and beats me". Not it, that, this or question:
# "it beats me" and "this question beats me" mean "I don't know".
HITS_ME = re.compile(r"\b(?!(?:it|that|this|question)\b)\w+ (?:hits|beats) me\b")
# A run of digit groups joined by single spaces or hyphens ("98765 43210", "987-654-3210"), never part of a
# decimal ("0.3333333", "12345678.9"). _has_phone_number decides which runs are phone numbers.
DIGIT_RUN = re.compile(r"(?<![\d.])\d+(?:[ -]\d+)*(?!\.?\d)")
# After "my number (is)": 3 digits said one by one ("nine eight seven"), or 4 digits ("9876"), but not an answer
# like "7", "25" ("twenty five" -> "20 5") or "132".
MY_NUMBER = re.compile(r"\b(?:my|mera) number(?: is)? (?:\d(?: \d){2}|\d(?:[ -]?\d){3})")


def is_safety(text: str) -> bool:
    """True for a distress phrase, someone hurting the student, or personal details like a phone number."""
    clean = " ".join(spoken_maths(text).split())       # lower case, "nine eight" -> "9 8", single spaces
    words = " ".join(re.findall(r"\w+", clean))        # punctuation gone: "my dad... beats me" -> "my dad beats me"
    return (any(rule.search(clean) for rule in (SAFETY, HITS_ME, MY_NUMBER))
            or any(rule.search(words) for rule in (HURT, I_WAS_HIT)) or _has_phone_number(clean))


def _has_phone_number(text: str) -> bool:
    """True for a run of 7+ digits whose groups all have 2+ digits ("98765 43210", "011 2345 6789"), or are all
    1 digit, as when a number is said one by one ("9 8 7 6 5 4 3"). One digit in front of 2+-digit groups, like
    a trunk 0 or a country code ("0 98765 43210", "+1 987 654 3210"), is set aside first. Any other mix is maths,
    like the factor pairs "1 12 2 6 3 4"."""
    for run in DIGIT_RUN.findall(text):
        groups = re.split("[ -]", run)
        if len(groups[0]) == 1 and all(len(group) >= 2 for group in groups[1:]):
            groups = groups[1:]                       # a trunk 0 or a country code like +1: the number follows
        all_long = all(len(group) >= 2 for group in groups)
        all_single = all(len(group) == 1 for group in groups)
        if sum(len(group) for group in groups) >= 7 and (all_long or all_single):
            return True
    return False
