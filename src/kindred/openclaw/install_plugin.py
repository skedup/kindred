"""OpenClaw CLI and Mouth Plugin lifecycle for the interactive installer."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kindred.config import KindredConfig
from kindred.mouth_host.model import OpenClawRuntimeModel

OPENCLAW_VERSION = "2026.6.10"
OPENCLAW_BUILD = "aa69b12"
OPENCLAW_PROTOCOL = 4
MOUTH_PLUGIN_ID = "kindred-mouth"
MOUTH_PLUGIN_VERSION = "0.4.0"
_MOUTH_PLUGIN_V1_DIGEST = "002a628ce6f3b0b84a4fd150fea3812ef196a77c1096cf6bdb0ce6b6d5dc7f60"
_MOUTH_PLUGIN_V2_DIGEST = "be6ec3e16734fa7b0441d060870449da5d1ebb508700846b18ffcc716594f4de"
_MOUTH_PLUGIN_V3_DIGEST = "4f8744c858305bc8eab7b937f942ea4fae2f70d6cf270a18bde63c85cd9d09a1"
_OPENCLAW_ENV_KEYS = (
    "HOME",
    "PATH",
    "OPENCLAW_HOME",
    "OPENCLAW_PROFILE",
    "OPENCLAW_STATE_DIR",
    "OPENCLAW_CONFIG_PATH",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "SHELL",
)
_OPENCLAW_CREDENTIAL_KEYS = frozenset({"OPENCLAW_GATEWAY_TOKEN"})


class OpenClawInstallError(RuntimeError):
    """安装失败；文本不得拼接 OpenClaw 原始输出或私有 wire。"""


@dataclass(frozen=True)
class OpenClawProfile:
    version: str
    build: str
    protocol: int


@dataclass(frozen=True)
class OpenClawIdentity:
    version: str
    build: str
    profile: OpenClawProfile | None


OPENCLAW_PROFILES = (
    OpenClawProfile(OPENCLAW_VERSION, OPENCLAW_BUILD, OPENCLAW_PROTOCOL),
    OpenClawProfile("2026.7.1-2", "0790d9f", OPENCLAW_PROTOCOL),
)
_UNVERIFIED_VERSION_WARNING = "当前 OpenClaw identity 尚未经过 Kindred 验证；将继续执行完整合同检查"


def _ops() -> Any:
    """Resolve the stable facade lazily so its patch seams stay authoritative."""
    from kindred.openclaw import install

    return install


def _openclaw_environment(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    ops = _ops()
    extra = dict(extra_env or {})
    if extra.keys() - ops._OPENCLAW_CREDENTIAL_KEYS:
        raise OpenClawInstallError("unsupported OpenClaw CLI environment")
    return {
        **{key: ops.os.environ[key] for key in ops._OPENCLAW_ENV_KEYS if key in ops.os.environ},
        **extra,
    }


def _command(
    argv: Sequence[str],
    *,
    optional_missing: bool = False,
    extra_env: Mapping[str, str] | None = None,
) -> str | None:
    ops = _ops()
    try:
        result = ops.subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            env=ops._openclaw_environment(extra_env),
        )
    except (OSError, ops.subprocess.TimeoutExpired) as exc:
        raise OpenClawInstallError("OpenClaw CLI is unavailable") from exc
    if result.returncode:
        if optional_missing and "not found" in result.stderr.lower():
            return None
        raise OpenClawInstallError("OpenClaw CLI command failed")
    return cast(str, result.stdout)


def _json(argv: Sequence[str], *, extra_env: Mapping[str, str] | None = None) -> Any:
    ops = _ops()
    text = ops._command(argv) if extra_env is None else ops._command(argv, extra_env=extra_env)
    try:
        return ops.json.loads(text or "")
    except ops.json.JSONDecodeError as exc:
        raise OpenClawInstallError("OpenClaw CLI JSON schema changed") from exc


def _require_version() -> OpenClawIdentity:
    ops = _ops()
    text = (ops._command(["openclaw", "--version"]) or "").strip()
    matched = re.fullmatch(
        r"OpenClaw ([0-9A-Za-z][0-9A-Za-z._+-]{0,63}) "
        r"\(([0-9A-Za-z][0-9A-Za-z._+-]{0,63})\)",
        text,
    )
    if matched is None:
        raise OpenClawInstallError("OpenClaw version identity schema changed")
    version, build = matched.groups()
    profile = next(
        (item for item in ops.OPENCLAW_PROFILES if (item.version, item.build) == (version, build)),
        None,
    )
    return OpenClawIdentity(version=version, build=build, profile=profile)


def _version_warning(identity: OpenClawIdentity | None) -> str | None:
    return (
        _UNVERIFIED_VERSION_WARNING if identity is not None and identity.profile is None else None
    )


def _agent_verbose(agent_id: str) -> tuple[int, str | None]:
    ops = _ops()
    raw = ops._json(["openclaw", "config", "get", "agents.list", "--json"])
    try:
        if not isinstance(raw, list):
            raise ValueError
        matches = [
            (index, row)
            for index, row in enumerate(raw)
            if isinstance(row, Mapping) and row.get("id") == agent_id
        ]
        if len(matches) != 1:
            raise ValueError
        index, row = matches[0]
        value = row.get("verboseDefault")
        if value not in (None, "off", "on", "full"):
            raise ValueError
        return index, cast(str | None, value)
    except (TypeError, ValueError) as exc:
        raise OpenClawInstallError("OpenClaw agent verbosity schema changed") from exc


def _agent_verbose_default(agent_id: str) -> str | None:
    ops = _ops()
    return cast(str | None, ops._agent_verbose(agent_id)[1])


def _configure_agent_verbose_off(agent_id: str) -> None:
    ops = _ops()
    index, value = ops._agent_verbose(agent_id)
    if value == "off":
        return
    ops.click.echo("将关闭所选 Mouth agent 的工具调用过程展示。")
    ops._command(
        [
            "openclaw",
            "config",
            "set",
            f"agents.list[{index}].verboseDefault",
            '"off"',
            "--strict-json",
        ]
    )
    if ops._agent_verbose_default(agent_id) != "off":
        raise OpenClawInstallError("OpenClaw agent verbosity update did not take effect")


def _inspect_plugin() -> Mapping[str, Any] | None:
    ops = _ops()
    argv = ["openclaw", "plugins", "inspect", ops.MOUTH_PLUGIN_ID, "--runtime", "--json"]
    report_text = ops._command(argv, optional_missing=True)
    if report_text is None:
        return None
    try:
        report = ops.json.loads(report_text)
    except ops.json.JSONDecodeError as exc:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted") from exc
    if not isinstance(report, Mapping):
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
    return report


def _require_plugin_report(
    report: Mapping[str, Any] | None,
    *,
    require_prompt_injection: bool,
) -> None:
    ops = _ops()
    if ops._plugin_generation(report, require_prompt_injection=require_prompt_injection) != "v4":
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")


def _plugin_generation(
    report: Mapping[str, Any] | None,
    *,
    require_prompt_injection: bool = False,
) -> str:
    ops = _ops()
    if report is None:
        return "absent"
    try:
        plugin, hooks = report["plugin"], report["typedHooks"]
        installed = ops.Path(plugin["source"]).parent
        version = plugin["version"]
        valid_header = (
            plugin["id"] == ops.MOUTH_PLUGIN_ID
            and plugin["status"] == "loaded"
            and plugin["origin"] == "global"
            and ops.Path(report["install"]["installPath"]).resolve() == installed.resolve()
            and report["install"]["source"] == "path"
            and any(item.get("name") == "before_prompt_build" for item in hooks)
        )
        digest = ops._plugin_tree_digest(installed)
        if version == ops.MOUTH_PLUGIN_VERSION:
            valid = digest == ops._plugin_tree_digest(ops.MOUTH_PLUGIN_DIR) and (
                not require_prompt_injection or report["policy"]["allowPromptInjection"] is True
            )
            generation = "v4"
        elif version == "0.3.0":
            valid, generation = digest == ops._MOUTH_PLUGIN_V3_DIGEST, "v3"
        elif version == "0.2.0":
            valid, generation = digest == ops._MOUTH_PLUGIN_V2_DIGEST, "v2"
        elif version == "0.1.0":
            valid, generation = digest == ops._MOUTH_PLUGIN_V1_DIGEST, "v1"
        else:
            valid, generation = False, "unknown"
    except (KeyError, OSError, TypeError) as exc:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted") from exc
    if not valid_header or not valid:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
    return generation


def _plugin_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _plugin(config: KindredConfig | None = None) -> None:
    ops = _ops()
    report = ops._inspect_plugin()
    plugin_state = ops._plugin_generation(report)
    binding_state = ops._binding_generation(config) if config is not None else "absent"
    if binding_state == "unknown":
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
    if plugin_state == "v4" and report is not None:
        try:
            prompt_policy = report["policy"].get("allowPromptInjection")
            if prompt_policy is True:
                ops._require_plugin_report(report, require_prompt_injection=True)
                return
            if prompt_policy not in (None, False):
                raise TypeError
        except (AttributeError, KeyError, TypeError) as exc:
            raise OpenClawInstallError("installed OpenClaw plugin contract drifted") from exc
    if plugin_state in {"v1", "v2", "v3"}:
        ops._command(["openclaw", "plugins", "uninstall", ops.MOUTH_PLUGIN_ID, "--force"])
        if ops._inspect_plugin() is not None:
            raise OpenClawInstallError("Kindred Mouth Plugin uninstall did not take effect")
        report = None
    if report is None:
        ops._command(["openclaw", "plugins", "install", str(ops.MOUTH_PLUGIN_DIR)])
        report = ops._inspect_plugin()
    ops._require_plugin_report(report, require_prompt_injection=False)
    ops._command(["openclaw", "plugins", "doctor"])
    ops._command(
        [
            "openclaw",
            "config",
            "set",
            f"plugins.entries.{ops.MOUTH_PLUGIN_ID}.hooks.allowPromptInjection",
            "true",
            "--strict-json",
        ]
    )
    report = ops._inspect_plugin()
    ops._require_plugin_report(report, require_prompt_injection=True)


def require_openclaw_runtime(
    config: KindredConfig,
    *,
    home: Path | None = None,
) -> OpenClawIdentity | None:
    """离线确认 OpenClaw、Plugin v4 与 private binding v3 一致。"""
    if not isinstance(config.mouth_host, OpenClawRuntimeModel):
        return None
    ops = _ops()
    identity = cast(OpenClawIdentity | None, ops._require_version())
    ops.require_openclaw_binding(config, home=home)
    ops._require_plugin_report(ops._inspect_plugin(), require_prompt_injection=True)
    return identity


def _uninstall_plugin() -> None:
    ops = _ops()
    report = ops._inspect_plugin()
    if report is None:
        return
    if ops._plugin_generation(report) not in {"v2", "v3", "v4"}:
        raise OpenClawInstallError("installed OpenClaw plugin contract drifted")
    ops._command(["openclaw", "plugins", "uninstall", ops.MOUTH_PLUGIN_ID, "--force"])
    if ops._inspect_plugin() is not None:
        raise OpenClawInstallError("Kindred Mouth Plugin uninstall did not take effect")


__all__ = ["OpenClawInstallError"]
