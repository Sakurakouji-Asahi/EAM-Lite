# 交给 Codex 的首条执行指令

请在仓库 `Sakurakouji-Asahi/EAM-Lite` 当前 `main` 最新提交基础上，实施 **Sprint 13：低值物品基础档案与模块骨架**。

## 必须先做

1. 拉取并确认当前 `main`，记录起始 commit SHA。
2. 新建分支：`codex/sprint13-supplies-foundation`。
3. 完整阅读：
   - `AGENTS.md`
   - `README-CODEX.md`
   - `docs/00-Requirements-Baseline.md`
   - `docs/01-PRD.md`
   - `docs/02-Business-Rules.md`
   - `docs/04-Database-Design.md`
   - `docs/05-UI-UX.md`
   - `docs/07-Permissions-and-Workflows.md`
   - `docs/10-Definition-of-Done.md`
   - `docs/13-Low-Value-Goods-Requirements.md`
   - `docs/14-Low-Value-Goods-Technical-Design.md`
   - `docs/15-Low-Value-Goods-Data-Dictionary.md`
   - `tasks/Sprint-13-Supplies-Foundation.md`
   - `PATCH-NOTES.md`
4. 检查当前代码中 Company、Department、Employee、Location、角色、权限、审计、导入和模板导航的实际写法，按现有项目风格实现，不要另起一套基础设施。

## 本次只实现 Sprint 13

必须完成：

- 新建并注册 `apps.supplies`；
- `SupplyCategory`、`SupplyWarehouse`、`SupplyItem`；
- 模型约束、company 边界、规范化编码；
- service 层新增/修改/停用；
- 后端权限与对象范围；
- 分类、仓库、物品页面和分页筛选；
- 物品档案 `.xlsx` 模板、校验、预览、全有或全无确认、幂等；
- “逐件低值耐用品”入口复用现有 Asset；
- 最小文档修订；
- 迁移和测试。

## 关键架构边界

1. **不得修改或放宽 `Asset.quantity = 1`。**
2. **不得新建与 `Asset` 重复的逐件低值资产模型。**
3. 逐件低值耐用品继续使用 `Asset + AssetFinance(accounting_treatment="controlled_non_fixed")`。
4. 数量型低值易耗品和数量型低值耐用品放入新 `apps.supplies`。
5. 本次不实现库存余额、库存流水、入库过账、领用、退回、调拨、保管、盘点、清退或报表。
6. 不引入采购、供应商、发票、会计凭证、T+ API、BOM、生产物料、通用审批或自动摊销。
7. 不引入微服务、React、Vue、DRF、Redis 或 Celery。

## 不要做大范围安全审核

不要重新执行 Sprint 12 的全仓库安全、部署、备份、依赖漏洞或附件审核。只保证本 Sprint 直接相关的：

- company 隔离；
- 后端角色权限；
- 导入确认幂等；
- 模型约束；
- 必要 AuditLog；
- 现有回归测试。

发现与 Sprint 13 无关的问题时，不要扩展修复范围；在 Completion Report 的 Follow-up 中简要记录即可。发现会直接阻止 Sprint 13 正确实现的问题时，只修复最小必要范围并说明。

## 实施方式

- 业务状态和关键规则放 service，不在 View 中直接写模型状态。
- 使用现有 `write_business_audit_log`。
- 复用现有 `normalize_identifier` 和组织权限工具。
- 页面使用 Django Templates、Bootstrap 和项目现有 HTMX 模式。
- 所有列表分页。
- Excel 逻辑不得写进 View。
- 不允许仅靠前端限制权限或字段。
- 代码、迁移、页面和测试同时完成，不能只写文档或模型草稿。

## 验证

至少执行：

- Sprint 13 新增测试；
- supplies、masterdata、accounts、imports 相关回归；
- 空库迁移；
- 从当前 main 迁移；
- 现有完整测试套件，若环境确有阻碍，明确列出未执行项和原因，不得声称已通过。

## 提交

完成并通过验证后：

1. 更新必要文档和 CHANGELOG，但不要提前实现 Sprint 14。
2. 独立提交，建议提交信息：
   `feat: add Sprint 13 supplies foundation`
3. 不推送远程。
4. 工作树保持干净。

## 最终汇报格式

### Starting Point
- branch
- starting SHA

### Completed

### Files Changed

### Database Migrations

### Tests
- 命令
- 通过/失败/跳过数量

### Business Rules Verified

### Not Implemented
- 明确列出全部 Sprint 14+ 功能

### Follow-up

### Commit
- commit SHA
- working tree status

完成 Sprint 13 后立即停止，等待下一条指令。
