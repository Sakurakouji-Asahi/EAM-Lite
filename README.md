# EAM-Lite

EAM-Lite 是公司局域网内使用的轻量级企业资产管理系统。本仓库根目录就是包含
AGENTS.md、docs/、tasks/ 和 manage.py 的当前目录，不存在第二层项目仓库。

当前代码完成到 Sprint 1：除 Sprint 0 的身份、登录和审计基础外，已提供公司、部门、
人员、位置、实物分类、用户部门范围、非财务技术设置、初始化向导步骤 1–5/8，以及
部门/人员 XLSX 的受保护上传、校验、预览和原子确认。资产主档、编码、财务、折旧、
盘点等后续业务仍未开始。

## 版本与依赖

- Python：>=3.14.7,<3.15（本 Sprint 验证版本 3.14.7；不使用 3.15 预览版）
- Django：5.2 LTS，项目范围 >=5.2,<5.3，精确锁定 5.2.17
- PostgreSQL：支持 16–18，本 Sprint PostgreSQL 验证版本 18.4
- psycopg：3.3 系列，精确锁定 3.3.4
- pytest：9.1 系列，精确锁定 9.1.1
- pytest-django：4.14 系列，精确锁定 4.14.0
- Bootstrap：5.3.8，本地静态文件
- HTMX：2.0.10，本地静态文件
- openpyxl：3.1.5，用于生成并解析无宏 XLSX
- defusedxml：0.7.1，作为 XML 解析安全加固依赖

requirements/production.in 和 requirements/development.in 记录直接依赖的兼容范围；
production.lock 和 development.lock 记录当前环境实际解析出的全部精确版本。安装和
部署只使用 lock 文件。

psycopg 是 Django 连接 PostgreSQL 的驱动，psycopg-binary 提供与 Python 3.14
匹配的预编译运行组件。pytest 和 pytest-django 只属于开发/测试依赖。项目不依赖
python-dotenv、React、Vue、Redis 或 Celery。

## 建立虚拟环境

在仓库根目录执行：

~~~powershell
python -VV
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements\development.lock
~~~

仅安装生产运行依赖时使用：

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements\production.lock
~~~

## 环境配置

.env.example 是完整配置清单，不含真实凭据。应用直接读取进程环境变量，不会自动
读取 .env 文件；服务环境应由操作系统服务管理器或组织认可的 Secret 机制注入。
这样无需引入 dotenv 依赖，也不会误把 .env 提交到 Git。

本地 PowerShell 示例：

~~~powershell
$env:SECRET_KEY = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"
$env:DEBUG = "true"
$env:DB_ENGINE = "postgresql"
$env:DB_NAME = "eam_lite"
$env:DB_USER = "eam_lite"
$secureDbPassword = Read-Host "PostgreSQL password" -AsSecureString
$dbPasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureDbPassword)
try { $env:DB_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($dbPasswordPointer) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($dbPasswordPointer) }
$env:DB_HOST = "127.0.0.1"
$env:DB_PORT = "5432"
$env:ALLOWED_HOSTS = "localhost,127.0.0.1,testserver"
$env:CSRF_TRUSTED_ORIGINS = ""
$env:LOG_LEVEL = "INFO"
$env:LOGIN_FAILURE_WINDOW_SECONDS = "900"
$env:LOGIN_FAILURE_PAIR_LIMIT = "5"
$env:STATIC_ROOT = "var/static"
$env:MEDIA_ROOT = "var/media"
$env:IMPORT_TEMP_ROOT = "var/tmp"
$env:BUSINESS_CURRENCY = "CNY"
$env:SECURE_SSL_REDIRECT = "false"
$env:SESSION_COOKIE_SECURE = "false"
$env:CSRF_COOKIE_SECURE = "false"
$env:TRUST_PROXY_SSL_HEADER = "false"
~~~

SECRET_KEY、DEBUG、ALLOWED_HOSTS 以及 PostgreSQL 连接信息是启动所需配置。
DEBUG 和所有安全开关只接受 true/false、1/0、yes/no 或 on/off；其他值会直接拒绝
启动。两个 LOGIN_FAILURE_* 配置只接受正整数。DEBUG=false 时 ALLOWED_HOSTS 禁止
使用通配符。

登录限速默认在 15 分钟内允许同一来源 IP + 用户名组合产生 5 次连续失败；第 N 次
失败仍执行认证并写 AuditLog，第 N+1 次返回与错误凭据完全相同的提示，不再验证密码
或新增失败审计。该组合成功登录后，此前失败不再计数。限速状态存于数据库 AuditLog，
可由多个应用进程共享；并发请求可能在审计记录提交前同时穿过计数检查。

