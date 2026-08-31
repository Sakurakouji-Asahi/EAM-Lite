import re
import uuid
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import DisallowedHost, SuspiciousOperation
from django.http import UnreadablePostError


_QR_CONFIRM_PATHS = (
    re.compile(r"^/assets/scan/[A-Za-z0-9_-]{22,128}/confirm/$"),
    re.compile(
        r"^/assets/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/labels/confirm/$"
    ),
)
_INVENTORY_QR_BRIDGE_PATH = re.compile(
    r"^/inventory/tasks/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/scan/$"
)


class QrOpaqueOriginCsrfCompatibilityMiddleware:
    """Let Django validate tokens for Edge QR pages with an opaque origin.

    Edge on Android can open a scanned LAN URL in an opaque browser context
    and submit ``Origin: null`` even though the visible URL is the EAM host.
    The compatibility paths are intentionally narrow: they only treat that
    origin as absent for QR scan attachment and per-asset Web attachment POSTs,
    with the configured QR host, session cookie, CSRF cookie and submitted
    CSRF token all still required. The inventory scan path is accepted only
    for the short-lived signed ``scan_bridge`` handoff rendered by a valid QR
    page. Django's normal CsrfViewMiddleware performs the actual token
    validation.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        configured = urlsplit(settings.QR_BASE_URL)
        self.expected_host = configured.netloc.casefold()
        self.expected_origin = (
            f"{configured.scheme.casefold()}://{configured.netloc.casefold()}"
        )

    def __call__(self, request):
        if self._is_compatible_request(request):
            request.qr_opaque_origin_csrf_compatibility = True
            request.META.pop("HTTP_ORIGIN", None)
        return self.get_response(request)

    def _is_compatible_request(self, request):
        if request.method != "POST":
            return False
        if request.META.get("HTTP_ORIGIN", "").strip().casefold() != "null":
            return False
        is_qr_confirmation = any(
            pattern.fullmatch(request.path_info)
            for pattern in _QR_CONFIRM_PATHS
        )
        is_inventory_bridge = bool(
            _INVENTORY_QR_BRIDGE_PATH.fullmatch(request.path_info)
        )
        if not is_qr_confirmation and not is_inventory_bridge:
            return False
        try:
            if request.get_host().casefold() != self.expected_host:
                return False
        except DisallowedHost:
            return False
        if not request.COOKIES.get(settings.SESSION_COOKIE_NAME):
            return False
        if not request.COOKIES.get(settings.CSRF_COOKIE_NAME):
            return False
        if is_inventory_bridge:
            try:
                if not request.POST.get("scan_bridge", "").strip():
                    return False
            except (SuspiciousOperation, UnreadablePostError):
                return False
        submitted_token = request.META.get(settings.CSRF_HEADER_NAME, "").strip()
        if not submitted_token:
            try:
                submitted_token = request.POST.get("csrfmiddlewaretoken", "").strip()
            except (SuspiciousOperation, UnreadablePostError):
                return False
        if not submitted_token:
            return False
        referer = request.META.get("HTTP_REFERER", "").strip()
        if referer:
            parsed = urlsplit(referer)
            if parsed.scheme.casefold() in {"http", "https"}:
                referer_origin = (
                    f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
                )
                if referer_origin != self.expected_origin:
                    return False
        return True


class TrustedProxyClientIpMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        try:
            self.trusted_networks = tuple(
                ip_network(value, strict=False)
                for value in settings.TRUSTED_PROXY_NETWORKS
            )
        except ValueError as exc:
            from django.core.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured("TRUSTED_PROXY_NETWORKS 包含非法网段") from exc

    def __call__(self, request):
        remote_text = request.META.get("REMOTE_ADDR", "")
        request.client_ip_address = remote_text or None
        if settings.TRUST_PROXY_CLIENT_IP and self.trusted_networks:
            try:
                remote = ip_address(remote_text)
            except ValueError:
                remote = None
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "").strip()
            if remote is not None and any(remote in network for network in self.trusted_networks):
                # Caddy is configured to overwrite, not append, this header.
                if forwarded and "," not in forwarded:
                    try:
                        request.client_ip_address = str(ip_address(forwarded))
                    except ValueError:
                        pass
        return self.get_response(request)


class CorrelationIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        supplied = request.headers.get("X-Correlation-ID", "")
        try:
            correlation_id = uuid.UUID(supplied) if supplied else uuid.uuid4()
        except (ValueError, TypeError, AttributeError):
            correlation_id = uuid.uuid4()
        request.correlation_id = correlation_id
        response = self.get_response(request)
        response.headers["X-Correlation-ID"] = str(correlation_id)
        return response


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'",
        )
        return response
