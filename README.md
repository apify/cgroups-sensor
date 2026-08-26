# cgroups-sensor

Reports the CPU and memory limits that actually apply to the running process, read from cgroups.

## Why

Inside a container the usual answers describe the machine, not the process. `psutil.virtual_memory().total` reports the memory of the node, `os.cpu_count()` reports every core of it. A process that sizes a budget from those numbers keeps growing until the kernel kills it.

Reading the cgroup files directly is not enough either. The limit that applies is rarely written where the process is, and a cgroup that restricts nothing does not say so by leaving its limit file empty. A consumer that reads one file and takes it at face value believes it has 8 EB of memory.

This package handles both, and reports only what actually restricts the process. `None` means nothing visible to the process restricts it - the machine is then the honest answer, and `get_machine_cpu_count()` and `get_machine_memory_bytes()` below give it.

Off Linux every limit reads as `None`. Some examples below pair it with `psutil`, which this package does not require - it is there to answer what the machine is using, which is not a question about limits.

## How it reads a limit

A process does not sit in one cgroup. It sits in a chain of them: its own group, the group above that, and so on up to the root. A limit on any level of that chain restricts the process, and the kernel enforces whichever level is tightest. That level is often not the group of the process. Under Kubernetes the limit usually sits on the pod cgroup rather than the container's, and the `kubepods` slice above them holds the memory the node is allowed to hand out at all. Under systemd a scope carries no CPU quota of its own while the slice above it does.

This package therefore walks the whole chain and reports the tightest limit on it. Three things follow from that, and they run through everything below.

**A reported number can describe a level above this process.** Everything under that level shares the limit. The memory of sibling groups is charged against it, and its CPU quota throttles them all. `describe()` names the level each reading came from: `memory_limit_level` for the memory limit, `cpu_limit_level` for the CPU one.

**A limit that restricts nothing is dropped rather than reported.** An unrestricted cgroup still holds a limit file, and what it holds looks like a limit: cgroup v1 spells "no limit" as a sentinel near 2\*\*63, a CPU quota can exceed the cores of the machine, and a CPU set can cover every core of it. Every reading is therefore compared against the machine, and one that turns out to restrict nothing is dropped. The answer is then `None`, and `describe().notices` says which reading was dropped and why.

**A limit this process cannot see is not reported.** The chain is read through the cgroup filesystem as this process sees it, which is usually the whole tree. A container can instead be given a subtree, and a cgroup namespace can hide the levels above it - a limit there is still enforced, and nothing here can read it. A truncated mount is reported as a `*_MOUNT_HIDES_ANCESTORS` notice, but a namespace leaves nothing to detect, so the absence of that notice is not a guarantee.

## Use

Size a memory budget from the limit, and fall back to the machine when nothing limits you:

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

Size a worker pool the same way, from the cores you may use:

```python
import cgroups_sensor

cores = cgroups_sensor.get_cpu_limit() or cgroups_sensor.get_machine_cpu_count() or 1
workers = max(1, round(cores))
```

`get_machine_cpu_count()` reads the count from the kernel. `os.cpu_count()` and `psutil.cpu_count()` can report the process instead of the machine, which turns a real restriction into the whole node.

Measure the CPU load against what you may use, rather than against the machine. `CpuLoad` is a sampler: each `sample()` returns the load since the previous `sample()` on the same object, so the measurement window is however long you leave between calls:

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

`sample()` never waits: it reads the counter and compares it against the one it kept. Pacing the loop is the caller's job, as the `sleep` above does it, and a few seconds is a good gap - the kernel advances the counter in coarse steps, so over a shorter window a busy process can read as idle. Give each caller a `CpuLoad` of its own, or two of them consume each other's windows.

`None` means there is no ratio to report: the first call, nothing restricting the CPU, or two calls too close together.

The load is measured where the limit binds, which may be the slice above this process. An idle service inside a saturated slice therefore reports the slice's load, because that is what predicts its throttling. `describe().cpu_rate_level` names the level.

For a single measurement rather than a series, `get_cpu_used_ratio(interval)` waits out the window itself and `get_cpu_used_ratio_async(interval)` is its asyncio form. Both refuse an interval below 0.01 seconds.

Log what applies when a service starts, so a surprising number can be explained later:

```python
import logging

import cgroups_sensor

logger = logging.getLogger(__name__)
logger.info(f'resource limits: {cgroups_sensor.snapshot()}')

for notice in cgroups_sensor.describe().notices:
    logger.info(f'{notice.code}: {notice.message}')
```

## What it reports

Which call for which job: `get_memory_budget().available` to size a memory budget, `get_cpu_limit()` to size a pool, `CpuLoad().sample()` or `get_cpu_used_ratio()` for a load, `describe()` when a number looks wrong.

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

Do not divide `get_cpu_usage()` by `get_cpu_limit()`. The counter belongs to the group of this process and the limit can come from a level above it, so the two describe different scopes. `CpuLoad` pairs them correctly.

### The memory budget

`get_memory_budget()` returns a `MemoryBudget`, which carries four numbers:

