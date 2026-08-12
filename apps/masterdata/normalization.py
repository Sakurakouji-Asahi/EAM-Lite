import unicodedata


def normalize_identifier(value: str) -> str:
    """Return the canonical, case-insensitive value used by unique keys."""
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def clean_display_identifier(value: str) -> str:
    """Normalize compatibility characters while preserving display case."""
    return unicodedata.normalize("NFKC", value or "").strip()
