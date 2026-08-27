from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import _cgroup
from ._cpu_list import count_cpu_list

_PROC_MEMINFO = Path('/proc/meminfo')
"""Holds the total memory of the machine."""

_SYS_CPU_ONLINE = Path('/sys/devices/system/cpu/online')
"""Lists the cores of the machine that are online, e.g. `0-3`."""

_SHORTEST_WINDOW_SECONDS = 0.01
"""Below this a rate says more about the counter's own resolution than about the load.

The kernel advances the consumed time in steps of a scheduler tick or so. Two readings taken closer together
than that usually differ by nothing, which would read as an idle process however busy it is.
"""


@dataclass(frozen=True)
class MemoryBudget:
    """A memory limit that actually restricts this process, with the usage charged against it.

    `available` is what this is for: the memory this process can still allocate. That is the number to size a
    budget from, and the one this pair is built to keep exact.

    The other two describe the cgroup the limit was found at, which is not always the cgroup of this process -
    a limit on a slice or a pod restricts everything under it. Where that happens, `working_set` counts the
    memory of everything under that level and `used_ratio` is its share, not this process's.
    `describe().memory_limit_level` names the level, and `available` stays the honest answer either way: the
    room left there is the room left here.
    """

    limit: int
    """The tightest limit in bytes. Always below the memory of the machine."""

    working_set: int
    """The memory charged against the limit, in bytes. Excludes reclaimable file cache.

    Where several levels hold a limit, this is the smallest distance to one along the chain, moved onto the
    tightest limit so the pair stays comparable. An out-of-memory kill follows from that distance rather than
    from a ratio, which is why `available` is the number kept exact.
    """

    @property
    def available(self) -> int:
        """The memory this process can still allocate before something kills it, in bytes."""
        return self.limit - self.working_set

    @property
    def used_ratio(self) -> float:
        """The share of the limit in use, between 0 and 1.

        Of the limit, not of this process: where the limit belongs to a level above, sibling cgroups are
        charged against it too.
        """
        return self.working_set / self.limit if self.limit > 0 else 1.0


@dataclass(frozen=True)
class Snapshot:
    """Every reading the sensor takes, taken at once.

    Each reading answers on its own. The CPU counter belongs to the group of this process and the CPU limit
    can belong to a level above it, so the three are not a set of numbers to combine.
    """

    memory_budget: MemoryBudget | None
    """The memory budget, or `None` when nothing restricts the memory."""

    cpu_limit: float | None
    """The number of usable CPU cores, or `None` when nothing restricts the CPU."""

    cpu_usage: float | None
    """The consumed CPU time in seconds, or `None` when it cannot be read.

    Counted in the group of this process, which need not be the level `cpu_limit` applies to. Dividing one by
    the other is therefore not a load: `CpuLoad` and `get_cpu_used_ratio()` pair the two properly.
    """


