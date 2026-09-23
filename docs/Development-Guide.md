# 开发、验证与实现指南

更新：2026-09-09。本文从原 README 的技术说明集中整理，描述现有实现和操作入口；历史 Sprint 名称只解释迁移来源。当前任务范围、文档地位和自主改进方式以 [AGENTS.md](../AGENTS.md) 为准。

所有相对命令、`apps/`、`requirements/` 和 `var/` 路径都以仓库根目录执行或解析。普通 Windows 使用者直接阅读 [本机使用说明](../README-本机使用版.md)。

## 模块定位

| 要处理的内容 | 现有实现 |
|---|---|
| 身份与基础档案 | [accounts](../apps/accounts/)、[masterdata](../apps/masterdata/)、[audit](../apps/audit/) |
| 编码、主档及标签 | [coding](../apps/coding/)、[assets](../apps/assets/) |
| 金额与折旧 | [finance](../apps/finance/)，计算在 domain，业务动作在 services |
| 导入与导出 | [imports](../apps/imports/)、[reports](../apps/reports/)；低值物品复用这两处 |
| 盘点、保养及清退 | [inventory](../apps/inventory/)、[maintenance](../apps/maintenance/)、[offboarding](../apps/offboarding/) |
| 数量型低值物品 | [supplies](../apps/supplies/) 的领域计算、服务、权限与核对 |
| 部署及恢复 | [operations](../apps/operations/)、[deploy](../deploy/)、[本机脚本](../scripts/local/) |

按本次修改选择模块和相关测试，不要求每次遍历全仓。设计参考、历史任务及验证记录均从 [文档索引](README.md) 进入。

## 版本与依赖

- Python：>=3.14.7,<3.15（当前镜像固定为 3.14.7）
- Django：5.2 LTS，项目范围 >=5.2,<5.3，精确锁定 5.2.17
- PostgreSQL：支持 16–18，生产镜像与客户端精确固定为安全修复版 18.6
- psycopg：3.3 系列，精确锁定 3.3.4
- pytest：9.1 系列，精确锁定 9.1.1
- pytest-django：4.14 系列，精确锁定 4.14.0
- Bootstrap：5.3.8，本地静态文件
- HTMX：2.0.10，本地静态文件
- openpyxl：3.1.5，用于生成并解析无宏 XLSX
- defusedxml：0.7.1，作为 XML 解析安全加固依赖
- Pillow：12.3.0，用于附件图片真实解码、格式确认和像素上限保护
- qrcode：8.2，用于本地生成带安静区的 SVG 二维码，不调用外网二维码服务
- gunicorn：26.1.0，生产 Docker 容器中的 WSGI 服务
- Caddy：2.11.4，生产 LAN HTTPS 反向代理
- cryptography：50.0.0，用于 AES-256-GCM 加密备份包

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
$env:EAM_ENVIRONMENT = "development"
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
$env:QR_BASE_URL = "https://eam.company.lan"
$env:SECURE_SSL_REDIRECT = "false"
$env:SESSION_COOKIE_SECURE = "false"
$env:CSRF_COOKIE_SECURE = "false"
$env:TRUST_PROXY_SSL_HEADER = "false"
$env:SECURE_HSTS_SECONDS = "0"
$env:BACKUP_ROOT = "var/backups"
$env:BACKUP_TEMP_ROOT = "var/backup-tmp"
$env:BACKUP_MIRROR_ROOT = "var/backup-mirror"
$env:BACKUP_PG_MODE = "docker"
$env:BACKUP_POSTGRES_CONTAINER = "eam-lite-sprint0-pg"
$env:BACKUP_KEY_FILE = "var/local/backup_key.txt"
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
生产反向代理配置见 deploy/ 和运维说明。

生产环境必须使用随机 Secret、DEBUG=false、准确的内网域名和 HTTPS 安全开关。
生产部署和恢复方式见 deploy/、当前运维说明及
docs/09-Security-Backup-and-Deployment.md 的相关设计参考。

## PostgreSQL 准备

