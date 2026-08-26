from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

from ._cpu_list import count_cpu_list

_PROC_SELF_CGROUP = Path('/proc/self/cgroup')
"""Lists the cgroup this process belongs to. One line per mounted hierarchy."""

_PROC_SELF_MOUNTINFO = Path('/proc/self/mountinfo')
"""Lists the mounted filesystems. Read to locate the hierarchies instead of assuming `/sys/fs/cgroup`."""

_MICROSECONDS_PER_SECOND = 1_000_000
_NANOSECONDS_PER_SECOND = 1_000_000_000

_CONVENTIONAL_MOUNT_POINT = Path('/sys/fs/cgroup')
"""Where a hierarchy is mounted unless someone chose otherwise. Used only to break a tie."""


@dataclass(frozen=True)
class _FileNames:
    """What one cgroup interface calls the files this module reads.

    The two interfaces differ in the names far more than in the meaning, so the names live here and nowhere
    else. Each file is also the probe that says its controller is present at a level.
    """

    memory_limit: str
    """Holds the hard memory limit."""

    memory_usage: str
    """Holds the memory charged to the cgroup, page cache included."""

    memory_stat: str
    """Holds the breakdown of that memory, as `<key> <value>` lines."""

    inactive_file: str
    """The `memory_stat` key holding the reclaimable file cache."""

    cpu_quota: str
    """Holds the CPU bandwidth quota."""

    cpu_usage: str
    """Holds the consumed CPU time."""

    cpu_set: str
    """Holds the cores the cgroup may run on."""


_V2 = _FileNames(
    memory_limit='memory.max',
    memory_usage='memory.current',
    memory_stat='memory.stat',
    inactive_file='inactive_file',
    cpu_quota='cpu.max',
    cpu_usage='cpu.stat',
    cpu_set='cpuset.cpus.effective',
)

_V1 = _FileNames(
    memory_limit='memory.limit_in_bytes',
    memory_usage='memory.usage_in_bytes',
    memory_stat='memory.stat',
    inactive_file='total_inactive_file',
    cpu_quota='cpu.cfs_quota_us',
    cpu_usage='cpuacct.usage',
    cpu_set='cpuset.cpus',
)

_V2_UNLIMITED = 'max'
"""How cgroup v2 spells "no limit" in every file that can hold one. cgroup v1 has no such word: its memory
limit carries a sentinel near 2**63, and its CPU quota a negative number."""

_V1_CPU_PERIOD = 'cpu.cfs_period_us'
"""The period a cgroup v1 quota is spelled against. It has no place in the table above: cgroup v2 keeps the
quota and the period in one file."""

_V1_CPU_SET_EFFECTIVE = 'cpuset.effective_cpus'
"""What a cgroup v1 cpuset may really run on, inheritance resolved. `cpuset.cpus` holds what was configured
there, and an empty one means "whatever the parent allows"."""

_V2_CPU_CONTROLLER = 'cpu'
"""How `cgroup.controllers` names the controller that accounts CPU time."""

_V2_CONTROLLERS = 'cgroup.controllers'
"""Lists the controllers bound to a cgroup v2 group. It carries no metric, so it has no place in the table
either - it is read to tell a real unified hierarchy from the controller-less one a hybrid machine mounts."""


@dataclass(frozen=True)
class _Metric:
    """A metric this module reads, and how each interface says a level carries it.

    The three fields are one fact stated three ways, so they are named together rather than passed apart. A
    caller names the metric; which controller and which probe follow from it.
    """

    v1_controller: str
    """The cgroup v1 controller carrying the metric. Several can share one mount, as `cpu,cpuacct` usually do."""

    v2_probe: str
    """The file that exists only where the cgroup v2 hierarchy carries the metric."""

    v1_probe: str
    """The file cgroup v1 is probed by. Not always the v2 one renamed: a cpuset is probed by the configured
    set under v1 and by the effective set under v2."""


_MEMORY = _Metric(v1_controller='memory', v2_probe=_V2.memory_usage, v1_probe=_V1.memory_usage)
_CPU_QUOTA = _Metric(v1_controller='cpu', v2_probe=_V2.cpu_quota, v1_probe=_V1.cpu_quota)
_CPU_SET = _Metric(v1_controller='cpuset', v2_probe=_V2.cpu_set, v1_probe=_V1.cpu_set)

_V1_CPU_ACCT = 'cpuacct'
"""The cgroup v1 controller counting consumed CPU time. `_locate_cpu_usage` locates it, because `cpu.stat`
says nothing about where the cgroup v2 metrics live."""