class NoticeCode(str, Enum):
    """Why a reading was dropped or a rate could not be measured.

    Compare against these rather than against the strings they carry: the strings are what a log line shows,
    the members are what code should branch on.
    """

    # Without this a member prints as `NoticeCode.MEMORY_LIMIT_COVERS_MACHINE`, and an f-string of it differs
    # between the supported Python versions. `StrEnum` would do the same, and arrives only in 3.11.
    __str__ = str.__str__

    MEMORY_LIMIT_COVERS_MACHINE = 'memory-limit-covers-machine'
    """The limit is at least the memory of the machine, which is how an unrestricted group spells "no limit"."""

    MEMORY_USAGE_UNAVAILABLE = 'memory-usage-unavailable'
    """A limit was found, but some level on the chain reports no usage to pair with it."""

    MACHINE_MEMORY_UNKNOWN = 'machine-memory-unknown'
    """The memory of the machine cannot be read, so a limit cannot be told apart from a sentinel."""

    CPU_QUOTA_COVERS_MACHINE = 'cpu-quota-covers-machine'
    """The quota is at least the cores of the machine, so it restricts nothing."""

    CPU_SET_COVERS_MACHINE = 'cpu-set-covers-machine'
    """The set of allowed cores covers every core of the machine, so it restricts nothing."""

    CPU_USAGE_SCOPE_MISMATCH = 'cpu-usage-scope-mismatch'
    """The level the CPU limit applies to counts no CPU time, so no rate can be measured against it."""

    MEMORY_METRICS_UNAVAILABLE = 'memory-metrics-unavailable'
    """Nothing here carries a memory limit at all: not Linux, or no cgroup filesystem is mounted."""

    CPU_METRICS_UNAVAILABLE = 'cpu-metrics-unavailable'
    """Nothing here carries a CPU limit at all: not Linux, or no cgroup filesystem is mounted."""

    MEMORY_LIMIT_UNREADABLE = 'memory-limit-unreadable'
    """A level holds a memory limit that says nothing usable, so what it enforces is unknown."""

    CPU_LIMIT_UNREADABLE = 'cpu-limit-unreadable'
    """A level holds a CPU limit that says nothing usable, so what it enforces is unknown."""

    MEMORY_MOUNT_HIDES_ANCESTORS = 'memory-mount-hides-ancestors'
    """The memory mount exposes part of its hierarchy, so a limit above it is enforced but never read."""

    CPU_MOUNT_HIDES_ANCESTORS = 'cpu-mount-hides-ancestors'
    """A CPU mount exposes part of its hierarchy, so a limit above it is enforced but never read."""


@dataclass(frozen=True)
class Notice:
    """One reason a reading was dropped."""

    code: NoticeCode
    """What happened, as a member of `NoticeCode`. It carries its own string, so an f-string of it, a `%s` in
    a log line and `json.dumps` all show that string rather than the member."""

    message: str
    """A human-readable explanation with the values involved."""


class Interface(str, Enum):
    """The mechanism a reading comes from.

    A machine can serve different metrics through different interfaces, so this is reported per metric rather
    than once. Compare against these members rather than against the strings they carry.
    """

    # As in `NoticeCode`, so that a member prints as its string on every supported Python version.
    __str__ = str.__str__

    CGROUP_V2 = 'cgroup-v2'
    """The unified hierarchy, where one cgroup carries every controller."""

    CGROUP_V1 = 'cgroup-v1'
    """The older interface, where each controller is a hierarchy of its own."""


@dataclass(frozen=True)
class Source:
    """Where one metric is read from."""

    interface: Interface
    """The mechanism providing the metric. It carries its own string, as `Notice.code` does."""

    levels: tuple[str, ...]
    """The levels the metric is looked for in. The cgroup of this process first, then its ancestors.

    Most of a chain holds no limit, and the whole chain is kept regardless: a limit can be written to any level
    later, and discovery happens only once. `memory_limit_level` and `cpu_limit_level` name the levels the
    readings actually came from.
    """


