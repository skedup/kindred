from __future__ import annotations

import getpass
import os
import plistlib
import shlex
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
    if configured := os.environ.get("KINDRED_CONFIG"):
        return Path(configured)
    root = Path(os.environ.get(ENV_XDG_CONFIG_HOME, Path.home() / ".config"))
    return root / "kindred/config.yaml"


def install_services(config_path: Path, *, include_web: bool) -> tuple[str, ...]:
    platform, paths, rendered = _definitions(config_path)
    names = _NAMES if include_web or paths["web"].exists() else ("heart",)
    paths["heart"].parent.mkdir(parents=True, exist_ok=True)
    for name in names:
        _create_exact(
            paths[name],
            rendered[name],
            legacy=_legacy_service_definitions(platform, name, rendered[name]),
        )
    return names


def control_services(config_path: Path | None = None, *, action: str) -> tuple[str, ...]:
    platform, paths = _owned(config_path) if action == "start" else _recoverable_owned()
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
    platform, paths = _recoverable_owned()
    names = tuple(name for name in reversed(_NAMES) if paths[name].is_file())
    for name in names:
        paths[name].unlink()
    if platform == "linux" and names:
        _run(("systemctl", "--user", "daemon-reload"))
    return names


def service_status(config_path: Path | None = None) -> tuple[tuple[str, bool, bool, bool], ...]:
    platform, paths = _recoverable_owned()
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
    platform, paths = _recoverable_owned()
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
        service_path = _service_search_path(python)
        home = Path(os.environ.get("HOME", Path.home())).expanduser().resolve()
    except Exception as exc:
        raise PlatformServiceError("service executable or config is missing or invalid") from exc
    platform, paths = _service_paths()
    resource = files("kindred.service_templates")
    template = resource.joinpath("launchd.plist" if platform == "darwin" else "systemd.service")
    rendered = {}
    for name in _NAMES:
        command = "run" if name == "heart" else "serve"
        argv = [
            "/usr/bin/env",
            "-i",
            f"HOME={home}",
            f"PATH={service_path}",
            f"KINDRED_CONFIG={config_path}",
            str(python),
            "-I",
            "-m",
            "kindred.cli",
            command,
            "--config",
            str(config_path),
        ]
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
                .replace(
                    "__ENVIRONMENT__",
                    "",
                )
            )
    return platform, paths, rendered


def _service_paths() -> tuple[str, dict[str, Path]]:
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
    return platform, paths


def _owned(config_path: Path | None) -> tuple[str, dict[str, Path]]:
    platform, paths, rendered = _definitions(config_path)
    for name, path in paths.items():
        if path.exists() and (
            not path.is_file() or path.read_text(encoding="utf-8") != rendered[name]
        ):
            raise PlatformServiceError(f"Kindred service file drifted: {path.name}")
    return platform, paths


def _recoverable_owned() -> tuple[str, dict[str, Path]]:
    platform, paths = _service_paths()
    for name, path in paths.items():
        if path.exists() and not _is_kindred_definition(platform, name, path):
            raise PlatformServiceError(f"Kindred service file drifted: {path.name}")
    return platform, paths


def _is_kindred_definition(
    platform: str,
    name: str,
    path: Path,
) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    command = "run" if name == "heart" else "serve"
    try:
        if platform == "linux":
            actual_text = path.read_text(encoding="utf-8")
            commands = [
                line.removeprefix("ExecStart=")
                for line in actual_text.splitlines()
                if line.startswith("ExecStart=")
            ]
            if len(commands) != 1:
                return False
            environments = [
                line for line in actual_text.splitlines() if line.startswith("Environment=")
            ]
            identity = _kindred_argv_identity(shlex.split(commands[0].replace("%%", "%")), command)
            if identity is None:
                return False
            outer_path = None
            if identity[2] is None:
                if not environments:
                    pass
                elif len(environments) != 1:
                    return False
                else:
                    assignments = shlex.split(
                        environments[0].removeprefix("Environment=").replace("%%", "%")
                    )
                    if len(assignments) != 1 or not assignments[0].startswith("PATH="):
                        return False
                    outer_path = assignments[0].removeprefix("PATH=")
                    if not _is_safe_service_path(outer_path):
                        return False
            elif environments:
                return False
            expected = (
                files("kindred.service_templates")
                .joinpath("systemd.service")
                .read_text()
                .replace("__DESCRIPTION__", f"Kindred {name.title()}")
                .replace("__EXEC_START__", commands[0])
            )
            if outer_path is not None:
                expected = expected.replace("__ENVIRONMENT__", environments[0])
            elif identity[2] is None:
                expected = expected.replace("__ENVIRONMENT__\n", "")
            else:
                expected = expected.replace("__ENVIRONMENT__", "")
            return actual_text == expected
        actual_bytes = path.read_bytes()
        payload = plistlib.loads(actual_bytes)
        argv = payload.get("ProgramArguments")
        environment = payload.get("EnvironmentVariables")
        stdout, stderr = payload.get("StandardOutPath"), payload.get("StandardErrorPath")
        expected = plistlib.loads(
            files("kindred.service_templates").joinpath("launchd.plist").read_bytes()
        )
        expected.update(
            Label=_LABEL[name],
            ProgramArguments=argv,
            StandardOutPath=stdout,
            StandardErrorPath=stderr,
        )
        if environment is not None:
            expected["EnvironmentVariables"] = environment
        identity = _kindred_argv_identity(argv, command)
        if identity is None:
            return False
        if identity[2] is None:
            valid_environment = environment is None or (
                isinstance(environment, dict)
                and set(environment) == {"PATH"}
                and _is_safe_service_path(environment.get("PATH"))
            )
        else:
            valid_environment = environment is None
        return (
            actual_bytes == plistlib.dumps(expected, sort_keys=True)
            and valid_environment
            and isinstance(stdout, str)
            and isinstance(stderr, str)
            and Path(stdout).is_absolute()
            and Path(stderr).is_absolute()
        )
    except (KeyError, OSError, TypeError, ValueError, plistlib.InvalidFileException):
        return False


