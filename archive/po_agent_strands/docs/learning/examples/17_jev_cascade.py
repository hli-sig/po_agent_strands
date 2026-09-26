"""Lesson 17 — a confidence-gated router cascade, with a FAKE Jev client (offline).

The shape you'll build in J1: ask a cheap calibrated decision model first; act on
its answer only when it's confident; otherwise fall back to the LLM router; and
when the two disagree with low confidence, ask the user. No TypeSafe call here —
the fake returns canned probabilities so you can see every branch.

    uv run docs/learning/examples/17_jev_cascade.py
"""

from dataclasses import dataclass

print("\n=== 17 Jev cascade — fake client, offline only (no --live mode) ===\n")

TAU, TAU_LOW = 0.7, 0.5


@dataclass
class JevChoice:          # the shape typesafe_sdk's ChoiceAnswer gives you
    choice: str
    confidence: float
    probabilities: dict


FAKE_JEV = {   # message -> what a Jev Choice over the routes might return
    "How is sprint 31?": JevChoice("sprint", 1.0, {"sprint": 0.99, "query": 0.01}),
    "Draft a story for the FX column": JevChoice("requirements", 0.49,
                                                  {"requirements": 0.55, "ticket": 0.43, "query": 0.02}),
    "sort out the FX thing": JevChoice("query", 0.3, {"query": 0.45, "ticket": 0.35, "sprint": 0.2}),
}
FAKE_LLM = {"Draft a story for the FX column": "ticket", "sort out the FX thing": "sprint"}


def route(message: str) -> tuple[str, str]:
    jev = FAKE_JEV.get(message)                      # real code: try/except -> None on any error
    if jev and jev.confidence >= TAU:
        return jev.choice, f"jev ({jev.confidence:.2f})"
    llm = FAKE_LLM.get(message, "query")             # real code: router.classify_with_reason
    top2 = sorted(jev.probabilities, key=jev.probabilities.get, reverse=True)[:2] if jev else []
    if jev and jev.confidence < TAU_LOW and llm not in top2:
        return "clarify", f"ask the user: {top2[0]} or {top2[1]}?"
    return llm, f"jev->llm ({jev.confidence:.2f})" if jev else "llm (jev unavailable)"


for msg in FAKE_JEV:
    r, why = route(msg)
    print(f"{msg:<34} -> {r:<9} {why}")
