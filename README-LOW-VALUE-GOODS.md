# EAM-Lite 低值易耗品与低值耐用品扩展包

本扩展包面向 EAM-Lite `main` 分支当前功能基线，供 Codex 按 Sprint 逐步实现。

## 核心架构结论

本扩展采用两条管理路径，避免破坏现有资产模型：

1. **逐件管理的低值耐用品**
   - 继续使用现有 `Asset`、`AssetFinance`、二维码、调拨、盘点、处置和离职清退。
   - 财务认定使用 `AssetFinance.accounting_treatment = controlled_non_fixed`。
   - 每条资产仍严格代表一件实物，`quantity = 1`。
   - 不生成折旧配置，不参加固定资产折旧。

2. **按数量管理的低值物品**
   - 新建独立 `apps.supplies` 应用。
   - 管理低值易耗品以及不需要一物一码的低值耐用品。
   - 支持入库、领用、退回、仓库调拨、耐用品保管、盘点、离职清退、库存与领用报表。
   - 使用不可变库存流水和移动加权平均成本，不建立采购、供应商、发票或会计凭证模块。

## 文件说明

- `docs/13-Low-Value-Goods-Requirements.md`：业务需求和范围基线。
- `docs/14-Low-Value-Goods-Technical-Design.md`：架构、模型、服务、成本、权限和迁移设计。
- `docs/15-Low-Value-Goods-Data-Dictionary.md`：字段、状态机和约束明细。
- `docs/16-Low-Value-Goods-UAT.md`：功能验收场景。
- `tasks/Sprint-13-...` 至 `tasks/Sprint-18-...`：逐 Sprint Codex 任务。
- `CODEX-FIRST-INSTRUCTION.md`：可直接复制给 Codex 的首条执行指令。
- `PATCH-NOTES.md`：合并到现有仓库文档时需要修改的最小位置。

## 使用顺序

1. 将本包内 `docs/`、`tasks/` 和根目录补充文件复制到 EAM-Lite 仓库根目录。
2. 先审阅业务口径，尤其是“逐件低值耐用品”和“数量型低值耐用品”的区分。
3. 把 `CODEX-FIRST-INSTRUCTION.md` 中的指令交给 Codex。
4. 每次只执行一个 Sprint；Sprint 13 已建立基础档案，Sprint 14 已建立库存入库引擎，Sprint 15 已建立领退调拨、完整冲销与耐用品保管基础，Sprint 16 已完成耐用品保管生命周期和逐件资产集成。
5. 后续仍须逐 Sprint 单独下达任务；不得从 Sprint 16 自动进入 Sprint 17。

## Sprint 14 已开放能力

- 期初入库与日常入库草稿、编辑、取消和原子过账；
- 公司内按年度、单据类型并发安全编号：`QC-YYYY-000001` / `RK-YYYY-000001`；
- 移动加权平均成本、只读库存余额和不可变库存流水；
- “导入中心 → 低值物品期初库存”标准 `.xlsx` 模板、预览和整批确认；确认只按仓库生成期初草稿，不自动过账；
- 只读余额核对命令：

~~~powershell
.\.venv\Scripts\python.exe manage.py reconcile_supply_balances --company <公司编码>
~~~

核对命令发现差异时以非 0 状态退出，只报告流水汇总与余额缓存差异，不会修复、重建或写入库存。

## Sprint 15 已开放能力

- 领用出库：部分领用按当前移动平均成本，全部领用取尽剩余金额；
- 低值易耗品从原领用明细发起退回，累计有效退回不超原领用，成本沿用原领用快照；
- 仓库调拨：同一行形成来源 `transfer_out` 与目标 `transfer_in` 两条等额流水；
- 完整冲销：仅在对应仓库/物品没有后续流水且余额精确匹配原快照时生成 `CX` 冲销单；
- 数量型低值耐用品领用时，在同一事务建立开放保管记录和不可变 `issue` 保管流水；
- 部门经理按授权部门、员工按本人查看领用/退回与保管；无成本权限的页面不输出金额；
- 只读保管核对命令：

~~~powershell
.\.venv\Scripts\python.exe manage.py reconcile_supply_custodies --company <公司编码>
~~~

该命令根据保管流水重算数量、金额与状态；发现差异时以非 0 状态退出，不会自动修复。

## Sprint 16 已开放能力

- 数量型耐用品从开放保管发起部分或全部归还，并继续使用 `return` 库存单据过账；
- 归还过账原子减少保管、增加仓库库存，按准确保管金额参与移动平均，并支持满足最新流水条件的完整冲销；
- 责任转交每次新建独立子保管，以 `parent_custody` 保留来源链，不合并成本批次；
- 报损和报废减少在管数量与管理金额，不改变仓库库存、不生成会计凭证；
- “耐用品期初保管”标准 `.xlsx` 模板、逐行校验和整批确认；确认直接建立根保管和 `opening` 流水，确认后不提供破坏性回滚；
- 保管列表、来源类型筛选、详情来源链、完整流水及员工只读“我的耐用品”；
- 逐件低值耐用品继续使用 `Asset + AssetFinance(controlled_non_fixed)`，资产列表可按会计认定筛选；
- `controlled_non_fixed` 从折旧试算、Profile 动作、批次、理论试算及固定资产折旧查询中统一排除；
- 服务级 `durable_management_totals()` 分开返回数量型仓库/在管金额与逐件受控非固定资产原值。

Sprint 16 仍未开放仓库或保管盘点、盘点调整、数量型耐用品离职清退、综合报表或正式 Dashboard 指标。

## 明确不做

本扩展不是通用 ERP 库存模块，不管理生产原料、BOM、批次保质期、采购订单、供应商结算、进项发票、自动会计凭证、T+ API 或自动摊销。
