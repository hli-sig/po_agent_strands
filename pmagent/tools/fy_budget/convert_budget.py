"""
Convert FY budget spreadsheets to the Snowflake-ingest daily CSV.

Inputs
------
core_path      : 'Core.xlsx' -> sheet 'Total by month' (Total/Private/Non Private; Net sales & GP).
                 The MG1 cuts are taken from here (incl. CW warehouse).
custplant_path : customer/plant workbook -> sheets 'Customer view' and 'Plant_View'.
dailyrate_path : OPTIONAL daily-weight file. If omitted, each month is split evenly
                 across its days (monthly totals are identical either way).
fy_start       : YYYYMM of the first fiscal month, e.g. 202502 for the legacy FY26
                 fixture. Twelve months are emitted. FY27's merged workbook is read
                 by `fy27_merged.py` through `pipeline.convert_fy_budget`.

Output: one CSV in the validation schema, one row per (entity, day).

The readers detect the month columns by content, so they work regardless of which
month the fiscal year starts on or where the Net sales / GP blocks sit.
"""
import argparse
import calendar
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIG  — edit here when a source file changes shape.
# =============================================================================

# Internal schema used while source rollups are validated.
RAW_OUTPUT_COLS = [
    'Type', 'Partition', 'CalYearMthDay', 'DivisionCode', 'DistrChannelCode',
    'OrderType', 'MG1Code', 'ProductHierarchyLevel5', 'SalesOrganisation',
    'CustomerGroupCode', 'PrivateLabelFlag', 'BannerGroupCode', 'State',
    'Budget', 'GP', 'RQF', 'Comments', 'Plant',
]

# Published Snowflake view schema.  This column order and the calculated key
# fields are evidenced by EDP_PROD_PRES_Rpt_Sales_Budget_VW.csv supplied with
# the FY27 change request.
OUTPUT_COLS = [
    'Type', 'Partition', 'Cal Year Mth Day', 'Division Code', 'Distr Channel Code',
    'Order Type', 'Plant Code', 'MG1 Code', 'Product Hierarchy Level 5',
    'Sales Organisation', 'Customer Group Code', 'Private Label Flag',
    'Banner Group Code', 'State', 'Budget', 'GP', 'RQF', 'Comments',
    'Division Key', 'Dist Channel Key', 'Div_Dist_Key', 'MG1_PL_SO_DC_Key',
    'Ban_State_Key', 'Group_State_Key',
]

# Constant values stamped on every row.
DEFAULT_FIELDS = {'DivisionCode': 2, 'DistrChannelCode': 1, 'SalesOrganisation': 1010}

# Customer-group codes that are NOT real groups (grand total / unclassified) -> skipped.
EXCLUDE_CG_CODES: set[str] = {'NA', 'Total'}

# All twelve month abbreviations, used to locate the month header row by content.
MONTH_ABBR = {'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'}


@dataclass(frozen=True)
class CoreLayout:
    sheet: str = 'Total by month'                 # MG1 cuts come from the Total (incl-CW) sheet
    section_names: tuple = ('Total', 'Private', 'Non Private')
    metric_names: tuple = ('Net sales', 'GP')
    month_cols_0idx: tuple = tuple(range(1, 13))  # 12 monthly values sit in cols 1..12


@dataclass(frozen=True)
class CustViewLayout:
    sheet: str = 'Customer view'
    banner_codes: tuple = ('AMC', 'DDS')
    group_marker: str = 'Group code'


@dataclass(frozen=True)
class PlantViewLayout:
    sheet: str = 'Plant_View'
    core_marker: str = 'Core'
    cw_markers: tuple = ('CW', 'CW-Updated')


@dataclass(frozen=True)
class DailyRateLayout:
    date_aliases: tuple = ('date', 'CalYearMthDay', 'CalYearMthDay_x', 'YYYYMMDD', 'Cal Year Mth Day')
    weight_aliases: tuple = ('prec', 'Prec', 'Daily Weight', 'weight', 'Weight', 'Daily Rate', 'pct')


