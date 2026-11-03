"""Non-secret form checkpoints, bound to one verified private host installation.

A checkpoint is never an executable plan or evidence that a connection works.
Host ownership and normal provider/transaction checks remain authoritative.
"""
import json
from daemons_companion.setup.plan import validate_name
from daemons_companion.openclaw.host_install import checked_file
from daemon_core.memory.integrity import atomic_write_text

SCHEMA = "daemon.setup-progress.v1"
FIELDS = {"name", "gender", "language", "provider_kind", "model"}
PROVIDERS = {"codex_cli", "mistral_api", "openai_api", "anthropic_api", "xai_api"}


def validate(fields):
    if not isinstance(fields, dict) or set(fields) - FIELDS:
        raise ValueError("Saved setup choices are invalid.")
    for key, value in fields.items():
        if not isinstance(value, str):
            raise ValueError("Saved setup choices are invalid.")
        if key == "name":
            validate_name(value)
        elif key == "gender" and value not in {"feminine", "masculine"}:
            raise ValueError("Saved daemon agreement is invalid.")
        elif key == "language" and value not in {"en", "fr"}:
            raise ValueError("Saved conversation language is invalid.")
        elif key == "provider_kind" and value not in PROVIDERS:
            raise ValueError("Saved provider selection is invalid.")
        elif key == "model" and (not value.strip() or len(value) > 160 or any(not c.isprintable() for c in value)):
            raise ValueError("Saved model selection is invalid.")
    return dict(fields)


def load(scope, host):
    scope.receipt()
    path = scope.attempt / "setup-progress.json"
    if not path.exists() and not path.is_symlink():
        return {}
    data = json.loads(checked_file(path, 8192))
    if (not isinstance(data, dict) or set(data) != {"schema", "installation_id", "host", "choices"}
            or data.get("schema") != SCHEMA or data.get("installation_id") != scope.installation_id
            or data.get("host") != host):
        raise ValueError("Saved setup choices do not belong to this installation.")
    return validate(data["choices"])


def save(scope, host, **changes):
    fields = load(scope, host)
    fields.update(validate(changes))
    path = scope.attempt / "setup-progress.json"
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Saved setup choices require a direct private file.")
    atomic_write_text(path, json.dumps(dict(schema=SCHEMA, installation_id=scope.installation_id,
                                          host=host, choices=fields)) + "\n")
    path.chmod(0o600)
