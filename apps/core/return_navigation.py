"""Return only to known, read-only pages; never trust a client redirect URL."""
from urllib.parse import urlsplit, urlencode
from django.urls import resolve, Resolver404

RETURN_VIEWS = {
    'offboarding:clearance-detail', 'offboarding:clearance-list',
    'reports:external-reference-list', 'assets:loan-workbench', 'assets:disposal-workbench',
    'masterdata:department-list', 'masterdata:employee-list', 'masterdata:location-list',
    'masterdata:category-list', 'masterdata:employee-detail', 'masterdata:department-detail',
    'masterdata:location-detail', 'masterdata:category-detail',
}


def safe_return_url(request, fallback):
    value = request.POST.get('return_to', request.GET.get('return_to', ''))
    if not value or len(value) > 3000 or '\\' in value or any(ord(c) < 32 for c in value):
        return fallback
    try:
        parts = urlsplit(value)
        if parts.scheme or parts.netloc or not value.startswith('/') or value.startswith('//'):
            return fallback
        if resolve(parts.path).view_name in RETURN_VIEWS:
            return value
    except (ValueError, Resolver404):
        pass
    return fallback


def return_query(request):
    return urlencode({'return_to':safe_return_url(request, request.get_full_path())})
