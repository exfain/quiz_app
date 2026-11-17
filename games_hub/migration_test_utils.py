"""Historical targets without newer cross-app dependencies reapplying the schema."""


def targets_before(loader, migration):
    excluded = set(loader.graph.backwards_plan(migration))
    allowed = set(loader.graph.nodes) - excluded
    targets = {node for node in allowed if not any(
        child.key in allowed for child in loader.graph.node_map[node].children
    )}
    # An explicit same-app target is required to unapply its newer migrations;
    # merely targeting another app that depends on the old state does not do so.
    targets.update(parent.key for parent in loader.graph.node_map[migration].parents
        if parent.key[0] == migration[0])
    return sorted(targets)
