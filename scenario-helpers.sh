# shellcheck shell=bash
# Shared helpers for scenario files. Sourced by run.sh inside each scenario's subshell, before the scenario file.

BENCH_CLUSTER=${BENCH_CLUSTER:-cgroups-bench}
# A pod cannot mount the host's uv binary, so the k8s scenarios use an image that already carries one,
# independently of the $IMG the container scenarios run on.
K8S_IMAGE=${K8S_IMAGE:-ghcr.io/astral-sh/uv:python3.13-bookworm}
K8S_POD=bench-probe
K8S_CM=bench-probe

have() { command -v "$1" >/dev/null 2>&1; }

# $BENCH_CPUS is the run's CPU budget: how many cores the limited scenarios are given. Scenarios derive both
# their cpuset and their quota from it, so the same files stay meaningful on a 2-core runner and on a bigger
# host - and running the bench twice with different budgets is how the CPU axes get covered at more than one
# size. A budget larger than the host makes the scenario skip (see min<N>cpu), never silently measure nothing.
cpuset_list() { [ "$BENCH_CPUS" -le 1 ] && echo '0' || echo "0-$((BENCH_CPUS - 1))"; }

have_engine() { "$ENGINE" info >/dev/null 2>&1; }
have_sudo() { sudo -n true 2>/dev/null; }
have_systemd_user() { systemd-run --user --scope -q true 2>/dev/null; }
cgroup_driver() { "$ENGINE" info -f '{{.CgroupDriver}}' 2>/dev/null; }
ncpus() { nproc 2>/dev/null || getconf _NPROCESSORS_ONLN; }

# Prints the first unmet requirement from $REQUIRES, or nothing when all are met.
unmet_requirement() {
  local req n
  for req in $REQUIRES; do
    case "$req" in
      engine)       have_engine       || { echo "container engine '$ENGINE' is not available"; return; } ;;
      sudo)         have_sudo         || { echo "passwordless sudo is not available"; return; } ;;
      systemd-user) have_systemd_user || { echo "systemd user manager is not available"; return; } ;;
      systemd-driver)
        [ "$(cgroup_driver)" = systemd ] || { echo "$ENGINE uses the '$(cgroup_driver)' cgroup driver, this scenario needs 'systemd'"; return; } ;;
      kind)
        have kind && have kubectl && have_engine || { echo "kind, kubectl and a container engine are needed to bring up a cluster"; return; } ;;
      min*cpu)      n=${req#min}; n=${n%cpu}
                    [ "$(ncpus)" -ge "$n" ] || { echo "needs at least $n CPUs, host has $(ncpus)"; return; } ;;
      *)            have "$req"       || { echo "command '$req' is not available"; return; } ;;
    esac
  done
}

# Default so run.sh can call it unconditionally. Scenarios override when they have something to clean up.
scenario_cleanup() { :; }

# Run the probe command ($1) inside a fresh container; the remaining arguments are engine run flags. The engine
# is $ENGINE, which a scenario can set for itself, so rows from different engines can share a pass. uv and its
# downloaded interpreters are mounted in rather than baked into the image, so $IMG can be any glibc distro -
# fedora, rocky, debian - and only the first container of a run pays for the download.
container_probe() {
  local inner=$1
  shift

  # A cache directory per engine. A rootful container writes into it as real root, and a rootless one - whose
  # own root is this user - then cannot touch those files at all, so the two must never share.
  local cache="$BENCH_UV_CACHE/$ENGINE" interpreters="$BENCH_UV_PYTHON/$ENGINE"
  mkdir -p "$cache" "$interpreters"

  local mounts=(
    -v "$SENSOR_DIR:/sensor:ro"
    -v "$cache:/root/.cache/uv"
    -v "$interpreters:/root/.local/share/uv/python"
  )
  # The host's uv goes to a directory appended to PATH, never over the image's own copy: an image that ships uv
  # keeps using it, and only an image without one falls back to the mounted binary.
  [ -x "$UV_BIN" ] && mounts+=(-v "$UV_BIN:/opt/bench/uv:ro")

  "$ENGINE" run --rm --name "$CONTAINER" "${mounts[@]}" "$@" "$IMG" \
    sh -c 'PATH="$PATH:/opt/bench"; '"$inner"
}

