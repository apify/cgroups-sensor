# shellcheck shell=bash
SCENARIO_DESC="pod without limits, inside the same kubepods hierarchy"
REQUIRES="kind"
INNER_STYLE=container
SCENARIO_TIMEOUT=900

# Same nesting as k8s-limits but nothing set, so every reading should fall back to the node's values.
scenario_exec() { k8s_probe "$1" '{}'; }

scenario_cleanup() { k8s_cleanup; }
