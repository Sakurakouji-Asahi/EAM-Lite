# EAM-Lite Codex Starter Pack V1.1

本包是 EAM-Lite V1 的开发需求基线和逐 Sprint 执行任务。它只包含文档，不包含现成应用代码。

## 1. 使用方式

1. 将本目录中的全部内容放到一个新的 Git 仓库根目录；`AGENTS.md` 必须位于仓库根目录，不能只留在压缩包或下级目录。
2. 第一次只向 Codex 下达 `tasks/Sprint-0-Project-Initialization.md`。
3. 每次只执行一个 Sprint；完成、测试、人工复核后，才能明确下达下一个 Sprint。
4. Codex 不得自行连续执行多个 Sprint，也不得把提纲理解为一次性开发授权。
5. 出现文档冲突、未决业务规则或可能破坏历史数据的情况时，停止实现并列出冲突。

推荐首条指令：

> 完整阅读仓库根目录 `AGENTS.md` 以及 Sprint 0 指定文档，仅执行 `tasks/Sprint-0-Project-Initialization.md`。不要开始 Sprint 1。完成后按 Completion Report 汇报并等待人工确认。

## 2. 必读顺序

1. `AGENTS.md`
2. `docs/00-Requirements-Baseline.md`
3. `docs/01-PRD.md`
4. `docs/02-Business-Rules.md`
5. `docs/03-Asset-Coding-Rules.md`
6. `docs/04-Database-Design.md`
7. `docs/05-UI-UX.md`
8. `docs/06-Development-Plan.md`
9. `docs/07-Permissions-and-Workflows.md`
10. `docs/08-Depreciation-Calculation-Spec.md`
11. `docs/09-Security-Backup-and-Deployment.md`
12. `docs/10-Definition-of-Done.md`
13. `docs/11-Tplus-Reconciliation-Export.md`
14. `docs/12-UAT-Acceptance.md`
15. 当前 Sprint 对应的 `tasks/` 文件

## 3. 固定项目边界

- 公司内部中文 Web 应用，V1 仅公司 Wi-Fi / LAN 使用。
- 初始约 150 项资产，设计容量至少 5,000 项资产、100 个用户。
- V1 按单公司运行，但所有业务数据必须带公司边界，为未来多公司扩展留出安全空间。
- 用友 T+ 仍是正式会计系统；EAM-Lite 是资产实物主数据和固定资产辅助核算系统。
- V1 只导出 Excel 供 T+ 对账和人工记账，不直接写入 T+。
- 财务和设备部旧台账不作为权威迁移源；通过重新盘点、重新建档建立新台账。
- 旧台账只用于历史查证和补充已核实的财务期初数据。
- V1 不包含审批工作流引擎、钉钉、完整维修、采购、MES、RFID、外网公开访问和税务折旧。

## 4. 版本与变更

- 需求基线版本：V1.1。
- 关键业务决定不得由 Codex自行改写。
- 需求变更应先更新相应 `docs/`，再修改任务和实现。
- 已确认的历史编码、折旧、盘点、处置和审计记录不得因需求变更被覆盖。
