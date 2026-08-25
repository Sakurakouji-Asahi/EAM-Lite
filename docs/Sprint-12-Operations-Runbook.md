# EAM-Lite Sprint 12 运维手册

本手册固定 V1.1 的生产形态：Docker Compose + Gunicorn + Caddy + PostgreSQL 18。
生产只部署到批准的公司 LAN，不开放公网，不使用 `manage.py runserver`。

## 1. 角色与目录

- 系统负责人：批准发布、恢复点与切换时间。
- `system_admin`：应用内备份、权限和审计检查。
- 数据库迁移人员：只在发布窗口使用 migration 账号。
- runtime 账号：无 DDL/CREATEDB/CREATEROLE，敏感历史表无 DELETE/TRUNCATE。
- 备份保管人：管理 NAS/离线副本与独立解密口令。

代码采用只读发布镜像。持久目录：PostgreSQL volume、受保护附件 volume、备份暂存
volume、独立 NAS bind mount。TLS/数据库/备份密钥文件均位于仓库外。

## 2. 首次准备

1. 为服务器设置固定内网地址和 DNS，例如 `eam.company.lan`；禁止路由器端口映射、
   UPnP 和公网隧道。
2. 创建三个不同的随机数据库口令：bootstrap admin、migration、runtime。
3. 创建随机 `SECRET_KEY` 和独立备份密钥；每项至少 50 个随机字符，文件权限仅部署
   服务账号可读。
4. 准备独立 NAS/备份设备挂载目录。与应用主机同盘的另一目录不算独立副本。
5. 从 `deploy/compose.env.example` 复制仓库外配置文件，填写真实 DNS、当前 Git
   commit、三类数据库口令文件、Secret 文件、备份密钥文件和 NAS 路径。
6. 防火墙仅允许批准 LAN 到 TCP 443；80 只用于重定向。不得发布 5432 或 Gunicorn
   8000。

Compose 首次初始化会建立 admin/migration/runtime 三个 PostgreSQL 身份。应用只使用
runtime；迁移和权限刷新只使用 migration；bootstrap admin 不提供给应用。

## 3. 构建与首次发布

在版本化、干净的发布目录执行：

```bash
git status --short
git rev-parse HEAD
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml config --quiet
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml build app
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml up -d db
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml --profile release run --rm release
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml up -d app caddy
```

`release` 是唯一迁移步骤，顺序执行 migrate、collectstatic 和 runtime 权限刷新；Web
worker 不执行迁移。发布失败时不得继续启动不兼容应用。

从 Sprint 11 首次升级到 Sprint 12 时，operations 表尚不存在，必须先在维护窗口使用
PostgreSQL 18 `pg_dump -Fc --no-owner --no-privileges` 和附件归档建立迁移前恢复点，验证
`pg_restore --list` 与 SHA-256 后才执行 release。之后的版本可使用第 6 节受控备份命令。

## 4. LAN HTTPS

Caddy 使用内部 CA 签发 LAN 证书。把 Caddy 生成的根证书通过组织批准方式安装到所有
Chrome、Edge 和手机；出现证书警告时停止验收，禁止点击“继续访问”。确认全部终端受信任
后再逐步提高 `SECURE_HSTS_SECONDS`；不要在内部证书尚未分发时启用 preload。

验收：

```bash
curl --cacert /path/to/caddy-root.crt https://eam.company.lan/healthz/
```

返回仅允许 `{"status":"ok"}`。响应必须含 HSTS、nosniff、frame、CSP 和 correlation ID。

### 4.1 二维码地址与服务器迁移

- `EAM_HOSTNAME` 必须是固定的逻辑服务 DNS 名称，不得填写服务器 IP、`localhost`、个人
  电脑名或带端口地址。生产配置不满足时应用必须拒绝启动。
- `QR_BASE_URL`、`ALLOWED_HOSTS`、`CSRF_TRUSTED_ORIGINS` 必须指向同一个 HTTPS 名称。
- 更换服务器时保持该 DNS 名称不变；先在隔离地址完成恢复和扫码验证，再切换内部 DNS，
  不重新编码既有正式标签。
- 使用 Windows 一键本地启动生成的 HTTP/IP 标签均为临时验收标签。正式切换前重新生成并
  打印；已经流出且可能继续被使用的旧标签应走换标流程吊销旧 Token。
- 最终服务器必须至少完成三次“关机—开机—服务自动恢复—手机冷启动扫码—登录回跳”实测，
  并由 Android、iPhone（如均在批准范围）分别保存证据，未通过不得签署 UAT-010/UAT-027。

## 5. 启动、停止与日志

```bash
# 状态
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml ps

# 启动/重启
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml up -d
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml restart app caddy

# 停止（保留数据 volume）
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml stop

# 日志
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml logs --since 1h app caddy db
```

