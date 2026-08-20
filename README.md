# cgroups-sensor

Reports the CPU and memory limits that actually apply to the running process, read from cgroups.

## Why

Inside a container the usual answers describe the machine, not the process. `psutil.virtual_memory().total` reports the memory of the node, `os.cpu_count()` reports every core of it. A process that sizes a budget from those numbers keeps growing until the kernel kills it.

Reading the cgroup files directly is not enough either. An unrestricted cgroup does not leave its limit empty: cgroup v1 spells it as a sentinel near 2\*\*63, a CPU quota can exceed the cores of the machine, and a CPU set can cover every core. A consumer that takes those at face value believes it has 8 EB of memory.

This package reads the files, walks the levels, and reports only what restricts the process. `None` means nothing restricts it - the machine is then the honest answer, and `get_machine_cpu_count()` and `get_machine_memory_bytes()` below give it.

Linux only, Python 3.10 or newer, and no dependencies. Off Linux every limit reads as `None`. Some examples below pair it with `psutil`, which this package does not require - it is there to answer what the machine is using, which is not a question about limits.

## Use

Size a memory budget from the limit, and from the machine only when nothing limits you:

```python
import psutil

import cgroups_sensor


def memory_budget() -> tuple[int, int]:
    """The total and the used memory a budget should be derived from, in bytes."""
    budget = cgroups_sensor.get_memory_budget()
    if budget is not None:
        return budget.limit, budget.working_set

    memory = psutil.virtual_memory()
    return memory.total, memory.total - memory.available
```

Size a worker pool from the cores you may use, and from the machine when nothing limits you:

```python
import cgroups_sensor

cores = cgroups_sensor.get_cpu_limit() or cgroups_sensor.get_machine_cpu_count() or 1
workers = max(1, round(cores))
```

`get_machine_cpu_count()` rather than `os.cpu_count()`: the latter honours the `PYTHON_CPU_COUNT` override, and under musl it reports the affinity of the process instead of the machine. `psutil.cpu_count()` has the same two problems. This one asks the kernel.

Measure the CPU load against what you may use, rather than against the machine. A cgroup reports consumed CPU time as a counter, so a rate needs two readings. `CpuLoad` keeps the previous one, which makes the window as long as the interval between calls and costs no waiting:

```python
import time

import psutil

import cgroups_sensor

load = cgroups_sensor.CpuLoad()

while True:
    used_ratio = load.sample()
    if used_ratio is None:
        used_ratio = psutil.cpu_percent() / 100
    ...
    time.sleep(5)
```

The loop has to pace itself, as the `sleep` above does: every call here returns at once where there is nothing to measure, and `sample()` never waits at all. For a single measurement there is `get_cpu_used_ratio(interval)`, which waits out the window itself, and `get_cpu_used_ratio_async(interval)` for asyncio. Keep the interval generous - the kernel updates the counter in coarse steps, so a tenth of a second can report a busy process as idle, and anything below 0.01 seconds is refused outright.

Log what applies when a service starts, so a surprising number can be explained later:

```python
import logging

import cgroups_sensor

logger = logging.getLogger(__name__)
logger.info('resource limits: %s', cgroups_sensor.snapshot())

for notice in cgroups_sensor.describe().notices:
    logger.info('%s: %s', notice.code, notice.message)
```

## What it reports

Which call for which job: `get_memory_budget().available` to size a memory budget, `get_cpu_limit()` to size a pool, `CpuLoad().sample()` or `get_cpu_used_ratio()` for a load, `describe()` when a number looks wrong. `get_cpu_usage()` is a raw counter - do not divide it by `get_cpu_limit()` yourself, because the limit can come from a level above this process and the two would describe different scopes; that is what `CpuLoad` is for.