_V1_CONTROLLER_NAMES = frozenset({_V1_CPU_ACCT, *(metric.v1_controller for metric in (_MEMORY, _CPU_QUOTA, _CPU_SET))})
"""The controllers worth recording: the metrics above, plus the one counting consumed CPU time."""


class _UnreadableFileError(Exception):
    """A control file is there, and says nothing this module can use.

    No consumer ever sees this: it travels from the level that could not be read up to the reader, which turns
    it into a reading of nothing plus the name of that level. Skipping the level instead would answer with a
    looser limit from an ancestor - the one wrong answer this module must never give, because a consumer sizes
    a budget from it and the kernel then enforces something tighter.
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(f'{directory} holds a control file that says nothing usable')
        self.directory = directory
        """The level that could not be read."""


@dataclass(frozen=True)
class _Mount:
    """One line of the mount table, as far as this module cares."""

    root: str
    """The subtree of the filesystem this mount exposes."""

    point: Path
    """The directory it is mounted at."""

    filesystem: str
    """The kind of filesystem, e.g. `cgroup2`."""

    options: str
    """The options of the filesystem itself. A cgroup v1 mount names its controllers among them."""


@dataclass(frozen=True)
class _Hierarchy:
    """A mounted cgroup hierarchy and the cgroup this process belongs to in it."""

    mount_point: Path
    """The directory the hierarchy is mounted at."""

    mount_root: str
    """The subtree of the hierarchy the mount exposes. Spelled the same way as `own_path`."""

    own_path: str
    """The cgroup this process belongs to, as `/proc/self/cgroup` spells it."""


@dataclass(frozen=True)
class _Hierarchies:
    """The cgroup hierarchies this process belongs to. The two interfaces are kept apart.

    They are not interchangeable. Each spells its control files differently, so which one a controller was
    found in decides how it is read afterwards.
    """

    unified: _Hierarchy | None
    """The cgroup v2 hierarchy, where one is mounted and covers this process. A single hierarchy serves
    whichever controllers are bound to it."""

    v1: dict[str, _Hierarchy]
    """The cgroup v1 hierarchies, keyed by controller name. Controllers sharing a mount get an entry each."""


@dataclass(frozen=True)
class Controller:
    """A located controller: its interface and the directories holding its control files."""

    is_v2: bool
    """Whether the controller provides the cgroup v2 interface. It spells the control files differently."""

    dirs: tuple[Path, ...]
    """The cgroup of this process first, then its ancestors up to the mount point.

    A limit on an ancestor caps everything below it. Under Kubernetes the container, the pod and the QoS class
    each get a level of their own. Levels that carry none of the controller's files are kept: a limit can be
    written to one of them at any time, and discovery is only made once.
    """

    @property
    def names(self) -> _FileNames:
        """How this controller's interface spells its files."""
        return _V2 if self.is_v2 else _V1


@dataclass(frozen=True)
class Controllers:
    """The controllers that carry resource metrics, as located for this process."""

    memory: Controller | None
    """Carries the memory limit and the memory charged against it."""

    cpu_quota: Controller | None
    """Carries the CPU bandwidth quota."""

    cpu_usage: Controller | None
    """Carries the consumed CPU time. Under cgroup v1 that is `cpuacct`, a controller of its own."""

    cpu_set: Controller | None
    """Carries the set of cores the cgroup may run on."""


@dataclass(frozen=True)
class RawCpuQuota:
    """A CPU bandwidth quota and the level it binds at."""

    cores: float
    """The number of cores the quota allows, possibly fractional."""

    limit_directory: Path
    """The cgroup holding the quota.

    The kernel throttles that level as a whole, siblings included. It is often not the group of this process:
    a systemd scope carries no quota of its own, and the slice above it does.
    """

    usage_directory: Path | None
    """Where the CPU time of that level is counted, or `None` when nothing counts it.

    A rate measured anywhere else would divide the time of one scope by the limit of another, so `None` means
    no rate can be measured against this quota at all.
    """


@dataclass(frozen=True)
class RawCpuSet:
    """A set of allowed cores, the level it was read at, and where the CPU time it restricts is counted."""

    cores: int
    """The number of cores the set allows."""

    limit_directory: Path
    """The cgroup holding the set.

    Not necessarily the group of this process: a set is inherited, so the closest level carrying one restricts
    everything below it.
    """

    usage_directory: Path | None
    """Where the CPU time of the level the set applies to is counted, or `None` when nothing counts it.

    As for a quota, a rate measured anywhere else would divide the time of one cgroup by the cores allowed to
    another, so `None` means no rate can be measured against this set at all.
    """


