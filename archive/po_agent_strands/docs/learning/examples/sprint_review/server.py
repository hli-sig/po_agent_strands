"""The sprint review analyser as an MCP server you own (stdio transport).

Lesson 8's case study. `mcp_server.py` is the smallest possible server; this is
one shaped by a real design review (the handover package's DESIGN_REVIEW.md).
The original sprint-review MCP took any host path and overwrote a predictably
named deck. This version fixes both findings **inside the server**, because a
server can't trust the host model to be careful:

* **Path trust.** Every file stays under one `--root` folder. A CSV name that
  resolves outside it (`../../.env`, an absolute path, a symlink out) is
  refused, and so is anything that isn't a `.csv` file.
* **No silent overwrite.** `save_metrics` writes `runs/<run_name>/metrics.json`
  into a *new* folder and refuses a run name that already exists.
* **Numbers from code.** Both tools call `analyser.analyse`; the model never
  computes a count.
* **Refusals the model can read.** A refusal raises `RefusedError`, an MCP
  `ToolError`, so its message reaches the model. Any other exception is a crash,
  and its text stays on the server.

The tools carry MCP annotations (`readOnlyHint`, `destructiveHint`, ...). Those
are **hints from the server, not trust**: Strands passes them through untouched,
and this repo's gate still treats every discovered tool as a write until a human
lists it in `pmagent/tools/mcp_tools.py::APPROVED_READ_TOOLS`.

    uv run python -m sprint_review.server --root <folder>   # from docs/learning/examples

`08_sprint_review_mcp.py` starts it this way and talks to it.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from sprint_review.analyser import CsvError, analyse

RUNS_DIR = "runs"
_RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class RefusedError(ToolError):
    """A request the server will not carry out (outside the root, overwrite, ...).

    A `ToolError` on purpose: MCPServer sends its message to the model as an
    error result, so the model can explain or retry. Any *other* exception is
    treated as a crash, and the model only sees "Error executing tool <name>".
    """


# Each helper takes `root` already resolved (create_server does it once), so a
# containment check compares real paths on both sides.


def resolve_csv(root: Path, csv_name: str) -> Path:
    """`csv_name` under `root`, or RefusedError. Resolves symlinks before checking."""
    try:
        path = (root / csv_name).resolve()
        is_file = path.is_file()
    except (OSError, ValueError):  # a NUL byte, a name too long, a symlink loop...
        raise RefusedError(f"{csv_name!r} is not a usable file name.") from None
    if not path.is_relative_to(root):
        raise RefusedError(f"{csv_name!r} is outside the sprint export folder.")
    if path.suffix.lower() != ".csv":
        raise RefusedError(f"{csv_name!r} is not a .csv file.")
    if not is_file:
        raise RefusedError(f"No CSV named {csv_name!r} in the sprint export folder.")
    return path


def list_exports(root: Path) -> list[str]:
    """Exactly the names `resolve_csv` accepts, sorted."""
    names = []
    for p in sorted(root.rglob("*")):
        name = p.relative_to(root).as_posix()
        try:
            resolve_csv(root, name)
        except RefusedError:
            continue
        names.append(name)
    return names


def run_analysis(root: Path, csv_name: str, sprint_start: str | None = None,
                 exclude_unfinished_assignee_first_names: list[str] | None = None,
                 treat_as_in_progress_issue_keys: list[str] | None = None,
                 include_evidence: bool = True) -> dict:
    """`analyser.analyse` behind the server's guards; host paths never leave the server."""
    path = resolve_csv(root, csv_name)
    try:
        start = date.fromisoformat(sprint_start) if sprint_start else None
    except ValueError:
        raise RefusedError(f"sprint_start must be YYYY-MM-DD, got {sprint_start!r}.") from None
    try:
        result = analyse(
            path,
            sprint_start=start,
            exclude_unfinished_assignee_first_names=exclude_unfinished_assignee_first_names or (),
            treat_as_in_progress_issue_keys=treat_as_in_progress_issue_keys or (),
        )
    except (CsvError, csv.Error) as exc:  # the analyser stays MCP-free; the boundary translates
        raise RefusedError(f"{csv_name!r} can't be analysed: {exc}") from None
    except OSError as exc:  # e.g. not readable by the server's user
        raise RefusedError(f"{csv_name!r} can't be read: {exc.strerror}.") from None
    # The result lands in the model's conversation: cite the export by name, not
    # by where it sits on this machine.
    result["source"]["csv_path"] = path.relative_to(root).as_posix()
    if not include_evidence:
        result.pop("evidence")
    return result


