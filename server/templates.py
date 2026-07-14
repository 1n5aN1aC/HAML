"""Template files: reusable contest definitions (ADR-0003).

A template is a JSON file in server/templates/. Creating an Event copies the
template's content into the Event database, so these files are never read on
behalf of a live Event.
"""
import json
import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# How the client decides a Contact is a dupe (advisory only, ADR-0003).
DUPLICATE_TYPES = {"band-mode", "any", "band-mode-day", "none"}

TEMPLATE_ID_RE = re.compile(r"^[a-z0-9_-]+$")

# The example template is living documentation on disk, not a usable contest
# definition, so it's hidden from the listing the client sees.
HIDDEN_TEMPLATE_IDS = {"example"}


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
    if template.get("duplicate_type") not in DUPLICATE_TYPES:
        raise ValueError(
            f"template needs a 'duplicate_type', one of {sorted(DUPLICATE_TYPES)}")
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
        if not isinstance(field.get("required", False), bool):
            raise ValueError(f"field '{name}': 'required' must be a boolean")
        # remember: entry form re-fills this field from the most recent
        # contact with the same callsign (autofill on callsign blur)
        if not isinstance(field.get("remember", False), bool):
            raise ValueError(f"field '{name}': 'remember' must be a boolean")
        # order drives the entry form's sort; missing values would compare NaN
        order = field.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            raise ValueError(f"field '{name}' needs an integer 'order'")
        default = field.get("default")
        if default is not None and not isinstance(default, str):
            raise ValueError(f"field '{name}': 'default' must be a string")
        max_length = field.get("max_length")
        if (not isinstance(max_length, int) or isinstance(max_length, bool)
                or max_length < 1):
            raise ValueError(f"field '{name}' needs a positive integer 'max_length'")
        validation = field.get("validation")
        if validation is not None:
            if not isinstance(validation, dict) or set(validation) != {"pattern", "message"}:
                raise ValueError(
                    f"field '{name}': 'validation' must be an object with"
                    " exactly 'pattern' and 'message'")
            pattern = validation["pattern"]
            if not isinstance(pattern, str) or not pattern:
                raise ValueError(f"field '{name}': validation 'pattern' must be a non-empty string")
            try:
                # sanity check only — the pattern actually runs in the client's
                # JS regex engine, so stick to the common dialect subset
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"field '{name}': bad validation pattern: {exc}")
            if not isinstance(validation["message"], str) or not validation["message"]:
                raise ValueError(f"field '{name}': validation 'message' must be a non-empty string")
    contact_list = template.get("contact_list")
    if contact_list is not None:
        if (not isinstance(contact_list, list)
                or not all(isinstance(n, str) for n in contact_list)):
            raise ValueError("'contact_list' must be a list of field names")
        if len(set(contact_list)) != len(contact_list):
            raise ValueError("'contact_list' has duplicate field names")
        unknown = [n for n in contact_list if n not in seen]
        if unknown:
            raise ValueError(f"'contact_list' names unknown fields: {', '.join(unknown)}")


def list_templates(templates_dir=TEMPLATES_DIR):
    """Return [{id, name}] for every valid template file, sorted by id."""
    result = []
    for path in sorted(Path(templates_dir).glob("*.json")):
        if path.stem in HIDDEN_TEMPLATE_IDS:
            continue
        try:
            template = json.loads(path.read_text(encoding="utf-8"))
            validate_template(template)
        except (ValueError, json.JSONDecodeError):
            continue  # a broken file shouldn't take down the listing
        result.append({"id": path.stem, "name": template["name"]})
    return result


def load_template(template_id, templates_dir=TEMPLATES_DIR):
    """Load and validate one template by id (filename stem)."""
    # the id must be a bare filename stem — no separators or traversal
    if Path(template_id).name != template_id:
        raise ValueError(f"no such template: {template_id}")
    path = Path(templates_dir) / f"{template_id}.json"
    if not path.is_file():
        raise ValueError(f"no such template: {template_id}")
    template = json.loads(path.read_text(encoding="utf-8"))
    validate_template(template)
    return template


def save_template(template_id, template, templates_dir=TEMPLATES_DIR):
    """Validate and write a template file by id, creating or overwriting.
    Overwriting is safe: live Events use a frozen copy of the config."""
    if not isinstance(template_id, str) or not TEMPLATE_ID_RE.match(template_id):
        raise ValueError("template id must match [a-z0-9_-]+")
    validate_template(template)
    path = Path(templates_dir) / f"{template_id}.json"
    path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")


def delete_template(template_id, templates_dir=TEMPLATES_DIR):
    """Delete a template file by id. Returns False when no such template."""
    # the id must be a bare filename stem — no separators or traversal
    if Path(template_id).name != template_id:
        return False
    path = Path(templates_dir) / f"{template_id}.json"
    if not path.is_file():
        return False
    path.unlink()
    return True
