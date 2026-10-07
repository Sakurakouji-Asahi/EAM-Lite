"""Read-only presentation and return navigation for problem follow-up."""
from urllib.parse import urlsplit

from django.urls import reverse


def problem_return_url(request, fallback=""):
    value = request.POST.get("return_to", request.GET.get("return_to", ""))
    if not value or len(value) > 3000 or "\\" in value or any(ord(char) < 32 for char in value):
        return fallback
    try:
        parts = urlsplit(value)
        if parts.scheme or parts.netloc or not value.startswith("/") or value.startswith("//"):
            return fallback
        if parts.path == reverse("maintenance:problem-list"):
            return value
    except ValueError:
        pass
    return fallback


def problem_timing(problem, today):
    if problem.status == "closed":
        return {"label": "问题已关闭", "tone": "success"}
    if problem.target_date is None:
        return {"label": "未设期限", "tone": "secondary"}
    days = (problem.target_date - today).days
    if days < 0:
        return {"label": f"逾期 {-days} 天", "tone": "danger"}
    if days == 0:
        return {"label": "今日到期", "tone": "warning"}
    return {"label": f"距到期 {days} 天", "tone": "primary" if days <= 7 else "secondary"}
