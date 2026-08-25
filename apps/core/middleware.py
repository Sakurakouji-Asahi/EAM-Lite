import uuid
from ipaddress import ip_address, ip_network

from django.conf import settings


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
