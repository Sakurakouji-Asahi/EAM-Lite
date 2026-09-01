# EAM-Lite Windows 本机使用版

本机使用版适合在一台 Windows 10/11 电脑上日常使用 EAM-Lite。它只监听
`127.0.0.1`，不会向公司局域网或公网开放，也不能替代公司正式 LAN HTTPS 服务器部署。

## 1. 电脑要求

- Windows 10 或 Windows 11；
- Docker Desktop；
- 建议至少 8 GB 内存和 20 GB 可用磁盘空间；
- 首次下载镜像或更新时需要访问 GitHub/GHCR。

从 GitHub Release 下载的 Windows ZIP 不需要安装 Python、PostgreSQL、Git、
虚拟环境或项目依赖。

## 2. 下载和解压

1. 在 GitHub Releases 下载 `EAM-Lite-v<版本>-Windows.zip` 和同名
   `.sha256` 文件。
2. 核对 ZIP 的 SHA-256。
3. 将 ZIP 完整解压到普通文件夹。不要直接在压缩包预览窗口里运行。
4. 路径可以有中文、空格，也可以位于任意盘符。

## 3. 第一次启动

双击 `启动EAM-Lite.cmd`。脚本会：

1. 检查 Docker Desktop；未运行时尝试启动并等待；
2. 校验 Release 清单、版本、commit 和镜像 digest；
3. 在当前 Windows 用户的本地应用目录生成随机 Secret；
4. 启动独立 PostgreSQL；
5. 由一个 release 步骤执行迁移、静态文件和最小数据库权限；
6. 首次空库时在黑色窗口中要求输入管理员用户名、显示名称和密码；
7. 启动 Gunicorn 和本机 Caddy，再验证 `/healthz/` 与 `/version/`；
8. 打开 `http://127.0.0.1:8765`。

管理员密码不会显示，也不会写入命令行、脚本、日志或配置。首个账号是普通应用
`system_admin`，不是 Django superuser；创建账号和 AuditLog 在同一数据库事务完成。

## 4. 日常启动与停止

- 启动：双击 `启动EAM-Lite.cmd`。
- 查看状态：双击 `查看EAM-Lite状态.cmd`。
- 停止：双击 `停止EAM-Lite.cmd`。

重复启动时，如果同一 commit 已健康运行，只会打开浏览器，不重复执行 release。
停止只停止容器，不删除 PostgreSQL、附件或备份卷。不要手工执行
`docker compose down -v`，也不要删除 Docker Desktop 中以 `eam-lite-local` 开头的卷。

## 5. 第一次登录后的初始化

使用刚创建的管理员登录，按系统初始化向导建立公司、部门、员工、分类、位置、编码方案、
财务政策、权限和业务用户。`system_admin` 不自动拥有财务或 HR 权限；同一人员兼任时仍需
显式分配对应角色。

## 6. 稳定使用版和开发环境

稳定使用版：

- 地址 `http://127.0.0.1:8765`；
- Compose project `eam-lite-local`；
- 只运行正式 Release，或干净且与 `origin/main` 精确一致的 Git main/tag；
- `DEBUG=false`，使用 Gunicorn；
- 数据库、附件和备份卷均为稳定版专用。

开发环境：

- 地址 `http://127.0.0.1:8766`；
- Compose project `eam-lite-dev`；
- 可运行开发分支和未提交代码；
- 页面顶部始终显示“开发环境”；
- 数据库、数据库名、附件、临时文件、备份阶段、容器、项目名和端口均与稳定版分离。

开发人员双击 `启动开发环境.cmd` / `停止开发环境.cmd`。开发环境不会自动复制稳定数据，
也不会连接 `eam-lite-local` 的卷。

## 7. 数据和 Secret 在哪里

Docker 持久卷（不要删除）：

- `eam-lite-local_postgres_data`：PostgreSQL 数据；
- `eam-lite-local_media_data`：附件；
- `eam-lite-local_backup_stage`：应用生成的受保护备份阶段文件；
- 其他 `eam-lite-local_*`：静态、临时和备份临时卷。

当前用户本机配置：

