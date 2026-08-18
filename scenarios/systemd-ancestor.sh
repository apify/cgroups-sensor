# shellcheck shell=bash
SCENARIO_DESC="limit on an ancestor, leaf without memory controller files"
REQUIRES="systemd-user"
INNER_STYLE=local

SET_MEMORY_BYTES=$((512 * 1024 * 1024))

# The scope carries the limit; Delegate=yes lets the probe move into a child cgroup that inherits no memory
# controller files at all, so the sensor has to walk up past a level that has nothing to read.
scenario_exec() {
  INNER="$1" systemd-run --user --scope -q --unit "bench-ancestor-$$" \
    -p "MemoryMax=$SET_MEMORY_BYTES" -p Delegate=yes bash -c '
      own=$(grep "^0::" /proc/self/cgroup | cut -d: -f3)
      mkdir -p "/sys/fs/cgroup$own/leaf"
      echo $$ > "/sys/fs/cgroup$own/leaf/cgroup.procs"
      exec sh -c "$INNER"
    '
}
