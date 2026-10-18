"""Deterministic dependency ordering, preserving user order among independent nodes."""

from pg_maker.errors import ValidationError


def ordered_ids(items: list[dict], *, key: str = "id", dependencies: str = "requires") -> list[str]:
    nodes = {item[key]: item for item in items}
    if len(nodes) != len(items):
        raise ValidationError("duplicate graph node")
    pending = {name: set(item.get(dependencies, [])) for name, item in nodes.items()}
    missing = set().union(*pending.values()) - nodes.keys() if pending else set()
    if missing:
        raise ValidationError("missing dependency", details={"ids": sorted(missing)})
    result = []
    while pending:
        ready = next((name for name, deps in pending.items() if not deps), None)
        if ready is None:
            raise ValidationError("dependency cycle", details={"ids": list(pending)})
        result.append(ready)
        del pending[ready]
        for deps in pending.values():
            deps.discard(ready)
    return result
