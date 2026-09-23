"""Immutable snapshots behind the unified identity format."""
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from apps.coding.standard import MANAGEMENT_CHOICES


class AssetIdentityQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("统一编码身份只允许追加，不允许改写。")

    def delete(self):
        raise ValidationError("统一编码身份和子项号必须保留，不可删除或复用。")


class AssetIdentity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("masterdata.Company", on_delete=models.PROTECT)
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="identity_history")
    issued_code = models.OneToOneField("masterdata.IssuedCode", on_delete=models.PROTECT, related_name="identity")
    management_attribute = models.CharField("首次管理属性", max_length=2, choices=MANAGEMENT_CHOICES)
    category_code = models.CharField("编码类别快照", max_length=2)
    coding_year = models.PositiveSmallIntegerField("取得年份")
    year_source = models.CharField("年份来源", max_length=24)
    year_note = models.CharField("年份依据", max_length=500)
    sequence_value = models.PositiveIntegerField("主资产流水")
    subitem_number = models.PositiveSmallIntegerField("子项号", default=0)
    parent_asset = models.ForeignKey(
        "assets.Asset", null=True, blank=True, on_delete=models.PROTECT, related_name="component_identities",
    )
    parent_identity = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="component_identities",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    objects = AssetIdentityQuerySet.as_manager()

    class Meta:
        verbose_name = "统一资产编码身份"
        constraints = [
            models.UniqueConstraint(fields=("company", "management_attribute", "category_code", "coding_year", "sequence_value", "subitem_number"), name="uq_asset_standard_identity"),
            models.UniqueConstraint(fields=("parent_identity", "subitem_number"), condition=Q(parent_identity__isnull=False), name="uq_asset_parent_subitem"),
            models.CheckConstraint(condition=Q(management_attribute__in=[code for code, _ in MANAGEMENT_CHOICES]), name="ck_asset_identity_attribute"),
            models.CheckConstraint(condition=Q(category_code__regex=r"^(0[1-9]|[1-9][0-9])$"), name="ck_asset_identity_category"),
            models.CheckConstraint(condition=Q(coding_year__gte=1000, coding_year__lte=9999), name="ck_asset_identity_year"),
            models.CheckConstraint(condition=Q(sequence_value__gte=1, sequence_value__lte=999999), name="ck_asset_identity_sequence"),
            models.CheckConstraint(condition=Q(parent_asset__isnull=True, parent_identity__isnull=True, subitem_number=0) | Q(parent_asset__isnull=False, parent_identity__isnull=False, subitem_number__gte=1, subitem_number__lte=99), name="ck_asset_identity_subitem"),
            models.CheckConstraint(condition=~Q(parent_asset=F("asset")), name="ck_asset_identity_not_self"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("统一编码身份不可修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("统一编码身份不可删除。")