@dataclass(frozen=True)
class RawCpu:
    """Everything the CPU control files say, read in one go.

    The two restrictions are read together because a level that says nothing usable concerns both: reporting
    one of them while the other is unknown would hand out a limit that is not the tightest.
    """

    quota: RawCpuQuota | None
    """The bandwidth quota, or `None` when no level sets one."""

    cpu_set: RawCpuSet | None
    """The set of allowed cores, or `None` when no level sets one."""

    unreadable_directory: Path | None
    """The level whose control file says nothing usable, or `None` when every level answered.

    Both readings are dropped when this is set. The file exists, so what it holds is unknown rather than
    absent, and an ancestor's looser number is not a substitute for it.
    """


@dataclass(frozen=True)
class _MemoryLevel:
    """One level of the chain that holds a memory limit."""

    limit: int
    """The limit that level holds, in bytes."""

    usage: int | None
    """The memory charged to that level, in bytes, or `None` when it cannot be read."""

    directory: Path
    """The cgroup the two were read from."""


@dataclass(frozen=True)
class RawMemory:
    """The memory limit and usage as the control files spell them."""

    limit: int | None
    """The tightest limit along the chain, in bytes. `None` when no level holds one.

    Taken as read: cgroup v1 spells "no limit" as a sentinel near 2**63, and it passes through here.
    """

    working_set: int | None
    """The memory charged against `limit`, in bytes. Excludes reclaimable file cache.

    Not the content of one file. Where several levels hold a limit, `limit - working_set` is the memory that
    can still be allocated before the tightest of them is reached, whichever level that is. An out-of-memory
    kill follows from that distance, so it is the quantity worth preserving. `None` when the tightest limit
    has no usage to pair it with.
    """

    limit_directory: Path | None
    """The cgroup holding `limit`, or `None` when no level holds one.

    The whole chain is walked and the tightest limit wins, which is often not the one of this process. The
    number alone therefore does not say which level it came from.
    """

    unreadable_directory: Path | None
    """The level whose limit file says nothing usable, or `None` when every level answered.

    The reading is dropped when this is set, rather than falling back to a level that did answer: every such
    level is looser, and the kernel enforces the one that did not.
    """


_NO_MEMORY = RawMemory(limit=None, working_set=None, limit_directory=None, unreadable_directory=None)
"""What is reported where no level holds a memory limit at all."""


def read_memory() -> RawMemory:
    """Read the tightest memory limit along the chain, with a usage that keeps the distance to it.

    An out-of-memory kill follows from how much memory can still be allocated, not from a ratio. The smallest
    distance to a limit anywhere along the chain is therefore the quantity carried over, expressed against the
    tightest limit so that a consumer sees one comparable pair.
    """
    controller = locate_controllers().memory
    if controller is None:
        return _NO_MEMORY

    try:
        levels = _read_memory_levels(controller)
    except _UnreadableFileError as unreadable:
        return RawMemory(
            limit=None,
            working_set=None,
            limit_directory=None,
            unreadable_directory=unreadable.directory,
        )

    if not levels:
        return _NO_MEMORY

    tightest = min(levels, key=lambda level: level.limit)

    # Any level can be the closest to its own limit: memory is charged up the whole chain, so an ancestor
    # counts what its other children use as well. A pod at 96 of its 100 bytes can sit under a QoS class at
    # 498 of 500 - the tighter limit is the class's, the tighter distance the pod's. Which is why every level
    # is measured below, and not just this one.
    #
    # Any level whose usage cannot be read leaves one distance unknown, and an unknown may be the smallest.
    # Taking the minimum of what is left would then promise memory the kernel will not give, so the pair is
    # dropped and the ceiling reported alone.
    if any(level.usage is None for level in levels):
        return RawMemory(
            limit=tightest.limit,
            working_set=None,
            limit_directory=tightest.directory,
            unreadable_directory=None,
        )

    distances = [level.limit - level.usage for level in levels if level.usage is not None]

    # The tightest level is among these, so the smallest distance never exceeds its limit and the working set
    # never goes negative. It can still exceed the limit the other way: a cgroup sits above its limit while
    # the kernel reclaims, which makes that level's distance negative.
    used = min(tightest.limit - min(distances), tightest.limit)

    return RawMemory(
        limit=tightest.limit,
        working_set=used,
        limit_directory=tightest.directory,
        unreadable_directory=None,
    )


