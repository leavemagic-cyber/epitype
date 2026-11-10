"""Owner-configured core promotion evidence; project rules keep their own scope."""
import json
from pathlib import Path

CONFIG_FIELD = "core_min_distinct_projects"


def minimum(options):
    value = options.get(CONFIG_FIELD, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid core project minimum")
    return value


def inspect(fields, required, identities):
    """Check declared independent applicability, not incident counts or semantic truth."""
    from .fixed_load import identity
    value = fields.get("core_qualification")
    if isinstance(value, str):
        value = json.loads(value)
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("projects"), list)
            or not all(isinstance(value.get(key), str) and value[key].strip()
                       for key in ("reviewed_by", "reviewed_at"))):
        raise ValueError("core requires reviewed independent-project applicability")
    logical, physical, projects = set(), set(), []
    for project in value["projects"]:
        if (not isinstance(project, dict)
                or not all(isinstance(project.get(key), str) and project[key].strip()
                           for key in ("id", "root", "reason"))):
            raise ValueError("core project requires an id, root and applicability reason")
        root = Path(project["root"]).expanduser()
        if not root.is_absolute():
            raise ValueError("core project root must be absolute")
        root_key = str(root.resolve())
        if root_key not in identities:
            identities[root_key] = identity(root)
        key = identities[root_key]
        logical_key = project["id"].casefold()
        if logical_key in logical or key in physical:
            continue
        logical.add(logical_key);physical.add(key)
        projects.append({"id": project["id"], "root": root_key,
                         "identity": key, "reason": project["reason"]})
    if len(projects) < required:
        raise ValueError(f"core applicability has {len(projects)} independent projects; policy requires {required}")
    return {"minimum": required, "distinct_projects": len(projects), "projects": projects,
            "reviewed_by": value["reviewed_by"], "reviewed_at": value["reviewed_at"],
            "semantic_status": "REVIEWED_DECLARATION_NOT_AUTOMATIC_PROOF"}