# Create the bench's kind cluster if it is not up yet, and make sure the probe image is on its node.
ensure_kind_cluster() {
  if ! kind get clusters 2>/dev/null | grep -qx "$BENCH_CLUSTER"; then
    kind create cluster --name "$BENCH_CLUSTER" --wait 120s
  fi
  "$ENGINE" image inspect "$K8S_IMAGE" >/dev/null 2>&1 || "$ENGINE" pull "$K8S_IMAGE"
  kind load docker-image "$K8S_IMAGE" --name "$BENCH_CLUSTER"
}

# The script the pod runs. `kubectl logs` merges the container's stdout and stderr, so the probe's diagnostics
# are kept in a file and printed only when it fails - otherwise uv's progress output would land in the JSON.
# It always exits 0: a pod that fails takes the full wait to report it, and a missing JSON body already says so.
k8s_run_script() {
  cat <<EOF
$1 >/tmp/probe.json 2>/tmp/probe.err
rc=\$?
if [ \$rc -eq 0 ]; then
  cat /tmp/probe.json
else
  echo "probe exited \$rc"
  cat /tmp/probe.err
fi
exit 0
EOF
}

# Wait for the pod to finish, printing its phase. Polling rather than `kubectl wait --for=jsonpath` so that a
# quirk of one kubectl version cannot silently return at once and leave the logs unread. A pod the scheduler
# cannot place would otherwise sit there for the whole timeout, so that case ends the wait immediately.
k8s_wait() {
  local phase reason seconds=0

  while [ "$seconds" -lt "${K8S_WAIT_SECONDS:-300}" ]; do
    phase=$(kubectl get "pod/$K8S_POD" -o jsonpath='{.status.phase}' 2>/dev/null)
    case "$phase" in
      Succeeded | Failed) break ;;
    esac

    if [ "$seconds" -ge 5 ]; then
      reason=$(kubectl get "pod/$K8S_POD" \
        -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].reason}' 2>/dev/null)
      if [ "$reason" = Unschedulable ]; then
        echo "pod cannot be scheduled - its requests do not fit this node"
        break
      fi
    fi

    sleep 1
    seconds=$((seconds + 1))
  done

  echo "pod phase: ${phase:-unknown} after ${seconds}s"
  [ "$phase" = Succeeded ] || kubectl describe "pod/$K8S_POD"
}

# Run the probe command ($1) in a one-shot pod whose container resources are the JSON object in $2, and print
# the pod's stdout. probe.py and the command itself travel as a ConfigMap, so nothing has to be quoted into
# YAML. Everything except the pod's own output goes to stderr, or it would end up mixed into the probe's JSON.
k8s_probe() {
  local inner=$1 resources=$2

  {
    ensure_kind_cluster
    kubectl delete pod "$K8S_POD" --ignore-not-found --now
    kubectl create configmap "$K8S_CM" \
      --from-file=probe.py="$SENSOR_DIR/probe.py" --from-literal=run.sh="$(k8s_run_script "$inner")" \
      --dry-run=client -o yaml | kubectl apply -f -

    kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $K8S_POD
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: $K8S_IMAGE
      command: ["sh", "/sensor/run.sh"]
      resources: $resources
      volumeMounts:
        - name: sensor
          mountPath: /sensor
  volumes:
    - name: sensor
      configMap:
        name: $K8S_CM
EOF

    k8s_wait
  } >&2

  # A copy of the pod's output goes to stderr, so when it is not JSON the result file still shows what it was.
  kubectl logs "$K8S_POD" | tee /dev/stderr
}

k8s_cleanup() {
  kubectl delete pod "$K8S_POD" --ignore-not-found --now >/dev/null 2>&1 || true
  kubectl delete configmap "$K8S_CM" --ignore-not-found >/dev/null 2>&1 || true
}