@dataclass(frozen=True)
class Description:
    """How the sensor arrived at its readings.

    This is the diagnostic counterpart of `snapshot()`, and it carries the readings themselves, so that one
    dump answers what was reported as well as why. When a reading looks wrong, the description shows the
    mechanism, the levels, the raw values and the rejections.

    A reading of `None` next to no notice about it means the mechanism was there and nothing limited this
    process in a way that kills it. Only hard limits are read: `memory.high` throttles reclaim instead, so a
    cgroup can sit above it indefinitely and nothing here reports it.
    """

    memory_budget: MemoryBudget | None
    """What `get_memory_budget()` reports, taken at the same moment as everything below."""

    cpu_limit: float | None
    """What `get_cpu_limit()` reports, taken at the same moment as everything below."""

    cpu_usage: float | None
    """What `get_cpu_usage()` reports, taken at the same moment as everything below.

    Counted in the group of this process. A rate is measured at `cpu_rate_level` instead, so where that is
    another level, this counter and a rate that looks wrong do not describe the same scope. That is the first
    thing to check when they disagree.
    """

    memory_source: Source | None
    """Where the memory limit and usage are read from. `None` when no mechanism carries them."""

    cpu_quota_source: Source | None
    """Where the CPU bandwidth quota is read from. `None` when no mechanism carries it."""

    cpu_set_source: Source | None
    """Where the set of allowed cores is read from. `None` when no mechanism carries it."""

    cpu_usage_source: Source | None
    """Where the consumed CPU time is read from, as a mechanism and not as a level. `cpu_usage` says which
    group is counted and `cpu_rate_level` where a rate is measured. `None` when no mechanism carries it."""

    raw_memory_limit: int | None
    """The tightest memory limit before filtering. Sentinels included."""

    raw_memory_working_set: int | None
    """The usage paired with the raw limit, in bytes.

    Unlike the other `raw_` fields this one is computed, not read: `raw_memory_limit - raw_memory_working_set`
    is the smallest distance to a limit along the chain, moved onto that limit. It matches no single file. The
    reclaimable file cache comes off `memory.current`, and where several levels are visible the distance comes
    from whichever level is closest to its own limit. `None` when the tightest level has no usage to pair with
    it.
    """

    raw_cpu_quota: float | None
    """The CPU quota in cores before filtering. It can exceed the machine."""

    raw_cpu_set_size: int | None
    """The number of allowed cores before filtering. It can cover the whole machine."""

    memory_limit_level: str | None
    """The level holding `raw_memory_limit`. `None` when no level holds one.

    The tightest limit of the chain wins, which is often not the cgroup of this process. Under Kubernetes it
    is regularly the pod, or the `kubepods` slice holding what the node may hand out. The level is named even
    where the filters drop that limit, and a notice then says why.
    """

    cpu_limit_level: str | None
    """The level the reported CPU limit was read at. `None` when nothing restricts the CPU.

    Often not the cgroup of this process: a systemd scope carries no quota of its own and the slice above it
    does, and a CPU set on an ancestor restricts everything below it.
    """

    cpu_rate_level: str | None
    """The level whose consumed CPU time belongs to that limit, which is where a rate is measured.

    Under cgroup v2 it is `cpu_limit_level` itself. Under cgroup v1 the accounting is a controller of its own
    and can be mounted elsewhere, so it is the same cgroup under the other mount point. `None` when no level
    counts that time, and a notice then says so - no rate can be measured at all in that case.
    """

    machine_memory_bytes: int | None
    """The machine memory the filters compared against. `None` when it cannot be read."""

    machine_cpu_count: int | None
    """The machine core count the filters compared against. `None` when it cannot be read."""

    notices: tuple[Notice, ...]
    """Why readings were rejected. Empty when every reading passed."""


def get_memory_budget() -> MemoryBudget | None:
    """Get the memory budget that actually restricts this process.

    A limit that covers the whole machine is not reported. That is how an unrestricted group spells "no limit".
    A limit is also not reported when the machine memory is unknown, or when no usage metric can be paired with
    it. `describe()` explains every rejection.

    Returns:
        The budget, or `None` when nothing restricts the memory of this process.
    """
    return _evaluate_memory(get_machine_memory_bytes()).effective


def get_cpu_limit() -> float | None:
    """Get the number of CPU cores this process may actually use.

    A bandwidth quota and a CPU set restrict the CPU independently. The tighter one wins. A reading that covers
    the whole machine is ignored. Process affinity (`taskset`) is out of scope - it narrows one process, not its
    group. `describe()` shows the readings separately.

    Returns:
        The number of cores, possibly fractional. `None` when nothing restricts the CPU of this process.
    """
    return _evaluate_cpu_limit(get_machine_cpu_count()).effective


def get_cpu_usage() -> float | None:
    """Get the CPU time this process's group has consumed, in seconds.

    The value is a counter that only grows, read in the group of this process. Use it to see how much CPU time
    has been spent - not to compute a load: `get_cpu_limit()` can come from a level above this process, and
    dividing this counter by that limit compares two different scopes. `CpuLoad` and `get_cpu_used_ratio()`
    pair both correctly, and they are what a rate should come from.

    Returns:
        The cumulative CPU time. `None` when it cannot be read.
    """
    return _cgroup.read_cpu_usage()


