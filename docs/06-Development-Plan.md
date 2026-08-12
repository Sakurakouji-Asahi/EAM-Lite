# Development Plan V1.1

## 1. 执行方式

开发必须按 Sprint 0 → Sprint 12 的顺序进行。一次 Codex 任务只能执行一个 Sprint。

每个 Sprint 开始前必须：

1. 确认前一 Sprint 已按 `docs/10-Definition-of-Done.md` 验收通过。
2. 阅读 `AGENTS.md`、当前 Sprint 任务文件及任务中列出的业务文档。
3. 运行现有完整测试，确认基线为绿色。
4. 检查工作区和未应用迁移，不覆盖来源不明的已有改动。
5. 如文档、现有实现或迁移发生冲突，先报告并停止，不得自行创造业务口径。

每个 Sprint 完成后必须：

1. 执行当前 Sprint 任务文件中的全部测试和验收场景。
2. 满足 `docs/10-Definition-of-Done.md`。
3. 按 `AGENTS.md` 的 Completion Report 格式汇报证据。
4. 停止并等待人工确认，不得自动开始下一 Sprint。

## 2. Sprint 路线

### Sprint 0 — 项目初始化与审计基础

任务：`tasks/Sprint-0-Project-Initialization.md`

交付：

- 仓库根目录中的 Django 项目骨架
- 自定义用户、登录/退出和基础布局
- 角色组初始化基础
- PostgreSQL 与可选 SQLite 开发配置
- 环境变量、上海时区、CNY、日志
- 本地 Bootstrap/HTMX 静态资源
- AuditLog 与统一审计服务基础
- 版本选择、精确锁定和可重复安装
- PostgreSQL 冒烟测试及自动测试入口

### Sprint 1 — 基础资料与初始化向导前半段

任务：`tasks/Sprint-1-Master-Data.md`

交付：

- Company、Department、Employee、Location、AssetCategory、UserDepartmentScope
- 基础资料管理页面与树形校验
- 初始化向导步骤 1–5、用户/固定角色/部门范围步骤 8
- 部门、人员 Excel 校验/预览/确认导入
- 受保护导入文件、通用 ImportBatch/ImportRow staging 基础
- SystemSetting 固定 registry 的附件技术设置基础（唯一财务阈值 key 留到 Sprint 4）
- 基础资料权限、数据范围和审计日志

### Sprint 2 — 可配置编码引擎

任务：`tasks/Sprint-2-Coding-Engine.md`

交付：

- AssetCodingScheme、AssetCodingSegment、SequenceCounter
- 规则编辑、版本化、预览和十个示例
- 初始化向导编码规则步骤 6 及可验证完成标记
- PostgreSQL 发号所需的方案、计数器和永久占号 schema/算法契约；不在无 Asset 时提交正式发号
- 正式编号永久占用记录的空表基础；端到端发号与并发验收在 Sprint 4
- 不暴露脱离 Asset 的正式发号入口；正式资产发号在 Sprint 4 财务确认事务中接通

### Sprint 3 — 资产主档

任务：`tasks/Sprint-3-Asset-Master.md`

交付：

- Asset、动态字段、业务附件和资产台账
- 草稿创建、编辑、详情、查询和提交财务确认
- V1 全部正式资产单件追踪、quantity 恒为 1 的约束
- 当前责任、部门、树形位置及财务字段隔离
- 不提前完成财务确认或正式发号

### Sprint 4 — 财务确认与折旧引擎

任务：`tasks/Sprint-4-Finance-Depreciation.md`

交付：

- FixedAssetCategory、AssetFinance、DepreciationPolicy/Profile/Event/Schedule、Batch/Item、Entry、WorkUsage、ValueAdjustment、TheoreticalRun
- 六种折旧方法、试算、确认、计提、冲销和调整
- 期初累计折旧、理论/实际口径分离
- 财务确认、正式编号、发号记录和待贴标状态的同一事务
- 财务确认事务内同时创建二维码身份和安全 Token；Sprint 6 再完成打印/贴标
- Decimal、残值底线、最后一期修正和确认历史锁定
- 初始化向导财务规则步骤 7、最终校验步骤 9 及整体完成门槛

### Sprint 5 — 初始化建账与资产 Excel 导入

任务：`tasks/Sprint-5-Initial-Registration.md`

交付：

- 标准初始化模板
- 上传、解析、校验、预览、确认和行级错误
- 导入批次留痕与草稿资产创建
- 旧资产实际期初数承接及理论值只读参考
- 不把旧财务/设备台账直接迁移成正式资产

### Sprint 6 — QR 与贴标

任务：`tasks/Sprint-6-QR-Labels.md`

交付：

