# shellcheck shell=bash
SCENARIO_DESC="a tighter cgroup created inside the container, so two levels carry different limits"
REQUIRES="engine"
INNER_STYLE=container

OUTER_MEMORY_BYTES=$((512 * 1024 * 1024))
SET_MEMORY_BYTES=$((256 * 1024 * 1024))

# The container gets one limit and the probe then puts itself under a tighter one, the shape kubelet produces
# and the only scenario here where two levels hold different limits - so the tightest one has to win.
#
# The dance is the kernel's "no internal processes" rule: a cgroup cannot both hold processes and delegate
# controllers to its children, so the shell first vacates the namespace root, then enables the memory
# controller for children, and only then moves into the tighter cgroup it created.
NESTED_SETUP='
mount -o remount,rw /sys/fs/cgroup 2>/dev/null || true
mkdir -p /sys/fs/cgroup/init /sys/fs/cgroup/inner
echo $$ > /sys/fs/cgroup/init/cgroup.procs
echo +memory > /sys/fs/cgroup/cgroup.subtree_control
echo '"$SET_MEMORY_BYTES"' > /sys/fs/cgroup/inner/memory.max
echo $$ > /sys/fs/cgroup/inner/cgroup.procs
'

scenario_exec() {
  container_probe "$NESTED_SETUP $1" --privileged -m "$OUTER_MEMORY_BYTES"
}
