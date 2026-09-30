"""Locale-independent report numbers; calculations retain their original Decimal values."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from django import template

register = template.Library()
PLACES = {"money": 2, "decimal": 2, "quantity": 4, "unit_cost": 6, "integer": 0, "rate": 2}


@register.filter
def report_number(value, kind="money"):
    if value is None or value == "":
        return "—"
    if kind not in PLACES:
        return value
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return f"数值无效：{value}"
    if not number.is_finite():
        return f"数值无效：{value}"
    with localcontext() as context:
        context.prec = max(28, len(number.as_tuple().digits) + 8)
        context.rounding = ROUND_HALF_UP
        if kind == "rate":
            number *= 100
        if kind == "integer" and number != number.to_integral_value():
            return f"数值无效：{value}"
        result = format(number, f",.{PLACES[kind]}f")
    return result + ("%" if kind == "rate" else "")