def get_cpu_used_ratio(interval: float = 1.0) -> float | None:
    """Measure the CPU usage relative to the cores this process may use.

    Blocks the calling thread while measuring, but only while measuring: where there is nothing to measure it
    returns at once, so a loop around it has to pace itself. In asyncio code use `get_cpu_used_ratio_async()`.

    The kernel updates the counter in coarse steps, so a short window is noisy: a tenth of a second can report
    an idle process as busy or a busy one as idle. The default is long enough for that to average out. When
    sampling repeatedly, use `CpuLoad` instead - it measures across the time between calls and waits for
    nothing.

    Args:
        interval: How long to measure, in seconds. A tenth of a second is already noisy, and anything below
            0.01 is refused: the counter does not move that fast, so such a window can only report nothing.

    Returns:
        The ratio between 0 and 1. `None` when nothing restricts the CPU, when the counter cannot be read, or
        when the limit changed while measuring, in value or in level.

    Raises:
        ValueError: If `interval` is shorter than a measurable window. An argument is the caller's to get
            right, so it is refused rather than answered with `None`. `CpuLoad.sample()` returns `None` for a
            window that turned out too short, because there the window is what happened, not what was asked
            for.
    """
    _check_interval(interval)

    start = _read_cpu()
    if start is None:
        return None

    time.sleep(interval)

    end = _read_cpu()

    return _used_ratio(start, end) if end is not None else None


async def get_cpu_used_ratio_async(interval: float = 1.0) -> float | None:
    """Measure the CPU usage relative to the cores this process may use.

    The asyncio variant of `get_cpu_used_ratio()`. Waits with `asyncio.sleep`, so the event loop stays free.
    It waits only while measuring: where there is nothing to measure it returns at once, so a loop around it
    has to pace itself, or it spins. The same accuracy applies, and `CpuLoad` avoids the wait entirely.

    Args:
        interval: How long to measure, in seconds. The same floor of 0.01 applies.

    Returns:
        The ratio between 0 and 1. `None` when nothing restricts the CPU, when the counter cannot be read, or
        when the limit changed while measuring, in value or in level.

    Raises:
        ValueError: If `interval` is shorter than a measurable window.
    """
    _check_interval(interval)

    # Imported here: it costs a third of what this package costs to import, and only this one call needs it.
    import asyncio  # noqa: PLC0415

    start = _read_cpu()
    if start is None:
        return None

    await asyncio.sleep(interval)

    end = _read_cpu()

    return _used_ratio(start, end) if end is not None else None


def snapshot() -> Snapshot:
    """Take every reading at once.

    Returns:
        The memory budget, the CPU limit and the CPU usage counter. The rate is not included - it needs a
        measurement window.
    """
    return Snapshot(
        memory_budget=get_memory_budget(),
        cpu_limit=get_cpu_limit(),
        cpu_usage=get_cpu_usage(),
    )


def describe() -> Description:
    """Explain how the sensor arrives at its readings.

    Every control file is read again, so a limit changed between two calls shows up here. Where those files
    are was discovered once - `clear_cache()` is what forgets that.

    Returns:
        The readings, the source of each metric, the raw values before filtering, the machine facts, and a
        notice for every reading that is not there.
    """
    controllers = _cgroup.locate_controllers()

    # One read of the machine facts for both the report and the filtering. They cannot disagree this way.
    machine_memory = get_machine_memory_bytes()
    machine_cpus = get_machine_cpu_count()
    memory = _evaluate_memory(machine_memory)
    cpu = _evaluate_cpu_limit(machine_cpus)

    return Description(
        memory_budget=memory.effective,
        cpu_limit=cpu.effective,
        cpu_usage=get_cpu_usage(),
        memory_source=_source(controllers.memory),
        cpu_quota_source=_source(controllers.cpu_quota),
        cpu_set_source=_source(controllers.cpu_set),
        cpu_usage_source=_source(controllers.cpu_usage),
        raw_memory_limit=memory.raw.limit,
        raw_memory_working_set=memory.raw.working_set,
        raw_cpu_quota=cpu.raw_quota,
        raw_cpu_set_size=cpu.raw_set_size,
        memory_limit_level=str(memory.raw.limit_directory) if memory.raw.limit_directory is not None else None,
        cpu_limit_level=str(cpu.limit_directory) if cpu.limit_directory is not None else None,
        cpu_rate_level=str(cpu.usage_directory) if cpu.usage_directory is not None else None,
        machine_memory_bytes=machine_memory,
        machine_cpu_count=machine_cpus,
        notices=memory.notices + cpu.notices + _hidden_ancestor_notices(controllers),
    )