应用只使用 REMOTE_ADDR，不直接信任客户端提交的 X-Forwarded-For。生产反向代理必须
在可信客户端 IP 边界另设全 IP/随机用户名的边缘限速，并按部署方式安全传递来源地址；
本 Sprint 不实现生产反向代理配置。

生产环境必须使用随机 Secret、DEBUG=false、准确的内网域名和 HTTPS 安全开关。
Sprint 0 不包含生产部署，生产要求继续以
docs/09-Security-Backup-and-Deployment.md 为准。

## PostgreSQL 准备

以下简化命令仅用于开发/测试，由有建库权限的数据库管理员执行，密码通过交互提示输入：

~~~powershell
createuser --pwprompt eam_lite
createdb --owner=eam_lite --encoding=UTF8 eam_lite
~~~

上述兼任数据库 owner/迁移账号的 eam_lite 不是生产 runtime 账号方案。生产必须按
docs/09 分离 schema/migration owner 与最小权限 runtime 账号；本 Sprint 不实现生产
GRANT。运行 pytest 时 Django 会创建并删除独立测试数据库，因此测试账号需要
CREATEDB；不要把这项权限授予生产 runtime 账号。

## 迁移与检查

确保上述环境变量已设置，然后执行：

~~~powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py showmigrations
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py check
~~~

首次迁移创建自定义 accounts.User 和 audit.AuditLog。第二个账号迁移幂等创建以下
固定 Django Group：

- system_admin
- finance
- equipment
- department_manager
- employee
- warehouse
- hr
- management

反向迁移不会删除角色或既有分配，避免破坏身份数据。

Sprint 1 的 `masterdata.0001_initial` 建立基础资料、初始化、受保护附件元数据和导入
staging 表；`audit.0002_auditlog_company` 为业务审计增加 nullable `Company` 外键；
`masterdata.0002_postgresql_integrity_triggers` 在 PostgreSQL 上安装跨公司、任意深度树、
经理有效性、导入证据和技术设置约束。该触发器迁移按数据库 vendor 执行，SQLite 仅供
显式快速开发，不能作为 Sprint 验收数据库。

## Sprint 1 基础资料与初始化

登录后从侧栏进入“初始化向导”，或直接访问 `/setup/`。本 Sprint 实现：

1. 公司；
2. 部门；
3. 人员；
4. 实物分类；
5. 位置；
8. 用户、固定角色及部门数据范围。

步骤完成状态由真实数据和权限条件重新计算，不以访问页面为准；因为步骤 6、7、9 尚未
实现，`initialization_completed` 在 Sprint 1 始终保持 false。`system_admin` 协调公司、
部门、权限和技术关联，`hr` 维护人员任职资料，`equipment` 可维护位置和实物分类。
授权范围本身不授予业务角色；部门经理仍必须具有 `department_manager` 固定角色。

新绑定部门经理必须属于当前公司、任职状态为 `active`、员工启用，且所属部门启用；
可来自公司内任意启用部门，不要求登录账号或 `department_manager` 角色。通过受控
Service 把经理改为 leaving/resigned 或停用员工时，会在同一事务清空其全部经理关联并
逐条写入公司审计。

## 部门与人员 XLSX 导入

入口为 `/imports/`。模板规则是固定且版本化的：

- 部门：版本 `department-v1`，工作表 `部门导入`；列依次为 `部门编码`、`部门名称`、
  `上级部门编码`、`经理工号`、`是否启用`。
- 人员：版本 `employee-v1`，工作表 `人员导入`；列依次为 `员工编号`、`姓名`、
  `部门编码`、`任职状态`、`入职日期`、`离职日期`、`手机号码`、`备注`、`是否启用`。
- 部门必填 `部门编码`、`部门名称`；人员必填 `员工编号`、`姓名`、`部门编码`、
  `任职状态`。编码和员工编号匹配键统一执行 Unicode `NFKC`、首尾空白清理和
  `casefold`，显示值保留规范化后的原大小写。
- 导入只新增，按当前公司内规范化编码/员工编号匹配，不按名称覆盖；文件内或数据库内
  重复均报错。部门文件允许引用同文件中新建的上级；人员只匹配数据库中已有启用部门。
- `是否启用` 只接受 `是`/`否`，空白默认为 `是`。人员 `任职状态` 只接受
  `active`、`leaving`、`resigned`；`resigned` 必须填写离职日期并停用，
  `active`/`leaving` 的离职日期必须为空，`leaving` 也必须停用。
- 日期必须为 `YYYY-MM-DD`，离职日期不得早于入职日期；空白行忽略；未知列、公式单元格、
  非法枚举会形成行级错误。错误包含行号、字段、原始值和原因。
