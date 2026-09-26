"""Synthetic FY budget workbooks in both source layouts: synthetic values.

The real fixtures are Finance's budget rows and are never committed, so on their
own the converter's regression tests skip on a fresh clone. These builders write
tiny workbooks with the **same layouts** the readers expect, so the converter
always has a runnable test (`tests/test_fy_budget_synthetic.py`), and a rebuild of
`pmagent/tools/fy_budget/` has a checkpoint.

The banner codes (AMC, DDS), MG1 codes (FOS, PBS, PRV, OTC, MED) and the
constants the output stamps are the ones the converter itself requires; the
customer groups (GRA...), plants and every amount are made up.

Every value is a formula of its position (`value(code, month_idx, metric)`), so a
test can work out what any output row must sum to without reading the workbook.

Legacy layout (FY26-style, `fy_start=202502`), two files:

* `core.xlsx`, sheet `Total by month`: a row of 12 month abbreviations in columns
  B..M, then for each section (`Total`, `Private`, `Non Private`) and metric
  (`Net sales`, `GP`) a label row followed by one row per MG1 code.
* `customer_and_plant.xlsx`, sheets `Customer view` (banner rows AMC and DDS with
  no name, a `Group code` marker row, then customer groups) and `Plant_View`
  (`Core` and `CW` blocks of plant rows keyed by a numeric cost centre).

FY27 layout (`fy_start=202607`), one workbook with seven tabs: Customer View,
Plant View, Total/Core/CW Cat View, and FY27 Daily Allocation_Core/_CW.
"""

from __future__ import annotations

import calendar
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