CORE = CoreLayout()
CV = CustViewLayout()
PV = PlantViewLayout()
DR = DailyRateLayout()


# =============================================================================
# UTILITIES
# =============================================================================

def fy_months(fy_start: int) -> list[int]:
    """[202502, 202503, ..., 202601] for fy_start=202502 (12 consecutive YYYYMM)."""
    y, m = divmod(fy_start, 100)
    out = []
    for _ in range(12):
        out.append(y * 100 + m)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def days_of(yyyymm: int) -> list[int]:
    """All days of a month as YYYYMMDD ints."""
    y, m = divmod(yyyymm, 100)
    return [y * 10000 + m * 100 + d for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def _num(v) -> float:
    """Treat blanks/None/non-numeric as 0.0."""
    return float(v) if isinstance(v, (int, float)) else 0.0


# =============================================================================
# READERS — return long-format DataFrames so the builders don't see file layout.
# =============================================================================

def read_core_view(path: Path) -> pd.DataFrame:
    """Parse 'Total by month'. Returns columns: section, metric, mg1, month_idx, value."""
    wb = load_workbook(path, data_only=True)
    if CORE.sheet not in wb.sheetnames:
        raise RuntimeError(f"{path.name}: expected a sheet named {CORE.sheet!r}, found {wb.sheetnames}")
    grid = [list(r) for r in wb[CORE.sheet].iter_rows(values_only=True)]

    mc = CORE.month_cols_0idx
    months_ok = any(all(grid[i][c] in MONTH_ABBR for c in mc) for i in range(min(6, len(grid))))
    if not months_ok:
        raise RuntimeError(f"{path.name} [{CORE.sheet}]: could not find a 12-month header in columns 2..13")

    rows = []
    for i, row in enumerate(grid):
        if row[0] in CORE.section_names and row[1] in CORE.metric_names:
            section, metric = row[0], row[1]
            for j in range(i + 1, i + 7):                 # up to 5 MG1 rows then blank/next
                if j >= len(grid) or grid[j][0] in (None, '') or grid[j][0] in CORE.section_names:
                    break
                mg1 = grid[j][0]
                for idx, c in enumerate(mc):
                    rows.append({'section': section, 'metric': metric, 'mg1': mg1,
                                 'month_idx': idx, 'value': _num(grid[j][c])})
    return pd.DataFrame(rows)


def _detect_metric_cols(rows: list, label: str, n: int = 12) -> list[int]:
    """Column indices of the first `n` cells equal to `label` ('Net sales'/'GP'),
    scanning whichever row contains them. Skips the 13th (FY total) by taking only n."""
    for r in rows:
        cols = [i for i, c in enumerate(r) if c == label]
        if len(cols) >= n:
            return cols[:n]
    raise RuntimeError(f"Could not find {n} '{label}' columns")


def read_cust_plant(path: Path) -> dict[str, pd.DataFrame]:
    """Parse 'Customer view' + 'Plant_View'.
    Returns banner_df(code,month_idx,ns,gp), cust_group_df(code,month_idx,ns,gp),
    plant_df(cc,month_idx,ns,gp) with Core+CW summed per plant."""
    wb = load_workbook(path, data_only=True)

    # ---- Customer view ----
    cv = list(wb[CV.sheet].iter_rows(values_only=True))
    ns_cols = _detect_metric_cols(cv, 'Net sales')
    gp_cols = _detect_metric_cols(cv, 'GP')

    banner_rows, cg_rows = [], []
    in_groups = False
    for r in cv:
        name, code = r[0], r[1]
        if name is None and code in CV.banner_codes:
            for i in range(12):
                banner_rows.append({'code': code, 'month_idx': i,
                                    'ns': _num(r[ns_cols[i]]), 'gp': _num(r[gp_cols[i]])})
            continue
        if code == CV.group_marker:
            in_groups = True
            continue
        if not in_groups or code is None or not isinstance(code, str):
            continue
        if code in EXCLUDE_CG_CODES or code in CV.banner_codes:
            continue
        for i in range(12):
            cg_rows.append({'code': code, 'month_idx': i,
                            'ns': _num(r[ns_cols[i]]), 'gp': _num(r[gp_cols[i]])})

    # ---- Plant_View (Core + CW summed) ----
    pv = list(wb[PV.sheet].iter_rows(values_only=True))
    pns = _detect_metric_cols(pv, 'Net sales')
    pgp = _detect_metric_cols(pv, 'GP')

    def marker_row(markers) -> int:
        for i, r in enumerate(pv):
            if r and r[0] in markers:
                return i
        raise RuntimeError(f"Plant_View: no row starting with one of {markers}")

    def read_block(start_idx: int) -> dict:
        block = {}
        i = start_idx + 1
        while i < len(pv) and not isinstance(pv[i][2], (int, float)):   # skip to first CC row
            i += 1
        while i < len(pv) and isinstance(pv[i][2], (int, float)):
            r = pv[i]
            cc = str(int(r[2]))[-4:]
            block[cc] = ([_num(r[c]) for c in pns], [_num(r[c]) for c in pgp])
            i += 1
        return block

    core_block = read_block(marker_row((PV.core_marker,)))
    cw_block = read_block(marker_row(PV.cw_markers))
    plant_rows = []
    for cc, (cns, cgp) in core_block.items():
        wns, wgp = cw_block.get(cc, ([0.0] * 12, [0.0] * 12))
        for i in range(12):
            plant_rows.append({'cc': cc, 'month_idx': i,
                               'ns': cns[i] + wns[i], 'gp': cgp[i] + wgp[i]})

    return {'banner_df': pd.DataFrame(banner_rows),
            'cust_group_df': pd.DataFrame(cg_rows),
            'plant_df': pd.DataFrame(plant_rows)}


def read_daily_rate(path: Path) -> pd.DataFrame:
    """Optional daily-weight lookup -> DataFrame indexed by date(int YYYYMMDD), col 'prec'.
    Columns matched case-insensitively via DR aliases. Weights are relative (normalised later)."""
    suffix = path.suffix.lower()
    raw = pd.read_excel(path) if suffix == '.xlsx' else \
        pd.read_csv(path, sep='\t' if suffix in ('.tsv', '.txt') else ',')
    lower = {str(c).strip().lower(): c for c in raw.columns}
    date_col = next((lower[a.lower()] for a in DR.date_aliases if a.lower() in lower), None)
    weight_col = next((lower[a.lower()] for a in DR.weight_aliases if a.lower() in lower), None)
    if date_col is None or weight_col is None:
        raise RuntimeError(f"Daily-rate file {path.name}: need a date column {DR.date_aliases} "
                           f"and a weight column {DR.weight_aliases}; found {list(raw.columns)}")
    df = raw[[date_col, weight_col]].dropna().rename(columns={date_col: 'date', weight_col: 'prec'})
    df = df.drop_duplicates('date')
    df['date'] = df['date'].astype(int)
    df['prec'] = pd.to_numeric(df['prec'], errors='coerce')
    return df.dropna(subset=['prec']).set_index('date')


def check_daily_coverage(daily_rate: pd.DataFrame, months: list[int]) -> None:
    """Fail loudly if a supplied daily file doesn't cover every day of the fiscal year."""
    expected = [d for ym in months for d in days_of(ym)]
    missing = [d for d in expected if d not in daily_rate.index]
    if missing:
        have = daily_rate.index
        covered = f"{int(have.min())}..{int(have.max())}" if len(have) else "(none)"
        raise RuntimeError(
            f"Daily-rate file does not cover the fiscal year. Needs {expected[0]}..{expected[-1]} "
            f"({len(expected)} days); {len(missing)} missing (e.g. {missing[:5]}). File covers {covered}.")


# =============================================================================
# BUILDERS — expand monthly values to daily rows and stamp partition fields.
# =============================================================================

def _expand_to_days(monthly: dict, daily_rate, months: list[int]) -> pd.DataFrame:
    """monthly {(month_idx,'ns'|'gp'): value} -> one row per day (date, budget, gp).
    daily_rate=None -> split each month evenly; otherwise distribute by daily weights."""
    rows = []
    for mi, ym in enumerate(months):
        ns = monthly.get((mi, 'ns'), 0.0)
        gp = monthly.get((mi, 'gp'), 0.0)
        days = days_of(ym)
        if daily_rate is None:
            shares = [1.0 / len(days)] * len(days)
        else:
            w = [daily_rate.at[d, 'prec'] for d in days]
            total = sum(w)
            if total <= 0:
                raise RuntimeError(
                    f"Daily-rate weights sum to zero or less for month {ym}; "
                    "cannot distribute monthly values.")
            shares = [x / total for x in w]
        for d, s in zip(days, shares):
            rows.append({'date': d, 'budget': ns * s, 'gp': gp * s})
    return pd.DataFrame(rows)


def _finalize(df, type_, partition, mg1='', cg='', pl='No', banner='', plant='') -> pd.DataFrame:
    base = {c: '' for c in RAW_OUTPUT_COLS}
    base.update(DEFAULT_FIELDS)
    base.update({'Type': type_, 'Partition': partition, 'MG1Code': mg1,
                 'CustomerGroupCode': cg, 'PrivateLabelFlag': pl,
                 'BannerGroupCode': banner, 'Plant': plant})
    out = pd.DataFrame([base] * len(df)).reset_index(drop=True)
    out['CalYearMthDay'] = df['date'].astype(int).astype(str).values
    out['Budget'] = df['budget'].values
    out['GP'] = df['gp'].values
    out['RQF'] = out['Budget']
    return out


def to_snowflake_view_contract(raw: pd.DataFrame) -> pd.DataFrame:
    """Format validated rows to the supplied Snowflake view export contract.

    The supplied reference uses ``NA`` (literal text) for blank Plant, Banner
    and State values, while Customer Group remains blank when it is not part of
    the cut.  Its four key columns are derived from the visible output fields.
    """
    validate_raw_output_schema(raw)
    source = raw.copy()
    out = pd.DataFrame({
        'Type': source['Type'],
        'Partition': source['Partition'],
        'Cal Year Mth Day': source['CalYearMthDay'].astype(int),
        'Division Code': source['DivisionCode'].astype(int),
        'Distr Channel Code': source['DistrChannelCode'].astype(int),
        'Order Type': source['OrderType'].fillna('').astype(str),
        'Plant Code': source['Plant'].replace('', 'NA').fillna('NA').astype(str),
        'MG1 Code': source['MG1Code'].fillna('').astype(str),
        'Product Hierarchy Level 5': source['ProductHierarchyLevel5'].fillna('').astype(str),
        'Sales Organisation': source['SalesOrganisation'].astype(int),
        'Customer Group Code': source['CustomerGroupCode'].fillna('').astype(str),
        'Private Label Flag': source['PrivateLabelFlag'].fillna('').astype(str),
        'Banner Group Code': source['BannerGroupCode'].replace('', 'NA').fillna('NA').astype(str),
        'State': source['State'].replace('', 'NA').fillna('NA').astype(str),
        'Budget': source['Budget'],
        'GP': source['GP'],
        'RQF': source['RQF'],
        'Comments': source['Comments'].fillna('').astype(str),
    })
    out['Division Key'] = out['Division Code']
    out['Dist Channel Key'] = out['Distr Channel Code']
    out['Div_Dist_Key'] = out['Division Code'].astype(str) + '0' + out['Distr Channel Code'].astype(str)
    # This exact concatenation reproduces every distinct MG1_PL_SO_DC_Key in
    # the supplied reference extract (MG1/PL plus its visible fixed suffix).
    out['MG1_PL_SO_DC_Key'] = (
        out['MG1 Code'] + out['Private Label Flag'] + '0' +
        out['Distr Channel Code'].astype(str) + out['Sales Organisation'].astype(str)
    )
    out['Ban_State_Key'] = out['Banner Group Code'] + out['State']
    out['Group_State_Key'] = out['Customer Group Code'].where(
        out['Customer Group Code'].ne(''), ''
    )
    has_group = out['Group_State_Key'].ne('')
    out.loc[has_group, 'Group_State_Key'] = (
        out.loc[has_group, 'Customer Group Code'] + out.loc[has_group, 'State']
    )
    return out[OUTPUT_COLS]


def _monthly(grp, ns_attr='ns', gp_attr='gp') -> dict:
    """Build the {(month_idx,'ns'|'gp'): value} dict from a grouped source frame."""
    m = {}
    for r in grp.itertuples():
        m[(r.month_idx, 'ns')] = getattr(r, ns_attr)
        m[(r.month_idx, 'gp')] = getattr(r, gp_attr)
    return m


def build_banner_cut(banner_df, daily_rate, months):
    parts = []
    for code, grp in banner_df.groupby('code'):
        daily = _expand_to_days(_monthly(grp), daily_rate, months)
        parts.append(_finalize(daily, 'RQF', 'Banner State', mg1='FOS', banner=code))
    return pd.concat(parts, ignore_index=True)


def build_cg_cut(cust_group_df, daily_rate, months):
    parts = []
    for code, grp in cust_group_df.groupby('code'):
        daily = _expand_to_days(_monthly(grp), daily_rate, months)
        parts.append(_finalize(daily, 'RQF', 'Customer Group State', mg1='FOS', cg=code))
    return pd.concat(parts, ignore_index=True)


def build_plant_cut(plant_df, daily_rate, months):
    parts = []
    for cc, grp in plant_df.groupby('cc'):
        daily = _expand_to_days(_monthly(grp), daily_rate, months)
        parts.append(_finalize(daily, 'RQF', 'Customer Group State', mg1='FOS',
                               cg='PLANT', plant=int(cc)))
    return pd.concat(parts, ignore_index=True)


def _core_monthly(grp) -> dict:
    """Core sections store NS/GP as separate rows (metric col); fold into one dict."""
    m = {}
    for r in grp.itertuples():
        key = 'ns' if r.metric == 'Net sales' else 'gp'
        m[(r.month_idx, key)] = r.value
    return m


def build_private_mg1(core_df, daily_rate, months):
    parts = []
    sub = core_df[core_df['section'] == 'Private']
    for mg1, grp in sub.groupby('mg1'):
        daily = _expand_to_days(_core_monthly(grp), daily_rate, months)
        parts.append(_finalize(daily, 'RQF INCLD CWH', 'Customer Group State', mg1=mg1, pl='Yes'))
    return pd.concat(parts, ignore_index=True)


def build_nonprivate_mg1(core_df, daily_rate, months):
    """Non-Private MG1 cut straight from 'Total by month' Non Private (already incl. CW)."""
    parts = []
    sub = core_df[core_df['section'] == 'Non Private']
    for mg1, grp in sub.groupby('mg1'):
        daily = _expand_to_days(_core_monthly(grp), daily_rate, months)
        parts.append(_finalize(daily, 'RQF INCLD CWH', 'MG1', mg1=mg1, pl='No'))
    return pd.concat(parts, ignore_index=True)


# =============================================================================
# VALIDATION — every (entity, month) daily sum must equal the monthly source.
# =============================================================================

def validate(out, banner_df, cust_group_df, plant_df, core_df, months):
    out = out.copy()
    out['ym'] = out['CalYearMthDay'].astype(int) // 100
    out['Budget'] = pd.to_numeric(out['Budget'])
    out['GP'] = pd.to_numeric(out['GP'])
    failures = []

    def check(mask, src_df, out_key, src_key, label):
        sub = out[mask]
        if sub.empty:
            failures.append(f"{label}: no rows produced"); return
        g = sub.groupby([out_key, 'ym'])[['Budget', 'GP']].sum()
        for code in sub[out_key].unique():
            for mi, ym in enumerate(months):
                gb = g.loc[(code, ym), 'Budget'] if (code, ym) in g.index else 0.0
                gg = g.loc[(code, ym), 'GP'] if (code, ym) in g.index else 0.0
                s = src_df[(src_df[src_key].astype(str) == str(code)) & (src_df['month_idx'] == mi)]
                if s.empty:
                    continue
                if abs(gb - float(s['ns'].iloc[0])) > 0.01:
                    failures.append(f"{label} {code} {ym}: Budget {gb:.2f} != {float(s['ns'].iloc[0]):.2f}")
                if abs(gg - float(s['gp'].iloc[0])) > 0.01:
                    failures.append(f"{label} {code} {ym}: GP {gg:.2f} != {float(s['gp'].iloc[0]):.2f}")

    check((out.Type == 'RQF') & (out.Partition == 'Banner State'),
          banner_df, 'BannerGroupCode', 'code', 'Banner')
    check((out.Type == 'RQF') & (out.Partition == 'Customer Group State') & (out.CustomerGroupCode != 'PLANT'),
          cust_group_df, 'CustomerGroupCode', 'code', 'CG')
    check((out.Type == 'RQF') & (out.Partition == 'Customer Group State') & (out.CustomerGroupCode == 'PLANT'),
          plant_df.assign(cc=plant_df['cc'].astype(int)), 'Plant', 'cc', 'Plant')

    # MG1 cuts: source is core sections (NS/GP folded to ns/gp columns per mg1+month).
    def core_src(section):
        s = core_df[core_df['section'] == section]
        ns = s[s.metric == 'Net sales'].rename(columns={'value': 'ns'})[['mg1', 'month_idx', 'ns']]
        gp = s[s.metric == 'GP'].rename(columns={'value': 'gp'})[['mg1', 'month_idx', 'gp']]
        return ns.merge(gp, on=['mg1', 'month_idx'])

    check((out.Type == 'RQF INCLD CWH') & (out.Partition == 'Customer Group State'),
          core_src('Private'), 'MG1Code', 'mg1', 'PrivMG1')
    check((out.Type == 'RQF INCLD CWH') & (out.Partition == 'MG1'),
          core_src('Non Private'), 'MG1Code', 'mg1', 'NonPrivMG1')
    return failures


def _validate_columns(out: pd.DataFrame, expected: list[str], label: str) -> None:
    actual = list(out.columns)
    if actual != expected:
        missing = [c for c in expected if c not in actual]
        extra = [c for c in actual if c not in expected]
        raise RuntimeError(
            f"{label} schema mismatch. "
            f"Expected columns in order {expected}; got {actual}. "
            f"Missing={missing}; extra={extra}.")


def validate_raw_output_schema(out: pd.DataFrame) -> None:
    """Fail if the internal, source-validation columns drift."""
    _validate_columns(out, RAW_OUTPUT_COLS, 'Internal output')


def validate_output_schema(out: pd.DataFrame) -> None:
    """Fail if the published Snowflake view schema drifts."""
    _validate_columns(out, OUTPUT_COLS, 'Published output')


def _metadata_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.stem}.metadata.json")


