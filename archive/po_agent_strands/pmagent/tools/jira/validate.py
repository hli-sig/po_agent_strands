"""Draft validation — the machine-checkable half of `skills/ticket/SKILL.md`,
plus the scope reconciler.

Pure: no Jira calls. The standard itself lives in the Markdown skill; only the
parts a check can actually automate are mirrored here. Change the standard in
the skill first, and only then here.

The Jira-side half of validation (does this issue type exist, which fields does
this project mark required) needs `createmeta` and therefore a client, so it
lives in the `validate_ticket_drafts` tool rather than in this module.

`reconcile_scope` is a different kind of check from the rest of this module and
worth calling out. Everything else here asks "is this draft well-formed?" — a
property of one draft, in isolation. Scope asks "is this *set* the set that was
asked for?", which no individual draft can answer and which is why a batch could
be nine well-formed drafts and still wrong (TUTORIAL §4.8). It lives here because
it is pure and it validates drafts; it is separated below because the question is
not the same one.
"""

from __future__ import annotations

from pydantic import ValidationError

from pmagent.schemas import TicketDraft

# Types that must sit under a parent, per skills/ticket/SKILL.md.
_REQUIRES_PARENT = {"Sub-task"}
# Types whose value depends on having testable acceptance criteria.
_REQUIRES_ACCEPTANCE_CRITERIA = {"Story", "Bug"}
_FIBONACCI_POINTS = {1, 2, 3, 5, 8, 13}


def parse_drafts(drafts: list[dict]) -> tuple[list[TicketDraft], list[str]]:
    """Validate raw draft dicts against `TicketDraft`.

    Returns `(parsed, errors)`. Errors name the draft by position and summary so
    the agent can fix one ticket in a batch without re-drafting the rest.
    """
    parsed: list[TicketDraft] = []
    errors: list[str] = []

    for index, raw in enumerate(drafts, start=1):
        try:
            parsed.append(TicketDraft(**raw))
        except ValidationError as exc:
            label = (raw or {}).get("summary") or "<no summary>"
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            errors.append(f"Draft {index} ({label}): {detail}")

    return parsed, errors


def check_draft_standard(draft: TicketDraft) -> list[str]:
    """Check one draft against the ticket standard. Pure, no network.

    Encodes the Definition of Ready from `skills/ticket/SKILL.md`. Returns
    human-readable problems; an empty list means the draft is ready.
    """
    problems: list[str] = []

    if not draft.summary.strip():
        problems.append("summary is empty")
    elif len(draft.summary) > 80:
        problems.append(f"summary is {len(draft.summary)} chars (max 80)")

    if not draft.description.strip():
        problems.append("description is empty")

    if draft.issue_type in _REQUIRES_ACCEPTANCE_CRITERIA and not draft.acceptance_criteria:
        problems.append(f"a {draft.issue_type} needs acceptance criteria")

    if draft.issue_type in _REQUIRES_PARENT and not draft.parent_key:
        problems.append(f"a {draft.issue_type} needs parent_key")

    if draft.story_points is not None and draft.story_points not in _FIBONACCI_POINTS:
        problems.append(
            f"story_points {draft.story_points:g} is not on the Fibonacci scale "
            f"(1, 2, 3, 5, 8, 13)"
        )

    if draft.story_points == 13:
        problems.append("13 points is a smell — consider splitting before creating")

    return problems


# ---------------------------------------------------------------------------
# The scope reconciler — intent vs action
# ---------------------------------------------------------------------------
def normalise_key(key: str) -> str:
    """Canonical form of an issue key, for set comparison."""
    return (key or "").strip().upper()


def reconcile_scope(drafts: list[TicketDraft], scope: list[str]) -> list[str]:
    """Compare the set of drafts against the set of issue keys asked for.

    The intent-vs-action check. `scope` is what the *user* named; each draft's
    `source_issue` is what the *agent* proposes to act on. Returns human-readable
    differences; an empty list means the two sets agree.

    Deliberately symmetric — it reports both directions, because they are
    different mistakes:

    - **Drafted but not in scope** is the TUTORIAL §4.8 failure: a card the user
      never named, swept in by a fuzzy search, indistinguishable from a correct
      draft on every other field.
    - **In scope but not drafted** is the quieter one. It can be perfectly
      legitimate — "only the ones that are started" filters the set — which is
      exactly why it is reported rather than corrected. The user knows; this
      function cannot.

    Neither is repaired automatically. A scope mismatch is a difference between
    two intents, and no amount of context here makes one of them authoritative —
    auto-resolving it would turn a visible disagreement into a silent one.
    """
    wanted = {normalise_key(k) for k in scope if normalise_key(k)}
    if not wanted:
        return []

    problems: list[str] = []
    seen: dict[str, int] = {}
    undeclared: list[str] = []

    for index, draft in enumerate(drafts, start=1):
        source = normalise_key(draft.source_issue or "")
        if not source:
            undeclared.append(f"draft {index} ({draft.summary})")
            continue
        seen[source] = seen.get(source, 0) + 1

    extra = sorted(k for k in seen if k not in wanted)
    missing = sorted(wanted - set(seen))
    repeated = sorted(k for k, n in seen.items() if n > 1)

    if extra:
        problems.append(f"drafted but not in the scope you named: {', '.join(extra)}")
    if missing:
        problems.append(f"in the scope you named but not drafted: {', '.join(missing)}")
    if repeated:
        problems.append(
            f"more than one draft from the same source issue: {', '.join(repeated)} "
            "(fine for a deliberate split — say so and re-run without scope)"
        )
    if undeclared:
        problems.append(
            "no source_issue, so they cannot be checked against the scope: "
            + "; ".join(undeclared)
        )

    return problems
