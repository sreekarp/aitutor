"""Tests for the terminal tutor (app/cli.py) and the evals command (evals/__main__.py)."""
import subprocess

import evals.__main__ as evals_main
from app import cli


def test_cli_session_scripted(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")          # in memory, not data/cli.db
    monkeypatch.setenv("LLM_MODE", "scripted")                # never the network, even if .env has a key
    replies = iter("ok\nquit\n".splitlines())
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    cli.main(["--username", "asha"])
    out = capsys.readouterr().out
    assert "tutor: Hi Asha!" in out                           # the S0 greeting, by first name
    assert "why: R0 (next_step)" in out                       # "ok" moved on a step
    assert "q-label" not in out and "board:" in out           # still teaching: lesson lines, no question yet
    assert next(replies, None) is None                        # it stopped at "quit"


def test_evals_without_key_runs_layer1_only(monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")              # no key, even if .env has one
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, returncode=3)

    monkeypatch.setattr(evals_main.subprocess, "run", fake_run)
    assert evals_main.main() == 3                             # Layer 1's exit code is passed through
    assert len(commands) == 1 and commands[0][-3:] == ["-m", "pytest", "-q"]
    out = capsys.readouterr().out
    assert "Layer 2 needs OPENROUTER_API_KEY" in out