LEGACY_START = 202502
FY27_START = 202607
BANNERS = ("AMC", "DDS")
GROUPS = ("GRA", "GRB", "GRC")                 # real groups; "Total" and "NA" rows are skipped
PLANTS = {"1201": 101201, "1302": 101302}      # cost centre as the sheet shows it -> its last 4 digits
CW_PLANTS = ("1201",)                          # only some plants have a CW block
PRIVATE_MG1 = ("PBS", "PRV")
NONPRIVATE_MG1 = ("FOS", "OTC", "MED")
_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def months(fy_start: int) -> list[int]:
    y, m = divmod(fy_start, 100)
    out = []
    for _ in range(12):
        out.append(y * 100 + m)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def days(ym: int) -> list[int]:
    y, m = divmod(ym, 100)
    return [y * 10000 + m * 100 + d for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def value(code: str, month_idx: int, metric: str, part: str = "") -> float:
    """The synthetic monthly amount for one row. GP is 30% of net sales."""
    base = 1000.0 + 97.0 * (sum(map(ord, code + part)) % 50) + 13.0 * month_idx
    return round(base if metric == "ns" else base * 0.3, 2)


# --- legacy layout -----------------------------------------------------------

def write_legacy(folder: Path) -> tuple[Path, Path]:
    """Write core.xlsx and customer_and_plant.xlsx; return their paths."""
    fy = months(LEGACY_START)
    core = Workbook()
    ws = core.active
    ws.title = "Total by month"
    ws.append(["FY budget"])
    ws.append([None, *(_ABBR[ym % 100 - 1] for ym in fy)])
    ws.append([])
    sections = {"Total": PRIVATE_MG1 + NONPRIVATE_MG1, "Private": PRIVATE_MG1,
                "Non Private": NONPRIVATE_MG1}
    for section, mg1s in sections.items():
        for metric, key in (("Net sales", "ns"), ("GP", "gp")):
            ws.append([section, metric])
            for mg1 in mg1s:
                ws.append([mg1, *(value(mg1, i, key, section) for i in range(12))])
            ws.append([])
    core_path = folder / "core.xlsx"
    core.save(core_path)

    cp = Workbook()
    cv = cp.active
    cv.title = "Customer view"
    cv.append(["Customer", "Code", *(["Net sales"] * 13), *(["GP"] * 13)])   # 13th = FY total
    for banner in BANNERS:
        cv.append([None, banner, *_both(banner)])
    cv.append(["Customer group", "Group code"])
    for group in GROUPS:
        cv.append([f"Group {group}", group, *_both(group)])
    cv.append(["Grand total", "Total", *_both("Total")])
    cv.append(["Unclassified", "NA", *_both("NA")])

    pv = cp.create_sheet("Plant_View")
    pv.append([None, None, None, *(["Net sales"] * 12), *(["GP"] * 12)])
    for marker, plants, part in (("Core", tuple(PLANTS), "core"), ("CW", CW_PLANTS, "cw")):
        pv.append([marker])
        for cc in plants:
            pv.append([None, f"Plant {cc}", PLANTS[cc],
                       *(value(cc, i, "ns", part) for i in range(12)),
                       *(value(cc, i, "gp", part) for i in range(12))])
        pv.append(["Total", None, "Total"])
    cp_path = folder / "customer_and_plant.xlsx"
    cp.save(cp_path)
    return core_path, cp_path


def _both(code: str) -> list[float]:
    ns = [value(code, i, "ns") for i in range(12)]
    gp = [value(code, i, "gp") for i in range(12)]
    return [*ns, sum(ns), *gp, sum(gp)]


def legacy_expected(kind: str, code: str, month_idx: int, metric: str) -> float:
    """What the converter's output must sum to for one (entity, month)."""
    if kind == "plant":
        parts = ("core", "cw") if code in CW_PLANTS else ("core",)
        return round(sum(value(code, month_idx, metric, p) for p in parts), 2)
    if kind == "private":
        return value(code, month_idx, metric, "Private")
    if kind == "nonprivate":
        return value(code, month_idx, metric, "Non Private")
    return value(code, month_idx, metric)          # banner or customer group


# --- FY27 merged layout ------------------------------------------------------

def daily_weight(date: int, allocation: str) -> float:
    """Unnormalised weight of one day; normalised per month by `write_fy27`."""
    day = date % 100
    return (day % 7 + 1) * (2.0 if allocation == "cw" else 1.0) + (day % 3 if allocation == "cw" else 0)


def fy27_daily_share(date: int, allocation: str) -> float:
    ym = date // 100
    total = sum(daily_weight(d, allocation) for d in days(ym))
    return daily_weight(date, allocation) / total


def write_fy27(folder: Path, name: str = "FY27 budget upload.xlsx") -> Path:
    fy = months(FY27_START)
    dates = [datetime(ym // 100, ym % 100, 1) for ym in fy]
    wb = Workbook()

    cv = wb.active
    cv.title = "Customer View"
    cv.append([None, None, *dates, *dates])
    cv.append(["Customer", None, *(["Net Sales"] * 12), *(["GP"] * 12)])
    for banner in BANNERS:
        cv.append([f"{banner} banner", banner, *_monthly(banner)])
    cv.append([])                                   # the blank separator after the banners
    for group in GROUPS:
        cv.append([f"Group {group}", group, *_monthly(group)])
    cv.append(["Grand total", "Total", *_monthly("Total")])
    cv.append(["Unclassified", "NA", *_monthly("NA")])

    pv = wb.create_sheet("Plant View")
    pv.append([None, None, None, None, *dates, *dates])
    pv.append(["Component", "Plant", "State", "CC", *(["Net Sales"] * 12), *(["GP"] * 12)])
    for component, plants, part in (("Core", tuple(PLANTS), "core"), ("CW", CW_PLANTS, "cw")):
        for cc in plants:
            pv.append([component, f"Plant {cc}", "NA", PLANTS[cc], *_monthly(cc, part)])
        pv.append(["Total", None, None, "Total"])

    for title, part in (("Total Cat View", "total"), ("Core Cat View", "core"), ("CW Cat View", "cw")):
        ws = wb.create_sheet(title)
        ws.append([None, *dates, *dates])
        ws.append(["MG1", *(["Net Sales"] * 12), *(["GP"] * 12)])
        ws.append(["Total", *_category_total(part)])
        for section, mg1s in (("Private Label", PRIVATE_MG1), ("Exclude Private Label", NONPRIVATE_MG1)):
            ws.append([section])
            for mg1 in mg1s:
                ws.append([mg1, *_category(mg1, section, part)])
            ws.append(["Total"])

    for title, allocation in (("FY27 Daily Allocation_Core", "core"), ("FY27 Daily Allocation_CW", "cw")):
        ws = wb.create_sheet(title)
        ws.append(["Date", "Daily Sales %"])
        for ym in fy:
            for d in days(ym):
                ws.append([datetime(d // 10000, d // 100 % 100, d % 100), fy27_daily_share(d, allocation)])

    path = folder / name
    wb.save(path)
    return path


def _monthly(code: str, part: str = "") -> list[float]:
    return [*(value(code, i, "ns", part) for i in range(12)), *(value(code, i, "gp", part) for i in range(12))]


def fy27_category(mg1: str, section: str, part: str, month_idx: int, metric: str) -> float:
    """A category row's amount. CW has no private label; Total = Core + CW."""
    if part == "total":
        return round(sum(fy27_category(mg1, section, p, month_idx, metric) for p in ("core", "cw")), 2)
    if part == "cw" and section == "Private Label":
        return 0.0
    return value(mg1, month_idx, metric, section + part)


def _category(mg1: str, section: str, part: str) -> list[float]:
    return [*(fy27_category(mg1, section, part, i, "ns") for i in range(12)),
            *(fy27_category(mg1, section, part, i, "gp") for i in range(12))]


def _category_total(part: str) -> list[float]:
    rows = [_category(m, s, part) for s, ms in (("Private Label", PRIVATE_MG1),
                                                ("Exclude Private Label", NONPRIVATE_MG1)) for m in ms]
    return [round(sum(col), 2) for col in zip(*rows)]
