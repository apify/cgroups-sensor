# shellcheck shell=bash
SCENARIO_DESC="limit on a parent cgroup, set outside the container"
REQUIRES="engine sudo systemd-driver"
INNER_STYLE=container

SET_MEMORY_BYTES=$((512 * 1024 * 1024))

SLICE=bench-parent.slice
HOLDER=bench-parent-holder

# Under the systemd cgroup driver --cgroup-parent takes a slice, and a slice needs a live unit in it to exist.
# The limit goes on that slice; the container itself gets none, so the sensor has to walk up to find it.
scenario_exec() {
  sudo systemd-run -q --unit "$HOLDER" --slice "$SLICE" sleep infinity >&2
  sudo systemctl set-property --runtime "$SLICE" "MemoryMax=$SET_MEMORY_BYTES" >&2
  container_probe "$1" --cgroupns=host --cgroup-parent="$SLICE"
}

scenario_cleanup() { sudo systemctl stop "$HOLDER" "$SLICE" 2>/dev/null || true; }