以下简化命令仅用于开发/测试，由有建库权限的数据库管理员执行，密码通过交互提示输入：

~~~powershell
createuser --pwprompt eam_lite
createdb --owner=eam_lite --encoding=UTF8 eam_lite
~~~

上述兼任数据库 owner/迁移账号的 eam_lite 只用于开发。生产 Compose 由
`deploy/postgres-init.sh` 分离 bootstrap、migration 与 runtime 三个身份，release 步骤迁移后
执行 `grant_runtime_database_privileges`，并撤销关键历史表 DELETE/TRUNCATE。运行 pytest
时 Django 会创建并删除独立测试数据库，因此测试账号需要
CREATEDB；不要把这项权限授予生产 runtime 账号。

当前 PostgreSQL 门禁使用的 transaction-local 自定义 GUC 只是受控 Service 的完整性与
防误写标记；当同一个数据库身份既能直接修改业务表又能自行 `set_config` 时，它不构成
安全授权边界。生产上线前必须使用非登录 owner/独立迁移身份与最小权限 runtime 身份，
撤销 runtime 对敏感表的非必要直接 DML；确需的写入应通过经过评审、固定 `search_path`
且严格授权的 `SECURITY DEFINER` 入口完成。当前 Compose 已完成身份分离但尚未把全部关键
写操作收敛为 SECURITY DEFINER；此项仍记录为正式上线阻断，不影响本地验收使用。

## Windows 本机稳定版与开发环境

开发环境允许在工作区 `var/local/dev-state-root.txt` 指定一个已存在的本机运行目录。该配置只影响开发环境；用于迁移运行配置时，应原样保留五个密钥文件并验证新容器读到的内容一致。未指定时仍使用 `%LOCALAPPDATA%/EAM-Lite/dev`。启动先用运行身份执行只读迁移检查；需要迁移时仍使用原 release 身份，已有用户不重复初始化。

普通使用者与开发人员的一键启动、目录隔离、局域网扫码测试、便携备份和同步步骤集中在 [本机使用说明](../README-本机使用版.md)。本指南以下源码命令从仓库根目录执行，不要求 Windows 发行包用户另装虚拟环境。

## 生产部署、备份与恢复

生产固定使用 `deploy/compose.yaml`、`deploy/Dockerfile`、`deploy/Caddyfile` 和仓库外 Secret；
不得使用 runserver。完整的 DNS/CA、构建、release、启动停止、自动备份、30 日保留、手动
下载、隔离恢复、回滚与监控步骤见：

- `docs/Sprint-12-Operations-Runbook.md`
- `docs/Sprint-12-UAT-Evidence.md`

每日自动任务运行：

~~~bash
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml --profile backup run --rm backup
~~~

system_admin 也可从“系统设置 → 数据备份”生成手动加密备份。备份口令不会保存；浏览器
下载使用当前密码复核和一次性短时授权。隔离恢复只允许目标名称含 restore/uat/test 的全新
数据库及空附件目录，命令见运维手册。
下载进入 `started` 后会持有文件租约；租约有效到授权 `expires_at` 再加一个
`BACKUP_DOWNLOAD_GRANT_MINUTES` 宽限期。到期任务跳过有效租约，超过该边界的僵死
`started` 授权会原子标记失败后再回收备份。

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

Sprint 4 的 `masterdata.0006–0008` 增加固定资产会计类别、实物分类默认折旧政策和
`SequenceCounter` 永久/单调约束；`finance.0001–0006` 建立财务、政策、Profile、计划、
事件、工作量、批次、实际分录、调整、理论试算和正式化幂等结果，并安装 PostgreSQL
跨公司、确认历史不可变和来源一致性门禁；`assets.0003–0004` 增加随机二维码身份，并
把唯一受控正式化迁移扩展为 `pending_finance → pending_label`。所有迁移保留 Sprint 3
数据；SQLite 不能验证发号锁、延迟约束或并发。

