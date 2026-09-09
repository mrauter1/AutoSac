"""Run production Python through systemd, never through the caller's app environment.

Usage: python scripts/prod_exec.py [--service web|worker] -- -m alembic current
Requires permission to create transient system services. Does not start/stop AutoSac.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


COMMAND_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
PROPERTIES = (
    "User", "Group", "WorkingDirectory", "Environment", "EnvironmentFiles",
    "PassEnvironment", "UnsetEnvironment", "PAMName", "RootDirectory", "RootImage",
    "DynamicUser", "SupplementaryGroups",
)


class ConfigurationError(Exception):
    pass


def bus_json(*args):
    result = subprocess.run(
        ["busctl", "--system", "--json=short", *args],
        env=COMMAND_ENV, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["data"]


def service_config(service):
    path = bus_json(
        "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager", "LoadUnit", "s", f"autosac-{service}.service",
    )[0]
    return {
        name: bus_json("get-property", "org.freedesktop.systemd1", path,
                       "org.freedesktop.systemd1.Service", name)
        for name in PROPERTIES
    }


def environment(assignments):
    values = {}
    for assignment in assignments:
        key, separator, value = assignment.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key) or "\0" in value:
            raise ConfigurationError("Unsupported service environment assignment")
        values[key] = value
    return values


def validate_pair(web, worker):
    for config in (web, worker):
        for name in ("PassEnvironment", "UnsetEnvironment", "PAMName", "RootDirectory",
                     "RootImage", "DynamicUser", "SupplementaryGroups"):
            if config[name]:
                raise ConfigurationError(f"Unsupported systemd setting: {name}; operator review required")
        if not config["User"] or config["User"] == "root":
            raise ConfigurationError("A non-root production service user is required")
        if not Path(config["WorkingDirectory"]).is_absolute():
            raise ConfigurationError("Production WorkingDirectory must be absolute")
        if not config["EnvironmentFiles"]:
            raise ConfigurationError("Explicit production EnvironmentFiles are required")
        for path, optional in config["EnvironmentFiles"]:
            if optional or not path.startswith("/") or any(c.isspace() for c in path) or any(c in path for c in '%"\\'):
                raise ConfigurationError("Unsupported or optional EnvironmentFile; operator review required")
        if any("%" in value for value in environment(config["Environment"]).values()):
            raise ConfigurationError("Environment specifiers require operator review")
    for name in ("User", "Group", "WorkingDirectory", "EnvironmentFiles"):
        if web[name] != worker[name]:
            raise ConfigurationError(f"Web/worker {name} differs; operator review required")
    # PORT is a web listener setting, not an application-data configuration difference.
    web_env = {k: v for k, v in environment(web["Environment"]).items() if k != "PORT"}
    worker_env = {k: v for k, v in environment(worker["Environment"]).items() if k != "PORT"}
    if web_env != worker_env:
        raise ConfigurationError("Web/worker environments differ; operator review required")


def run_python(config, arguments):
    python = Path(config["WorkingDirectory"]) / ".venv/bin/python"
    if not python.is_file():
        raise ConfigurationError("Production Python interpreter is missing")
    # Inline Environment= values go first; systemd loads original EnvironmentFiles
    # afterwards, preserving its precedence and parsing (not shell/dotenv parsing).
    # The private temporary file keeps secret values out of process arguments.
    with tempfile.TemporaryDirectory(prefix="autosac-prod-exec-") as directory:
        path = Path(directory) / "environment"
        with path.open("w", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            for key, value in environment(config["Environment"]).items():
                escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
                stream.write(f'{key}="{escaped}"\n')
        command = [
            "systemd-run", "--system", "--quiet", "--wait", "--pipe", "--collect",
            "--no-ask-password", "--service-type=exec", "--expand-environment=no",
            f"--uid={config['User']}", f"--working-directory={config['WorkingDirectory']}",
            f"--property=EnvironmentFile={path}",
        ]
        if config["Group"]:
            command.append(f"--gid={config['Group']}")
        for file, _ in config["EnvironmentFiles"]:
            command.append(f"--property=EnvironmentFile={file}")
        command.extend(["--", str(python), *arguments])
        return subprocess.run(command, env=COMMAND_ENV, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("web", "worker"), default="worker")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if not arguments:
        parser.error("Specify production Python arguments after --")
    try:
        configs = {name: service_config(name) for name in ("web", "worker")}
        validate_pair(configs["web"], configs["worker"])
        return run_python(configs[args.service], arguments)
    except (ConfigurationError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        # Never print raw D-Bus output, environment values, or subprocess commands.
        message = str(error) if isinstance(error, ConfigurationError) else "Unable to read configuration or execute production command"
        print(f"prod_exec: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
