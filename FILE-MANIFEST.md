# EAM-Lite 文件入口

更新：2026-09-14。本清单区分材料用途，不把 AI 设计稿或历史任务当作新授权。文档定位与实际状态见 [docs/README](docs/README.md)。文件增减时维护对应条目，不使用手工固定总数。

## 当前协作与使用入口

- [AGENTS.md](AGENTS.md)：简短协作规则。
- [README-CODEX.md](README-CODEX.md)：开发入口。
- [README.md](README.md)：当前功能、依赖、配置与运行。
- [README-本机使用版.md](README-本机使用版.md)：Windows 使用、更新、备份与迁移。
- [README-LOW-VALUE-GOODS.md](README-LOW-VALUE-GOODS.md)：低值物品模块当前说明。
- [CHANGELOG.md](CHANGELOG.md)：软件变更与未发布记录。
- [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)：第三方许可说明。
- [FILE-MANIFEST.md](FILE-MANIFEST.md)：本清单。

## 运行与维护文件

- `启动EAM-Lite.cmd`、`停止EAM-Lite.cmd`、`更新EAM-Lite.cmd`：Windows 本机稳定版入口。
- `查看EAM-Lite状态.cmd`、`备份EAM-Lite数据.cmd`、`恢复EAM-Lite数据.cmd`：状态和便携数据操作。
- `启动开发环境.cmd`、`启动开发环境-局域网扫码测试.cmd`、`停止开发环境.cmd`：独立开发环境。
- `同步开发版到正式版.cmd`：备份与已提交程序代码同步。
- [scripts/local](scripts/local/)：Windows 运行脚本；[scripts/release](scripts/release/)：发布包构建；[deploy](deploy/)：部署配置。

## AI 设计参考

- [00-Requirements-Baseline.md](docs/00-Requirements-Baseline.md)
- [01-PRD.md](docs/01-PRD.md)
- [02-Business-Rules.md](docs/02-Business-Rules.md)
- [03-Asset-Coding-Rules.md](docs/03-Asset-Coding-Rules.md)
- [04-Database-Design.md](docs/04-Database-Design.md)
- [05-UI-UX.md](docs/05-UI-UX.md)
- [06-Development-Plan.md](docs/06-Development-Plan.md)
- [07-Permissions-and-Workflows.md](docs/07-Permissions-and-Workflows.md)
- [08-Depreciation-Calculation-Spec.md](docs/08-Depreciation-Calculation-Spec.md)
- [09-Security-Backup-and-Deployment.md](docs/09-Security-Backup-and-Deployment.md)
- [11-Tplus-Reconciliation-Export.md](docs/11-Tplus-Reconciliation-Export.md)
- [12-UAT-Acceptance.md](docs/12-UAT-Acceptance.md)
- [13-Low-Value-Goods-Requirements.md](docs/13-Low-Value-Goods-Requirements.md)
- [14-Low-Value-Goods-Technical-Design.md](docs/14-Low-Value-Goods-Technical-Design.md)
- [15-Low-Value-Goods-Data-Dictionary.md](docs/15-Low-Value-Goods-Data-Dictionary.md)
- [16-Low-Value-Goods-UAT.md](docs/16-Low-Value-Goods-UAT.md)

## 操作、状态与历史验证

- [实物类型移除与迁移记录（2026-09-14）](docs/Remove-Physical-Type-2026-09-14.md)
- [项目检查与改进建议（2026-09-12）](docs/Project-Review-2026-09-12.md)
- [六项项目改进、验证与开发更新记录（2026-09-12）](docs/Project-Improvements-2026-09-12.md)
- [实物建档与财务折旧确认分离](docs/Asset-Registration-and-Finance-2026-09-09.md)
- [当前资产编码规则](docs/Asset-Coding-Standard.md)
- [部门与人员初始化记录](docs/Organization-Initialization-2026-09-11.md)
- [三号楼位置与初始化完成记录](docs/Location-Initialization-2026-09-11.md)
- [开发环境业务数据清空记录](docs/Development-Data-Reset-2026-09-10.md)

