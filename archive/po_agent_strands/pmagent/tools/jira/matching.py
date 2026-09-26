"""Resolving what a user *named* to what Jira *has* — people and transitions.

Both functions share one contract: match exactly (after normalising case and
whitespace) or refuse and hand back the alternatives. Never fuzzy. A wrong
guess here reassigns someone's work or fires workflow post-functions, and both
are silent and annoying to undo — abstaining is the better failure.

Pure: no client, no network, so both are directly unit-testable on plain dicts.
"""

from __future__ import annotations


def pick_assignee(candidates: list[dict], query: str) -> tuple[dict | None, list[dict]]:
    """Choose one user from Jira's search results, or refuse to choose.

    Returns `(match, alternatives)`. When `match` is None the caller must ask
    the user which person they meant — guessing would assign someone's work to
    the wrong colleague, which is silent and annoying to undo.

    Inactive accounts are never matched; a deactivated user with a matching
    name would otherwise beat the real one.
    """
    active = [c for c in candidates if c.get("active", True)]
    if not active:
        return None, []

    needle = query.strip().lower()

    # An exact email address is unambiguous by construction.
    email_matches = [c for c in active if (c.get("email") or "").lower() == needle]
    if len(email_matches) == 1:
        return email_matches[0], []

    exact = [c for c in active if (c.get("display_name") or "").lower() == needle]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact

    if len(active) == 1:
        return active[0], []

    return None, active


def pick_transition(transitions: list[dict], target: str) -> tuple[dict | None, list[dict]]:
    """Choose the workflow transition that lands an issue in `target`.

    Returns `(match, ambiguous)`. `ambiguous` is non-empty only when several
    transitions match equally well; when nothing matches at all both are empty
    and the caller reports what *is* available.

    Users name the **destination** ("move it to In Review"), but a transition
    has its own name that often differs from where it leads ("Start Progress" ->
    "In Progress"). So target status is matched first, transition name second.

    Matching is exact (case- and whitespace-insensitive), never fuzzy. Same rule
    as `pick_assignee`: a wrong guess here moves real work to a wrong workflow
    state, sometimes firing notifications or post-functions on the way, so
    abstaining and listing the options is the better failure.
    """
    needle = (target or "").strip().lower()
    if not needle:
        return None, []

    for field in ("to_status", "name"):
        matches = [t for t in transitions if (t.get(field) or "").strip().lower() == needle]
        if len(matches) == 1:
            return matches[0], []
        if len(matches) > 1:
            return None, matches

    return None, []
