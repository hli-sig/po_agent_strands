"""Shared setup for the examples: pick a scripted (offline) or real (--live) model.

Offline is the default so every example runs with no API key and no network,
and prints the same thing every time. `--live` swaps in the model configured
in .env (see pmagent/llm.py) — the examples use only harmless local tools, so
nothing live is ever written.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

LIVE = "--live" in sys.argv


def model(script):
    """`script` is what the scripted model will say, turn by turn (tests/fakes.py)."""
    if LIVE:
        from pmagent.llm import get_model

        return get_model()
    from tests.fakes import ScriptedModel

    return ScriptedModel(script)


def banner(title: str) -> None:
    mode = "LIVE model" if LIVE else "scripted model (offline) — add --live for a real one"
    print(f"\n=== {title} — {mode} ===\n")
