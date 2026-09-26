"""
Regression tests for the FY budget converter — Finance's own suite, moved here
with the package (see pmagent/tools/fy_budget/PROVENANCE.md).

The headline test reproduces the FY26 output from the real fixtures and checks that
every (partition, entity, month) Budget/GP rollup matches the supplied validation
file. They match to within a few cents — the only difference is 2-decimal daily
rounding (both files round daily values independently), so the tolerance is set just
above that rounding band. A genuine mis-mapping would be off by thousands.

**This module skips itself unless the fixtures are present.** They are real budget
rows and are gitignored, so a fresh clone has none — and `uv run pytest tests -q`
must stay green with no network, no credentials and no financial data. Put them in
`pmagent/tools/fy_budget/fixtures/`, or point `FY_FIXTURES_DIR` at Finance's copy:

    FY_FIXTURES_DIR="<finance path>/fixtures" uv run pytest tests/test_fy_budget_conversion.py -q

Everything that does *not* need real data — the renderers, the fiscal calendar,
role detection, the overwrite guard — is in `tests/test_finance_tools.py` and
always runs.
"""
from __future__ import annotations
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from pmagent import env
from pmagent.tools.fy_budget import convert_budget as cb
from pmagent.tools.fy_budget.detect import detect_roles, detect_roles_with_warnings
from pmagent.tools.fy_budget.pipeline import convert_fy_budget
from pmagent.tools.fy_budget.fy27_merged import (
    _read_customer,
    _read_daily,
    _sheet_map,
    is_fy27_merged_workbook,
)

FX = Path(env.FY_FIXTURES_DIR).expanduser() if env.FY_FIXTURES_DIR else (
    Path(cb.__file__).parent / "fixtures"
)
CORE = FX / "core.xlsx"
CUSTPLANT = FX / "customer_and_plant.xlsx"
VALIDATION = FX / "validation_file.xlsx"
FY26_START = 202502                      # FY26 ran Feb 2025 - Jan 2026
FY27_MERGED = FX / 'FY27 budget  upload _ Daily Phasing.xlsx'
FY27_START = 202607

# Per (entity, month) tolerance. Daily 2dp rounding drifts at most
# ~ days_in_month * 0.005 ≈ $0.16; $1.00 sits safely above that and still
# catches real mapping errors (which are off by thousands+).
ROLLUP_TOL = 1.00

ROLLUP_KEYS = ['Type', 'Partition', 'MG1Code', 'CustomerGroupCode',
               'PrivateLabelFlag', 'BannerGroupCode', 'Plant', 'ym']

# Collection-time skip: without the real workbooks none of this can run, and a
# fresh clone has none of them. Naming the missing folder beats an ImportError.
pytestmark = pytest.mark.skipif(
    not CORE.exists(),
    reason=f"FY budget fixtures not found in {FX} — set FY_FIXTURES_DIR to run these",
)


