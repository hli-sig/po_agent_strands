"""
One unit of work: take a set of input file paths + a fiscal-year start, produce the
daily CSV, and return a human-readable summary. Used by the MCP server and the tests.
"""
from __future__ import annotations
import logging
from pathlib import Path

from . import convert_budget as cb
from .detect import detect_roles_with_warnings
from .fy27_merged import run as run_fy27_merged

logger = logging.getLogger(__name__)


def convert_fy_budget(file_paths: list[str | Path], fy_start: int, out_path: str | Path) -> dict:
    """
    file_paths : the uploaded files (order-independent; roles are detected by content).
    fy_start   : YYYYMM of the first fiscal month (e.g. 202502 for legacy FY26,
                 202607 for the FY27 merged workbook).
    out_path   : where to write the CSV.

    Returns {steps, summary, warnings, validation_failures, out_path, metadata_path}.
    """
    paths = [Path(p) for p in file_paths]
    out_path = Path(out_path)

    steps = []
    roles, warnings = detect_roles_with_warnings(paths)
    logger.info("Detected input roles: %s", {r: p.name for r, p in roles.items()})
    steps.append(f"Identified files -> " + ", ".join(f"{r}: {p.name}" for r, p in roles.items()))
    for warning in warnings:
        steps.append(f"Warning -> {warning}")
    if 'fy27_merged' in roles:
        result = run_fy27_merged(roles['fy27_merged'], fy_start, out_path)
        steps.append("Read the FY27 merged workbook, including its embedded Core/CW daily allocations.")
    else:
        if 'dailyrate' not in roles:
            steps.append("No daily-rate file supplied -> each month is split evenly across its days.")
        result = cb.run(
            core_path=roles['core'],
            custplant_path=roles['custplant'],
            fy_start=fy_start,
            out_path=out_path,
            dailyrate_path=roles.get('dailyrate'),
        )
    steps.append(f"Wrote {result['rows']} rows to {out_path.name} ({result['daily_rate']}).")
    steps.append(f"Wrote metadata to {Path(result['metadata_path']).name}.")

    summary = {
        'fiscal_year_start': fy_start,
        'fiscal_months': cb.fy_months(fy_start),
        'total_rows': result['rows'],
        'rows_by_partition': result['partitions'],
        'daily_distribution': result['daily_rate'],
        'warnings': warnings,
        'metadata_path': result['metadata_path'],
        'internal_validation': 'passed'
        if not result['validation_failures'] else f"{len(result['validation_failures'])} failures",
    }
    return {
        'steps': steps,
        'summary': summary,
        'warnings': warnings,
        'validation_failures': result['validation_failures'],
        'out_path': str(out_path),
        'metadata_path': result['metadata_path'],
    }