def _hidden_ancestor_notices(controllers: _cgroup.Controllers) -> tuple[Notice, ...]:
    """Name the mounts that expose a subtree, per metric reading through them.

    A metric is named separately because the two can be read through different mounts, and only one of them
    may be truncated.
    """
    cpu = (controllers.cpu_quota, controllers.cpu_set, controllers.cpu_usage)

    return _truncated_mounts(NoticeCode.MEMORY_MOUNT_HIDES_ANCESTORS, (controllers.memory,)) + _truncated_mounts(
        NoticeCode.CPU_MOUNT_HIDES_ANCESTORS, cpu
    )


def _truncated_mounts(code: NoticeCode, located: tuple[_cgroup.Controller | None, ...]) -> tuple[Notice, ...]:
    """One notice per mount among these controllers that covers only part of its hierarchy."""
    # The last directory is the mount point, where the walk had to stop. The root says how much it covers.
    truncated = {(str(c.dirs[-1]), c.mount_root) for c in located if c is not None and c.mount_root != '/'}

    return tuple(
        Notice(code=code, message=f'{point} exposes only {root}, so a limit above it is enforced but unreadable')
        for point, root in sorted(truncated)
    )


def clear_cache() -> None:
    """Forget the discovered metric sources.

    Discovery is cached for the lifetime of the process. Call this after the process was moved into another
    group. The next reading then locates the sources again.
    """
    _cgroup.clear_cache()


@dataclass(frozen=True)
class _CpuReading:
    """One reading of the CPU counter, with what it has to be compared against."""

    limit: float
    usage: float
    taken_at: float
    usage_directory: Path | None
    """The level the counter was read at, so a later reading can tell that the limit moved."""


def _check_interval(interval: float) -> None:
    """Reject a measurement window the counter cannot resolve.

    Raises:
        ValueError: If `interval` is shorter than a measurable window. The public callers document it.
    """
    # Anything shorter would sleep and then report nothing, because the counter has not moved yet.
    if interval < _SHORTEST_WINDOW_SECONDS:
        raise ValueError(
            f'interval must be at least {_SHORTEST_WINDOW_SECONDS} seconds, got {interval}. '
            'The kernel advances the consumed time in coarser steps than that.'
        )


def _read_cpu() -> _CpuReading | None:
    """Read the CPU limit and the consumed time at the level that limit binds at."""
    evaluation = _evaluate_cpu_limit(get_machine_cpu_count())
    if evaluation.effective is None or evaluation.usage_directory is None:
        return None

    usage = _cgroup.read_cpu_usage(evaluation.usage_directory)
    if usage is None:
        return None

    return _CpuReading(
        limit=evaluation.effective,
        usage=usage,
        taken_at=time.monotonic(),
        usage_directory=evaluation.usage_directory,
    )


def _used_ratio(start: _CpuReading, end: _CpuReading) -> float | None:
    """Turn two readings into the share of the allowed cores that was used between them."""
    # A limit that moved level, or changed in place, makes the two readings incomparable: the counters then
    # belong to two scopes, or the same consumption divides by two different numbers.
    if end.usage_directory != start.usage_directory or end.limit != start.limit:
        return None

    elapsed = end.taken_at - start.taken_at
    if elapsed < _SHORTEST_WINDOW_SECONDS or end.limit <= 0:
        return None

    used_ratio = (end.usage - start.usage) / (elapsed * end.limit)

    # A counter restart makes the difference negative. Clamp both ends.
    return min(max(used_ratio, 0.0), 1.0)