Sprint 6 的 `assets.0006_sprint6_qr_labels` 建立打印批次、不可变标签快照、明细和最小
`label_activation` 变动历史，并扩展 PostgreSQL 门禁以保护打印、贴标、换标和
二维码状态；`assets.0007_sprint6_label_attachment_idempotency` 保存不可变的贴标请求摘要，
确保同一幂等键只有相同资产、二维码版本和目标状态才能重放。
`pending_label → in_use/idle` 的原子状态机；`audit.0004_postgresql_audit_actor_set_null`
在保持审计其他字段只追加的同时，允许用户外键按模型约定安全 `SET_NULL`。

## 基础资料与初始化

登录后从“初始化向导”或 `/setup/` 建立以下资料并完成检查：

1. 公司；
2. 部门；
3. 人员；
4. 实物分类；
5. 位置；
6. 编码方案；
7. 财务政策；
8. 用户、固定角色及部门数据范围；
9. 完成初始化检查。

步骤状态由真实数据和权限条件重新计算。全部条件满足时，`system_admin` 才能原子完成
初始化。`system_admin` 协调公司、
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
公式、外部链接和嵌入对象，当前该流程未集成独立杀毒引擎。文件存储与数据库不是同一
事务资源；常规数据库失败会同步删除刚保存的对象，进程在两者之间异常终止时只可能留下
不可公开、无数据库引用的私有文件，由下述幂等清理命令处理。

### 导入 staging 与私有文件清理

清理入口为 `cleanup_import_staging`，仅允许可登录的 `system_admin` 作为 `--actor`。
命令默认只做 dry-run；真实删除必须显式提供 `--execute`，并建议为每次计划任务传入唯一
`--task-id`。自动批次清理只处理超过保留期且从未映射正式对象的 `uploaded`、`invalid`、
`failed`；默认和最小保留期都是 30 天。`validated` 永不自动清理，`confirmed` 批次、行、
映射、源附件和摘要永不删除。每个候选都会锁定 Batch 及 Row、取得与上传/确认相同的公司
幂等锁，并在删除前重新检查状态、created 映射和处理中幂等请求。

当前清理命令没有为“附件成为孤儿后的保留期”“遗留上传临时文件时限”和“无元数据私有文件
保留期”设置统一默认值，也不从 `.env` 推断。按实际保留需要和操作授权确定正整数后，
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

## 源码部署的恢复管理员与首批应用账号

本节是手工源码部署路径。Windows 本机启动器在空用户库中通过 `bootstrap_local_admin` 创建普通应用管理员，不要求普通使用者先创建 superuser；见 [本机操作说明](../README-本机使用版.md)。

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

首批 `system_admin` 建立后，后续普通应用用户可在“系统设置 → 用户权限 → 新增用户”中
创建。页面要求当前系统管理员再次输入本人密码，只允许选择固定角色；创建部门负责人时必须
同时配置一个启用部门范围。用户、初始范围、角色和 AuditLog 在同一事务内提交，页面不会
创建 Django staff、superuser 或自定义权限。

## 启动

开发环境启动：

~~~powershell
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
~~~

浏览器打开 http://127.0.0.1:8000/login/。登录页和首页使用中文；未登录访问首页会
跳转到登录页；停用用户不能登录；退出只接受带 CSRF 保护的 POST。

runserver 仅用于开发，不能作为生产进程。

## 自动测试

### 归还与来源更正的迁移兼容性

`assets.0020_custody_and_origin_corrections` 增加追加式撤销记录，将租入归还关联扩展为可保留多次登记的历史，并保存归还终止保养计划前的状态。原归还记录、编号、二维码和金额字段保留。

`assets.0021_custody_correction_guards` 维护有效来源关系、单一有效归还及其撤销的数据库约束。升级时，仍启用或暂停的历史归还资产保养计划会终止，原状态写入新快照字段，并追加迁移审计。旧归还的原始变动时间保留，历史报表依据实际归还日期解释业务状态。

已有撤销或保养联动记录后，直接降级会被拒绝，避免丢弃新增历史。版本回退须使用升级前数据库、附件备份及对应代码。恢复操作沿用项目既有隔离恢复流程。

