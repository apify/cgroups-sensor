from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from .harness import (
    IMAGE,
    MEMORY_LIMIT,
    PODMAN,
    QUOTA_CORES,
    ROOTFUL_PODMAN,
    pull_image,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@pytest.fixture(scope='session')
def _docker() -> None:
    """Pull the probe image once, before the tests that use it."""
    pull_image(IMAGE)


@pytest.fixture(scope='session')
def _podman() -> None:
    """Pull the probe image into the rootless podman store, which is the user's own."""
    pull_image(IMAGE, PODMAN)


@pytest.fixture(scope='session')
def _rootful_podman() -> None:
    """Pull the probe image into root's podman store, a third one again."""
    pull_image(IMAGE, ROOTFUL_PODMAN)


def _limited_slice(*properties: str, system: bool) -> Iterator[str]:
    """Create a slice carrying the given resource properties, and remove it afterwards.

    A slice exists only while a unit lives in it, so a sleeping holder keeps it alive.
    """
    name = f'cgroups-sensor-e2e-{os.getpid()}.slice'
    holder = f'cgroups-sensor-e2e-holder-{os.getpid()}'

    # A system slice needs root. A user one is where a rootless engine puts its containers.
    run_unit = ['sudo', 'systemd-run'] if system else ['systemd-run', '--user']
    control = ['sudo', 'systemctl'] if system else ['systemctl', '--user']

    steps = (
        [*run_unit, '-q', '--unit', holder, '--slice', name, 'sleep', 'infinity'],
        [*control, 'set-property', '--runtime', name, *properties],
    )
    for step in steps:
        result = subprocess.run(step, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            pytest.fail(f'{" ".join(step)} exited {result.returncode}\n{result.stderr.strip()}')

    yield name

    subprocess.run(
        [*control, 'stop', holder, name],
        capture_output=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def parent_slice() -> Iterator[str]:
    """A system slice carrying a memory limit, for the containers root's engine starts."""
    yield from _limited_slice(f'MemoryMax={MEMORY_LIMIT}', system=True)


@pytest.fixture
def quota_slice() -> Iterator[str]:
    """A system slice carrying a CPU quota, which throttles everything below it as one."""
    yield from _limited_slice(f'CPUQuota={int(QUOTA_CORES * 100)}%', system=True)


@pytest.fixture
def user_parent_slice() -> Iterator[str]:
    """The same, on this user's own manager, for what a rootless engine starts. Nothing here is root's."""
    yield from _limited_slice(f'MemoryMax={MEMORY_LIMIT}', system=False)


@pytest.fixture
def systemd_scope() -> Iterator[Callable[..., list[str]]]:
    """Build `systemd-run` wrappers, and stop whatever scopes the test started.

    A system scope needs sudo. Ask for it with `system=True`.
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