def _read_memory_levels(controller: Controller) -> list[_MemoryLevel]:
    """Read the limit and the usage of every level along the chain that holds a limit.

    A level with no readable usage is kept: its limit still caps everything below it, and dropping it would
    raise the ceiling above what the kernel enforces.

    Raises:
        _UnreadableFileError: If a level holds a limit file that says nothing usable.
    """
    levels = []

    for directory in controller.dirs:
        # Only the hard limit counts. `memory.high` throttles reclaim instead of killing, so a cgroup can sit
        # above it indefinitely.
        limit = _read_limit(directory / controller.names.memory_limit)
        if limit is None:
            continue

        # Zero is a limit, and the tightest one a cgroup can hold. A negative number of bytes is not a limit
        # at all, and no kernel writes one - so it is a file this module cannot use rather than an absent one.
        if limit < 0:
            raise _UnreadableFileError(directory)

        levels.append(_MemoryLevel(limit=limit, usage=_read_working_set(controller, directory), directory=directory))

    return levels


def read_cpu() -> RawCpu:
    """Read both CPU restrictions, and the level that said nothing usable if there was one."""
    try:
        return RawCpu(quota=read_cpu_quota(), cpu_set=read_cpu_set_size(), unreadable_directory=None)
    except _UnreadableFileError as unreadable:
        return RawCpu(quota=None, cpu_set=None, unreadable_directory=unreadable.directory)


def read_cpu_quota() -> RawCpuQuota | None:
    """Read the tightest CPU bandwidth quota along the chain, and the level it binds at.

    Returns:
        The quota, or `None` when no level sets one. Taken as read: a quota can exceed the cores of the
        machine.

    Raises:
        _UnreadableFileError: If a level holds a quota file that says nothing usable. `read_cpu` turns that into a
            reading of nothing, which is why it is the call the sensor layer makes.
    """
    controller = locate_controllers().cpu_quota
    if controller is None:
        return None

    read_quota = _read_cpu_quota_v2 if controller.is_v2 else _read_cpu_quota_v1

    # Levels without a readable quota drop out. An unlimited ancestor must not hide a quota set below it.
    quotas = [(quota, directory) for directory in controller.dirs if (quota := read_quota(directory)) is not None]
    if not quotas:
        return None

    cores, directory = min(quotas, key=lambda found: found[0])

    return RawCpuQuota(
        cores=cores,
        limit_directory=directory,
        usage_directory=_cpu_usage_dir(directory, controller),
    )


def read_cpu_set_size() -> RawCpuSet | None:
    """Read the set of CPU cores the cgroup of this process may run on.

    The cgroup is read, not the process affinity. A `taskset` narrows one process without narrowing the cgroup.

    Returns:
        The set, or `None` when no level sets one. Taken as read: a set can cover every core of the machine.

    Raises:
        _UnreadableFileError: If the level holds a set that says nothing usable. `read_cpu` turns that into a
            reading of nothing.
    """
    controller = locate_controllers().cpu_set
    if controller is None:
        return None

    # Only the closest level carrying a set is read. Under cgroup v2 the effective set already accounts for
    # the ancestors, and under cgroup v1 the effective file below does the same.
    directory = _first_with(controller.dirs, controller.names.cpu_set)
    if directory is None:
        return None

    cores = _read_cpu_set(directory, controller)
    if cores is None:
        return None

    # The set restricts the level it was read at, so that is the level whose time it has to be compared
    # against. It is looked up exactly as a quota's is: taking whatever level happens to carry a counter would
    # pair a set of two cores with the CPU time of an ancestor, or of the whole machine.
    return RawCpuSet(
        cores=cores,
        limit_directory=directory,
        usage_directory=_cpu_usage_dir(directory, controller),
    )


def _read_cpu_set(directory: Path, controller: Controller) -> int | None:
    """Count the cores one level allows. `None` when it sets none.

    Under cgroup v1 the configured file is empty where the set is inherited, and the effective file resolves
    that - so it is read first, and the configured one answers on kernels too old to have it. cgroup v2 spells
    only the effective set, which already accounts for the ancestors.

    Raises:
        _UnreadableFileError: If a file is there and holds no list of cores.
    """
    names = (controller.names.cpu_set,) if controller.is_v2 else (_V1_CPU_SET_EFFECTIVE, controller.names.cpu_set)

    for name in names:
        cpu_list = _read_control_file(directory / name)
        if cpu_list is None:
            continue

        # An empty file is how cgroup v1 spells an inherited set. The effective file above answers for it.
        if not cpu_list:
            continue

        cores = count_cpu_list(cpu_list)
        if cores is None:
            raise _UnreadableFileError(directory)

        return cores

    return None


