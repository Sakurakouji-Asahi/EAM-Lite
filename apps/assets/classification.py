"""Physical management views; financial reporting keeps its own definitions."""
from django.db.models import Q


def individual_durable_filter():
    return Q(management_attribute="LV") | Q(
        management_attribute="",
        finance__accounting_treatment="controlled_non_fixed",
        finance__finance_confirmed_at__isnull=False,
    )
