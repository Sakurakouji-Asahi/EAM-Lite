# EAM-Lite

EAM-Lite 是公司局域网内使用的轻量级企业资产管理系统。本仓库根目录就是包含
AGENTS.md、docs/、tasks/ 和 manage.py 的当前目录，不存在第二层项目仓库。

当前代码只完成 Sprint 0：Django 项目骨架、自定义用户、八个固定角色、登录/退出、
受保护首页、首批账号引导和追加式审计基础。Company、资产、财务、折旧、盘点等业务
模型尚未开始。

## 版本与依赖

- Python：>=3.14.7,<3.15（本 Sprint 验证版本 3.14.7；不使用 3.15 预览版）
- Django：5.2 LTS，项目范围 >=5.2,<5.3，精确锁定 5.2.17
- PostgreSQL：支持 16–18，本 Sprint PostgreSQL 验证版本 18.4
- psycopg：3.3 系列，精确锁定 3.3.4
- pytest：9.1 系列，精确锁定 9.1.1
- pytest-django：4.14 系列，精确锁定 4.14.0
- Bootstrap：5.3.8，本地静态文件
- HTMX：2.0.10，本地静态文件

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

### 显式 SQLite 快速测试

SQLite 只允许非并发本地开发和快速测试，不能关闭任何 PostgreSQL 迁移、锁、并发或
数据库约束验收项。项目不会因 PostgreSQL 配置错误自动回退 SQLite。必须显式设置：

~~~powershell
$env:DB_ENGINE = "sqlite"
$env:SQLITE_PATH = ":memory:"
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

Sprint 0 尚无 Company。Audit Service 只接受 company=None 的预初始化登录、安全和
首批账号事件；没有自由文本或假外键。Sprint 1 建立 Company 后，必须用跟踪迁移增加
company -> Company 的 nullable PROTECT 外键，并收紧业务事件的公司必填规则。

## Sprint 0 明确未实现

未创建 Company、Department、Employee、Location、AssetCategory、Asset、附件、
编码、财务、折旧、导入、二维码、盘点、保养、离职、处置、报表、T+、生产部署或
备份任务。后续功能只能在对应 Sprint 获得明确授权后开始。
