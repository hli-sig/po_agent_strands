"""Unit tests for the FY budget lane's agent-facing layer.

No network, no credentials, no real budget data — these exercise the half of
`finance_tools.py` that decides what the agent is *told*. The conversion itself
is Finance's package and is covered by `test_fy_budget_conversion.py`, which
needs the real fixtures and skips without them.

The renderers get the most attention on purpose. A tool's returned string is the
entire view a lane has of what happened, so "did a failed run read as a failure?"
is a correctness question, not a formatting one.
"""

from pathlib import Path

import pytest

from pmagent.tools import finance_tools as ft


# A clean legacy run, shaped exactly like pipeline.convert_fy_budget's return.
PASSING_RESULT = {
    "steps": [
        "Identified files -> core: core.xlsx, custplant: customer_and_plant.xlsx",
        "No daily-rate file supplied -> each month is split evenly across its days.",
        "Wrote 34310 rows to fy_budget_202607.csv (even split (no daily file)).",
    ],
    "summary": {
        "fiscal_year_start": 202607,
        "total_rows": 34310,
        "rows_by_partition": {"RQF | Banner State": 730, "RQF INCLD CWH | MG1": 4015},
        "daily_distribution": "embedded Core/CW daily allocations",
        "internal_validation": "passed",
    },
    "warnings": [],
    "validation_failures": [],
    "out_path": "/out/fy_budget_202607.csv",
    "metadata_path": "/out/fy_budget_202607.metadata.json",
}


def failing_result(count: int = 2) -> dict:
    result = {**PASSING_RESULT, "validation_failures": [
        f"Banner AMC 20260{i % 9 + 1}: Budget 1.00 != 2.00" for i in range(count)
    ]}
    result["summary"] = {**PASSING_RESULT["summary"],
                         "internal_validation": f"{count} failures"}
    return result


# --------------------------------------------------------------------------
# The fiscal calendar
# --------------------------------------------------------------------------


def test_fy27_expands_to_july_through_june():
    assert ft.fy_months(202607) == [
        202607, 202608, 202609, 202610, 202611, 202612,
        202701, 202702, 202703, 202704, 202705, 202706,
    ]


def test_legacy_fy26_expands_to_february_through_january():
    assert ft.fy_months(202502)[0] == 202502
    assert ft.fy_months(202502)[-1] == 202601


def test_fy_start_is_rendered_with_its_month_range():
    # This string is what the approval prompt shows. "202607" alone is not
    # something a human can check; "Jul 2026 - Jun 2027" is.
    assert ft.format_fy_start(202607) == "202607 (Jul 2026 - Jun 2027)"
    assert ft.format_fy_start(202502) == "202502 (Feb 2025 - Jan 2026)"


@pytest.mark.parametrize("value", [202607, 202502, 210012])
def test_plausible_fiscal_starts_are_accepted(value):
    assert ft.is_valid_fy_start(value)


@pytest.mark.parametrize("value", [2026, 20260701, 202613, 202600, "july", None, ""])
def test_a_year_or_a_date_is_not_a_fiscal_start(value):
    # The two realistic mistakes are a bare year and a full date; both would
    # otherwise reach the converter and fail deep inside it.
    assert not ft.is_valid_fy_start(value)


def test_a_bad_fiscal_start_names_both_conventions():
    with pytest.raises(ft.FyBudgetError) as excinfo:
        ft.check_fy_start(2026)
    message = str(excinfo.value)
    assert "202607" in message and "202502" in message
    assert "never infer" in message


def test_an_unrenderable_fiscal_start_does_not_crash_the_renderer():
    # The approval prompt must still print something for a bad value.
    assert ft.format_fy_start(2026) == "2026"


# --------------------------------------------------------------------------
# The run report — the verdict has to survive being skimmed
# --------------------------------------------------------------------------


def test_a_clean_run_reports_passed_with_its_paths():
    report = ft.render_run_report(PASSING_RESULT)
    assert report.splitlines()[0] == "FY budget conversion passed validation."
    assert "/out/fy_budget_202607.csv" in report
    assert "/out/fy_budget_202607.metadata.json" in report
    assert "202607 (Jul 2026 - Jun 2027)" in report
    assert "34310" in report
    assert "RQF | Banner State: 730" in report


def test_a_failed_run_leads_with_the_failure_and_says_the_csv_is_unusable():
    # The converter writes the CSV *before* validating it, so a failed run
    # leaves a real file on disk. Summarising that as success is the single
    # worst outcome this lane can produce.
    report = ft.render_run_report(failing_result(3))
    first = report.splitlines()[0]
    assert "FAILED" in first and "3 failure(s)" in first
    assert "must NOT be used" in report
    assert "passed validation" not in report


def test_every_failure_is_listed_when_the_list_is_short():
    report = ft.render_run_report(failing_result(3))
    assert report.count("    - Banner AMC") == 3


