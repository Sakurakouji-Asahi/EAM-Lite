"""Read-only batch progress and fixed-route navigation for label work."""
from django import forms
from django.db.models import Count, F, Q
from django.http import QueryDict
from django.urls import reverse

from .models import AssetLabelPrintBatch


PROGRESS_CHOICES = (
    ("", "全部贴标进度"),
    ("pending", "有待确认贴标"),
    ("attached", "当前标签全部已确认"),
    ("inactive", "含已失效标签"),
)


class LabelBatchListFilterForm(forms.Form):
    q = forms.CharField(label="批次、资产编号、设备编号或名称", max_length=200, required=False)
    status = forms.ChoiceField(label="打印批次状态", required=False,
        choices=(("", "全部状态"), *AssetLabelPrintBatch.Status.choices))
    progress = forms.ChoiceField(label="当前贴标进度", required=False, choices=PROGRESS_CHOICES)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"


def annotate_label_progress(batches):
    active = Q(items__qr_identity__status="active")
    attached = active & Q(items__qr_identity__label_status="attached")
    pending = active & Q(status="printed", items__qr_identity__label_status="printed")
    return batches.annotate(
        pending_count=Count("items", filter=pending, distinct=True),
        attached_count=Count("items", filter=attached, distinct=True),
        inactive_count=Count("items", filter=~active, distinct=True),
        waiting_count=Count("items", filter=active & ~attached & ~pending, distinct=True),
    )


def filter_label_progress(batches, progress):
    if progress == "pending":
        return batches.filter(pending_count__gt=0)
    if progress == "attached":
        return batches.filter(item_count__gt=0, attached_count=F("item_count"))
    if progress == "inactive":
        return batches.filter(inactive_count__gt=0)
    return batches


def label_list_query(raw):
    source = QueryDict(raw[:3000] if isinstance(raw, str) else "")
    result = QueryDict(mutable=True)
    if source.get("q", "").strip():
        result["q"] = source["q"].strip()[:200]
    if source.get("status") in AssetLabelPrintBatch.Status.values:
        result["status"] = source["status"]
    if source.get("progress") in {"pending", "attached", "inactive"}:
        result["progress"] = source["progress"]
    page = source.get("page", "")
    if page.isascii() and page.isdigit() and 0 < int(page) <= 1000000:
        result["page"] = page
    return result.urlencode()


def label_batch_navigation(raw):
    query = label_list_query(raw)
    url = reverse("assets:label-batch-list")
    return {"label_list_query": query, "label_list_url": url + ("?" + query if query else "")}


def label_batch_detail_url(batch, query="", *, pending=False):
    params = QueryDict(mutable=True)
    query = label_list_query(query)
    if query:
        params["list_query"] = query
    if pending:
        params["work"] = "pending"
    url = reverse("assets:label-batch-detail", args=[batch.pk])
    return url + ("?" + params.urlencode() if params else "")
