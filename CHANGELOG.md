# EAM-Lite Changelog

## Unreleased

- 逐件资产增加选填的设备编号，支持草稿填写和正式资产受控补录、更正；详情和权限范围内的台账搜索可查看，历史变更保留审计。
- 新资产二维码只编码随机标识，增加登录后的系统内相机/照片扫码入口，并让盘点入口识别同一标识；历史 URL 标签保留兼容。
- 简化位置档案录入和查看：不再要求选择位置类型，初始化按可供资产选择的末级位置判断；保留历史类型记录。
- 部门、位置、实物分类及数量物品分类的下级自动编号加入上级编码和两位下级序号；历史编号与手工填写保持不变。
- 菜单按“逐件资产”和“数量物品”区分，明确逐件低值耐用品、按数量保管的耐用品及易耗品入口，修复逐件低值耐用品总览与列表口径不一致。
- 公司、部门、员工、位置、分类、仓库和物品档案支持新增时留空自动编号，保留手填及原有编号；补充规范化判重、并发碰撞重试和对应测试。
- 操作说明见 [物品管理方式与档案编号](docs/Item-Management-and-Numbering.md)。

## v0.2.2-rc.1 - 2026-09-20

- 台账区分创建日期与正式建档日期，正式建档筛选与导出采用建档记录，详情及财务待办显示对应时间。
- 财务待办支持编号、名称、部门和资料状态筛选，显示已保存基础资料的缺项。
- 草稿支持跨页选择及批量补充责任资料，先预览后保存，保留逐件审计；预览后变化整批拒绝，重复提交不重复修改。
- 集中资产页面访问、状态含义、来源归还公共处理及批量选择校验，减少页面模块之间的依赖。
- 开发环境启动先只读检查迁移状态，已有完整结构时直接使用运行身份启动；需要迁移时仍通过原迁移身份执行。
- 操作说明见 [试用指南](docs/Trial-Guide.md)。

## 历史未发布变更

- 2026-09-14：按用户要求彻底移除实物分类的 `category_type` 字段、枚举及校验约束；同步分类页面、编码预设和后台服务。停用依赖旧分组的两类新报表，保留历史导出访问；已通过迁移审计保留变更前值并验证回退。开发版已更新，详见 [移除记录](docs/Remove-Physical-Type-2026-09-14.md)。
- 2026-09-12：完成检查报告中的六项代码改进：LV 实物列表独立于财务确认并兼容旧记录；资产导入 v3 增加编码属性、年份和组件来源，保留 v2 读取；统一日期回显；楼层筛选与导出保持相同权限范围；人员新增部门、岗位、标记查询；角色查询在单次 GET/HEAD 请求中复用。验证和开发环境状态见 [改进记录](docs/Project-Improvements-2026-09-12.md)。
- 2026-09-10：按用户提供并确认的编码规则实现 `AA-CC-YYYY-NNNNNN-SS`、独立首次管理属性与取得年份、跨规则版本连续发号、组件子项、组合清单历史和拆并来源；补充电子/替代标识及租入归还证据。财务认定不改号，拆并不自动转移金额，初始化允许后补财务政策。详见 [当前资产编码规则](docs/Asset-Coding-Standard.md)。
- 2026-09-10：经用户授权，完成实物建档与财务分离的开发环境加密备份、迁移与重启；原资产、财务、编号、二维码和附件记录核对一致，更新后检查通过。正式版未升级。
- 2026-09-09：按用户要求将实物建档与财务折旧确认分离；建档不需要照片或财务复核，编号和二维码独立生成，照片/发票可后补；调整财务待办、实物历史查询与对账提示，新增独立建档记录及兼容迁移。详见 [业务变化](docs/Asset-Registration-and-Finance-2026-09-09.md)。

- 2026-09-09：根据用户要求整合全部项目文档；精简协作规则，将 AI 长需求降为参考，补充当前文档和模块索引，长技术说明集中到开发指南；同步标签流程、低值物品进度与实现路径，归档旧入口和完成清单，为历史任务与证据关联后续状态。仅调整文档，详见 [整理记录](docs/Documentation-Refresh-2026-09-09.md)。
- 2026-09-07：台账筛选导出、盘点发布预览、部门人员联动、中文表单和备份测试工厂等已有工作树修复，见 [易用性修复记录](docs/Usability-Repair-2026-09-07.md)。此条补记历史工作，不表示本次重新实施、验证或发布。

## v0.2.1 - 2026-08-31

- 生产加固审计：修复折旧处置停止事件遗漏、月中/年度处置错账风险、陈旧主数据写入、
  跨公司用户绑定、正式编号冲突、低值物品领退冲销与清退竞态、权限范围泄露、
  库存/盘点锁序死锁、备份发布/过期/下载竞态及登录审计失败后的会话残留。
- 新增当前正式资产位置必须持续为叶级节点的 Service 与 PostgreSQL 触发器保护；
  升级迁移会拒绝既存违规数据，不会静默改写资产位置。
- 生产镜像升级并固定为 PostgreSQL 18.6 与 Caddy 2.11.4 的官方多平台摘要，
  同步收紧生产代理 HTTPS、Host、CSRF 来源及导入 XLSX 容器校验。