def test_a_long_failure_list_is_clipped_and_says_so():
    # Every cap needs a voice: a silently clipped failure list reads as a
    # shorter problem than it is.
    report = ft.render_run_report(failing_result(50))
    assert "Validation failures (50):" in report
    assert f"showing {ft._MAX_FAILURES_SHOWN} of 50" in report
    assert report.count("    - Banner AMC") == ft._MAX_FAILURES_SHOWN


def test_warnings_are_surfaced_even_on_a_passing_run():
    # "Multiple files matched role 'core'" means a file was ignored. The user
    # has to see that regardless of the verdict.
    result = {**PASSING_RESULT,
              "warnings": ["Multiple files matched role 'core'; using a.xlsx, ignoring b.xlsx."]}
    report = ft.render_run_report(result)
    assert "passed validation" in report
    assert "ignoring b.xlsx" in report


def test_the_steps_the_converter_took_are_shown():
    report = ft.render_run_report(PASSING_RESULT)
    assert "split evenly across its days" in report


# --------------------------------------------------------------------------
# The audit sidecar
# --------------------------------------------------------------------------


CLEAN_METADATA = {
    "generated_at_utc": "2026-08-06T01:00:00+00:00",
    "output_csv": "/out/fy_budget_202607.csv",
    "inputs": {"core": "/in/merged.xlsx", "custplant": "/in/merged.xlsx", "dailyrate": None},
    "fiscal_year_start": 202607,
    "total_rows": 34310,
    "rows_by_partition": {"RQF | Banner State": 730},
    "daily_distribution": "embedded Core/CW daily allocations",
    "schema_validation": "passed",
    "internal_validation": "passed",
    "validation_failures": [],
}


def test_clean_metadata_reads_as_passed():
    report = ft.render_metadata(CLEAN_METADATA, Path("/out/x.metadata.json"))
    assert "PASSED" in report.splitlines()[0]
    assert "/in/merged.xlsx" in report
    assert "202607 (Jul 2026 - Jun 2027)" in report


def test_metadata_with_failures_is_not_called_a_clean_run():
    metadata = {**CLEAN_METADATA,
                "internal_validation": "2 failures",
                "validation_failures": ["Banner AMC 202607: Budget mismatch", "CG X"]}
    report = ft.render_metadata(metadata, Path("/out/x.metadata.json"))
    assert "NOT a clean run" in report.splitlines()[0]
    assert "Banner AMC 202607" in report


def test_metadata_omits_the_inputs_that_were_not_supplied():
    report = ft.render_metadata(CLEAN_METADATA, Path("/out/x.metadata.json"))
    assert "dailyrate" not in report


def test_missing_metadata_explains_the_pairing_rule(tmp_path):
    answer = ft.read_fy_budget_run(**{"csv_path": str(tmp_path / "nothing.csv")})
    assert "No audit metadata" in answer
    assert "kept together" in answer


def test_metadata_is_read_from_the_sidecar_beside_a_csv(tmp_path):
    import json

    (tmp_path / "run.metadata.json").write_text(json.dumps(CLEAN_METADATA), encoding="utf-8")
    answer = ft.read_fy_budget_run(**{"csv_path": str(tmp_path / "run.csv")})
    assert "PASSED" in answer


# --------------------------------------------------------------------------
# Input selection
# --------------------------------------------------------------------------


