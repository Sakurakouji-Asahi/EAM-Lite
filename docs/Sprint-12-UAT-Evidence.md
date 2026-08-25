# Sprint 12 UAT 与生产就绪证据

状态：进行中；当前结论 **暂不上线**。本文只记录已经实际执行的证据，未执行项不以自动
测试代替人工签字。

## 1. 环境

- 日期：2026-08-24，业务时区 Asia/Shanghai。
- OS：Windows 本地验收主机 + Docker Desktop 29.4.3。
- Python：3.14.7；Django 5.2.17；PostgreSQL 18.4。
- 基线提交：`57d29a1e7f36f3200387ae269fdfbcaf837aedb9`；Sprint 12 改动尚未形成最终提交。
- 数据：非生产 `eam_lite_sprint1_browser`，恢复到独立
  `eam_lite_sprint12_restore_20260824a`。

## 2. 自动回归

| 项目 | 结果 |
|---|---|
| 最终 PostgreSQL 全量（含 Sprint 12） | `1059 passed, 3 skipped`，780.29 秒 |
| 跳过说明 | 2 个 legacy SQLite direct-delete fixture；1 个 SQLite 读侧回归，PostgreSQL 由提交约束覆盖 |
| SQLite 专用补跑 | 上述跳过相关文件/节点 `9 passed`，26.79 秒 |
| Sprint 12 备份/安全/性能/settings 定向 | `43 passed`；资产列表 p95 0.4518s、Dashboard p95 0.4533s、详情 p95 0.1420s、500 标签 3.3475s |
| `pip check` | 无 broken requirements |
| `manage.py check --deploy`（实现前） | 仅 HSTS 未配置；Sprint 12 已增加 production fail-closed 配置，待最终复跑 |

## 3. 真实备份与恢复演练

迁移前恢复点：`var/pre-sprint12-20260824-092545`（不进入 Git），含 PostgreSQL custom dump、
附件归档与 SHA-256 manifest；`pg_restore --list` 通过。

受控备份：

- BackupSet：`BKP-20260824093511-3F45B4E9`
- 状态：completed；加密包 SHA-256：
  `e13e36d6313a72e510c192f0652a80a4219b15a9c134254248e8dd79790647e8`
- 数据库：PostgreSQL 18.4 custom dump；迁移 61 条。
- 最终逐文件清单备份：`BKP-20260824104825-A6E86DDE`，包 SHA-256
  `e72e0cdc0ba8f6f3bfd2f70ca0499baccb6c49ac596b9493ef911fc83c36e244`；
  10,731 个附件逐路径、大小、SHA-256 和清单摘要均进入加密包并验证。
- 本机暂存与镜像副本逐包摘要一致。

隔离恢复：

- 目标库：`eam_lite_sprint12_restore_20260824a`；目标附件为独立空目录。
- 首轮 RTO 10.16 秒；逐文件版和无源库包恢复分别约 17–20 秒，均小于 4 小时目标。
- 备份生成后立即恢复，实测 RPO 小于 1 分钟，小于 24 小时目标。
- 恢复结果：61 migrations、1 Asset、1 active QR、2 Attachment、79 原始 AuditLog；恢复后
  冒烟新增 1 条登录审计。
- 恢复应用抽样：home、资产总账、报表中心、备份页、当前 QR 扫码均 HTTP 200；最终无源库
  路径 `...restore_20260824d` 恢复 61 migrations、1 Asset、83 AuditLog、10,731 个附件，
  并逐文件验证大小和摘要。

## 4. UAT-001 至 UAT-030 状态

生产式隔离栈补充证据：

- pinned Python 3.14.7 / PostgreSQL 18.4 / Caddy 2.10.2 镜像构建成功；Gunicorn 26.1.0。
- 空库 release 全迁移成功，collectstatic 8 个本地文件；runtime 权限刷新成功。
- admin/migration/runtime 三角色已分离；migration/runtime 均非 superuser、无 CREATEDB/
  CREATEROLE，runtime 无 schema CREATE，AuditLog 无 DELETE，数据库和 Gunicorn 不发布主机端口。
