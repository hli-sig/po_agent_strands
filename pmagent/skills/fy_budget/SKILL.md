---
name: fy-partition-rules
description: Deterministic source-to-Snowflake mapping for Finance FY budget workbooks.
---

# FY budget partition rules

Use this reference when running, validating, explaining, or deploying the FY Data
Prepare Tool. Do not infer mappings beyond the source rules below.

## Output contract

The output has one row per entity/day and these columns, in this exact order,
matching `fixtures/EDP_PROD_PRES_Rpt_Sales_Budget_VW.csv`:

`Type, Partition, Cal Year Mth Day, Division Code, Distr Channel Code, Order Type,
Plant Code, MG1 Code, Product Hierarchy Level 5, Sales Organisation,
Customer Group Code, Private Label Flag, Banner Group Code, State, Budget, GP,
RQF, Comments, Division Key, Dist Channel Key, Div_Dist_Key,
MG1_PL_SO_DC_Key, Ban_State_Key, Group_State_Key`

Constants: `Division Code=2`, `Distr Channel Code=1`, `Sales Organisation=1010`,
and `RQF=Budget`. Blank Plant, Banner and State values are written as literal
`NA`; `Budget` is Net Sales and `GP` is GP.

## FY27 merged workbook

The workbook is detected only when all of these sheets exist (case and surrounding
spaces are ignored): `Customer View`, `Plant View`, `Total Cat View`,
`Core Cat View`, `CW Cat View`, `FY27 Daily Allocation_Core`, and
`FY27 Daily Allocation_CW`.

`fy_start=202607`; the workbook covers Jul-2026 through Jun-2027.

| Cut | Source | Type / Partition | Daily allocation |
|---|---|---|---|
| Banner | `Customer View`: first AMC/DDS block | `RQF` / `Banner State` | `FY27 Daily Allocation_Core` |
| Customer group | `Customer View`: rows after the banner separator; exclude `NA`, `Total`, `AMC`, `DDS` | `RQF` / `Customer Group State` | `FY27 Daily Allocation_Core` |
| Plant | `Plant View`: Core and CW rows keyed by CC | `RQF` / `Customer Group State`, `CustomerGroupCode=PLANT` | Core and CW allocations applied separately, then summed |
| Private MG1 | `Core Cat View`: `Private Label` block | `RQF INCLD CWH` / `Customer Group State`, `PrivateLabelFlag=Yes` | Core allocation |
| Non-private MG1 | `Core Cat View` + `CW Cat View`: `Exclude Private Label` blocks | `RQF INCLD CWH` / `MG1`, `PrivateLabelFlag=No` | Core and CW allocations applied separately, then summed |

The Customer View/Core policy above is business-confirmed. Do not replace it with
a blend. The Core/CW daily rate sheets must each sum to 1.0 for every fiscal month.

## Legacy workbook format

Legacy input remains supported: a core workbook with `Total by month`, a
customer/plant workbook with `Customer view` and `Plant_View`, and an optional
daily-rate table. Without the optional table, months are split evenly; with it, all
days must be covered and each monthly weight total must be positive.

## Validation

The tool validates output schema and re-sums daily Budget/GP by entity and month.
FY27 also proves Core + CW equals source Total for plant and MG1 source components.
Only rounding cents may differ between a daily CSV roll-up and unrounded source.