def save_run(root: Path, run_name: str, analysis: dict) -> str:
    """Write `runs/<run_name>/metrics.json` into a new folder; never overwrite."""
    if not _RUN_NAME.fullmatch(run_name):
        raise RefusedError("run_name must be 1-64 letters, digits, '.', '_' or '-', "
                           "starting with a letter or digit.")
    runs = root / RUNS_DIR
    try:
        runs.mkdir(exist_ok=True)
        # A `runs` symlink would carry the write outside the root.
        inside = runs.resolve() == runs
    except OSError:  # e.g. `runs` exists as a file
        inside = False
    if not inside:
        raise RefusedError(f"{RUNS_DIR}/ must be a real folder inside the sprint export folder.")
    try:
        (runs / run_name).mkdir()  # atomic: exactly one caller can create a run folder
    except FileExistsError:
        raise RefusedError(f"Run {run_name!r} already exists. Choose a new run name; "
                           "an existing review is never overwritten.") from None
    target = runs / run_name / "metrics.json"
    target.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    return target.relative_to(root).as_posix()


def create_server(root: Path) -> MCPServer:
    """The MCP server, with three tools bound to `root`."""
    root = root.resolve()
    server = MCPServer(
        "sprint-review",
        instructions=("Deterministic sprint review metrics from Jira CSV exports. Quote the "
                      "numbers these tools return; never compute counts yourself."),
    )
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    @server.tool(annotations=read_only)
    def list_sprint_exports() -> list[str]:
        """List the Jira CSV exports available to analyse, as names to pass to analyze_sprint."""
        return list_exports(root)

    @server.tool(annotations=read_only)
    def analyze_sprint(csv_name: str, sprint_start: str | None = None,
                       exclude_unfinished_assignee_first_names: list[str] | None = None,
                       treat_as_in_progress_issue_keys: list[str] | None = None,
                       include_evidence: bool = True) -> dict:
        """Compute authoritative sprint review metrics from one Jira CSV export.

        csv_name is a name from list_sprint_exports. sprint_start (YYYY-MM-DD) enables the
        created/resolved-since-start counts. exclude_unfinished_assignee_first_names drops
        those people's unfinished cards but keeps their Done cards.
        treat_as_in_progress_issue_keys counts cards the user confirmed have moved into active
        work as In Progress. Returns counts by status, the Done-or-active percentage with its
        denominator (started_work), the source SHA-256, the filters applied and issue-level
        evidence. Read-only.
        """
        return run_analysis(root, csv_name, sprint_start, exclude_unfinished_assignee_first_names,
                            treat_as_in_progress_issue_keys, include_evidence)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                             idempotentHint=False, openWorldHint=False))
    def save_metrics(run_name: str, csv_name: str, sprint_start: str | None = None,
                     exclude_unfinished_assignee_first_names: list[str] | None = None,
                     treat_as_in_progress_issue_keys: list[str] | None = None) -> dict:
        """Analyse a Jira CSV export and save the result as runs/<run_name>/metrics.json.

        Takes the same filters as analyze_sprint. Writes a file, in a new run folder: it
        refuses a run_name that already exists, so a saved review is never overwritten.
        """
        analysis = run_analysis(root, csv_name, sprint_start, exclude_unfinished_assignee_first_names,
                                treat_as_in_progress_issue_keys)
        saved = save_run(root, run_name, analysis)
        return {"saved": saved, "sha256": analysis["source"]["sha256"], "totals": analysis["totals"]}

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path,
                        help="the only folder the server reads CSVs from and writes runs to")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error(f"--root {args.root} is not a folder")
    create_server(args.root).run()  # stdio


if __name__ == "__main__":
    main()
