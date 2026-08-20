from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from .harness import (
    CANNOT_SET_UP,
    MEMORY_LIMIT,
    PYTHON_VERSIONS,
    QUOTA_CORES,
    check_invariants,
    is_unified,
    machine_cpu_count,
    machine_memory_bytes,
    notices_about,
    probe_here,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_unrestricted() -> None:
    """Reads this machine as it is. Whatever it reports has to be coherent."""
    reading = probe_here()

    check_invariants(reading)
    # A Linux machine always has a cgroup, even when nothing is limited there.
    assert reading.sources['memory'] is not None
    assert reading.machine_cpu_count == machine_cpu_count()
    assert reading.machine_memory_bytes == machine_memory_bytes()


@pytest.mark.parametrize('python_version', PYTHON_VERSIONS)
def test_python_versions(python_version: str) -> None:
    """Reads the same machine facts on every supported interpreter.

    The version is a real axis here. `os.cpu_count()` started honoring `PYTHON_CPU_COUNT` in 3.13, and the
    filters compare against these numbers.
    """
    reading = probe_here(python_version=python_version)

    check_invariants(reading)
    assert reading.machine_cpu_count == machine_cpu_count()
    assert reading.machine_memory_bytes == machine_memory_bytes()


@pytest.mark.usefixtures('_systemd_user', '_unified')
def test_memory_limit(systemd_scope: Callable[..., list[str]]) -> None:
    """Reads a memory limit set on the scope this process runs in."""
    reading = probe_here(systemd_scope(f'MemoryMax={MEMORY_LIMIT}'))

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    # The floor `check_invariants` applies is the memory the probe charged to this cgroup on purpose.
    assert reading.working_set is not None
    assert reading.working_set < reading.memory_limit
    # The machine can carry unrelated notices, e.g. about a CPU set covering every core.
    assert not notices_about(reading, 'memory')


@pytest.mark.usefixtures('_systemd_user', '_unified')
def test_cpu_quota(systemd_scope: Callable[..., list[str]]) -> None:
    """Reads a CPU quota set on the scope this process runs in."""
    reading = probe_here(systemd_scope(f'CPUQuota={int(QUOTA_CORES * 100)}%'))

    check_invariants(reading)
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.raw_cpu_quota == QUOTA_CORES


@pytest.mark.usefixtures('_systemd_user', '_unified')
def test_memory_limit_above_the_machine(systemd_scope: Callable[..., list[str]]) -> None:
    """Drops a memory limit larger than the machine, and says so.

    Nothing rejects such a limit. A container given more memory than the node it landed on gets one.
    """
    limit = machine_memory_bytes() + 1024**3
    reading = probe_here(systemd_scope(f'MemoryMax={limit}'))

    check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.raw_memory_limit == limit
    assert 'memory-limit-covers-machine' in reading.notices


@pytest.mark.usefixtures('_systemd_user', '_unified')
def test_cpu_quota_above_the_machine(systemd_scope: Callable[..., list[str]]) -> None:
    """Drops a CPU quota larger than the machine, and says so.

    systemd writes the quota as asked. A Kubernetes limit above node capacity does the same.
    """
    quota_cores = machine_cpu_count() + 1
    reading = probe_here(systemd_scope(f'CPUQuota={quota_cores * 100}%'))

    check_invariants(reading)
    assert reading.cpu_limit is None
    assert reading.raw_cpu_quota == quota_cores
    assert 'cpu-quota-covers-machine' in reading.notices


@pytest.mark.usefixtures('_sudo', '_systemd_system', '_two_cores')
def test_every_axis_at_once(systemd_scope: Callable[..., list[str]]) -> None:
    """Reads all three limits at once. The tighter of the two CPU axes wins.

    A system scope, not a user one: under cgroup v1 a user scope gets no cgroup in the resource controllers,
    and `AllowedCPUs=` needs a delegated cpuset that only the unified hierarchy has.
    """
    properties = [f'MemoryMax={MEMORY_LIMIT}', f'CPUQuota={int(QUOTA_CORES * 100)}%']
    if is_unified():
        properties.append('AllowedCPUs=0')

    reading = probe_here(systemd_scope(*properties, system=True))

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    # The quota is half a core, the set is a whole one.
    assert reading.cpu_limit == QUOTA_CORES
    if is_unified():
        assert reading.raw_cpu_set_size == 1


@pytest.mark.usefixtures('_systemd_user', '_unified')
def test_limit_on_an_ancestor(systemd_scope: Callable[..., list[str]]) -> None:
    """Reads a limit from an ancestor when the own cgroup carries no memory files at all.

    `Delegate=yes` lets the probe move into a child cgroup. That child inherits no memory controller, so the
    sensor has to walk up past a level with nothing to read.
    """
    wrapper = systemd_scope(f'MemoryMax={MEMORY_LIMIT}', 'Delegate=yes')
    move_into_leaf = [
        'bash',
        '-c',
        (
            'set -e; '
            'own=$(grep "^0::" /proc/self/cgroup | cut -d: -f3); '
            'leaf="/sys/fs/cgroup$own/leaf"; '
            # What a delegated scope is for. Where systemd delegates it differently - the Fedora guest, with a
            # much newer one - the shape cannot be built, and this says so instead of probing the scope itself
            # and blaming the reading. The errno of whichever step failed travels out on stderr.
            # `0` is the kernel's word for "the writing process". An explicit PID takes the path meant for
            # moving somebody else, which the kernel of Fedora 43 refuses with EINVAL where the older one of
            # Ubuntu 22.04 allowed it.
            f'{{ mkdir -p "$leaf" && echo 0 > "$leaf/cgroup.procs"; }} || {{ '
            # Where it still fails, say what the kernel refused and who owns what a delegated scope hands
            # over - the two things that tell a missing delegation apart from a refused move.
            'echo "own=$own type=$(cat "/sys/fs/cgroup$own/cgroup.type" 2>&1)"'
            ' "subtree=[$(cat "/sys/fs/cgroup$own/cgroup.subtree_control" 2>&1)]"'
            ' "as=$(id -u):$(id -g)" "owner=$(stat -c %U:%G:%a "$leaf/cgroup.procs" 2>&1)" >&2; '
            f'exit {CANNOT_SET_UP}; }}; '
            'exec "$@"'
        ),
        '--',
    ]
    reading = probe_here([*wrapper, *move_into_leaf])

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT

    # The walk starts at the leaf, which carries no memory files at all, and finds the limit above it.
    levels = reading.sources['memory']['levels']
    assert levels[0].endswith('/leaf')
    assert len(levels) > 1
    # The chain says where it looked, this says where the limit came from.
    assert reading.memory_limit_level in levels[1:]


def test_expected_interface() -> None:
    """Check that this run reached the cgroup interface it was started for.

    A guest booted for cgroup v1 that comes up on v2 runs a smaller suite and reports success, which is the
    one way this lane can lie. `E2E_INTERFACE=cgroup-v1` makes it say so instead.

    The limits are what has to come from that interface. The CPU counter is allowed to differ, for the reason
    `Reading.limit_interfaces` gives.
    """
    expected = os.environ.get('E2E_INTERFACE')
    if not expected:
        pytest.skip('E2E_INTERFACE names no interface to check')

    reading = probe_here()

    assert reading.limit_interfaces, 'no mechanism carries any limit here'
    assert reading.limit_interfaces == {expected}