def _kindred_argv_identity(
    argv: object, command: str
) -> tuple[Path, Path, dict[str, str] | None] | None:
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        return None
    if len(argv) == 6 and argv[1:5] == ["-m", "kindred.cli", command, "--config"]:
        python, config = Path(argv[0]), Path(argv[5])
        return (python, config, None) if python.is_absolute() and config.is_absolute() else None
    if len(argv) != 12 or argv[:2] != ["/usr/bin/env", "-i"]:
        return None
    try:
        environment = dict(value.split("=", 1) for value in argv[2:5])
    except ValueError:
        return None
    if set(environment) != {"HOME", "PATH", "KINDRED_CONFIG"}:
        return None
    python, config = Path(argv[5]), Path(argv[11])
    if (
        argv[6:11] != ["-I", "-m", "kindred.cli", command, "--config"]
        or not python.is_absolute()
        or not config.is_absolute()
        or not Path(environment["HOME"]).is_absolute()
        or not _is_safe_service_path(environment["PATH"])
        or environment["KINDRED_CONFIG"] != str(config)
    ):
        return None
    return python, config, environment


def _quote(value: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise PlatformServiceError("service argument contains a control character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _service_search_path(python: Path) -> str:
    home = Path(os.environ.get("HOME", Path.home()))
    paths = [python.parent, home / ".local/bin"]
    if sys.platform == "darwin":
        paths.extend((Path("/opt/homebrew/opt/node@22/bin"), Path("/opt/homebrew/bin")))
    paths.extend(
        (
            Path("/usr/local/bin"),
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/sbin"),
            Path("/sbin"),
        )
    )
    result = os.pathsep.join(dict.fromkeys(str(path) for path in paths))
    if not _is_safe_service_path(result):
        raise PlatformServiceError("service PATH is invalid")
    return result


def _is_safe_service_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parts = value.split(os.pathsep)
    return all(
        part and Path(part).is_absolute() and not any(ord(char) < 32 for char in part)
        for part in parts
    )


def _legacy_service_definitions(platform: str, name: str, content: str) -> tuple[str, str]:
    command = "run" if name == "heart" else "serve"
    if platform == "linux":
        current = next(
            line.removeprefix("ExecStart=")
            for line in content.splitlines()
            if line.startswith("ExecStart=")
        )
        identity = _kindred_argv_identity(shlex.split(current.replace("%%", "%")), command)
        if identity is None or identity[2] is None:
            raise PlatformServiceError("current service definition is invalid")
        python, config, environment = identity
        assert environment is not None
        legacy_argv = [str(python), "-m", "kindred.cli", command, "--config", str(config)]
        template = files("kindred.service_templates").joinpath("systemd.service").read_text()
        base = template.replace("__DESCRIPTION__", f"Kindred {name.title()}").replace(
            "__EXEC_START__", " ".join(_quote(value) for value in legacy_argv)
        )
        path_assignment = _quote("PATH=" + environment["PATH"])
        return (
            base.replace("__ENVIRONMENT__\n", ""),
            base.replace("__ENVIRONMENT__", f"Environment={path_assignment}"),
        )
    payload = plistlib.loads(content.encode())
    identity = _kindred_argv_identity(payload.get("ProgramArguments"), command)
    if identity is None or identity[2] is None:
        raise PlatformServiceError("current service definition is invalid")
    python, config, environment = identity
    assert environment is not None
    payload["ProgramArguments"] = [
        str(python),
        "-m",
        "kindred.cli",
        command,
        "--config",
        str(config),
    ]
    plain = plistlib.dumps(payload, sort_keys=True).decode()
    payload["EnvironmentVariables"] = {"PATH": environment["PATH"]}
    return plain, plistlib.dumps(payload, sort_keys=True).decode()


def _create_exact(path: Path, content: str, *, legacy: tuple[str, ...] = ()) -> None:
    if path.exists():
        if not path.is_file():
            raise PlatformServiceError(f"Kindred service file drifted: {path.name}")
        current = path.read_text(encoding="utf-8")
        if current == content:
            return
        if current not in legacy:
            raise PlatformServiceError(f"Kindred service file drifted: {path.name}")
        staged = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with staged.open("x", encoding="utf-8") as stream:
                stream.write(content)
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
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
