#!/usr/bin/env bash
# Bench driver. Runs scenarios sequentially and ALWAYS writes one result per scenario into the results
# directory - a crash becomes a recorded failure, never a missing row. Exit code is 0 unless the driver itself
# cannot start; judging the run is report.py's job (report.py --check).
#
# Usage:  ./run.sh [all|<name> [<name>...]] [results_dir]
#   A <name> matches a scenario file with or without its NN- prefix (e.g. "bare" or "00-bare").
#
# Environment:
#   CRAWLEE_REPO / CRAWLEE_REF   target to install (default: Mantisus/crawlee-python @ container-limits)
#   BENCH_CPUS                   CPU budget for the limited scenarios, in cores (default 1). Scenarios larger
#                                than the host skip. Run the bench once per budget to cover more than one size.
#   ENGINE                       container engine (default: docker)
#   SCENARIO_TIMEOUT             per-scenario timeout in seconds (default 300)

set -u

SENSOR_DIR=$(cd "$(dirname "$0")" && pwd)

RESULTS="results"
SELECTORS=()
for arg in "$@"; do
  if [ "$arg" = "all" ] || [ -f "$SENSOR_DIR/scenarios/$arg.sh" ]; then
    SELECTORS+=("$arg")
  else
    RESULTS="$arg"
  fi
done
[ ${#SELECTORS[@]} -gt 0 ] || SELECTORS=("all")

ENGINE=${ENGINE:-docker}
IMG=${IMG:-ghcr.io/astral-sh/uv:python3.13-bookworm}
PY3=${PY3:-$(command -v python3)}
UV_BIN=${UV_BIN:-$(command -v uv || echo uv)}
BENCH_UV_CACHE=${BENCH_UV_CACHE:-/tmp/cgroups-bench-uv-cache}
# uv downloads interpreters here rather than into its cache; shared so only the first container of a run pays.
BENCH_UV_PYTHON=${BENCH_UV_PYTHON:-/tmp/cgroups-bench-uv-python}
CRAWLEE_REPO=${CRAWLEE_REPO:-https://github.com/Mantisus/crawlee-python}
CRAWLEE_REF=${CRAWLEE_REF:-container-limits}
BENCH_CPUS=${BENCH_CPUS:-1}

export ENGINE IMG SENSOR_DIR BENCH_UV_CACHE BENCH_UV_PYTHON BENCH_CPUS UV_BIN
mkdir -p "$RESULTS" "$BENCH_UV_CACHE" "$BENCH_UV_PYTHON"

# A git+ dependency makes uv run git inside the container, and most base images do not ship one. GitHub serves
# the same tree as a tarball, so an image needs nothing but network. Anything else falls back to git+.
case "$CRAWLEE_REPO" in
  https://github.com/*) SOURCE="${CRAWLEE_REPO%.git}/archive/${CRAWLEE_REF}.tar.gz" ;;
  *) SOURCE="git+${CRAWLEE_REPO}@${CRAWLEE_REF}" ;;
esac

WITH_SPEC="crawlee @ $SOURCE"
# The interpreter is part of what gets measured, so it is pinned rather than left to whatever the image has.
PROBE_FLAGS="--source $SOURCE"

# The probe command, in container paths (scenarios mount $SENSOR_DIR at /sensor) and in local paths.
# --refresh-package makes uv re-fetch the ref instead of trusting a cached resolution, so a re-run after pushing
# to the branch never silently measures the previous commit.
UV_ARGS="run --no-project --python ${BENCH_PYTHON:-3.13} --refresh-package crawlee --with \"$WITH_SPEC\""
INNER_CONTAINER="uv $UV_ARGS python /sensor/probe.py $PROBE_FLAGS"
INNER_LOCAL="'$UV_BIN' $UV_ARGS python '$SENSOR_DIR/probe.py' $PROBE_FLAGS"

TARGET_JSON=$(printf '{"repo": "%s", "ref": "%s"}' "$CRAWLEE_REPO" "$CRAWLEE_REF")

echo "bench: $CRAWLEE_REPO @ $CRAWLEE_REF"
echo "  image: $IMG, cpu budget: $BENCH_CPUS of $(nproc 2>/dev/null || echo '?') cores"

selected() {
  local name=$1 sel
  for sel in "${SELECTORS[@]}"; do
    [ "$sel" = "all" ] || [ "$sel" = "$name" ] && return 0
  done
  return 1
}

run_one() {
  local file=$1
  local name
  name=$(basename "${file%.sh}")
  local work
  work=$(mktemp -d)

  (
    SCENARIO_NAME=$name
    CONTAINER="bench-$name"
    SCENARIO_DESC=""
    REQUIRES=""
    INNER_STYLE=local
    # What the scenario configures, per axis. The report prints each next to what the sensor read, so a value
    # left unset here means "this scenario deliberately restricts nothing on that axis".
    SET_MEMORY_BYTES="" SET_CPU_CORES="" SET_CPUSET_CORES=""

    . "$SENSOR_DIR/scenario-helpers.sh"
    . "$file"

    wrap() {
      "$PY3" "$SENSOR_DIR/wrap.py" \
        --scenario "$name" --desc "$SCENARIO_DESC" \
        ${SET_MEMORY_BYTES:+--set-memory "$SET_MEMORY_BYTES"} \
        ${SET_CPU_CORES:+--set-cpu "$SET_CPU_CORES"} \
        ${SET_CPUSET_CORES:+--set-cpuset "$SET_CPUSET_CORES"} \
        --target-json "$TARGET_JSON" --out "$RESULTS/$name.json" "$@"
    }

    unmet=$(unmet_requirement)
    if [ -n "$unmet" ]; then
      wrap --skip-reason "$unmet"
      exit 0
    fi

    trap 'scenario_cleanup' EXIT

    INNER=$INNER_LOCAL
    [ "$INNER_STYLE" = "container" ] && INNER=$INNER_CONTAINER

    # scenario_exec is a function, so `timeout` cannot wrap it directly; a watchdog in this shell kills a hang.
    # The watchdog is detached from stdout/stderr - otherwise its orphaned sleep holds the pipe open and a
    # `./run.sh | tee` style caller waits up to the full timeout for EOF after the run has finished.
    scenario_exec "$INNER" >"$work/probe.json" 2>"$work/run.err" &
    runpid=$!
    ( sleep "${SCENARIO_TIMEOUT:-300}" && kill "$runpid" 2>/dev/null ) >/dev/null 2>&1 </dev/null &
    watchdog=$!
    wait "$runpid"
    rc=$?
    kill "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null

    wrap --probe-file "$work/probe.json" --exit-rc "$rc" --stderr-file "$work/run.err"
  )

  rm -rf "$work"

  # Baseline between scenarios: a leaked busy-loop would distort the next scenario's CPU readings.
  leftovers=$(pgrep -f 'while :; do :; done' 2>/dev/null || true)
  if [ -n "$leftovers" ]; then
    echo "WARN: leftover busy-loop processes after $name, killing: $leftovers" >&2
    kill $leftovers 2>/dev/null || true
  fi
}

for file in "$SENSOR_DIR"/scenarios/*.sh; do
  name=$(basename "${file%.sh}")
  selected "$name" || continue
  run_one "$file"
done
