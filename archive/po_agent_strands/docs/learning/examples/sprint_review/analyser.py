"""The deterministic half of the sprint review: a Jira CSV in, `metrics.json` out.

A Python port of the handover package's `analyze-sprint.ps1` (schema
`sprint-review-analysis.v1`, the same rules, the same field names). It is plain
Python with no Strands and no MCP, for the reason `pmagent/tools/jira/metrics.py`
is: **the model does judgment, code does arithmetic.** A sprint review's headline
numbers drive a stakeholder conversation, so they come from here and never from a
model's memory of the CSV.

The rules it keeps from the PowerShell original:

* Required columns: `Issue key`, `Summary`, `Status`. A UTF-8 BOM is ignored.
* The six standard statuses are matched case-insensitively and shown with their
  standard spelling. **Any other status is kept and counted under its own name**,
  never silently mapped to a known one.
* The scope rule: an assignee whose first name is excluded loses their
  *unfinished* cards only. Their `Done` cards stay in scope.
* A user-confirmed key can be counted as `In Progress` (for example, a card that
  moved after the export). The source status is kept, and the adjustment is
  recorded on the issue.
* `done_or_active_pct_of_started` = (Done + In review + In Progress) / (those +
  Blocked). The denominator is `started_work`, and it is in the output so a slide
  can cite it.
* Status counts must sum to the included rows, or the analysis fails loudly.
  (Kept from the original as a belt-and-braces check: the counts are built from
  the included rows, so today it cannot fire.)
* The source file's SHA-256 is in the output. The file is read once, and the
  same bytes are hashed and parsed, so the hash ties every figure to one export.

Intended differences from the PowerShell original (none changes a count):

* Dates are parsed with explicit day-first formats, then month-first (the
  original's en-AU, then en-US order) and ISO 8601, instead of .NET's lenient
  `TryParse`. An ISO offset is dropped, where .NET converts to local time.
  `sprint_start` is a date, not a date and time.
* **Added:** `metrics.unreadable_date_issue_keys` lists included issues whose
  non-blank date couldn't be read, so a date-window count never drops a row
  silently. It is `None` without a sprint start, like the date metrics.
* Bytes that aren't valid UTF-8 are replaced, as the original's parser does,
  instead of failing (Excel's "CSV (Comma delimited)" is not UTF-8).
* `generated_at_utc` is Python's ISO format (`+00:00`), and unknown statuses
  sort by `casefold`, not .NET's culture-aware order.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "sprint-review-analysis.v1"
REQUIRED_COLUMNS = ("Issue key", "Summary", "Status")
_WANTED = ("Issue key", "Summary", "Status", "Assignee", "Created", "Resolved")
STANDARD_STATUSES = ("Done", "In review", "In Progress", "Blocked", "To Do", "Duplicate")
_BY_LOWER = {s.lower(): s for s in STANDARD_STATUSES}
SCOPE_RULE = "Exclude matching assignees only when status is not Done; retain their Done cards."

# Jira's CSV export writes "01/Sep/26 9:00 AM"; sites configure others. The
# original tried en-AU (day-first), then en-US (month-first) culture parsing.
_DAYS = ("%d/%b/%y", "%d/%b/%Y", "%d/%m/%y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y")
_MONTH_FIRST = ("%m/%d/%y", "%m/%d/%Y")
_TIMES = ("", " %I:%M %p", " %I:%M:%S %p", " %H:%M", " %H:%M:%S")
JIRA_DATE_FORMATS = tuple(d + t for d in _DAYS + _MONTH_FIRST for t in _TIMES)


class CsvError(ValueError):
    """The export can't be analysed as asked (missing columns, no rows, ...)."""


def normalize_status(status: str) -> str:
    """A standard status in its standard spelling; anything else trimmed, as-is."""
    trimmed = status.strip()
    return _BY_LOWER.get(trimmed.lower(), trimmed)