def write_metadata(
    *,
    out_path: Path,
    core_path: Path,
    custplant_path: Path,
    fy_start: int,
    months: list[int],
    rows: int,
    partitions: dict,
    daily_rate: str,
    validation_failures: list[str],
    dailyrate_path: Path | None = None,
) -> Path:
    """Write an auditable sidecar JSON file beside the generated CSV."""
    inputs = {
        'core': str(core_path),
        'custplant': str(custplant_path),
        'dailyrate': str(dailyrate_path) if dailyrate_path is not None else None,
    }
    metadata = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'output_csv': str(out_path),
        'inputs': inputs,
        'fiscal_year_start': fy_start,
        'fiscal_months': months,
        'total_rows': rows,
        'rows_by_partition': partitions,
        'daily_distribution': daily_rate,
        'output_schema': OUTPUT_COLS,
        'schema_validation': 'passed',
        'internal_validation': 'passed'
        if not validation_failures else f"{len(validation_failures)} failures",
        'validation_failures': validation_failures,
    }
    path = _metadata_path(out_path)
    path.write_text(json.dumps(metadata, indent=2, default=str), encoding='utf-8')
    logger.info("Wrote metadata to %s", path)
    return path


# =============================================================================
# ORCHESTRATION + CLI
# =============================================================================