def read_cpu_usage(directory: Path | None = None) -> float | None:
    """Read the CPU time consumed since the cgroup was created, in seconds.

    Args:
        directory: The level to read. Defaults to the closest level along the chain that counts any time. A
            quota that binds on an ancestor has to be paired with the time consumed at that ancestor, siblings
            included.

    Returns:
        The cumulative CPU time. `None` when it cannot be read.
    """
    controller = locate_controllers().cpu_usage
    if controller is None:
        return None

    level = directory if directory is not None else _first_with(controller.dirs, controller.names.cpu_usage)
    if level is None:
        return None

    path = level / controller.names.cpu_usage

    # cgroup v2 keeps the time among other counters, in microseconds. cgroup v1 gives it a file of its own,
    # in nanoseconds.
    if controller.is_v2:
        microseconds = _read_stat_value(path, 'usage_usec')
        return microseconds / _MICROSECONDS_PER_SECOND if microseconds is not None else None

    nanoseconds = _read_counter(path)
    return nanoseconds / _NANOSECONDS_PER_SECOND if nanoseconds is not None else None


@lru_cache(maxsize=1)
def locate_controllers() -> Controllers:
    """Locate the control files carrying the resource metrics of this process.

    This is the whole discovery walk, and the rest of this module reads what it finds. It goes:

    1. `/proc/self/cgroup` says which cgroup this process belongs to, once per hierarchy. The unified
       hierarchy is the line with no controller named, and each cgroup v1 hierarchy has a line of its own.
    2. `/proc/self/mountinfo` says where those hierarchies are mounted. Nothing here assumes `/sys/fs/cgroup`,
       and a mount can expose a subtree rather than the whole tree.
    3. A mount counts as ours only where it exposes the cgroup from step 1 and that cgroup exists under it.
       The same hierarchy can be mounted several times, and the other mounts belong to somebody else.
    4. Each controller is then located per metric, because a machine can serve different metrics through
       different interfaces. The unified hierarchy wins where it carries the metric, and cgroup v1 answers
       where it does not. The counter of consumed CPU time takes one more test, for the reason
       `_locate_cpu_usage` gives.
    5. What is recorded per controller is a chain of directories: the cgroup of this process first, then its
       ancestors up to the mount point. `Controller.dirs` says why the whole chain is kept.

    Discovery walks `/proc` and is cached for the lifetime of the process. The control files themselves are
    read again on every sample.
    """
    try:
        hierarchies = _read_hierarchies()
    except (OSError, ValueError):
        # Not Linux, `/proc` is not mounted, or its content does not decode. Nothing to read either way.
        return Controllers(memory=None, cpu_quota=None, cpu_usage=None, cpu_set=None)

    return Controllers(
        memory=_locate_controller(hierarchies, _MEMORY),
        cpu_quota=_locate_controller(hierarchies, _CPU_QUOTA),
        cpu_usage=_locate_cpu_usage(hierarchies),
        cpu_set=_locate_controller(hierarchies, _CPU_SET),
    )


def clear_cache() -> None:
    """Forget the located controllers. The next reading discovers them again."""
    locate_controllers.cache_clear()


# A child of `fork` inherits what its parent discovered. A pre-fork server whose supervisor puts each worker
# into a cgroup of its own would then have every child reading the parent's levels. Discovery is two files.
if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=clear_cache)


def _locate_cpu_usage(hierarchies: _Hierarchies) -> Controller | None:
    """Locate the counter of consumed CPU time. The hierarchy carrying the limits wins.

    Every other metric is probed by a file that exists only where its controller does. `cpu.stat` is not such
    a file: a cgroup v2 group has one whether or not the CPU controller is enabled for it, so its presence
    says nothing about where the metrics live. A hybrid machine mounts a controller-less cgroup2 next to the
    cgroup v1 controllers, and names this process as belonging to another cgroup there - counting the time of
    somebody else's group. So the unified hierarchy answers here only where it carries controllers at all, or
    where nothing else counts anything. Under cgroup v1 the accounting is `cpuacct`, a controller of its own,
    which can be mounted apart from the quota.
    """
    unified = hierarchies.unified
    with_controllers = unified if unified is not None and _carries_controller(unified, _V2_CPU_CONTROLLER) else None

    counter = _controller_in(with_controllers, probe=_V2.cpu_usage, is_v2=True) or _controller_in(
        hierarchies.v1.get(_V1_CPU_ACCT), probe=_V1.cpu_usage, is_v2=False
    )

    return counter or _controller_in(unified, probe=_V2.cpu_usage, is_v2=True)


