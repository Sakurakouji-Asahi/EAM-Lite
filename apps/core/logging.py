import logging
import re


_SENSITIVE_NAME = (
    r"(?:password|passwd|secret|token|authorization|cookie|session|csrf|"
    r"api[_-]?key|private[_-]?key)"
)
_SENSITIVE_QUOTED_ASSIGNMENT = re.compile(
    rf"(?i)([\"']?{_SENSITIVE_NAME}[\"']?\s*[=:]\s*)([\"'])(.*?)(\2)"
)
_SENSITIVE_UNQUOTED_ASSIGNMENT = re.compile(
    rf"(?i)([\"']?{_SENSITIVE_NAME}[\"']?\s*[=:]\s*)([^,\s&;}}]+)"
)
_QR_SCAN_PATH = re.compile(
    r"(?i)(/assets/scan/)[^/?#\s]+"
)
_QR_SCAN_PATH_ENCODED = re.compile(
    r"(?i)(%2Fassets%2Fscan%2F)[^%&?#\s]+"
)


def redact_log_text(value):
    redacted = _QR_SCAN_PATH.sub(r"\1[REDACTED]", str(value))
    redacted = _QR_SCAN_PATH_ENCODED.sub(r"\1[REDACTED]", redacted)
    redacted = _SENSITIVE_QUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        redacted,
    )
    return _SENSITIVE_UNQUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        redacted,
    )


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_log_text(super().format(record))