class CpuLoad:
    """Measures the CPU load between calls, without blocking.

    A cgroup reports consumed CPU time as a counter, so a rate needs two readings. This keeps the previous one
    and measures against it, which makes the window as long as the interval between calls. That is what makes
    it accurate: a short window is dominated by how coarsely the kernel updates the counter, and a sampler
    called every few seconds does not pay for that.

    Give each caller a sampler of its own. Two callers sharing one measure each other's windows, and a window
    of nearly no time reports nothing at all.

    Safe to call from several threads, which is what makes the sampling of a worker thread safe next to a
    `clear_cache()` elsewhere.
    """

    def __init__(self) -> None:
        self._previous: _CpuReading | None = None
        self._lock = threading.Lock()

    def sample(self) -> float | None:
        """Measure the load since the previous call.

        Returns:
            The ratio between 0 and 1. `None` on the first call, when nothing restricts the CPU, when the
            counter cannot be read, when the limit changed since the previous call, or when that call was too
            recent for the counter to have moved.
        """
        reading = _read_cpu()

        with self._lock:
            previous, self._previous = self._previous, reading if reading is not None else self._previous

        if reading is None or previous is None:
            return None

        return _used_ratio(previous, reading)


@dataclass(frozen=True)
class _MemoryEvaluation:
    """The memory reading with the judgement applied."""

    effective: MemoryBudget | None
    raw: _cgroup.RawMemory
    notices: tuple[Notice, ...]


_COVERS_MACHINE_MESSAGES = {
    NoticeCode.CPU_QUOTA_COVERS_MACHINE: 'The CPU quota of {cores} cores is at least the cores of the machine '
    '({machine_cpus}), so it does not restrict this process.',
    NoticeCode.CPU_SET_COVERS_MACHINE: 'The set of {cores} allowed cores covers every core of the machine '
    '({machine_cpus}), so it does not restrict this process.',
}
"""How each CPU reading explains itself away where it covers the machine.

Kept apart from the reading so that the sentence is built only for a reading that is dropped. `CpuLoad.sample()`
evaluates the CPU limit on every call, and nothing there ever reads these.
"""


def _spell_cores(cores: float) -> str:
    """Spell a number of cores for a message.

    A quota can allow half a core, so the number is a float throughout. A whole number of them is not written
    as a fraction: a set of cores has no fractional size at all, and "64.0 allowed cores" reads as a bug.
    """
    # Not `float.is_integer()`: an `int` satisfies this annotation, and only Python 3.12 gives `int` that
    # method. The remainder answers for both, and for a value no arithmetic here can produce anyway.
    return str(int(cores)) if cores % 1 == 0 else str(cores)


@dataclass(frozen=True)
class _CpuRestriction:
    """One CPU restriction as read, with the notice that explains it away where it covers the machine."""

    cores: float
    limit_directory: Path
    """The level this restriction was read at."""

    usage_directory: Path | None
    """Where the CPU time this restriction applies to is counted. `None` when nothing counts it."""

    code: NoticeCode


@dataclass(frozen=True)
class _CpuEvaluation:
    """The CPU readings with the judgement applied."""

    effective: float | None
    limit_directory: Path | None
    """The level `effective` was read at. `None` when nothing restricts the CPU."""

    usage_directory: Path | None
    """Where the consumed CPU time has to be read to match `effective`.

    `None` when no level counts the time this limit applies to, and therefore no rate can be measured. It is
    never a stand-in for the group of this process: that group is named like any other.
    """

    raw_quota: float | None
    raw_set_size: int | None
    notices: tuple[Notice, ...]


def _evaluate_memory(machine_memory: int | None) -> _MemoryEvaluation:
    """Judge the raw memory reading."""
    raw = _cgroup.read_memory()
    rejection = _memory_rejection(raw, machine_memory)

    if rejection is not None:
        return _MemoryEvaluation(effective=None, raw=raw, notices=(rejection,))

    if raw.limit is None or raw.working_set is None:
        # Nothing to report and nothing to explain: the mechanism is there and no level limits anything. The
        # second test cannot be true here - `_memory_rejection` has already answered for it - and it stays
        # because it is what narrows the type below.
        return _MemoryEvaluation(effective=None, raw=raw, notices=())

    return _MemoryEvaluation(effective=MemoryBudget(limit=raw.limit, working_set=raw.working_set), raw=raw, notices=())


