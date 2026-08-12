import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.masterdata.models import (
    AssetCategory,
    AssetCodingScheme,
    Attachment,
    Company,
    Department,
    Employee,
    IssuedCode,
    Location,
)
from apps.masterdata.normalization import (
    clean_display_identifier,
    normalize_identifier,
)


class AssetQuerySet(models.QuerySet):
    def update(self, **kwargs):
        protected = {
            "asset_status",
            "record_status",
            "asset_code",
            "current_issued_code",
            "current_issued_code_id",
            "requested_coding_scheme",
            "requested_coding_scheme_id",
            "submitted_at",
        }.intersection(kwargs)
        submitted_actor_keys = {"submitted_by", "submitted_by_id"}.intersection(kwargs)
        if submitted_actor_keys and any(kwargs[key] is not None for key in submitted_actor_keys):
            protected.update(submitted_actor_keys)
        if protected:
            raise ValidationError("资产状态、编号和提交元数据只能通过受控 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("资产只能通过受控 Service 物理删除未提交草稿。")


class Asset(models.Model):
    class AssetStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_FINANCE = "pending_finance", "待财务确认"
        PENDING_LABEL = "pending_label", "待贴标"
        IN_USE = "in_use", "在用"
        IDLE = "idle", "闲置"
        LOANED = "loaned", "借出"
        UNDER_REPAIR = "under_repair", "维修中"
        PENDING_DISPOSAL = "pending_disposal", "处置处理中"
        DISPOSED = "disposed", "已报废"
        SOLD = "sold", "已出售"
        OTHER_DISPOSED = "other_disposed", "已其他处置"

    class RecordStatus(models.TextChoices):
        ACTIVE = "active", "有效"
        ARCHIVED = "archived", "已归档"

    class TrackingMode(models.TextChoices):
        SINGLE_ITEM = "single_item", "单件追踪"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="assets",
    )
    asset_code = models.CharField(
        "正式资产编号", max_length=64, null=True, blank=True, editable=False
    )
    current_issued_code = models.OneToOneField(
        IssuedCode,
        verbose_name="当前正式编号登记",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="current_asset",
    )
    requested_coding_scheme = models.ForeignKey(
        AssetCodingScheme,
        verbose_name="指定编码方案版本",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requested_by_assets",
    )
    asset_status = models.CharField(
        "资产状态",
        max_length=32,
        choices=AssetStatus.choices,
        default=AssetStatus.DRAFT,
    )
    record_status = models.CharField(
        "记录状态",
        max_length=16,
        choices=RecordStatus.choices,
        default=RecordStatus.ACTIVE,
    )
    asset_name = models.CharField("资产名称", max_length=200)
    category = models.ForeignKey(
        AssetCategory,
        verbose_name="实物分类",
        on_delete=models.PROTECT,
        related_name="assets",
    )
    brand = models.CharField("品牌", max_length=100, blank=True)
    model = models.CharField("型号", max_length=100, blank=True)
    manufacturer = models.CharField("厂家", max_length=200, blank=True)
    serial_number = models.CharField("序列号", max_length=200, blank=True)
    factory_number = models.CharField("出厂编号", max_length=200, blank=True)
    historical_code = models.CharField("历史参考编号", max_length=200, blank=True)
    tracking_mode = models.CharField(
        "追踪方式",
        max_length=16,
        choices=TrackingMode.choices,
        default=TrackingMode.SINGLE_ITEM,
        editable=False,
    )
    quantity = models.PositiveSmallIntegerField("数量", default=1, editable=False)
    unit = models.CharField("单位", max_length=32, blank=True)
    description = models.TextField("说明", blank=True)
    department = models.ForeignKey(
        Department,
        verbose_name="当前部门",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    responsible_employee = models.ForeignKey(
        Employee,
        verbose_name="当前责任人",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="responsible_assets",
    )
    location = models.ForeignKey(
        Location,
        verbose_name="当前位置",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    acquisition_date = models.DateField("购置日期", null=True, blank=True)
    commissioning_date = models.DateField(
        "达到可使用状态日期", null=True, blank=True
    )
    is_maintenance_required = models.BooleanField("需要保养", default=False)
    initialization_source = models.CharField(
        "初始化来源", max_length=32, default="manual"
    )
    initialization_date = models.DateField(
        "初始化日期", default=timezone.localdate
    )
    initialized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="初始化人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="initialized_assets",
    )
    notes = models.TextField("备注", blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="提交人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_assets",
    )
    submitted_at = models.DateTimeField("提交时间", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_assets",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="最后修改人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_assets",
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    objects = AssetQuerySet.as_manager()

    class Meta:
        verbose_name = "资产"
        verbose_name_plural = "资产"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "asset_code"),
                condition=Q(asset_code__isnull=False),
                name="uq_asset_company_code",
            ),
            models.CheckConstraint(
                condition=Q(quantity=1), name="ck_asset_quantity_one"
            ),
            models.CheckConstraint(
                condition=Q(tracking_mode="single_item"),
                name="ck_asset_tracking_single",
            ),
            models.CheckConstraint(
                condition=Q(
                    asset_status__in=(
                        "draft",
                        "pending_finance",
                        "pending_label",
                        "in_use",
                        "idle",
                        "loaned",
                        "under_repair",
                        "pending_disposal",
                        "disposed",
                        "sold",
                        "other_disposed",
                    )
                ),
                name="ck_asset_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(record_status__in=("active", "archived")),
                name="ck_asset_record_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        asset_status__in=("draft", "pending_finance"),
                        asset_code__isnull=True,
                        current_issued_code__isnull=True,
                    )
                    | Q(
                        asset_status__in=(
                            "pending_label",
                            "in_use",
                            "idle",
                            "loaned",
                            "under_repair",
                            "pending_disposal",
                            "disposed",
                            "sold",
                            "other_disposed",
                        ),
                        asset_code__isnull=False,
                        current_issued_code__isnull=False,
                    )
                ),
                name="ck_asset_code_status",
            ),
            models.CheckConstraint(
                condition=Q(asset_code__isnull=True) | ~Q(asset_code=""),
                name="ck_asset_code_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        asset_status="draft",
                        submitted_by__isnull=True,
                        submitted_at__isnull=True,
                    )
                    | Q(
                        asset_status__in=(
                            "pending_finance",
                            "pending_label",
                            "in_use",
                            "idle",
                            "loaned",
                            "under_repair",
                            "pending_disposal",
                            "disposed",
                            "sold",
                            "other_disposed",
                        ),
                        submitted_at__isnull=False,
                    )
                ),
                name="ck_asset_submission_metadata",
            ),
            models.CheckConstraint(
                condition=(
                    Q(record_status="active")
                    | Q(
                        asset_status__in=("disposed", "sold", "other_disposed"),
                        record_status="archived",
                    )
                ),
                name="ck_asset_archive_terminal_only",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(
                        asset_status__in=(
                            "in_use",
                            "idle",
                            "loaned",
                            "under_repair",
                            "pending_disposal",
                        )
                    )
                    | Q(
                        department__isnull=False,
                        responsible_employee__isnull=False,
                        location__isnull=False,
                    )
                ),
                name="ck_asset_operational_responsibility",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_status not in {
            self.AssetStatus.DRAFT,
            self.AssetStatus.PENDING_FINANCE,
        }:
            errors["asset_status"] = "Sprint 3 仅允许草稿或待财务确认状态。"
        if self.asset_code == "":
            errors["asset_code"] = "未发号资产必须保存 NULL，不能使用空字符串。"
        if self.category_id:
            if self.category.company_id != self.company_id:
                errors["category"] = "实物分类必须属于同一公司。"
            elif not self.category.is_active:
                errors["category"] = "实物分类必须处于启用状态。"
        if self.department_id:
            if self.department.company_id != self.company_id:
                errors["department"] = "部门必须属于同一公司。"
            elif not self.department.is_active:
                errors["department"] = "部门必须处于启用状态。"
        if self.responsible_employee_id:
            employee = self.responsible_employee
            if employee.company_id != self.company_id:
                errors["responsible_employee"] = "责任人必须属于同一公司。"
            elif self.department_id and employee.department_id != self.department_id:
                errors["responsible_employee"] = "责任人必须属于资产当前部门。"
            elif (
                employee.employment_status != Employee.EmploymentStatus.ACTIVE
                or not employee.is_active
            ):
                errors["responsible_employee"] = "责任人必须是在职且启用的员工。"
        if self.location_id:
            if self.location.company_id != self.company_id:
                errors["location"] = "位置必须属于同一公司。"
            elif not self.location.is_active:
                errors["location"] = "位置必须处于启用状态。"
        if self.requested_coding_scheme_id:
            scheme = self.requested_coding_scheme
            today = timezone.localdate()
            if scheme.company_id != self.company_id:
                errors["requested_coding_scheme"] = "编码方案必须属于同一公司。"
            elif (
                scheme.status != AssetCodingScheme.Status.ACTIVE
                or scheme.effective_from is None
                or scheme.effective_from > today
                or (scheme.effective_to is not None and scheme.effective_to < today)
            ):
                errors["requested_coding_scheme"] = "编码方案必须是当前生效版本。"
        if self.current_issued_code_id:
            issued = self.current_issued_code
            if issued.company_id != self.company_id:
                errors["current_issued_code"] = "正式编号登记必须属于同一公司。"
            elif self.asset_code != issued.display_code:
                errors["asset_code"] = "正式编号必须与当前编号登记一致。"
        if self.asset_status in {
            self.AssetStatus.DRAFT,
            self.AssetStatus.PENDING_FINANCE,
        } and (self.asset_code is not None or self.current_issued_code_id is not None):
            errors["asset_code"] = "草稿和待财务确认资产不得包含正式编号。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "asset_status",
                "record_status",
                "asset_code",
                "current_issued_code_id",
                "requested_coding_scheme_id",
                "submitted_by_id",
                "submitted_at",
            ).first()
            if previous is not None:
                current = {
                    "asset_status": self.asset_status,
                    "record_status": self.record_status,
                    "asset_code": self.asset_code,
                    "current_issued_code_id": self.current_issued_code_id,
                    "requested_coding_scheme_id": self.requested_coding_scheme_id,
                    "submitted_by_id": self.submitted_by_id,
                    "submitted_at": self.submitted_at,
                }
                if previous != current:
                    raise ValidationError(
                        "资产状态、编号和提交元数据只能通过受控 Service 修改。"
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("资产只能通过受控 Service 物理删除未提交草稿。")

    @property
    def draft_number(self):
        return f"D-{str(self.pk).split('-')[0].upper()}"

    @property
    def cover_attachment_link(self):
        return self.attachment_links.filter(
            role=AttachmentLink.Role.COVER,
            status=AttachmentLink.Status.ACTIVE,
            attachment__is_available=True,
            attachment__malware_scan_status__in=(
                Attachment.MalwareScanStatus.POLICY_LIMITED,
                Attachment.MalwareScanStatus.CLEAN,
            ),
        ).select_related("attachment").first()

    def __str__(self):
        return self.asset_code or self.draft_number


class AssetCustomField(models.Model):
    class FieldType(models.TextChoices):
        TEXT = "text", "文本"
        DECIMAL = "decimal", "小数"
        DATE = "date", "日期"
        BOOLEAN = "boolean", "布尔"
        SELECT = "select", "单选"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_custom_fields",
    )
    category = models.ForeignKey(
        AssetCategory,
        verbose_name="适用实物分类",
        on_delete=models.PROTECT,
        related_name="custom_fields",
    )
    name = models.CharField("字段名称", max_length=100)
    code = models.CharField("字段编码", max_length=100)
    normalized_code = models.CharField(
        "规范化字段编码", max_length=100, editable=False
    )
    field_type = models.CharField(
        "字段类型", max_length=16, choices=FieldType.choices
    )
    required = models.BooleanField("提交时必填", default=False)
    options_json = models.JSONField("选项", null=True, blank=True)
    display_order = models.PositiveIntegerField("显示顺序", default=1)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "资产动态字段"
        verbose_name_plural = "资产动态字段"
        ordering = ("company_id", "display_order", "normalized_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_asset_custom_field_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""),
                name="ck_asset_custom_field_code_nonempty",
            ),
            models.CheckConstraint(
                condition=Q(
                    field_type__in=("text", "decimal", "date", "boolean", "select")
                ),
                name="ck_asset_custom_field_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(field_type="select", options_json__isnull=False)
                    | Q(
                        field_type__in=("text", "decimal", "date", "boolean"),
                        options_json__isnull=True,
                    )
                ),
                name="ck_asset_custom_field_options_type",
            ),
            models.CheckConstraint(
                condition=Q(display_order__gte=1),
                name="ck_asset_custom_field_order_positive",
            ),
        ]

    def _normalize_code(self):
        self.code = clean_display_identifier(self.code)
        self.normalized_code = normalize_identifier(self.code)

    def clean(self):
        super().clean()
        self._normalize_code()
        errors = {}
        if not self.normalized_code:
            errors["code"] = "字段编码不能为空。"
        if self.category_id and self.category.company_id != self.company_id:
            errors["category"] = "适用分类必须属于同一公司。"
        if self.field_type == self.FieldType.SELECT:
            options = self.options_json
            if not isinstance(options, list) or not options:
                errors["options_json"] = "单选字段必须配置非空字符串选项数组。"
            elif any(
                not isinstance(option, str)
                or not option
                or option != option.strip()
                for option in options
            ):
                errors["options_json"] = "单选选项必须是无首尾空白的非空字符串。"
            elif len(options) != len(set(options)):
                errors["options_json"] = "单选选项不能重复。"
        elif self.options_json is not None:
            errors["options_json"] = "只有单选字段可以配置选项。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._normalize_code()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "code" in update_fields:
            kwargs["update_fields"] = {*update_fields, "normalized_code"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class AssetCustomValue(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_custom_values",
    )
    asset = models.ForeignKey(
        Asset,
        verbose_name="资产",
        on_delete=models.CASCADE,
        related_name="custom_values",
    )
    custom_field = models.ForeignKey(
        AssetCustomField,
        verbose_name="动态字段",
        on_delete=models.PROTECT,
        related_name="values",
    )
    value_text = models.TextField("文本值", null=True, blank=True)
    value_decimal = models.DecimalField(
        "小数值", max_digits=30, decimal_places=8, null=True, blank=True
    )
    value_date = models.DateField("日期值", null=True, blank=True)
    value_boolean = models.BooleanField("布尔值", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "资产动态字段值"
        verbose_name_plural = "资产动态字段值"
        constraints = [
            models.UniqueConstraint(
                fields=("asset", "custom_field"),
                name="uq_asset_custom_value_asset_field",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        value_text__isnull=False,
                        value_decimal__isnull=True,
                        value_date__isnull=True,
                        value_boolean__isnull=True,
                    )
                    | Q(
                        value_text__isnull=True,
                        value_decimal__isnull=False,
                        value_date__isnull=True,
                        value_boolean__isnull=True,
                    )
                    | Q(
                        value_text__isnull=True,
                        value_decimal__isnull=True,
                        value_date__isnull=False,
                        value_boolean__isnull=True,
                    )
                    | Q(
                        value_text__isnull=True,
                        value_decimal__isnull=True,
                        value_date__isnull=True,
                        value_boolean__isnull=False,
                    )
                ),
                name="ck_asset_custom_value_one_column",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        if self.custom_field_id:
            field = self.custom_field
            if field.company_id != self.company_id:
                errors["custom_field"] = "动态字段必须属于同一公司。"
            elif self.asset_id and field.category_id != self.asset.category_id:
                errors["custom_field"] = "动态字段不适用于该资产实物分类。"
            values = {
                AssetCustomField.FieldType.TEXT: self.value_text,
                AssetCustomField.FieldType.SELECT: self.value_text,
                AssetCustomField.FieldType.DECIMAL: self.value_decimal,
                AssetCustomField.FieldType.DATE: self.value_date,
                AssetCustomField.FieldType.BOOLEAN: self.value_boolean,
            }
            expected_value = values.get(field.field_type)
            if field.field_type not in values:
                errors["custom_field"] = "不支持的动态字段类型。"
            elif expected_value is None:
                errors["custom_field"] = "动态字段值保存到了错误的值列。"
            if (
                field.field_type == AssetCustomField.FieldType.SELECT
                and self.value_text not in (field.options_json or [])
            ):
                errors["value_text"] = "单选值不属于已批准选项。"
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.asset} / {self.custom_field}"


class AssetCodeHistory(models.Model):
    class EventType(models.TextChoices):
        ISSUED = "issued", "首次发号"
        CORRECTED = "corrected", "编号更正"
        VOIDED = "voided", "编号作废"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_code_histories",
    )
    asset = models.ForeignKey(
        Asset,
        verbose_name="资产",
        on_delete=models.PROTECT,
        related_name="code_history",
    )
    event_type = models.CharField(
        "事件类型", max_length=16, choices=EventType.choices
    )
    old_issued_code = models.ForeignKey(
        IssuedCode,
        verbose_name="原编号登记",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="old_code_history",
    )
    new_issued_code = models.ForeignKey(
        IssuedCode,
        verbose_name="新编号登记",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="new_code_history",
    )
    reason = models.TextField("原因", blank=True)
    effective_at = models.DateTimeField("业务生效时间")
    operated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="operated_asset_code_histories",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "资产编号历史"
        verbose_name_plural = "资产编号历史"
        ordering = ("asset_id", "created_at")
        constraints = [
            models.CheckConstraint(
                condition=Q(event_type__in=("issued", "corrected", "voided")),
                name="ck_asset_code_history_type",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="issued",
                        old_issued_code__isnull=True,
                        new_issued_code__isnull=False,
                    )
                    | Q(
                        event_type="corrected",
                        old_issued_code__isnull=False,
                        new_issued_code__isnull=False,
                        reason__gt="",
                    )
                    | Q(
                        event_type="voided",
                        old_issued_code__isnull=False,
                        new_issued_code__isnull=True,
                        reason__gt="",
                    )
                ),
                name="ck_asset_code_history_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(old_issued_code__isnull=True)
                    | Q(new_issued_code__isnull=True)
                    | ~Q(old_issued_code=models.F("new_issued_code"))
                ),
                name="ck_asset_code_history_distinct_codes",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        for field_name in ("old_issued_code", "new_issued_code"):
            issued = getattr(self, field_name)
            if issued is not None and issued.company_id != self.company_id:
                errors[field_name] = "编号登记必须属于同一公司。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("资产编号历史只允许追加。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("资产编号历史不可删除。")

    def __str__(self):
        return f"{self.asset} - {self.get_event_type_display()}"