不要执行 `down -v`、删除 PostgreSQL volume、覆盖 media 或对生产库 `reset --hard`。

## 6. 自动备份与保留

每日计划任务执行：

```bash
docker compose --env-file /etc/eam-lite/compose.env -f deploy/compose.yaml \
  --profile backup run --rm backup
```

它生成 PostgreSQL custom dump、附件 tar、逐文件大小/SHA-256、迁移列表和版本清单，
整体以 AES-256-GCM 加密；本机暂存与 NAS 镜像摘要一致后才标记 completed。随后仅把超过
30 天的 completed 集标记 expired 并删除文件，BackupSet/AuditLog 元数据永久保留。
命令任何失败均返回非零，计划任务必须告警。每天另检查最近 completed 备份不超过 24 小时、
大小无异常且 NAS 剩余空间充足。

浏览器手动备份位于“系统设置 → 数据备份”，仅 system_admin 可生成/查看/下载。创建时需
重输当前密码并提供不会保存的加密口令；下载再次重输密码并使用一次性、短时、单用户授权。
下载中断记录 failed，不伪记完成。浏览器下载不替代每日自动备份。

## 7. 隔离恢复演练

绝不覆盖唯一生产库。选择 completed 备份，在隔离主机/网络准备空数据库名（名称含
`restore`、`uat` 或 `test`）和空附件目录：

```bash
EAM_BACKUP_PASSPHRASE_FILE=/run/secrets/backup_key
python manage.py verify_eam_backup <backup-uuid> --passphrase-file "$EAM_BACKUP_PASSPHRASE_FILE"
python manage.py restore_eam_backup <backup-uuid> \
  --passphrase-file "$EAM_BACKUP_PASSPHRASE_FILE" \
  --target-database eam_lite_restore_YYYYMMDD \
  --target-media-root /var/lib/eam-lite-restore/media \
  --confirm-isolated
```

源业务库不可用时，不依赖 BackupSet 行，直接从 NAS 包恢复：

```bash
python manage.py verify_eam_backup --package-file /nas/backups/<id>.eambak \
  --expected-sha256 <manifest-recorded-sha256> --passphrase-file /run/secrets/backup_key
python manage.py restore_eam_backup --package-file /nas/backups/<id>.eambak \
  --expected-sha256 <manifest-recorded-sha256> --passphrase-file /run/secrets/backup_key \
  --target-database eam_lite_restore_YYYYMMDD \
  --target-media-root /var/lib/eam-lite-restore/media --confirm-isolated
```

恢复命令拒绝当前数据库、任意名称、已存在数据库和非空附件目录；验证包、内部 dump、
附件归档及每个附件 SHA-256。恢复后记录 migrations、资产、审计和附件数量。

随后以恢复库启动匹配 commit 的应用并抽查：八角色登录与拒绝、资产/编号/QR、最近折旧、
盘点、保养、清退、处置、随机至少 10 个附件、一个报表。记录开始结束时间、备份完成到
事故点的 RPO、实际 RTO、执行人和问题。首次上线前及以后至少每季度执行。

## 8. 发布升级与回滚

发布顺序：维护提示/暂停写入 → 当前版本自动备份并验证 → 构建不可变镜像 → 单一 release
迁移 → 启动 app/caddy → health → 登录/资产查询/权限/附件/只读报表冒烟 → 退出维护。

代码回滚必须先确认迁移向后兼容；不得盲目反迁移。需要数据恢复时由系统负责人批准 RPO
损失窗口，先在隔离环境验证，再切换 DNS/服务指向；旧实例只读保留。

## 9. 监控与故障排查

至少监控：HTTPS/health、5xx、登录失败、数据库连接、CPU/内存、磁盘、media、最后备份、
NAS、证书 30 天到期、NTP。错误页只显示 correlation ID；用该 ID 在受限应用日志和
AuditLog 定位，不向用户展示 SQL、路径或 Secret。

- health 失败：先看 app/db health 和连接，禁止反复迁移。
- 静态 404：重新运行 release 的 collectstatic，确认 Caddy static volume。
- 附件失败：检查 media volume 权限，不能临时公开 MEDIA_ROOT。
- 备份失败：保留 failed 元数据和暂存日志，修复 NAS/空间/密钥/pg_dump 后使用新幂等键。
- 证书失败：重新分发受信任 CA/证书，不允许用户绕过警告。

## 10. 上线停止条件

缺少受信任 HTTPS、独立备份目标、真实恢复演练、Chrome/Edge/手机/纸张 QR 证据、5000 项
性能证据、第二测试人或任一 P0/P1 未关闭时，结论必须是“暂不上线”。本手册和自动测试不能
替代业务负责人、财务、设备、HR 与系统负责人的最终签字。
