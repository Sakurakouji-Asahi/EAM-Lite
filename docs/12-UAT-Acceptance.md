# User Acceptance Test and Traceability V1.1

## 1. Acceptance method

Run UAT on the intended LAN deployment using PostgreSQL, production-like settings and a copy of non-sensitive test attachments. Record tester, date, build/commit, result and evidence for every case.

A case is accepted only when its expected result is demonstrated and no open severity-P0/P1 defect affects the flow. Failed cases must be corrected and rerun; they may not be waived by deleting tests or changing expected results after implementation.

## 2. Normative test data

Prepare at least:
- 1 company, 5 departments, 12 employees and a three-level location tree
- 8 physical categories covering equipment, mold, tool, inspection tool and office equipment
- 20 individually tracked assets, including fixed assets, controlled low-value assets and old assets with opening balances
- Assets using every V1 depreciation method
- 2 coding schemes with different reset scopes
- Finance, equipment, department-manager, employee, warehouse, HR, management and system-admin users
- Inventory, maintenance, offboarding and disposal scenarios

## 3. Performance acceptance baseline

Run the capacity test on the intended acceptance server over the approved LAN HTTPS endpoint with production permission checks, audit logging and PostgreSQL enabled. Record CPU, memory, storage, OS, browser, application build, database version and network conditions with the result.

Use a reproducible dataset containing 5,000 individually tracked assets, 100 enabled user accounts, representative departments/categories/locations, 12 months of depreciation and representative movement, inventory, maintenance and audit history. Read-request results use 10 concurrent active sessions, one warm-up run and at least 20 measured samples; the reported value is end-to-end p95. Run each batch scenario three times after warm-up and require every run to meet its limit.

| Scenario | Acceptance limit |
|---|---:|
| Authenticated Dashboard load | p95 <= 5 seconds |
| Paginated asset-ledger search/filter | p95 <= 3 seconds |
| Asset detail load, including permitted summaries | p95 <= 3 seconds |
| Inventory scan validation and recording | p95 <= 2 seconds |
| Create and freeze a 5,000-asset inventory snapshot | <= 60 seconds |
| Generate a monthly depreciation trial or confirmation for up to 5,000 depreciable assets | <= 300 seconds |
| Generate an Excel/T+ reconciliation export of 5,000 asset rows | <= 120 seconds |
| Render the browser A4 print preview and pagination for 500 labels | <= 60 seconds |

Timeouts, server errors, missing rows, duplicate writes, disabled authorization or reduced audit evidence fail the scenario even when elapsed time is within the limit. If the production server is materially weaker than the acceptance server, rerun this baseline before go-live.

### 3.1 UAT ownership

The system owner coordinates evidence and final sign-off. Primary business testers are: system_admin for setup/security/deployment/backup; finance for accounting, depreciation and T+; equipment for asset master, labels, lifecycle, inventory and maintenance; HR for offboarding; department managers/employees/warehouse/management for their scoped positive and negative permission cases. Security and cross-role cases require a second tester who did not implement the feature. Actual names and dates are recorded in the run evidence before execution.

## 4. Core UAT cases

