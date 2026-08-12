# Codex Task — Sprint 1：基础资料与初始化向导

## 前置

- Sprint 0 已按 `docs/10-Definition-of-Done.md` 验收通过。
- 当前完整测试套件通过。
- PostgreSQL 迁移和登录/角色/审计基础可用。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/09-Security-Backup-and-Deployment.md`
- `docs/10-Definition-of-Done.md`

本次只开发基础资料、部门数据范围，以及初始化向导步骤 1–5 和步骤 8（用户、固定角色及部门范围）。不得开发资产主档。

## 范围

实现：

- Company
- Department
- Employee
- Location
- AssetCategory
- UserDepartmentScope
- InitializationSetting
- SystemSetting 通用键值/类型校验基础（本 Sprint 只开放非财务技术设置）
- 通用 Attachment 文件元数据（仅用于受保护的导入源文件，本 Sprint 不做业务附件 Link）
- ImportBatch / ImportRow staging 基础（department/employee 类型）
- 对应管理页面
- `/setup/` 初始化向导步骤 1–5 和步骤 8
- 部门和人员 Excel 模板、校验、预览和确认导入
- 权限、数据范围和审计

V1 的公司范围、代码唯一性、物理分类与会计分类分离，必须以修订后的数据库和权限文档为准，不得从旧枚举自行推断。

## 模型与约束

### Company

字段按数据库设计，至少包括 code、name、short_name、is_active 和时间字段。

要求：

- code 按文档规定范围唯一，并执行去除首尾空格、统一大小写等项目约定的标准化。
- 停用不等于删除。
- 建立 Company 后以跟踪迁移为 AuditLog 增加 `company -> Company PROTECT/NULL`；此后所有本 Sprint 及后续业务审计必须写当前 company，只有 Sprint 0 的预初始化系统事件允许 NULL。

### Department

至少包括 company、code、name、parent、manager_employee、is_active 和时间字段。

要求：

- 同公司 code 唯一。
- parent 必须属于同公司。
- 禁止自身作为父级及任意深度循环。
- manager_employee 必须满足同公司/有效范围规则。
- 不得通过级联删除破坏员工、资产或历史；基础资料优先停用。

### Employee

Employee 与 User 分离。员工不一定有登录账号。

至少包括 employee_no、name、company/department 归属、user、employment_status、hire_date、termination_date、mobile、remark、is_active 和时间字段，以数据库设计为准。

employment_status：

- active
- leaving
- resigned

要求：

- employee_no 唯一范围明确并由数据库约束。
- user 关联若存在，必须满足唯一性及公司范围要求。
- `employment_status` 表示 HR 任职流程，`is_active` 表示能否参与新业务；新责任/内部借用候选统一要求 `employment_status=active AND is_active=true`。active 可被管理停用，进入 leaving 的 Service 明确置 is_active=false，leaving/resigned 不得再启用；User 账号启停独立，不静默联动。
- termination_date 仅 resigned 必填，active/leaving 必须为空；V1 普通页面不允许 resigned→active。
- Sprint 1 只维护状态；离职资产清退在 Sprint 10 接入。

### UserDepartmentScope

按数据库设计建立带公司、用户、授权根部门、`include_descendants`、启用/撤销及操作人时间的显式授权记录。

要求：

- 只有 system_admin 可为用户分配/撤销部门范围；操作必须审计。
- 固定角色变更走受控 Service；不得停用/移除最后一名可登录 system_admin，最后一名 finance 也必须先配置替代人。
- 授权部门与公司一致，若 User 已绑定 Employee，也必须同公司。
- 默认包含下级部门，不包含上级或同级；支持多个授权根并去重合并。
- 部门树改挂导致范围扩大/缩小时显示影响并写审计，后端统一范围解析器立即生效。
- 部门范围本身不授予角色动作；无 `department_manager` 等允许角色时仍然拒绝。

### Location

至少包括 company、code、name、parent、level、location_type、is_active 和时间字段。

类型以数据库设计为准，至少支持 site、workshop、department_area、warehouse、office、position、other。

要求：

- 同公司 code 唯一。
- parent 必须属于同公司并禁止循环。
- level 由树关系计算或严格校验，客户端不得任意写入不一致 level。
- 数据库只保存树和单个位置节点；UI 可做厂区→区域/车间→具体位置的级联选择，不创建固定三层位置列。
- 支持超过三层的未来嵌套。

### AssetCategory

至少包括 company/适用范围、code、name、parent、物理 category_type、默认维护属性、is_active 和时间字段，以修订后的数据库设计为准。

要求：

- 物理类别与 `AssetFinance.fixed_asset_category` 等会计分类完全分离。
- 模具、工具、设备等物理类别不能因为是否固定资产而互斥。
- code 唯一范围明确。
- parent 同范围且禁止循环。
- Sprint 1 不创建指向尚不存在模型的假整数 ID 或 GenericForeignKey。
- 编码方案和折旧政策默认外键在相应模型建立后用跟踪迁移补充，或按数据库设计的无循环方式实现。

## 基础资料页面

为 `docs/07-Permissions-and-Workflows.md` 授权的对应角色提供可用的列表、新增、编辑、停用/启用和查看页面。不得只交付 Django Admin 作为唯一业务界面。

页面要求：

- 中文标签和明确校验错误。
- Department、Location、AssetCategory 显示树形层级。
- 停用操作有确认提示。
- 已被引用的资料不能物理删除。
- 列表支持基本搜索、启用状态筛选和公司范围过滤。

## 初始化向导

实现 `/setup/` 的前五步：

1. 公司
2. 部门
3. 人员
4. 资产分类
5. 位置

并实现第 8 步“用户、角色及部门数据范围”：列出应用用户、固定角色和活动的 `UserDepartmentScope`，由 system_admin 显式分配。`department_manager` 用户至少有一个活动授权部门才可通过该步骤；至少一名可登录 `system_admin` 和一名可登录 `finance` 必须存在。满足并保存后分别设置 `users_configured`、`permissions_configured`，但不得提前设置整体完成。

要求：

- system_admin 可进入并协调已保存的初始化进度；人员任职资料仍由 hr 按权限矩阵维护，system_admin 只处理技术关联。
- Sprint 0 的首批账号引导必须已创建可登录的 HR/Finance 等所需角色用户；缺少对应业务角色时向导显示阻断和账号引导链接，而不是让 system_admin 代行其权限。
- 每一步的数据写入必须按权限矩阵允许的角色执行，不能因为位于 setup 向导就扩大权限。
- 普通用户不得进入 setup 或绕过 URL 提交数据。
- 每步完成条件以 `InitializationSetting` 的修订字段为准，不以“访问过页面”作为完成。
- 必填基础资料未满足时不能标记该步骤完成。
- 尚有编码、折旧和最终校验步骤未完成，因此 Sprint 1 不得把整个初始化状态设置为 completed。
- 初始化未完成时，非 system_admin 按权限文档只能看到允许的初始化提示，不得进入正式业务模块。

## Excel 导入

至少支持部门和人员两种标准模板。

流程必须为：

1. 下载标准模板。
2. 上传文件。
3. 解析并验证，不写业务表。
4. 显示预览和行级错误。
5. 用户确认。
6. 在事务中写入。
7. 保存导入结果及审计日志。

必须明确：

- 模板版本、工作表名和列名。
- 必填列、日期格式、代码标准化。
- 公司/部门匹配键。
- 文件内重复和数据库重复的处理。
- 新增与更新策略；不得凭名称静默覆盖现有记录。
- 空行、未知列、公式单元格和无效枚举的行为。
- 文件类型和大小限制。

默认采用确认批次全有或全无：任一写入失败回滚本次确认批次。解析错误不得写入任何正式基础资料。

上传原文件必须进入受保护的 Attachment 存储并由 ImportBatch 以真实 FK 引用；不得暂存在公开 media URL、把路径塞进 JSON，或另造一套以后无法复用的临时上传模型。Sprint 5 复用同一 Batch/Row 扩展资产导入类型。

SystemSetting 在本 Sprint 按数据库固定 registry 建表并提供类型化 Service。system_admin 只可配置 `attachment_allowed_extensions` 和 `attachment_max_size_bytes`；Secret 不入表。`fixed_asset_warning_amount` 在 Sprint 4 才由 finance 开放，Sprint 1 拒绝写入。币种/时区使用 Company，残值、寿命、方法、起止和期间默认使用 DepreciationPolicy/批准的会计类别默认，不得擅自创建重复 SystemSetting key。

错误至少显示：行号、字段、原始值、原因。不得只返回“导入失败”。

## 权限与数据范围

严格执行 `docs/07-Permissions-and-Workflows.md`：

- system_admin 管理公司、系统配置、部门、位置、实物分类和用户技术关联，不自动获得 HR 任职资料业务维护权。
- system_admin 管理固定角色分配及 `UserDepartmentScope`；部门经理的所有列表/对象操作通过统一范围解析器限制。
- hr 维护人员基础资料和任职状态；equipment 可维护文档允许的位置/分类；其他角色按矩阵只读或拒绝。
- finance-only 概念不在本 Sprint 提前暴露。
- 页面隐藏之外，View、Form、Service 和导入确认接口都必须校验权限。
- 禁止通过修改 URL/POST ID 把记录挂到无权公司或部门。

新增、编辑、停用、启用、导入确认及关键 setup 变化写入 AuditLog。

## 事务与删除策略

- 同一导入确认批次使用原子事务。
- 树结构变更在保存时重新检查循环及公司范围。
- 并发创建相同 code 由数据库唯一约束兜底，并返回可理解错误。
- 引用中的 Company、Department、Employee、Location、AssetCategory 不得级联物理删除业务数据。
- 审计记录与关键变更在同一业务事务中完成。

## 自动测试

至少覆盖：

1. Company code 唯一和标准化。
2. Department 同公司 code 唯一。
3. Department 自环、深层循环和跨公司父级被拒绝。
4. manager_employee 跨公司/无效关联被拒绝。
5. Employee 无 User 可创建且 User 关联唯一；active/inactive、leaving/resigned 合法组合、termination_date、候选谓词及 leaving 明确停用在数据库/Service 生效，User 启停不被静默联动。
6. employee_no 唯一范围正确。
7. Location 树、level、循环及跨公司父级校验。
8. AssetCategory 树、循环、代码范围及物理/会计分类分离。
9. 停用不会物理删除引用数据。
10. UserDepartmentScope 同公司、活动唯一、下级范围、多个根合并和撤销规则。
11. 无角色只有范围、或有 department_manager 角色但目标超出范围时均被后端拒绝；最后一名 system_admin/finance 保护生效。
12. system_admin 可协调 setup 和配置固定角色/部门范围；hr 可维护人员步骤；无权角色被后端拒绝。
13. 步骤 1–5、8 完成状态正确，整体 initialization_completed 仍为 False。
14. 部门导入解析、预览、确认和回滚。
15. 人员导入解析、预览、确认和回滚。
16. 错误包含行、字段、值和原因。
17. 文件内重复、数据库重复、无效日期、未知部门被拒绝。
18. 导入越权和跨部门 ID 篡改被拒绝。
19. SystemSetting 仅接受 registry 三个 key 及其精确 value_type；本 Sprint system_admin 可配置附件白名单/20 MiB 上限，Secret、未知 key、重复真源 key和 finance 的阈值 key 均被拒绝。
20. CRUD、角色/范围分配、技术设置、停用和导入确认产生审计日志。
21. Sprint 1 业务 AuditLog 均带 company，Sprint 0 预初始化系统事件可安全保留 NULL。
22. Sprint 0 全部回归测试通过。

所有数据库约束测试在 PostgreSQL 上运行。

## 本 Sprint 排除

不得开发：

- Asset 主档、资产附件和正式资产导入
- AttachmentLink 业务对象关联（Sprint 3）
- 编码方案、正式编号
- 财务确认、折旧
- 生命周期、QR、盘点、保养、离职、报表、T+
- 初始化向导步骤 6、7、9 的完成逻辑

## 验收场景

至少人工/集成验证：

1. system_admin 建立公司、三级部门、物理资产分类和三级位置，hr 建立人员；向导记录统一进度。
2. 管理员离开后重新进入 setup，进度保持。
3. 导入一个含正确与错误行的文件时，预览准确显示错误且数据库不变化。
4. 修正文件并确认后，整批成功且产生审计记录。
5. system_admin 为部门经理分配固定角色和一个含下级的部门范围；该经理只能查询授权树，撤销后立即失权且历史可审计。
6. 普通员工直接访问 setup、基础资料编辑和导入确认 URL 均被拒绝。

## 完成与停止条件

- 全部模型、页面、导入、权限、事务和测试达到上述要求。
- 空库及 Sprint 0 数据库升级迁移均成功。
- 满足 `docs/10-Definition-of-Done.md`。
- Completion Report 明确列出尚未实现的初始化步骤 6、7、9。

汇报后立即停止，不得开始 Sprint 2。
