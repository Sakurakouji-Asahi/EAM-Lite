# Requirements Baseline V1.1

## Baseline status

- Status: approved for staged implementation.
- Scope: EAM-Lite business V1.
- Implementation rule: one Sprint at a time; each Sprint requires tests and human acceptance before the next starts.
- This baseline resolves the V1.0 audit findings on coding history, accounting/physical classification, depreciation precision, permissions, workflow, attachments, QR labels, deployment and acceptance.

## Confirmed business facts

- Initial managed asset volume: about 150 items.
- Existing finance ledger and equipment-department ledger are inconsistent and will not be used as authoritative migration sources.
- The company will rebuild a new asset ledger from scratch.
- Management scope includes:
  - Fixed assets
  - Controlled low-value assets
  - Production equipment
  - Computers and office equipment
  - Molds
  - Tools
  - Inspection tools
- Current fixed-asset recognition reference threshold: CNY 5,000.
- Current default residual value rate: 5%.
- Depreciation start default: the month following the asset reaching usable condition.
- Depreciation method must be selected per policy / category / individual asset; system must support all V1 methods, not only straight-line.
- All departments will use the system.
- V1 does not require approval workflow; approval is under separate process design.
- QR code asset labels are required.
- Department users perform routine inventory; Finance participates in major/full inventory.
- V1 is used on company Wi-Fi / LAN only.
- Asset attachments are required, including invoices, contracts, photos, acceptance documents, manuals and disposal evidence.
- T+ integration mode for V1: Excel export and manual accounting/reconciliation only.
- Every in-use asset must have a responsible employee.
- Location structure should support at least: site → workshop/department area → specific position.
- Employee offboarding must check unresolved assigned assets.
- Full repair management is V2.
- Preventive maintenance is V1.
- Disposal retains full historical and financial snapshot.
- QR labels in V1 support A4 printing; dedicated label printers may be supported later.
- Dashboard follows the approved basic financial + physical + pending-work layout.
- Asset coding rules are NOT predetermined; administrators must configure coding rules during initialization.

## Confirmed interpretation rules

- V1 operates for one company. Company ownership is still stored on business data so future expansion cannot mix records accidentally.
- Physical classification and accounting classification are independent. An item may be physically classified as a mold/equipment/tool while Finance separately confirms whether it is a fixed asset.
- CNY 5,000 is a configurable warning threshold, not an automatic accounting conclusion.
- “No approval workflow in V1” means no generic approval engine or multi-level approval routing. Finance confirmation, depreciation-period confirmation, inventory closing and disposal completion remain controlled state transitions with permissions and audit logs.
- Routine maintenance reminders are in-system lists/dashboard warnings. V1 does not send DingTalk, SMS or email notifications.
- V1 maintenance cycles use calendar day/week/month/year. Runtime-hour maintenance is excluded until a meter-reading source is implemented.
- Every individually tracked asset record represents one physical item and has quantity `1`. If identical items need individual responsibility, location, QR or movement, create separate records. Batch quantity and partial transfer are outside V1.
- A formal in-use asset must have one department, one responsible employee and one leaf location. Shared equipment still has one named responsible employee.
- Old ledger data may be consulted only after physical verification. Imported or manually entered opening accumulated depreciation must be explicitly confirmed by Finance and must never be overwritten by theoretical calculation.
- A4 QR labels are required. Dedicated label-printer adaptation remains future scope.
- V1 uses Chinese labels, CNY, Asia/Shanghai business time and two-decimal monetary presentation.

## Delivery gates

- Sprint 0 establishes the project, authentication, permission/audit foundation and PostgreSQL-capable environment.
- Sprint 2 cannot issue an unbound official asset code; official issuance occurs only through a durable issuance record and is atomically bound to an asset during formalization.
- No depreciation Sprint may be accepted without the calculation examples and rounding rules in `08-Depreciation-Calculation-Spec.md`.
- No production deployment may be accepted without backup restoration verification and LAN security requirements in `09-Security-Backup-and-Deployment.md`.
- Full V1 acceptance follows `12-UAT-Acceptance.md`.
