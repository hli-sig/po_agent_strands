# FY budget converter — provenance

Not written here. This is Finance's `fy_data_prepare_tool` package, taken on
**2026-08-05** from:

    C:\Users\HL2\OneDrive - Sigma Company Limited\Documents\finance review\fy_data_prepare_tool

**This repo is now the home for this code.** Changes land here, in git, with
history. The original folders are deliberately left in place — deleting them is a
manual decision for their owner, not something this repo does.

## Why it was absorbed

Two copies existed and had silently diverged in three weeks:

| | `Downloads\FY27` | OneDrive package (this copy's source) |
|---|---|---|
| `convert_budget.py` | 540 lines | 613 lines |
| `to_snowflake_view_contract` | absent | present |
| `validate_raw_output_schema` | absent | present |
| Output | 18 columns | **24 columns** — matches `EDP_PROD_PRES_Rpt_Sales_Budget_VW.csv` |

Neither copy was under version control — no `.git` anywhere — so "which one is
current" was answerable only by comparing file timestamps. The stale copy was the
one wired into the Codex MCP config, and the `fy27_snowflake_ingest.csv` sitting
next to it is an 18-column file that does not satisfy the documented output
contract.

## What changed from Finance's copy

Exactly five import lines, so the flat modules work as a package:

```python
import convert_budget as cb                  ->  from . import convert_budget as cb
from detect import detect_roles_with_warnings ->  from .detect import detect_roles_with_warnings
from fy27_merged import ...                   ->  from .fy27_merged import ...
```

Nothing else was reformatted, renamed, or refactored. The move and those five
lines are a single commit on purpose: `git show <that commit>` is the complete
diff against Finance's copy, and `git log --follow` still reaches the original
files.

Dropped in the same commit, because this repo supersedes them:

| Dropped | Replaced by |
|---|---|
| `mcp_server.py` (Codex stdio server) | `pmagent/tools/finance_tools.py` — the tools the PM Agent's finance lane binds |
| `pyproject.toml`, `requirements*.txt` | the root `pyproject.toml` (`pandas`, `openpyxl` pins carried over unchanged) |
| `README.md`, `handover.md`, `VENDORED.md` | this file, `pmagent/skills/fy_budget/SKILL.md`, and `snowflake/fy_budget/` |

Moved, unchanged: `skills/fy-partition-rules/SKILL.md` →
`pmagent/skills/fy_budget/SKILL.md`; `snowflake/*` → `snowflake/fy_budget/`;
`validate_fy27_core_cw.py` → `scripts/`; `test_tool.py` →
`tests/test_fy_budget_conversion.py`.

## Re-syncing from Finance

If Finance ships a new version, diff before copying:

```bash
diff -r "<finance path>" pmagent/tools/fy_budget \
     --exclude fixtures --exclude .venv --exclude __pycache__ \
     --exclude PROVENANCE.md
```

Every hunk except the five import lines above is a real change. Run the
acceptance gate below before trusting the result.

## Non-negotiable guardrails (from Finance's handover)

1. Do not create values, map unknown source layouts, or fill missing allocation
   rates. Fail with an error that names the missing source evidence.
2. `fy_start` must match the first visible month in the source. FY27: `202607`;
   FY26 and earlier: `202502`.
3. Do not rename, reorder, add or remove output CSV columns. The published
   contract is the 24-column layout in
   `fixtures/EDP_PROD_PRES_Rpt_Sales_Budget_VW.csv`.
4. A successful result requires `internal_validation=passed` **and** an empty
   `validation_failures` list.
5. Preserve the metadata JSON beside the CSV; it is the run audit trail.

The FY27 allocation policy (Customer View uses the Core rate; Plant and
non-private MG1 sum Core and CW only *after* each component has had its own rate
applied) is business-confirmed. Do not replace it with a blend. It is disclosed
in the metadata and enforced by a test.

## Fixtures are NOT committed

`fixtures/` holds real budget data (`FY27 budget upload _ Daily Phasing.xlsx`,
and a 4.6 MB `EDP_PROD_PRES_Rpt_Sales_Budget_VW.csv` of actual rows). It is
gitignored, so `tests/test_fy_budget_conversion.py` skips itself on a fresh
clone. To run it, copy the fixtures to `pmagent/tools/fy_budget/fixtures/` or
point `FY_FIXTURES_DIR` at them:

```bash
FY_FIXTURES_DIR="<finance path>/fixtures" uv run pytest tests/test_fy_budget_conversion.py -q
```

Expected FY27 evidence: `total_rows=34310`, `internal_validation=passed`, no
validation failures. For a human-readable report, run
`scripts/validate_fy27_core_cw.py` and keep its JSON with the release evidence.
