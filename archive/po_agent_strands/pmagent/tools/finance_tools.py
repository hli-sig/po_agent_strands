"""Agent-facing tools for the FY budget converter.

The calculation lives entirely in `pmagent/tools/fy_budget/` — Finance's package,
absorbed unchanged (see its `PROVENANCE.md`). This module does four things and
deliberately nothing else:

1. selects the input files in a folder,
2. calls `pipeline.convert_fy_budget(file_paths, fy_start, out_path)`,
3. checks the validation result,
4. renders all of it as text.

It does not compute, allocate, map, round or reshape a single figure. If you find
yourself adding arithmetic here, it belongs in the package instead.

**Three tools, one of them gated.** `inspect_fy_budget_inputs` and
`read_fy_budget_run` only read; `create_fy_budget_csv` writes a CSV and its audit
sidecar, so it is in `WRITE_TOOL_NAMES` and the approval gate halts before it runs.

The inspect tool exists for the same reason `find_jira_user` sits next to
`assign_jira_issue`: a write needs a value the model cannot know. `fy_start` is
that value — 202502 for a Feb-start FY26 workbook, 202607 for the FY27 merged
one — and Finance's rule is that it must match the first visible month in the
source, never be inferred. Making the look-up free is what stops the model
guessing to avoid an approval prompt.

Every number in the output of these tools comes from the converter or from
`len()`. The model narrates; it never restates a figure the tool didn't return.
"""

from __future__ import annotations

import json
from pathlib import Path

from strands import tool

from pmagent import env


# What the folder scan will pick up. Excel is the real input; the others exist
# because a legacy daily-rate table is sometimes a csv/tsv export.
INPUT_SUFFIXES = (".xlsx", ".csv", ".tsv", ".txt")

# Roles `detect.py` recognises, in the order worth reading them.
ROLE_LABELS = {
    "fy27_merged": "FY27 merged workbook (all views + embedded Core/CW daily allocations)",
    "core": "core workbook ('Total by month')",
    "custplant": "customer/plant workbook ('Customer view' + 'Plant_View')",
    "dailyrate": "optional daily-rate table (date + weight columns)",
}

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Long failure lists are clipped, and the clip is always announced — a truncated
# failure list reading as the whole story is the same lie as a truncated search.
_MAX_FAILURES_SHOWN = 20


class FyBudgetError(RuntimeError):
    """Raised when inputs cannot be used. Rendered to the agent, never swallowed."""


# ---------------------------------------------------------------------------
# Pure helpers — no I/O, no pandas, unit-tested in tests/test_finance_tools.py
# ---------------------------------------------------------------------------


def fy_months(fy_start: int) -> list[int]:
    """The twelve YYYYMM months a run will emit, from the package's own calendar.

    Delegates rather than reimplementing: the fiscal calendar is finance logic
    and there must be exactly one copy of it. Imported lazily because the
    package pulls in pandas and openpyxl, and a lane that never touches finance
    shouldn't pay that at import time (same reasoning as `jira.get_client`).
    """
    from pmagent.tools.fy_budget import convert_budget as cb

    return cb.fy_months(fy_start)


def format_fy_start(fy_start: int) -> str:
    """'202607 (Jul 2026 - Jun 2027)'. Used wherever a fiscal year is shown."""
    if not is_valid_fy_start(fy_start):
        return str(fy_start)
    months = fy_months(fy_start)
    return f"{fy_start} ({_month_label(months[0])} - {_month_label(months[-1])})"


def _month_label(yyyymm: int) -> str:
    year, month = divmod(int(yyyymm), 100)
    return f"{_MONTH_NAMES[month - 1]} {year}"


def is_valid_fy_start(fy_start) -> bool:
    """A YYYYMM in a plausible range. Cheap guard against '2026' or '20260701'."""
    try:
        year, month = divmod(int(fy_start), 100)
    except (TypeError, ValueError):
        return False
    return 2000 <= year <= 2100 and 1 <= month <= 12


def check_fy_start(fy_start) -> None:
    """Raise with a usable message when `fy_start` isn't a fiscal month."""
    if not is_valid_fy_start(fy_start):
        raise FyBudgetError(
            f"fy_start must be the YYYYMM of the FIRST fiscal month, got {fy_start!r}. "
            "FY27's merged workbook is 202607 (Jul 2026 - Jun 2027); FY26 and "
            "earlier ran Feb-Jan, so 202502. Confirm it against the first visible "
            "month in the source workbook — never infer it."
        )


