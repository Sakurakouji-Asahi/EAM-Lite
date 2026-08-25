import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


_STORAGE_KEY_RE = re.compile(
    r"^backups/[0-9A-Fa-f-]{36}/[0-9A-Fa-f-]{36}\.eambak$"
)


class BackupSetQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if kwargs:
            raise ValidationError("备份集只能通过受控备份 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("备份元数据必须永久保留，不可删除。")


class BackupSet(models.Model):
    class Kind(models.TextChoices):
        AUTOMATIC = "automatic", "自动日备份"
        MANUAL = "manual", "管理员手动备份"

    class Status(models.TextChoices):
        PENDING = "pending", "生成中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"
        EXPIRED = "expired", "已过期"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="backup_sets",
        verbose_name="公司",
    )
    backup_set_id = models.CharField("备份集编号", max_length=64)
    kind = models.CharField("类型", max_length=16, choices=Kind.choices)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.PENDING
    )
    request_hash = models.CharField("请求摘要", max_length=64)
    idempotency_key = models.CharField("幂等键", max_length=128)
    storage_key = models.CharField("受保护存储键", max_length=255, blank=True)
    package_sha256 = models.CharField("备份包摘要", max_length=64, blank=True)
    package_size = models.BigIntegerField("备份包字节数", null=True, blank=True)
    manifest_json = models.JSONField("清单快照", default=dict, blank=True)
    data_snapshot_at = models.DateTimeField("数据快照时间", null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_backup_sets",
        verbose_name="发起人",
    )
    started_at = models.DateTimeField("开始时间")
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)
    expires_at = models.DateTimeField("保留到期时间", null=True, blank=True)
    expired_at = models.DateTimeField("实际过期时间", null=True, blank=True)
    error_summary = models.TextField("失败摘要", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = BackupSetQuerySet.as_manager()

    class Meta:
        ordering = ("-started_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "backup_set_id"), name="uq_backup_company_set"
            ),
            models.UniqueConstraint(
                fields=("company", "idempotency_key"), name="uq_backup_company_idem"
            ),
            models.CheckConstraint(
                condition=Q(package_size__isnull=True) | Q(package_size__gte=0),
                name="ck_backup_package_size",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not str(self.backup_set_id or "").strip():
            errors["backup_set_id"] = "备份集编号不能为空。"
        if not str(self.idempotency_key or "").strip():
            errors["idempotency_key"] = "幂等键不能为空。"
        if len(str(self.request_hash or "")) != 64:
            errors["request_hash"] = "请求摘要必须是 64 位 SHA-256。"

        if self.status == self.Status.PENDING:
            if any(
                (
                    self.storage_key,
                    self.package_sha256,
                    self.package_size is not None,
                    self.finished_at is not None,
                    self.expires_at is not None,
                    bool(self.error_summary),
                )
            ):
                errors["status"] = "生成中的备份不得提前填写发布或失败字段。"
        elif self.status == self.Status.COMPLETED:
            if not _STORAGE_KEY_RE.fullmatch(self.storage_key or ""):
                errors["storage_key"] = "备份存储键格式非法。"
            if len(self.package_sha256 or "") != 64:
                errors["package_sha256"] = "完成备份必须保存 64 位 SHA-256。"
            if self.package_size is None or self.package_size < 0:
                errors["package_size"] = "完成备份必须保存非负文件大小。"
            if not self.manifest_json or not self.finished_at or not self.expires_at:
                errors["status"] = "完成备份必须保存清单、结束时间和保留时间。"
            if self.error_summary or self.expired_at:
                errors["status"] = "完成备份不得带失败或过期字段。"
        elif self.status == self.Status.FAILED:
            if not self.finished_at or not str(self.error_summary or "").strip():
                errors["status"] = "失败备份必须保存结束时间和失败摘要。"
            if self.storage_key or self.package_sha256 or self.package_size is not None:
                errors["status"] = "失败备份不得关联可下载文件。"
        elif self.status == self.Status.EXPIRED:
            if not self.finished_at or not self.expires_at or not self.expired_at:
                errors["status"] = "过期备份必须保留原完成和过期时间。"
            if self.storage_key:
                errors["storage_key"] = "过期备份不得继续提供下载存储键。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("备份集只能通过受控备份 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("备份元数据必须永久保留，不可删除。")

    def __str__(self):
        return self.backup_set_id


class BackupDownloadGrantQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if kwargs:
            raise ValidationError("备份下载授权只能通过受控 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("备份下载授权是安全审计历史，不可删除。")


class BackupDownloadGrant(models.Model):
    class Status(models.TextChoices):
        ISSUED = "issued", "已签发"
        STARTED = "started", "下载中"
        COMPLETED = "completed", "已完成"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        "masterdata.Company",
        on_delete=models.PROTECT,
        related_name="backup_download_grants",
        verbose_name="公司",
    )
    backup_set = models.ForeignKey(
        BackupSet,
        on_delete=models.PROTECT,
        related_name="download_grants",
        verbose_name="备份集",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backup_download_grants",
        verbose_name="下载用户",
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ISSUED
    )
    idempotency_key = models.CharField("幂等键", max_length=128)
    issued_at = models.DateTimeField("签发时间")
    expires_at = models.DateTimeField("过期时间")
    started_at = models.DateTimeField("开始下载时间", null=True, blank=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)
    failure_reason = models.TextField("失败原因", blank=True)

    objects = BackupDownloadGrantQuerySet.as_manager()

    class Meta:
        ordering = ("-issued_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_backup_grant_company_idem",
            )
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.backup_set_id and self.company_id != self.backup_set.company_id:
            errors["backup_set"] = "备份下载授权与备份集必须属于同一公司。"
        if self.expires_at and self.issued_at and self.expires_at <= self.issued_at:
            errors["expires_at"] = "下载授权到期时间必须晚于签发时间。"
        if self.status == self.Status.ISSUED:
            if self.started_at or self.finished_at or self.failure_reason:
                errors["status"] = "未使用授权不得带下载结果。"
        elif self.status == self.Status.STARTED:
            if not self.started_at or self.finished_at or self.failure_reason:
                errors["status"] = "下载中授权字段不完整。"
        elif self.status == self.Status.COMPLETED:
            if not self.started_at or not self.finished_at or self.failure_reason:
                errors["status"] = "完成授权字段不完整。"
        elif self.status == self.Status.FAILED:
            if not self.started_at or not self.finished_at or not self.failure_reason:
                errors["status"] = "失败授权必须保存开始、结束和原因。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("备份下载授权只能通过受控 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("备份下载授权是安全审计历史，不可删除。")
