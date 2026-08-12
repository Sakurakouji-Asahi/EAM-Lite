# T+ Reconciliation Export Specification V1.1

## 1. Purpose and boundary

EAM-Lite does not call or write the T+ API in V1. T+ remains the official accounting ledger.

This workbook supports month-end manual reconciliation. It must never display wording that implies a voucher or asset card was posted to T+.

## 2. Export parameters

Required parameters:
- Company
- Accounting period `YYYY-MM`
- Data status: confirmed only by default
- Optional department, physical category and fixed-asset category filters
- Include disposed assets: yes by default when they had activity in the selected period

Each export receives a unique `export_id` and records user, Shanghai-time timestamp, filters, source data cut-off time and row count in `AuditLog`/`ExportLog`. Its registered monetary totals are persisted as typed Decimal `ExportLogTotal` rows under the approved schema version, not embedded in filter JSON.

## 3. Workbook sheets

### 3.1 `导出说明`

Contains:
- Workbook purpose and the statement “仅供T+人工对账，不代表已记账”
- Company and period
- Export id, user, export time and source cut-off
- Applied filters
- Calculation identity and column descriptions

### 3.2 `EAM固定资产明细`

One row per fixed asset with confirmed Finance data. Columns, in order:

1. EAM资产编号
2. T+资产卡片编码（可空）
3. 资产名称
4. 实物分类
5. 固定资产会计类别
6. 使用部门
7. 责任人
8. 当前位置
9. 资产状态
10. 达到可使用状态日期
11. 资本化日期
12. 折旧起始日期
13. 折旧方法
14. 使用年限（月）
15. 残值方式
16. 残值率
17. 残值金额
18. 原值
19. 期初累计折旧
20. 本期自动折旧
21. 本期手工折旧
22. 本期调整净额
23. 本期冲销净额
24. 期末累计折旧
25. 减值准备
26. 期末账面净值
27. 本期处置日期
28. 处置类型
29. 处置收入
30. 备注

Only confirmed entries and their confirmed reversal entries contribute to official columns; draft/cancelled batches do not. An original entry that was later reversed remains in its gross source column, and the linked reversal's signed accounting effect is shown in `本期冲销净额`, so the net effect reconciles to zero. Theoretical trial depreciation is never mixed into these amounts.

`本期调整净额` 只汇总本期非冲销、`source_type='adjustment'` 且来源为 `depreciation_adjustment` 的已确认折旧分录，保留正负号；cost_correction 和减值类调整分别进入原值或减值口径，不得混入累计折旧。`本期冲销净额` 是本期所有 `reversal_of_id` 非空分录金额的带符号代数和：冲销正折旧通常为负数，冲销原先的负折旧调整则为正数。勾稽公式对该列执行加法，绝不能取绝对值后假定所有冲销方向相同。

### 3.3 `本期折旧分录`

One row per posted `DepreciationEntry`, including entries from confirmed regular batches, confirmed reversal batches, opening sources and depreciation adjustments:
- Batch code
- EAM asset code
- T+ asset card code
- Period
- Entry type
- Source
- Original entry reference for reversal
- Amount
- Posted user/time and, where applicable, batch confirmer or reverser
- Remark

### 3.4 `T+数据粘贴区`

Finance may paste a T+ export without changing EAM source sheets. Required columns:
- T+资产卡片编码
- EAM资产编号（如T+已维护）
- 资产名称
- 原值
- 期初累计折旧
- 本期折旧
- 期末累计折旧
- 减值准备
- 期末账面净值

No pasted T+ data is written back into EAM-Lite business tables.

### 3.5 `对账差异`

Match priority:
1. Unique T+ asset card code;
2. Unique EAM asset code;
3. Otherwise mark as unmatched—never fuzzy-match amounts or names automatically.

Show:
- Match status: matched / EAM only / T+ only / duplicate key
- EAM and T+ values side by side
- Difference for original cost, current depreciation, accumulated depreciation, impairment and book value
- Difference reason and handling note columns for Finance to fill manually

## 4. Calculation identities

For the selected period:

`期末累计折旧 = 期初累计折旧 + 本期自动折旧 + 本期手工折旧 + 本期调整净额 + 本期冲销净额`

`期末账面净值 = 原值 - 期末累计折旧 - 减值准备`

The displayed value must agree with confirmed depreciation entries and must not fall below the applicable salvage floor except through an explicit, audited special adjustment allowed by Finance policy.

## 5. Excel typing and security

- Monetary cells are numeric with two-decimal display; do not convert them to text.
- Rates are numeric percentage cells.
- Dates are Excel date cells.
- Codes are text to preserve leading zeros.
- User-entered text beginning with `=`, `+`, `-` or `@` is escaped as safe text.
- The workbook contains no macros, external links or hidden executable content.
- Protect source sheets from accidental edits when supported, without setting a secret password in source code.

## 6. Acceptance

- Sheet names, column order and types match this specification.
- Totals in the detail, entry and difference sheets reconcile.
- The workbook totals equal the complete registered `ExportLogTotal` set, including disposal income from the typed `处置收入` detail column; unknown/missing metric keys or non-Decimal totals prevent completion.
- An asset with opening depreciation, current-period depreciation, adjustment, reversal and disposal can be traced end to end.
- Re-running the same export does not change business data and produces an auditable new `export_id`.
