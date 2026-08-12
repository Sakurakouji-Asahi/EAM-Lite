import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("审计日志只允许追加，不能更新")

    def delete(self):
        raise TypeError("审计日志只允许追加，不能删除")


class AuditLog(models.Model):
    company = models.ForeignKey(
        "masterdata.Company",
        verbose_name="公司",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_logs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作用户",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField("动作", max_length=100)
    object_type = models.CharField("对象类型", max_length=100)
    object_id = models.CharField("对象标识", max_length=255, blank=True)
    old_data_json = models.JSONField(
        "变更前数据", default=dict, encoder=DjangoJSONEncoder
    )
    new_data_json = models.JSONField(
        "变更后数据", default=dict, encoder=DjangoJSONEncoder
    )
    ip_address = models.GenericIPAddressField("来源 IP", null=True, blank=True)
    user_agent = models.TextField("用户代理", blank=True)
    correlation_id = models.UUIDField("关联标识", default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = "审计日志"
        verbose_name_plural = "审计日志"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("action", "created_at")),
            models.Index(fields=("object_type", "object_id")),
            models.Index(fields=("correlation_id",)),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("审计日志只允许追加，不能更新")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("审计日志只允许追加，不能删除")

    def __str__(self):
        return f"{self.action} {self.object_type}:{self.object_id}"
