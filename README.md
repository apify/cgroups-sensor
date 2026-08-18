# cgroups-sensor

Utility functions to measure resource limits from cgroups in scenarios where psutils is not sufficient.

Today the repository is a bench for that measurement: it runs a library's cgroup detection inside real
environment shapes — systemd scopes, containers, kubernetes pods — and prints what the library saw there. It
runs when a human triggers it and reports to a human; nothing is scheduled and nothing asserts on values.

## Running it

On GitHub: **Actions → bench → Run workflow**. Locally, on any cgroup-v2 Linux box with `uv`:

```bash
./run.sh                             # every scenario, results into results/
./run.sh bare docker-private         # only these two
BENCH_CPUS=2 IMG=fedora:41 ./run.sh  # a different budget, in another distro's userspace
python3 report.py results/           # the tables
python3 report.py results/ --check   # same, exit 1 if a probe failed
```

| knob | what it sets |
| --- | --- |
| `CRAWLEE_REPO`, `CRAWLEE_REF` | what to measure; installed as a source tarball, so the image needs no `git` |
| `BENCH_CPUS` | cores the CPU-limited scenarios ask for, default 1. They derive cpuset and quota from it, so run it twice: once below the host's core count and once equal to it, where a cpuset stops restricting anything |
| `IMG` | base image for the container scenarios, default the uv one. Any glibc distro works — uv and the interpreter are mounted in |
| `BENCH_PYTHON` | interpreter to measure on, default 3.13, so the image cannot change it |

Scenarios whose prerequisites are missing (docker, passwordless sudo, a systemd user manager, kind, enough
cores) are recorded as `skipped` with the reason; only a crashed probe reddens a run. The `k8s-*` scenarios
bring up a kind cluster named `cgroups-bench` and leave it running — `kind delete cluster --name cgroups-bench`.

## Files

| file | what it does |
| --- | --- |
| [run.sh](run.sh) | Runs each scenario and always writes a result, so a crash is a recorded row rather than a missing one. |
| [scenario-helpers.sh](scenario-helpers.sh) | The vocabulary scenarios are written in: prerequisite probes, cpuset derivation, the container and pod launchers. |
| [scenarios/](scenarios/) | One file per environment shape; every file here is a scenario. |
| [probe.py](probe.py) | Runs inside the prepared environment and prints one JSON object with what Crawlee sees there. |
| [wrap.py](wrap.py) | Folds the probe's output and the scenario's configuration into one result file. |
| [report.py](report.py) | Turns a results directory into the tables. `--check` is the only gate. |
| [.github/workflows/bench.yaml](.github/workflows/bench.yaml) | Manual trigger only, one job per CPU budget. |

## Scenarios

A scenario declares what it needs, what it configures, and how to run the probe in the environment it prepares.
The commands use the declared values rather than repeating them, so the report's `set` column is literally what
was applied.

```bash
SCENARIO_DESC="private cgroupns (docker's default), all three axes at once"
REQUIRES="engine min${BENCH_CPUS}cpu"   # unmet -> skipped, with this reason in the table

QUOTA=$(awk "BEGIN{print $BENCH_CPUS - 0.5}")

SET_MEMORY_BYTES=$((512 * 1024 * 1024))  # an axis left undeclared means the scenario restricts nothing
SET_CPU_CORES=$QUOTA                     # there, so the sensor reporting nothing is the right answer
SET_CPUSET_CORES=$BENCH_CPUS

scenario_exec() { container_probe "$1" -m "$SET_MEMORY_BYTES" --cpus "$QUOTA" --cpuset-cpus "$(cpuset_list)"; }
```

`scenario_exec` must keep its stdout pure probe JSON, so setup noise goes to stderr; an optional
`scenario_cleanup` runs in a trap. A new scenario is one such file — it is picked up and reported automatically.

| scenario | shape it exercises |
| --- | --- |
| `bare` | no limits at all, so every reading should fall back to host values |
| `systemd-own` | deep chain, limits on the process's own cgroup, cpuset delegated |
| `systemd-ancestor` | limit on an ancestor while the leaf carries no memory controller files |
| `docker-private` | private cgroupns (docker's default), all three axes at once |
| `docker-memory-only` | memory alone, CPU falls back to host |
| `docker-cpuset-only` | cpuset alone, memory falls back to host |
| `docker-host-ns` | `--cgroupns=host`, where the container sees the full chain instead of its own root |
| `docker-cgroup-parent` | limit on a parent cgroup, set outside the container (needs docker's systemd driver) |
| `k8s-limits` | a pod with container limits, as kubelet writes them |
| `k8s-no-limits` | a pod with nothing set, so every reading should fall back to the node's values |

## Results

One `results/<scenario>.json` per scenario with the scenario's `configured` values, its `status` (`ok`,
`probe_failed`, `skipped`) and everything the probe printed:

| section | contents |
| --- | --- |
| `sensor` | what `crawlee._utils.cgroup` reports: memory limit and working set, cpu quota, cpuset size, cpu time, and the levels it walked |
| `derived` | what `crawlee._utils.system` makes of that in `get_memory_info()` and `get_cpu_info()` |
| `evidence` | verbatim contents of the cgroup control files at every level, never interpreted |
| `host` | kernel, cores and RAM of the machine |
| `errors` | readings that raised; the value becomes `null` and the probe carries on |

`report.py` renders two tables. In the first, each limit has a `set` column next to a `read` one: a value under
`set` with a dash under `read` is a limit the sensor missed, and dashes under both mean the scenario restricts
nothing there and the sensor agrees. `mem in use` is the memory charged against the limit excluding reclaimable
page cache — what `docker stats` shows. The second table is the same run seen through `get_memory_info()` and
`get_cpu_info()`, where a limit either reaches the caller or falls back to host values. When a reading looks
wrong, `evidence` says who is at fault: the limit is in the control files, or it never got there.
