# shellcheck shell=bash
SCENARIO_DESC="a memory limit larger than the machine has RAM"
REQUIRES="systemd-user"
INNER_STYLE=local

# The memory twin of systemd-quota-above-host, and the axis where the library already guards itself: a limit at
# or above the host's total is treated as no limit, so the derived figures fall back to host values while the
# sensor still reports the number the file holds. Realistic wherever a container or pod is given a limit larger
# than the machine it landed on.
SET_MEMORY_BYTES=$(($(mem_total_bytes) + 1024 * 1024 * 1024))

UNIT="bench-memory-above-host-$$"

scenario_exec() {
  systemd-run --user --scope -q --unit "$UNIT" -p "MemoryMax=$SET_MEMORY_BYTES" sh -c "$1"
}
