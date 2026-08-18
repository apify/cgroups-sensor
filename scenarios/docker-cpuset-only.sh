# shellcheck shell=bash
SCENARIO_DESC="cpuset alone, memory falls back to host"
REQUIRES="engine min${BENCH_CPUS}cpu"
INNER_STYLE=container

SET_CPUSET_CORES=$BENCH_CPUS

scenario_exec() { container_probe "$1" --cpuset-cpus "$(cpuset_list)"; }
