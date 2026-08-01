from __future__ import annotations

import getpass
import os
import plistlib
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from kindred.config import ENV_XDG_CONFIG_HOME, load_kindred_config

_NAMES = ("heart", "web")
_LABEL = {name: f"com.kindred.{name}" for name in _NAMES}
_UNIT = {name: f"kindred-{name}.service" for name in _NAMES}


class PlatformServiceError(RuntimeError):
    pass


def service_config_path() -> Path:
    root = Path(os.environ.get(ENV_XDG_CONFIG_HOME, Path.home() / ".config"))
    return root / "kindred/config.yaml"


def install_services(config_path: Path, *, include_web: bool) -> tuple[str, ...]:
    _, paths, rendered = _definitions(config_path)
    names = _NAMES if include_web else ("heart",)
    paths["heart"].parent.mkdir(parents=True, exist_ok=True)
    for name in names:
        _create_exact(paths[name], rendered[name])
    return names


def control_services(config_path: Path | None = None, *, action: str) -> tuple[str, ...]:
    platform, paths = _owned(config_path)
    names = tuple(name for name in _NAMES if paths[name].is_file())
    if action == "start" and (not names or names[0] != "heart"):
        raise PlatformServiceError("Kindred Heart service is not installed")
    ordered = names if action == "start" else tuple(reversed(names))
    if platform == "linux" and ordered:
        if action == "start":
            if _run(("systemctl", "--user", "show-environment"), check=False)[0]:
                raise PlatformServiceError("systemd user manager is unavailable; use `kindred run`")
            user = getpass.getuser()
            linger = ("loginctl", "show-user", user, "-p", "Linger", "--value")
            code, value = _run(linger, check=False)
            if code or value.strip().lower() != "yes":
                raise PlatformServiceError(
                    f"Linux background mode requires Linger=yes; ask an administrator to run "
                    f"`sudo loginctl enable-linger {user}`"
                )
            _run(("systemctl", "--user", "daemon-reload"))
        verb = ("enable", "--now") if action == "start" else ("disable", "--now")
        for name in ordered:
            _run(("systemctl", "--user", *verb, _UNIT[name]))
    elif platform == "darwin":
        domain = f"gui/{os.getuid()}"
        for name in ordered:
            missing = bool(_run(("launchctl", "print", f"{domain}/{_LABEL[name]}"), check=False)[0])
            if action == "start":
                if missing:
                    _run(("launchctl", "bootstrap", domain, str(paths[name])))
                _run(("launchctl", "kickstart", f"{domain}/{_LABEL[name]}"))
            elif not missing:
                _run(("launchctl", "bootout", domain, str(paths[name])))
    return ordered


def uninstall_services(config_path: Path | None = None) -> tuple[str, ...]:
    platform, paths = _owned(config_path)
    names = tuple(name for name in reversed(_NAMES) if paths[name].is_file())
    for name in names:
        paths[name].unlink()
    if platform == "linux" and names:
        _run(("systemctl", "--user", "daemon-reload"))
    return names


def service_status(config_path: Path | None = None) -> tuple[tuple[str, bool, bool, bool], ...]:
    platform, paths = _owned(config_path)
    result = []
    for name in _NAMES:
        installed = paths[name].is_file()
        if platform == "linux" and installed:
            enabled = not _run(("systemctl", "--user", "is-enabled", _UNIT[name]), check=False)[0]
            active = not _run(("systemctl", "--user", "is-active", _UNIT[name]), check=False)[0]
        elif installed:
            code, output = _run(
                ("launchctl", "print", f"gui/{os.getuid()}/{_LABEL[name]}"), check=False
            )
            enabled, active = code == 0, code == 0 and "state = running" in output
        else:
            enabled = active = False
        result.append((name, installed, enabled, active))
    return tuple(result)


def show_logs(service: str, *, follow: bool, lines: int, config_path: Path | None = None) -> None:
    platform, paths = _owned(config_path)
    if service not in _NAMES or not paths.get(service, Path()).is_file():
        raise PlatformServiceError("Kindred service is not installed")
    if platform == "linux":
        argv = ["journalctl", "--user", "-u", _UNIT[service], "-n", str(lines)]
    else:
        payload = plistlib.loads(paths[service].read_bytes())
        argv = ["tail", "-n", str(lines), payload["StandardOutPath"], payload["StandardErrorPath"]]
    _run(tuple([*argv, *(("-f",) if follow else ())]), capture=False)


def _definitions(config_path: Path | None) -> tuple[str, dict[str, Path], dict[str, str]]:
    try:
        if config_path is None:
            config_path = service_config_path()
        config_path = config_path.expanduser().resolve(strict=True)
        run_dir = load_kindred_config(config_path, load_secrets=False).paths.run_dir
        python = Path(sys.executable).absolute()
        if not python.is_file():
            raise FileNotFoundError(python)
    except Exception as exc:
        raise PlatformServiceError("service executable or config is missing or invalid") from exc
    if sys.platform == "darwin":
        platform, root = "darwin", Path.home() / "Library/LaunchAgents"
        paths = {name: root / f"{_LABEL[name]}.plist" for name in _NAMES}
    elif sys.platform.startswith("linux"):
        platform = "linux"
        root = Path(os.environ.get(ENV_XDG_CONFIG_HOME, Path.home() / ".config"))
        paths = {name: root / "systemd/user" / _UNIT[name] for name in _NAMES}
    else:
        raise PlatformServiceError(
            "unsupported platform; use foreground `kindred run` and optional `kindred serve`"
        )
    resource = files("kindred.service_templates")
    template = resource.joinpath("launchd.plist" if platform == "darwin" else "systemd.service")
    rendered = {}
    for name in _NAMES:
        command = "run" if name == "heart" else "serve"
        argv = [str(python), "-m", "kindred.cli", command, "--config", str(config_path)]
        for value in argv:
            _quote(value)
        if platform == "darwin":
            payload = plistlib.loads(template.read_bytes())
            payload.update(
                Label=_LABEL[name],
                ProgramArguments=argv,
                StandardOutPath=str(run_dir / f"{name}.stdout.log"),
                StandardErrorPath=str(run_dir / f"{name}.stderr.log"),
            )
            rendered[name] = plistlib.dumps(payload, sort_keys=True).decode()
        else:
            command_line = " ".join(_quote(value) for value in argv)
            rendered[name] = (
                template.read_text()
                .replace("__DESCRIPTION__", f"Kindred {name.title()}")
                .replace("__EXEC_START__", command_line)
            )
    return platform, paths, rendered


def _owned(config_path: Path | None) -> tuple[str, dict[str, Path]]:
    platform, paths, rendered = _definitions(config_path)
    for name, path in paths.items():
        if path.exists() and (
            not path.is_file() or path.read_text(encoding="utf-8") != rendered[name]
        ):
            raise PlatformServiceError(f"Kindred service file drifted: {path.name}")
    return platform, paths


def _quote(value: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise PlatformServiceError("service argument contains a control character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _create_exact(path: Path, content: str) -> None:
    if path.exists():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise PlatformServiceError(f"Kindred service file drifted: {path.name}")
        return
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except OSError as exc:
        raise PlatformServiceError(f"cannot install Kindred service: {path.name}") from exc


def _run(argv: tuple[str, ...], *, check: bool = True, capture: bool = True) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, text=capture, capture_output=capture, check=False)
    except OSError as exc:
        raise PlatformServiceError("platform service command is unavailable") from exc
    if check and result.returncode:
        raise PlatformServiceError("platform service command failed")
    return result.returncode, result.stdout if capture else ""