`masterdata.0015_remove_asset_category_type` 的审计依赖已明确为包含公司字段的 `audit.0002_auditlog_company`，使历史版本回退能正常保留分类审计；该调整不再次移除或更改分类数据。

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

基础审计通过 apps.audit.services.write_audit_log 及其受控包装入口写入。Service 递归处理
Decimal、日期、时间和 UUID，并对 password、Secret、Token、Cookie、Session、
Authorization、CSRF、API key 和私钥字段统一脱敏；调用者还能显式排除字段。
AuditLog 未注册普通编辑/删除入口，模型及 QuerySet 均拒绝更新和删除。

尚未建立 Company 的预初始化阶段，Audit Service 只允许固定白名单内的登录、安全和
首批账号事件使用 `company=None`；没有自由文本或假外键。一旦存在任意活动或停用
Company，通用入口也拒绝 NULL company。现有迁移已建立
`company -> Company` 的 nullable `PROTECT` 外键；所有业务事件必须使用真实 Company。

## 编码规则

system_admin 可在“基础资料 → 编码规则”建立草稿、维护片段、预览 1 或 10 个示例、启用、
设置公司唯一默认版本、退役和克隆新版本。finance 与 management 只读。编码格式、固定日期
输出、五种 reset 模式和规范 `scope_key` 均由 `apps.coding.domain` 集中实现；物理分类只能
选择同公司且当前生效的活动方案。

生效期为上海业务日闭区间，结束日当天仍有效。启用方案必须有且只有一个 sequence，
`sequence_start` 是未来首次签发值；预览只在内存中从该值或计数器只读快照模拟，绝不创建、
锁定或更新 `SequenceCounter` / `IssuedCode`。`format_string` 与 `custom_field` 不在页面、URL、
命令或 Service 中提供。初始化向导步骤 6 只有当前生效且唯一的公司默认方案存在时才通过。
无消耗预览仍绝不写计数器或正式编号；正式发号只由 Sprint 4 的资产财务确认事务调用，
编码配置页面、命令和独立 API 都没有发号入口。

## 实物建档与后补附件

新资产主操作为“建立资产”，资料未齐可暂存草稿。正式实物建档通过 `apps.assets.registration` 在同一事务中验证权限、公司/责任范围和必填物理资料，调用共享编码引擎，建立 AssetRegistration、编号历史及二维码；照片和财务确认不再是前置条件。

建档后进入待贴标，打印和逐项贴标后按原流程启用。A0 照片可以后补，A1 发票仍按财务权限上传和下载。重复请求校验请求标识及资料摘要，不重复建立资产或占号；任一步失败回滚整笔实物建档。

原有草稿、导入 staging、附件私有存储和权限校验继续使用。更多细节见 [实物建档与财务分离](Asset-Registration-and-Finance-2026-09-09.md)。

## 财务确认与折旧

财务工作台列出已建档、尚未确认财务与折旧的资产。财务资料可先保存或试算；资料齐备后确认会计认定、原值、可用日期和折旧参数。确认不会重新发号或更换二维码，不修改已建档资产的使用状态。待确认资产不生成实际折旧，也不以零成本计入 T+ 对账。

实物登记使用独立 AssetRegistration；FinanceFormalizationRequest 保留为财务确认的幂等记录和旧调用兼容。旧 pending_finance 草稿仍可处理，正常新建流程已经不再经过财务复核关卡。

折旧默认全部来自版本化 `DepreciationPolicy`，SystemSetting 只保存 finance 可写的
`fixed_asset_warning_amount`；5,000 CNY 是可配置提示，不自动认定固定资产。政策支持
`straight_line`、`units_of_production`、`double_declining_balance`、
`sum_of_years_digits`、`manual`、`no_depreciation`，金额全程使用 Decimal，实际入账按
`ROUND_HALF_UP` 到 2 位并由最后一期直接消除尾差。Profile 已确认后不得原地覆盖；参数
变化建立新生效版本，旧 Schedule/Entry 保留。