def _memory_rejection(raw: _cgroup.RawMemory, machine_memory: int | None) -> Notice | None:
    """Say why the raw memory reading cannot be reported. `None` where it can.

    Each rule here drops a limit that would mislead a consumer, and names what was dropped.
    """
    if raw.unreadable_directory is not None:
        # Every level that did answer is looser than the one that did not, so there is nothing safe to report.
        return Notice(
            code=NoticeCode.MEMORY_LIMIT_UNREADABLE,
            message=f'The memory limit of {raw.unreadable_directory} cannot be read, so a tighter limit than '
            'any this process can see may apply and nothing is reported.',
        )

    if raw.limit is None:
        # No limit and no mechanism read the same way from the outside, and only one of them is a fact about
        # this machine. A consumer that expected a limit needs to know which it is looking at.
        return (
            Notice(
                code=NoticeCode.MEMORY_METRICS_UNAVAILABLE,
                message='No mechanism here carries a memory limit, so nothing was read. This is what a '
                'machine without cgroups looks like.',
            )
            if _cgroup.locate_controllers().memory is None
            else None
        )

    if machine_memory is None:
        # Without the machine memory, a real limit and a v1 "unlimited" sentinel look the same.
        return Notice(
            code=NoticeCode.MACHINE_MEMORY_UNKNOWN,
            message=f'The memory of the machine cannot be read, so the limit of {raw.limit} bytes cannot be '
            'told apart from an "unlimited" sentinel and is not reported.',
        )

    if raw.limit >= machine_memory:
        # This is how an unrestricted group spells "no limit". The exact sentinel differs between runtimes.
        return Notice(
            code=NoticeCode.MEMORY_LIMIT_COVERS_MACHINE,
            message=f'The memory limit of {raw.limit} bytes is at least the memory of the machine '
            f'({machine_memory} bytes), so it does not restrict this process.',
        )

    if raw.working_set is None:
        return Notice(
            code=NoticeCode.MEMORY_USAGE_UNAVAILABLE,
            message=f'Found a memory limit of {raw.limit} bytes but no usage metric to pair it with, so the '
            'limit is not reported.',
        )

    return None


def _evaluate_cpu_limit(machine_cpus: int | None) -> _CpuEvaluation:
    """Judge the raw CPU readings. The tighter of the quota and the set wins.

    A reading is trusted when the machine core count is unknown. CPU has no sentinel: an absent quota or set is
    an absent file, so whatever was read is a number a human configured.
    """
    raw = _cgroup.read_cpu()

    if raw.unreadable_directory is not None:
        # As for memory: what that level enforces is unknown, and every level that answered is looser. Both
        # readings are already empty here, and this is before them so that nothing has to rely on that.
        return _CpuEvaluation(
            effective=None,
            limit_directory=None,
            usage_directory=None,
            raw_quota=None,
            raw_set_size=None,
            notices=(
                Notice(
                    code=NoticeCode.CPU_LIMIT_UNREADABLE,
                    message=f'The CPU limit of {raw.unreadable_directory} cannot be read, so a tighter limit '
                    'than any this process can see may apply and nothing is reported.',
                ),
            ),
        )

    quota, cpu_set = raw.quota, raw.cpu_set

    restrictions = []

    if quota is not None:
        restrictions.append(
            _CpuRestriction(
                cores=quota.cores,
                limit_directory=quota.limit_directory,
                usage_directory=quota.usage_directory,
                code=NoticeCode.CPU_QUOTA_COVERS_MACHINE,
            )
        )

    if cpu_set is not None:
        restrictions.append(
            _CpuRestriction(
                cores=float(cpu_set.cores),
                limit_directory=cpu_set.limit_directory,
                usage_directory=cpu_set.usage_directory,
                code=NoticeCode.CPU_SET_COVERS_MACHINE,
            )
        )

    notices = []
    candidates = []

    if not restrictions and _no_cpu_mechanism():
        # As for memory: "nothing limits the CPU here" and "nothing here can say" are different answers.
        notices.append(
            Notice(
                code=NoticeCode.CPU_METRICS_UNAVAILABLE,
                message='No mechanism here carries a CPU limit, so nothing was read. This is what a machine '
                'without cgroups looks like.',
            )
        )

    for restriction in restrictions:
        if machine_cpus is not None and restriction.cores >= machine_cpus:
            message = _COVERS_MACHINE_MESSAGES[restriction.code]
            notices.append(
                Notice(
                    code=restriction.code,
                    message=message.format(cores=_spell_cores(restriction.cores), machine_cpus=machine_cpus),
                )
            )
        else:
            candidates.append(restriction)

    tightest = min(candidates, key=lambda restriction: restriction.cores) if candidates else None

    # A limit whose level counts no CPU time still limits, and is still reported. Only the rate is impossible,
    # and saying so is the whole point of the notice: the alternative is a ratio taken from another scope.
    if tightest is not None and tightest.usage_directory is None:
        notices.append(
            Notice(
                code=NoticeCode.CPU_USAGE_SCOPE_MISMATCH,
                message=f'The CPU limit of {_spell_cores(tightest.cores)} cores applies to a level whose '
                'consumed CPU time cannot be read, so no rate can be measured against it.',
            )
        )

    return _CpuEvaluation(
        effective=tightest.cores if tightest is not None else None,
        limit_directory=tightest.limit_directory if tightest is not None else None,
        usage_directory=tightest.usage_directory if tightest is not None else None,
        raw_quota=quota.cores if quota is not None else None,
        raw_set_size=cpu_set.cores if cpu_set is not None else None,
        notices=tuple(notices),
    )