def render_input_report(
    folder: Path,
    files: list[Path],
    roles: dict,
    warnings: list[str],
    out_path: Path | None,
) -> str:
    """Render what a conversion would read, and what it still needs from a human.

    `out_path` is None when the caller can't know it yet — the default filename
    embeds `fy_start`, and for a legacy workbook that value is the user's to
    supply. Say that rather than checking an invented path.
    """
    lines = [f"FY budget inputs in {folder}:", ""]
    for path in sorted(files):
        matched = next((role for role, p in roles.items() if Path(p) == path), None)
        label = ROLE_LABELS.get(matched, "not used — no recognised sheets/columns")
        lines.append(f"  {path.name}")
        lines.append(f"    -> {label}")

    merged = "fy27_merged" in roles
    lines.append("")
    if merged:
        lines.append("Format: FY27 merged workbook. fy_start must be 202607 "
                     f"({format_fy_start(202607)}); the workbook's own month headers "
                     "are checked against it and the run aborts on a mismatch.")
    else:
        lines.append("Format: legacy monthly workbooks.")
        if "dailyrate" in roles:
            lines.append("A daily-rate table was supplied: it must cover every day of "
                         "all 12 fiscal months or the run aborts.")
        else:
            lines.append("No daily-rate table: each month will be split evenly across "
                         "its days. Monthly totals are unaffected.")
        lines.append("fy_start is NOT inferable from a legacy workbook — ask which "
                     "month the budget starts. FY26 and earlier ran Feb-Jan (202502).")

    for warning in warnings:
        lines.append(f"  ! {warning}")

    lines.append("")
    if out_path is None:
        lines.append(
            "Output would be written to "
            f"{Path(env.FY_BUDGET_OUTPUT_DIR) / 'fy_budget_<fy_start>.csv'} — the "
            "filename depends on the fiscal start, so there is nothing to check "
            "for a clash until that is confirmed."
        )
    elif out_path.exists():
        lines.append(f"NOTE: {out_path} already exists. Converting will refuse to "
                     "replace it unless overwrite=true is passed.")
    else:
        lines.append(f"Output would be written to {out_path} (does not exist yet).")
    return "\n".join(lines)


def render_run_report(result: dict) -> str:
    """Render a `convert_fy_budget` result. The verdict is the first line.

    A run that wrote a CSV but failed validation is not a success, and must not
    be summarised as one — the file exists, which is exactly why saying so
    matters. Same honesty rule as `_render_batch_result` in `jira/render.py`.
    """
    summary = result.get("summary") or {}
    failures = result.get("validation_failures") or []

    if failures:
        lines = [
            f"FY budget conversion FAILED validation — {len(failures)} failure(s).",
            "The CSV was written but must NOT be used or ingested. Do not edit it "
            "to make it pass; the mapping or the source is wrong.",
        ]
    else:
        lines = ["FY budget conversion passed validation."]

    fy_start = summary.get("fiscal_year_start")
    lines += [
        "",
        f"  Output:   {result.get('out_path')}",
        f"  Metadata: {result.get('metadata_path')}",
        f"  Fiscal year: {format_fy_start(fy_start) if fy_start else 'unknown'}",
        f"  Total rows: {summary.get('total_rows')}",
        f"  Daily distribution: {summary.get('daily_distribution')}",
    ]

    partitions = summary.get("rows_by_partition") or {}
    if partitions:
        lines.append("  Rows by partition:")
        lines += [f"    {name}: {count}" for name, count in partitions.items()]

    steps = result.get("steps") or []
    if steps:
        lines.append("  Steps:")
        lines += [f"    - {step}" for step in steps]

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("  Warnings (report these to the user):")
        lines += [f"    ! {warning}" for warning in warnings]

    if failures:
        shown = failures[:_MAX_FAILURES_SHOWN]
        lines.append(f"  Validation failures ({len(failures)}):")
        lines += [f"    - {failure}" for failure in shown]
        if len(failures) > len(shown):
            lines.append(f"    ... showing {len(shown)} of {len(failures)}.")

    return "\n".join(lines)