| Attribute | Answers |
| --- | --- |
| `available` | how much more memory can be allocated before something kills this process |
| `limit` | the tightest limit on the chain, in bytes |
| `working_set` | the memory charged against that limit, reclaimable file cache excluded |
| `used_ratio` | `working_set / limit` |

`available` is the number to size a budget from, and the one kept exact: a kill follows from a distance, not from a ratio. `working_set` is whatever keeps that distance exact, so it need not match the content of any file - where several levels hold a limit, `limit - working_set` is the smallest distance to any of them. `used_ratio` follows from the pair, and is the approximate one.

The level those numbers describe may be above this process - the pod, or the slice. `describe().memory_limit_level` names it. Everything under that level is charged there, so `used_ratio` is the share of the level rather than of this process. `available` still answers for this process: the room left at that level is the room left here.

The working set excludes reclaimable file cache, as `docker stats` and `kubectl top` do. Those tools read one cgroup and never walk up, so they agree with this only while a single level holds a limit.

## What it does not do

It reports two facts about the machine and no more: the online cores and the total memory. They are what a reading is compared against, and what a consumer needs when it gets `None`. Everything else about the machine - free memory, load, per-process figures - is `psutil`'s job.

Process affinity stays out too. `taskset` narrows one process without narrowing the cgroup its CPU time is accounted to, so what it sets is not a limit on the group and is not reported here.

## Diagnostics

`describe()` answers why a number looks wrong. It repeats the readings themselves, so one dump says what was reported as well as why, and adds:

| Field | Carries |
| --- | --- |
| `raw_memory_limit`, `raw_cpu_quota`, `raw_cpu_set_size` | the values as the files spell them, before anything was dropped |
| `raw_memory_working_set` | the usage paired with the raw limit, derived the way `working_set` is |
| `memory_limit_level`, `cpu_limit_level` | the cgroup each reported limit came from |
| `cpu_rate_level` | the cgroup a CPU rate is measured in |
| `memory_source`, `cpu_quota_source`, `cpu_set_source`, `cpu_usage_source` | the interface each metric was read through, and the chain searched for it |
| `machine_memory_bytes`, `machine_cpu_count` | what the readings were compared against |
| `notices` | one entry per reading that was dropped |

`Source.levels` is the whole chain that was searched, not the levels that answered. Most levels on a chain hold no limit, which is normal - the chain is searched once and kept, because a limit can be written to any of them later. The level a reading did come from is `memory_limit_level` or `cpu_limit_level`.

A reading of `None` with no notice about it means nothing went wrong: the files were there, and no level held a limit. Only hard limits count - `memory.high` throttles reclaim rather than killing, so a cgroup can sit above it indefinitely and nothing here reports that.

`Source.interface` is an `Interface` member and a notice carries a `NoticeCode`. Branch on those rather than on the strings they print as:

| `NoticeCode` | What happened | Example |
| --- | --- | --- |
| `MEMORY_LIMIT_COVERS_MACHINE` | the limit is the memory of the machine or more, so it restricts nothing | a cgroup v1 container started without `-m`, whose limit file holds the sentinel near 2\*\*63 |
| `CPU_QUOTA_COVERS_MACHINE` | the quota allows the cores of the machine or more | `--cpus=8` on an 8-core host |
| `CPU_SET_COVERS_MACHINE` | the allowed set covers every core of the machine | a container with no `--cpuset-cpus`, whose effective set lists every core of the host |
| `MEMORY_USAGE_UNAVAILABLE` | a limit was found, but some level on the chain reports no usage to pair with it | an ancestor that holds a limit and no readable `memory.current` |
| `MACHINE_MEMORY_UNKNOWN` | the memory of the machine cannot be read, so a limit cannot be told apart from a sentinel | a sandbox that masks `/proc/meminfo` but leaves `/sys/fs/cgroup` in place |
| `CPU_USAGE_SCOPE_MISMATCH` | the level the CPU limit applies to counts no CPU time, so no rate can be measured | cgroup v1 with `cpu` and `cpuacct` on separate mounts, where the level holding the quota has no counterpart under the accounting one |
| `MEMORY_METRICS_UNAVAILABLE` | nothing here carries a memory limit at all | a host with no cgroup filesystem mounted, or one that is not Linux |
| `CPU_METRICS_UNAVAILABLE` | nothing here carries a CPU limit at all | the same |
| `MEMORY_MOUNT_HIDES_ANCESTORS` | the memory mount exposes only part of its hierarchy, so a limit above it is enforced but unreadable | a container given a subtree as its whole `/sys/fs/cgroup` |
| `CPU_MOUNT_HIDES_ANCESTORS` | a CPU mount exposes only part of its hierarchy | the same |
| `MEMORY_LIMIT_UNREADABLE` | a level holds a memory limit that says nothing usable, so what it enforces is unknown | a hand-built or mocked cgroup tree; no kernel writes such a value |
| `CPU_LIMIT_UNREADABLE` | the same for a CPU limit | the same, plus a `cpuset.cpus` no kernel writes, such as the overlapping `0-2,1-3` |
