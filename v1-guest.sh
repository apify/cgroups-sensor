#!/usr/bin/env bash
# Run the whole bench inside a guest booted on cgroup v1, and bring its results back.
#
# cgroup v1 cannot be produced on a modern host: a controller belongs to exactly one hierarchy, and on a
# unified system they all belong to v2 - mounting them as v1 fails with EBUSY even as root, and inside a user
# namespace v1 cannot be mounted at all. A guest kernel booted in legacy mode is the only way to see those code
# paths, so this is a lane rather than a scenario: the same run.sh runs inside, and every scenario that can run
# there exercises the v1 side of the library.
#
# Usage:  ./v1-guest.sh [results_dir]
#
# Environment:
#   V1_IMAGE_URL / V1_KERNEL_URL / V1_INITRD_URL   guest to boot (default: Ubuntu 22.04 cloud image)
#   V1_CMDLINE       kernel command line (default: legacy cgroup mode)
#   V1_SCENARIOS     what to run inside (default: everything the guest can do)
#   V1_MEMORY / V1_CPUS   guest size (default 4096 MB, 2 cores)
#   plus CRAWLEE_REPO, CRAWLEE_REF, BENCH_CPUS, which are passed through to the bench

set -eu

SENSOR_DIR=$(cd "$(dirname "$0")" && pwd)
RESULTS=${1:-results}

WORK=${V1_WORK:-/tmp/cgroups-bench-v1}
SSH_PORT=${V1_SSH_PORT:-2222}
GUEST_USER=bench

BASE=https://cloud-images.ubuntu.com/releases/22.04/release
V1_IMAGE_URL=${V1_IMAGE_URL:-$BASE/ubuntu-22.04-server-cloudimg-amd64.img}
V1_KERNEL_URL=${V1_KERNEL_URL:-$BASE/unpacked/ubuntu-22.04-server-cloudimg-amd64-vmlinuz-generic}
V1_INITRD_URL=${V1_INITRD_URL:-$BASE/unpacked/ubuntu-22.04-server-cloudimg-amd64-initrd-generic}

# systemd honours these up to v255; they are ignored from v256 and cgroup v1 is gone in v258, which is why the
# guest is Ubuntu 22.04 (systemd 249) rather than something newer. Booting the kernel directly, rather than
# through the image's own bootloader, is what lets the line be set from here without editing the image first.
#
# hybrid (the default) mounts the v1 controllers and a controller-less cgroup2 next to them, so the library has
# to notice that the unified hierarchy carries nothing and fall back per controller. legacy is plain v1.
case ${V1_MODE:-hybrid} in
  hybrid) CGROUP_ARGS="systemd.unified_cgroup_hierarchy=0" ;;
  legacy) CGROUP_ARGS="systemd.unified_cgroup_hierarchy=0 systemd.legacy_systemd_cgroup_controller=1" ;;
  *) echo "v1-guest: V1_MODE must be hybrid or legacy" >&2; exit 1 ;;
esac
V1_CMDLINE=${V1_CMDLINE:-"root=/dev/vda1 console=ttyS0 $CGROUP_ARGS"}

# kind needs a container runtime and a lot of memory; the k8s shapes are already covered on the v2 host.
V1_SCENARIOS=${V1_SCENARIOS:-"bare systemd-own systemd-ancestor systemd-quota-above-host systemd-memory-above-host docker-private docker-memory-only docker-cpuset-only docker-host-ns docker-nested-subgroup"}