`%LOCALAPPDATA%\EAM-Lite\local\`

其中包含随机 Secret、Compose 环境文件、版本标记和更新日志。不要提交 Git、发送给他人或
用其他电脑的文件覆盖。开发环境使用单独的 `%LOCALAPPDATA%\EAM-Lite\dev\`。

## 8. 创建便携备份

双击 `备份EAM-Lite数据.cmd`，输入并再次确认迁移密码。默认在
“文档\EAM-Lite备份”生成单个文件，例如：

`EAM-Lite-数据-20260901-083000-v0.2.1.eambak`

包内包含 PostgreSQL custom dump、附件归档、应用版本、BUILD_COMMIT、已应用 migrations、
关键记录数量、逐文件 SHA-256、创建时间、业务时区和包格式版本。整个包使用
AES-256-GCM 加密，密钥由迁移密码、随机盐及 PBKDF2-HMAC-SHA256（60 万次）派生。
脚本在完成前会校验外层和内部 SHA-256、`pg_restore --list`、附件成员和逐文件哈希。

迁移密码：

- 不回显；
- 不作为命令行参数；
- 不写入日志或 Git；
- 只短暂进入当前用户专用临时文件，使用后覆盖并删除；
- 不保存在 `.eambak` 中。

请把 `.eambak` 复制到另一块磁盘或受控备份设备。只保存在同一电脑不算独立备份。
丢失迁移密码后，无法恢复加密包。

## 9. 迁移到另一台电脑

1. 在新电脑安装并启动 Docker Desktop。
2. 下载同版本或更新的 EAM-Lite Windows Release 并解压。
3. 把 `.eambak` 复制到新电脑。
4. 在尚未建立业务数据的全新本机实例中双击 `恢复EAM-Lite数据.cmd`。
5. 选择文件并输入相同迁移密码。
6. 脚本验证包格式、AES-GCM 完整性、SHA-256、dump、附件、版本和 migration 清单。
7. 只有目标 PostgreSQL 没有任何业务表且附件卷为空时才恢复；发现已有数据会拒绝覆盖。
8. 恢复后运行当前迁移、库存余额核对、耐用品保管核对、健康检查、登录页和版本检查。

用户、密码哈希、公司、员工、资产、低值物品、余额、流水、保管、AuditLog 和附件都会随包
迁移。旧 Session 可以失效，用户使用原账号密码重新登录。脚本不会直接复制 PostgreSQL
原始数据目录。

## 10. 更新稳定使用版

双击 `更新EAM-Lite.cmd`。

- Git clone 模式只允许干净 main，先建立便携备份，再 fetch、`pull --ff-only`、构建精确
  commit 镜像、执行 release、验证健康与版本；不自动合并或 rebase。
- GitHub Release 模式先备份，再下载最新正式 Release ZIP 和 SHA-256，解压到新的版本目录，
  使用清单中的精确镜像 digest；成功后才切换当前版本指针。
- 更新日志记录旧/新 commit、备份文件和 migration 状态。
- 失败时保留数据库、附件、备份、旧镜像和旧版本目录，并显示恢复点，不自动覆盖备份。

## 11. 二维码和本机地址

本机稳定地址是 `http://127.0.0.1:8765`，只代表“这台电脑本身”。手机中的
`127.0.0.1` 是手机自己，因此手机不能用本模式扫描访问电脑。

如将来需要手机扫码，应另行部署批准的公司 LAN HTTPS 模式和固定逻辑主机名。迁移到另一台
电脑不会自动保证旧纸质二维码地址可访问；恢复脚本不会静默重写二维码 Token。

开发验收时可双击 `启动开发环境-局域网扫码测试.cmd`，临时把独立开发环境发布到当前
局域网。启动窗口会显示手机访问地址；手机必须连接同一网络。该模式使用当前电脑 IP，IP
变化后应重新启动并重新生成/打印测试二维码，不用于正式纸质标签。使用普通
`启动开发环境.cmd` 会把开发环境恢复为仅 `127.0.0.1:8766` 可访问。

## 12. 不要删除或复制的内容

- 不删除任何 `eam-lite-local_*` Docker volume；
- 不删除 `%LOCALAPPDATA%\EAM-Lite\local\`；
- 不把数据库卷目录、附件卷或本机 Secret 当作迁移方式；
- 不把 `.eambak`、`.env`、Secret、数据库文件或附件提交 Git；
- 不使用 `latest` 镜像替换 Release 清单中的 digest；
- 不把本机 HTTP 模式暴露到局域网或公网。

## 13. 常见问题

Docker 未启动：等待脚本自动启动；超时后手动打开 Docker Desktop，再重试。脚本不会恢复
出厂设置。

端口被占用：状态会显示进程名和 PID。请先确认用途并由用户自行处理；脚本不会结束未知进程。

数据库不健康：打开 Docker Desktop 查看 `eam-lite-local` 数据库容器日志；不要删除卷重试。

镜像下载失败：检查 GitHub/GHCR 网络后重试。Release 不依赖本机 Python 或 Git。

备份密码错误：AES-GCM 验证会失败，不会恢复任何数据。确认密码后在新的空实例重试。

目标数据库非空：脚本会拒绝。先为现有实例建立备份，然后改用新电脑或新的空本机实例。

版本不一致：运行 `查看EAM-Lite状态.cmd`，比较 VERSION、预期 commit 和容器 commit；再执行
更新或重新启动，不要让旧容器继续服务。

## 14. 本模式的安全边界

本机稳定版使用 PostgreSQL、最小运行账号、后端权限、审计、受保护附件、加密备份和
`DEBUG=false`。它只绑定 `127.0.0.1`，因此有意使用本机 HTTP。它不是公司 LAN 上线方案；
公司多终端/手机访问仍必须按 `docs/09-Security-Backup-and-Deployment.md` 部署受信任 HTTPS、
固定 DNS、防火墙、异机自动备份和恢复演练。

## 15. 日常建议

- 每天：双击启动，使用完可停止；
- 每次重要录入后或更新前：建立新的便携备份；
- 至少保留多个日期的备份，并在另一物理设备保存副本；
- 每季度在空实例实际恢复一次；
- 开发只使用 8766 开发环境，合并并人工验收后才由 main/tag 进入稳定版；
- 发现异常先查看状态和日志，不删除卷“重装”。

## 16. 技术支持所需信息

报告问题时提供：

- `查看EAM-Lite状态.cmd` 中的版本、commit 和容器状态；
- 发生时间和操作步骤；
- 相关错误文字或截图；
- 更新日志文件名（不要发送 Secret 或迁移密码）。

不要发送 `.eambak`、Secret 文件、数据库密码或真实附件，除非已经通过公司批准的安全渠道。
