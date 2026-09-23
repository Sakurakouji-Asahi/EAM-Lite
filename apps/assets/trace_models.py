"""Append-only composition/source evidence; accounting balances stay separate."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q


class TraceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if kwargs and set(kwargs) <= {"recorded_by", "recorded_by_id"} and all(value is None for value in kwargs.values()):
            return super().update(**kwargs)
        raise ValidationError("来源和组合记录只允许追加，不允许改写。")

    def delete(self):
        raise ValidationError("来源和组合历史不可删除。")


class TraceRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("masterdata.Company", on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    reason = models.CharField("依据或说明", max_length=1000)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    recorded_at = models.DateTimeField(auto_now_add=True)
    objects = TraceQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self)._base_manager.get(pk=self.pk)
            if not (old.recorded_by_id is not None and self.recorded_by_id is None and all(
                getattr(old, field.attname) == getattr(self, field.attname)
                for field in self._meta.concrete_fields if field.name != "recorded_by"
            )):
                raise ValidationError("来源和组合历史不可改写。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("来源和组合历史不可删除。")


class AssetOriginLink(TraceRecord):
    relation_type = models.CharField("来源关系", max_length=16, choices=(("split", "拆分来源"), ("merge", "合并来源")))
    source_asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="origin_outgoing")
    target_asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="origin_incoming")
    source_issued_code = models.ForeignKey("masterdata.IssuedCode", on_delete=models.PROTECT, related_name="origin_sources")
    target_issued_code = models.ForeignKey("masterdata.IssuedCode", on_delete=models.PROTECT, related_name="origin_targets")

    class Meta:
        verbose_name = "资产来源记录"
        constraints = [
            models.UniqueConstraint(fields=("company", "idempotency_key"), name="uq_origin_company_idem"),
            models.CheckConstraint(condition=~Q(source_asset=F("target_asset")), name="ck_origin_not_self"),
            models.CheckConstraint(condition=Q(relation_type__in=("split", "merge")), name="ck_origin_type"),
        ]


class AssetCompositionRevision(TraceRecord):
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="composition_revisions")
    revision = models.PositiveIntegerField("清单版本")
    members = models.JSONField("组合成员清单")
    previous_revision = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="next_revision")

    class Meta:
        verbose_name = "资产组合清单历史"
        ordering = ("-revision",)
        constraints = [
            models.UniqueConstraint(fields=("company", "idempotency_key"), name="uq_composition_company_idem"),
            models.UniqueConstraint(fields=("asset", "revision"), name="uq_asset_composition_revision"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="ck_composition_revision"),
            models.CheckConstraint(condition=~Q(id=F("previous_revision")), name="ck_composition_not_self"),
        ]


class AssetCustodyReturn(TraceRecord):
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="custody_returns")
    issued_code = models.ForeignKey("masterdata.IssuedCode", on_delete=models.PROTECT)
    movement = models.OneToOneField("assets.AssetMovement", null=True, blank=True, on_delete=models.PROTECT)
    returned_on = models.DateField("实际归还日期")
    counterparty = models.CharField("接收单位或接收人", max_length=200)
    contract_reference = models.CharField("合同或受托依据", max_length=200)
    acceptance_evidence = models.CharField("归还验收依据", max_length=1000)
    composition_revision = models.ForeignKey(AssetCompositionRevision, null=True, blank=True, on_delete=models.PROTECT)
    maintenance_plan_states = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name = "租入受托资产归还记录"
        constraints = [models.UniqueConstraint(fields=("company", "idempotency_key"), name="uq_custody_return_company_idem")]


class AssetOriginReversal(TraceRecord):
    origin = models.OneToOneField(AssetOriginLink, on_delete=models.PROTECT, related_name="reversal")

    class Meta:
        verbose_name = "来源登记撤销记录"
        constraints = [models.UniqueConstraint(fields=("company", "idempotency_key"), name="uq_origin_reversal_idem")]


class AssetCustodyReturnReversal(TraceRecord):
    custody_return = models.OneToOneField(AssetCustodyReturn, on_delete=models.PROTECT, related_name="reversal")
    movement = models.OneToOneField("assets.AssetMovement", on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="custody_return_reversal")

    class Meta:
        verbose_name = "租入受托归还撤销记录"
        constraints = [models.UniqueConstraint(fields=("company", "idempotency_key"), name="uq_return_reversal_idem")]