- 仅接受 `.xlsx`；拒绝宏、外部链接、嵌入对象、任意工作表中的公式和异常 ZIP；大小由
  `attachment_max_size_bytes` 控制且不超过 20 MiB。ZIP 最多 512 个成员、单成员解压后
  最多 10 MiB、总解压量最多 20 MiB、压缩比最多 100；工作簿每张表最多 10,001 行、
  32 列，单批最多导入 10,000 条业务数据，超限会在解析业务行之前拒绝。
- 解析/预览不写正式基础资料。只有整个批次无错才能确认；确认在一个数据库事务中
  全有或全无，重复确认不会重复创建记录。
- 原文件先写入 Web/static root 之外的私有随机路径；附件元数据先以 `pending`、
  `is_available=false` 建立，校验行和审计均成功提交时才随同一数据库事务发布为
  `policy_limited`、`is_available=true`。存储后端不提供公开 URL，原文件只能通过权限
  检查后的下载视图读取；反向代理不得映射 `MEDIA_ROOT`、`IMPORT_TEMP_ROOT` 或
  `/protected-media/`。

当前上传校验状态为 `policy_limited`：应用会检查扩展名、ZIP/XLSX 结构、解压规模、宏、
公式、外部链接和嵌入对象，但 Sprint 1 未集成独立杀毒引擎。文件存储与数据库不是同一
事务资源；常规数据库失败会同步删除刚保存的对象，进程在两者之间异常终止时只可能留下
不可公开、无数据库引用的私有文件，由下述幂等清理命令处理。

### 导入 staging 与私有文件清理

清理入口为 `cleanup_import_staging`，仅允许可登录的 `system_admin` 作为 `--actor`。
命令默认只做 dry-run；真实删除必须显式提供 `--execute`，并建议为每次计划任务传入唯一
`--task-id`。自动批次清理只处理超过保留期且从未映射正式对象的 `uploaded`、`invalid`、
`failed`；默认和最小保留期都是 30 天。`validated` 永不自动清理，`confirmed` 批次、行、
映射、源附件和摘要永不删除。每个候选都会锁定 Batch 及 Row、取得与上传/确认相同的公司
幂等锁，并在删除前重新检查状态、created 映射和处理中幂等请求。

批准文档没有为“附件成为孤儿后的保留期”“遗留上传临时文件时限”和“无元数据私有文件
保留期”规定统一数值，因此程序不猜测默认值，也不把它们写入 `.env`。组织批准正整数后，
每次命令都必须显式传入；不得传 0。以下 PowerShell 先交互取得已批准值，再执行 dry-run：