- 使用财务确认时已创建的不透明安全 Token，完成 QR 展示、换标和权限校验
- A4 批量标签和本地打印页面
- 打印/贴标状态与确认贴标流程
- 手机扫码资产页面
- 将 pending_label 资产通过确认贴标转为 in_use/idle，为后续生命周期提供可达状态
- 建立 AssetMovement 基础并记录 `label_activation`；其他生命周期类型留给 Sprint 7

### Sprint 7 — 生命周期与处置

任务：`tasks/Sprint-7-Lifecycle-Disposal.md`

交付：

- 领用/归还、部门/责任人/位置变更，以及结构化 AssetLoan 借出/归还
- 扩展既有 AssetMovement 的生命周期类型、完整状态机和编码修正历史
- 终态正式资产归档/恢复显示
- 报废/出售/处置、不可变财务快照、取消及 AssetDisposalReversal
- 以结构化 disposal_stop/disposal_restore 折旧事件保存处置来源、停止前状态和一对一恢复链
- 当前主档更新、历史、审计日志的原子事务

### Sprint 8 — 盘点

任务：`tasks/Sprint-8-Inventory.md`

交付：

- 部门盘点、财务全盘、专项盘点
- 发布时原子生成不可变应盘快照
- 明确的 InventoryTaskAssignee 和 append-only InventoryResolution
- 手机扫码、差异、盘亏和盘盈待确认
- 盘盈附件不伪造 asset_id
- 差异处理、任务关闭和报表

### Sprint 9 — 预防性保养

任务：`tasks/Sprint-9-Preventive-Maintenance.md`

交付：

- 保养计划、周期和到期计算
- 即将到期/逾期查询及首页待办
- 完成记录、MaintenanceProblem 问题跟进、照片和附件
- 日、周、月、年日历周期口径测试；V1 不做运行小时周期
- 扩展既有处置完成/冲销 Service，终止并按来源受控恢复保养计划
- 不扩展为完整维修工单

### Sprint 10 — 员工离职资产清退

任务：`tasks/Sprint-10-Employee-Offboarding.md`

交付：

- 员工进入 leaving 时建立清退任务和资产快照
- 归还、转交、处置复用生命周期服务
- 未解决数量、阻断提示和完成规则
- HR、资产管理和财务职责分离

### Sprint 11 — 报表、Dashboard 与 T+ 对账导出

任务：`tasks/Sprint-11-Reports-Tplus-Export.md`

业务口径：`docs/11-Tplus-Reconciliation-Export.md`

交付：

- 资产、财务、折旧、部门、人员、盘点、保养和处置报表
- 权限化 Dashboard
- system_admin/finance/hr 分域且二次脱敏的只读操作日志查询
- 数值型金额、日期型日期的 Excel 导出
- 与 T+ 人工核对所需的对账导出及导出留痕
- ExportLog 及按 schema 注册、与工作簿勾稽的 Decimal ExportLogTotal
- T+ 资产卡片外部引用的 finance 维护、唯一约束和审计
- 不调用、不写入 T+ API 或数据库

### Sprint 12 — 生产就绪与 UAT

任务：`tasks/Sprint-12-Production-Readiness.md`

验收基线：`docs/12-UAT-Acceptance.md`

交付：

- 权限、审计、安全和依赖复核
- LAN 生产部署说明
- 数据库与附件每日备份、30 天保留和恢复演练
- 管理员手动备份下载的权限与审计
- Chrome/Edge、PC/手机验证
- 5,000 项资产规模的关键页面性能验证
- 全链路 UAT 与上线/暂不上线结论

## 3. 跨 Sprint 不变量

以下规则从首次实现相关能力起持续有效，不得推迟到 Sprint 12 才补救：

- 权限在后端实施，并按 `docs/07-Permissions-and-Workflows.md` 验证角色和数据范围。
- 关键业务变更同步写入审计日志。
- 正式资产、历史编号、已确认折旧、盘点快照和处置快照不可被普通流程物理删除或覆盖。
- 所有金额均使用 Decimal；默认阈值、残值率和起算规则可配置。
- 所有 schema 变化使用迁移；所有新增依赖进入锁定文件。
- Excel 金额保持数值单元格，日期保持日期单元格。
- 手机页面保持响应式；核心静态资源不得依赖公共 CDN。
- 发现缺失业务决定时，记录阻塞并停止，不用技术便利替代业务决定。

## 4. 里程碑建议

- 基础平台：Sprint 0–2
- 可建档和辅助核算：Sprint 3–5
- 完整实物闭环：Sprint 6–10
- 对账、上线和验收：Sprint 11–12

任何里程碑通过都不代表后续 Sprint 已获授权；仍需逐 Sprint 人工确认。
