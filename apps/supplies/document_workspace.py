"""Presentation and safe navigation for supply document work."""
from urllib.parse import urlencode, urlsplit

from django.urls import reverse

from apps.core.return_navigation import safe_return_url
from .stock_navigation import stock_query_return, stock_return_context


def document_navigation(request, document=None):
    target = safe_return_url(request, "") or stock_query_return(request)
    query = urlencode({"return_to": target}) if target else ""
    list_url = reverse("supplies:document-list")
    is_list = bool(target and urlsplit(target).path == list_url)
    detail_url = reverse("supplies:document-detail", args=[document.pk]) if document else ""
    if detail_url and query:
        detail_url += "?" + query
    return {
        "document_return_to": target,
        "document_navigation_query": query,
        "document_detail_url": detail_url,
        "document_form_back_url": detail_url or target or list_url,
        "workflow_return_url": target,
        "return_is_document_list": is_list,
        **stock_return_context(request),
    }


def document_destination(request, document, view="supplies:document-detail"):
    query = document_navigation(request, document)["document_navigation_query"]
    return reverse(view, args=[document.pk]) + ("?" + query if query else "")


def document_filter_chip(request, name, label, value):
    params = request.GET.copy()
    params.pop("page", None)
    params.pop(name, None)
    return {"name": name, "label": label, "value": value, "url": "?" + params.urlencode()}