def parse_jira_date(text: str) -> datetime | None:
    """A Jira export date, or None when blank or unreadable. An unreadable row is
    not counted in the date-window metrics; `analyse` lists it instead."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in JIRA_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        # ISO 8601. An offset is dropped: the sprint window is a calendar date.
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def read_jira_csv(data: bytes, name: str) -> list[dict[str, str]]:
    """One dict per issue row, holding only the columns the analysis uses."""
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"), newline=""))
    headers = next(reader, None)
    if headers is None:
        raise CsvError(f"CSV is empty: {name}")
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise CsvError(f"CSV is missing required column(s): {', '.join(missing)}. "
                       f"Found: {', '.join(headers)}")
    index = {col: headers.index(col) if col in headers else -1 for col in _WANTED}
    # A blank line has no fields; the original's parser skips it too.
    return [{col: fields[i] if 0 <= i < len(fields) else "" for col, i in index.items()}
            for fields in reader if fields]


def _clean_set(values) -> dict[str, str]:
    """Case-insensitive set that keeps the first spelling it was given."""
    out: dict[str, str] = {}
    for v in values or ():
        if v and v.strip():
            out.setdefault(v.strip().casefold(), v.strip())
    return out


def analyse(
    csv_path: str | Path,
    *,
    sprint_start: date | None = None,
    exclude_unfinished_assignee_first_names=(),
    treat_as_in_progress_issue_keys=(),
) -> dict:
    """Analyse one Jira CSV export. Returns the `sprint-review-analysis.v1` dict.

    Raises `CsvError` for an unusable export, or when the status counts don't
    reconcile to the included rows.
    """
    path = Path(csv_path).resolve()
    data = path.read_bytes()  # read once: these bytes are hashed *and* parsed
    rows = read_jira_csv(data, path.name)
    if not rows:
        raise CsvError(f"No issue rows found in {path.name}")

    excluded_names = _clean_set(exclude_unfinished_assignee_first_names)
    overrides = _clean_set(treat_as_in_progress_issue_keys)

    issues = []
    for row in rows:
        assignee = row["Assignee"]
        first_name = assignee.strip().split()[0] if assignee.strip() else ""
        key = row["Issue key"].strip()
        source_status = normalize_status(row["Status"])
        excluded = source_status != "Done" and first_name.casefold() in excluded_names
        override = not excluded and source_status != "Done" and key.casefold() in overrides
        issues.append({
            "key": key,
            "summary": row["Summary"],
            "status": row["Status"],
            "source_normalized_status": source_status,
            "normalized_status": "In Progress" if override else source_status,
            "assignee": assignee,
            "created": row["Created"],
            "resolved": row["Resolved"],
            "excluded": excluded,
            "exclusion_reason": (f"Unfinished item assigned to excluded first name '{first_name}'"
                                 if excluded else ""),
            "status_adjustment": "User-confirmed as In Progress for this review." if override else "",
        })

    included = [i for i in issues if not i["excluded"]]
    excluded_rows = [i for i in issues if i["excluded"]]

    count = Counter(i["normalized_status"] for i in included)
    # Standard statuses first, in the standard order; then every other status,
    # grouped and sorted case-insensitively (PowerShell's Group-Object/Sort-Object),
    # under the first spelling seen.
    status_counts = {s: count[s] for s in STANDARD_STATUSES if count[s]}
    others: dict[str, list] = {}
    for name, n in count.items():
        if name not in STANDARD_STATUSES:
            others.setdefault(name.casefold(), [name, 0])[1] += n
    status_counts |= {name: n for _, (name, n) in sorted(others.items())}

    done, in_review, in_progress = count["Done"], count["In review"], count["In Progress"]
    blocked, to_do, duplicate = count["Blocked"], count["To Do"], count["Duplicate"]
    active = in_review + in_progress
    started = done + active + blocked
    # round() is banker's rounding, the same as PowerShell's [math]::Round.
    pct = round(100 * (done + active) / started) if started else 0

    created_since = done_resolved_since = unreadable = None
    if sprint_start is not None:
        start = datetime.combine(sprint_start, datetime.min.time())
        created_since = done_resolved_since = 0
        unreadable = []
        for issue in included:
            is_done = issue["normalized_status"] == "Done"
            created = parse_jira_date(issue["created"])
            resolved = parse_jira_date(issue["resolved"]) if is_done else None
            if (created is None and issue["created"].strip()) or \
                    (is_done and resolved is None and issue["resolved"].strip()):
                unreadable.append(issue["key"])
            created_since += created is not None and created >= start
            done_resolved_since += resolved is not None and resolved >= start

    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "csv_path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "filters": {
            "sprint_start": sprint_start.isoformat() if sprint_start else None,
            "excluded_unfinished_assignee_first_names": sorted(excluded_names.values(), key=str.casefold),
            "user_confirmed_in_progress_issue_keys": sorted(overrides.values(), key=str.casefold),
            "rule": SCOPE_RULE,
        },
        "totals": {
            "exported": len(issues),
            "included": len(included),
            "excluded": len(excluded_rows),
            "status_sum": sum(status_counts.values()),
        },
        "metrics": {
            "status_counts": status_counts,
            "done": done,
            "active_flow": active,
            "in_review": in_review,
            "in_progress": in_progress,
            "blocked": blocked,
            "to_do": to_do,
            "duplicate": duplicate,
            "started_work": started,
            "done_or_active_pct_of_started": pct,
            "created_since_sprint_start": created_since,
            "done_resolved_since_sprint_start": done_resolved_since,
            "unreadable_date_issue_keys": unreadable,
        },
        "evidence": {"included_issues": included, "excluded_issues": excluded_rows},
    }
    totals = result["totals"]
    if totals["status_sum"] != totals["included"]:
        raise CsvError(f"Status reconciliation failed: {totals['status_sum']} statuses "
                       f"for {totals['included']} included rows.")
    return result