- [README.md](docs/README.md)
- [Development-Guide.md](docs/Development-Guide.md)
- [10-Definition-of-Done.md](docs/10-Definition-of-Done.md)
- [17-Low-Value-Goods-Operations.md](docs/17-Low-Value-Goods-Operations.md)
- [Sprint-12-Operations-Runbook.md](docs/Sprint-12-Operations-Runbook.md)
- [Sprint-1-Acceptance-Evidence.md](docs/Sprint-1-Acceptance-Evidence.md)
- [Sprint-12-UAT-Evidence.md](docs/Sprint-12-UAT-Evidence.md)
- [18-Low-Value-Goods-UAT-Evidence.md](docs/18-Low-Value-Goods-UAT-Evidence.md)
- [Usability-Repair-2026-09-07.md](docs/Usability-Repair-2026-09-07.md)
- [Documentation-Refresh-2026-09-09.md](docs/Documentation-Refresh-2026-09-09.md)
- [项目文档时效性检测报告-2026-09-08.md](项目文档时效性检测报告-2026-09-08.md)
- [项目需求与易用性检测报告-2026-09-06.md](项目需求与易用性检测报告-2026-09-06.md)

## 历史 Sprint 任务

原路径保留用于追溯，当前用户明确指定时再核对后续变化。

- [Sprint-0-Project-Initialization.md](tasks/Sprint-0-Project-Initialization.md)
- [Sprint-1-Master-Data.md](tasks/Sprint-1-Master-Data.md)
- [Sprint-2-Coding-Engine.md](tasks/Sprint-2-Coding-Engine.md)
- [Sprint-3-Asset-Master.md](tasks/Sprint-3-Asset-Master.md)
- [Sprint-4-Finance-Depreciation.md](tasks/Sprint-4-Finance-Depreciation.md)
- [Sprint-5-Initial-Registration.md](tasks/Sprint-5-Initial-Registration.md)
- [Sprint-6-QR-Labels.md](tasks/Sprint-6-QR-Labels.md)
- [Sprint-7-Lifecycle-Disposal.md](tasks/Sprint-7-Lifecycle-Disposal.md)
- [Sprint-8-Inventory.md](tasks/Sprint-8-Inventory.md)
- [Sprint-9-Preventive-Maintenance.md](tasks/Sprint-9-Preventive-Maintenance.md)
- [Sprint-10-Employee-Offboarding.md](tasks/Sprint-10-Employee-Offboarding.md)
- [Sprint-11-Reports-Tplus-Export.md](tasks/Sprint-11-Reports-Tplus-Export.md)
- [Sprint-12-Production-Readiness.md](tasks/Sprint-12-Production-Readiness.md)
- [Sprint-13-Supplies-Foundation.md](tasks/Sprint-13-Supplies-Foundation.md)
- [Sprint-14-Supply-Stock-Ledger.md](tasks/Sprint-14-Supply-Stock-Ledger.md)
- [Sprint-15-Supply-Issue-Return-Transfer.md](tasks/Sprint-15-Supply-Issue-Return-Transfer.md)
- [Sprint-16-Low-Value-Durables.md](tasks/Sprint-16-Low-Value-Durables.md)
- [Sprint-17-Supply-Inventory-Offboarding.md](tasks/Sprint-17-Supply-Inventory-Offboarding.md)
- [Sprint-18-Supply-Reports-UAT.md](tasks/Sprint-18-Supply-Reports-UAT.md)

## 历史入口与原文归档

- [CODEX-FIRST-INSTRUCTION.md](CODEX-FIRST-INSTRUCTION.md)
- [PATCH-NOTES.md](PATCH-NOTES.md)
- [docs/archive/AGENTS-before-2026-09-09.md](docs/archive/AGENTS-before-2026-09-09.md)
- [docs/archive/README-CODEX-before-2026-09-09.md](docs/archive/README-CODEX-before-2026-09-09.md)
- [docs/archive/Sprint-13-First-Instruction.md](docs/archive/Sprint-13-First-Instruction.md)

- [原 Sprint 完成清单](docs/archive/Sprint-Definition-of-Done-Reference.md)

## 第三方资源资料

- [本地前端资源版本、来源及校验值](static/vendor/README.md)
