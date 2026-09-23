"""Company-bound location traversal without per-node database lookups."""
from collections import defaultdict

from apps.masterdata.models import Location


class LocationTree:
    def __init__(self, company):
        self.nodes = {row.pk: row for row in Location.objects.filter(company=company).only(
            "id", "company_id", "parent_id", "code", "normalized_code", "name", "level"
        )}
        self.children = defaultdict(list)
        for node in self.nodes.values():
            self.children[node.parent_id].append(node.pk)

    def ancestors(self, identifiers):
        pending, result = list(identifiers), set()
        while pending:
            pk = pending.pop()
            if pk not in self.nodes or pk in result:
                continue
            result.add(pk)
            pending.append(self.nodes[pk].parent_id)
        return result

    def descendants(self, identifier):
        pending, result = [int(identifier)], set()
        while pending:
            pk = pending.pop()
            if pk not in self.nodes or pk in result:
                continue
            result.add(pk)
            pending.extend(self.children[pk])
        return result

    def path(self, identifier):
        parts, seen = [], set()
        while identifier in self.nodes and identifier not in seen:
            seen.add(identifier)
            node = self.nodes[identifier]
            parts.append(node.name)
            identifier = node.parent_id
        return " / ".join(reversed(parts))

    def options(self, identifiers):
        allowed = self.ancestors(identifiers)
        result = []
        for pk in allowed:
            node = self.nodes[pk]
            node.path_label = self.path(pk)
            result.append(node)
        return sorted(result, key=lambda node: node.path_label)
