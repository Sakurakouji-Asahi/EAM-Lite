import logging
import re
import secrets
import uuid
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import DisallowedHost, SuspiciousOperation
from django.http import UnreadablePostError

from apps.core.qr_csrf import validate_qr_opaque_origin_bridge


logger = logging.getLogger("eam_lite.security")


_QR_SCAN_CONFIRM_PATH = re.compile(
    r"^/assets/scan/(?P<token>[A-Za-z0-9_-]{22,128})/confirm/$"
)
_QR_WEB_CONFIRM_PATH = re.compile(
    r"^/assets/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/labels/confirm/$"
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
    with the configured QR host, session cookie, CSRF cookie, submitted CSRF
    token and a short-lived signed bridge bound to the current session and
    exact POST path all still required. The inventory scan path is accepted
    only for its separate signed ``scan_bridge`` handoff rendered by a valid
    QR page. Django's normal CsrfViewMiddleware performs the actual token
    validation.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        configured = urlsplit(settings.QR_BASE_URL)
        self.expected_origin = (
            f"{configured.scheme.casefold()}://{configured.netloc.casefold()}"
        )
        trusted_origins = {self.expected_origin}
        for value in settings.CSRF_TRUSTED_ORIGINS:
            parsed = urlsplit(str(value).rstrip("/"))
            if parsed.scheme and parsed.netloc and "*" not in parsed.netloc:
                trusted_origins.add(
                    f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
                )
        self.expected_origins = frozenset(trusted_origins)
        self.expected_hosts = frozenset(
            urlsplit(origin).netloc.casefold() for origin in trusted_origins
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
        scan_confirmation = _QR_SCAN_CONFIRM_PATH.fullmatch(request.path_info)
        is_web_confirmation = bool(
            _QR_WEB_CONFIRM_PATH.fullmatch(request.path_info)
        )
        is_qr_confirmation = bool(scan_confirmation or is_web_confirmation)
        is_inventory_bridge = bool(
            _INVENTORY_QR_BRIDGE_PATH.fullmatch(request.path_info)
        )
        if not is_qr_confirmation and not is_inventory_bridge:
            return False
        endpoint_kind = "qr_confirmation" if is_qr_confirmation else "inventory_bridge"

        def rejected(reason):
            logger.warning(
                "opaque-origin CSRF compatibility rejected endpoint=%s reason=%s",
                endpoint_kind,
                reason,
            )
            return False

        try:
            actual_host = request.get_host().casefold()
            if actual_host not in self.expected_hosts:
                logger.warning(
                    "opaque-origin host mismatch actual=%s expected=%s",
                    actual_host,
                    ",".join(sorted(self.expected_hosts)),
                )
                return rejected("host_mismatch")
        except DisallowedHost:
            return rejected("disallowed_host")
        if not request.COOKIES.get(settings.SESSION_COOKIE_NAME):
            return rejected("session_cookie_missing")
        if not request.COOKIES.get(settings.CSRF_COOKIE_NAME):
            return rejected("csrf_cookie_missing")
        if is_inventory_bridge:
            try:
                if not request.POST.get("scan_bridge", "").strip():
                    return rejected("inventory_bridge_missing")
            except (SuspiciousOperation, UnreadablePostError):
                return rejected("post_unreadable")
        else:
            try:
                bridge = request.POST.get("opaque_origin_bridge", "").strip()
            except (SuspiciousOperation, UnreadablePostError):
                return rejected("post_unreadable")
            if bridge:
                if not validate_qr_opaque_origin_bridge(
                    bridge,
                    user_id=request.session.get("_auth_user_id"),
                    session_key=request.session.session_key,
                    path=request.path_info,
                ):
                    return rejected("qr_bridge_invalid")
            elif scan_confirmation:
                submitted_scan_token = request.POST.get("scanned_token", "").strip()
                if not submitted_scan_token or not secrets.compare_digest(
                    submitted_scan_token,
                    scan_confirmation.group("token"),
                ):
                    return rejected("scan_token_bridge_missing_or_mismatch")
            else:
                return rejected("qr_bridge_missing")
        submitted_token = request.META.get(settings.CSRF_HEADER_NAME, "").strip()
        if not submitted_token:
            try:
                submitted_token = request.POST.get("csrfmiddlewaretoken", "").strip()
            except (SuspiciousOperation, UnreadablePostError):
                return rejected("post_unreadable")
        if not submitted_token:
            return rejected("csrf_form_token_missing")
        referer = request.META.get("HTTP_REFERER", "").strip()
        if referer:
            parsed = urlsplit(referer)
            if parsed.scheme.casefold() in {"http", "https"}:
                referer_origin = (
                    f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
                )
                if (
                    referer_origin not in self.expected_origins
                    and is_inventory_bridge
                ):
                    return rejected("inventory_referer_mismatch")
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
