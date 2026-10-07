"""Read-only import history, row lookup and fixed-route return links."""

from urllib.parse import parse_qsl, urlencode

from django.db.models import Count, Q
from django.urls import reverse

from apps.masterdata.models import ImportBatch


HISTORY_FIELDS = ("q", "import_type", "status", "page")
ROW_FIELDS = ("row_view", "row_number", "page")


def _history_values(values):
    query = {}
    for key in HISTORY_FIELDS:
        value = str(values.get(key, "") or "").strip()
        if not value:
            continue
        if key == "q":
            value = value[:200]
        elif key == "import_type" and value not in ImportBatch.ImportType.values:
            continue
        elif key == "status" and value not in ImportBatch.Status.values:
            continue
        elif key == "page" and (not value.isdecimal() or len(value) > 8):
            continue
        query[key] = value
    return query


def _row_values(values):
    query = {}
    for key in ROW_FIELDS:
        value = str(values.get(key, "") or "").strip()
        if not value:
            continue
        if key == "row_view" and value not in {"errors", "warnings", "clean"}:
            continue
        if key == "page" and (not value.isdecimal() or len(value) > 8):
            continue
        if key == "row_number" and (not value.isdecimal() or len(value) > 10 or not 1 <= int(value) <= 2147483647):
            continue
        query[key] = value
    return query


def _url(name, query, *, pk=None, anchor=""):
    url = reverse(name, kwargs={"pk": pk} if pk is not None else None)
    encoded = urlencode(query)
    return url + (f"?{encoded}" if encoded else "") + anchor


def history_navigation(request):
    raw = request.POST.get("history_query", request.GET.get("history_query", ""))
    try:
        values = dict(parse_qsl(raw[:3000], max_num_fields=30))
    except ValueError:
        values = {}
    query = _history_values(values)
    return {"history_query": urlencode(query), "history_return_url": _url("imports:home", query)}


def import_detail_url(batch, history_query="", *, row_values=None, view="imports:batch_detail", anchor=""):
    query = _row_values(row_values or {})
    if history_query:
        query["history_query"] = history_query
    return _url(view, query, pk=batch.pk, anchor=anchor)


def filter_import_history(queryset, data, *, ignore_status=False):
    search = data.get("q")
    if search:
        matches = Q(file_attachment__original_filename__icontains=search)
        if search.isdecimal() and 0 < int(search) <= 9223372036854775807:
            matches |= Q(pk=int(search))
        queryset = queryset.filter(matches)
    if data.get("import_type"):
        queryset = queryset.filter(import_type=data["import_type"])
    if not ignore_status and data.get("status"):
        queryset = queryset.filter(status=data["status"])
    return queryset


def _summary(form, query, fields, url_for):
    result = []
    for key in fields:
        value = form.cleaned_data.get(key)
        if value in (None, ""):
            continue
        label = dict(form.fields[key].choices)[value] if key in {"status", "import_type", "row_view"} else str(value)
        remaining = {name: val for name, val in query.items() if name not in {key, "page"}}
        result.append({"label": f"{form.fields[key].label}：{label}", "remove_url": url_for(remaining)})
    return result


def history_context(request, *, form, queryset, page):
    query = _history_values(request.GET)
    query.pop("page", None)
    navigation_query = {**query}
    if request.GET.get("page"):
        navigation_query["page"] = str(page.number)
    encoded = urlencode(navigation_query)
    context = {"import_filter_summary": [], "history_status_cards": [],
        "has_history_filters": any(request.GET.get(key) for key in ("q", "import_type", "status"))}
    if form.is_valid():
        context["import_filter_summary"] = _summary(form, query, ("q", "import_type", "status"),
            lambda values: _url("imports:home", values))
        counts = filter_import_history(queryset, form.cleaned_data, ignore_status=True).aggregate(
            total=Count("pk"), **{status: Count("pk", filter=Q(status=status)) for status in ImportBatch.Status.values})
        for status, label in [("", "全部记录"), *ImportBatch.Status.choices]:
            card_query = {key: value for key, value in query.items() if key != "status"}
            if status:
                card_query["status"] = status
            context["history_status_cards"].append({"label": label, "count": counts[status or "total"],
                "url": _url("imports:home", card_query), "active": (form.cleaned_data.get("status") or "") == status})
    for batch in page.object_list:
        batch.workspace_detail_url = import_detail_url(batch, encoded)
        batch.workspace_errors_url = import_detail_url(batch, encoded, row_values={"row_view": "errors"}, anchor="#import-rows")
    return context


def detail_context(request, *, batch, form, rows):
    navigation = history_navigation(request)
    query = _row_values(request.GET)
    query.pop("page", None)
    url_for = lambda values: import_detail_url(batch, navigation["history_query"], row_values=values, anchor="#import-rows")
    errors = batch.rows.exclude(errors_json=[])
    error_count = errors.count()
    first_error = errors.order_by("row_number").values_list("row_number", flat=True).first()
    context = {**navigation, "import_filter_summary": [], "row_status_cards": [], "error_navigation": [],
        "clear_row_filters_url": url_for({}), "error_row_count": error_count,
        "can_download_error_rows": bool(error_count and not errors.filter(raw_data_json={}).exists()),
        "import_confirm_url": import_detail_url(batch, navigation["history_query"], row_values=request.GET, view="imports:confirm"),
        "import_cancel_url": import_detail_url(batch, navigation["history_query"], row_values=request.GET, view="imports:cancel"),
    }
    if form.is_valid():
        context["import_filter_summary"] = _summary(form, query, ("row_view", "row_number"), url_for)
        scope = batch.rows.all()
        number = form.cleaned_data.get("row_number")
        if number:
            scope = scope.filter(row_number=number)
        counts = scope.aggregate(total=Count("pk"), errors=Count("pk", filter=~Q(errors_json=[])),
            warnings=Count("pk", filter=~Q(warnings_json=[])),
            clean=Count("pk", filter=Q(validation_status__in=("valid", "created"), errors_json=[], warnings_json=[])))
        for value, label, count_key in (("", "全部明细", "total"), ("errors", "错误", "errors"),
                                      ("warnings", "提示", "warnings"), ("clean", "通过且无提示", "clean")):
            card_query = {key: val for key, val in query.items() if key != "row_view"}
            if value:
                card_query["row_view"] = value
            context["row_status_cards"].append({"label": label, "count": counts[count_key], "url": url_for(card_query),
                "active": (form.cleaned_data.get("row_view") or "") == value})
        targets = [("首条错误", first_error)]
        if number:
            targets += [("上一错误", errors.filter(row_number__lt=number).order_by("-row_number").values_list("row_number", flat=True).first()),
                        ("下一错误", errors.filter(row_number__gt=number).order_by("row_number").values_list("row_number", flat=True).first())]
        for label, target in targets:
            if target is not None:
                context["error_navigation"].append({"label": label, "row_number": target,
                    "url": url_for({"row_view": "errors", "row_number": target})})
    for row in rows:
        row.workspace_focus_url = url_for({"row_number": row.row_number})
    return context
