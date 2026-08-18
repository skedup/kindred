import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, cast

COMPANION_BYTE_CEILING = 4096
_BINDING_FIELDS = (
    "install_id",
    "plugin_digest",
    "host_identity",
    "session_id",
    "platform",
    "sender_id",
    "bundle_path",
)
_last_published: dict[str, str] = {}
_logger = logging.getLogger(__name__)


def _read_text(path: Path, label: str, *, optional: bool = False) -> str | None:
    try:
        if optional and not path.exists():
            return None
        if not path.is_file():
            raise OSError
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"{label} is unavailable") from exc


def load_binding(home: Path) -> dict[str, Any]:
    try:
        text = _read_text(home / ".kindred/mouth-binding.json", "Hermes binding")
        value = json.loads(text or "")
        valid = (
            isinstance(value, dict)
            and set(value) == {"schema_version", *_BINDING_FIELDS}
            and value.get("schema_version") == 3
            and all(
                isinstance(value.get(key), str) and value[key]
                for key in _BINDING_FIELDS
                if key != "host_identity"
            )
            and isinstance(value.get("host_identity"), list)
            and len(value["host_identity"]) == 2
            and all(
                type(item) is str and item.strip() == item and item
                for item in value["host_identity"]
            )
            and value.get("plugin_digest") == plugin_digest(Path(__file__).parent)
            and value["platform"] != "cli"
            and Path(value["bundle_path"]).is_absolute()
        )
        if not valid:
            raise ValueError
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("Hermes binding is invalid") from exc
    return cast(dict[str, Any], value)


def _context_bundle(binding: dict[str, Any]) -> str:
    bundle = _read_text(Path(binding["bundle_path"]), "context bundle")
    if bundle is None or not bundle.strip():
        raise RuntimeError("context bundle is unavailable")
    return bundle


def _companion(persona_dir: Path) -> str:
    sections = []
    for name in ("IDENTITY.md", "USER.md", "SOUL_excerpt.md"):
        content = _read_text(persona_dir / name, name, optional=name == "USER.md")
        if content is not None:
            sections.append(f"## {name}\n{content.strip()}")
    return (
        "[KINDRED COMPANION SNAPSHOT - CURRENT AUTHORITY]\n"
        "This snapshot supersedes every earlier Kindred companion snapshot in this session.\n"
        + "\n\n".join(sections)
        + "\n[END KINDRED COMPANION SNAPSHOT]"
    )


def plugin_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ("__init__.py", "plugin.yaml"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def validate_runtime_resources(home: Path) -> dict[str, Any]:
    """Validate binding and Persona resources that must exist before the first tick."""
    binding = load_binding(home)
    if len(_companion(home / ".kindred/persona").encode("utf-8")) > COMPANION_BYTE_CEILING:
        raise RuntimeError("Kindred companion exceeds its byte ceiling")
    return binding


def _on_pre_llm_call(**event: Any) -> dict[str, str] | None:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    try:
        binding = load_binding(home)
    except RuntimeError:
        return None
    if (
        not all(isinstance(event.get(key), str) and event[key] for key in ("turn_id", "task_id"))
        or event.get("parent_session_id")
        or any(event.get(key) != binding[key] for key in ("session_id", "platform", "sender_id"))
    ):
        return None

    try:
        bundle = _context_bundle(binding)
    except RuntimeError:
        return None
    context = f"[KINDRED CONTEXT BUNDLE]\n{bundle.strip()}\n[END KINDRED CONTEXT BUNDLE]"
    companion = _companion(home / ".kindred/persona")
    encoded = companion.encode("utf-8")
    if len(encoded) > COMPANION_BYTE_CEILING:
        _logger.warning("companion_overflow bytes=%d max=%d", len(encoded), COMPANION_BYTE_CEILING)
    elif _last_published.get(binding["session_id"]) != (
        digest := hashlib.sha256(encoded).hexdigest()
    ):
        context += f"\n\n{companion}"
        _last_published[binding["session_id"]] = digest
    return {"context": context}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
