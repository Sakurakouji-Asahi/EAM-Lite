# AGENTS.md

# EAM-Lite V1.1 Development Instructions

## 1. Project Purpose

EAM-Lite is an internal enterprise asset management system.

Primary responsibilities:
- Asset master data
- Asset ownership and location
- Asset lifecycle history
- Configurable asset coding
- Fixed asset accounting support
- Multiple depreciation methods
- QR code asset identification
- Asset inventory
- Preventive maintenance
- Employee asset clearance
- Reporting
- Audit trail

EAM-Lite is NOT:
- An ERP replacement
- A general inventory system
- A MES system
- A purchasing system
- A general accounting ledger

The official accounting system remains external. V1 only exports Excel data for reconciliation with T+.

V1 is a staged single-company implementation. Keep an explicit company boundary on business data and reject cross-company references so future expansion cannot mix records.

## 2. Required Documentation

Before implementing any business feature, read the relevant documentation under `docs/`.

At minimum read:
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/04-Database-Design.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/10-Definition-of-Done.md`
- the relevant module documentation
- this `AGENTS.md`

When documentation and existing implementation conflict:
1. Do not silently choose one.
2. Preserve existing data.
3. Report the conflict.
4. Prefer the latest explicitly approved business requirement.
5. Do not invent a new business rule merely to simplify implementation.

The numbered `docs/` files are the approved business and technical baseline. A `tasks/` file authorizes only its named Sprint. Never continue into the next Sprint without an explicit new user instruction.

### 2.1 Repository Placement

This file must remain at the Git repository root. If the starter pack was extracted into a nested directory, move its contents to the repository root before implementation.

Do not implement inside an uploaded archive directory. Initialize or use a Git repository, inspect existing changes, and preserve user work.

## 3. Core Business Principles

### 3.0 Physical and Accounting Classification Are Separate

`AssetCategory` is a physical/management classification such as equipment, mold, tool, inspection tool, office equipment, or other.

Fixed-asset recognition and fixed-asset accounting category belong to Finance data. Never infer one directly from the other.

Every formal V1 asset is individually tracked and has quantity 1. Do not implement partial-quantity transfer, inventory or disposal in V1.

### 3.1 Asset Codes Are Configurable

Never hard-code a single asset coding format.

The system must support configurable coding schemes including:
- Fixed text
- Company code
- Asset category code
- Department code
- Year
- Year-month
- Date
- Sequence
- Custom fixed-text segments (`custom_text`); V1 does not expose a `custom_field` value resolver

Asset codes must be generated through the coding engine.

### 3.2 Sequence Generation Must Be Concurrency Safe

Never generate asset numbers using `MAX(asset_code) + 1`.

Use:
- Database transaction
- Row locking
- Atomic sequence counter update
- Unique database constraint

Concurrent requests must not generate duplicate codes. Used official asset codes must never be reused.

Official issuance requires a durable code registry/history row. A unique constraint on the current `Asset.asset_code` alone is insufficient because corrected, voided and replaced codes remain permanently occupied.

The first official number, counter scope, zero padding, version resolution and transaction boundary are defined in `docs/03-Asset-Coding-Rules.md`; do not reinterpret them.

## 4. Financial Calculation Rules

### 4.1 Never Use Float for Money

Financial fields must use Decimal.

Do not use binary floating point for:
- Original cost
- Depreciation
- Salvage value
- Book value
- Disposal income
- Impairment
- Adjustments

### 4.2 Fixed Asset Threshold Is Configurable

Current reference threshold: 5,000 CNY.

This is a warning threshold only. It must not automatically classify an asset as a fixed asset.

Final classification must be explicitly confirmed by an authorized finance user.

Do not hard-code 5,000 in business logic.

## 5. Depreciation Rules

Depreciation must be a configurable engine.

Supported V1 methods:
1. Straight-line
2. Units of production
3. Double declining balance
4. Sum of years digits
5. Manual depreciation
6. No depreciation

Configuration priority:
Single asset configuration > Asset category default > System default.

Support:
- Salvage percentage
- Fixed salvage amount
- Current month start
- Next month start
- Specified month start
- Specified date start
- Monthly / yearly period
- Suspend / resume / stop
- Opening accumulated depreciation
- Adjustments with audit trail

Confirmed depreciation history must never be silently recalculated or overwritten.

All formulas, period conventions, two-decimal `ROUND_HALF_UP` behavior, final-period correction, change treatment, reversals and examples are defined in `docs/08-Depreciation-Calculation-Spec.md`.

Do not implement a depreciation method from its name alone. A Sprint involving depreciation is incomplete until results match the normative examples.

## 6. Asset History

Formal asset records must never be physically deleted during normal operation.

Preserve history for:
- Department transfer
- Responsible employee change
- Location change
- Loan / return
- Disposal / sale
- Depreciation adjustment
- Asset value adjustment
- Coding correction
- Inventory result
- Maintenance completion

## 7. Asset Responsibility

An asset may exist as a draft without a responsible employee.

An active/in-use asset must have:
- Department
- Responsible employee
- Location

Large shared machines still require one responsible employee.

The saved location is one leaf `Location` foreign key. A three-level UI is a hierarchical selector, not three duplicated asset columns.

## 8. Location Structure

Locations are hierarchical. Do not hard-code exactly three database columns.

Current business convention:
Site → Workshop/Department Area → Specific Position

Future nesting must remain possible.

## 9. Permissions

Permissions must be enforced in the backend.

Finance-only fields include, at minimum:
- Original cost
- Capitalization information
- Depreciation configuration
- Accumulated depreciation
- Book value
- Financial adjustments
- Disposal financial snapshot

Hiding UI buttons is not sufficient.

Use the role, action, field and department-scope matrix in `docs/07-Permissions-and-Workflows.md`. Default to deny. Every object query must apply company scope and, where required, department scope.

`system_admin` is an application role. A Django superuser may recover the system but does not replace normal role tests.

## 10. Audit Logs

Critical business changes require audit logs.

At minimum record:
- User
- Timestamp
- Object type
- Object id
- Action
- Old values
- New values

Do not log plaintext passwords or secrets.

Audit infrastructure begins in Sprint 0 and each Sprint must connect its own critical actions. Do not defer operational audit logging until production hardening.

Audit rows are append-only for application users. Business state changes and audit records must commit in the same database transaction where practical.

## 11. QR Codes

QR codes must not embed sensitive asset details.

Prefer:
- Random token
- Secure application URL

Scanning a code must still pass backend permission checks.

Use a dedicated QR/label model with a unique random token, active/revoked state, issuance, print and label-confirmation timestamps. Do not expose sequential primary keys as QR credentials.

## 12. Excel Imports

Asset imports must not directly create finalized production assets.

Default import result: Draft asset.

Import flow:
1. Upload
2. Parse
3. Validate
4. Preview
5. Show row-level errors
6. Confirm
7. Create draft records

Use an import batch/staging model. Default behavior is all-or-nothing after confirmation: any invalid row prevents creation of all business drafts. Retrying a confirmed batch must be idempotent.

Reject macro-enabled workbooks unless a future approved requirement authorizes them. Never execute formulas or external links.

Errors must identify row, field, invalid value, and reason.

## 13. Excel Exports

Financial values must remain numeric Excel cells.
Dates must remain date cells.
Do not export all values as text.

Protect against spreadsheet formula injection for user-entered text beginning with `=`, `+`, `-`, or `@`. Record export type, filters, user, time and row count in the audit trail.

T+ reconciliation exports must follow `docs/11-Tplus-Reconciliation-Export.md`.

## 14. Database Migrations

All schema changes must use tracked migrations.

Do not manually alter the production schema as part of feature implementation.

For referenced master data and history, explicitly choose `PROTECT` or justified `SET_NULL`; do not accept implicit destructive cascade behavior. Add the named uniqueness and check constraints defined in `docs/04-Database-Design.md`.

## 15. Testing Requirements

Business-critical logic requires automated tests.

At minimum test:
- Permissions
- Coding uniqueness
- Sequence resets
- Concurrent code generation
- Financial calculations
- Depreciation methods
- Asset state transitions
- Asset movement history
- Inventory snapshot behavior
- Maintenance due-date calculation
- Disposal preservation
- Employee asset clearance

Never delete failing tests merely to make the suite pass.

Tests must include negative permission cases, cross-company/cross-department access rejection, fresh-database migration, migration from the previous Sprint, and transaction rollback for critical multi-row operations.

PostgreSQL-specific locking and concurrency behavior must be tested on PostgreSQL. SQLite results cannot close a concurrency acceptance item.

## 16. Financial Test Precision

Use exact Decimal assertions.

Test:
- Rounding
- Final period correction
- Salvage floor
- Opening accumulated depreciation
- Remaining useful life
- Adjustments

## 17. Security

Do not:
- Store passwords in plaintext
- Commit secrets
- Commit database credentials
- Commit private keys
- Expose uploaded files through unrestricted public URLs

Use environment variables for secrets and environment-specific settings.

Follow `docs/09-Security-Backup-and-Deployment.md`. LAN-only does not mean trusted-by-default: production uses authenticated access, CSRF protection, secure attachment delivery and HTTPS where browser camera or credentials are used.

Bootstrap, HTMX, icons, QR scripts and other runtime UI assets must be served locally in V1; the application must remain usable when the public internet is unavailable.

## 18. Scope Control

Only implement the requested Sprint.

V1 excludes:
- DingTalk approval
- DingTalk notification
- T+ API posting
- Public internet deployment
- Full repair management
- Tax depreciation
- RFID
- MES
- Purchase management
- General material inventory
- Runtime-hour maintenance
- Partial-quantity asset tracking

Do not expand the project into a full ERP.

## 19. UI Principles

Prioritize:
- Chinese labels
- Clear field grouping
- Short workflows
- Responsive mobile inventory pages
- Explicit validation messages
- Safe confirmation for destructive operations

## 20. Preferred Stack

Preferred:
- Python
- Django
- PostgreSQL
- Bootstrap
- HTMX
- Django templates
- pytest + pytest-django

Avoid unnecessary frontend architecture.
Do not introduce React/Vue unless approved.
Do not introduce microservices.

At Sprint 0, choose mutually compatible maintained versions of Python, Django, PostgreSQL driver, PostgreSQL and the selected test framework; record and pin them in project dependency/configuration files. Do not leave production dependencies as unbounded version ranges.

Business defaults:
- Language: Simplified Chinese
- Time zone: `Asia/Shanghai`
- Currency: CNY
- Money display/calculation precision: 2 decimals after business rounding

## 21. Code Quality

Before declaring a task complete:
1. Run relevant automated tests.
2. Run the existing full test suite when practical.
3. Check migrations.
4. Check permissions.
5. Check validation.
6. Check formatting/linting if configured.
7. Report changed files.
8. Report migrations.
9. Report test results.
10. Report remaining known limitations.

The universal completion gate is `docs/10-Definition-of-Done.md`. A task is not complete merely because its happy-path page loads.

## 22. Dependency Policy

Before adding a third-party dependency:
1. Explain its use.
2. Prefer stable and maintained libraries.
3. Avoid large packages for trivial functionality.
4. Add it to requirements/lock file.
5. Add tests where relevant.

## 23. Backward Compatibility

Once production asset data exists:
- Prefer additive schema changes.
- Preserve existing IDs.
- Preserve historical financial data.
- Preserve historical asset codes.
- Preserve audit records.

## 24. Completion Report Format

At the end of each Codex task, report:

### Completed
### Files Changed
### Database Migrations
### Tests
### Business Rules Verified
### Not Implemented
### Risks / Follow-up

## 25. Authoritative Data Sources

- Physical identity, current department, responsible employee, location and operational status: `Asset` plus immutable movement/history records.
- Accounting treatment, fixed-asset category, original cost and capitalization date: `AssetFinance`.
- Reaching-usable-condition date: the single `Asset.commissioning_date`, confirmed by Finance before formalization.
- Impairment: confirmed `AssetValueAdjustment` entries; any balance on AssetFinance is a read-only rebuildable cache.
- Effective depreciation configuration: versioned `AssetDepreciationProfile`.
- Accumulated depreciation and book value: the algebraic sum of all posted original, opening, reversal and depreciation-adjustment entries; never delete or silently exclude the original side of a reversal pair.
- Current labels and QR status: active `AssetQrIdentity` plus its `AssetLabelPrintBatch`/`AssetLabelPrintItem` history.
- T+ remains the official accounting ledger; EAM-Lite financial outputs are reconciliation support.

Do not maintain independently editable duplicate totals. Cached values, if introduced for performance, must be derived transactionally and covered by reconciliation tests.

## 26. Transaction and Idempotency Rules

Use one database transaction for critical operations including:
- Asset formalization plus official code issuance
- Movement plus current asset responsibility/location update
- Depreciation batch confirmation and entry creation
- Reversal plus reversal entry creation
- Inventory task creation plus snapshot rows
- Inventory close plus final result calculation
- Disposal completion plus financial snapshot and asset terminal status
- Import confirmation plus draft creation

User retries must not create duplicate codes, depreciation entries, movements, scans counted twice, disposals or imported drafts. Use database constraints and idempotency keys/batch state, not UI button disabling alone.

## 27. Workflow and State Machines

Implement state changes through service functions described in `docs/07-Permissions-and-Workflows.md`. Do not expose unrestricted model status editing through Django Admin or generic CRUD forms.

V1 has no general approval engine. Named confirmations and closings are controlled business actions, not configurable multi-level approval workflows.

## 28. Attachments

Store file metadata separately from business-object links so an attachment can belong to an asset, maintenance record, inventory surplus, disposal record or other approved object without requiring a nonexistent asset.

Use Django storage-managed filenames, random stored names, type/size validation and permission-checked download views. Never trust the client MIME type or original filename as a storage path.

## 29. Production and Recovery

Do not declare production readiness until:
- PostgreSQL is used
- HTTPS/LAN host configuration is documented and tested
- Static assets work without public CDN access
- Database and media backups run automatically
- Retention is enforced
- A restore drill has succeeded
- Recovery steps and responsible role are documented

The detailed gate is `docs/09-Security-Backup-and-Deployment.md`; final UAT is `docs/12-UAT-Acceptance.md`.
