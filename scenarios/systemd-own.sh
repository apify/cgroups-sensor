# shellcheck shell=bash
SCENARIO_DESC="deep chain, limits on the process's own cgroup, cpuset delegated"
REQUIRES="sudo systemd-run min${BENCH_CPUS}cpu"
INNER_STYLE=local

# A system scope, not a user one: user slices do not delegate cpuset, so AllowedCPUs would be silently ignored.
# The quota is a core looser than the cpuset here, the mirror of docker-private, where it is tighter. It stops
# at the machine's core count: a quota above that is a different question, asked on purpose by
# systemd-quota-above-host, and letting this row drift into it would just duplicate that one.
QUOTA_CORES=$((BENCH_CPUS + 1))
[ "$QUOTA_CORES" -gt "$(ncpus)" ] && QUOTA_CORES=$(ncpus)

SET_MEMORY_BYTES=$((512 * 1024 * 1024))
SET_CPU_CORES=$QUOTA_CORES
SET_CPUSET_CORES=$BENCH_CPUS

UNIT="bench-systemd-own-$$"

# sudo strips the environment, so the probe needs PATH and a cache root can write to.
scenario_exec() {
  sudo systemd-run --scope -q --unit "$UNIT" \
    -p "MemoryMax=$SET_MEMORY_BYTES" -p "CPUQuota=$((QUOTA_CORES * 100))%" -p "AllowedCPUs=$(cpuset_list)" \
    env "PATH=$PATH" "HOME=/root" "UV_CACHE_DIR=$BENCH_UV_CACHE/root" sh -c "$1"
}

scenario_cleanup() { sudo systemctl stop "$UNIT.scope" 2>/dev/null || true; }
