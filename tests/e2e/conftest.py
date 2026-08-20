from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .harness import (
    CAPABILITIES,
    IMAGE,
    MEMORY_LIMIT,
    REQUIRED_CAPABILITIES,
    TIMEOUT_SECONDS,
    command_works,
    have,
    is_unified,
    machine_cpu_count,
    unavailable,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

TWO_CORES = 2


def pytest_sessionstart() -> None:
    """Reject a capability name this suite does not know, which would otherwise disarm the gate silently.

    The raise is what ends the run, with pytest's own usage-error code. Nothing reads `session.exitstatus`
    this early, unlike in `pytest_sessionfinish` below, where setting it is the only way to fail a run.
    """
    unknown = REQUIRED_CAPABILITIES - CAPABILITIES
    if unknown:
        raise pytest.UsageError(f'E2E_REQUIRE names {", ".join(sorted(unknown))}, not one of {sorted(CAPABILITIES)}')


@pytest.fixture(scope='session', autouse=True)
def _linux_only() -> None:
    """Skip the whole suite where cgroups cannot exist."""
    if not Path('/proc/self/cgroup').exists():
        pytest.skip('cgroups exist on Linux only')


@pytest.fixture(scope='session')
def _docker() -> None:
    """Skip unless a working Docker is available, and pull the probe image once."""
    if not have('docker') or not command_works(['docker', 'info']):
        unavailable('docker', 'docker is not available')

    subprocess.run(
        ['docker', 'pull', '--quiet', IMAGE],
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture
def _sudo() -> None:
    """Skip unless sudo runs without asking for a password."""
    if not have('sudo') or not command_works(['sudo', '-n', 'true']):
        unavailable('sudo', 'passwordless sudo is not available')


@pytest.fixture
def _systemd_user() -> None:
    """Skip unless this user has a systemd manager that can hold a scope."""
    if not have('systemd-run') or not command_works(['systemd-run', '--user', '--scope', '-q', 'true']):
        unavailable('systemd', 'a systemd user manager is not available')


@pytest.fixture
def _systemd_system() -> None:
    """Skip unless a system scope can be started, which needs both sudo and a running systemd.

    The command is tried rather than the binary looked for: a machine can carry `systemd-run` and boot with
    another init, and then every scope fails instead of skipping.
    """
    if not have('systemd-run') or not command_works(['sudo', '-n', 'systemd-run', '--scope', '-q', 'true']):
        unavailable('systemd', 'a system systemd manager is not available')


@pytest.fixture
def _unified() -> None:
    """Skip unless the controllers live on the cgroup v2 unified hierarchy.

    Under cgroup v1 systemd delegates only `name=systemd` to user sessions, so a user scope gets no cgroup in
    the resource controllers and its properties do nothing.
    """
    if not is_unified():
        pytest.skip('the controllers are not on the unified hierarchy')


@pytest.fixture
def _systemd_cgroup_driver() -> None:
    """Skip unless docker puts its containers under systemd slices.

    `--cgroup-parent` takes a slice name only under that driver.
    """
    result = subprocess.run(
        ['docker', 'info', '--format', '{{.CgroupDriver}}'],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.stdout.strip() != 'systemd':
        pytest.skip(f'docker uses the {result.stdout.strip() or "unknown"!r} cgroup driver, this needs systemd')


@pytest.fixture
def parent_slice() -> Iterator[str]:
    """Create a slice carrying a memory limit, and remove it afterwards.

    A slice exists only while a unit lives in it, so a sleeping holder keeps it alive.
    """
    name = f'cgroups-sensor-e2e-{os.getpid()}.slice'
    holder = f'cgroups-sensor-e2e-holder-{os.getpid()}'

    subprocess.run(
        ['sudo', 'systemd-run', '-q', '--unit', holder, '--slice', name, 'sleep', 'infinity'],
        capture_output=True,
        timeout=60,
        check=True,
    )
    subprocess.run(
        ['sudo', 'systemctl', 'set-property', '--runtime', name, f'MemoryMax={MEMORY_LIMIT}'],
        capture_output=True,
        timeout=60,
        check=True,
    )

    yield name

    subprocess.run(
        ['sudo', 'systemctl', 'stop', holder, name],
        capture_output=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def _two_cores() -> None:
    """Skip where pinning one core would not restrict anything."""
    if machine_cpu_count() < TWO_CORES:
        pytest.skip('a single-core machine cannot be restricted to one core')


@pytest.fixture
def systemd_scope() -> Iterator[Callable[..., list[str]]]:
    """Build `systemd-run` wrappers, and stop whatever scopes the test started.

    A system scope needs sudo. Ask for it with `system=True`, together with the `_sudo` fixture.
    """
    units: list[tuple[str, bool]] = []

    def build(*properties: str, system: bool = False) -> list[str]:
        unit = f'cgroups-sensor-e2e-{os.getpid()}-{len(units)}'
        units.append((unit, system))

        wrapper = ['sudo'] if system else []
        wrapper += ['systemd-run', '--scope', '-q', '--unit', unit]
        wrapper += [] if system else ['--user']
        for prop in properties:
            wrapper += ['-p', prop]

        return wrapper

    yield build

    for unit, system in units:
        stop = ['systemctl', '--user'] if not system else ['sudo', 'systemctl']
        subprocess.run([*stop, 'stop', f'{unit}.scope'], capture_output=True, check=False, timeout=60)


_EXECUTED: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record the tests that ran, for the guard below."""
    if report.when == 'call' and report.passed:
        _EXECUTED.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a run in which nothing ran at all.

    Every test here skips when its environment is missing, so an all-skipped suite would exit 0 and prove
    nothing. That is the one outcome this suite must never report as success. Collecting is not running, so a
    plain `--collect-only` is left alone.
    """
    if session.config.getoption('--collect-only'):
        return

    if exitstatus == 0 and not _EXECUTED:
        session.exitstatus = 1
        print('\nthe e2e suite proved nothing: every test skipped')
