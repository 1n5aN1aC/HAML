"""Template files: reusable contest definitions (ADR-0003).

A template is a JSON file in server/templates/. Creating an Event copies the
template's content into the Event database, so these files are never read on
behalf of a live Event.
"""
import json
import re
from pathlib import Path

from db import BUILTIN_FIELDS

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
        # custom fields can't shadow a built-in — the name would be ambiguous
        # between the blob and the column (entry_list references built-ins by
        # their bare name)
        if name in BUILTIN_FIELDS:
            raise ValueError(f"field '{name}' collides with a built-in field name")
        seen.add(name)
        if not isinstance(field.get("label"), str) or not field["label"]:
            raise ValueError(f"field '{name}' needs a label")
        if not isinstance(field.get("required", False), bool):
            raise ValueError(f"field '{name}': 'required' must be a boolean")
        # remember: entry form re-fills this field from the most recent
        # contact with the same callsign (autofill on callsign blur)
        if not isinstance(field.get("remember", False), bool):
            raise ValueError(f"field '{name}': 'remember' must be a boolean")
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
    # entry_list drives the callsign-entry inputs; contact_list drives the
    # contact-log columns. Both are required and may name custom fields *or*
    # built-ins (a column can exist without an entry input, e.g. Country).
    known = seen | set(BUILTIN_FIELDS)
    _validate_display_list(template, "entry_list", known)
    _validate_display_list(template, "contact_list", known)


# Keys allowed on an object-form display-list entry (a per-event override of the
# field's registry/template defaults). A bare string is the no-override form.
_DISPLAY_ENTRY_KEYS = {"name", "required", "remember", "default"}


def _validate_display_list(template, key, known):
    """Validate a required entry_list/contact_list: a list of bare names or
    {name, required?, remember?, default?} objects; names unique and each a
    known custom field or built-in. Empty lists are allowed."""
    items = template.get(key)
    if not isinstance(items, list):
        raise ValueError(f"template needs a list '{key}'")
    names = []
    for item in items:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"'{key}' entry needs a string 'name'")
            extra = set(item) - _DISPLAY_ENTRY_KEYS
            if extra:
                raise ValueError(
                    f"'{key}' entry '{name}' has unknown keys: {', '.join(sorted(extra))}")
            for flag in ("required", "remember"):
                if flag in item and not isinstance(item[flag], bool):
                    raise ValueError(f"'{key}' entry '{name}': '{flag}' must be a boolean")
            if "default" in item and not isinstance(item["default"], str):
                raise ValueError(f"'{key}' entry '{name}': 'default' must be a string")
        else:
            raise ValueError(f"'{key}' entries must be strings or objects")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError(f"'{key}' has duplicate field names")
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"'{key}' names unknown fields: {', '.join(unknown)}")


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