- 强化低值物品库存核算不变量：拒绝非有限 Decimal，统一数量/金额/平均成本勾稽，
  保留全量出库及分次退回尾差、原来源成本、调拨双腿、完整冲销和严格幂等链路。
- 将库存与保管 reconcile/rebuild 提升为逐笔链路完整性核对，并增加 PostgreSQL
  延迟约束、正式历史表运行时防删权限以及并发锁序回归。
- 重构角色导航、任务中心、搜索筛选与分页，增加资产、盘点、低值物品和离职清退的
  直接办理入口、高风险确认与 390px 手机端操作优化。
- 二维码盘点的 `Origin: null` 兼容仅限带短时签名的指定桥接端点，并持续要求正确 Host、
  登录 Session、CSRF Cookie/Token 和任务权限；其他 POST 端点仍按标准 CSRF 拒绝。

## v0.2.0 - 2026-08-27

- Sprint 13：新增 `apps.supplies` 基础骨架、低值物品分类/仓库/物品档案、
  公司隔离与后端角色权限、分页页面和物品档案 XLSX 全有或全无导入。
- 逐件低值耐用品入口继续复用现有 `Asset + controlled_non_fixed`，未修改
  `Asset.quantity=1`，未提前实现库存余额、流水、过账、领退调拨、保管、
  盘点、清退或低值物品报表。
- Sprint 14：新增期初/日常入库、移动加权平均、不可变库存流水、余额缓存及期初库存导入。
- Sprint 15：新增领用、退回、调拨、完整冲销、数量型耐用品保管及 PostgreSQL 并发控制。
- Sprint 16：新增耐用品归还、转交、报损、报废、期初保管导入，并完成逐件
  `controlled_non_fixed` 防折旧集成。
- Sprint 17：新增仓库/保管盘点、差异处理和数量型耐用品离职清退闭环。
- Sprint 18：新增低值物品正式 Dashboard、12 张分页报表、按权限裁剪的流式 XLSX、
  ExportLog 审计、库存/保管余额 dry-run 与受控重建、导航首页和业务操作文档。
- Sprint 18 PostgreSQL 容量验证覆盖 10,000 个物品、20 个仓库、100 个用户和最高
  520,000 条库存流水；基于 EXPLAIN 为公司级库存流水分页增加发生时间索引，未盲目增加其他索引。
- 低值物品 UAT 使用隔离 PostgreSQL、内置 Chromium 和 Windows Edge；生成本版证据时
  默认业务数据库仍未迁移，且未创建版本标签或 Release。

## v0.1.0 - 2026-08-25

- 整合 Sprint 0–12、Requirements V1.0/V1.1 及后续纠正提交为首个预发布软件功能基线。
- 完成资产主档、财务确认、六类折旧、二维码标签、生命周期、盘点、保养、离职清退、报表和 T+ 人工对账。
- 完成中文企业界面、受控用户创建、中文审计展示、二维码查看/打印及 Web 贴标确认。
- 兼容 Edge Android 扫码产生的受限 `Origin: null` 场景，同时保留登录、CSRF Token、固定 Host、权限、当前二维码和幂等校验。
- 移除无法由浏览器验证的二次打印确认；点击打印即记录批次并打开 A4 预览，实际贴标仍须逐项确认。
- 提供 PostgreSQL 18、Gunicorn、Caddy、加密备份、30 日保留和隔离恢复工具。
- PostgreSQL 最终全量自动回归 `1085 passed, 3 skipped`。
- 本标签代表功能冻结基线；固定 DNS、受信任 HTTPS、独立备份设备和多角色人工 UAT 仍是生产上线门槛。

## Requirements Baseline V1.1

- Clarified that development is authorized one Sprint at a time.
- Added complete Sprint 3–12 execution tasks and a universal Definition of Done.
- Separated physical asset category from Finance fixed-asset classification.
- Fixed official-code issuance, permanent history, counter uniqueness, reset scope and scheme versioning requirements.
- Added role, field and department-scope permission matrices and controlled workflow state machines.
- Added durable user-department grants, structured loan/return records, inventory assignees/resolutions and distinct disposal cancellation/reversal records.
- Added normative depreciation formulas, rounding, final-period correction, opening-balance, adjustment and reversal rules.
- Resolved duplicated Finance data sources and added monthly depreciation batch control.
- Added QR/label records, secure tokens, label state and LAN/mobile security requirements.
- Reworked attachments so inventory surplus, maintenance and disposal records can own files safely.
- Added import staging/idempotency and spreadsheet-injection protection.
- Moved audit integration to Sprint 0 and every subsequent Sprint.
- Added T+ manual reconciliation workbook specification.
- Added LAN HTTPS, local static assets, backup retention, restore drill and production gates.
- Added end-to-end UAT and requirement-to-Sprint traceability.
- Closed all nine initialization steps across Sprint 1/2/4 and added numeric 5,000-asset performance thresholds.
- Required original cost for every formal asset, structured internal/external borrowers, planned versus actual disposal dates, exact import staging fields, disposal-aware maintenance restoration and reachable offboarding refresh rules.
- Finalized custom-field, Employee activation, depreciation token, Loan/Movement, multi-dimensional inventory, maintenance-problem invalidation, disposal-linked depreciation-event and typed export-total contracts so each Sprint can implement without inventing schema values.

## Requirements Baseline V1.0

- Initial requirements baseline and Sprint 0–2 task set.