def test_excel_lock_files_and_unrelated_files_are_ignored(tmp_path):
    for name in ("budget.xlsx", "~$budget.xlsx", "rates.csv", "notes.docx", "old.pbix"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    found = [path.name for path in ft.collect_inputs(tmp_path)]
    assert found == ["budget.xlsx", "rates.csv"]


def test_a_missing_input_folder_says_which_folder(tmp_path):
    missing = tmp_path / "not-there"
    with pytest.raises(ft.FyBudgetError) as excinfo:
        ft.collect_inputs(missing)
    assert str(missing) in str(excinfo.value)


def test_an_empty_input_folder_is_a_refusal_not_an_empty_run(tmp_path):
    with pytest.raises(ft.FyBudgetError):
        ft.collect_inputs(tmp_path)


def test_inspecting_a_bad_folder_answers_instead_of_raising(tmp_path):
    # Inside a lane an exception is a crashed tool call the agent can't respond
    # to. A read tool should hand back the problem as its result.
    answer = ft.inspect_fy_budget_inputs(**{"input_dir": str(tmp_path / "nope")})
    assert "Could not read FY budget inputs" in answer


# --------------------------------------------------------------------------
# The input report
# --------------------------------------------------------------------------


def test_the_input_report_names_the_role_of_every_file(tmp_path):
    core, custplant, stray = (tmp_path / n for n in ("c.xlsx", "cp.xlsx", "notes.csv"))
    report = ft.render_input_report(
        tmp_path,
        [core, custplant, stray],
        {"core": core, "custplant": custplant},
        [],
        tmp_path / "out.csv",
    )
    assert "Total by month" in report          # the core file's role
    assert "Plant_View" in report              # the custplant file's role
    assert "not used" in report                # and the one that matched nothing


def test_a_legacy_report_refuses_to_guess_the_fiscal_start(tmp_path):
    report = ft.render_input_report(
        tmp_path, [tmp_path / "c.xlsx"], {"core": tmp_path / "c.xlsx"}, [], None
    )
    assert "NOT inferable" in report
    assert "202502" in report
    assert "split evenly" in report            # no daily-rate file was supplied


def test_an_fy27_report_states_the_only_valid_fiscal_start(tmp_path):
    merged = tmp_path / "fy27.xlsx"
    report = ft.render_input_report(tmp_path, [merged], {"fy27_merged": merged}, [], None)
    assert "202607" in report
    assert "aborts on a mismatch" in report


def test_an_existing_output_file_is_flagged_before_anything_is_written(tmp_path):
    existing = tmp_path / "out.csv"
    existing.write_text("old", encoding="utf-8")
    report = ft.render_input_report(tmp_path, [tmp_path / "c.xlsx"], {}, [], existing)
    assert "already exists" in report
    assert "overwrite=true" in report


def test_duplicate_role_warnings_reach_the_input_report(tmp_path):
    report = ft.render_input_report(
        tmp_path, [tmp_path / "a.xlsx"], {}, ["Multiple files matched role 'core'"], None
    )
    assert "Multiple files matched" in report


# --------------------------------------------------------------------------
# The write tool's own guards
# --------------------------------------------------------------------------


def test_converting_refuses_to_replace_an_existing_csv(tmp_path):
    existing = tmp_path / "fy_budget_202607.csv"
    existing.write_text("real,data", encoding="utf-8")
    (tmp_path / "book.xlsx").write_text("x", encoding="utf-8")

    answer = ft.create_fy_budget_csv(**{
        "fy_start": 202607,
        "input_dir": str(tmp_path),
        "out_path": str(existing),
    })

    assert answer.startswith("Refused")
    assert "overwrite=true" in answer
    assert existing.read_text(encoding="utf-8") == "real,data"


def test_a_bad_fiscal_start_is_rejected_before_any_file_is_touched(tmp_path):
    (tmp_path / "book.xlsx").write_text("x", encoding="utf-8")
    answer = ft.create_fy_budget_csv(**{
        "fy_start": 2026,
        "input_dir": str(tmp_path),
        "out_path": str(tmp_path / "out.csv"),
    })
    assert "FAILED" in answer
    assert not (tmp_path / "out.csv").exists()


def test_a_converter_failure_is_reported_not_worked_around(tmp_path):
    # An .xlsx that isn't a workbook: detection raises, and the tool must return
    # that as a failure the agent can relay rather than a traceback.
    (tmp_path / "book.xlsx").write_text("not really a workbook", encoding="utf-8")
    answer = ft.create_fy_budget_csv(**{
        "fy_start": 202607,
        "input_dir": str(tmp_path),
        "out_path": str(tmp_path / "out.csv"),
    })
    assert answer.startswith("FY budget conversion FAILED")
    assert "Do not work around this" in answer


def test_the_default_output_path_is_named_after_the_fiscal_start(monkeypatch):
    # Not "FY27": FY26 began in February and FY27 in July, so a fiscal-year
    # label is a business convention this code has no business inventing.
    monkeypatch.setattr(ft.env, "FY_BUDGET_OUTPUT_DIR", "/tmp/out")
    assert ft.default_out_path(202607).name == "fy_budget_202607.csv"


# --- the report on a real pipeline run (synthetic workbooks, tests/fy_synthetic.py) ---

def test_a_real_run_is_reported_with_its_fiscal_start(tmp_path):
    from pmagent.tools.fy_budget.pipeline import convert_fy_budget
    from tests import fy_synthetic as fx

    result = convert_fy_budget([fx.write_fy27(tmp_path)], fx.FY27_START, tmp_path / "ok.csv")
    report = ft.render_run_report(result)
    assert report.splitlines()[0] == "FY budget conversion passed validation."
    assert "202607 (Jul 2026 - Jun 2027)" in report


def test_a_real_run_that_does_not_tie_back_is_reported_as_failed(tmp_path, monkeypatch):
    from pmagent.tools.fy_budget import convert_budget as cb
    from pmagent.tools.fy_budget.pipeline import convert_fy_budget
    from tests import fy_synthetic as fx

    real = cb._expand_to_days
    monkeypatch.setattr(cb, "_expand_to_days", lambda *a, **k: real(*a, **k).iloc[:-1])
    core, custplant = fx.write_legacy(tmp_path)
    result = convert_fy_budget([core, custplant], fx.LEGACY_START, tmp_path / "bad.csv")
    assert ft.render_run_report(result).startswith("FY budget conversion FAILED")