def _carries_controller(hierarchy: _Hierarchy, name: str) -> bool:
    """Whether one named controller is bound to this cgroup2 hierarchy.

    The mount lists the bound controllers at its top, by name. A hybrid machine leaves that list empty when
    its cgroup v1 hierarchies claim every controller, and leaves in it whatever they did not claim.
    """
    try:
        return name in (hierarchy.mount_point / _V2_CONTROLLERS).read_text().split()
    except (OSError, ValueError):
        return False


def _cpu_usage_dir(directory: Path, source: Controller) -> Path | None:
    """Find where the CPU time of one level is counted, given that level in another hierarchy.

    Under cgroup v1 the quota and the accounting can be two hierarchies with two mount points, so the level
    is translated by its path below the mount point. Every chain ends at its own mount point, which is what
    makes that path comparable.

    The translation is textual, so the result has to be one of the levels this process belongs to. Two
    hierarchies can name different cgroups for the same process, and a group of the same name in the other
    hierarchy then belongs to somebody else - its counter would be read as ours. `None` in that case, and
    `None` where the level carries no counter: no rate can be measured either way.
    """
    usage = locate_controllers().cpu_usage
    if usage is None:
        return None

    try:
        relative = directory.relative_to(source.dirs[-1])
    except ValueError:
        return None

    translated = usage.dirs[-1] / relative
    if translated not in usage.dirs:
        return None

    return translated if _exists(translated / usage.names.cpu_usage) else None


def _read_working_set(controller: Controller, directory: Path) -> int | None:
    """Read the memory charged to one cgroup, in bytes. Excludes reclaimable file cache.

    The raw usage counts the page cache, which the kernel drops on demand. Subtracting the inactive file cache
    gives the working set - the figure `docker stats` and `kubectl top` report.
    """
    current = _read_counter(directory / controller.names.memory_usage)
    if current is None:
        return None

    # No fallback to the raw usage. That would count reclaimable cache as usage.
    inactive_file = _read_stat_value(directory / controller.names.memory_stat, controller.names.inactive_file)
    if inactive_file is None:
        return None

    return max(current - inactive_file, 0)


def _locate_controller(hierarchies: _Hierarchies, metric: _Metric) -> Controller | None:
    """Locate the directories carrying one metric. The cgroup v2 unified hierarchy wins.

    A hybrid system can mount both interfaces with only some controllers on the unified hierarchy, so each
    candidate counts only where the file it is probed by exists.
    """
    return _controller_in(hierarchies.unified, probe=metric.v2_probe, is_v2=True) or _controller_in(
        hierarchies.v1.get(metric.v1_controller), probe=metric.v1_probe, is_v2=False
    )


def _controller_in(hierarchy: _Hierarchy | None, *, probe: str, is_v2: bool) -> Controller | None:
    """Locate one controller in one hierarchy, if that hierarchy carries it at all.

    The whole chain is kept, not only the levels that carry the controller today. A cgroup gets a controller's
    files once its parent enables that controller for its children, and `systemctl set-property` does exactly
    that at runtime. Discovery happens once per process, so a chain trimmed at startup would hide such a limit
    for the lifetime of the process.
    """
    if hierarchy is None:
        return None

    dirs = _candidate_dirs(hierarchy)

    return Controller(is_v2=is_v2, dirs=dirs) if _first_with(dirs, probe) is not None else None


def _first_with(dirs: tuple[Path, ...], file_name: str) -> Path | None:
    """Find the closest level that carries one file, or `None` when no level does."""
    return next((directory for directory in dirs if _exists(directory / file_name)), None)


def _exists(path: Path) -> bool:
    """Whether a path exists.

    `Path.exists()` raises on a directory this process may not traverse. Only Python 3.14 turns that into
    `False`, and a cgroup chain can hold such a directory.
    """
    try:
        return path.exists()
    except OSError:
        return False


def _candidate_dirs(hierarchy: _Hierarchy) -> tuple[Path, ...]:
    """List the directories a controller's files can be read from. The cgroup of this process first."""
    parts = _own_path_within_mount(hierarchy)

    # Descend from the mount point one level at a time, keeping each. The walk starts there because nothing
    # above the mount belongs to the hierarchy.
    chain = [hierarchy.mount_point]
    for part in parts:
        chain.append(chain[-1] / part)

    # The cgroup of this process first, then its ancestors.
    return tuple(reversed(chain))


def _own_path_within_mount(hierarchy: _Hierarchy) -> tuple[str, ...]:
    """Spell the cgroup of this process as the path components below the mount point."""
    path = PurePosixPath(hierarchy.own_path)

    # A mount can expose just a subtree. The paths in `/proc/self/cgroup` then carry the mount root as a
    # prefix, which has to come off.
    if hierarchy.mount_root != '/':
        try:
            path = PurePosixPath('/') / path.relative_to(hierarchy.mount_root)
        except ValueError:
            # The mount does not cover the cgroup of this process. Only the top of the mount is readable.
            path = PurePosixPath('/')

    return path.parts[1:] if path.is_absolute() else path.parts