| Call | Answers |
| --- | --- |
| `get_memory_budget()` | the memory limit and the memory charged against it, or `None` |
| `get_cpu_limit()` | how many cores may be used, or `None` |
| `get_cpu_usage()` | CPU seconds consumed so far, a counter that only grows |
| `CpuLoad().sample()` | the share of the allowed cores used since the previous call |
| `get_cpu_used_ratio(interval)` | the same, measured across one window it waits out |
| `get_cpu_used_ratio_async(interval)` | the same, for asyncio |
| `snapshot()` | all of the above except the ratio, taken at once |
| `describe()` | the readings again, where they came from, the raw values, and why any are missing |
| `get_machine_cpu_count()` | the cores the kernel lists as online, whatever this process may use |
| `get_machine_memory_bytes()` | the total memory of the machine |
| `clear_cache()` | forget the discovered files after the process was moved to another cgroup |

`MemoryBudget` carries `limit` and `working_set`, and reports `available` and `used_ratio` derived from them. `available` is the memory this process can still allocate before something kills it, and it is the number to size a budget from. Where several levels hold a limit, that distance is the smallest one along the chain, moved onto the tightest limit so the pair stays comparable. An out-of-memory kill follows from that distance rather than from a ratio, which is why it is the number kept exact.

`limit` and `working_set` describe the cgroup the limit was found at, and that is not always the cgroup of this process: a limit on a slice or a Kubernetes pod restricts everything under it, and the memory of everything under it is charged against it. Where that happens `used_ratio` is the share of that whole level, not of this process. `describe().memory_limit_level` names the level. `available` is unaffected: the room left there is the room left here.

The working set excludes reclaimable file cache, the same way `docker stats` and `kubectl top` do. Those tools read one cgroup and never walk up, so they agree with this only while a single level is visible.

The CPU works the same way. A quota often sits above the process - a systemd scope carries none of its own, and the slice above it does - and the kernel then throttles that whole level, siblings included. The load is therefore measured where the quota binds, not in the group of this process, which would report an idle service inside a saturated slice.

## What it does not do

It reports two facts about the machine and no more: the online cores and the total memory, which are the numbers the filters compare against and the ones a consumer needs when a reading is `None`. Anything else about the machine - free memory, load, per-process figures - is what `psutil` is for. It does not read the affinity of the process, because `taskset` narrows one process without narrowing the cgroup its CPU time is accounted to. It never logs: everything that was dropped, and why, is available from `describe()`.

## Diagnostics

`describe()` explains a reading that looks wrong. It carries the readings themselves, so one dump answers what was reported as well as why, and around them a `Source` per metric - the mechanism it was read through and the levels searched - the raw values before filtering, the machine it compared against, the levels the memory and the CPU limit actually came from, and a notice for every reading that is not there. A reading of `None` with no notice about it means the mechanism was there and nothing limited this process in a way that kills it - only hard limits are read, and `memory.high` throttles reclaim instead. The levels searched are not the levels a reading came from: a level carries no files until a limit is written there, and it is kept in the chain regardless.

`Source.interface` is an `Interface` member, and a notice carries a `NoticeCode`. Branch on those rather than on the strings they print as:

| `NoticeCode` | Meaning |
| --- | --- |
| `MEMORY_LIMIT_COVERS_MACHINE` | the limit is at least the memory of the machine |
| `MEMORY_USAGE_UNAVAILABLE` | a limit was found, but no usage to pair it with |
| `MACHINE_MEMORY_UNKNOWN` | the memory of the machine cannot be read, so a sentinel cannot be told apart |
| `CPU_QUOTA_COVERS_MACHINE` | the quota is at least the cores of the machine |
| `CPU_SET_COVERS_MACHINE` | the set covers every core of the machine |
| `CPU_USAGE_SCOPE_MISMATCH` | the level the CPU limit applies to counts no CPU time, so no rate can be measured there |
| `MEMORY_METRICS_UNAVAILABLE` | nothing here carries a memory limit at all, which is what a machine without cgroups looks like |
| `CPU_METRICS_UNAVAILABLE` | nothing here carries a CPU limit at all, for the same reasons |
| `MEMORY_LIMIT_UNREADABLE` | a level holds a memory limit that says nothing usable, so what it enforces is unknown |
| `CPU_LIMIT_UNREADABLE` | the same for a CPU limit |