CRAWLEE_REPO=${CRAWLEE_REPO:-https://github.com/Mantisus/crawlee-python}
CRAWLEE_REF=${CRAWLEE_REF:-container-limits}
BENCH_CPUS=${BENCH_CPUS:-1}

need() { command -v "$1" >/dev/null 2>&1 || { echo "v1-guest: $1 is required (apt install $2)" >&2; exit 1; }; }
need qemu-system-x86_64 qemu-system-x86
need cloud-localds cloud-image-utils
need ssh openssh-client
# Emulation works without any privileges but is slow enough to be useful only for checking the plumbing, so it
# has to be asked for by name rather than silently turning a five minute lane into a thirty minute one.
V1_ACCEL=${V1_ACCEL:-kvm}
if [ "$V1_ACCEL" = kvm ] && ! { [ -r /dev/kvm ] && [ -w /dev/kvm ]; }; then
  echo "v1-guest: /dev/kvm is not usable. On a GitHub runner:" >&2
  echo "  echo 'KERNEL==\"kvm\", GROUP=\"kvm\", MODE=\"0666\", OPTIONS+=\"static_node=kvm\"' | sudo tee /etc/udev/rules.d/99-kvm4all.rules" >&2
  echo "  sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=kvm" >&2
  echo "  locally: sudo usermod -aG kvm \$USER (then log in again), or run with V1_ACCEL=tcg to emulate" >&2
  exit 1
fi
[ "$V1_ACCEL" = kvm ] || echo "v1-guest: no KVM, emulating - expect this to be several times slower" >&2

mkdir -p "$WORK" "$RESULTS"

fetch() {  # url -> cached file, downloaded once per machine
  local url=$1 dest="$WORK/$(basename "$1")"
  [ -s "$dest" ] || { echo "v1-guest: downloading $(basename "$url")" >&2; curl -fsSL -o "$dest" "$url"; }
  echo "$dest"
}

IMAGE=$(fetch "$V1_IMAGE_URL")
KERNEL=$(fetch "$V1_KERNEL_URL")
INITRD=$(fetch "$V1_INITRD_URL")

# A throwaway overlay, so a run never dirties the downloaded image and every run starts clean.
DISK=$WORK/run.qcow2
rm -f "$DISK"
qemu-img create -q -f qcow2 -F qcow2 -b "$IMAGE" "$DISK" 8G

KEY=$WORK/id_ed25519
[ -s "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -f "$KEY"

cat > "$WORK/user-data" <<EOF
#cloud-config
users:
  - name: $GUEST_USER
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$KEY.pub")
package_update: false
packages:
  - docker.io
runcmd:
  # A user manager has to exist for the systemd --user scopes, and it does not start on its own for a user who
  # is only ever reached over ssh.
  - loginctl enable-linger $GUEST_USER
  - systemctl enable --now docker
EOF
cloud-localds "$WORK/seed.img" "$WORK/user-data"

echo "v1-guest: booting ${V1_IMAGE_URL##*/} with '$V1_CMDLINE'"
qemu-system-x86_64 \
  -accel "$V1_ACCEL" -m "${V1_MEMORY:-4096}" -smp "${V1_CPUS:-2}" -display none -serial file:"$WORK/console.log" \
  -kernel "$KERNEL" -initrd "$INITRD" -append "$V1_CMDLINE" \
  -drive file="$DISK",if=virtio,format=qcow2 \
  -drive file="$WORK/seed.img",if=virtio,format=raw \
  -nic user,hostfwd=tcp::"$SSH_PORT"-:22 \
  -pidfile "$WORK/qemu.pid" -daemonize

cleanup() {
  [ -s "$WORK/qemu.pid" ] && kill "$(cat "$WORK/qemu.pid")" 2>/dev/null
  rm -f "$WORK/qemu.pid"
}
trap cleanup EXIT

# scp spells the port -P and reads -p as "preserve timestamps", so the two need separate option strings.
SSH_OPTS="-i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
# shellcheck disable=SC2086
in_guest() { ssh $SSH_OPTS -p "$SSH_PORT" "$GUEST_USER@127.0.0.1" "$@"; }
# shellcheck disable=SC2086
copy_in() { scp -q $SSH_OPTS -P "$SSH_PORT" -r "$@" "$GUEST_USER@127.0.0.1:bench/"; }
# shellcheck disable=SC2086
copy_out() { scp -q $SSH_OPTS -P "$SSH_PORT" "$GUEST_USER@127.0.0.1:$1" "$2"; }

echo -n "v1-guest: waiting for ssh"
for _ in $(seq 1 120); do
  in_guest true 2>/dev/null && break
  echo -n .
  sleep 2
done
echo
in_guest true 2>/dev/null || {
  echo "v1-guest: the guest never came up; the last of its console output:" >&2
  tail -30 "$WORK/console.log" >&2
  exit 1
}

# v1 controllers each get a mount of their own, so counting both kinds says which mode the guest came up in:
# only cgroup mounts is legacy, both kinds is hybrid, only cgroup2 means the kernel line did not take.
echo -n 'v1-guest: guest is up, '
in_guest 'printf "cgroup mounts: %s v1, %s v2\n" \
  "$(grep -c " - cgroup " /proc/self/mountinfo || true)" "$(grep -c " - cgroup2 " /proc/self/mountinfo || true)"'

# ssh answers as soon as sshd is up, while cloud-init is still installing packages behind it.
echo "v1-guest: waiting for cloud-init to finish"
in_guest 'cloud-init status --wait >/dev/null 2>&1 || true'

in_guest 'mkdir -p bench'
copy_in "$SENSOR_DIR/run.sh" "$SENSOR_DIR/probe.py" "$SENSOR_DIR/wrap.py" "$SENSOR_DIR/report.py" \
  "$SENSOR_DIR/scenario-helpers.sh" "$SENSOR_DIR/scenarios" "$(command -v uv)"
in_guest 'sudo install -m 0755 bench/uv /usr/local/bin/uv'
in_guest 'getent group docker >/dev/null && sudo usermod -aG docker '"$GUEST_USER"' || true'

echo "v1-guest: running the bench inside"
BENCH_CMD="cd bench && CRAWLEE_REPO='$CRAWLEE_REPO' CRAWLEE_REF='$CRAWLEE_REF' BENCH_CPUS='$BENCH_CPUS' \
  ./run.sh $V1_SCENARIOS results/"
# The freshly granted docker group only applies to new logins, so borrow it for this command when it exists.
if in_guest 'getent group docker >/dev/null'; then
  in_guest "sg docker -c \"$BENCH_CMD\""
else
  in_guest "$BENCH_CMD"
fi

# shellcheck disable=SC2086
copy_out 'bench/results/*.json' "$RESULTS/"
in_guest 'sudo poweroff' 2>/dev/null || true

echo "v1-guest: results in $RESULTS"
