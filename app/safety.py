"""The keyword safety check. It runs before anything else reads a student's reply, and looks for distress,
someone hurting the student, or personal details like a phone number. A match gets the safety reply and a
safety_review flag on the turn. With the LLM, UNDERSTAND's own safety flag is a second net.
"""
from __future__ import annotations

import re

from app.fallback import phrases, spoken_maths

# Distress, abuse and personal details, as whole words ("self harm" also finds "self-harm").
SAFETY = phrases("kill myself", "suicide", "want to die", "end my life", "hurt myself", "self harm", "cut myself",
                 "suicidal", "wanna die", "hurting myself", "cutting myself", "abuse", "abused", "abusive",
                 "bullied", "bully me", "bullies me", "bad touch", "scared to go home", "my address",
                 "my phone number", "my mobile number", "i live at", "marna chahta", "marna chahti")
# Someone hurting the student: "my dad beats me", "he slapped me". ("Beats me" alone means "I don't know".)
HURT = re.compile(r"\b(?:he|she|they|someone|somebody|my \w+|papa|mummy|mom|dad|mother|father|brother|sister"
                  r"|uncle|teacher|friend)(?: (?:always|sometimes|often|still|also|really|even|would|used to"
                  r"|keeps|is|was))? (?:hit|beat|slap|kick|hurt|touch|abus)\w* me\b")
# Any word, then "hits me" / "beats me": "Rohan beats me", "and beats me". Not it, that, this or question:
# "it beats me" and "this question beats me" mean "I don't know".
HITS_ME = re.compile(r"\b(?!(?:it|that|this|question)\b)\w+ (?:hits|beats) me\b")
# A phone number: 7+ digits, maybe split by single spaces or hyphens ("98765 43210", "987-654-3210"), never
# part of a decimal ("0.3333333", "12345678.9"). After "my number (is)": 3 digits said one by one ("nine eight
# seven"), or 4 digits ("9876"), but not an answer like "7", "25" ("twenty five" -> "20 5") or "132".
PHONE = re.compile(r"(?<![\d.])\d(?:[ -]?\d){6,}(?!\.?\d)")
MY_NUMBER = re.compile(r"\b(?:my|mera) number(?: is)? (?:\d(?: \d){2}|\d(?:[ -]?\d){3})")


def is_safety(text: str) -> bool:
    """True for a distress phrase, someone hurting the student, or personal details like a phone number."""
    clean = " ".join(spoken_maths(text).split())       # lower case, "nine eight" -> "9 8", single spaces
    return any(rule.search(clean) for rule in (SAFETY, HURT, HITS_ME, PHONE, MY_NUMBER))
