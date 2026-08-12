# Codex Task — Sprint 0：项目初始化与审计基础

## 前置与工作目录

这是第一个 Sprint。压缩包解压后的目录，即包含 `AGENTS.md`、`docs/` 和 `tasks/` 的目录，必须作为仓库根目录。

必须在该根目录直接创建 `manage.py`、项目配置、应用目录和依赖文件。不得再创建第二层 Git 仓库或 `eam_lite/eam_lite/` 式无必要的项目根套娃。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/04-Database-Design.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/09-Security-Backup-and-Deployment.md`
- `docs/10-Definition-of-Done.md`

本次只完成 Sprint 0。若当前目录已有项目代码或迁移与本任务冲突，先报告并停止，不得覆盖。

## 目标

建立一个可重复安装、可在 PostgreSQL 上运行和测试、结构清晰的 Django 项目骨架，并从第一天提供用户、角色和审计基础。

必须能够：

1. 启动 Web 服务。
2. 连接 PostgreSQL，并允许开发者明确选择 SQLite 进行非并发本地开发。
3. 用户登录、退出和访问受保护首页。
4. 运行自动测试和迁移检查。
5. 使用环境变量保存敏感配置。
6. 预置后续业务所需角色组。
7. 通过统一 Service 写入不可由普通业务修改的审计日志。
8. 在无公共互联网时正常加载基础页面样式和 HTMX。

## 技术与依赖策略

采用：

- Python
- Django
- PostgreSQL
- Django Templates
- Bootstrap
- HTMX
- pytest + pytest-django

本任务文件不锁死未来的具体版本号。实现时必须：

1. 选择当时仍受支持且彼此兼容的稳定版本。
2. 在 README 记录支持的 Python、PostgreSQL 和主要框架版本范围。
3. 在 requirements/lock 文件中记录实际解析出的精确版本，使干净环境可重复安装。
4. 将生产依赖与开发/测试依赖清晰分开。
5. 说明每个非显然第三方依赖的用途。

不得引入 React、Vue、微服务、Redis 或 Celery。不得为了环境变量、树形菜单等简单能力引入大型包。

Bootstrap、HTMX 及运行页面必需的前端资源必须存放在项目本地静态目录，不使用公共 CDN。

## 建议结构

可合理调整，但至少保持职责清晰：

```text
manage.py
config/
apps/accounts/
apps/core/
apps/audit/
templates/
static/vendor/
tests/
requirements/ 或 pyproject + lock
.env.example
.gitignore
README.md
AGENTS.md
```

不得提前创建 Asset、折旧、盘点等业务模型。

## 环境配置

所有环境差异通过配置读取。`.env.example` 至少说明：

- `SECRET_KEY`
- `DEBUG`
- 数据库引擎及 PostgreSQL 的 name/user/password/host/port
- `ALLOWED_HOSTS`
- 必要时的 `CSRF_TRUSTED_ORIGINS`
- 日志级别
- 静态/媒体目录的非敏感配置

要求：

- `.env` 加入 `.gitignore`。
- `DEBUG` 使用严格布尔解析，不得将任意非空字符串当成 True。
- 默认语言为简体中文。
- 时区为 `Asia/Shanghai`，启用 timezone-aware datetime。
- 系统业务币种默认 CNY；此处只建立配置，不做财务功能。
- 开发和测试配置不得静默回退到错误数据库。
- 真实 Secret、密码、Token、私钥不得进入仓库、日志或测试快照。

## 用户与角色

从第一版迁移开始使用自定义 User 模型，至少包含：

- username
- display_name
- email
- mobile
- is_active
- is_staff
- created_at
- updated_at

使用 Django Group/Permission 或文档批准的等效方案，数据迁移幂等创建：

- system_admin
- finance
- equipment
- department_manager
- employee
- warehouse
- hr
- management

Sprint 0 只建立身份和角色基础，不提前实现资产数据范围。未初始化系统的首次管理员使用 Django superuser 进入；后续业务权限按 `docs/07-Permissions-and-Workflows.md` 实施。

提供受控的首批账号引导方式（管理命令或仅 superuser 可用的最小页面），用于创建应用用户并显式分配固定角色。它不得创建通用自定义权限，也不得让 `system_admin` 自动继承 `hr` 或 `finance`；账号及角色变更必须审计。

## 审计基础

实现 `AuditLog` 及统一审计写入 Service，字段以数据库设计为准，至少包括：

- user
- action
- object_type
- object_id
- old_data_json
- new_data_json
- ip_address
- user_agent
- created_at

Company 要到 Sprint 1 建立。Sprint 0 不得用自由文本/假外键伪造 company；预初始化系统事件暂时不带业务 company，代码和迁移接口必须明确预留 Sprint 1 添加 `company -> Company PROTECT/NULL`。Sprint 1 起所有业务审计必须带当前 company，只有公司建立前的系统级事件可保持 NULL。

要求：

- 审计记录默认只追加，不提供普通编辑/删除入口。
- JSON 必须可安全序列化 Decimal、日期和 UUID。
- 提供字段脱敏/排除机制，密码、Secret、Token 原文永不进入日志。
- 业务 Service 可在自身事务内调用审计 Service；不得只依赖模板或前端。
- 本 Sprint 至少为登录成功、登录失败或管理员基础操作中的适用事件建立安全日志/审计示例，但不得记录密码。

## 页面与访问控制

### 登录页

- 中文标签。
- 用户名和密码。
- 错误凭据显示统一错误，不泄露账号是否存在。

### 首页

登录后显示：

- “EAM-Lite 企业资产管理系统”
- “系统初始化尚未完成”

### 基础布局

统一 `base.html` 包含：

- 顶部栏
- 左侧菜单占位
- 当前用户
- 退出
- 内容区域

未登录访问首页及后续业务页面必须跳转登录。停用用户不得登录。

退出使用 POST 或框架认可的安全方式，不通过可被跨站触发的无保护 GET 完成状态变更。

## 日志

至少提供 INFO、WARNING、ERROR 级别配置，开发环境输出到控制台。日志不得包含密码、Secret、Token 或完整敏感请求体。

日志与 AuditLog 职责分离：运行日志用于诊断，AuditLog 用于业务追溯。

## 事务与迁移

- 自定义 User 必须在首次迁移中正确设置，后续不得再切换用户模型。
- 角色初始化使用可重复执行的数据迁移或幂等管理命令。
- AuditLog 写入失败时，对要求强审计的未来关键业务操作应使同一事务失败；本 Sprint 建立可测试接口。
- AuditLog 的公司关联迁移边界已文档化，且不提前创建 Company 业务模型。
- 所有迁移必须能在空 PostgreSQL 数据库运行。

## 自动测试

至少覆盖：

1. 用户可使用正确密码登录。
2. 错误密码不能登录且密码不进入日志。
3. 停用用户不能登录。
4. 未登录不能访问首页。
5. 登录后可访问首页。
6. 用户可安全退出，退出后会话失效。
7. 自定义 User 可正常创建及迁移。
8. 八个角色组被幂等创建。
9. Audit Service 正常写入 old/new 数据。
10. Audit Service 对敏感字段脱敏或拒绝记录。
11. 本地静态资源请求不依赖公共 CDN。
12. 配置能识别 `Asia/Shanghai`、CNY 和严格 DEBUG 布尔值。

### PostgreSQL 冒烟验证

Sprint 0 不得只用 SQLite 完成。至少在 PostgreSQL 测试环境执行：

- 依赖安装
- 全部迁移
- 创建测试用户或管理员
- 自动测试
- Web 首页请求冒烟

README 必须写出项目实际选择的准确命令。若当前环境无法提供 PostgreSQL，Sprint 0 状态必须报告为阻塞，不得声明完成。

## README

项目 `README.md` 至少写清：

- 项目定位和仓库根目录
- 支持的运行时/数据库版本范围及精确锁文件
- 虚拟环境和依赖安装
- `.env` 配置
- PostgreSQL 数据库准备
- 迁移
- 创建管理员
- 启动
- 测试、迁移检查和 PostgreSQL 冒烟命令
- SQLite 仅适用的开发范围及限制
- 静态资源本地化说明

## 本 Sprint 排除

不得开发：

- Company、Department、Employee、Location、AssetCategory
- Asset 及附件
- 编码、财务、折旧
- 导入、二维码、盘点、保养、离职、报表、T+
- 生产部署和备份任务

## 验收与停止条件

只有以下全部成立才可完成：

- 根目录结构正确且没有第二层仓库。
- 干净环境可按锁文件安装。
- 空 PostgreSQL 可迁移，自动测试和首页冒烟通过。
- 登录、退出、角色和审计基础可验证。
- 中文、上海时区、CNY、本地静态资源及 `.env.example` 已落实。
- 满足 `docs/10-Definition-of-Done.md`。

按 `AGENTS.md` Completion Report 格式汇报后立即停止。不得开始 Sprint 1。