工作量法要求先按期间录入同单位工作量；手工折旧在生成批次时通过结构化 JSON 提供资产
UUID、十进制字符串金额和原因，明确 0 也必须有原因。普通月度/年度折旧、工作量和手工法
都通过“生成试算 → 检查明细 → 原子确认”批次入账；错误明细阻断整批。已确认实际分录只
追加，错误通过精确反向批次或价值调整冲销处理，原行不删除。实际累计折旧只等于所有已
确认原始及反向 `DepreciationEntry.amount` 的代数和；计划和理论试算不会进入账面。

初始化步骤 7 只有当前生效且唯一的公司默认折旧政策与 finance 明确保存的提示金额同时
通过同一验证器才完成；`system_admin` 只能查看/协调，不能代填财务值。步骤 9 会重新查询
公司、部门、人员、实物分类、具体位置、编码方案、财务规则、应用用户和部门范围九项真实
条件，全部通过后才原子写 `initialization_completed`、完成者、时间和审计。失败时不产生
部分完成，页面按每项提供修复链接。

## 二维码标签

生产或验收环境必须将 `QR_BASE_URL` 配为批准的局域网 HTTPS 应用根地址；它不得包含用户
凭据、查询参数、片段或额外路径。二维码只保存该根地址下的 `/assets/scan/<随机 Token>/`
入口，不嵌入资产名称、编号、责任人、位置或财务信息。扫码仍要求登录并按公司、角色和
部门/本人对象范围鉴权；扫码响应禁止缓存和 Referer，日志格式化器会遮蔽扫码路径中的完整
Token。

正式环境会拒绝以 IP、`localhost`、临时电脑名或非标准端口作为 `QR_BASE_URL`，并要求该
固定 DNS HTTPS 根地址与 `ALLOWED_HOSTS`、`CSRF_TRUSTED_ORIGINS` 精确一致。服务器迁移时
保持 DNS 名称不变，只在验收后切换 DNS 指向，因此既有正式标签无需因更换电脑而改变 URL。
Windows 一键开发启动默认使用的 HTTP/IP 二维码仅供验收，打印页会明确标记“本地验收 · 部署后
重印”。开发启动器可通过用户环境变量 `EAM_DEV_DURABLE_QR_BASE_URL` 改用将来正式环境的固定
HTTPS 域名，并同步加入开发环境允许主机及可信来源；这只改变二维码指向，不提供 DNS、证书
或代理。先在实际手机上验证域名可达，完整恢复含二维码身份的数据库后，正式环境继续使用同一
域名，再将 DNS 指向新服务器。旧 HTTP/IP 标签不能仅靠数据库迁移变成固定域名标签；必要时
执行换标以吊销已经流出的旧地址标签。

finance、equipment、warehouse 可从“标签打印与贴标”选择待打印资产。生成 A4 预览时，后端
在同一事务中保存标签快照、页码和位置，记录打印操作人和时间，并把批次、明细和当前二维码
置为 `printed`。不再要求返回页面进行二次“确认打印”。每页 24 张，QR 按 100% 打印为
22 mm，并保留安静区；操作记录不能证明纸张实际输出，实际贴标仍需逐项核对。

已打印但未贴标可明确重印并沿用当前 Token；已贴标资产通过换标吊销旧 Token，再打印和确认
新标签。旧版本遗留的 `generated` 批次保留继续打印或取消入口，不作为新批次的常规步骤。

贴标确认支持扫描当前二维码，或从受权限保护的 Web 页面逐项查看二维码并勾选实物核对。
两类入口使用同一服务重新校验当前身份、打印记录、权限和责任资料，并保存确认方式。
首次贴标在同一 PostgreSQL 事务中写入唯一、追加式 `label_activation` Movement，保存当时
部门、责任人和位置快照，将资产从 `pending_label` 转为明确选择的 `in_use` 或 `idle`，并把
二维码改为 `attached`；换标确认不改变既有业务状态。各项关键操作记录审计但不保存完整 Token。
所有页面、SVG 生成和打印样式均由本应用本地提供，不依赖 CDN、远程字体或外网 QR 服务。
