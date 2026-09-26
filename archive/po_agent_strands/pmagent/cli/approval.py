"""
What the human sees at the approval prompt.

Ported from the LangGraph original's `main.py` with its logic unchanged — this
part never depended on the framework. The pending calls arrive from
`PMAssistant.send()/resume()` as `{"id", "name", "args"}` dicts, the same keys
LangChain tool calls had.

The design rule, from the original: **a gate shows what could be wrong, not what
is there.** Render the field that distinguishes a right write from a wrong one —
the sprint a ticket lands in, the source issue each draft derives from, the full
comment text. An approval prompt the human can't read is a keystroke, not a gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pmagent import env
from pmagent.cli.echo import brief
from pmagent.messages import last_user_text

# Above this many drafts, the detail blocks stop being reviewable: nine
# seven-line entries whose summaries differ by one word mid-line read as a wall,
# and a batch that grew by one is invisible in it. The manifest is the scannable
# index that makes an odd row stand out before the details are read at all.
_MANIFEST_THRESHOLD = 3

# Layer 3 of the scope reconciler (docs/harness-handover.md §9.3).
#
# `create_jira_issues(scope=...)` only checks a batch if the agent *passes*
# scope, so a model that omits it skips the check entirely — a prompt asking for
# something is not a mechanism that delivers it. This is what the harness can do
# instead: notice that the user's message named several issue keys, notice that
# the pending write declared no scope, and say so.
#
# Deliberately a warning and not a block. The scan is a heuristic, and a
# heuristic that blocks is defeated by its first false positive. Its whole job is
# to make sure "the check did not run" is never invisible — the same failure mode
# as the silently-wrong read field in TUTORIAL §4.7.
_ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")
_SCOPE_WARN_MIN_KEYS = 3


def unchecked_scope_warning(messages: list[dict], calls: list[dict]) -> str | None:
    """Warn when a create batch skipped the scope check the message called for."""
    creates = [
        c for c in calls
        if c["name"] in ("create_jira_issues", "create_jira_issue")
        and not (c["args"] or {}).get("scope")
    ]
    if not creates:
        return None

    keys = sorted(set(_ISSUE_KEY.findall(last_user_text(messages))))
    if len(keys) < _SCOPE_WARN_MIN_KEYS:
        return None

    drafts = sum(len(c["args"].get("drafts") or [c["args"]]) for c in creates)
    return (
        f"NOTE: your message named {len(keys)} issue keys "
        f"({', '.join(keys[:6])}{' ...' if len(keys) > 6 else ''}) but this write "
        f"declared no scope, so the {drafts} draft(s) were NOT checked against "
        "them. Compare the 'from:' column yourself."
    )


def _clip_cell(value: str, width: int) -> str:
    """Fit a table cell, marking truncation rather than hiding it."""
    text = str(value or "")
    return text if len(text) <= width else text[: width - 1] + "…"


def draft_manifest(drafts: list[dict]) -> list[str]:
    """One line per draft: index, source issue, assignee, summary.

    Deliberately not a summary of the batch — it is the batch, at a density a
    person can actually check against the list they asked for. The source-issue
    column is the point: a draft derived from a card the user never named is
    obvious here and invisible in the detail blocks.
    """
    if len(drafts) <= _MANIFEST_THRESHOLD:
        return []

    sources = [str(d.get("source_issue") or "-") for d in drafts]
    assignees = [str(d.get("assignee") or "unassigned") for d in drafts]
    src_width = max(4, min(14, max(len(s) for s in sources)))
    who_width = max(8, min(22, max(len(a) for a in assignees)))

    rows = [
        "",
        f"  {'#':>3}  {'from':<{src_width}}  {'assignee':<{who_width}}  summary",
    ]
    for index, draft in enumerate(drafts, start=1):
        rows.append(
            f"  {index:>3}  {_clip_cell(sources[index - 1], src_width):<{src_width}}  "
            f"{_clip_cell(assignees[index - 1], who_width):<{who_width}}  "
            f"{_clip_cell(draft.get('summary', ''), 60)}"
        )

    named = [s for s in sources if s != "-"]
    if named:
        note = f"  {len(drafts)} draft(s), {len(set(named))} distinct source issue(s)"
        if len(set(named)) != len(named):
            note += " — a source appears more than once"
        if len(named) != len(drafts):
            note += f"; {len(drafts) - len(named)} draft(s) name no source"
        rows.append(note)
    else:
        rows.append(
            f"  {len(drafts)} draft(s), none naming a source issue — if these were "
            "derived from existing cards, that link is missing"
        )
    rows.append("")
    return rows


def _describe_write(call: dict) -> str:
    """Human-readable summary of a pending write, for the approval prompt."""
    name, args = call["name"], call["args"]

    if name in ("create_jira_issues", "create_jira_issue"):
        drafts = args.get("drafts") or [args]
        lines = [f"Create {len(drafts)} Jira issue(s) in {env.JIRA_PROJECT_KEY}:"]
        # The declared scope is what the batch was checked against, so it is part
        # of what is being approved: "checked against 8 keys" and "not checked at
        # all" are very different things to say yes to.
        if args.get("scope"):
            scope = args["scope"]
            lines.append(
                f"  checked against {len(scope)} key(s) you named: {', '.join(scope)}"
            )
        lines += draft_manifest(drafts)
        for index, draft in enumerate(drafts, start=1):
            lines.append(
                f"    {index}. [{draft.get('issue_type', 'Task')}] {draft.get('summary', '')}"
            )
            # Provenance first: in a per-issue batch, "which card is this for?"
            # is the question an extra draft fails, and every other field on a
            # wrong draft looks perfectly correct.
            if draft.get("source_issue"):
                lines.append(f"      from:     {draft['source_issue']}")
            if draft.get("assignee"):
                lines.append(f"      assignee: {draft['assignee']}")
            if draft.get("story_points") is not None:
                lines.append(f"      points:   {draft['story_points']}")
            # Sprint is part of what's being agreed to: a create that quietly
            # lands in the wrong sprint looks identical to a correct one here.
            if draft.get("sprint") is not None:
                lines.append(f"      sprint:   {draft['sprint']}")
            elif draft.get("sprint_id") is not None:
                lines.append(f"      sprint:   internal id {draft['sprint_id']}")
            for criterion in draft.get("acceptance_criteria") or []:
                lines.append(f"      AC: {criterion}")
        return "\n".join(lines)

    if name == "assign_jira_issue":
        return f"Assign {args.get('issue_key')} to {args.get('assignee')}"

    if name == "move_jira_issues_to_sprint":
        keys = args.get("issue_keys") or []
        return f"Move {len(keys)} issue(s) into sprint {args.get('sprint')}: {', '.join(keys)}"

    if name == "remove_jira_issues_from_sprint":
        keys = args.get("issue_keys") or []
        return f"Remove {len(keys)} issue(s) from their sprint: {', '.join(keys)}"

    if name == "transition_jira_issues":
        keys = args.get("issue_keys") or []
        return (
            f"Move {len(keys)} issue(s) to status '{args.get('status')}': "
            f"{', '.join(keys)}"
        )

    if name == "add_jira_comment":
        keys = args.get("issue_keys") or []
        # The comment body is never truncated. `_brief` would cut it off behind
        # a long key list, and approving wording you cannot see is not approval
        # — this is the one write whose exact text is the thing being agreed to.
        lines = [f"Comment on {len(keys)} issue(s): {', '.join(keys)}", ""]
        lines += [f"    | {line}" for line in (args.get("comment") or "").splitlines()]
        return "\n".join(lines)

    if name == "create_fy_budget_csv":
        # Everything here is a path or a fiscal year the user has to recognise:
        # the wrong fy_start silently produces twelve wrong months, and the
        # overwrite line is the only warning that an existing CSV is about to go.
        from pmagent.tools import finance_tools

        fy_start = args.get("fy_start")
        folder = finance_tools.resolve_input_dir(args.get("input_dir") or "")
        out = args.get("out_path") or (
            finance_tools.default_out_path(fy_start)
            if finance_tools.is_valid_fy_start(fy_start)
            else "<invalid fy_start>"
        )
        lines = [
            "Build the FY budget Snowflake-ingest CSV:",
            f"    fiscal year: {finance_tools.format_fy_start(fy_start)}",
            f"    reading:     {folder}",
        ]
        try:
            lines += [f"      - {path.name}" for path in finance_tools.collect_inputs(folder)]
        except Exception as exc:  # noqa: BLE001 — show the problem, don't hide the gate
            lines.append(f"      ! {exc}")
        lines.append(f"    writing:     {out}")
        lines.append(f"                 {Path(str(out)).stem}.metadata.json")
        if args.get("overwrite"):
            lines.append("    OVERWRITES the existing CSV and its metadata.")
        return "\n".join(lines)

    if name == "update_jira_issue":
        key = args.get("issue_key")
        changes = {k: v for k, v in args.items() if k != "issue_key" and v is not None}
        lines = [f"Update {key}:"]
        lines += [
            f"    {field}: {json.dumps(value, default=str)[:100]}"
            for field, value in changes.items()
        ]
        return "\n".join(lines)

    if name == "prepare_spreadsheet_approval_queue":
        return (
            "Add the 'PM Agent Approvals' sheet to the workbook:\n"
            f"    {args.get('workbook_url')}"
        )

    if name == "propose_spreadsheet_cell_update":
        # The value is shown in full and unquoted. A queued proposal is what the
        # human later approves *inside* the spreadsheet, so this prompt is the
        # last point at which they can see it next to its target cell.
        return (
            f"Queue a pending change to {args.get('sheet')}!{args.get('cell')}"
            f" -> {args.get('value')}\n"
            f"    in {args.get('workbook_url')}\n"
            "    (queued only — nothing changes until it is Approved in the sheet)"
        )

    if name == "apply_approved_spreadsheet_updates":
        return (
            "Apply every row already marked Approved in the workbook's queue:\n"
            f"    {args.get('workbook_url')}\n"
            "    This writes to the real cells."
        )

    # Fallback for a write nobody has written a case for. It is honest rather
    # than helpful — if you are reading raw arguments at an approval prompt,
    # the tool above this line is missing an entry.
    return f"{name}({brief(args)})"


def describe_write(call: dict) -> str:
    """`_describe_write`, but it can never raise.

    A renderer that crashes would abort the approval prompt and leave the lane
    paused on an interrupt nobody can answer. Show the failure and fall back to
    the raw call instead — the human can still read it and say no.
    """
    try:
        return _describe_write(call)
    except Exception as exc:  # noqa: BLE001
        return (
            f"{call.get('name')}({brief(call.get('args') or {})})\n"
            f"    (could not render this action: {type(exc).__name__}: {exc})"
        )