def _read_hierarchies() -> _Hierarchies:
    """Locate the mounted cgroup hierarchies and the cgroup this process belongs to in each.

    Raises:
        OSError: If `/proc/self/mountinfo` or `/proc/self/cgroup` cannot be read.
    """
    unified_path, controller_paths = _read_own_paths()
    mounts = _read_mounts()

    controllers: dict[str, _Hierarchy] = {}

    for mount in mounts:
        if mount.filesystem != 'cgroup':
            continue

        # A cgroup v1 mount names its controllers among its options, e.g. `rw,cpu,cpuacct`.
        for option in mount.options.split(','):
            own_path = controller_paths.get(option)
            if option not in _V1_CONTROLLER_NAMES or own_path is None:
                continue

            # A controller can be mounted more than once, so the same rule as for the unified hierarchy: a
            # mount that does not expose the cgroup of this process belongs to somebody else. The first mount
            # that does wins, because mounts are listed in the order they were made.
            hierarchy = _Hierarchy(mount_point=mount.point, mount_root=mount.root, own_path=own_path)
            if _covers_own_cgroup(hierarchy):
                controllers.setdefault(option, hierarchy)

    unified = _pick_unified(mounts, unified_path) if unified_path is not None else None

    return _Hierarchies(unified=unified, v1=controllers)


def _pick_unified(mounts: list[_Mount], own_path: str) -> _Hierarchy | None:
    """Pick the cgroup2 mount that exposes the cgroup of this process.

    The same hierarchy can be mounted more than once. An agent that watches the machine from inside a
    container bind-mounts the whole of it somewhere else, and a runtime can expose another subtree entirely.
    Taking the first line of the mount table would then report another cgroup as this one, so a mount counts
    only where it covers the cgroup of this process and that cgroup exists under it.

    Where several still qualify, the conventional mount point wins, and after that the order the mounts were
    made in. Where none does, there is no unified hierarchy to read: the cgroup v1 hierarchies are left to
    answer instead, and where there are none every reading is `None`. Both are better answers than the numbers
    of a cgroup that is not this one.
    """
    hierarchies = (
        _Hierarchy(mount_point=mount.point, mount_root=mount.root, own_path=own_path)
        for mount in mounts
        if mount.filesystem == 'cgroup2'
    )

    covering = [hierarchy for hierarchy in hierarchies if _covers_own_cgroup(hierarchy)]
    if not covering:
        return None

    conventional = [hierarchy for hierarchy in covering if hierarchy.mount_point == _CONVENTIONAL_MOUNT_POINT]

    return (conventional or covering)[0]


def _covers_own_cgroup(hierarchy: _Hierarchy) -> bool:
    """Whether one mount exposes the cgroup of this process, and that cgroup exists under it."""
    mount_root = PurePosixPath(hierarchy.mount_root)

    # A mount of a subtree only covers the cgroups below that subtree.
    if mount_root != PurePosixPath('/') and not PurePosixPath(hierarchy.own_path).is_relative_to(mount_root):
        return False

    return _exists(_candidate_dirs(hierarchy)[0])


def _read_mounts() -> list[_Mount]:
    """Read the mount table, skipping the lines that are not mount entries.

    Raises:
        OSError: If `/proc/self/mountinfo` cannot be read.
    """
    # `errors='replace'` because a foreign mount point with non-UTF-8 bytes must not hide our own cgroup.
    # Such a path decodes to something that matches no file, so only that mount drops out.
    lines = _PROC_SELF_MOUNTINFO.read_text(errors='replace').splitlines()

    return [mount for line in lines if (mount := _parse_mount(line)) is not None]


def _parse_mount(line: str) -> _Mount | None:
    """Read one line of the mount table, or `None` when it is not a mount entry."""
    # A variable number of optional fields sits before the ` - ` separator. Split on it first.
    before, separator, after = line.partition(' - ')
    if not separator:
        return None

    try:
        _mount_id, _parent_id, _device, root, point, *_ = before.split(' ')
        filesystem, _source, options, *_ = after.split(' ')
    except ValueError:
        return None

    # `/proc/self/cgroup` spells the same paths unescaped. Without this the two cannot be compared, and the
    # mount point cannot be opened either.
    return _Mount(root=_unescape(root), point=Path(_unescape(point)), filesystem=filesystem, options=options)


