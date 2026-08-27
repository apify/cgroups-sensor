from __future__ import annotations

import pytest

from .harness import (
    DISTRO_IMAGES,
    MEMORY_LIMIT,
    PYTHON_VERSIONS,
    QUOTA_CORES,
    TIGHTER_MEMORY_LIMIT,
    check_invariants,
    probe_command,
    probe_in_container,
    pull_image,
)

pytestmark = pytest.mark.usefixtures('_docker')


def test_no_limits() -> None:
    """Reports no restriction for a container that sets none, spelled `max` under cgroup v2 and a sentinel under v1."""
    reading = probe_in_container()

    check_invariants(reading)
    assert reading.memory_limit is None
    assert reading.cpu_limit is None
    if reading.raw_memory_limit is not None:
        assert 'memory-limit-covers-machine' in reading.notices


def test_memory_only() -> None:
    """Reads a memory limit. Nothing restricts the CPU, so no CPU limit is reported."""
    reading = probe_in_container('--memory', str(MEMORY_LIMIT))

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.working_set is not None
    assert reading.working_set < reading.memory_limit
    assert reading.cpu_limit is None


def test_cpu_quota() -> None:
    """Reads a fractional CPU quota."""
    reading = probe_in_container('--cpus', str(QUOTA_CORES))

    check_invariants(reading)
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.memory_limit is None


def test_cpu_set_only() -> None:
    """Reads a set of allowed cores, which restricts the CPU without any quota."""
    reading = probe_in_container('--cpuset-cpus', '0')

    check_invariants(reading)
    assert reading.cpu_limit == 1.0
    assert reading.raw_cpu_set_size == 1
    assert reading.raw_cpu_quota is None


def test_every_axis_at_once() -> None:
    """Takes the tighter of the two CPU axes. Here the quota is tighter than the set."""
    reading = probe_in_container(
        '--memory',
        str(MEMORY_LIMIT),
        '--cpus',
        str(QUOTA_CORES),
        '--cpuset-cpus',
        '0',
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.raw_cpu_set_size == 1


def test_host_cgroup_namespace() -> None:
    """Reads the limit with the cgroup namespace of the machine, not one of its own."""
    reading = probe_in_container('--memory', str(MEMORY_LIMIT), '--cgroupns=host')

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT

    # Under cgroup v2 the whole hierarchy is mounted, so the container sees its ancestry. Under cgroup v1
    # each controller is mounted at the container's own cgroup, which hides it whatever the namespace.
    if reading.sources['memory']['interface'] == 'cgroup-v2':
        assert len(reading.sources['memory']['levels']) > 1


def test_tighter_limit_inside_the_container() -> None:
    """Takes the tightest limit when two levels carry different ones, which is the shape kubelet produces."""
    # The kernel forbids a cgroup that both holds processes and delegates controllers, so the shell vacates
    # the root first, enables the controller, then moves in. `0` is how a process names itself to cgroupfs.
    setup = f"""
    set -e
    if [ -e /sys/fs/cgroup/cgroup.controllers ]; then
        mkdir -p /sys/fs/cgroup/init /sys/fs/cgroup/inner
        echo 0 > /sys/fs/cgroup/init/cgroup.procs
        echo +memory > /sys/fs/cgroup/cgroup.subtree_control
        echo {TIGHTER_MEMORY_LIMIT} > /sys/fs/cgroup/inner/memory.max
        echo 0 > /sys/fs/cgroup/inner/cgroup.procs
    else
        mkdir -p /sys/fs/cgroup/memory/inner
        echo {TIGHTER_MEMORY_LIMIT} > /sys/fs/cgroup/memory/inner/memory.limit_in_bytes
        echo 0 > /sys/fs/cgroup/memory/inner/cgroup.procs
    fi
    exec {probe_command()}
    """

    reading = probe_in_container(
        '--privileged',
        '--memory',
        str(MEMORY_LIMIT),
        command=['sh', '-c', setup],
    )

    check_invariants(reading)
    assert reading.memory_limit == TIGHTER_MEMORY_LIMIT


@pytest.mark.parametrize('image', DISTRO_IMAGES)
def test_distributions(image: str) -> None:
    """Reads the same limits on every distribution, including musl, whose `sysconf` once reported the affinity."""
    pull_image(image)

    reading = probe_in_container(
        '--memory',
        str(MEMORY_LIMIT),
        '--cpus',
        str(QUOTA_CORES),
        image=image,
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == QUOTA_CORES
    assert reading.cpu_usage is not None


@pytest.mark.parametrize('python_version', PYTHON_VERSIONS)
def test_python_versions(python_version: str) -> None:
    """Reads the same limits on every supported interpreter, in a container this time."""
    reading = probe_in_container(
        '--memory',
        str(MEMORY_LIMIT),
        '--cpus',
        str(QUOTA_CORES),
        python_version=python_version,
    )

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    assert reading.cpu_limit == QUOTA_CORES


@pytest.mark.systemd_slices
def test_limit_on_a_parent_cgroup(parent_slice: str) -> None:
    """Reads a limit set outside the container, on the slice it was put into, when it carries none of its own."""
    reading = probe_in_container('--cgroupns=host', f'--cgroup-parent={parent_slice}')

    check_invariants(reading)
    assert reading.memory_limit == MEMORY_LIMIT
    # The limit sits above the container's own cgroup.
    assert len(reading.sources['memory']['levels']) > 1
