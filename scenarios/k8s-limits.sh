# shellcheck shell=bash
SCENARIO_DESC="pod with container limits, read from inside its own cgroup namespace"
REQUIRES="kind"
INNER_STYLE=container
SCENARIO_TIMEOUT=900   # the first k8s scenario of a run brings the cluster up

# Half a core tighter than the budget, as in docker-private. Requests are kept small on purpose: kubernetes
# copies the limits into the requests when none are given, and a pod requesting every core of the node is one
# the scheduler can never place.
MILLICORES=$((BENCH_CPUS * 1000 - 500))

SET_MEMORY_BYTES=$((512 * 1024 * 1024))
SET_CPU_CORES=$(awk "BEGIN{print $BENCH_CPUS - 0.5}")

# kubelet nests a pod several levels under kubepods, but the container gets a cgroup namespace of its own, so
# from inside the chain is one level deep and the limit sits on it. The walk up to an ancestor is what
# docker-cgroup-parent covers; here what matters is that kubelet's shape is read correctly at all.
scenario_exec() {
  k8s_probe "$1" "$(printf '{"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "%s", "cpu": "%sm"}}' \
    "$SET_MEMORY_BYTES" "$MILLICORES")"
}

scenario_cleanup() { k8s_cleanup; }
