"""Lesson 8's case study: the sprint review analyser and the MCP server around it.

The analyser tests are the handover package's `self-test.ps1` plus the rules its
DESIGN_REVIEW.md names (unknown statuses kept, Done cards kept, reconciliation,
provenance). The server tests are the two guards that review asked for: no path
outside the root and no overwrite. The last test runs the lesson's example end
to end: a real stdio MCP server, the real approval gate and the scripted model.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "docs" / "learning" / "examples"
sys.path.insert(0, str(EXAMPLES))

from sprint_review.analyser import CsvError, analyse, parse_jira_date  # noqa: E402
from sprint_review.server import (  # noqa: E402
    RefusedError,
    list_exports,
    resolve_csv,
    run_analysis,
    save_run,
)

SAMPLE = EXAMPLES / "sprint_review" / "sample-jira.csv"
HEADER = "Issue key,Summary,Status,Assignee,Created,Resolved\n"


def write_csv(folder: Path, rows: str, name: str = "sprint.csv", header: str = HEADER) -> Path:
    path = folder / name
    path.write_text(header + rows, encoding="utf-8")
    return path


# --- the analyser -----------------------------------------------------------

def test_the_handover_self_test_expectations_hold():
    r = analyse(SAMPLE, sprint_start=date(2026, 9, 1), exclude_unfinished_assignee_first_names=["Alex"])
    assert r["schema_version"] == "sprint-review-analysis.v1"
    assert (r["totals"]["exported"], r["totals"]["included"], r["totals"]["excluded"]) == (6, 5, 1)
    m = r["metrics"]
    assert m["done"] == 2                       # Alex's Done card is retained
    assert m["active_flow"] == 2 and m["blocked"] == 1
    assert m["started_work"] == 5 and m["done_or_active_pct_of_started"] == 80
    assert m["created_since_sprint_start"] == 4
    assert m["done_resolved_since_sprint_start"] == 2


def test_excluding_an_assignee_drops_only_their_unfinished_cards():
    r = analyse(SAMPLE, exclude_unfinished_assignee_first_names=["  alex "])
    assert [i["key"] for i in r["evidence"]["excluded_issues"]] == ["DEMO-5"]
    assert "DEMO-1" in [i["key"] for i in r["evidence"]["included_issues"]]
    assert r["evidence"]["excluded_issues"][0]["exclusion_reason"] == \
        "Unfinished item assigned to excluded first name 'Alex'"
    assert r["filters"]["excluded_unfinished_assignee_first_names"] == ["alex"]


def test_no_filters_includes_every_row_and_no_date_metrics():
    r = analyse(SAMPLE)
    assert r["totals"]["included"] == r["totals"]["exported"] == 6
    assert r["metrics"]["created_since_sprint_start"] is None
    assert r["metrics"]["done_resolved_since_sprint_start"] is None
    assert r["filters"]["sprint_start"] is None


def test_unknown_statuses_are_kept_counted_and_listed_after_the_standard_ones(tmp_path):
    csv = write_csv(tmp_path, "A-1,x,DONE,,,\nA-2,x,Ready for QA,,,\nA-3,x,ready for qa,,,\n"
                              "A-4,x,Awaiting vendor,,,\nA-5,x,  in review ,,,\n")
    counts = analyse(csv)["metrics"]["status_counts"]
    assert counts == {"Done": 1, "In review": 1, "Awaiting vendor": 1, "Ready for QA": 2}
    assert list(counts) == ["Done", "In review", "Awaiting vendor", "Ready for QA"]


def test_a_user_confirmed_move_counts_as_in_progress_but_keeps_its_source_status(tmp_path):
    csv = write_csv(tmp_path, "A-1,x,To Do,Sam Lee,,\nA-2,x,Done,,,\nA-3,x,To Do,Kim Park,,\n")
    r = analyse(csv, treat_as_in_progress_issue_keys=["a-1", "A-2"],
                exclude_unfinished_assignee_first_names=["Kim"])
    issues = {i["key"]: i for i in r["evidence"]["included_issues"]}
    assert issues["A-1"]["normalized_status"] == "In Progress"
    assert issues["A-1"]["source_normalized_status"] == "To Do"
    assert issues["A-1"]["status_adjustment"]
    assert issues["A-2"]["normalized_status"] == "Done"      # Done is never overridden
    assert r["metrics"]["in_progress"] == 1 and r["metrics"]["to_do"] == 0


def test_the_percentage_rounds_like_powershell(tmp_path):
    # 5 of 8 started = 62.5 → 62 (banker's rounding, as [math]::Round does)
    rows = "".join(f"A-{n},x,Done,,,\n" for n in range(5)) + "B-1,x,Blocked,,,\nB-2,x,Blocked,,,\nB-3,x,Blocked,,,\n"
    assert analyse(write_csv(tmp_path, rows))["metrics"]["done_or_active_pct_of_started"] == 62


def test_nothing_started_is_zero_percent_not_a_crash(tmp_path):
    r = analyse(write_csv(tmp_path, "A-1,x,To Do,,,\n"))
    assert r["metrics"]["started_work"] == 0 and r["metrics"]["done_or_active_pct_of_started"] == 0


def test_provenance_is_the_file_hash_and_the_rule():
    r = analyse(SAMPLE)
    assert r["source"]["sha256"] == hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    assert "retain their Done cards" in r["filters"]["rule"]


def test_a_bom_and_quoted_commas_and_optional_columns_are_handled(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_text('\ufeffIssue key,Summary,Status\nA-1,"Load feed, then map",Done\n', encoding="utf-8")
    r = analyse(path, sprint_start=date(2026, 9, 1))
    assert r["evidence"]["included_issues"][0]["summary"] == "Load feed, then map"
    assert r["metrics"]["done_resolved_since_sprint_start"] == 0   # no Resolved column


def test_a_csv_that_is_not_utf8_is_read_like_the_original(tmp_path):
    # Excel's "CSV (Comma delimited)" is cp1252; the original replaces bad bytes.
    path = tmp_path / "excel.csv"
    path.write_bytes(HEADER.encode() + "A-1,Café feed,Done,,,\n".encode("cp1252"))
    r = analyse(path)
    assert r["totals"]["included"] == 1
    assert r["evidence"]["included_issues"][0]["summary"].startswith("Caf")


def test_the_hash_is_of_the_bytes_that_were_analysed(tmp_path, monkeypatch):
    path = write_csv(tmp_path, "A-1,x,Done,,,\n")
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or real(self))
    r = analyse(path)
    assert len(reads) == 1
    assert r["source"]["sha256"] == hashlib.sha256(real(path)).hexdigest()


def test_an_excluded_card_is_not_rescued_by_an_override(tmp_path):
    csv = write_csv(tmp_path, "A-1,x,To Do,Kim Park,,\n")
    r = analyse(csv, exclude_unfinished_assignee_first_names=["Kim"],
                treat_as_in_progress_issue_keys=["A-1"])
    assert r["totals"]["excluded"] == 1 and r["metrics"]["in_progress"] == 0


def test_filters_keep_the_first_spelling_given(tmp_path):
    r = analyse(SAMPLE, exclude_unfinished_assignee_first_names=["Alex", "ALEX", " alex"])
    assert r["filters"]["excluded_unfinished_assignee_first_names"] == ["Alex"]


def test_unreadable_dates_are_listed_not_silently_dropped(tmp_path):
    csv = write_csv(tmp_path, "A-1,x,Done,,sometime,02/Sep/26 9:00 AM\n"
                              "A-2,x,Done,,02/Sep/26 9:00 AM,soon\n"
                              "A-3,x,To Do,,02/Sep/26 9:00 AM,whenever\n"   # not Done: Resolved unused
                              "A-4,x,To Do,,,\n")                             # blank is not unreadable
    m = analyse(csv, sprint_start=date(2026, 9, 1))["metrics"]
    assert m["unreadable_date_issue_keys"] == ["A-1", "A-2"]
    assert m["created_since_sprint_start"] == 2 and m["done_resolved_since_sprint_start"] == 1
    assert analyse(csv)["metrics"]["unreadable_date_issue_keys"] is None


@pytest.mark.parametrize("text,expected", [
    ("01/Sep/26 9:00 AM", (2026, 9, 1, 9, 0)),
    ("04/Sep/2026 3:00 PM", (2026, 9, 4, 15, 0)),
    ("2026-09-14T08:30:00+10:00", (2026, 9, 14, 8, 30)),
    ("14/09/2026", (2026, 9, 14, 0, 0)),
    ("14/Sep/26 09:00", (2026, 9, 14, 9, 0)),          # 24-hour Jira format
    ("14/Sep/26 9:00:15 AM", (2026, 9, 14, 9, 0)),
    ("14/09/26", (2026, 9, 14, 0, 0)),
    ("14/09/2026 8:30 AM", (2026, 9, 14, 8, 30)),
    ("14 Sep 2026", (2026, 9, 14, 0, 0)),
    ("04/09/2026", (2026, 9, 4, 0, 0)),                # ambiguous: day-first, as en-AU
    ("09/14/2026", (2026, 9, 14, 0, 0)),               # impossible day-first: en-US fallback
])
def test_jira_dates_parse_day_first(text, expected):
    d = parse_jira_date(text)
    assert (d.year, d.month, d.day, d.hour, d.minute) == expected


@pytest.mark.parametrize("text", ["", "   ", "not a date"])
def test_blank_or_unreadable_dates_are_none(text):
    assert parse_jira_date(text) is None


@pytest.mark.parametrize("header,rows,message", [
    ("Issue key,Summary\n", "A-1,x\n", r"missing required column\(s\): Status"),
    (HEADER, "", "No issue rows"),
    ("", "", "CSV is empty"),
])
def test_an_unusable_export_fails_loudly(tmp_path, header, rows, message):
    with pytest.raises(CsvError, match=message):
        analyse(write_csv(tmp_path, rows, header=header))


# --- the server's guards ------------------------------------------------------

@pytest.fixture
def root(tmp_path):
    folder = (tmp_path / "exports").resolve()   # the server resolves its root once
    folder.mkdir()
    (folder / "sprint35.csv").write_bytes(SAMPLE.read_bytes())
    return folder


@pytest.mark.parametrize("name", ["../secret.csv", "/etc/passwd", "sprint35.txt", "nope.csv"])
def test_the_server_refuses_anything_but_a_csv_under_its_root(root, name):
    (root.parent / "secret.csv").write_text(HEADER + "S-1,x,Done,,,\n")
    (root / "sprint35.txt").write_text("x")
    with pytest.raises(RefusedError):
        resolve_csv(root, name)


@pytest.mark.parametrize("name", ["bad\x00name.csv", "x" * 5000 + ".csv"])
def test_an_unusable_name_is_a_refusal_not_a_crash(root, name):
    with pytest.raises(RefusedError, match="not a usable file name"):
        resolve_csv(root, name)


def test_exports_list_exactly_what_the_server_accepts(root):
    (root / "UPPER.CSV").write_bytes(SAMPLE.read_bytes())
    (root / "notes.txt").write_text("x")
    (root / "sub").mkdir()
    (root / "sub" / "s36.csv").write_bytes(SAMPLE.read_bytes())
    assert list_exports(root) == ["UPPER.CSV", "sprint35.csv", "sub/s36.csv"]
    assert all(resolve_csv(root, name) for name in list_exports(root))


def test_a_symlink_out_of_the_root_is_refused(root):
    outside = root.parent / "outside.csv"
    outside.write_text(HEADER + "S-1,x,Done,,,\n")
    (root / "link.csv").symlink_to(outside)
    with pytest.raises(RefusedError, match="outside"):
        resolve_csv(root, "link.csv")
    assert "link.csv" not in list_exports(root)


def test_results_cite_the_export_by_name_not_by_host_path(root):
    r = run_analysis(root, "sprint35.csv", include_evidence=False)
    assert r["source"]["csv_path"] == "sprint35.csv"
    assert "evidence" not in r


def test_bad_input_becomes_a_refusal_the_model_can_read(root):
    with pytest.raises(RefusedError, match="YYYY-MM-DD"):
        run_analysis(root, "sprint35.csv", sprint_start="14/09/2026")
    (root / "bad.csv").write_text("Issue key,Summary\nA-1,x\n")
    with pytest.raises(RefusedError, match="missing required"):
        run_analysis(root, "bad.csv")


def test_an_oversized_field_or_unreadable_file_is_a_refusal(root):
    (root / "huge.csv").write_text(HEADER + "A-1," + "x" * 200_000 + ",Done,,,\n")
    with pytest.raises(RefusedError, match="can't be analysed"):
        run_analysis(root, "huge.csv")
    locked = root / "locked.csv"
    locked.write_text(HEADER + "A-1,x,Done,,,\n")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as root: chmod can't make a file unreadable")
        with pytest.raises(RefusedError, match="can't be read"):
            run_analysis(root, "locked.csv")
    finally:
        locked.chmod(0o644)


def test_a_saved_run_is_never_overwritten(root):
    first = run_analysis(root, "sprint35.csv")
    assert save_run(root, "sprint-35", first) == "runs/sprint-35/metrics.json"
    before = (root / "runs/sprint-35/metrics.json").read_bytes()
    with pytest.raises(RefusedError, match="already exists"):
        save_run(root, "sprint-35", {"tampered": True})
    assert (root / "runs/sprint-35/metrics.json").read_bytes() == before
    assert list_exports(root) == ["sprint35.csv"]      # saved runs aren't offered as exports


def test_a_runs_symlink_cannot_carry_a_save_outside_the_root(root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "runs").symlink_to(elsewhere)
    with pytest.raises(RefusedError, match="real folder"):
        save_run(root, "x", {})
    assert list(elsewhere.iterdir()) == []


def test_a_runs_file_is_a_refusal_not_a_crash(root):
    (root / "runs").write_text("not a folder")
    with pytest.raises(RefusedError, match="real folder"):
        save_run(root, "x", {})


@pytest.mark.parametrize("name", ["", "..", "../x", "a/b", ".hidden", "x" * 65])
def test_a_run_name_cannot_escape_the_runs_folder(root, name):
    with pytest.raises(RefusedError, match="run_name"):
        save_run(root, name, {})


# --- end to end ---------------------------------------------------------------

def test_the_lesson_example_runs_offline_through_the_real_gate():
    run = subprocess.run([sys.executable, str(EXAMPLES / "08_sprint_review_mcp.py")],
                         capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    out = run.stdout
    # the server marks two tools read-only, and the gate still pauses on all three
    assert "analyze_sprint       readOnlyHint=True   gated=True" in out
    assert out.count("PAUSED before:") == 3
    # the server's refusal reached the model as an error result with its reason
    assert "[error]" in out and "already exists" in out
    assert "files the server wrote: ['runs/sprint-35/metrics.json']" in out
    assert "2 Done, 2 in active flow, 1 Blocked. 80% of started work (5 cards)" in out
    assert "Traceback" not in run.stderr
