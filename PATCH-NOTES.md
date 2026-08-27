# 合并到现有 EAM-Lite 文档的最小修订说明

## 1. 目的

现有 V1.1 文档把“通用库存”和“批量数量资产”排除在范围外。V1.2 低值物品扩展需要做有限例外，但不得改写现有逐件资产规则。

Codex 应采用小范围补充，而不是重写 Sprint 0–12 文档。

## 2. `AGENTS.md`

### 建议在系统职责中补充

```markdown
- 管理公司内部低值易耗品及不需要逐件编号的数量型低值耐用品；
- 数量型低值物品由独立 `apps.supplies` 管理，不属于 `Asset`；
- 需要一物一码的低值耐用品继续作为 `Asset`，会计认定为 `controlled_non_fixed`。
```

### 建议在排除项中澄清

把“通用库存/物料库存不在范围”解释为：

```markdown
仍不建设生产原料、半成品、产成品、BOM、采购和 ERP 通用库存。
V1.2 批准的 `apps.supplies` 仅管理公司内部低值易耗品和数量型低值耐用品。
```

### 必须保留

```markdown
每个 Asset 仍代表一件实物，quantity 固定为 1；不得用 Asset 管理批量数量。
```

## 3. `docs/00-Requirements-Baseline.md`

新增 V1.2 补充段：

```markdown
V1.2 增加有限低值物品管理：
- 逐件低值耐用品继续使用 Asset + controlled_non_fixed；
- 数量型低值易耗品和数量型低值耐用品使用独立 supplies 模块；
- 不改变 Asset 单件追踪、固定资产核算和折旧规则；
- 不扩展至生产物料、采购、会计凭证或通用 ERP 库存。
```

不要删除原“批量数量不属于 Asset V1.1”的文字；追加说明新模块是独立数量域。

## 4. `docs/01-PRD.md`

在系统边界后增加“V1.2 低值物品扩展”小节，引用：

- `docs/13-Low-Value-Goods-Requirements.md`
- `docs/14-Low-Value-Goods-Technical-Design.md`
- `docs/15-Low-Value-Goods-Data-Dictionary.md`
- `docs/16-Low-Value-Goods-UAT.md`

明确：

- `Asset.quantity=1` 不变；
- `controlled_non_fixed` 是逐件受控非固定资产；
- 数量库存不进入 `Asset`；
- 财务正式凭证仍在 T+ 人工处理。

## 5. `docs/02-Business-Rules.md`

新增交叉引用，不复制全部成本规则：

```markdown
低值物品数量、移动加权平均成本、退回原成本、调拨和不可变流水规则见 docs/13–15。
```

## 6. `docs/04-Database-Design.md`

增加 `apps.supplies` 的模型总览和关系图引用。详细字段仍以 `docs/14`、`docs/15` 为准，避免在三处维护完整字段表。

## 7. `docs/05-UI-UX.md`

增加：

- 低值物品导航；
- 数量库存和保管页面；
- 逐件低值耐用品跳转现有资产；
- 移动端本人保管和盘点录入。

## 8. `docs/07-Permissions-and-Workflows.md`

增加 supplies 权限矩阵，使用现有八个角色，不新增角色，不增加通用审批。

## 9. `docs/10-Definition-of-Done.md`

增加 supplies 完成条件：

- 余额可由流水重建；
- 无负库存；
- 过账幂等；
- controlled_non_fixed 无折旧；
- 离职清退覆盖开放耐用品保管；
- docs/16 UAT 通过。

## 10. `README.md` / `README-CODEX.md`

只增加模块说明、Sprint 13–18 执行顺序和新文档索引。

## 11. `CHANGELOG.md` / `VERSION`

- Sprint 13–17 可在 Unreleased 下累计。
- Sprint 18 完成并验收后再按项目版本策略提升版本。
- Codex 不应在 Sprint 13 直接宣称整个低值物品模块完成。

## 12. 不应修改

除非实现中出现直接冲突，不要改写：

- 现有资产状态机；
- 资产正式编号规则；
- 固定资产折旧计算；
- 资产二维码规则；
- 现有资产盘点模型；
- 现有部署、备份和恢复方案；
- Sprint 0–12 已完成的任务历史。
