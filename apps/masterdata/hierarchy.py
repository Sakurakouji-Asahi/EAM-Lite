"""Traverse saved parent relationships without relying on code prefixes."""
from collections import defaultdict


def descendant_ids(model, *, company, identifier):
    children = defaultdict(list)
    for pk, parent in model.objects.filter(company=company).values_list("pk", "parent_id"):
        children[parent].append(pk)
    pending, found = [int(identifier)], set()
    while pending:
        pk = pending.pop()
        if pk not in found:
            found.add(pk)
            pending.extend(children[pk])
    return found
