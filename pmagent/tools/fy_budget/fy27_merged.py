"""Reader for the FY27 single-workbook budget upload.

Every source range in this module is located from its visible labels rather than
hard-coded row/column numbers.  The source references written to metadata mirror
the tabs and labelled sections used below.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from . import convert_budget as cb


REQUIRED_SHEETS = {
    "customer view", "plant view", "total cat view", "core cat view",
    "cw cat view", "fy27 daily allocation_cw", "fy27 daily allocation_core",
}
EXCLUDE_CUSTOMER_GROUPS = {"NA", "Total", "AMC", "DDS"}
MG1_SECTIONS = {"Private Label": "private", "Exclude Private Label": "nonprivate"}


def _normalised(name: str) -> str:
    return " ".join(str(name).strip().casefold().split())


def is_fy27_merged_workbook(path: Path) -> bool:
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        return REQUIRED_SHEETS <= {_normalised(s) for s in wb.sheetnames}
    except Exception:
        return False


def _sheet_map(wb) -> dict[str, str]:
    sheets = {_normalised(s): s for s in wb.sheetnames}
    missing = REQUIRED_SHEETS - sheets.keys()
    if missing:
        raise RuntimeError(f"{wb}: missing FY27 sheets {sorted(missing)}")
    return sheets


def _as_yyyymmdd(value) -> int:
    if isinstance(value, datetime):
        return value.year * 10000 + value.month * 100 + value.day
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return value.year * 10000 + value.month * 100 + value.day
    raise RuntimeError(f"Expected an Excel date, got {value!r}")


def _month_columns(ws, metric: str) -> tuple[list[int], list[int]]:
    """Return the twelve metric columns and their YYYYMM values, from labels."""
    for row in ws.iter_rows():
        cols = [c.column for c in row if str(c.value).strip() == metric]
        if len(cols) < 12:
            continue
        cols = cols[:12]
        for header_row in range(row[0].row - 1, 0, -1):
            values = [ws.cell(header_row, c).value for c in cols]
            try:
                months = [_as_yyyymmdd(v) // 100 for v in values]
            except RuntimeError:
                continue
            if len(set(months)) == 12:
                return cols, months
    raise RuntimeError(f"{ws.title}: could not locate 12 monthly {metric!r} columns")


def _values(ws, row: int, cols: list[int]) -> list[float]:
    raw = [ws.cell(row, c).value for c in cols]
    values = pd.to_numeric(raw, errors="coerce")
    if any(pd.isna(v) and raw[i] not in (None, "") for i, v in enumerate(values)):
        raise RuntimeError(f"{ws.title}: row {row} contains a non-numeric monthly value")
    # The Core Cat View has genuinely blank Private Label MG1 rows where the
    # corresponding Total Cat View row is zero; retain that source meaning.
    return [0.0 if pd.isna(v) else float(v) for v in values]


def _monthly_rows(code: str, row: int, ns_cols: list[int], gp_cols: list[int], ws) -> list[dict]:
    ns, gp = _values(ws, row, ns_cols), _values(ws, row, gp_cols)
    return [{"code": code, "month_idx": i, "ns": ns[i], "gp": gp[i]}
            for i in range(12)]


def _read_customer(ws) -> tuple[pd.DataFrame, pd.DataFrame]:
    ns_cols, months = _month_columns(ws, "Net Sales")
    gp_cols, gp_months = _month_columns(ws, "GP")
    if months != gp_months:
        raise RuntimeError(f"{ws.title}: Net Sales and GP month headers differ")

    # Customer View does not label its code column.  Find the column containing
    # the two explicitly-labelled banner rows; in the supplied file this is B.
    code_col = next((c for c in range(1, min(ns_cols))
                     if {"AMC", "DDS"} <= {str(ws.cell(r, c).value or "").strip()
                                           for r in range(3, ws.max_row + 1)}), None)
    if code_col is None:
        raise RuntimeError(f"{ws.title}: could not locate the AMC/DDS code column")
    populated = [(r, ws.cell(r, code_col).value) for r in range(3, ws.max_row + 1)
                 if ws.cell(r, code_col).value not in (None, "")]
    first_blank_after_banners = next(
        (r for r in range(populated[0][0], ws.max_row + 1)
         if ws.cell(r, code_col).value in (None, "")), None)
    if first_blank_after_banners is None:
        raise RuntimeError(f"{ws.title}: missing blank separator after banner rows")
    banner_rows = [(r, str(v).strip()) for r, v in populated if r < first_blank_after_banners]
    if [code for _, code in banner_rows] != ["AMC", "DDS"]:
        raise RuntimeError(f"{ws.title}: expected banner rows AMC, DDS; found {banner_rows}")

    banners = []
    groups = []
    for r, value in populated:
        code = str(value).strip()
        if r < first_blank_after_banners:
            banners.extend(_monthly_rows(code, r, ns_cols, gp_cols, ws))
        elif code not in EXCLUDE_CUSTOMER_GROUPS:
            groups.extend(_monthly_rows(code, r, ns_cols, gp_cols, ws))
    if not groups:
        raise RuntimeError(f"{ws.title}: no customer-group rows found")
    return pd.DataFrame(banners), pd.DataFrame(groups)


def _read_plant(ws) -> pd.DataFrame:
    ns_cols, months = _month_columns(ws, "Net Sales")
    gp_cols, gp_months = _month_columns(ws, "GP")
    if months != gp_months:
        raise RuntimeError(f"{ws.title}: Net Sales and GP month headers differ")
    cc_col = next(c.column for c in ws[2] if str(c.value).strip() == "CC")
    component_col = cc_col - 3  # labelled Total/CW/Core values immediately precede Plant/State/CC.
    rows = []
    for r in range(3, ws.max_row + 1):
        component = str(ws.cell(r, component_col).value or "").strip()
        cc = ws.cell(r, cc_col).value
        if component not in {"Core", "CW"} or not isinstance(cc, (int, float)):
            continue
        for item in _monthly_rows(str(int(cc))[-4:], r, ns_cols, gp_cols, ws):
            item["allocation"] = component.casefold()
            item["cc"] = item.pop("code")
            rows.append(item)
    found = pd.DataFrame(rows)
    if found.empty or set(found["allocation"]) != {"core", "cw"}:
        raise RuntimeError(f"{ws.title}: expected Core and CW plant blocks")
    return found


def _read_category(ws) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ns_cols, months = _month_columns(ws, "Net Sales")
    gp_cols, gp_months = _month_columns(ws, "GP")
    if months != gp_months:
        raise RuntimeError(f"{ws.title}: Net Sales and GP month headers differ")
    mg1_col = next(c.column for c in ws[2] if str(c.value).strip() == "MG1")
    section = None
    rows = []
    total_rows = []
    for r in range(3, ws.max_row + 1):
        label = str(ws.cell(r, mg1_col).value or "").strip()
        if label in MG1_SECTIONS:
            section = MG1_SECTIONS[label]
            continue
        if not section:
            continue
        if label == "Total":
            section = None
            continue
        if label in {"FOS", "PBS", "PRV", "OTC", "MED"}:
            for item in _monthly_rows(label, r, ns_cols, gp_cols, ws):
                item["section"] = section
                item["mg1"] = item.pop("code")
                rows.append(item)
    # The first Total is the total category value used to calculate the blended
    # Customer View daily allocation.
    for r in range(3, ws.max_row + 1):
        if str(ws.cell(r, mg1_col).value or "").strip() == "Total":
            total_rows = _monthly_rows("Total", r, ns_cols, gp_cols, ws)
            break
    found = pd.DataFrame(rows)
    if found.empty or not total_rows:
        raise RuntimeError(f"{ws.title}: expected category Total, Private Label and Exclude Private Label sections")
    return found, pd.DataFrame(total_rows), pd.DataFrame({"months": months})


def _read_daily(ws, months: list[int]) -> pd.DataFrame:
    header = next((r for r in range(1, ws.max_row + 1)
                   if str(ws.cell(r, 1).value).strip() == "Date"), None)
    if header is None:
        raise RuntimeError(f"{ws.title}: missing Date header")
    columns = {str(c.value).strip(): c.column for c in ws[header] if c.value is not None}
    if "Daily Sales %" not in columns:
        raise RuntimeError(f"{ws.title}: missing Daily Sales % header")
    rows = []
    for r in range(header + 1, ws.max_row + 1):
        date = ws.cell(r, columns["Date"]).value
        weight = ws.cell(r, columns["Daily Sales %"]).value
        if date is None and weight is None:
            continue
        rows.append({"date": _as_yyyymmdd(date), "prec": float(weight)})
    out = pd.DataFrame(rows).drop_duplicates("date").set_index("date")
    cb.check_daily_coverage(out, months)
    for month in months:
        check = out.loc[[d for d in cb.days_of(month)], "prec"].sum()
        if abs(check - 1.0) > 1e-9:
            raise RuntimeError(f"{ws.title}: daily weights for {month} sum to {check}, not 1")
    return out


def _expand_components(group: pd.DataFrame, rates: dict[str, pd.DataFrame], months: list[int]) -> pd.DataFrame:
    parts = []
    for allocation, component in group.groupby("allocation"):
        monthly = {(r.month_idx, "ns"): r.ns for r in component.itertuples()}
        monthly.update({(r.month_idx, "gp"): r.gp for r in component.itertuples()})
        parts.append(cb._expand_to_days(monthly, rates[allocation], months))
    return pd.concat(parts).groupby("date", as_index=False)[["budget", "gp"]].sum()


def _build_components(source: pd.DataFrame, code_col: str, rates: dict[str, pd.DataFrame], months: list[int],
                      type_: str, partition: str, stamp) -> pd.DataFrame:
    parts = []
    for code, group in source.groupby(code_col):
        parts.append(cb._finalize(_expand_components(group, rates, months), type_, partition, **stamp(code)))
    return pd.concat(parts, ignore_index=True)


def _validate(out: pd.DataFrame, expected: pd.DataFrame, mask, output_key: str, expected_key: str,
              label: str, months: list[int]) -> list[str]:
    actual = out.loc[mask].copy()
    actual[output_key] = actual[output_key].astype(str)
    actual["ym"] = actual["CalYearMthDay"].astype(int) // 100
    actual = actual.groupby([output_key, "ym"])[["Budget", "GP"]].sum()
    failures = []
    for row in expected.groupby([expected_key, "month_idx"])[["ns", "gp"]].sum().reset_index().itertuples():
        key = (str(getattr(row, expected_key)), months[int(row.month_idx)])
        got = actual.loc[key] if key in actual.index else pd.Series({"Budget": 0.0, "GP": 0.0})
        if abs(float(got.Budget) - float(row.ns)) > 0.01 or abs(float(got.GP) - float(row.gp)) > 0.01:
            failures.append(f"{label} {key[0]} {key[1]} does not roll up to source")
    return failures


def run(path: str | Path, fy_start: int, out_path: str | Path) -> dict:
    path, out_path = Path(path), Path(out_path)
    months = cb.fy_months(fy_start)
    wb = load_workbook(path, data_only=True)
    sheets = _sheet_map(wb)
    customer = wb[sheets["customer view"]]
    plant = wb[sheets["plant view"]]
    total_cat, core_cat, cw_cat = (wb[sheets[n]] for n in ("total cat view", "core cat view", "cw cat view"))
    banner, groups = _read_customer(customer)
    plants = _read_plant(plant)
    total_mg1, total_category, month_frame = _read_category(total_cat)
    core_mg1, _, _ = _read_category(core_cat)
    cw_mg1, _, _ = _read_category(cw_cat)
    if list(month_frame.months) != months:
        raise RuntimeError(f"FY27 month headers are {list(month_frame.months)}, but fy_start={fy_start} expects {months}")
    rates = {
        "core": _read_daily(wb[sheets["fy27 daily allocation_core"]], months),
        "cw": _read_daily(wb[sheets["fy27 daily allocation_cw"]], months),
    }
    private = core_mg1[core_mg1.section == "private"].assign(allocation="core")
    nonprivate = pd.concat([
        core_mg1[core_mg1.section == "nonprivate"].assign(allocation="core"),
        cw_mg1[cw_mg1.section == "nonprivate"].assign(allocation="cw"),
    ], ignore_index=True)
    parts = [
        # Business-confirmed rule: Customer View is a Core view. It uses the
        # Core allocation directly and is never a Core/CW blended cut.
        cb.build_banner_cut(banner, rates["core"], months),
        cb.build_cg_cut(groups, rates["core"], months),
        _build_components(plants, "cc", rates, months, "RQF", "Customer Group State",
                          lambda cc: {"mg1": "FOS", "cg": "PLANT", "plant": int(cc)}),
        _build_components(private, "mg1", rates, months, "RQF INCLD CWH", "Customer Group State",
                          lambda mg1: {"mg1": mg1, "pl": "Yes"}),
        _build_components(nonprivate, "mg1", rates, months, "RQF INCLD CWH", "MG1",
                          lambda mg1: {"mg1": mg1, "pl": "No"}),
    ]
    raw = pd.concat(parts, ignore_index=True)[cb.RAW_OUTPUT_COLS]
    cb.validate_raw_output_schema(raw)
    plant_total = plants.groupby(["cc", "month_idx"])[["ns", "gp"]].sum().reset_index()
    failures = []
    failures += _validate(raw, banner, (raw.Type == "RQF") & (raw.Partition == "Banner State"), "BannerGroupCode", "code", "Banner", months)
    failures += _validate(raw, groups, (raw.Type == "RQF") & (raw.Partition == "Customer Group State") & (raw.CustomerGroupCode != "PLANT"), "CustomerGroupCode", "code", "Customer group", months)
    failures += _validate(raw, plant_total, (raw.Type == "RQF") & (raw.Partition == "Customer Group State") & (raw.CustomerGroupCode == "PLANT"), "Plant", "cc", "Plant", months)
    failures += _validate(raw, total_mg1[total_mg1.section == "private"], (raw.Type == "RQF INCLD CWH") & (raw.Partition == "Customer Group State"), "MG1Code", "mg1", "Private MG1", months)
    failures += _validate(raw, total_mg1[total_mg1.section == "nonprivate"], (raw.Type == "RQF INCLD CWH") & (raw.Partition == "MG1"), "MG1Code", "mg1", "Non-private MG1", months)
    if failures:
        raise RuntimeError("FY27 internal validation failed: " + "; ".join(failures[:10]))
    out = cb.to_snowflake_view_contract(raw)
    cb.validate_output_schema(out)
    write_df = out.copy()
    for column in ("Budget", "GP", "RQF"):
        write_df[column] = pd.to_numeric(write_df[column]).round(2)
    write_df.to_csv(out_path, index=False)
    partitions = {f"{t} | {p}": int(n) for (t, p), n in out.groupby(["Type", "Partition"]).size().items()}
    metadata_path = cb.write_metadata(out_path=out_path, core_path=path, custplant_path=path,
        dailyrate_path=None, fy_start=fy_start, months=months, rows=len(out), partitions=partitions,
        daily_rate="embedded Core/CW daily allocations", validation_failures=[])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source_layout"] = {
        "customer_view": "Customer View: AMC/DDS banner rows and customer groups after their blank separator",
        "plant_view": "Plant View: Core and CW blocks, keyed by CC",
        "private_mg1": "Total/Core Cat View: Private Label rows; allocated with FY27 Daily Allocation_Core",
        "nonprivate_mg1": "Core Cat View + CW Cat View: Exclude Private Label rows; daily values are summed",
        "customer_daily_allocation": "FY27 Daily Allocation_Core applied directly to all Customer View banner and customer-group rows (business-confirmed rule).",
        "cw_daily_allocation": "FY27 Daily Allocation_CW applied only to CW Category and CW Plant components; Core components use FY27 Daily Allocation_Core.",
        "output_contract": "24-column EDP_PROD_PRES_Rpt_Sales_Budget_VW.csv layout: spaces in column names, literal NA placeholders, and four derived key fields.",
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return {"rows": len(out), "partitions": partitions, "daily_rate": "embedded Core/CW daily allocations",
            "validation_failures": [], "metadata_path": str(metadata_path)}