~~~powershell
$orphanDays = Read-Host "经批准的附件孤儿保留天数"
$tempHours = Read-Host "经批准的遗留临时文件时限（小时）"
$privateDays = Read-Host "经批准的无元数据私有文件保留天数"
.\.venv\Scripts\python.exe manage.py cleanup_import_staging `
  --actor app-admin --task-id "manual-review-20260812" `
  --batch-retention-days 30 `
  --orphan-retention-days $orphanDays `
  --temp-older-than-hours $tempHours `
  --unreferenced-private-days $privateDays
~~~

人工核对输出中的候选/跳过数量后，用完全相同参数加 `--execute` 执行；重复执行应返回零个
新增删除结果且不会误删。批次删除只把原 Attachment 标为不可用孤儿并记录 `orphaned_at`；
附件清理达到独立保留期后仍会再次检查引用，随后先提交元数据删除和审计，再删除底层对象。
若最后一步存储删除失败，命令返回非零，遗留文件可由无元数据私有文件扫描再次处理。

明确放弃一个 `validated` 批次是独立人工操作，必须填写原因；先不加 `--execute` 预演：

~~~powershell
.\.venv\Scripts\python.exe manage.py cleanup_import_staging `
  --actor app-admin --task-id "abandon-<batch-id>" `
  --abandon-validated <batch-id> --reason "重新制表并废弃本批次"
~~~

复核后追加 `--execute`。该操作不新增 cancelled 状态；它锁定并重查 validated、无 created
映射后写 `import_abandon` 审计，再让源附件进入孤儿候选。所有清理均保留摘要而不在输出或
审计中暴露原始存储路径/幂等键。临时目录只能保存解析副本，不能作为导入证据；生产应由
受控计划任务周期调用同一 management command，并保留命令退出码和审计记录。处理磁盘
临时上传时，应用会在 `IMPORT_TEMP_ROOT/.active/` 持有跨进程文件锁；重启清理只删除已
超过显式时限且活动锁未被占用的文件，锁仍被工作进程持有时一律跳过。清理元数据/审计与
数据库操作同事务提交；底层文件删除发生在提交之后，失败会以非零退出并可安全重试。

## 创建恢复管理员与首批应用账号

先在受控管理终端创建唯一的恢复用 Django superuser：

~~~powershell
.\.venv\Scripts\python.exe manage.py createsuperuser
~~~

createsuperuser 除用户名、邮箱和密码外还会要求填写必填的 display_name。

再由该 superuser 作为明确执行人，通过受控命令创建应用用户。命令只允许分配上面的
固定角色，不创建自定义权限。命令强制填写原因，并默认依次隐藏输入执行人当前密码、
新用户密码：

~~~powershell
.\.venv\Scripts\python.exe manage.py bootstrap_user --actor root --username app-admin --display-name "应用管理员" --roles system_admin --reason "建立首批系统管理员"
~~~

同一人确需多个角色时必须显式列出，例如：

~~~powershell
.\.venv\Scripts\python.exe manage.py bootstrap_user --actor root --username finance-user --display-name "财务用户" --roles system_admin finance --reason "建立首批财务账号"
~~~

命令拒绝覆盖现有用户名；用户创建、角色分配与 AuditLog 在同一数据库事务内完成。
审计失败时账号创建整体回滚。执行人的当前密码在任何用户写入前校验。自动化场景可用
--actor-password-env ACTOR_VARIABLE 和 --password-env NEW_USER_VARIABLE 分别从进程
环境读取两个密码；禁止把密码作为命令行明文参数，密码原文也不会进入审计。

## 启动

开发环境启动：

~~~powershell
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
~~~

浏览器打开 http://127.0.0.1:8000/login/。登录页和首页使用中文；未登录访问首页会
跳转到登录页；停用用户不能登录；退出只接受带 CSRF 保护的 POST。

runserver 仅用于开发，不能作为生产进程。

## 自动测试

### PostgreSQL 完整验证

保持 DB_ENGINE=postgresql 和测试数据库连接环境变量，然后执行：

~~~powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py check
~~~

### 首页请求冒烟

SMOKE_USERNAME 应指向已存在的启用用户。该命令不读取或输出密码：

~~~powershell
$env:SMOKE_USERNAME = "app-admin"
.\.venv\Scripts\python.exe manage.py shell -c "import os; from django.contrib.auth import get_user_model; from django.test import Client; u=get_user_model().objects.get(username=os.environ['SMOKE_USERNAME']); c=Client(HTTP_HOST='localhost'); c.force_login(u); r=c.get('/'); assert r.status_code == 200; assert 'EAM-Lite 企业资产管理系统' in r.content.decode(); print('homepage smoke: 200')"
~~~

### 显式 SQLite 快速检查

SQLite 只允许非并发本地开发的快速回归；标记为 PostgreSQL 专用的触发器、锁、并发和
数据库约束用例会明确跳过，因此这个结果不能关闭任何 PostgreSQL 验收项。项目不会因
PostgreSQL 配置错误自动回退 SQLite。必须显式设置：

~~~powershell
$env:DB_ENGINE = "sqlite"
$env:SQLITE_PATH = ":memory:"
.\.venv\Scripts\python.exe manage.py migrate --noinput
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe -m pytest
~~~

本地持久开发可把 SQLITE_PATH 显式改为 var/dev.sqlite3。切回 PostgreSQL 时必须重新
设置 DB_ENGINE=postgresql 及全部 DB_* 变量。

## 静态资源

Bootstrap 5.3.8 和 HTMX 2.0.10 已固定在 static/vendor/，页面模板没有公共 CDN、
Google Fonts、远程脚本或远程图片依赖。第三方许可证、来源和 SHA-256 见
static/vendor/README.md。

类生产环境收集静态资源：

~~~powershell
.\.venv\Scripts\python.exe manage.py collectstatic --noinput
~~~

生产中应由反向代理提供 STATIC_ROOT 的只读内容；业务附件不会放入 static 目录。

## 审计边界

所有 Sprint 0 审计通过 apps.audit.services.write_audit_log 写入。Service 递归处理
Decimal、日期、时间和 UUID，并对 password、Secret、Token、Cookie、Session、
Authorization、CSRF、API key 和私钥字段统一脱敏；调用者还能显式排除字段。
AuditLog 未注册普通编辑/删除入口，模型及 QuerySet 均拒绝更新和删除。

Sprint 0 尚无 Company。Audit Service 只接受固定白名单内的预初始化登录、安全和
首批账号事件使用 `company=None`；没有自由文本或假外键。一旦存在任意活动或停用
Company，通用入口也拒绝 NULL company。Sprint 1 已通过跟踪迁移增加
`company -> Company` 的 nullable `PROTECT` 外键；所有业务事件必须使用真实 Company。

## Sprint 1 明确未实现

未创建 Asset 主档、AttachmentLink、正式资产导入、编码与正式编号、财务确认、折旧、
二维码、盘点、保养、离职清退、处置、报表、T+、生产部署或备份任务；初始化向导步骤
6、7、9 也未实现。后续功能只能在对应 Sprint 获得明确授权后开始。