class AttachmentLinkQuerySet(models.QuerySet):
    def update(self, **kwargs):
        immutable = {
            "company",
            "company_id",
            "asset",
            "asset_id",
            "attachment",
            "attachment_id",
            "role",
            "security_class",
        }.intersection(kwargs)
        if immutable:
            raise ValidationError("附件关联身份、用途和安全分类不可修改；请作废后新建。")
        protected = {
            "status",
            "void_reason",
            "voided_at",
        }.intersection(kwargs)
        void_actor_keys = {"voided_by", "voided_by_id"}.intersection(kwargs)
        if void_actor_keys and any(kwargs[key] is not None for key in void_actor_keys):
            protected.update(void_actor_keys)
        if protected:
            raise ValidationError("附件业务状态只能通过受控 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("附件业务关联不得物理删除，只能通过受控 Service 作废。")


class AttachmentLink(models.Model):
    class Role(models.TextChoices):
        COVER = "cover", "封面照片"
        PHOTO = "photo", "资产照片"
        INVOICE = "invoice", "发票"
        CONTRACT = "contract", "合同"
        ACCEPTANCE = "acceptance", "验收单"
        CERTIFICATE = "certificate", "合格证"
        MANUAL = "manual", "说明书"
        OTHER = "other", "其他"

    class SecurityClass(models.TextChoices):
        A0 = "A0", "普通附件"
        A1 = "A1", "财务附件"

    class Status(models.TextChoices):
        ACTIVE = "active", "有效"
        VOIDED = "voided", "已作废"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="attachment_links",
    )
    attachment = models.OneToOneField(
        Attachment,
        verbose_name="附件",
        on_delete=models.PROTECT,
        related_name="business_link",
    )
    asset = models.ForeignKey(
        Asset,
        verbose_name="资产",
        on_delete=models.PROTECT,
        related_name="attachment_links",
    )
    role = models.CharField("附件用途", max_length=32, choices=Role.choices)
    security_class = models.CharField(
        "安全分类", max_length=2, choices=SecurityClass.choices
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    void_reason = models.TextField("作废原因", blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="作废人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_attachment_links",
    )
    voided_at = models.DateTimeField("作废时间", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="关联人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_attachment_links",
    )
    created_at = models.DateTimeField("关联时间", auto_now_add=True)

    objects = AttachmentLinkQuerySet.as_manager()

    class Meta:
        verbose_name = "附件业务关联"
        verbose_name_plural = "附件业务关联"
        ordering = ("asset_id", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(role="cover", status="active"),
                name="uq_attachment_link_active_cover",
            ),
            models.CheckConstraint(
                condition=Q(
                    role__in=(
                        "cover",
                        "photo",
                        "invoice",
                        "contract",
                        "acceptance",
                        "certificate",
                        "manual",
                        "other",
                    )
                ),
                name="ck_attachment_link_role",
            ),
            models.CheckConstraint(
                condition=Q(security_class__in=("A0", "A1")),
                name="ck_attachment_link_security",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        role__in=("cover", "photo", "certificate", "manual"),
                        security_class="A0",
                    )
                    | Q(
                        role__in=("invoice", "contract", "acceptance"),
                        security_class="A1",
                    )
                    | Q(role="other", security_class__in=("A0", "A1"))
                ),
                name="ck_attachment_link_role_security",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="active",
                        void_reason="",
                        voided_by__isnull=True,
                        voided_at__isnull=True,
                    )
                    | Q(
                        status="voided",
                        void_reason__gt="",
                        voided_at__isnull=False,
                    )
                ),
                name="ck_attachment_link_status_fields",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        if self.attachment_id and self.attachment.company_id != self.company_id:
            errors["attachment"] = "附件必须属于同一公司。"
        a0_roles = {
            self.Role.COVER,
            self.Role.PHOTO,
            self.Role.CERTIFICATE,
            self.Role.MANUAL,
        }
        a1_roles = {self.Role.INVOICE, self.Role.CONTRACT, self.Role.ACCEPTANCE}
        if self.role in a0_roles and self.security_class != self.SecurityClass.A0:
            errors["security_class"] = "该附件用途必须使用 A0 普通分类。"
        if self.role in a1_roles and self.security_class != self.SecurityClass.A1:
            errors["security_class"] = "该附件用途必须使用 A1 财务分类。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "company_id",
                "asset_id",
                "attachment_id",
                "role",
                "security_class",
                "status",
                "void_reason",
                "voided_by_id",
                "voided_at",
            ).first()
            current = {
                "company_id": self.company_id,
                "asset_id": self.asset_id,
                "attachment_id": self.attachment_id,
                "role": self.role,
                "security_class": self.security_class,
                "status": self.status,
                "void_reason": self.void_reason,
                "voided_by_id": self.voided_by_id,
                "voided_at": self.voided_at,
            }
            if previous is not None and previous != current:
                identity_fields = (
                    "company_id",
                    "asset_id",
                    "attachment_id",
                    "role",
                    "security_class",
                )
                if any(previous[field] != current[field] for field in identity_fields):
                    raise ValidationError(
                        "附件关联身份、用途和安全分类不可修改；请作废后新建。"
                    )
                raise ValidationError("附件业务状态只能通过受控 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("附件业务关联不得物理删除，只能通过受控 Service 作废。")

    def __str__(self):
        return f"{self.asset} - {self.get_role_display()}"