def run(core_path, custplant_path, fy_start, out_path, dailyrate_path=None):
    core_path = Path(core_path)
    custplant_path = Path(custplant_path)
    out_path = Path(out_path)
    dailyrate_path = Path(dailyrate_path) if dailyrate_path is not None else None
    logger.info("Starting FY budget conversion: fy_start=%s out=%s", fy_start, out_path)
    months = fy_months(fy_start)
    core_df = read_core_view(core_path)
    cp = read_cust_plant(custplant_path)

    daily_rate = None
    if dailyrate_path is not None:
        daily_rate = read_daily_rate(dailyrate_path)
        check_daily_coverage(daily_rate, months)

    parts = [
        build_banner_cut(cp['banner_df'], daily_rate, months),
        build_cg_cut(cp['cust_group_df'], daily_rate, months),
        build_plant_cut(cp['plant_df'], daily_rate, months),
        build_private_mg1(core_df, daily_rate, months),
        build_nonprivate_mg1(core_df, daily_rate, months),
    ]
    raw = pd.concat(parts, ignore_index=True)[RAW_OUTPUT_COLS]
    validate_raw_output_schema(raw)
    failures = validate(raw, cp['banner_df'], cp['cust_group_df'], cp['plant_df'], core_df, months)
    out = to_snowflake_view_contract(raw)
    validate_output_schema(out)

    write_df = out.copy()
    for c in ('Budget', 'GP', 'RQF'):
        write_df[c] = pd.to_numeric(write_df[c]).round(2)
    write_df.to_csv(out_path, index=False)
    validate_output_schema(write_df)
    partitions = {f'{t} | {p}': int(n) for (t, p), n in
                  out.groupby(['Type', 'Partition']).size().items()}
    daily_rate_label = 'applied' if daily_rate is not None else 'even split (no daily file)'
    metadata_path = write_metadata(
        out_path=out_path,
        core_path=core_path,
        custplant_path=custplant_path,
        dailyrate_path=dailyrate_path,
        fy_start=fy_start,
        months=months,
        rows=len(out),
        partitions=partitions,
        daily_rate=daily_rate_label,
        validation_failures=failures,
    )
    logger.info("Finished FY budget conversion: rows=%s validation_failures=%s", len(out), len(failures))

    return {
        'rows': len(out),
        'partitions': partitions,
        'daily_rate': daily_rate_label,
        'validation_failures': failures,
        'metadata_path': str(metadata_path),
    }


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
    p = argparse.ArgumentParser(description="Convert FY budget spreadsheets to the daily CSV.")
    p.add_argument('--core', required=True, type=Path)
    p.add_argument('--custplant', required=True, type=Path)
    p.add_argument('--fy-start', required=True, type=int, help='YYYYMM of first fiscal month, e.g. 202502')
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--dailyrate', type=Path, default=None, help='optional daily-weight file')
    a = p.parse_args()
    result = run(a.core, a.custplant, a.fy_start, a.out, dailyrate_path=a.dailyrate)
    import json
    print(json.dumps(result, indent=2, default=str))


if __name__ == '__main__':
    main()