def render_metadata(metadata: dict, path: Path) -> str:
    """Render an audit sidecar — what a CSV was built from, and whether it held up."""
    failures = metadata.get("validation_failures") or []
    internal = metadata.get("internal_validation", "unknown")
    schema = metadata.get("schema_validation", "unknown")
    verdict = (
        "PASSED" if internal == "passed" and schema == "passed" and not failures
        else "NOT a clean run"
    )

    inputs = metadata.get("inputs") or {}
    fy_start = metadata.get("fiscal_year_start")
    lines = [
        f"Audit metadata from {path} — {verdict}.",
        "",
        f"  Generated:   {metadata.get('generated_at_utc')}",
        f"  Output CSV:  {metadata.get('output_csv')}",
        f"  Fiscal year: {format_fy_start(fy_start) if fy_start else 'unknown'}",
        f"  Total rows:  {metadata.get('total_rows')}",
        f"  Daily distribution: {metadata.get('daily_distribution')}",
        f"  Schema validation:   {schema}",
        f"  Internal validation: {internal}",
        "  Inputs:",
    ]
    lines += [
        f"    {role}: {value}"
        for role, value in inputs.items()
        if value and value != "None"
    ]

    partitions = metadata.get("rows_by_partition") or {}
    if partitions:
        lines.append("  Rows by partition:")
        lines += [f"    {name}: {count}" for name, count in partitions.items()]

    if failures:
        shown = failures[:_MAX_FAILURES_SHOWN]
        lines.append(f"  Validation failures ({len(failures)}):")
        lines += [f"    - {failure}" for failure in shown]
        if len(failures) > len(shown):
            lines.append(f"    ... showing {len(shown)} of {len(failures)}.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Filesystem plumbing
# ---------------------------------------------------------------------------


def resolve_input_dir(input_dir: str = "") -> Path:
    return Path(input_dir or env.FY_BUDGET_INPUT_DIR).expanduser()


def default_out_path(fy_start: int) -> Path:
    """`fy_budget_202607.csv` in the configured output folder.

    Named after `fy_start` rather than "FY27", because the fiscal-year label is a
    business naming convention this module has no business inventing — FY26 began
    in February and FY27 in July.
    """
    return Path(env.FY_BUDGET_OUTPUT_DIR).expanduser() / f"fy_budget_{fy_start}.csv"


def collect_inputs(folder: Path) -> list[Path]:
    """Every candidate source file in a folder, ignoring Excel's `~$` lock files."""
    if not folder.is_dir():
        raise FyBudgetError(
            f"Input folder does not exist: {folder}. Put the approved workbooks "
            f"there, or pass input_dir explicitly."
        )
    files = [
        path
        for path in sorted(folder.iterdir())
        if path.is_file()
        and path.suffix.lower() in INPUT_SUFFIXES
        and not path.name.startswith("~")
    ]
    if not files:
        raise FyBudgetError(
            f"No {'/'.join(INPUT_SUFFIXES)} files in {folder}. The converter reads "
            "approved source workbooks; it cannot proceed without them."
        )
    return files


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def inspect_fy_budget_inputs(input_dir: str = "", out_path: str = "") -> str:
    """Show which FY budget source files are present and what role each plays.

    Read-only — reads nothing but sheet names and column headers, converts
    nothing, writes nothing. Call this BEFORE `create_fy_budget_csv` so you can
    tell the user which workbook was matched to which role, whether the format is
    the FY27 merged workbook or the legacy pair, and whether a daily-rate table
    was supplied. It also reports whether the output file already exists.

    It does NOT tell you `fy_start` for a legacy workbook — that must come from
    the user, matching the first visible month in the source.

    Args:
        input_dir: Folder holding the source workbooks. Defaults to
            FY_BUDGET_INPUT_DIR.
        out_path: The CSV path you intend to write, to check for a clash.
            Defaults to the same path `create_fy_budget_csv` would choose.
    """
    from pmagent.tools.fy_budget.detect import detect_roles_with_warnings

    try:
        folder = resolve_input_dir(input_dir)
        files = collect_inputs(folder)
        roles, warnings = detect_roles_with_warnings(files)
    except Exception as exc:  # noqa: BLE001 — a bad folder is an answer, not a crash
        return f"Could not read FY budget inputs: {type(exc).__name__}: {exc}"

    if out_path:
        target = Path(out_path).expanduser()
    elif "fy27_merged" in roles:
        # The only case where the fiscal start is a property of the file itself.
        target = default_out_path(202607)
    else:
        target = None
    return render_input_report(folder, files, roles, warnings, target)


@tool
def create_fy_budget_csv(
    fy_start: int,
    input_dir: str = "",
    out_path: str = "",
    overwrite: bool = False,
) -> str:
    """Convert approved FY budget workbooks into the daily Snowflake-ingest CSV.

    Writes a 24-column CSV (one row per entity per calendar day) plus a
    `.metadata.json` audit sidecar beside it. Every figure — Budget, GP, daily
    allocation, fiscal months, partition mapping — is computed by the converter
    and validated against the source; never restate or adjust one yourself.

    Returns a report whose first line is the verdict. A run counts as successful
    only when it says validation passed AND no failures are listed. A failed run
    still leaves a CSV on disk — say so, and never present it as usable.

    Args:
        fy_start: YYYYMM of the FIRST fiscal month. 202607 for the FY27 merged
            workbook (Jul 2026 - Jun 2027); 202502 for a Feb-start FY26 workbook.
            This must be confirmed by the user against the first visible month in
            the source. Do not infer it, and do not retry with a different value
            to make a mismatch go away.
        input_dir: Folder holding the source files. Defaults to
            FY_BUDGET_INPUT_DIR. Roles are detected by content, not filename.
        out_path: Output CSV path. Defaults to
            FY_BUDGET_OUTPUT_DIR/fy_budget_<fy_start>.csv.
        overwrite: Must be true to replace an existing CSV. Ask the user first.
    """
    try:
        check_fy_start(fy_start)
        folder = resolve_input_dir(input_dir)
        files = collect_inputs(folder)
        target = Path(out_path).expanduser() if out_path else default_out_path(fy_start)
        if target.exists() and not overwrite:
            return (
                f"Refused: {target} already exists and overwrite is false. "
                "Confirm with the user, then call again with overwrite=true, or "
                "choose a different out_path. Use read_fy_budget_run to see what "
                "the existing file was built from."
            )
        # Imported here, after the guards: this pulls in pandas and openpyxl, and
        # a refused or misconfigured call should cost nothing.
        from pmagent.tools.fy_budget.pipeline import convert_fy_budget

        target.parent.mkdir(parents=True, exist_ok=True)
        result = convert_fy_budget(files, int(fy_start), target)
    except Exception as exc:  # noqa: BLE001 — the agent must be able to answer with this
        return (
            f"FY budget conversion FAILED: {type(exc).__name__}: {exc}\n"
            "Nothing usable was produced. Do not work around this — report it and "
            "ask for the correct source file or fiscal start."
        )

    return render_run_report(result)


@tool
def read_fy_budget_run(csv_path: str) -> str:
    """Read the audit metadata for a generated FY budget CSV.

    Read-only. Answers "where did this file come from and did it validate?" from
    the `.metadata.json` sidecar written next to every generated CSV: source
    files, fiscal months, distribution mode, rows per partition, schema and
    roll-up validation status.

    Args:
        csv_path: Path to the generated CSV, or to the `.metadata.json` itself.
    """
    path = Path(csv_path).expanduser()
    if path.suffix.lower() != ".json":
        path = path.with_name(f"{path.stem}.metadata.json")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (
            f"No audit metadata at {path}. Every generated CSV has one beside it; "
            "if it is missing, the CSV was not produced by this tool or the pair "
            "was split up — they must be kept together."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Could not read {path}: {type(exc).__name__}: {exc}"

    return render_metadata(metadata, path)


# Declared for the gate reconciliation in `tests/test_agent_lanes.py` — see the
# note in `tools/spreadsheet_tools.py`. The read/write pairing here is the point
# of the lane: `fy_start` is unknowable to the model, so the inspection that
# reveals it must be callable without going through the approval gate.
READ_TOOLS = [inspect_fy_budget_inputs, read_fy_budget_run]
WRITE_TOOLS = [create_fy_budget_csv]
