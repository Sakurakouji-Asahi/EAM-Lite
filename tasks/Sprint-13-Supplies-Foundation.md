# Codex Task — Sprint 13：低值物品基础档案与模块骨架

## 前置

当前仓库已完成 Sprint 0–12，并以当前 `main` 作为功能基线。

开始前完整阅读：

- `AGENTS.md`
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
- 本任务文件

本 Sprint 只建立新模块、基础档案、权限和物品档案导入。不要开始库存过账、领用、保管、盘点、清退或报表。

## 目标

建立可运行的 `apps.supplies` Django app，为后续数量库存提供稳定的分类、仓库和物品主数据。

## 必须实现

### 1. 模块注册

新增并注册：

- `apps.supplies.apps.SuppliesConfig`
- `apps.supplies.urls`
- `apps.supplies.context_processors`
- `/supplies/` 路由
- `templates/supplies/`
- 主导航“低值物品”入口

不得引入 React、Vue、REST framework 或新服务进程。

### 2. 基础档案模型

按技术设计实现：

- `SupplyCategory`
- `SupplyWarehouse`
- `SupplyItem`

要求：

- UUID 主键；
- 显式 `company`；
- 复用现有编码清理和规范化函数；
- 公司内规范化编码唯一；
- 树形分类同公司且无循环；
- 仓库位置和负责人同公司；
- 物品只允许 `consumable` 和 `durable_quantity`；
- 最低库存非负；
- 所有跨表引用执行同公司校验；
- 迁移中有批准的唯一和 CheckConstraint；
- 有历史引用时使用 `PROTECT`。

### 3. 受控修改和停用

实现 service 层：

```python
create_supply_category(...)
update_supply_category(...)
deactivate_supply_category(...)
create_supply_warehouse(...)
update_supply_warehouse(...)
deactivate_supply_warehouse(...)
create_supply_item(...)
update_supply_item(...)
deactivate_supply_item(...)
```

现阶段还没有已过账库存流水，但代码结构必须为后续“发生业务后冻结物品编码和管理模式”预留统一判断函数，不要在 View 中散落判断。

分类、仓库和物品如已被引用，不允许物理删除。页面只提供停用。

### 4. 权限

新增 `apps/supplies/permissions.py`，复用：

- `role_names_for()`；
- `resolve_department_ids()`；
- 现有 `can_* / require_* / scoped_*` 风格。

本 Sprint 权限：

- `system_admin`、`finance`、`warehouse`：查看和维护全部分类、仓库、物品；
- `equipment`：查看全部；可维护 `durable_quantity` 物品，但不得维护易耗品仓库业务；
- `department_manager`、`employee`、`hr`、`management`：只读访问按需求矩阵决定；至少 management 可查看基础档案，普通 employee 不开放管理页面。

权限必须在后端校验。只测试本模块必要权限，不执行全仓库安全复审。

### 5. 页面

至少实现：

- 低值物品首页占位页；
- 分类列表、新增、编辑、停用；
- 仓库列表、新增、编辑、停用；
- 物品列表、新增、编辑、停用；
- 物品筛选：编码/名称、分类、管理模式、启用状态；
- “新增逐件低值耐用品”说明卡片，暂只跳转现有资产新增页，不修改资产流程。

页面使用现有 Bootstrap 和模板结构。列表分页。

### 6. 物品档案 Excel 导入

实现：

- 下载模板；
- 上传 `.xlsx`；
- 解析、校验、预览；
- 行级错误；
- 确认后全有或全无创建物品；
- 批次确认幂等。

可以在本 Sprint 创建通用：

- `SupplyImportBatch`
- `SupplyImportRow`

但只启用 `item_master` 类型。期初库存和期初保管留到后续 Sprint。

导入字段以需求文档为准。复用现有附件/存储或导入模式，不复制一套不必要的文件基础设施。

### 7. 审计

复用 `write_business_audit_log`，记录：

- 基础档案新增、修改、停用；
- 物品导入确认。

不新增审计表，不重新审核整个系统。

### 8. 文档最小修订

按 `PATCH-NOTES.md` 更新现有文档，使以下边界明确：

- 数量型低值物品是批准的 V1.2 有限扩展；
- 通用 ERP/生产物料库存仍排除；
- `Asset.quantity=1` 不变；
- 逐件低值耐用品继续使用 `Asset + controlled_non_fixed`。

不要大规模改写既有 Sprint 0–12 文档。

## 数据库迁移

- 新建 supplies app 的初始迁移。
- 迁移必须支持空库和从当前 `main` 升级。
- 不修改现有资产、财务、盘点或清退表。

## 自动测试

至少覆盖：

1. 分类、仓库、物品模型和约束。
2. 规范化编码重复。
3. 分类循环和跨公司父级。
4. 仓库跨公司位置/负责人。
5. 员工状态校验。
6. 物品管理模式和最低库存。
7. 基础档案权限正向和负向。
8. 直接 URL/POST 的后端权限。
9. 物品导入行级错误、全有或全无和重复确认。
10. 新 app 空库迁移和从当前基线迁移。
11. 现有完整回归测试。

## 本 Sprint 排除

- 库存余额和库存流水；
- 期初/入库过账；
- 领用、退回、调拨、冲销；
- 耐用品保管；
- 盘点和离职清退；
- 低值物品正式报表；
- 修改 `Asset.quantity`；
- 新建逐件低值资产模型；
- 全仓库安全、部署或依赖漏洞复审。

## 验收条件

- `/supplies/` 可进入；
- 三类基础档案可按权限维护；
- 物品档案导入闭环可用；
- 管理模式判定说明清楚；
- 逐件入口复用现有资产；
- 迁移和测试通过；
- 没有库存过账代码被提前实现。

## Completion Report

按项目格式汇报：

### Completed
### Files Changed
### Database Migrations
### Tests
### Business Rules Verified
### Not Implemented
### Risks / Follow-up

完成后停止，不要自行开始 Sprint 14，不要推送远程分支。
