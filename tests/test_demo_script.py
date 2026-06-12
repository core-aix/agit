"""scripts/demo.sh (#53): the runnable showcase of aGiT's scripted JSON mode.

The agent-driving happy path needs a real backend CLI, so it cannot run in the
test suite; these tests pin everything that can break without one — script
syntax, the documented interface, and argument validation (which must happen
before any directory is created).
"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo.sh"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)


def test_demo_script_is_executable_and_parses_cleanly():
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_demo_script_help_documents_the_backend_choice():
    result = _run("--help")

    assert result.returncode == 0
    assert "--backend claude|opencode" in result.stdout
    assert "--model" in result.stdout
    assert "--dir" in result.stdout


def test_demo_script_rejects_unknown_backend():
    result = _run("--backend", "nope")

    assert result.returncode == 1
    assert "Unknown backend" in result.stderr


def test_demo_script_rejects_unknown_option():
    result = _run("--frobnicate")

    assert result.returncode == 1
    assert "Unknown option" in result.stderr