| ID | Scenario | Required result |
|---|---|---|
| UAT-001 | First-time setup | Roles complete their permitted steps; final validation rechecks all nine real conditions, only system admin completes, and normal business users remain blocked until success. |
| UAT-002 | Physical/accounting classification | A mold can remain physically a mold while Finance marks it fixed or non-fixed independently. |
| UAT-003 | New controlled asset | A controlled non-fixed item cannot pass Finance confirmation with blank original cost; original cost 0, accumulated depreciation 0 and impairment 0 are accepted, while fixed-asset category and automatic depreciation remain absent. Threshold warnings never auto-classify it. |
| UAT-004 | New fixed asset | Finance confirms financial data; formalization atomically issues one permanent code and enters pending-label state. |
| UAT-005 | Code configuration | Admin changes supported components, length, start and reset scope without Python changes; preview issues no official numbers, and `custom_field` is absent from V1.1 UI/API choices and cannot be enabled from a crafted request. |
| UAT-006 | Concurrent codes | PostgreSQL concurrent formalization creates no duplicate or unregistered codes; committed codes remain permanently occupied. |
| UAT-007 | Code correction | system_admin correction records old/new code and reason, permanently occupies the old code, rotates QR and requires a new label without changing business state. |
| UAT-008 | Location and responsibility | In-use asset cannot lack department, responsible employee or leaf location. |
| UAT-009 | Movement and loan | Transfer updates current values/history atomically; an internal loan requires `borrower_employee` plus a server-side name snapshot while external input fields remain empty, an external loan requires name and has no employee/snapshot link, mixed/empty data is rejected, and loan/return uses one structured active Loan, two linked movements and idempotent return. |
| UAT-010 | QR label | Browser A4 print preview paginates and renders 500 labels within the Section 3 limit; at 100% print the QR is at least 20 mm and scans from paper. Authorized asset and label pages show the current non-cacheable QR thumbnail, while scan and Web per-asset confirmation both enforce current identity, printed state, explicit checks, idempotency, audit and field permissions. |
| UAT-011 | Attachment security | Valid attachments upload and permission-checked download; forbidden type, oversize and unauthorized access are rejected. |
| UAT-012 | Straight-line depreciation | Results, rounding, salvage floor and final-period correction match `08-Depreciation-Calculation-Spec.md`. |
| UAT-013 | Other depreciation methods | Units, double-declining, sum-of-years-digits, manual and no-depreciation cases match normative examples. |
| UAT-014 | Opening asset | Actual opening accumulated depreciation remains authoritative; theoretical trial is visibly separate and never overwrites it. |
| UAT-015 | Monthly batch | Trial has no accounting effect; confirmation is idempotent and immutable; reversal links to the original entries. |
| UAT-016 | Inventory snapshot | Assignees are locked at publish; later movement does not change snapshots, and simultaneous location/responsible/status differences persist as one multiple_mismatch with every dimension visible. After scanning stops, ordinary scans fail while authorized reviewers can reason-code one supplemental scan without reopening; every final abnormal/missing line has a resolution. Published cancellation and post-close correction follow task-type close permissions and preserve evidence. |
| UAT-017 | Repeated scanning | Scan events are retained, but one asset is counted once using its latest valid result. |
| UAT-018 | Inventory surplus | A not-yet-created physical item can store photos, be confirmed and then create an asset draft without fake asset id. |
| UAT-019 | Preventive maintenance | Day/week/month/year due calculations work; result is normal/problem_found, problem evidence uses its own attachment target, and V1 assignment is the plan responsible employee. Only equipment voids a confirmed record; its Problem remains historical but leaves current-open queries, and reconstruction rechecks the current responsible/scope permission. |
| UAT-020 | Offboarding | active→leaving explicitly disables the Employee for new business, snapshots responsibility/internal loans and rejects new links without silently changing the User account. HR-only refresh adds only pre-initiation links; confirmed disposal is a terminal resolution despite retained historical responsibility fields. Initial completion requires a valid explicit termination date. A later omission creates one linked supplemental clearance without reopening the original or changing its date; a disposal used by an Item cannot then be reversed silently. |
| UAT-021 | Disposal | Planned date cannot lock or complete. Finance uses a required actual date and blocks missing due depreciation; locked date errors require cancellation/restart. Department managers may initiate but not cancel. Completion/restoration use linked disposal_stop/disposal_restore events; reversal keeps evidence, blocks later conflicts and requires an eligible replacement if the last responsible employee is no longer active. |
| UAT-022 | Permissions | Every role and explicit department scope passes allowed actions and fails forbidden actions, including direct URL/API attempts, revoked scope and other-department data. |
| UAT-023 | Excel import | Invalid row blocks the whole batch; errors identify row/field/value/reason; retry does not duplicate drafts. |
| UAT-024 | T+ workbook | Workbook matches `11-Tplus-Reconciliation-Export.md`, retains numeric/date types, and reconciles signed reversal effects—including reversal of a negative depreciation adjustment—without mixing cost or impairment changes into accumulated depreciation. Its full registered Decimal ExportLogTotal set equals the workbook/detail totals. |
| UAT-025 | Audit trail | Critical actions contain user/time/object/before-after data. The read-only log page enforces system-admin all, Finance self/financial-object and HR personnel/clearance scopes; crafted object filters and sensitive old/new values cannot bypass redaction. |
| UAT-026 | Backup and restore | A database-plus-attachments recovery point restores successfully within the documented RTO and meets RPO. |
| UAT-027 | LAN/mobile operation | Chinese PC and mobile pages work without public CDN; QR/inventory flows work on approved LAN HTTPS endpoint. |
| UAT-028 | Browser compatibility | Current supported Chrome and Edge complete core PC and mobile flows without blocking layout or script errors. |
| UAT-029 | Capacity smoke | With the Section 3 dataset and concurrency model, every interactive and batch scenario meets its numeric limit without correctness, permission or audit regression. |
| UAT-030 | T+ boundary | No V1 function writes T+ or claims a reconciliation export is posted accounting data. |

## 5. Traceability by Sprint

| Requirement group | Primary Sprint |
|---|---:|
| Project, authentication, audit foundation | 0 |
| Company, department, employee, location, physical category | 1 |
| Configurable coding and permanent code registry | 2 |
| Asset master, attachments, search and finance-confirmation preparation | 3 |
| Finance and depreciation | 4 |
| Fresh registration and opening-data import | 5 |
| QR and A4 labels | 6 |
| Movement and disposal | 7 |
| Inventory | 8 |
| Preventive maintenance | 9 |
| Offboarding | 10 |
| Reports and T+ reconciliation export | 11 |
| Production security, backup, recovery and full UAT | 12 |

## 6. Final sign-off checklist

- All Sprint tasks satisfy `10-Definition-of-Done.md`.
- All migrations apply to a fresh database and upgrade from the prior Sprint.
- Full automated suite passes on PostgreSQL.
- No unresolved P0/P1 defects.
- Backup/restore evidence exists.
- Permission matrix evidence exists.
- Finance signs off depreciation and T+ reconciliation outputs.
- Equipment/department users sign off QR, inventory and maintenance flows.
- HR signs off offboarding clearance.
- System owner records the accepted commit/build and go-live date.