def _rollup(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Generated CSVs use the supplied Snowflake-view column names; the legacy
    # validation fixture uses the original internal names.  Compare both on
    # the shared business dimensions.
    df = df.rename(columns={
        'Cal Year Mth Day': 'CalYearMthDay',
        'MG1 Code': 'MG1Code',
        'Customer Group Code': 'CustomerGroupCode',
        'Private Label Flag': 'PrivateLabelFlag',
        'Banner Group Code': 'BannerGroupCode',
        'Plant Code': 'Plant',
    })
    df['ym'] = df['CalYearMthDay'].astype(int) // 100
    for c in ['Type', 'Partition', 'MG1Code', 'CustomerGroupCode',
              'PrivateLabelFlag', 'BannerGroupCode']:
        df[c] = df[c].astype('object').where(df[c].notna(), '').astype(str).str.strip()
    df['Plant'] = pd.to_numeric(df['Plant'], errors='coerce').fillna(0).astype(int)
    df['Budget'] = pd.to_numeric(df['Budget'])
    df['GP'] = pd.to_numeric(df['GP'])
    return df.groupby(ROLLUP_KEYS)[['Budget', 'GP']].sum().round(2)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "fy26.csv"
    res = convert_fy_budget([CORE, CUSTPLANT], FY26_START, out)
    return pd.read_csv(out, low_memory=False), res


def test_detection_maps_files():
    roles = detect_roles([CUSTPLANT, CORE])          # order shouldn't matter
    assert roles['core'].name == "core.xlsx"
    assert roles['custplant'].name == "customer_and_plant.xlsx"
    assert 'dailyrate' not in roles                  # none supplied


def test_duplicate_role_warning(tmp_path):
    core_copy = tmp_path / "core_duplicate.xlsx"
    shutil.copyfile(CORE, core_copy)
    roles, warnings = detect_roles_with_warnings([CORE, core_copy, CUSTPLANT])
    assert roles['core'].name == "core.xlsx"
    assert any("Multiple files matched role 'core'" in w for w in warnings)


def test_row_count_and_partitions(generated):
    df, res = generated
    assert len(df) == 36865
    assert res['summary']['rows_by_partition'] == {
        'RQF | Banner State': 730,
        'RQF | Customer Group State': 32485,
        'RQF INCLD CWH | Customer Group State': 1825,
        'RQF INCLD CWH | MG1': 1825,
    }


def test_internal_validation_passes(generated):
    _, res = generated
    assert res['validation_failures'] == []


def test_metadata_file_written(generated):
    _, res = generated
    metadata_path = Path(res['metadata_path'])
    assert metadata_path.exists()
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    assert metadata['schema_validation'] == 'passed'
    assert metadata['internal_validation'] == 'passed'
    assert metadata['output_schema'] == cb.OUTPUT_COLS
    assert metadata['total_rows'] == 36865


def test_even_split_when_no_daily_file(generated):
    df, res = generated
    assert res['summary']['daily_distribution'].startswith('even split')
    # within a single (entity, month) every day carries an equal share
    one = df[(df.Partition == 'Banner State') & (df['Banner Group Code'] == 'AMC')].copy()
    one['ym'] = one['Cal Year Mth Day'].astype(int) // 100
    feb = pd.to_numeric(one[one.ym == 202502]['Budget'])
    assert feb.nunique() == 1 and len(feb) == 28


def test_monthly_rollup_matches_validation(generated):
    got, _ = generated
    g = _rollup(got)
    v = _rollup(pd.read_excel(VALIDATION, sheet_name='Sheet1'))

    assert len(g) == len(v), f"group count differs: got {len(g)} vs validation {len(v)}"
    j = g.join(v, how='outer', lsuffix='_g', rsuffix='_v').fillna(0.0)
    dB = (j['Budget_g'] - j['Budget_v']).abs()
    dG = (j['GP_g'] - j['GP_v']).abs()
    bad = j[(dB > ROLLUP_TOL) | (dG > ROLLUP_TOL)]
    assert bad.empty, f"{len(bad)} groups exceed ${ROLLUP_TOL} tolerance; worst Budget diff ${dB.max():.2f}"


def test_supplied_daily_file_must_cover_year(tmp_path):
    # a daily file that covers only one month must abort loudly rather than equal-split
    daily = tmp_path / "daily.csv"
    pd.DataFrame({'date': cb.days_of(202502), 'prec': 1.0}).to_csv(daily, index=False)
    with pytest.raises(RuntimeError, match="does not cover the fiscal year"):
        convert_fy_budget([CORE, CUSTPLANT, daily], FY26_START, tmp_path / "x.csv")


def test_full_daily_file_is_applied(tmp_path):
    # a complete daily file should be detected and applied (not equal split)
    days = [d for ym in cb.fy_months(FY26_START) for d in cb.days_of(ym)]
    daily = tmp_path / "daily.csv"
    pd.DataFrame({'date': days, 'prec': range(1, len(days) + 1)}).to_csv(daily, index=False)
    res = convert_fy_budget([CORE, CUSTPLANT, daily], FY26_START, tmp_path / "y.csv")
    assert res['summary']['daily_distribution'] == 'applied'
    assert res['validation_failures'] == []          # weighting still ties back to monthly source


def test_zero_weight_month_fails_loudly(tmp_path):
    days = [d for ym in cb.fy_months(FY26_START) for d in cb.days_of(ym)]
    daily = tmp_path / "zero_daily.csv"
    pd.DataFrame({'date': days, 'prec': 0}).to_csv(daily, index=False)
    with pytest.raises(RuntimeError, match="weights sum to zero or less"):
        convert_fy_budget([CORE, CUSTPLANT, daily], FY26_START, tmp_path / "z.csv")


def test_output_schema_validation_fails_on_column_drift():
    df = pd.DataFrame(columns=cb.OUTPUT_COLS[:-1] + ['UnexpectedColumn'])
    with pytest.raises(RuntimeError, match="schema mismatch"):
        cb.validate_output_schema(df)


def test_fy27_merged_workbook_converts_with_embedded_daily_allocations(tmp_path):
    assert is_fy27_merged_workbook(FY27_MERGED)
    out = tmp_path / 'fy27.csv'
    result = convert_fy_budget([FY27_MERGED], FY27_START, out)
    got = pd.read_csv(out, low_memory=False)
    assert len(got) == 34310
    assert list(got.columns) == cb.OUTPUT_COLS
    assert result['validation_failures'] == []
    assert result['summary']['daily_distribution'] == 'embedded Core/CW daily allocations'
    assert got['Cal Year Mth Day'].min() == 20260701
    assert got['Cal Year Mth Day'].max() == 20270630
    metadata = json.loads(Path(result['metadata_path']).read_text(encoding='utf-8'))
    assert 'customer_daily_allocation' in metadata['source_layout']
    assert 'business-confirmed rule' in metadata['source_layout']['customer_daily_allocation']


def test_fy27_customer_view_uses_core_daily_allocation(tmp_path):
    """AMC's first daily amount must use the Core percentage, never a blend."""
    out = tmp_path / 'fy27.csv'
    convert_fy_budget([FY27_MERGED], FY27_START, out)
    got = pd.read_csv(out, keep_default_na=False, low_memory=False)
    wb = load_workbook(FY27_MERGED, data_only=True)
    sheets = _sheet_map(wb)
    banner, _ = _read_customer(wb[sheets['customer view']])
    core = _read_daily(wb[sheets['fy27 daily allocation_core']], cb.fy_months(FY27_START))
    monthly = float(banner[(banner.code == 'AMC') & (banner.month_idx == 0)].ns.iloc[0])
    expected = round(monthly * float(core.at[20260701, 'prec']), 2)
    actual = float(got[(got['Type'] == 'RQF') & (got['Partition'] == 'Banner State') &
                       (got['Banner Group Code'] == 'AMC') &
                       (got['Cal Year Mth Day'] == 20260701)]['Budget'].iloc[0])
    assert actual == pytest.approx(expected, abs=0.005)


def test_published_schema_uses_supplied_snowflake_view_contract(tmp_path):
    out = tmp_path / 'fy27.csv'
    convert_fy_budget([FY27_MERGED], FY27_START, out)
    got = pd.read_csv(out, keep_default_na=False, low_memory=False)
    assert list(got.columns) == cb.OUTPUT_COLS
    assert set(got['State']) == {'NA'}
    assert set(got['Plant Code'].replace('NA', '').unique()) == {'', '1201', '1302', '1401', '1402', '1501', '1601', '1702', '1801'}
    assert (got['Ban_State_Key'] == got['Banner Group Code'] + got['State']).all()
