from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import prod_exec


def config(tmp_path):
    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    return {
        'User': 'rauter', 'Group': 'rauter', 'WorkingDirectory': str(tmp_path),
        'Environment': ['HOME=/home/rauter', 'PATH=/usr/bin', 'SECRET=a$"\\b'],
        'EnvironmentFiles': [[str(tmp_path / '.env'), False]],
        'PassEnvironment': [], 'UnsetEnvironment': [], 'PAMName': '',
        'RootDirectory': '', 'RootImage': '', 'DynamicUser': False,
        'SupplementaryGroups': [],
    }


def test_accepts_service_configuration_and_port_difference(tmp_path):
    worker = config(tmp_path)
    web = deepcopy(worker)
    web['Environment'].append('PORT=8001')
    prod_exec.validate_pair(web, worker)


@pytest.mark.parametrize('property,value', [
    ('User', 'root'), ('DynamicUser', True), ('PassEnvironment', ['DATABASE_URL']),
    ('UnsetEnvironment', ['HOME']), ('WorkingDirectory', 'relative'),
    ('EnvironmentFiles', [['/missing', True]]), ('EnvironmentFiles', [['/%n.env', False]]),
    ('Environment', ['HOME=%h']), ('EnvironmentFiles', []),
])
def test_refuses_unsupported_configuration(tmp_path, property, value):
    current = config(tmp_path)
    current[property] = value
    with pytest.raises(prod_exec.ConfigurationError):
        prod_exec.validate_pair(current, current)


def test_refuses_mismatched_data_environment_without_exposing_values(tmp_path):
    worker = config(tmp_path)
    web = deepcopy(worker)
    web['Environment'].append('DATABASE_URL=private-secret')
    with pytest.raises(prod_exec.ConfigurationError) as error:
        prod_exec.validate_pair(web, worker)
    assert 'private-secret' not in str(error.value)


def test_executor_isolates_caller_preserves_order_and_cleans_secrets(tmp_path, monkeypatch):
    current = config(tmp_path)
    monkeypatch.setenv('DATABASE_URL', 'wrong-database')
    observed = []

    def run(command, *, env, check):
        assert 'DATABASE_URL' not in env
        assert env == prod_exec.COMMAND_ENV
        assert not any('SECRET=' in arg for arg in command)
        assert '--expand-environment=no' in command
        assert '--uid=rauter' in command
        assert command[-3:] == [str(tmp_path / '.venv/bin/python'), '-c', 'print("$literal")']
        files = [arg.split('=', 2)[2] for arg in command if arg.startswith('--property=EnvironmentFile=')]
        assert files[1:] == [str(tmp_path / '.env')]
        secret_file = Path(files[0])
        assert secret_file.stat().st_mode & 0o777 == 0o600
        assert 'SECRET="a\\$\\"\\\\b"' in secret_file.read_text()
        observed.append(secret_file)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(prod_exec.subprocess, 'run', run)
    assert prod_exec.run_python(current, ['-c', 'print("$literal")']) == 7
    assert not observed[0].exists()


def test_bus_failure_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr(prod_exec.sys, 'argv', ['prod_exec.py', '--', '-m', 'alembic', 'current'])
    def fail(_):
        raise prod_exec.subprocess.CalledProcessError(1, ['secret-command'], stderr='secret')
    monkeypatch.setattr(prod_exec, 'service_config', fail)
    assert prod_exec.main() == 1
    assert 'secret' not in capsys.readouterr().err