def _read_own_paths() -> tuple[str | None, dict[str, str]]:
    """Read the cgroup this process belongs to in the unified hierarchy and in each cgroup v1 one.

    Raises:
        OSError: If `/proc/self/cgroup` cannot be read.
    """
    unified: str | None = None
    controllers: dict[str, str] = {}

    for line in _PROC_SELF_CGROUP.read_text(errors='replace').splitlines():
        try:
            _hierarchy_id, controller_list, cgroup_path = line.split(':', 2)
        except ValueError:
            continue

        # The unified hierarchy is the entry with no controllers listed, spelled `0::<path>`.
        if not controller_list:
            unified = cgroup_path
            continue

        for controller in controller_list.split(','):
            # A cgroup v1 hierarchy without a controller carries a name instead, e.g. `name=systemd`.
            controllers[controller.removeprefix('name=')] = cgroup_path

    return unified, controllers


def _unescape(field: str) -> str:
    """Decode the octal escapes in a path field of `/proc/self/mountinfo`."""
    # The backslash goes last. Undoing it first would decode `\134040` into a space.
    return field.replace('\\040', ' ').replace('\\011', '\t').replace('\\012', '\n').replace('\\134', '\\')


def _read_cpu_quota_v2(directory: Path) -> float | None:
    """Read the cores allowed by a cgroup v2 `cpu.max` file, which holds the quota and its period.

    Raises:
        _UnreadableFileError: If the file is there and describes no bandwidth this module can use.
    """
    content = _read_control_file(directory / _V2.cpu_quota)
    if content is None:
        return None

    # An unlimited cgroup spells the quota as `max`, and keeps the period next to it.
    written, _separator, period_written = content.partition(' ')
    if written == _V2_UNLIMITED:
        return None

    try:
        quota, period = int(written), int(period_written)
    except ValueError:
        raise _UnreadableFileError(directory) from None

    # Neither can be zero or negative on a real kernel, and cgroup v2 has `max` for "no quota". Reading one as
    # a limit would hand out a quota of no cores at all, which every consumer would then divide by.
    if quota <= 0 or period <= 0:
        raise _UnreadableFileError(directory)

    return quota / period


def _read_cpu_quota_v1(directory: Path) -> float | None:
    """Read the cores allowed by the cgroup v1 quota and period files.

    Raises:
        _UnreadableFileError: If a file is there and describes no bandwidth this module can use.
    """
    quota = _read_limit(directory / _V1.cpu_quota)
    period = _read_limit(directory / _V1_CPU_PERIOD)
    if quota is None or period is None:
        return None

    # Unlike cgroup v2, this interface has no word for "no quota": it writes a negative number instead.
    if quota < 0:
        return None

    if quota == 0 or period <= 0:
        raise _UnreadableFileError(directory)

    return quota / period


def _read_text(path: Path) -> str | None:
    """Read a control file. `None` when it cannot be read at all."""
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return None


def _read_control_file(path: Path) -> str | None:
    """Read a control file, telling an absent one from an unreadable one. `None` when the level has no such file.

    Every reader of a limit goes through here, so the rule lives in one place: a level that carries no such
    file simply does not limit anything, while one that carries a file nobody can read limits something by an
    amount nothing here can name.

    Raises:
        _UnreadableFileError: If the file is there and cannot be read.
    """
    content = _read_text(path)
    if content is None and _exists(path):
        raise _UnreadableFileError(path.parent)

    return content


def _read_counter(path: Path) -> int | None:
    """Read a counter, best effort. `None` when it is missing or says anything but a number.

    A counter that cannot be read costs a rate, and nothing else: there is no looser value to fall back to,
    which is what makes best effort right here and wrong for a limit.
    """
    content = _read_text(path)
    if content is None:
        return None

    try:
        return int(content)
    except ValueError:
        return None


def _read_limit(path: Path) -> int | None:
    """Read a file holding a limit. `None` when the level sets none.

    Raises:
        _UnreadableFileError: If the file is there and holds no number this module can use. That is not the same
            as no limit, and `_UnreadableFileError` says why.
    """
    content = _read_control_file(path)
    if content is None or content == _V2_UNLIMITED:
        return None

    try:
        return int(content)
    except ValueError:
        raise _UnreadableFileError(path.parent) from None


def _read_stat_value(path: Path, key: str) -> int | None:
    """Read one entry of a control file holding `<key> <value>` lines."""
    try:
        with path.open() as file:
            for line in file:
                entry_key, _separator, value = line.partition(' ')
                if entry_key == key:
                    return int(value)
    except (OSError, ValueError):
        return None

    return None
