"""Template files: reusable contest definitions (ADR-0003).

A template is a JSON file in server/templates/. Creating an Event copies the
template's content into the Event database, so these files are never read on
behalf of a live Event.
"""
import json
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

FIELD_TYPES = {"text", "number", "choice"}


def validate_template(template):
    """Raise ValueError if the template JSON is malformed."""
    if not isinstance(template, dict):
        raise ValueError("template must be a JSON object")
    if not isinstance(template.get("name"), str) or not template["name"]:
        raise ValueError("template needs a name")
    for key in ("bands", "modes"):
        values = template.get(key)
        if (not isinstance(values, list) or not values
                or not all(isinstance(v, str) for v in values)):
            raise ValueError(f"template needs a non-empty string list '{key}'")
    dupe_key = template.get("dupe_key")
    if not isinstance(dupe_key, list) or not all(isinstance(v, str) for v in dupe_key):
        raise ValueError("template needs a string list 'dupe_key' (may be empty)")
    fields = template.get("fields")
    if not isinstance(fields, list):
        raise ValueError("template needs a list 'fields' (may be empty)")
    seen = set()
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("each field must be an object")
        name = field.get("name")
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError(f"field has a missing or duplicate name: {name!r}")
        seen.add(name)
        if not isinstance(field.get("label"), str) or not field["label"]:
            raise ValueError(f"field '{name}' needs a label")
        if field.get("type") not in FIELD_TYPES:
            raise ValueError(f"field '{name}' has bad type (want {sorted(FIELD_TYPES)})")
        if not isinstance(field.get("required", False), bool):
            raise ValueError(f"field '{name}': 'required' must be a boolean")
        if field["type"] == "choice":
            options = field.get("options")
            if (not isinstance(options, list) or not options
                    or not all(isinstance(o, str) for o in options)):
                raise ValueError(f"choice field '{name}' needs a string list 'options'")


def list_templates(templates_dir=TEMPLATES_DIR):
    """Return [{id, name}] for every valid template file, sorted by id."""
    result = []
    for path in sorted(Path(templates_dir).glob("*.json")):
        try:
            template = json.loads(path.read_text(encoding="utf-8"))
            validate_template(template)
        except (ValueError, json.JSONDecodeError):
            continue  # a broken file shouldn't take down the listing
        result.append({"id": path.stem, "name": template["name"]})
    return result


def load_template(template_id, templates_dir=TEMPLATES_DIR):
    """Load and validate one template by id (filename stem)."""
    path = Path(templates_dir) / f"{template_id}.json"
    if not path.is_file():
        raise ValueError(f"no such template: {template_id}")
    template = json.loads(path.read_text(encoding="utf-8"))
    validate_template(template)
    return template
