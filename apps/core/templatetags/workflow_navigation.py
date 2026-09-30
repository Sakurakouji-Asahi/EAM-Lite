from django import template
from apps.core.return_navigation import return_query

register = template.Library()


@register.simple_tag(takes_context=True)
def workflow_query(context):
    return return_query(context['request'])
