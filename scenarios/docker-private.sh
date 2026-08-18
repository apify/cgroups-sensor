# shellcheck shell=bash
SCENARIO_DESC="private cgroupns (docker's default), all three axes at once"
REQUIRES="engine min${BENCH_CPUS}cpu"
INNER_STYLE=container

# Half a core tighter than the cpuset, so the row also shows which of the two CPU axes wins.
QUOTA=$(awk "BEGIN{print $BENCH_CPUS - 0.5}")

SET_MEMORY_BYTES=$((512 * 1024 * 1024))
SET_CPU_CORES=$QUOTA
SET_CPUSET_CORES=$BENCH_CPUS

scenario_exec() { container_probe "$1" -m "$SET_MEMORY_BYTES" --cpus "$QUOTA" --cpuset-cpus "$(cpuset_list)"; }
