"""Read-only authorization rules for the Sprint 11 audit-log page."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.masterdata.permissions import role_names_for


# Canonical AuditLog.object_type values accepted by the filter.  This is a
# fixed server-side registry: request data can select an entry, never invent an
# alias or expand the caller's underlying role scope.
AUDIT_OBJECT_TYPE_REGISTRY = {
    "Asset": "资产",
    "AssetCategory": "实物分类",
    "AssetCodeHistory": "资产编码历史",
    "AssetCodingScheme": "编码方案",
    "AssetCodingSegment": "编码片段",
    "AssetCustomField": "资产自定义字段",
    "AssetCustomValue": "资产自定义值",
    "AssetDepreciationProfile": "资产折旧配置",
    "AssetDisposal": "资产处置",
    "AssetDisposalReversal": "处置冲销",
    "AssetExternalReference": "外部资产引用",
    "AssetFinance": "资产财务资料",
    "AssetLabelAttachmentRequest": "贴标确认请求",
    "AssetLabelPrintBatch": "标签打印批次",
    "AssetLabelPrintItem": "标签打印明细",
    "AssetLoan": "资产借用",
    "AssetMovement": "资产变动",
    "AssetQrIdentity": "资产二维码身份",
    "AssetValueAdjustment": "资产价值调整",
    "AssetWorkUsage": "资产工作量",
    "BackupDownloadGrant": "备份下载授权",
    "BackupSet": "数据备份集",
    "Attachment": "附件",
    "AttachmentLink": "附件关联",
    "Company": "公司",
    "Department": "部门",
    "DepreciationBatch": "折旧批次",
    "DepreciationBatchItem": "折旧批次明细",
    "DepreciationEntry": "折旧分录",
    "DepreciationPolicy": "折旧政策",
    "DepreciationProfileEvent": "折旧配置事件",
    "DepreciationSchedule": "折旧计划",
    "Employee": "人员",
    "EmployeeAssetClearance": "离职资产清退",
    "EmployeeAssetClearanceItem": "离职资产清退明细",
    "ExportLog": "导出记录",
    "ExportLogTotal": "导出合计",
    "FinanceFormalizationRequest": "财务正式化请求",
    "FixedAssetCategory": "固定资产类别",
    "IdempotencyRecord": "幂等记录",
    "ImportBatch": "导入批次",
    "ImportRow": "导入明细",
    "ImportTempFile": "导入临时文件",
    "InitializationSetting": "初始化设置",
    "InventoryResolution": "盘点处理结论",
    "InventoryScan": "盘点扫描",
    "InventorySurplus": "盘盈记录",
    "InventoryTask": "盘点任务",
    "InventoryTaskAsset": "盘点快照",
    "InventoryTaskAssignee": "盘点执行人",
    "IssuedCode": "已发资产编号",
    "Location": "位置",
    "MaintenancePlan": "保养计划",
    "MaintenanceProblem": "保养问题",
    "MaintenanceRecord": "保养记录",
    "PrivateAssetFile": "资产私有文件",
    "PrivateImportFile": "导入私有文件",
    "PrivateInventoryFile": "盘点私有文件",
    "SequenceCounter": "编号计数器",
    "SystemSetting": "系统设置",
    "SupplyCategory": "低值物品分类",
    "SupplyWarehouse": "低值物品仓库",
    "SupplyItem": "低值物品档案",
    "SupplyDocumentSequence": "低值物品单据序号",
    "SupplyDocument": "低值物品库存单据",
    "SupplyDocumentLine": "低值物品库存单据明细",
    "SupplyStockBalance": "低值物品库存余额",
    "SupplyStockLedger": "低值物品库存流水",
    "SupplyCustody": "数量型耐用品保管",
    "SupplyCustodyMovement": "数量型耐用品保管流水",
    "SupplyCountTask": "低值物品盘点任务",
    "SupplyCountLine": "低值物品盘点明细",
    "EmployeeSupplyClearanceItem": "员工低值耐用品清退明细",
    "TheoreticalDepreciationLine": "理论折旧明细",
    "TheoreticalDepreciationRun": "理论折旧试算",
    "User": "用户",
    "UserAuthentication": "用户认证",
    "UserDepartmentScope": "用户部门范围",
}

FINANCE_AUDIT_OBJECT_TYPES = frozenset(
    {
        "FixedAssetCategory",
        "AssetFinance",
        "DepreciationPolicy",
        "AssetDepreciationProfile",
        "DepreciationSchedule",
        "DepreciationProfileEvent",
        "AssetWorkUsage",
        "DepreciationBatch",
        "DepreciationBatchItem",
        "AssetValueAdjustment",
        "DepreciationEntry",
        "TheoreticalDepreciationRun",
        "TheoreticalDepreciationLine",
        "AssetDisposal",
        "AssetDisposalReversal",
        "AssetExternalReference",
        "ExportLog",
        "ExportLogTotal",
    }
)

HR_AUDIT_OBJECT_TYPES = frozenset(
    {
        "Employee",
        "EmployeeAssetClearance",
        "EmployeeAssetClearanceItem",
    }
)

AUDIT_READ_ROLES = frozenset({"system_admin", "finance", "hr"})


def can_view_audit_logs(user) -> bool:
    return bool(role_names_for(user).intersection(AUDIT_READ_ROLES))


def require_view_audit_logs(user) -> None:
    if not can_view_audit_logs(user):
        raise PermissionDenied("您没有查看操作日志的权限。")


def scoped_audit_logs(user, company, queryset=None):
    """Apply the immutable company/role scope before any request filters."""

    from apps.audit.models import AuditLog

    queryset = queryset if queryset is not None else AuditLog.objects.all()
    queryset = queryset.filter(company=company)
    roles = role_names_for(user)
    if "system_admin" in roles:
        return queryset

    allowed = Q(pk__in=[])
    if "finance" in roles:
        allowed |= Q(user=user) | Q(object_type__in=FINANCE_AUDIT_OBJECT_TYPES)
    if "hr" in roles:
        allowed |= Q(user=user) | Q(object_type__in=HR_AUDIT_OBJECT_TYPES)
    return queryset.filter(allowed)


__all__ = [
    "AUDIT_OBJECT_TYPE_REGISTRY",
    "AUDIT_READ_ROLES",
    "FINANCE_AUDIT_OBJECT_TYPES",
    "HR_AUDIT_OBJECT_TYPES",
    "can_view_audit_logs",
    "require_view_audit_logs",
    "scoped_audit_logs",
]
