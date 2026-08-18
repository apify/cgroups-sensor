# shellcheck shell=bash
SCENARIO_DESC="a cpu quota larger than the machine has cores"
REQUIRES="systemd-user unified"
INNER_STYLE=local

# Nothing rejects a quota above the machine's core count: systemd writes it as asked, and a kubernetes limit
# above node capacity does the same whenever the requests are small enough to schedule. Docker is the exception,
# it validates --cpus against nproc. The cores that exist are the real ceiling, so a derived "allowed cores"
# above them means the utilization ratio is divided by more than can ever be consumed.
QUOTA_CORES=$(($(ncpus) + 1))

SET_CPU_CORES=$QUOTA_CORES

UNIT="bench-quota-above-host-$$"

scenario_exec() {
  systemd-run --user --scope -q --unit "$UNIT" -p "CPUQuota=$((QUOTA_CORES * 100))%" sh -c "$1"
}
