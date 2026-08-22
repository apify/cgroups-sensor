from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from .harness import (
    IMAGE,
    MEMORY_LIMIT,
    pull_image,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@pytest.fixture(scope='session')
def _docker() -> None:
    """Pull the probe image once, before the tests that use it."""
    pull_image(IMAGE)


@pytest.fixture
def parent_slice() -> Iterator[str]:
    """Create a slice carrying a memory limit, and remove it afterwards.

    A slice exists only while a unit lives in it, so a sleeping holder keeps it alive.
    """
    name = f'cgroups-sensor-e2e-{os.getpid()}.slice'
    holder = f'cgroups-sensor-e2e-holder-{os.getpid()}'

    steps = (
        ['sudo', 'systemd-run', '-q', '--unit', holder, '--slice', name, 'sleep', 'infinity'],
        ['sudo', 'systemctl', 'set-property', '--runtime', name, f'MemoryMax={MEMORY_LIMIT}'],
    )
    for step in steps:
        result = subprocess.run(step, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            pytest.fail(f'{" ".join(step)} exited {result.returncode}\n{result.stderr.strip()}')

    yield name

    subprocess.run(
        ['sudo', 'systemctl', 'stop', holder, name],
        capture_output=True,
        timeout=60,
        check=False,
    )


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