- Caddy 内部 CA 链与主机名验证通过；HTTPS health 200，HSTS/CSP/nosniff/frame/referrer/
  correlation ID 齐全；HTTP 308；Bootstrap 静态文件通过 Caddy 返回 200。
- `check --deploy` 的 W005/W021（HSTS includeSubDomains/preload）在内部 CA 尚未向真实终端分发
  前刻意不启用；这是已处理但未关闭的上线门槛，不以静默忽略方式清除警告。
- production 自动备份 `BKP-20260824100357-870718C1`、NAS bind 镜像和验证命令通过。
- 最终生产镜像又生成 `BKP-20260824104018-73B09E5A`，随后以 DB admin 恢复身份、在不查询
  源 BackupSet 的情况下只从镜像 `.eambak` 恢复到全新 `eam_lite_restore_compose_20260824`；
  61 migrations、3 AuditLog、空附件清单均验证通过。
- `pip-audit 2.10.1` 首轮发现 sqlparse 0.5.5 的 4 项漏洞；升级锁定 0.6.0 后复扫为
  `No known vulnerabilities found`。

UAT-001–025、030 已有 Sprint 0–11 自动测试覆盖其服务、权限、事务、Excel、审计和负向路径，
但仍需本文第 3.1 节规定的业务测试人按批准数据人工执行并签字，不能仅凭自动测试标记 accepted。

| UAT | 当前证据 | 状态 |
|---|---|---|
| 001–009 | 初始化、分类、正式化、编码并发、责任/借还 PostgreSQL 回归 | 自动通过/待人工 |
| 010 | A4 22mm、认证、状态自动测试；实际纸张扫码尚未由设备测试人签字 | 待人工 |
| 011–025 | 附件、六方法折旧、盘点、保养、清退、处置、权限、导入、T+、审计回归 | 自动通过/待人工 |
| 026 | 数据库+附件 AES-GCM 备份、本机/镜像、逐文件清单、BackupSet 与无源库两种隔离恢复已实测 | 自动通过/待负责人签字 |
| 027 | 本地 HTTP LAN 可用；生产受信任 HTTPS/固定 DNS 尚无现场证据 | 阻塞 |
| 028 | 当前浏览器冒烟；Edge 独立证据尚无 | 阻塞 |
| 029 | 既有 5,000 项导出 <=120s 测试；其余 10 会话/p95/盘点/折旧/500 标签未全跑 | 阻塞 |
| 030 | 全仓无 T+ 写客户端/入口，工作簿明确人工对账 | 自动通过/待人工 |

## 5. 上线阻断项

1. 需在实际接受服务器部署固定 DNS 和 Caddy 内部 CA，并在所有 Chrome、Edge、手机安装
   受信任根证书；不得以证书警告绕过。
2. 需配置真实独立 NAS/备份设备和离线加密副本；当前本地镜像目录只证明程序逻辑。
3. 需完整执行 5,000 项/100 用户、10 并发、20 样本的盘点快照、折旧确认和扫码写场景；
   当前已完成资产列表、Dashboard、详情、500 标签和既有 5,000 行 Excel 导出。
4. 需实际 Chrome、Edge、手机和纸张二维码证据，以及 finance/equipment/HR/system owner 和
   第二安全测试人签字。
5. 自定义 GUC 仍只是完整性标记；虽然 Sprint 12 Compose 已分离 migration/runtime 并撤销
   历史表 DELETE/TRUNCATE，但 runtime 对受控更新仍有 DML 时，同一身份可伪造 GUC。
   完全 Service 专属授权需要把关键写入收敛到固定 search_path 的 SECURITY DEFINER API 并
   撤销对应直接 DML；当前不得宣称该边界已闭合。

以上任一项未完成，按 `tasks/Sprint-12-Production-Readiness.md` 必须结论“暂不上线”。本地
开发/验收使用不等于生产批准。