def _no_cpu_mechanism() -> bool:
    """Whether nothing on this machine carries a CPU limit of either kind."""
    controllers = _cgroup.locate_controllers()

    return controllers.cpu_quota is None and controllers.cpu_set is None


def _source(controller: _cgroup.Controller | None) -> Source | None:
    """Spell one located controller as a source."""
    if controller is None:
        return None

    return Source(
        interface=Interface.CGROUP_V2 if controller.is_v2 else Interface.CGROUP_V1,
        levels=tuple(str(directory) for directory in controller.dirs),
    )


def get_machine_memory_bytes() -> int | None:
    """Read the total memory of the machine, in bytes.

    This is the number the filters compare a limit against, and it is here so that a consumer that got `None`
    from `get_memory_budget()` has the other half of the answer without reaching for a second library.

    Containers normally see `/proc/meminfo` unvirtualized, so this is the memory of the node rather than of the
    container. A runtime that virtualizes it (lxcfs) makes the limit and the "machine" coincide, and the limit
    is then reported as no restriction.

    Returns:
        The total memory, or `None` when it cannot be read - which is what happens off Linux.
    """
    try:
        for line in _PROC_MEMINFO.read_text().splitlines():
            key, _separator, value = line.partition(':')
            if key == 'MemTotal':
                # The value carries a unit, e.g. `8054932 kB`.
                return int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None

    return None


def get_machine_cpu_count() -> int | None:
    """Read the number of online CPU cores of the machine.

    This is the number the filters compare a quota or a set against, and the reason it is public: the usual
    answers describe the process instead. `os.cpu_count()` honors the `PYTHON_CPU_COUNT` override, and under
    musl both it and `psutil.cpu_count()` report the affinity of the process. Either would make a real
    restriction look like the whole machine. The kernel is asked directly here.

    Returns:
        The online cores, or `None` when even the fallbacks say nothing.
    """
    try:
        online = _SYS_CPU_ONLINE.read_text().strip()
    except (OSError, ValueError):
        return _machine_cpu_count_fallback()

    return count_cpu_list(online) or _machine_cpu_count_fallback()


def _machine_cpu_count_fallback() -> int | None:
    """Count the cores where the kernel does not list them."""
    try:
        count = os.sysconf('SC_NPROCESSORS_ONLN')
    except (AttributeError, OSError, ValueError):
        return os.cpu_count()

    return count if count > 0 else os.cpu_count()
