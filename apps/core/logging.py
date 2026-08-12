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


def redact_log_text(value):
    redacted = _SENSITIVE_QUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        str(value),
    )
    return _SENSITIVE_UNQUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        redacted,
    )


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_log_text(super().format(record))
