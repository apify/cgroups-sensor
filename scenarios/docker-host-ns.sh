# shellcheck shell=bash
SCENARIO_DESC="cgroupns=host, where the container sees the full chain instead of its own root"
REQUIRES="engine"
INNER_STYLE=container

SET_MEMORY_BYTES=$((512 * 1024 * 1024))

scenario_exec() { container_probe "$1" -m "$SET_MEMORY_BYTES" --cgroupns=host; }
